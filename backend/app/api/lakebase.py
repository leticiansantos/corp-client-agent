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
from psycopg_pool import ConnectionPool

from app.config import settings

_token: str = ""
_token_expiry: float = 0.0
_pool: ConnectionPool | None = None


def _refresh_token() -> None:
    global _token, _token_expiry

    # Priority 1: OAuth M2M via service principal (works locally and deployed)
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

    # Priority 2: Databricks CLI (local dev with personal user account)
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

    # Priority 3: PAT fallback
    if settings.databricks_token:
        _token = settings.databricks_token
        _token_expiry = time.time() + 86400
        return

    raise RuntimeError("Nenhuma credencial Databricks configurada para Lakebase.")


def _ensure_token() -> str:
    global _pool
    if not _token or time.time() >= _token_expiry:
        _refresh_token()
        # Invalidate pool so connections are rebuilt with the new token
        if _pool and not _pool.closed:
            try:
                _pool.close()
            except Exception:
                pass
        _pool = None
    return _token


def _conninfo() -> str:
    token = _ensure_token()
    host  = settings.lakebase_host
    db    = settings.lakebase_database
    user  = settings.databricks_client_id
    # psycopg conninfo string — password is URL-encoded inside the library
    return (
        f"host={host} dbname={db} user={user} password={token} "
        f"sslmode=require"
    )


def _get_pool() -> ConnectionPool:
    global _pool
    if _pool is None or _pool.closed:
        _pool = ConnectionPool(
            conninfo=_conninfo(),
            min_size=1,
            max_size=5,
            kwargs={"row_factory": dict_row},
            open=True,
        )
    return _pool


def get_connection() -> psycopg.Connection:
    """Return a connection from the pool (use as context manager)."""
    return _get_pool().getconn()


def execute(sql: str, params: tuple = ()) -> list[dict]:
    """Execute SQL and return a list of row dicts."""
    pool = _get_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            if cur.description:
                return cur.fetchall()
            return []


def execute_one(sql: str, params: tuple = ()) -> dict | None:
    rows = execute(sql, params)
    return rows[0] if rows else None


def create_domain_schema(domain: str) -> None:
    """Create PostgreSQL schema + tables for a new domain (idempotent)."""
    idx = domain.replace("-", "_")
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{domain}"')
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS "{domain}".tools_config (
                    tool_name    TEXT        NOT NULL PRIMARY KEY,
                    kind         TEXT        NOT NULL,
                    ref          TEXT        NOT NULL,
                    description  TEXT,
                    owner        TEXT,
                    environment  TEXT        NOT NULL DEFAULT 'dev',
                    status       TEXT        NOT NULL DEFAULT 'active',
                    created_at   TIMESTAMPTZ DEFAULT NOW(),
                    created_by   TEXT,
                    approved_by  TEXT,
                    approved_at  TIMESTAMPTZ
                )
            """)
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS "{domain}".agents_config (
                    agent_id              TEXT             NOT NULL PRIMARY KEY,
                    name                  TEXT             NOT NULL,
                    description           TEXT,
                    model                 TEXT,
                    tools_enabled         TEXT[],
                    status                TEXT             NOT NULL DEFAULT 'active',
                    environment           TEXT             NOT NULL DEFAULT 'dev',
                    runtime_mode          TEXT,
                    eval_profile          TEXT,
                    min_safety_score      DOUBLE PRECISION,
                    min_correctness_score DOUBLE PRECISION,
                    created_at            TIMESTAMPTZ      DEFAULT NOW(),
                    created_by            TEXT,
                    updated_at            TIMESTAMPTZ      DEFAULT NOW(),
                    serving_endpoint_name TEXT,
                    agent_type            TEXT,
                    owner_principal       TEXT,
                    instructions          TEXT,
                    approval_requested    BOOLEAN          DEFAULT FALSE,
                    mlflow_experiment_id  TEXT,
                    mlflow_url            TEXT,
                    eval_run_id           TEXT,
                    eval_status           TEXT,
                    guardrails_config     JSONB
                )
            """)
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS "{domain}".eval_datasets (
                    id                TEXT        NOT NULL PRIMARY KEY,
                    agent_id          TEXT        NOT NULL
                                      REFERENCES "{domain}".agents_config(agent_id) ON DELETE CASCADE,
                    request           TEXT        NOT NULL,
                    expected_response TEXT        NOT NULL,
                    created_at        TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            cur.execute(f"""
                CREATE INDEX IF NOT EXISTS "idx_eval_{idx}_agent_id"
                ON "{domain}".eval_datasets(agent_id)
            """)
            _migrate_domain(cur, domain)
        conn.commit()


def list_domain_schemas() -> list[str]:
    """Return all domain names from app.domain_envs."""
    rows = execute("SELECT DISTINCT domain FROM app.domain_envs ORDER BY domain")
    return [r["domain"] for r in rows]


def migrate_domain(domain: str) -> None:
    """Public idempotent per-domain migration — safe to call on every request."""
    pool = _get_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            _migrate_domain(cur, domain)
        conn.commit()


def _migrate_domain(cur, domain: str) -> None:
    """Idempotent per-domain migrations — called for every known domain at startup.

    NOTE: No ALTER TABLE here — the service principal is not the table owner and
    cannot run DDL on tables it doesn't own. Schema migrations that require ALTER TABLE
    must be run via setup/lakebase/setup_lakebase.py --migrate as the owner user.
    """
    # guardrails_defaults table
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS "{domain}".guardrails_defaults (
            stage       TEXT NOT NULL,
            name        TEXT NOT NULL,
            action      TEXT NOT NULL DEFAULT 'block',
            params      JSONB DEFAULT '{{}}'::jsonb,
            enabled     BOOLEAN NOT NULL DEFAULT true,
            priority    INT NOT NULL DEFAULT 0,
            description TEXT,
            created_at  TIMESTAMPTZ DEFAULT NOW(),
            updated_at  TIMESTAMPTZ DEFAULT NOW(),
            PRIMARY KEY (stage, name)
        )
    """)
    # Seed default entries (idempotent)
    cur.execute(f"""
        INSERT INTO "{domain}".guardrails_defaults (stage, name, action, params, priority, description)
        VALUES
            ('input',  'pii_detector',    'block',    '{{}}'::jsonb, 0, 'Blocks messages containing PII (CPF, email, phone, credit card)'),
            ('input',  'prompt_injection', 'block',    '{{}}'::jsonb, 1, 'Blocks common prompt injection attempts'),
            ('output', 'pii_scrubber',     'sanitize', '{{}}'::jsonb, 0, 'Redacts PII from agent responses')
        ON CONFLICT (stage, name) DO NOTHING
    """)


def ensure_schema() -> None:
    """Idempotent schema migrations — run once at startup."""
    execute("CREATE SCHEMA IF NOT EXISTS app")
    execute("""
        CREATE TABLE IF NOT EXISTS app.domain_envs (
            domain        TEXT        NOT NULL,
            env           TEXT        NOT NULL,
            workspace_url TEXT,
            token         TEXT,
            warehouse_id  TEXT,
            notes         TEXT,
            updated_at    TIMESTAMPTZ DEFAULT NOW(),
            PRIMARY KEY (domain, env)
        )
    """)
    # Idempotent: add warehouse_id column if it didn't exist yet
    execute("""
        ALTER TABLE app.domain_envs
        ADD COLUMN IF NOT EXISTS warehouse_id TEXT
    """)
    execute("""
        CREATE TABLE IF NOT EXISTS app.domain_model_approvals (
            domain      TEXT NOT NULL,
            model_name  TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'pending',
            notes       TEXT,
            updated_at  TIMESTAMPTZ DEFAULT NOW(),
            PRIMARY KEY (domain, model_name)
        )
    """)
    # Run per-domain migrations for all existing domains
    try:
        domains = list_domain_schemas()
        if domains:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    for domain in domains:
                        _migrate_domain(cur, domain)
                conn.commit()
    except Exception:
        pass  # Domains may not exist yet on first boot
