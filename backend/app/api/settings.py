"""
Settings API — workspace configuration per environment (dev / staging / prod).

Routes:
  GET  /api/settings/workspaces  — list workspace configs for all envs
  PUT  /api/settings/workspaces  — upsert workspace configs (all envs at once)
"""

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from app.api.db import _execute_sql, _rows_to_dicts, _esc
from app.config import settings

router = APIRouter(prefix="/api/settings")

ENVS = ("dev", "staging", "prod")


# ── Table helpers ──────────────────────────────────────────────

def _table() -> str:
    return f"{settings.uc_prefix}.workspace_envs"


def _ensure_table() -> None:
    _execute_sql(f"CREATE SCHEMA IF NOT EXISTS {settings.uc_prefix}")
    _execute_sql(f"""
    CREATE TABLE IF NOT EXISTS {_table()} (
      env           STRING NOT NULL,
      workspace_url STRING,
      catalog       STRING,
      schema_name   STRING,
      warehouse_id  STRING,
      token         STRING,
      notes         STRING,
      updated_at    TIMESTAMP
    )
    USING DELTA
    """)


# ── Pydantic models ────────────────────────────────────────────

class WorkspaceEnvConfig(BaseModel):
    env: str
    workspace_url: str | None = ""
    catalog: str | None = ""
    schema_name: str | None = ""
    warehouse_id: str | None = ""
    token: str | None = ""
    notes: str | None = ""


class SaveWorkspaceEnvsRequest(BaseModel):
    envs: list[WorkspaceEnvConfig]


# ── Routes ─────────────────────────────────────────────────────

@router.get("/workspaces")
def get_workspace_envs():
    """Return workspace config for each environment."""
    _ensure_table()
    response = _execute_sql(f"""
    SELECT env, workspace_url, catalog, schema_name, warehouse_id, token, notes, updated_at
    FROM {_table()}
    ORDER BY CASE env WHEN 'dev' THEN 1 WHEN 'staging' THEN 2 WHEN 'prod' THEN 3 ELSE 4 END
    """)
    rows = _rows_to_dicts(response)
    existing = {r["env"]: r for r in rows}
    str_fields = ["workspace_url", "catalog", "schema_name", "warehouse_id", "token", "notes"]
    result = []
    for env in ENVS:
        row = existing.get(env, {})
        result.append({
            "env": env,
            **{f: row.get(f) or "" for f in str_fields},
            "updated_at": row.get("updated_at"),
        })
    return {"envs": result}


@router.put("/workspaces")
def save_workspace_envs(body: SaveWorkspaceEnvsRequest):
    """Upsert workspace config for each environment."""
    _ensure_table()
    for cfg in body.envs:
        if cfg.env not in ENVS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Ambiente inválido: '{cfg.env}'. Aceitos: {', '.join(ENVS)}",
            )
        _execute_sql(f"""
        MERGE INTO {_table()} AS target
        USING (
          SELECT
            '{_esc(cfg.env)}'                         AS env,
            '{_esc(cfg.workspace_url or "")}'         AS workspace_url,
            '{_esc(cfg.catalog or "")}'               AS catalog,
            '{_esc(cfg.schema_name or "")}'           AS schema_name,
            '{_esc(cfg.warehouse_id or "")}'          AS warehouse_id,
            '{_esc(cfg.token or "")}'                 AS token,
            '{_esc(cfg.notes or "")}'                 AS notes
        ) AS source
        ON target.env = source.env
        WHEN MATCHED THEN UPDATE SET
          target.workspace_url = source.workspace_url,
          target.catalog       = source.catalog,
          target.schema_name   = source.schema_name,
          target.warehouse_id  = source.warehouse_id,
          target.token         = source.token,
          target.notes         = source.notes,
          target.updated_at    = current_timestamp()
        WHEN NOT MATCHED THEN INSERT (
          env, workspace_url, catalog, schema_name, warehouse_id, token, notes, updated_at
        ) VALUES (
          source.env, source.workspace_url, source.catalog, source.schema_name,
          source.warehouse_id, source.token, source.notes, current_timestamp()
        )
        """)
    return {"saved": len(body.envs)}
