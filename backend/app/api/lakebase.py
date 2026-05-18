"""
PostgreSQL (Lakebase Autoscale) connection module for corp-client-agent.

Tokens are obtained via Databricks OAuth M2M (service principal) or PAT and used as
the PostgreSQL password. Tokens are cached and refreshed ~5 min before expiry (1 h).
"""
import subprocess
import time

import psycopg
import requests
from psycopg.rows import dict_row

from app.config import settings

_token: str = ""
_token_expiry: float = 0.0


def _refresh_token() -> None:
    global _token, _token_expiry

    # Local dev: get user token via Databricks CLI (user owns the Lakebase instance)
    if settings.local_dev:
        result = subprocess.run(
            ["databricks", "auth", "token", "--output", "json",
             "--host", settings.databricks_host],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            raise RuntimeError(f"databricks auth token failed: {result.stderr}")
        import json
        data = json.loads(result.stdout)
        _token = data["access_token"]
        _token_expiry = time.time() + data.get("expires_in", 3600) - 300
        return

    # Deployed app: OAuth M2M (service principal must be granted access to Lakebase)
    if settings.databricks_client_id and settings.databricks_client_secret:
        host = settings.databricks_host.rstrip("/")
        resp = requests.post(
            f"{host}/oidc/v1/token",
            data={
                "grant_type": "client_credentials",
                "scope": "all-apis",
                "client_id": settings.databricks_client_id,
                "client_secret": settings.databricks_client_secret,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        _token = data["access_token"]
        _token_expiry = time.time() + data.get("expires_in", 3600) - 300
        return

    # PAT fallback
    if settings.databricks_token:
        _token = settings.databricks_token
        _token_expiry = time.time() + 86400
        return

    raise RuntimeError("Nenhuma credencial Databricks configurada para Lakebase.")


def _ensure_token() -> str:
    if not _token or time.time() >= _token_expiry:
        _refresh_token()
    return _token


def get_connection() -> psycopg.Connection:
    return psycopg.connect(
        host=settings.lakebase_host,
        dbname=settings.lakebase_database,
        user=settings.lakebase_username,
        password=_ensure_token(),
        sslmode="require",
        row_factory=dict_row,
    )


def execute(sql: str, params: tuple = ()) -> list[dict]:
    """Execute SQL and return a list of row dicts."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            if cur.description:
                return cur.fetchall()
            return []


def execute_one(sql: str, params: tuple = ()) -> dict | None:
    rows = execute(sql, params)
    return rows[0] if rows else None


def ensure_schema() -> None:
    """Idempotent schema migrations — run once at startup."""
    execute("""
        ALTER TABLE IF EXISTS tools_config
            ADD COLUMN IF NOT EXISTS domain TEXT NOT NULL DEFAULT 'default'
    """)
    execute("""
        ALTER TABLE IF EXISTS agents_config
            ADD COLUMN IF NOT EXISTS domain TEXT NOT NULL DEFAULT 'default'
    """)
    execute("""
        CREATE TABLE IF NOT EXISTS domain_model_approvals (
            domain      TEXT NOT NULL,
            model_name  TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'pending',
            notes       TEXT,
            updated_at  TIMESTAMPTZ DEFAULT NOW(),
            PRIMARY KEY (domain, model_name)
        )
    """)
