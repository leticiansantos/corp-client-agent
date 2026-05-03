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

from databricks.sdk import WorkspaceClient
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from app.api.db import _execute_sql, _rows_to_dicts, _esc
from app.api.tools import _env_client, _get_env_config, _sql, _ENVS_DESC, _ENV_RANK
from app.config import settings

router = APIRouter(prefix="/api")

ENVS = ("dev", "staging", "prod")

VALID_STATUSES = {"draft", "evaluating", "qa", "approved", "deployed", "disabled"}

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
    "ADD COLUMN environment           STRING",
    "ADD COLUMN runtime_mode          STRING",
    "ADD COLUMN created_by            STRING",
    "ADD COLUMN updated_at            TIMESTAMP",
    "ADD COLUMN eval_profile          STRING",
    "ADD COLUMN min_safety_score      DOUBLE",
    "ADD COLUMN min_correctness_score DOUBLE",
    # Extra columns our client layer adds
    "ADD COLUMN agent_type       STRING",
    "ADD COLUMN owner_principal  STRING",
    "ADD COLUMN instructions     STRING",
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

@router.get("/agents")
def list_agents():
    """Aggregate agents from all configured envs, highest-env record wins for duplicates."""
    best: dict[str, tuple[int, dict]] = {}
    for env in ENVS:
        parts = _env_client(env)
        if parts is None:
            continue
        w, catalog, schema_name, warehouse_id = parts
        prefix = f"{catalog}.{schema_name}"
        rank = _ENV_RANK.get(env, 0)
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
                       eval_profile,
                       min_safety_score,
                       min_correctness_score,
                       environment,
                       status,
                       runtime_mode,
                       created_at,
                       updated_at
                FROM {prefix}.agents_config
                ORDER BY created_at DESC
            """)
            for row in _rows_to_dicts(resp):
                key = row.get("agent_id") or ""
                if not key:
                    continue
                row["environment"] = row.get("environment") or env
                current_rank, _ = best.get(key, (-1, {}))
                if rank > current_rank:
                    best[key] = (rank, row)
        except Exception:
            continue

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
        agents.append(row)

    return {"agents": agents}


# ── Register agent ────────────────────────────────────────────

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
        eval_profile          = source.eval_profile,
        min_safety_score      = source.min_safety_score,
        min_correctness_score = source.min_correctness_score,
        runtime_mode          = source.runtime_mode,
        updated_at            = current_timestamp()
    WHEN NOT MATCHED THEN INSERT (
        agent_id, name, agent_type, owner_principal, description, instructions,
        tools_enabled, model, eval_profile, min_safety_score, min_correctness_score,
        environment, status, runtime_mode, created_at, created_by, updated_at
    ) VALUES (
        source.agent_id, source.name, source.agent_type, source.owner_principal,
        source.description, source.instructions, source.tools_enabled, source.model,
        source.eval_profile, source.min_safety_score, source.min_correctness_score,
        'dev', 'draft', source.runtime_mode,
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
