"""
Tools registry API.

tools_config lives in a single Lakebase (PostgreSQL) database shared across all
environments. The `environment` column (dev / staging / prod) is a lifecycle flag.
Workspace API calls (Genie, Vector Search, UC functions) still go to each env workspace.
"""

import time

from databricks.sdk import WorkspaceClient
from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel

from app.api import lakebase
from app.api.db import _execute_sql, _rows_to_dicts, _esc  # used for workspace_envs lookup
from app.config import settings

router = APIRouter(prefix="/api")


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
    """Build a WorkspaceClient from an env config dict. Falls back to .env credentials.

    auth_type is always set explicitly to prevent the SDK from picking up conflicting
    credentials from env vars (DATABRICKS_CLIENT_ID/SECRET injected by Databricks Apps
    for the app's own M2M OAuth) when creating a PAT-based client for another workspace.
    """
    host  = (cfg.get("workspace_url") or settings.databricks_host).rstrip("/")
    token = cfg.get("token") or ""
    if token:
        return WorkspaceClient(host=host, token=token, auth_type="pat")
    if settings.databricks_client_id and settings.databricks_client_secret:
        return WorkspaceClient(
            host=host,
            client_id=settings.databricks_client_id,
            client_secret=settings.databricks_client_secret,
            auth_type="oauth-m2m",
        )
    return WorkspaceClient(host=host, token=settings.databricks_token, auth_type="pat")


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


def _get_domain_config(domain: str, env: str = "dev") -> dict | None:
    """Read workspace config for a domain+env from domain_envs (Lakebase)."""
    rows = lakebase.execute(
        "SELECT workspace_url, token FROM domain_envs WHERE domain = %s AND env = %s",
        (domain, env),
    )
    return rows[0] if rows else None


def _domain_client(domain: str, env: str = "dev") -> tuple[WorkspaceClient, str] | None:
    """
    Returns (WorkspaceClient, host) for a domain's workspace config.
    Falls back to the env workspace config when domain has no workspace configured.
    Returns None when neither has a host.
    """
    cfg = _get_domain_config(domain, env)
    host  = (cfg.get("workspace_url") or "").rstrip("/") if cfg else ""
    token = (cfg.get("token") or "") if cfg else ""

    # Fall back to env-level config when domain doesn't override it
    if not host:
        env_cfg = _get_env_config(env)
        host  = (env_cfg.get("workspace_url") or "").rstrip("/")
        token = token or env_cfg.get("token") or ""
        if not host and env == "dev":
            host = settings.databricks_host.rstrip("/")

    if not host:
        return None

    w = _workspace_client_for_env({"workspace_url": host, "token": token})
    return w, host


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


@router.get("/tools")
def list_tools(domain: str | None = None):
    """List tools from Lakebase, optionally filtered by domain."""
    if domain:
        rows = lakebase.execute("""
            SELECT tool_name, kind, ref, description, owner, status, environment, domain,
                   created_at, created_by, approved_by, approved_at
            FROM tools_config
            WHERE domain = %s
            ORDER BY
                CASE environment WHEN 'prod' THEN 0 WHEN 'staging' THEN 1 ELSE 2 END,
                tool_name
        """, (domain,))
    else:
        rows = lakebase.execute("""
            SELECT tool_name, kind, ref, description, owner, status, environment, domain,
                   created_at, created_by, approved_by, approved_at
            FROM tools_config
            ORDER BY
                CASE environment WHEN 'prod' THEN 0 WHEN 'staging' THEN 1 ELSE 2 END,
                tool_name
        """)
    return {"tools": rows}


# ── Register tool (always in dev) ────────────────────────────
class RegisterToolRequest(BaseModel):
    tool_name: str
    kind: str
    ref: str
    description: str
    owner: str = "platform-team@empresa.com"
    environment: str = "dev"
    domain: str = "default"


@router.post("/tools", status_code=status.HTTP_201_CREATED)
def register_tool(body: RegisterToolRequest):
    """Register a new tool into Lakebase with environment='dev'."""
    lakebase.execute("""
        INSERT INTO tools_config
            (tool_name, kind, ref, description, owner, environment, domain, status, created_at, created_by)
        VALUES (%s, %s, %s, %s, %s, 'dev', %s, 'active', NOW(), %s)
        ON CONFLICT (tool_name) DO UPDATE SET
            kind        = EXCLUDED.kind,
            ref         = EXCLUDED.ref,
            description = EXCLUDED.description,
            owner       = EXCLUDED.owner,
            domain      = EXCLUDED.domain
    """, (body.tool_name, body.kind, body.ref, body.description, body.owner, body.domain, body.owner))
    return {"tool_name": body.tool_name, "status": "active", "domain": body.domain}


# ── Workspace host ─────────────────────────────────────────────
@router.get("/workspace-host")
def get_workspace_host(domain: str | None = Query(default=None)):
    if domain:
        result = _domain_client(domain)
        if result:
            return {"host": result[1]}
    cfg  = _get_env_config("dev")
    host = (cfg.get("workspace_url") or settings.databricks_host).rstrip("/")
    return {"host": host}


# ── List Genie rooms ───────────────────────────────────────────
@router.get("/genie-rooms")
def list_genie_rooms(domain: str | None = Query(default=None)):
    """Returns all Genie spaces from the workspace configured for the given domain (dev env)."""
    if domain:
        result = _domain_client(domain)
        if result is None:
            raise HTTPException(status_code=503, detail=f"Workspace não configurado para o domínio '{domain}'.")
        w, host = result
    else:
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


# ── List Vector Search indexes ─────────────────────────────────
@router.get("/vector-search-indexes")
def list_vector_search_indexes(domain: str | None = Query(default=None)):
    """Lists all Vector Search indexes in the workspace configured for the given domain."""
    if domain:
        result = _domain_client(domain)
        if result is None:
            raise HTTPException(status_code=503, detail=f"Workspace não configurado para o domínio '{domain}'.")
        w, host = result
        catalog = settings.framework_catalog  # fallback; domain could override in the future
    else:
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


# ── List registered agents ────────────────────────────────────
@router.get("/agents-list")
def list_agents_for_tool(domain: str | None = Query(default=None)):
    """Lists agents from agents_config in Lakebase filtered by domain (dev environment)."""
    if domain:
        rows = lakebase.execute(
            "SELECT agent_id, name AS agent_name, agent_type, status FROM agents_config "
            "WHERE environment = 'dev' AND domain = %s ORDER BY name",
            (domain,),
        )
    else:
        rows = lakebase.execute("""
            SELECT agent_id, name AS agent_name, agent_type, status
            FROM agents_config
            WHERE environment = 'dev'
            ORDER BY name
        """)
    return {"agents": rows}


# ── List UC functions ──────────────────────────────────────────
@router.get("/uc-functions")
def list_uc_functions(domain: str | None = Query(default=None)):
    """Lists all user-defined functions in the workspace configured for the given domain."""
    if domain:
        result = _domain_client(domain)
        if result is None:
            raise HTTPException(status_code=503, detail=f"Workspace não configurado para o domínio '{domain}'.")
        w_domain, _ = result
        # For UC functions we still need a warehouse — fall back to dev env
        parts = _env_client("dev")
        if parts is None:
            raise HTTPException(status_code=503, detail="Ambiente dev não configurado para SQL.")
        _, catalog, _, warehouse_id = parts
        w = w_domain  # use domain workspace for the query
    else:
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
    """Update tool status in Lakebase."""
    allowed = {"active", "inactive", "deprecated", "pending_review"}
    if body.new_status not in allowed:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Status inválido. Valores permitidos: {', '.join(sorted(allowed))}",
        )
    row = lakebase.execute_one(
        "SELECT tool_name FROM tools_config WHERE tool_name = %s", (tool_name,)
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Tool não encontrada.")
    lakebase.execute("""
        UPDATE tools_config
        SET status      = %s,
            approved_by = %s,
            approved_at = NOW()
        WHERE tool_name = %s
    """, (body.new_status, body.approved_by, tool_name))
    return {"tool_name": tool_name, "status": body.new_status}


# ── Delete tool ───────────────────────────────────────────────
@router.delete("/tools/{tool_name}", status_code=status.HTTP_200_OK)
def delete_tool(tool_name: str):
    """Permanently delete a tool from Lakebase."""
    row = lakebase.execute_one(
        "SELECT tool_name FROM tools_config WHERE tool_name = %s", (tool_name,)
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Tool não encontrada.")
    lakebase.execute("DELETE FROM tools_config WHERE tool_name = %s", (tool_name,))
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

    _read_only = {"space_id", "id", "created_at", "updated_at", "creator", "owner", "etag"}
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
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Falha ao criar Genie space: {exc}") from exc
        target_space_id = (result or {}).get("space_id") or (result or {}).get("id") or ""
        if not target_space_id:
            raise HTTPException(
                status_code=502,
                detail=f"Falha ao criar Genie space: API não retornou o ID do espaço. Resposta: {result}",
            )
        # Verify the space is accessible before registering the tool
        try:
            target_w.api_client.do("GET", f"/api/2.0/genie/spaces/{target_space_id}")
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Genie space {target_space_id} criado mas inacessível: {exc}",
            ) from exc

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
    - Updates the tool's environment flag in Lakebase (no record copy)
    """
    allowed_transitions = {"dev": "staging", "staging": "prod"}

    # 1. Find tool in Lakebase
    tool = lakebase.execute_one(
        "SELECT tool_name, kind, ref, description, owner, environment FROM tools_config WHERE tool_name = %s",
        (tool_name,),
    )
    if tool is None:
        raise HTTPException(status_code=404, detail="Tool não encontrada.")

    kind        = tool.get("kind") or ""
    src_ref     = tool.get("ref") or ""
    current_env = tool.get("environment") or "dev"
    expected_target = allowed_transitions.get(current_env)

    if expected_target is None:
        raise HTTPException(status_code=400, detail=f"Ambiente '{current_env}' não pode ser promovido.")
    if body.target_environment != expected_target:
        raise HTTPException(status_code=400,
            detail=f"De '{current_env}' só é possível promover para '{expected_target}'.")

    # 2. Resolve source and target workspace clients
    src_parts = _env_client(current_env)
    if src_parts is None:
        raise HTTPException(status_code=400,
            detail=f"Ambiente '{current_env}' não configurado em Settings.")
    src_w, src_catalog, src_schema, src_warehouse_id = src_parts

    target_parts = _env_client(body.target_environment)
    if target_parts is None:
        raise HTTPException(status_code=400,
            detail=f"Ambiente '{body.target_environment}' não configurado em Settings.")
    target_w, target_catalog, target_schema, target_warehouse_id = target_parts

    target_cfg  = _get_env_config(body.target_environment)
    target_host = (target_cfg.get("workspace_url") or settings.databricks_host).rstrip("/")

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

    # 4. Update environment flag and ref in Lakebase (no record copy)
    lakebase.execute("""
        UPDATE tools_config
        SET environment = %s,
            ref         = %s,
            status      = 'active',
            approved_at = NOW()
        WHERE tool_name = %s
    """, (body.target_environment, target_ref, tool_name))

    return {
        "tool_name": tool_name,
        "environment": body.target_environment,
        "target_ref": target_ref,
    }
