"""
Agents registry API.

Each environment's agents_config lives in that environment's own workspace:
  dev     → {dev_catalog}.{dev_schema}.agents_config
  staging → {staging_catalog}.{staging_schema}.agents_config
  prod    → {prod_catalog}.{prod_schema}.agents_config

New agents are always registered in dev with status='draft'.

Column mapping (framework schema uses 'name', not 'agent_name'):
  DB column  ↔  API/frontend field
  name       ↔  agent_name
"""

import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.ml import ExperimentTag
import time as _time

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, status
from pydantic import BaseModel

from app.api.db import _execute_sql, _rows_to_dicts, _esc
from app.api.tools import _env_client, _get_env_config, _sql, _ENVS_DESC, _ENV_RANK, _ensure_tools_table, _workspace_client_for_env, _promote_genie_space
from app.config import settings

router = APIRouter(prefix="/api")

ENVS = ("dev", "staging", "prod")

VALID_STATUSES = {"active", "disabled"}

# Virtual status is derived from the environment at read time.
# The DB always stores 'active'; 'disabled' is the only real status change possible.
_ENV_VIRTUAL_STATUS = {
    "dev":     "draft",
    "staging": "evaluating",
    "prod":    "approved",
}

_ENV_NEXT = {"dev": "staging", "staging": "prod"}

# Framework's existing schema uses 'name', not 'agent_name'.
# We CREATE with 'name' so new tables match the framework convention.
_AGENTS_CONFIG_DDL = """
    CREATE TABLE IF NOT EXISTS {prefix}.agents_config (
        agent_id              STRING NOT NULL,
        name                  STRING NOT NULL,
        description           STRING,
        model                 STRING,
        tools_enabled         ARRAY<STRING>,
        status                STRING NOT NULL,
        environment           STRING,
        runtime_mode          STRING,
        eval_profile          STRING,
        min_safety_score      DOUBLE,
        min_correctness_score DOUBLE,
        created_at            TIMESTAMP,
        created_by            STRING,
        updated_at            TIMESTAMP
    )
    USING DELTA
    COMMENT 'Registro de agents do corp-agent-framework.'
"""

# Columns that may be missing from older tables created by the framework
_MIGRATIONS = [
    "ADD COLUMN environment             STRING",
    "ADD COLUMN runtime_mode            STRING",
    "ADD COLUMN created_by              STRING",
    "ADD COLUMN updated_at              TIMESTAMP",
    "ADD COLUMN eval_profile            STRING",
    "ADD COLUMN min_safety_score        DOUBLE",
    "ADD COLUMN min_correctness_score   DOUBLE",
    # Framework columns that may be missing on older tables
    "ADD COLUMN serving_endpoint_name   STRING",
    # Extra columns our client layer adds
    "ADD COLUMN agent_type              STRING",
    "ADD COLUMN owner_principal         STRING",
    "ADD COLUMN instructions            STRING",
    # Approval workflow
    "ADD COLUMN approval_requested      BOOLEAN",
    # MLflow experiment linked at promote-to-staging time
    "ADD COLUMN mlflow_experiment_id    STRING",
    "ADD COLUMN mlflow_url              STRING",
    # Eval run tracking
    "ADD COLUMN eval_run_id             STRING",
    "ADD COLUMN eval_status             STRING",
]


def _ensure_agents_table(w: WorkspaceClient, warehouse_id: str, prefix: str) -> None:
    """Create schema + agents_config table if they don't exist, and migrate missing columns."""
    try:
        _sql(w, warehouse_id, f"CREATE SCHEMA IF NOT EXISTS {prefix}")
    except Exception:
        pass
    _sql(w, warehouse_id, _AGENTS_CONFIG_DDL.format(prefix=prefix))
    for migration in _MIGRATIONS:
        try:
            _sql(w, warehouse_id, f"ALTER TABLE {prefix}.agents_config {migration}")
        except Exception:
            pass  # column already exists → ok
    # Migrate existing 'draft' agents to 'active' so the framework can load them
    try:
        _sql(w, warehouse_id,
             f"UPDATE {prefix}.agents_config SET status = 'active' WHERE status = 'draft'")
    except Exception:
        pass


def _find_agent(agent_id: str) -> tuple[WorkspaceClient, str, str, str] | None:
    """Search prod→staging→dev, return (w, warehouse_id, prefix, env) for highest env containing the agent."""
    for env in _ENVS_DESC:
        parts = _env_client(env)
        if parts is None:
            continue
        w, catalog, schema_name, warehouse_id = parts
        prefix = f"{catalog}.{schema_name}"
        try:
            resp = _sql(w, warehouse_id,
                f"SELECT agent_id FROM {prefix}.agents_config WHERE agent_id = '{_esc(agent_id)}'")
            rows = _rows_to_dicts(resp)
            if rows:
                return w, warehouse_id, prefix, env
        except Exception:
            continue
    return None


# ── List agents ───────────────────────────────────────────────

# Cache: prefixes whose late-added columns have already been migrated this run.
# ALTER TABLE is idempotent but slow — skip after first successful migration.
_agents_col_migrated: set[str] = set()

_LATE_AGENT_COLS = (
    "ADD COLUMN approval_requested   BOOLEAN",
    "ADD COLUMN mlflow_experiment_id STRING",
    "ADD COLUMN mlflow_url           STRING",
    "ADD COLUMN eval_run_id          STRING",
    "ADD COLUMN eval_status          STRING",
)


def _fetch_agents_for_env(env: str) -> list[tuple[int, dict]]:
    """Query agents for one environment. Returns [(rank, row), ...] or []."""
    parts = _env_client(env)
    if parts is None:
        return []
    w, catalog, schema_name, warehouse_id = parts
    prefix = f"{catalog}.{schema_name}"
    rank = _ENV_RANK.get(env, 0)

    # Run late-added column migrations only once per prefix per server start
    if prefix not in _agents_col_migrated:
        for col_ddl in _LATE_AGENT_COLS:
            try:
                _sql(w, warehouse_id, f"ALTER TABLE {prefix}.agents_config {col_ddl}")
            except Exception:
                pass  # column already exists or table doesn't exist yet
        _agents_col_migrated.add(prefix)

    try:
        resp = _sql(w, warehouse_id, f"""
            SELECT agent_id,
                   name            AS agent_name,
                   agent_type,
                   owner_principal,
                   description,
                   instructions,
                   to_json(tools_enabled) AS tools_enabled,
                   model,
                   serving_endpoint_name,
                   eval_profile,
                   min_safety_score,
                   min_correctness_score,
                   environment,
                   status,
                   runtime_mode,
                   approval_requested,
                   mlflow_experiment_id,
                   mlflow_url,
                   eval_run_id,
                   eval_status,
                   created_at,
                   updated_at
            FROM {prefix}.agents_config
            ORDER BY created_at DESC
        """)
        rows = _rows_to_dicts(resp)
        for row in rows:
            row["environment"] = row.get("environment") or env
        return [(rank, row) for row in rows if row.get("agent_id")]
    except Exception:
        return []


@router.get("/agents")
def list_agents(env: str | None = Query(default=None)):
    """Aggregate agents from configured envs. When env is given, only that env is queried."""
    envs_to_query = [env] if env in ENVS else list(ENVS)
    best: dict[str, tuple[int, dict]] = {}

    # Query environments in parallel
    with ThreadPoolExecutor(max_workers=len(envs_to_query)) as executor:
        futures = {executor.submit(_fetch_agents_for_env, e): e for e in envs_to_query}
        try:
            done_iter = as_completed(futures, timeout=30)
            for future in done_iter:
                try:
                    for rank, row in future.result():
                        key = row["agent_id"]
                        current_rank, _ = best.get(key, (-1, {}))
                        if rank > current_rank:
                            best[key] = (rank, row)
                except Exception:
                    pass
        except TimeoutError:
            # Some envs didn't respond in time — return what already completed
            for future in futures:
                if future.done():
                    try:
                        for rank, row in future.result():
                            key = row["agent_id"]
                            current_rank, _ = best.get(key, (-1, {}))
                            if rank > current_rank:
                                best[key] = (rank, row)
                    except Exception:
                        pass

    agents = []
    for _, row in sorted(best.values(), key=lambda x: x[0], reverse=True):
        te = row.get("tools_enabled")
        if isinstance(te, str):
            try:
                row["tools_enabled"] = json.loads(te)
            except Exception:
                row["tools_enabled"] = []
        elif te is None:
            row["tools_enabled"] = []
        # Map environment to virtual status; keep 'disabled' as-is
        if row.get("status") != "disabled":
            env = row.get("environment", "dev")
            approval_req = row.get("approval_requested")
            if env == "dev" and approval_req in (True, "true", 1, "1"):
                row["status"] = "pending_approval"
            else:
                row["status"] = _ENV_VIRTUAL_STATUS.get(env, "draft")
        agents.append(row)

    return {"agents": agents}


# ── Register agent ────────────────────────────────────────────

_FRAMEWORK_ENDPOINT = "corp-config-driven-agent-dev"

# Framework serving endpoint name per environment
_ENV_FRAMEWORK_ENDPOINT = {
    "dev":     "corp-config-driven-agent-dev",
    "staging": "corp-config-driven-agent-staging",
    "prod":    "corp-config-driven-agent",
}


class RegisterAgentRequest(BaseModel):
    agent_id: str
    agent_name: str
    agent_type: str = "conversational"
    owner_principal: str = ""
    description: str = ""
    instructions: str = ""
    tools_enabled: list[str] = []
    model: str = ""
    eval_profile: str = "default_internal"
    min_safety_score: float | None = None
    min_correctness_score: float | None = None
    runtime_mode: str = "config-driven-single-endpoint"


@router.post("/agents", status_code=status.HTTP_201_CREATED)
def register_agent(body: RegisterAgentRequest):
    """Register a new agent into the dev environment's agents_config."""
    parts = _env_client("dev")
    if parts is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Ambiente dev não configurado em Settings.",
        )
    w, catalog, schema_name, warehouse_id = parts
    prefix = f"{catalog}.{schema_name}"

    dev_cfg = _get_env_config("dev")
    if not dev_cfg.get("catalog") or not dev_cfg.get("schema_name"):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Catalog e schema_name não configurados para o ambiente dev. Configure em Settings → Workspace Dev.",
        )

    _ensure_agents_table(w, warehouse_id, prefix)

    tools_json  = json.dumps(body.tools_enabled).replace("'", "\\'")
    min_safety  = str(body.min_safety_score)         if body.min_safety_score         is not None else "NULL"
    min_correct = str(body.min_correctness_score)    if body.min_correctness_score    is not None else "NULL"

    _sql(w, warehouse_id, f"""
    MERGE INTO {prefix}.agents_config AS target
    USING (
      SELECT
        '{_esc(body.agent_id)}'        AS agent_id,
        '{_esc(body.agent_name)}'      AS name,
        '{_esc(body.agent_type)}'      AS agent_type,
        '{_esc(body.owner_principal)}' AS owner_principal,
        '{_esc(body.description)}'     AS description,
        '{_esc(body.instructions)}'    AS instructions,
        from_json('{tools_json}', 'array<string>') AS tools_enabled,
        '{_esc(body.model)}'           AS model,
        '{_FRAMEWORK_ENDPOINT}'        AS serving_endpoint_name,
        '{_esc(body.eval_profile)}'    AS eval_profile,
        {min_safety}                   AS min_safety_score,
        {min_correct}                  AS min_correctness_score,
        'dev'                          AS environment,
        '{_esc(body.runtime_mode)}'    AS runtime_mode
    ) AS source
    ON target.agent_id = source.agent_id
    WHEN MATCHED THEN UPDATE SET
        name                  = source.name,
        agent_type            = source.agent_type,
        owner_principal       = source.owner_principal,
        description           = source.description,
        instructions          = source.instructions,
        tools_enabled         = from_json('{tools_json}', 'array<string>'),
        model                 = source.model,
        serving_endpoint_name = '{_FRAMEWORK_ENDPOINT}',
        eval_profile          = source.eval_profile,
        min_safety_score      = source.min_safety_score,
        min_correctness_score = source.min_correctness_score,
        runtime_mode          = source.runtime_mode,
        status                = 'active',
        updated_at            = current_timestamp()
    WHEN NOT MATCHED THEN INSERT (
        agent_id, name, agent_type, owner_principal, description, instructions,
        tools_enabled, model, serving_endpoint_name, eval_profile,
        min_safety_score, min_correctness_score,
        environment, status, runtime_mode, created_at, created_by, updated_at
    ) VALUES (
        source.agent_id, source.name, source.agent_type, source.owner_principal,
        source.description, source.instructions, source.tools_enabled, source.model,
        source.serving_endpoint_name, source.eval_profile,
        source.min_safety_score, source.min_correctness_score,
        'dev', 'active', source.runtime_mode,
        current_timestamp(), source.owner_principal, current_timestamp()
    )
    """)
    return {"agent_id": body.agent_id, "status": "draft", "environment": "dev"}


# ── Update agent ──────────────────────────────────────────────

class UpdateAgentRequest(BaseModel):
    agent_name: str | None = None
    agent_type: str | None = None
    owner_principal: str | None = None
    description: str | None = None
    instructions: str | None = None
    tools_enabled: list[str] | None = None
    model: str | None = None
    serving_endpoint_name: str | None = None
    eval_profile: str | None = None
    min_safety_score: float | None = None
    min_correctness_score: float | None = None
    runtime_mode: str | None = None


@router.put("/agents/{agent_id}")
def update_agent(agent_id: str, body: UpdateAgentRequest):
    """Update an existing agent."""
    found = _find_agent(agent_id)
    if found is None:
        raise HTTPException(status_code=404, detail=f"Agente '{agent_id}' não encontrado.")
    w, warehouse_id, prefix, env = found

    updates = []
    if body.agent_name is not None:
        updates.append(f"name = '{_esc(body.agent_name)}'")          # DB column is 'name'
    if body.agent_type is not None:
        updates.append(f"agent_type = '{_esc(body.agent_type)}'")
    if body.owner_principal is not None:
        updates.append(f"owner_principal = '{_esc(body.owner_principal)}'")
    if body.description is not None:
        updates.append(f"description = '{_esc(body.description)}'")
    if body.instructions is not None:
        updates.append(f"instructions = '{_esc(body.instructions)}'")
    if body.tools_enabled is not None:
        te_json = json.dumps(body.tools_enabled).replace("'", "\\'")
        updates.append(f"tools_enabled = from_json('{te_json}', 'array<string>')")
    if body.model is not None:
        updates.append(f"model = '{_esc(body.model)}'")
    if body.serving_endpoint_name is not None:
        updates.append(f"serving_endpoint_name = '{_esc(body.serving_endpoint_name)}'")
    if body.eval_profile is not None:
        updates.append(f"eval_profile = '{_esc(body.eval_profile)}'")
    if body.min_safety_score is not None:
        updates.append(f"min_safety_score = {body.min_safety_score}")
    if body.min_correctness_score is not None:
        updates.append(f"min_correctness_score = {body.min_correctness_score}")
    if body.runtime_mode is not None:
        updates.append(f"runtime_mode = '{_esc(body.runtime_mode)}'")
    updates.append("updated_at = current_timestamp()")

    _sql(w, warehouse_id, f"""
        UPDATE {prefix}.agents_config
        SET {', '.join(updates)}
        WHERE agent_id = '{_esc(agent_id)}'
    """)
    return {"agent_id": agent_id, "environment": env}


# ── Update status ─────────────────────────────────────────────

class UpdateStatusRequest(BaseModel):
    new_status: str


@router.patch("/agents/{agent_id}/status")
def update_agent_status(agent_id: str, body: UpdateStatusRequest):
    if body.new_status not in VALID_STATUSES:
        raise HTTPException(status_code=400, detail=f"Status inválido: '{body.new_status}'.")
    found = _find_agent(agent_id)
    if found is None:
        raise HTTPException(status_code=404, detail=f"Agente '{agent_id}' não encontrado.")
    w, warehouse_id, prefix, env = found
    _sql(w, warehouse_id, f"""
        UPDATE {prefix}.agents_config
        SET status = '{_esc(body.new_status)}', updated_at = current_timestamp()
        WHERE agent_id = '{_esc(agent_id)}'
    """)
    return {"agent_id": agent_id, "new_status": body.new_status, "environment": env}


# ── Promote agent ─────────────────────────────────────────────

@router.post("/agents/{agent_id}/promote")
def promote_agent(agent_id: str):
    """Copy agent config to the next environment (dev→staging or staging→prod)."""
    found = _find_agent(agent_id)
    if found is None:
        raise HTTPException(status_code=404, detail=f"Agente '{agent_id}' não encontrado.")
    w_src, wh_src, prefix_src, env_src = found

    next_env = _ENV_NEXT.get(env_src)
    if not next_env:
        raise HTTPException(status_code=400, detail="Agente já está no ambiente prod.")

    parts = _env_client(next_env)
    if parts is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Ambiente '{next_env}' não configurado em Settings.",
        )
    w_dst, catalog_dst, schema_dst, wh_dst = parts
    prefix_dst = f"{catalog_dst}.{schema_dst}"

    resp = _sql(w_src, wh_src, f"""
        SELECT name, agent_type, owner_principal, description, instructions,
               to_json(tools_enabled) AS tools_enabled_json,
               model, serving_endpoint_name, eval_profile,
               min_safety_score, min_correctness_score, runtime_mode
        FROM {prefix_src}.agents_config
        WHERE agent_id = '{_esc(agent_id)}'
        LIMIT 1
    """)
    rows = _rows_to_dicts(resp)
    if not rows:
        raise HTTPException(status_code=404, detail=f"Agente '{agent_id}' não encontrado.")
    row = rows[0]

    # ── 1. Register MLflow experiment BEFORE writing to staging ──
    mlflow_experiment_id: str | None = None
    if next_env == "staging":
        exp_name = f"/Framework Agents/{agent_id}"
        print(f"[MLflow] Criando experimento '{exp_name}' no staging...", file=sys.stderr)
        try:
            # Ensure parent directory exists in the staging workspace
            w_dst.workspace.mkdirs(path="/Framework Agents")
        except Exception as mk_exc:
            print(f"[MLflow] mkdirs aviso: {mk_exc}", file=sys.stderr)
        try:
            # Create experiment (or recover existing)
            try:
                create_resp = w_dst.experiments.create_experiment(
                    name=exp_name,
                    tags=[ExperimentTag(key="mlflow.experimentType", value="GENAI_EXPERIMENT")],
                )
                mlflow_experiment_id = create_resp.experiment_id
                print(f"[MLflow] Criado: experiment_id={mlflow_experiment_id}", file=sys.stderr)
            except Exception as create_exc:
                print(f"[MLflow] create_experiment falhou ({create_exc}), tentando get_by_name...", file=sys.stderr)
                get_resp = w_dst.experiments.get_by_name(experiment_name=exp_name)
                if get_resp and get_resp.experiment:
                    mlflow_experiment_id = get_resp.experiment.experiment_id
                    # mlflow.experimentType is a system tag — immutable once set; ignore update errors
                    try:
                        w_dst.experiments.set_experiment_tag(
                            experiment_id=mlflow_experiment_id,
                            key="mlflow.experimentType",
                            value="GENAI_EXPERIMENT",
                        )
                    except Exception:
                        pass
                    print(f"[MLflow] Recuperado: experiment_id={mlflow_experiment_id}", file=sys.stderr)

            if not mlflow_experiment_id:
                raise ValueError("experiment_id não retornado após create/get_by_name.")

        except Exception as exc:
            print(f"[MLflow] Falha: {exc}", file=sys.stderr)
            raise HTTPException(
                status_code=400,
                detail=f"Falha ao registrar experimento MLflow no staging: {exc}",
            )
        if not mlflow_experiment_id:
            raise HTTPException(
                status_code=400,
                detail="Falha ao registrar experimento MLflow no staging: experiment_id não retornado.",
            )

    # ── 2. Write agent config to destination env ──────────────────
    _ensure_agents_table(w_dst, wh_dst, prefix_dst)

    tools_json  = (row.get("tools_enabled_json") or "[]").replace("'", "\\'")
    min_safety  = str(row["min_safety_score"])      if row.get("min_safety_score")      is not None else "NULL"
    min_correct = str(row["min_correctness_score"]) if row.get("min_correctness_score") is not None else "NULL"
    endpoint    = _ENV_FRAMEWORK_ENDPOINT.get(next_env, _FRAMEWORK_ENDPOINT)
    mlflow_col  = f"'{_esc(mlflow_experiment_id)}'" if mlflow_experiment_id else "NULL"

    staging_host = (_get_env_config(next_env).get("workspace_url") or "").rstrip("/") if next_env == "staging" else ""
    mlflow_url_val = f"{staging_host}/ml/experiments/{mlflow_experiment_id}" if (mlflow_experiment_id and staging_host) else None
    mlflow_url_col = f"'{_esc(mlflow_url_val)}'" if mlflow_url_val else "NULL"

    _sql(w_dst, wh_dst, f"""
    MERGE INTO {prefix_dst}.agents_config AS target
    USING (
      SELECT
        '{_esc(agent_id)}'                      AS agent_id,
        '{_esc(row.get("name",""))}'             AS name,
        '{_esc(row.get("agent_type",""))}'       AS agent_type,
        '{_esc(row.get("owner_principal",""))}'  AS owner_principal,
        '{_esc(row.get("description",""))}'      AS description,
        '{_esc(row.get("instructions",""))}'     AS instructions,
        from_json('{tools_json}', 'array<string>') AS tools_enabled,
        '{_esc(row.get("model",""))}'            AS model,
        '{_esc(endpoint)}'                       AS serving_endpoint_name,
        '{_esc(row.get("eval_profile",""))}'     AS eval_profile,
        {min_safety}                             AS min_safety_score,
        {min_correct}                            AS min_correctness_score,
        '{_esc(row.get("runtime_mode",""))}'     AS runtime_mode,
        {mlflow_col}                             AS mlflow_experiment_id,
        {mlflow_url_col}                         AS mlflow_url
    ) AS source
    ON target.agent_id = source.agent_id
    WHEN MATCHED THEN UPDATE SET
        name                  = source.name,
        agent_type            = source.agent_type,
        owner_principal       = source.owner_principal,
        description           = source.description,
        instructions          = source.instructions,
        tools_enabled         = source.tools_enabled,
        model                 = source.model,
        serving_endpoint_name = source.serving_endpoint_name,
        eval_profile          = source.eval_profile,
        min_safety_score      = source.min_safety_score,
        min_correctness_score = source.min_correctness_score,
        runtime_mode          = source.runtime_mode,
        mlflow_experiment_id  = source.mlflow_experiment_id,
        mlflow_url            = source.mlflow_url,
        status                = 'active',
        updated_at            = current_timestamp()
    WHEN NOT MATCHED THEN INSERT (
        agent_id, name, agent_type, owner_principal, description, instructions,
        tools_enabled, model, serving_endpoint_name, eval_profile,
        min_safety_score, min_correctness_score,
        environment, status, runtime_mode, mlflow_experiment_id, mlflow_url,
        created_at, created_by, updated_at
    ) VALUES (
        source.agent_id, source.name, source.agent_type, source.owner_principal,
        source.description, source.instructions, source.tools_enabled, source.model,
        source.serving_endpoint_name, source.eval_profile,
        source.min_safety_score, source.min_correctness_score,
        '{next_env}', 'active', source.runtime_mode, source.mlflow_experiment_id, source.mlflow_url,
        current_timestamp(), source.owner_principal, current_timestamp()
    )
    """)

    # ── 3. Copy tools to destination env ──────────────────────────
    try:
        tools_list = json.loads(row.get("tools_enabled_json") or "[]")
    except Exception:
        tools_list = []

    if tools_list:
        try:
            _ensure_tools_table(w_dst, wh_dst, prefix_dst)
        except Exception:
            pass
        for tool_name in tools_list:
            try:
                tool_resp = _sql(w_src, wh_src, f"""
                    SELECT tool_name, kind, ref, description, owner
                    FROM {prefix_src}.tools_config
                    WHERE tool_name = '{_esc(tool_name)}'
                    LIMIT 1
                """)
                tool_rows = _rows_to_dicts(tool_resp)
                if not tool_rows:
                    continue
                t        = tool_rows[0]
                t_kind   = t.get("kind") or ""
                t_src_ref = t.get("ref") or ""
                t_desc   = _esc(t.get("description") or "")
                t_owner  = _esc(t.get("owner") or "")

                # For Genie tools: create the Genie space in the destination workspace.
                # If space creation fails, skip the tool — don't register a broken ref.
                if t_kind == "mcp_genie":
                    try:
                        src_catalog = prefix_src.split(".")[0]
                        t_dst_ref = _promote_genie_space(
                            src_ref=t_src_ref,
                            src_w=w_src,
                            target_w=w_dst,
                            src_catalog=src_catalog,
                            target_catalog=catalog_dst,
                            target_warehouse=wh_dst,
                            target_host=staging_host,
                        )
                    except HTTPException as exc:
                        print(f"[Promote] Genie space para tool '{tool_name}' falhou: {exc.detail}", file=sys.stderr)
                        continue  # skip — don't register tool without its Genie space
                else:
                    t_dst_ref = t_src_ref

                t_ref = _esc(t_dst_ref)
                _sql(w_dst, wh_dst, f"""
                MERGE INTO {prefix_dst}.tools_config AS target
                USING (
                  SELECT
                    '{_esc(tool_name)}'       AS tool_name,
                    '{_esc(t_kind)}'          AS kind,
                    '{t_ref}'                 AS ref,
                    '{t_desc}'                AS description,
                    '{t_owner}'               AS owner,
                    '{next_env}'              AS environment
                ) AS source
                ON target.tool_name = source.tool_name
                WHEN MATCHED THEN UPDATE SET
                    kind        = source.kind,
                    ref         = source.ref,
                    description = source.description,
                    owner       = source.owner,
                    environment = source.environment,
                    status      = 'active',
                    approved_at = current_timestamp()
                WHEN NOT MATCHED THEN INSERT (
                    tool_name, kind, ref, description, owner,
                    environment, status, created_at
                ) VALUES (
                    source.tool_name, source.kind, source.ref, source.description, source.owner,
                    source.environment, 'active', current_timestamp()
                )
                """)
            except Exception:
                pass  # non-critical

    # ── 4. Clear approval_requested in source env ─────────────────
    try:
        _sql(w_src, wh_src, f"""
            UPDATE {prefix_src}.agents_config
            SET approval_requested = false, updated_at = current_timestamp()
            WHERE agent_id = '{_esc(agent_id)}'
        """)
    except Exception:
        pass  # non-critical

    return {
        "agent_id": agent_id,
        "promoted_to": next_env,
        "tools_promoted": tools_list,
        "mlflow_experiment_id": mlflow_experiment_id,
    }


# ── Approval workflow ─────────────────────────────────────────

@router.post("/agents/{agent_id}/request-approval")
def request_approval(agent_id: str):
    """Mark an agent as pending approval review (dev only)."""
    found = _find_agent(agent_id)
    if found is None:
        raise HTTPException(status_code=404, detail=f"Agente '{agent_id}' não encontrado.")
    w, warehouse_id, prefix, env = found
    if env != "dev":
        raise HTTPException(status_code=400, detail="Apenas agentes dev podem solicitar aprovação.")
    _sql(w, warehouse_id, f"""
        UPDATE {prefix}.agents_config
        SET approval_requested = true, updated_at = current_timestamp()
        WHERE agent_id = '{_esc(agent_id)}'
    """)
    return {"agent_id": agent_id, "approval_requested": True}


@router.post("/agents/{agent_id}/reject-approval")
def reject_approval(agent_id: str):
    """Reject an approval request, returning agent to draft state."""
    found = _find_agent(agent_id)
    if found is None:
        raise HTTPException(status_code=404, detail=f"Agente '{agent_id}' não encontrado.")
    w, warehouse_id, prefix, _ = found
    _sql(w, warehouse_id, f"""
        UPDATE {prefix}.agents_config
        SET approval_requested = false, updated_at = current_timestamp()
        WHERE agent_id = '{_esc(agent_id)}'
    """)
    return {"agent_id": agent_id, "approval_requested": False}


# ── Run eval ──────────────────────────────────────────────────

def _run_eval_task(agent_id: str, staging_cfg: dict, dev_prefix: str, dev_cfg: dict) -> None:
    """Background task: run mlflow.genai.evaluate() against the staging endpoint."""
    import os
    import mlflow
    from mlflow.genai.scorers import Correctness, Safety, Guidelines

    w_staging = _workspace_client_for_env(staging_cfg)
    wh        = staging_cfg["warehouse_id"]
    prefix    = f"{staging_cfg['catalog']}.{staging_cfg['schema_name']}"

    try:
        # Fetch staging agent details
        resp = _sql(w_staging, wh,
            f"SELECT serving_endpoint_name, mlflow_experiment_id FROM {prefix}.agents_config "
            f"WHERE agent_id = '{_esc(agent_id)}' LIMIT 1")
        row           = _rows_to_dicts(resp)[0]
        experiment_id = (row.get("mlflow_experiment_id") or "").strip()
        endpoint      = (row.get("serving_endpoint_name") or "").strip()

        if not experiment_id:
            raise ValueError("mlflow_experiment_id ausente no staging.")

        # Fetch eval entries from dev
        w_dev  = _workspace_client_for_env(dev_cfg)
        wh_dev = dev_cfg["warehouse_id"]
        eval_resp = _sql(w_dev, wh_dev,
            f"SELECT request, expected_response FROM {dev_prefix}.eval_datasets "
            f"WHERE agent_id = '{_esc(agent_id)}' ORDER BY created_at ASC")
        entries = _rows_to_dicts(eval_resp)

        if not entries:
            raise ValueError("Dataset de avaliação vazio.")

        # Build eval data in mlflow.genai.evaluate() format
        eval_data = [
            {
                "inputs": {"query": e.get("request") or ""},
                "expectations": {"expected_response": e.get("expected_response") or ""},
            }
            for e in entries
        ]

        # predict_fn closure — calls staging serving endpoint
        def predict_fn(query: str) -> dict:
            if not endpoint:
                return {"response": "ERRO: endpoint não configurado"}
            try:
                result = w_staging.api_client.do(
                    "POST",
                    f"/serving-endpoints/{endpoint}/invocations",
                    body={
                        "input": [{"role": "user", "content": query}],
                        "custom_inputs": {"agent_id": agent_id},
                    },
                )
                choices = result.get("choices") or []
                if choices:
                    content = (choices[0].get("message") or {}).get("content") or ""
                    return {"response": content}
                return {"response": str(result)}
            except Exception as call_exc:
                return {"response": f"ERRO: {call_exc}"}

        # Point MLflow to the staging workspace (save/restore env vars)
        staging_host  = (staging_cfg.get("workspace_url") or "").rstrip("/")
        staging_token = staging_cfg.get("token") or ""
        _env_keys = ("DATABRICKS_HOST", "DATABRICKS_TOKEN", "DATABRICKS_CLIENT_ID", "DATABRICKS_CLIENT_SECRET")
        _orig = {k: os.environ.get(k) for k in _env_keys}
        try:
            os.environ["DATABRICKS_HOST"] = staging_host
            if staging_token:
                os.environ["DATABRICKS_TOKEN"] = staging_token
                os.environ.pop("DATABRICKS_CLIENT_ID", None)
                os.environ.pop("DATABRICKS_CLIENT_SECRET", None)
            elif settings.databricks_client_id and settings.databricks_client_secret:
                os.environ["DATABRICKS_CLIENT_ID"]     = settings.databricks_client_id
                os.environ["DATABRICKS_CLIENT_SECRET"] = settings.databricks_client_secret
                os.environ.pop("DATABRICKS_TOKEN", None)
            else:
                os.environ["DATABRICKS_TOKEN"] = settings.databricks_token or ""

            mlflow.set_tracking_uri("databricks")
            mlflow.set_experiment(f"/Framework Agents/{agent_id}")

            results = mlflow.genai.evaluate(
                data=eval_data,
                predict_fn=predict_fn,
                scorers=[
                    Correctness(),
                    Safety(),
                    Guidelines(
                        name="helpful",
                        guidelines="A resposta deve ser útil e relevante para a pergunta do usuário.",
                    ),
                ],
            )
            run_id = results.run_id
            print(f"[Eval] Concluído: agent={agent_id} run_id={run_id} metrics={results.metrics}", file=sys.stderr)

        finally:
            for k, v in _orig.items():
                if v is not None:
                    os.environ[k] = v
                else:
                    os.environ.pop(k, None)

        _sql(w_staging, wh, f"""
            UPDATE {prefix}.agents_config
            SET eval_status = 'completed', eval_run_id = '{_esc(run_id)}',
                updated_at = current_timestamp()
            WHERE agent_id = '{_esc(agent_id)}'
        """)

    except Exception as exc:
        print(f"[Eval] Falha: {exc}", file=sys.stderr)
        try:
            _sql(w_staging, wh, f"""
                UPDATE {prefix}.agents_config
                SET eval_status = 'failed', updated_at = current_timestamp()
                WHERE agent_id = '{_esc(agent_id)}'
            """)
        except Exception:
            pass


@router.post("/agents/{agent_id}/run-eval")
def run_eval(agent_id: str, background_tasks: BackgroundTasks):
    """Trigger async evaluation run for a staging agent."""
    found = _find_agent(agent_id)
    if found is None:
        raise HTTPException(status_code=404, detail=f"Agente '{agent_id}' não encontrado.")
    _, _, _, env = found
    if env != "staging":
        raise HTTPException(status_code=400, detail="Avaliação só pode ser disparada para agentes no staging.")

    staging_parts = _env_client("staging")
    dev_parts     = _env_client("dev")
    if staging_parts is None:
        raise HTTPException(status_code=503, detail="Staging não configurado.")
    if dev_parts is None:
        raise HTTPException(status_code=503, detail="Dev não configurado.")

    _, s_catalog, s_schema, s_wh = staging_parts
    _, d_catalog, d_schema, d_wh = dev_parts

    staging_cfg = {**_get_env_config("staging"), "catalog": s_catalog, "schema_name": s_schema, "warehouse_id": s_wh}
    dev_cfg     = {**_get_env_config("dev"),     "catalog": d_catalog, "schema_name": d_schema, "warehouse_id": d_wh}
    dev_prefix  = f"{d_catalog}.{d_schema}"

    # Mark as running immediately (persists across refreshes)
    w_staging = _workspace_client_for_env(staging_cfg)
    _sql(w_staging, s_wh, f"""
        UPDATE {s_catalog}.{s_schema}.agents_config
        SET eval_status = 'running', eval_run_id = NULL, updated_at = current_timestamp()
        WHERE agent_id = '{_esc(agent_id)}'
    """)

    background_tasks.add_task(_run_eval_task, agent_id, staging_cfg, dev_prefix, dev_cfg)
    return {"agent_id": agent_id, "eval_status": "running"}


# ── Chat with agent ───────────────────────────────────────────

class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage]


@router.post("/agents/{agent_id}/chat")
def chat_with_agent(agent_id: str, body: ChatRequest):
    """
    Send messages to the corp-agent-framework's serving endpoint for the agent's environment.

    The framework deploys a single multi-tenant ConfigDrivenAgent endpoint per environment.
    The specific agent is selected by passing agent_id in custom_inputs.
    """
    found = _find_agent(agent_id)
    if found is None:
        raise HTTPException(status_code=404, detail=f"Agente '{agent_id}' não encontrado.")
    w, warehouse_id, prefix, _ = found

    resp = _sql(w, warehouse_id,
        f"SELECT serving_endpoint_name FROM {prefix}.agents_config "
        f"WHERE agent_id = '{_esc(agent_id)}' LIMIT 1")
    rows = _rows_to_dicts(resp)
    if not rows:
        raise HTTPException(status_code=404, detail=f"Agente '{agent_id}' não encontrado.")

    endpoint = (rows[0].get("serving_endpoint_name") or "").strip()
    if not endpoint:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Agente não está associado a nenhum serving endpoint. "
                "O campo 'serving_endpoint_name' precisa ser preenchido após o deploy do "
                "corp-agent-framework neste ambiente."
            ),
        )

    messages = [{"role": m.role, "content": m.content} for m in body.messages]

    try:
        result = w.api_client.do(
            "POST",
            f"/serving-endpoints/{endpoint}/invocations",
            body={
                "input": messages,
                "custom_inputs": {"agent_id": agent_id},
            },
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Erro ao chamar endpoint '{endpoint}': {exc}",
        ) from exc

    # Handle OpenAI-compatible response (most common from Databricks serving)
    choices = result.get("choices") or []
    if choices:
        content = (choices[0].get("message") or {}).get("content") or ""
        if content:
            return {"reply": content}

    # Handle ResponsesAgent output format
    for item in (result.get("output") or []):
        if isinstance(item, dict) and item.get("type") == "message":
            for c in (item.get("content") or []):
                if isinstance(c, dict) and c.get("type") == "output_text":
                    return {"reply": c.get("text", "")}

    return {"reply": str(result)}


# ── Eval dataset ──────────────────────────────────────────────

_EVAL_DATASET_DDL = """
    CREATE TABLE IF NOT EXISTS {prefix}.eval_datasets (
        id                 STRING NOT NULL,
        agent_id           STRING NOT NULL,
        request            STRING NOT NULL,
        expected_response  STRING NOT NULL,
        created_at         TIMESTAMP
    )
    USING DELTA
    COMMENT 'Eval dataset entries for agent testing.'
"""


def _ensure_eval_dataset_table(w: WorkspaceClient, warehouse_id: str, prefix: str) -> None:
    try:
        _sql(w, warehouse_id, f"CREATE SCHEMA IF NOT EXISTS {prefix}")
    except Exception:
        pass
    try:
        _sql(w, warehouse_id, _EVAL_DATASET_DDL.format(prefix=prefix))
    except Exception:
        pass


class EvalEntryBody(BaseModel):
    request: str
    expected_response: str


@router.get("/agents/{agent_id}/eval-dataset")
def list_eval_dataset(agent_id: str):
    parts = _env_client("dev")
    if parts is None:
        raise HTTPException(status_code=503, detail="Dev workspace não configurado.")
    w, catalog, schema_name, warehouse_id = parts
    prefix = f"{catalog}.{schema_name}"
    _ensure_eval_dataset_table(w, warehouse_id, prefix)
    resp = _sql(w, warehouse_id, f"""
        SELECT id, agent_id, request, expected_response, created_at
        FROM {prefix}.eval_datasets
        WHERE agent_id = '{_esc(agent_id)}'
        ORDER BY created_at ASC
    """)
    return {"entries": _rows_to_dicts(resp)}


@router.post("/agents/{agent_id}/eval-dataset", status_code=status.HTTP_201_CREATED)
def add_eval_entry(agent_id: str, body: EvalEntryBody):
    import uuid as _uuid
    parts = _env_client("dev")
    if parts is None:
        raise HTTPException(status_code=503, detail="Dev workspace não configurado.")
    w, catalog, schema_name, warehouse_id = parts
    prefix = f"{catalog}.{schema_name}"
    _ensure_eval_dataset_table(w, warehouse_id, prefix)
    entry_id = str(_uuid.uuid4())
    _sql(w, warehouse_id, f"""
        INSERT INTO {prefix}.eval_datasets (id, agent_id, request, expected_response, created_at)
        VALUES (
            '{entry_id}',
            '{_esc(agent_id)}',
            '{_esc(body.request)}',
            '{_esc(body.expected_response)}',
            current_timestamp()
        )
    """)
    return {"id": entry_id, "agent_id": agent_id, "request": body.request, "expected_response": body.expected_response}


@router.put("/agents/{agent_id}/eval-dataset/{entry_id}")
def update_eval_entry(agent_id: str, entry_id: str, body: EvalEntryBody):
    parts = _env_client("dev")
    if parts is None:
        raise HTTPException(status_code=503, detail="Dev workspace não configurado.")
    w, catalog, schema_name, warehouse_id = parts
    prefix = f"{catalog}.{schema_name}"
    _sql(w, warehouse_id, f"""
        UPDATE {prefix}.eval_datasets
        SET request = '{_esc(body.request)}',
            expected_response = '{_esc(body.expected_response)}'
        WHERE id = '{_esc(entry_id)}' AND agent_id = '{_esc(agent_id)}'
    """)
    return {"id": entry_id, "agent_id": agent_id}


@router.delete("/agents/{agent_id}/eval-dataset/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_eval_entry(agent_id: str, entry_id: str):
    parts = _env_client("dev")
    if parts is None:
        raise HTTPException(status_code=503, detail="Dev workspace não configurado.")
    w, catalog, schema_name, warehouse_id = parts
    prefix = f"{catalog}.{schema_name}"
    _sql(w, warehouse_id, f"""
        DELETE FROM {prefix}.eval_datasets
        WHERE id = '{_esc(entry_id)}' AND agent_id = '{_esc(agent_id)}'
    """)


# ── Delete agent ──────────────────────────────────────────────

@router.delete("/agents/{agent_id}")
def delete_agent(agent_id: str):
    found = _find_agent(agent_id)
    if found is None:
        raise HTTPException(status_code=404, detail=f"Agente '{agent_id}' não encontrado.")
    w, warehouse_id, prefix, env = found
    _sql(w, warehouse_id, f"""
        DELETE FROM {prefix}.agents_config WHERE agent_id = '{_esc(agent_id)}'
    """)
    return {"deleted": agent_id, "environment": env}
