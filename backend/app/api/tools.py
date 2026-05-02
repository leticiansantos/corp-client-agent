"""
Tools registry API.

Each environment's tools_config lives in that environment's own workspace:
  dev     → {dev_catalog}.{dev_schema}.tools_config     (dev workspace)
  staging → {staging_catalog}.{staging_schema}.tools_config (staging workspace)
  prod    → {prod_catalog}.{prod_schema}.tools_config   (prod workspace)

This matches how corp_agent_framework's ConfigDrivenAgent reads tools at runtime.
"""

import time

from databricks.sdk import WorkspaceClient
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from app.api.db import _execute_sql, _rows_to_dicts, _esc  # _execute_sql used for workspace_envs lookup
from app.config import settings

router = APIRouter(prefix="/api")

ENVS = ("dev", "staging", "prod")

_TOOLS_CONFIG_DDL = """
    CREATE TABLE IF NOT EXISTS {prefix}.tools_config (
        tool_name   STRING NOT NULL,
        kind        STRING NOT NULL,
        ref         STRING NOT NULL,
        description STRING,
        owner       STRING,
        environment STRING,
        status      STRING NOT NULL,
        created_at  TIMESTAMP,
        created_by  STRING,
        approved_by STRING,
        approved_at TIMESTAMP
    )
    USING DELTA
    COMMENT 'Registro de tools do corp-agent-framework.'
"""


# ── Env workspace helpers ──────────────────────────────────────
def _get_env_config(env: str) -> dict:
    """
    Read workspace config for the given environment from workspace_envs.
    Propagates HTTPException (auth/warehouse errors) so the caller sees the real problem.
    Returns {} only when the table or row doesn't exist yet.
    """
    try:
        resp = _execute_sql(
            f"SELECT workspace_url, catalog, schema_name, warehouse_id, token "
            f"FROM {settings.uc_prefix}.workspace_envs WHERE env = '{_esc(env)}'"
        )
        rows = _rows_to_dicts(resp)
        return rows[0] if rows else {}
    except HTTPException:
        raise  # propagate: warehouse down, missing credentials, etc.
    except Exception:
        return {}  # table not created yet — silent fallback is fine


def _workspace_client_for_env(cfg: dict) -> WorkspaceClient:
    """Build a WorkspaceClient from an env config dict. Falls back to .env credentials."""
    host  = (cfg.get("workspace_url") or settings.databricks_host).rstrip("/")
    token = cfg.get("token") or ""
    if token:
        return WorkspaceClient(host=host, token=token)
    if settings.databricks_client_id and settings.databricks_client_secret:
        return WorkspaceClient(
            host=host,
            client_id=settings.databricks_client_id,
            client_secret=settings.databricks_client_secret,
        )
    return WorkspaceClient(host=host, token=settings.databricks_token)


def _env_client(env: str) -> tuple[WorkspaceClient, str, str, str] | None:
    """
    Returns (WorkspaceClient, catalog, schema_name, warehouse_id) for an env.
    Returns None if host or warehouse_id are not available.
    Catalog and schema fall back to app defaults only when not configured for this env.
    """
    cfg = _get_env_config(env)
    host         = (cfg.get("workspace_url") or "").rstrip("/")
    warehouse_id = cfg.get("warehouse_id") or ""
    catalog      = cfg.get("catalog") or ""
    schema_name  = cfg.get("schema_name") or ""

    # Dev only: host and warehouse can fall back to .env defaults
    if env == "dev":
        host         = host or settings.databricks_host.rstrip("/")
        warehouse_id = warehouse_id or settings.databricks_warehouse_id

    if not host or not warehouse_id:
        return None

    # Catalog and schema fall back to corp_agent_framework defaults when not configured
    catalog     = catalog     or settings.framework_catalog
    schema_name = schema_name or settings.framework_schema

    w = _workspace_client_for_env({**cfg, "workspace_url": host})
    return w, catalog, schema_name, warehouse_id


def _sql(w: WorkspaceClient, warehouse_id: str, statement: str):
    """Execute SQL on any workspace, polling until SUCCEEDED/FAILED."""
    try:
        response = w.statement_execution.execute_statement(
            warehouse_id=warehouse_id,
            statement=statement.strip(),
            wait_timeout="50s",
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Erro ao executar SQL: {exc}",
        ) from exc

    deadline = time.time() + 300
    while True:
        state = str(response.status.state) if response.status else "UNKNOWN"
        state_up = state.upper()
        if "SUCCEEDED" in state_up:
            return response
        if any(s in state_up for s in ("FAILED", "CANCELED", "CLOSED")):
            error_msg = (
                response.status.error.message
                if (response.status and response.status.error)
                else state
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"SQL falhou (state={state}): {error_msg}",
            )
        if time.time() > deadline:
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                detail=f"SQL timeout (state={state})",
            )
        time.sleep(2)
        try:
            response = w.statement_execution.get_statement(response.statement_id)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Erro ao verificar status: {exc}",
            ) from exc


def _ensure_tools_table(w: WorkspaceClient, warehouse_id: str, prefix: str) -> None:
    """Create schema + tools_config table if they don't exist, and migrate missing columns."""
    try:
        _sql(w, warehouse_id, f"CREATE SCHEMA IF NOT EXISTS {prefix}")
    except Exception:
        pass  # schema may already exist
    _sql(w, warehouse_id, _TOOLS_CONFIG_DDL.format(prefix=prefix))
    # Migrate older tables that may be missing columns added after initial creation.
    # Use plain ADD COLUMN (without IF NOT EXISTS) for broader version compatibility.
    # "Column already exists" errors are caught and ignored.
    _MIGRATIONS = [
        f"ALTER TABLE {prefix}.tools_config ADD COLUMN environment STRING",
        f"ALTER TABLE {prefix}.tools_config ADD COLUMN created_by  STRING",
        f"ALTER TABLE {prefix}.tools_config ADD COLUMN approved_by STRING",
        f"ALTER TABLE {prefix}.tools_config ADD COLUMN approved_at TIMESTAMP",
    ]
    for stmt in _MIGRATIONS:
        try:
            _sql(w, warehouse_id, stmt)
        except Exception:
            pass  # column already exists → ok


_ENV_RANK = {"dev": 0, "staging": 1, "prod": 2}
# Highest-priority search order for operations that must pick one record
_ENVS_DESC = ("prod", "staging", "dev")


def _find_tool(tool_name: str) -> tuple[WorkspaceClient, str, str] | None:
    """
    Search envs from highest to lowest (prod→staging→dev).
    Returns (WorkspaceClient, warehouse_id, prefix) for the highest env that has the tool,
    or None if not found in any env.
    """
    for env in _ENVS_DESC:
        parts = _env_client(env)
        if parts is None:
            continue
        w, catalog, schema_name, warehouse_id = parts
        prefix = f"{catalog}.{schema_name}"
        try:
            resp = _sql(w, warehouse_id,
                f"SELECT tool_name FROM {prefix}.tools_config "
                f"WHERE tool_name = '{_esc(tool_name)}' LIMIT 1")
            if _rows_to_dicts(resp):
                return w, warehouse_id, prefix
        except Exception:
            continue
    return None


# ── List tools (all envs) ─────────────────────────────────────
@router.get("/tools")
def list_tools():
    """
    Aggregate tools from all configured environment workspaces.
    When the same tool_name appears in multiple envs, keep the highest-env record
    (prod > staging > dev).
    """
    # key → (env_rank, row)
    best: dict[str, tuple[int, dict]] = {}

    for env in ENVS:
        parts = _env_client(env)
        if parts is None:
            continue
        w, catalog, schema_name, warehouse_id = parts
        prefix = f"{catalog}.{schema_name}"
        rank = _ENV_RANK[env]
        try:
            resp = _sql(w, warehouse_id, f"""
                SELECT tool_name, kind, ref, description, owner, status, environment, created_at
                FROM {prefix}.tools_config
                ORDER BY created_at DESC
            """)
            for row in _rows_to_dicts(resp):
                key = row.get("tool_name", "")
                if not key:
                    continue
                row["environment"] = row.get("environment") or env
                current_rank, _ = best.get(key, (-1, {}))
                if rank > current_rank:
                    best[key] = (rank, row)
        except Exception:
            # Table may not exist yet in this env — skip
            pass

    # Return sorted: prod tools first, then staging, then dev; within each env by name
    all_tools = [row for _, row in sorted(best.values(), key=lambda x: (-x[0], x[1].get("tool_name", "")))]
    return {"tools": all_tools}


# ── Register tool (always in dev) ────────────────────────────
class RegisterToolRequest(BaseModel):
    tool_name: str
    kind: str
    ref: str
    description: str
    owner: str = "platform-team@empresa.com"
    environment: str = "dev"


@router.post("/tools", status_code=status.HTTP_201_CREATED)
def register_tool(body: RegisterToolRequest):
    """Register a new tool into the dev environment's tools_config."""
    parts = _env_client("dev")
    if parts is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Ambiente dev não configurado. "
                "Defina workspace_url e warehouse_id em Settings → Workspace Dev."
            ),
        )
    w, catalog, schema_name, warehouse_id = parts
    prefix = f"{catalog}.{schema_name}"

    # Verify we're not accidentally using the app workspace defaults
    dev_cfg = _get_env_config("dev")
    if not dev_cfg.get("catalog") or not dev_cfg.get("schema_name"):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"Catalog e schema_name não configurados para o ambiente dev em Settings. "
                f"Usando fallback: {prefix}. "
                f"Configure catalog e schema_name em Settings → Workspace Dev."
            ),
        )

    _ensure_tools_table(w, warehouse_id, prefix)

    merge_sql = f"""
    MERGE INTO {prefix}.tools_config AS target
    USING (
      SELECT
        '{_esc(body.tool_name)}'   AS tool_name,
        '{_esc(body.kind)}'        AS kind,
        '{_esc(body.ref)}'         AS ref,
        '{_esc(body.description)}' AS description,
        '{_esc(body.owner)}'       AS owner,
        'dev'                      AS environment
    ) AS source
    ON target.tool_name = source.tool_name
    WHEN MATCHED THEN UPDATE SET
        kind = source.kind, ref = source.ref,
        description = source.description, owner = source.owner
    WHEN NOT MATCHED THEN INSERT (
        tool_name, kind, ref, description, owner, environment,
        status, created_at, created_by
    ) VALUES (
        source.tool_name, source.kind, source.ref, source.description,
        source.owner, 'dev', 'pending_review', current_timestamp(), source.owner
    )
    """
    _sql(w, warehouse_id, merge_sql)
    return {"tool_name": body.tool_name, "status": "pending_review"}


# ── Workspace host (dev environment) ─────────────────────────
@router.get("/workspace-host")
def get_workspace_host():
    cfg  = _get_env_config("dev")
    host = (cfg.get("workspace_url") or settings.databricks_host).rstrip("/")
    return {"host": host}


# ── List Genie rooms (dev workspace) ─────────────────────────
@router.get("/genie-rooms")
def list_genie_rooms():
    """Returns all Genie spaces from the dev workspace configured in Settings."""
    parts = _env_client("dev")
    if parts is None:
        raise HTTPException(status_code=503, detail="Ambiente dev não configurado.")
    w, _, _, _ = parts
    cfg  = _get_env_config("dev")
    host = (cfg.get("workspace_url") or settings.databricks_host).rstrip("/")

    for path in ["/api/2.0/genie/spaces", "/api/2.0/genie/rooms"]:
        try:
            data = w.api_client.do("GET", path)
        except Exception as exc:
            err = str(exc)
            if "404" in err or "NOT_FOUND" in err.upper():
                continue
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Erro ao listar espaços Genie: {exc}",
            ) from exc

        raw = (
            data.get("genie_spaces")
            or data.get("spaces")
            or data.get("rooms")
            or []
        )
        rooms = [
            {
                "id": r.get("space_id") or r.get("id", ""),
                "name": r.get("title") or r.get("display_name") or r.get("name", ""),
                "mcp_url": f"{host}/api/2.0/mcp/genie/{r.get('space_id') or r.get('id', '')}",
            }
            for r in raw
        ]
        return {"rooms": rooms, "host": host}

    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail="Endpoint de Genie não encontrado. Use o ID manual.",
    )


# ── List Vector Search indexes (dev workspace) ────────────────
@router.get("/vector-search-indexes")
def list_vector_search_indexes():
    """Lists all Vector Search indexes in the dev workspace/catalog."""
    parts = _env_client("dev")
    if parts is None:
        raise HTTPException(status_code=503, detail="Ambiente dev não configurado.")
    w, catalog, _, _ = parts
    cfg  = _get_env_config("dev")
    host = (cfg.get("workspace_url") or settings.databricks_host).rstrip("/")

    try:
        ep_data = w.api_client.do("GET", "/api/2.0/vector-search/endpoints")
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Erro ao listar endpoints Vector Search: {exc}",
        ) from exc

    endpoints = [e.get("name") for e in (ep_data.get("endpoints") or []) if e.get("name")]

    indexes: list[dict] = []
    for ep_name in endpoints:
        try:
            idx_data = w.api_client.do(
                "GET",
                "/api/2.0/vector-search/indexes",
                query={"endpoint_name": ep_name},
            )
        except Exception:
            continue
        for idx in (idx_data.get("vector_indexes") or []):
            name = idx.get("name", "")
            if not name.startswith(f"{catalog}."):
                continue
            indexes.append({
                "name": name,
                "endpoint": ep_name,
                "mcp_url": f"{host}/api/2.0/mcp/vector-search/{name}",
                "status": (idx.get("status") or {}).get("ready", "UNKNOWN"),
            })

    return {"indexes": indexes}


# ── List registered agents (dev workspace) ────────────────────
@router.get("/agents-list")
def list_agents_for_tool():
    """Lists agents from agents_config in the dev workspace."""
    parts = _env_client("dev")
    if parts is None:
        raise HTTPException(status_code=503, detail="Ambiente dev não configurado.")
    w, catalog, schema_name, warehouse_id = parts
    prefix = f"{catalog}.{schema_name}"
    sql = f"""
    SELECT agent_id, agent_name, agent_type, status
    FROM {prefix}.agents_config
    ORDER BY agent_name
    """
    resp = _sql(w, warehouse_id, sql)
    return {"agents": _rows_to_dicts(resp)}


# ── List UC functions (dev workspace) ─────────────────────────
@router.get("/uc-functions")
def list_uc_functions():
    """Lists all user-defined functions in the dev catalog."""
    parts = _env_client("dev")
    if parts is None:
        raise HTTPException(status_code=503, detail="Ambiente dev não configurado.")
    w, catalog, _, warehouse_id = parts
    resp = _sql(w, warehouse_id, f"SHOW USER FUNCTIONS IN CATALOG {catalog}")
    rows = _rows_to_dicts(resp)
    functions = [next(iter(row.values()), "") for row in rows if row]
    return {"functions": [f for f in functions if f]}


# ── Update tool status ────────────────────────────────────────
class PatchToolStatusRequest(BaseModel):
    new_status: str   # active | inactive | deprecated | pending_review
    approved_by: str = "platform-team@empresa.com"


@router.patch("/tools/{tool_name}/status")
def update_tool_status(tool_name: str, body: PatchToolStatusRequest):
    """Update tool status (approve/reject/deprecate) in the env where the tool lives."""
    allowed = {"active", "inactive", "deprecated", "pending_review"}
    if body.new_status not in allowed:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Status inválido. Valores permitidos: {', '.join(sorted(allowed))}",
        )
    found = _find_tool(tool_name)
    if found is None:
        raise HTTPException(status_code=404, detail="Tool não encontrada.")
    w, warehouse_id, prefix = found
    _sql(w, warehouse_id, f"""
    UPDATE {prefix}.tools_config
    SET status      = '{_esc(body.new_status)}',
        approved_by = '{_esc(body.approved_by)}',
        approved_at = current_timestamp()
    WHERE tool_name = '{_esc(tool_name)}'
    """)
    return {"tool_name": tool_name, "status": body.new_status}


# ── Delete tool ───────────────────────────────────────────────
@router.delete("/tools/{tool_name}", status_code=status.HTTP_200_OK)
def delete_tool(tool_name: str):
    """Permanently delete a tool from the env where it lives."""
    found = _find_tool(tool_name)
    if found is None:
        raise HTTPException(status_code=404, detail="Tool não encontrada.")
    w, warehouse_id, prefix = found
    _sql(w, warehouse_id,
         f"DELETE FROM {prefix}.tools_config WHERE tool_name = '{_esc(tool_name)}'")
    return {"tool_name": tool_name, "deleted": True}


# ── Promote tool to next environment ─────────────────────────
class PromoteToolRequest(BaseModel):
    target_environment: str  # staging | prod


def _ref_tail(ref: str, marker: str) -> str:
    if marker in ref:
        return ref.split(marker, 1)[-1].strip("/")
    return ref.strip("/").split("/")[-1]


def _find_genie_room_serialized(w: WorkspaceClient, space_id: str, creator: str = "") -> str:
    import base64 as _b64

    def _export_path(path: str) -> str:
        try:
            resp = w.api_client.do(
                "GET", "/api/2.0/workspace/export",
                query={"path": path, "format": "AUTO"},
            )
            raw = resp.get("content") or ""
            if raw:
                return _b64.b64decode(raw).decode("utf-8")
        except Exception:
            pass
        return ""

    def _search(path: str, depth: int = 0) -> str:
        if depth > 4:
            return ""
        try:
            resp = w.api_client.do("GET", "/api/2.0/workspace/list", query={"path": path})
        except Exception:
            return ""
        for obj in (resp.get("objects") or []):
            obj_path = obj.get("path", "")
            obj_type = obj.get("object_type", "")
            if obj_type == "GENIE_ROOM":
                if space_id in obj_path:
                    content = _export_path(obj_path)
                    if content:
                        return content
                else:
                    content = _export_path(obj_path)
                    if content and space_id in content:
                        return content
            elif obj_type == "DIRECTORY" and depth < 4:
                result = _search(obj_path, depth + 1)
                if result:
                    return result
        return ""

    search_roots = []
    if creator:
        search_roots.append(f"/Users/{creator}")
    search_roots += ["/Shared", "/"]
    for root in search_roots:
        result = _search(root)
        if result:
            return result
    return ""


def _promote_genie_space(
    src_ref: str,
    src_w: WorkspaceClient,
    target_w: WorkspaceClient,
    src_catalog: str,
    target_catalog: str,
    target_warehouse: str,
    target_host: str,
) -> str:
    space_id = _ref_tail(src_ref, "/mcp/genie/")
    try:
        space = src_w.api_client.do("GET", f"/api/2.0/genie/spaces/{space_id}?include_serialized_space=true")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Genie space '{space_id}' não encontrado: {exc}") from exc

    title       = space.get("title") or space.get("display_name") or ""
    description = space.get("description") or ""
    serialized_space: str = space.get("serialized_space") or ""

    if not serialized_space:
        serialized_space = _find_genie_room_serialized(src_w, space_id, space.get("creator") or "")
    if not serialized_space:
        raise HTTPException(status_code=400,
            detail=f"Não foi possível exportar o Genie space '{space_id}'. Crie manualmente no destino.")

    if src_catalog != target_catalog:
        serialized_space = serialized_space.replace(f"{src_catalog}.", f"{target_catalog}.")

    _read_only = {"space_id", "id", "created_at", "updated_at", "creator", "owner"}
    payload: dict = {k: v for k, v in space.items() if k not in _read_only}
    payload["warehouse_id"]     = target_warehouse
    payload["serialized_space"] = serialized_space
    if description:
        payload["description"] = description

    if space.get("tables"):
        adapted = []
        for t in space["tables"]:
            tbl = t.get("table_name") or t.get("name") or ""
            if tbl.startswith(f"{src_catalog}."):
                tbl = f"{target_catalog}.{tbl[len(src_catalog) + 1:]}"
            adapted.append({**t, "table_name": tbl, "name": tbl})
        payload["tables"] = adapted

    target_space_id = None
    for path in ("/api/2.0/genie/spaces", "/api/2.0/genie/rooms"):
        try:
            ex = target_w.api_client.do("GET", path)
        except Exception:
            continue
        for s in (ex.get("genie_spaces") or ex.get("spaces") or ex.get("rooms") or []):
            if (s.get("title") or s.get("display_name") or "") == title:
                target_space_id = s.get("space_id") or s.get("id")
                break
        if target_space_id:
            break

    if target_space_id:
        try:
            target_w.api_client.do("PUT", f"/api/2.0/genie/spaces/{target_space_id}", body=payload)
        except Exception:
            try:
                target_w.api_client.do("PATCH", f"/api/2.0/genie/spaces/{target_space_id}", body=payload)
            except Exception as exc:
                raise HTTPException(status_code=502, detail=f"Falha ao atualizar Genie space: {exc}") from exc
    else:
        try:
            result = target_w.api_client.do("POST", "/api/2.0/genie/spaces", body=payload)
            target_space_id = result.get("space_id") or result.get("id") or ""
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Falha ao criar Genie space: {exc}") from exc

    return f"{target_host}/api/2.0/mcp/genie/{target_space_id}"


def _promote_vector_search_index(
    src_ref: str,
    src_w: WorkspaceClient,
    target_w: WorkspaceClient,
    src_catalog: str,
    target_catalog: str,
    target_schema: str,
    target_host: str,
) -> str:
    index_name = _ref_tail(src_ref, "/mcp/vector-search/")
    try:
        index = src_w.api_client.do("GET", f"/api/2.0/vector-search/indexes/{index_name}")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"VS Index '{index_name}' não encontrado: {exc}") from exc

    idx_base          = index_name.split(".")[-1] if "." in index_name else index_name
    target_index_name = f"{target_catalog}.{target_schema}.{idx_base}"

    try:
        ep_data = target_w.api_client.do("GET", "/api/2.0/vector-search/endpoints")
        target_endpoints = [e.get("name") for e in (ep_data.get("endpoints") or []) if e.get("name")]
    except Exception:
        target_endpoints = []

    if not target_endpoints:
        raise HTTPException(status_code=400, detail="Nenhum endpoint VS encontrado no workspace destino.")

    src_endpoint    = index.get("endpoint_name") or ""
    target_endpoint = src_endpoint if src_endpoint in target_endpoints else target_endpoints[0]

    delta_spec = dict(index.get("delta_sync_index_spec") or {})
    if delta_spec.get("source_table", "").startswith(f"{src_catalog}."):
        delta_spec["source_table"] = f"{target_catalog}.{delta_spec['source_table'][len(src_catalog) + 1:]}"
    direct_spec = index.get("direct_access_index_spec")

    try:
        target_w.api_client.do("DELETE", f"/api/2.0/vector-search/indexes/{target_index_name}")
    except Exception:
        pass

    create_body: dict = {
        "name": target_index_name,
        "endpoint_name": target_endpoint,
        "primary_key": index.get("primary_key") or "id",
        "index_type": index.get("index_type"),
    }
    if delta_spec:
        create_body["delta_sync_index_spec"] = delta_spec
    elif direct_spec:
        create_body["direct_access_index_spec"] = direct_spec

    try:
        target_w.api_client.do("POST", "/api/2.0/vector-search/indexes", body=create_body)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Falha ao criar VS Index no destino: {exc}") from exc

    return f"{target_host}/api/2.0/mcp/vector-search/{target_index_name}"


def _promote_uc_function(
    src_ref: str,
    src_w: WorkspaceClient,
    src_warehouse: str,
    target_w: WorkspaceClient,
    target_warehouse: str,
    src_catalog: str,
    target_catalog: str,
    target_schema: str,
) -> str:
    try:
        resp = _sql(src_w, src_warehouse, f"SHOW CREATE FUNCTION {src_ref}")
        rows = _rows_to_dicts(resp)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Função '{src_ref}' não encontrada: {exc}") from exc

    if not rows:
        raise HTTPException(status_code=400, detail=f"DDL da função '{src_ref}' está vazio.")

    ddl: str = next(iter(rows[0].values()), "")
    func_name  = src_ref.split(".")[-1]
    src_prefix = ".".join(src_ref.split(".")[:-1]) if "." in src_ref else ""
    target_ref = f"{target_catalog}.{target_schema}.{func_name}"

    if src_prefix:
        ddl = ddl.replace(f"CREATE FUNCTION {src_prefix}.{func_name}",
                          f"CREATE OR REPLACE FUNCTION {target_ref}")
        ddl = ddl.replace(f"CREATE FUNCTION `{src_prefix}`.`{func_name}`",
                          f"CREATE OR REPLACE FUNCTION {target_ref}")
    if "CREATE OR REPLACE" not in ddl:
        ddl = ddl.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)

    if src_catalog != target_catalog:
        ddl = ddl.replace(f"`{src_catalog}`.", f"`{target_catalog}`.")
        ddl = ddl.replace(f" {src_catalog}.", f" {target_catalog}.")

    try:
        _sql(target_w, target_warehouse, ddl)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Falha ao criar função no destino: {exc}") from exc

    return target_ref


@router.post("/tools/{tool_name}/promote", status_code=status.HTTP_200_OK)
def promote_tool(tool_name: str, body: PromoteToolRequest):
    """
    Promote a tool to the next environment (dev→staging or staging→prod).

    - Replicates the underlying asset (Genie space / VS index / UC function) in target workspace
    - Upserts the tool record into the target env's tools_config (status='active')
    - Removes the tool from the source env's tools_config
    """
    allowed_transitions = {"dev": "staging", "staging": "prod"}

    # 1. Find tool in its current env
    found = _find_tool(tool_name)
    if found is None:
        raise HTTPException(status_code=404, detail="Tool não encontrada.")
    src_w, src_warehouse_id, src_prefix = found

    # Read full tool record from source
    rows = _rows_to_dicts(_sql(src_w, src_warehouse_id,
        f"SELECT tool_name, kind, ref, description, owner, environment "
        f"FROM {src_prefix}.tools_config WHERE tool_name = '{_esc(tool_name)}'"))
    if not rows:
        raise HTTPException(status_code=404, detail="Tool não encontrada.")

    tool        = rows[0]
    kind        = tool.get("kind") or ""
    src_ref     = tool.get("ref") or ""
    current_env = tool.get("environment") or "dev"
    expected_target = allowed_transitions.get(current_env)

    if expected_target is None:
        raise HTTPException(status_code=400, detail=f"Ambiente '{current_env}' não pode ser promovido.")
    if body.target_environment != expected_target:
        raise HTTPException(status_code=400,
            detail=f"De '{current_env}' só é possível promover para '{expected_target}'.")

    # 2. Resolve source and target env clients
    src_cfg          = _get_env_config(current_env)
    src_catalog      = src_cfg.get("catalog") or settings.framework_catalog

    target_parts = _env_client(body.target_environment)
    if target_parts is None:
        raise HTTPException(status_code=400,
            detail=f"Ambiente '{body.target_environment}' não configurado em Settings.")
    target_w, target_catalog, target_schema, target_warehouse_id = target_parts

    target_cfg  = _get_env_config(body.target_environment)
    target_host = (target_cfg.get("workspace_url") or settings.databricks_host).rstrip("/")
    target_prefix = f"{target_catalog}.{target_schema}"

    # 3. Replicate the asset in the target workspace
    if kind == "mcp_genie":
        target_ref = _promote_genie_space(
            src_ref, src_w, target_w,
            src_catalog, target_catalog,
            target_warehouse_id, target_host,
        )
    elif kind == "mcp_vector_search":
        target_ref = _promote_vector_search_index(
            src_ref, src_w, target_w,
            src_catalog, target_catalog, target_schema, target_host,
        )
    elif kind in ("uc_function", "skill"):
        target_ref = _promote_uc_function(
            src_ref, src_w, src_warehouse_id,
            target_w, target_warehouse_id,
            src_catalog, target_catalog, target_schema,
        )
    else:
        target_ref = src_ref

    # 4. Ensure target schema + tools_config table exist
    _ensure_tools_table(target_w, target_warehouse_id, target_prefix)

    # 5. Upsert tool into target tools_config
    _sql(target_w, target_warehouse_id, f"""
        MERGE INTO {target_prefix}.tools_config AS target
        USING (
            SELECT
                '{_esc(tool_name)}'                      AS tool_name,
                '{_esc(kind)}'                           AS kind,
                '{_esc(target_ref)}'                     AS ref,
                '{_esc(tool.get("description") or "")}'  AS description,
                '{_esc(tool.get("owner") or "")}'        AS owner,
                '{_esc(body.target_environment)}'        AS environment
        ) AS source
        ON target.tool_name = source.tool_name
        WHEN MATCHED THEN UPDATE SET
            kind = source.kind, ref = source.ref,
            description = source.description, owner = source.owner,
            environment = source.environment,
            status = 'active', approved_at = current_timestamp()
        WHEN NOT MATCHED THEN INSERT (
            tool_name, kind, ref, description, owner, environment,
            status, created_at, created_by
        ) VALUES (
            source.tool_name, source.kind, source.ref, source.description,
            source.owner, source.environment, 'active',
            current_timestamp(), source.owner
        )
    """)

    # 6. Remove tool from source env's tools_config
    _sql(src_w, src_warehouse_id,
         f"DELETE FROM {src_prefix}.tools_config WHERE tool_name = '{_esc(tool_name)}'")

    return {
        "tool_name": tool_name,
        "environment": body.target_environment,
        "target_ref": target_ref,
        "deployed_to": f"{target_prefix}.tools_config",
    }
