"""
SQLite local backend — drop-in replacement for lakebase.py.

Activated automatically by lakebase.py when LAKEBASE_HOST is not set.

SQL translation (PostgreSQL → SQLite):
  "schema".table        → schema__table (prefixed table names)
  app.table             → app__table
  %s                    → ?
  NOW()                 → datetime('now')
  TEXT[], JSONB         → TEXT  (arrays stored as JSON strings)
  TIMESTAMPTZ           → TEXT
  DOUBLE PRECISION      → REAL
  ::type casts          → removed
  CREATE SCHEMA         → no-op
  ALTER TABLE ADD COLUMN IF NOT EXISTS → try/ignore if duplicate

DB file: backend/local.db
"""

import json
import re
import sqlite3
import threading
from pathlib import Path

# File is at: backend/app/api/local_db.py
# DB lives at: backend/local.db
_DB_PATH = Path(__file__).parent.parent.parent / "local.db"

# Column metadata for type coercion on SELECT
_ARRAY_COLUMNS = {"tools_enabled"}
_BOOL_COLUMNS  = {"enabled", "approval_requested"}
_JSON_COLUMNS  = {"params", "guardrails_config"}

_local_storage = threading.local()
_lock = threading.Lock()


# ── Connection ─────────────────────────────────────────────────────────────────

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _get_conn() -> sqlite3.Connection:
    """Per-thread SQLite connection."""
    if not hasattr(_local_storage, "conn") or _local_storage.conn is None:
        _local_storage.conn = _connect()
    return _local_storage.conn


# ── SQL translation ────────────────────────────────────────────────────────────

def _translate(sql: str) -> str:
    # "schema".table → schema__table  (quoted if name contains non-word chars, e.g. hyphens)
    def _schema_table(m: re.Match) -> str:
        combined = f"{m.group(1)}__{m.group(2)}"
        return f'"{combined}"' if re.search(r'\W', combined) else combined
    sql = re.sub(r'"([^"]+)"\.([\w]+)', _schema_table, sql)
    # app.table (unquoted) → app__table
    sql = re.sub(r'\bapp\.([\w]+)\b', r'app__\1', sql)
    # Parameters
    sql = sql.replace('%s', '?')
    # Functions
    sql = re.sub(r'\bNOW\(\)', "datetime('now')", sql, flags=re.IGNORECASE)
    # DDL types
    sql = sql.replace('TEXT[]', 'TEXT')
    sql = sql.replace('JSONB', 'TEXT')
    sql = sql.replace('TIMESTAMPTZ', 'TEXT')
    sql = sql.replace('DOUBLE PRECISION', 'REAL')
    # PostgreSQL casts (e.g. '{}'::jsonb)
    sql = re.sub(r'::[a-zA-Z_]+', '', sql)
    return sql


def _coerce_params(params: tuple) -> tuple:
    """Convert Python lists/dicts/bools to SQLite-compatible types."""
    result = []
    for p in params:
        if isinstance(p, list):
            result.append(json.dumps(p))
        elif isinstance(p, dict):
            result.append(json.dumps(p))
        elif isinstance(p, bool):
            result.append(1 if p else 0)
        else:
            result.append(p)
    return tuple(result)


def _to_dict(row: sqlite3.Row) -> dict:
    """Convert a sqlite3.Row to a plain dict, restoring arrays/bools/json."""
    d = {}
    for key in row.keys():
        val = row[key]
        if key in _ARRAY_COLUMNS:
            if isinstance(val, str):
                try:
                    d[key] = json.loads(val)
                except (json.JSONDecodeError, ValueError):
                    d[key] = []
            else:
                d[key] = val or []
        elif key in _BOOL_COLUMNS:
            d[key] = bool(val) if val is not None else None
        elif key in _JSON_COLUMNS:
            if isinstance(val, str):
                try:
                    d[key] = json.loads(val)
                except (json.JSONDecodeError, ValueError):
                    d[key] = {}
            else:
                d[key] = val
        else:
            d[key] = val
    return d


# ── Public API ─────────────────────────────────────────────────────────────────

def execute(sql: str, params: tuple = ()) -> list[dict]:
    """Execute SQL and return list of row dicts."""
    # CREATE SCHEMA is a no-op in SQLite
    if re.match(r'\s*CREATE\s+SCHEMA', sql, re.IGNORECASE):
        return []

    tsql   = _translate(sql)
    tparams = _coerce_params(params)

    conn = _get_conn()
    with _lock:
        try:
            cur = conn.execute(tsql, tparams)
            conn.commit()
            return [_to_dict(r) for r in cur.fetchall()] if cur.description else []
        except sqlite3.OperationalError as exc:
            # ALTER TABLE ADD COLUMN IF NOT EXISTS — ignore "duplicate column" on older SQLite
            if re.match(r'\s*ALTER\s+TABLE', sql, re.IGNORECASE) and "duplicate column" in str(exc).lower():
                return []
            raise


def execute_one(sql: str, params: tuple = ()) -> dict | None:
    rows = execute(sql, params)
    return rows[0] if rows else None


def ensure_schema() -> None:
    """Create app-level tables (idempotent). Called once at startup."""
    conn = _get_conn()
    with _lock:
        conn.execute("""CREATE TABLE IF NOT EXISTS app__domain_envs (
            domain        TEXT NOT NULL,
            env           TEXT NOT NULL,
            workspace_url TEXT,
            token         TEXT,
            warehouse_id  TEXT,
            notes         TEXT,
            updated_at    TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (domain, env)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS app__domain_model_approvals (
            domain      TEXT NOT NULL,
            model_name  TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'pending',
            notes       TEXT,
            updated_at  TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (domain, model_name)
        )""")
        conn.commit()
    # Migrate any existing domains (lock released above to avoid deadlock with list_domain_schemas)
    try:
        for domain in list_domain_schemas():
            migrate_domain(domain)
    except Exception:
        pass


def create_domain_schema(domain: str) -> None:
    """Create SQLite tables for a new domain (idempotent)."""
    idx = domain.replace("-", "_")
    conn = _get_conn()
    with _lock:
        conn.execute(f"""CREATE TABLE IF NOT EXISTS "{domain}__tools_config" (
            tool_name    TEXT NOT NULL PRIMARY KEY,
            kind         TEXT NOT NULL,
            ref          TEXT NOT NULL,
            description  TEXT,
            owner        TEXT,
            environment  TEXT NOT NULL DEFAULT 'dev',
            status       TEXT NOT NULL DEFAULT 'active',
            created_at   TEXT DEFAULT (datetime('now')),
            created_by   TEXT,
            approved_by  TEXT,
            approved_at  TEXT
        )""")
        conn.execute(f"""CREATE TABLE IF NOT EXISTS "{domain}__agents_config" (
            agent_id              TEXT NOT NULL PRIMARY KEY,
            name                  TEXT NOT NULL,
            description           TEXT,
            model                 TEXT,
            tools_enabled         TEXT,
            status                TEXT NOT NULL DEFAULT 'active',
            environment           TEXT NOT NULL DEFAULT 'dev',
            runtime_mode          TEXT,
            eval_profile          TEXT,
            min_safety_score      REAL,
            min_correctness_score REAL,
            created_at            TEXT DEFAULT (datetime('now')),
            created_by            TEXT,
            updated_at            TEXT DEFAULT (datetime('now')),
            serving_endpoint_name TEXT,
            agent_type            TEXT,
            owner_principal       TEXT,
            instructions          TEXT,
            approval_requested    INTEGER DEFAULT 0,
            mlflow_experiment_id  TEXT,
            mlflow_url            TEXT,
            eval_run_id           TEXT,
            eval_status           TEXT,
            guardrails_config     TEXT
        )""")
        conn.execute(f"""CREATE TABLE IF NOT EXISTS "{domain}__eval_datasets" (
            id                TEXT NOT NULL PRIMARY KEY,
            agent_id          TEXT NOT NULL
                              REFERENCES "{domain}__agents_config"(agent_id) ON DELETE CASCADE,
            request           TEXT NOT NULL,
            expected_response TEXT NOT NULL,
            created_at        TEXT DEFAULT (datetime('now'))
        )""")
        conn.execute(
            f'CREATE INDEX IF NOT EXISTS "idx_eval_{idx}_agent_id" '
            f'ON "{domain}__eval_datasets"(agent_id)'
        )
        _migrate_domain_locked(conn, domain)
        # Track domain so list_domain_schemas() finds it
        conn.execute(
            'INSERT OR IGNORE INTO app__domain_envs (domain, env) VALUES (?, ?)',
            (domain, 'dev'),
        )
        conn.commit()


def list_domain_schemas() -> list[str]:
    """Return all distinct domain names."""
    rows = execute("SELECT DISTINCT domain FROM app__domain_envs ORDER BY domain")
    return [r["domain"] for r in rows]


def migrate_domain(domain: str) -> None:
    """Idempotent per-domain migration (guardrails table + default seeds)."""
    conn = _get_conn()
    with _lock:
        _migrate_domain_locked(conn, domain)
        conn.commit()


def _migrate_domain_locked(conn: sqlite3.Connection, domain: str) -> None:
    """Run domain migrations while the caller already holds _lock."""
    conn.execute(f"""CREATE TABLE IF NOT EXISTS "{domain}__guardrails_defaults" (
        stage       TEXT NOT NULL,
        name        TEXT NOT NULL,
        action      TEXT NOT NULL DEFAULT 'block',
        params      TEXT DEFAULT '{{}}',
        enabled     INTEGER NOT NULL DEFAULT 1,
        priority    INTEGER NOT NULL DEFAULT 0,
        description TEXT,
        created_at  TEXT DEFAULT (datetime('now')),
        updated_at  TEXT DEFAULT (datetime('now')),
        PRIMARY KEY (stage, name)
    )""")
    for row in [
        ('input',  'pii_detector',    'block',    '{}', 0, 'Blocks messages containing PII (CPF, email, phone, credit card)'),
        ('input',  'prompt_injection', 'block',    '{}', 1, 'Blocks common prompt injection attempts'),
        ('output', 'pii_scrubber',     'sanitize', '{}', 0, 'Redacts PII from agent responses'),
    ]:
        conn.execute(
            f'INSERT OR IGNORE INTO "{domain}__guardrails_defaults" '
            f'(stage, name, action, params, priority, description) VALUES (?,?,?,?,?,?)',
            row,
        )
