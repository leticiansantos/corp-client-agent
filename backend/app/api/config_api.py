"""
Config API — read-only endpoints consumed by corp-agent-framework serving endpoints.

The framework runs in other Databricks workspaces and calls these endpoints to read
agent/tool config from the SQLite store at inference time.

Auth: optional Bearer token via CORP_CONFIG_TOKEN env var.
If not set, all requests are accepted (suitable for internal/private deployments).
"""

from fastapi import APIRouter, Header, HTTPException, Query

from app.api import lakebase
from app.config import settings

router = APIRouter(prefix="/api/config")


def _check_auth(authorization: str | None) -> None:
    required = (settings.corp_config_token or "").strip()
    if not required:
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authorization header required.")
    if authorization[len("Bearer "):] != required:
        raise HTTPException(status_code=403, detail="Invalid token.")


@router.get("/agents/{domain}/{agent_id}")
def get_agent_config(
    domain: str,
    agent_id: str,
    environment: str | None = Query(default=None),
    authorization: str | None = Header(default=None),
):
    """Return a single agent's config row from the domain schema."""
    _check_auth(authorization)

    sql = (
        f'SELECT * FROM "{domain}".agents_config'
        f" WHERE agent_id = %s AND status IN ('active', 'certified')"
    )
    params: list = [agent_id]
    if environment:
        sql += " AND environment = %s"
        params.append(environment)

    row = lakebase.execute_one(sql, tuple(params))
    if row is None:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found.")
    return row


@router.get("/tools/{domain}")
def get_tools_config(
    domain: str,
    names: str = Query(..., description="Comma-separated tool names"),
    environment: str | None = Query(default=None),
    authorization: str | None = Header(default=None),
):
    """Return tool specs for the given names from the domain schema."""
    _check_auth(authorization)

    tool_names = [n.strip() for n in names.split(",") if n.strip()]
    if not tool_names:
        raise HTTPException(status_code=400, detail="'names' query param is required.")

    placeholders = ", ".join(["%s"] * len(tool_names))
    sql = (
        f'SELECT tool_name, kind, ref, description FROM "{domain}".tools_config'
        f" WHERE tool_name IN ({placeholders}) AND status = 'active'"
    )
    params: list = list(tool_names)
    if environment:
        sql += " AND environment = %s"
        params.append(environment)

    rows = lakebase.execute(sql, tuple(params))
    return {"tools": rows}


@router.get("/guardrails/{domain}")
def get_guardrails_defaults(
    domain: str,
    authorization: str | None = Header(default=None),
):
    """Return active guardrails defaults for the domain."""
    _check_auth(authorization)

    sql = (
        f'SELECT stage, name, action, params, priority FROM "{domain}".guardrails_defaults'
        " WHERE enabled = %s ORDER BY priority"
    )
    rows = lakebase.execute(sql, (True,))
    return {"guardrails": rows}


@router.get("/domain-env/{domain}/{env}")
def get_domain_env(
    domain: str,
    env: str,
    authorization: str | None = Header(default=None),
):
    """Return workspace credentials (url, token, warehouse_id) for the domain+env."""
    _check_auth(authorization)

    row = lakebase.execute_one(
        "SELECT workspace_url, token, warehouse_id FROM app.domain_envs WHERE domain = %s AND env = %s",
        (domain, env),
    )
    if row is None or not row.get("workspace_url"):
        raise HTTPException(
            status_code=404,
            detail=f"Domain env '{domain}/{env}' not found or workspace_url not configured.",
        )
    return row
