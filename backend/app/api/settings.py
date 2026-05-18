"""
Settings API — workspace configuration per environment (dev / staging / prod).

Routes:
  GET  /api/settings/workspaces                    — list workspace configs for all envs
  PUT  /api/settings/workspaces                    — upsert workspace configs (all envs at once)
  GET  /api/settings/framework-endpoints           — endpoint status per env
  POST /api/settings/framework-endpoints/{env}/deploy — full framework setup + endpoint deploy
"""

import base64 as _b64
import logging as _logging
import os as _os
import subprocess as _subprocess
import sys as _sys
import tempfile as _tempfile
import threading as _threading
import time as _time
from concurrent.futures import ThreadPoolExecutor as _ThreadPoolExecutor, as_completed as _as_completed

_log = _logging.getLogger(__name__)

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from app.api.db import _execute_sql, _rows_to_dicts, _esc
from app.config import settings

router = APIRouter(prefix="/api/settings")

ENVS = ("dev", "staging", "prod")

ENDPOINT_NAME_TPL        = "corp-config-driven-agent-{env}"
DOMAIN_ENDPOINT_NAME_TPL = "corp-config-driven-agent-{domain}-{env}"
_CORP_WHL_VERSION  = "0.3.13"
_CORP_MODEL_SUFFIX = "corp_config_driven_agent"

# In-process deploy tracking (survives server restart via jobs API check)
_DEPLOYING_ENVS: set[str] = set()
_DEPLOYING_LOCK  = _threading.Lock()
# Human-readable progress step shown while deploying
_DEPLOY_STEP:     dict[str, str] = {}
_DEPLOY_STEP_IDX: dict[str, int] = {}
# Last deploy error per env (persists after deploy finishes for UI display)
_DEPLOY_ERROR:    dict[str, str] = {}
# Total number of deploy steps — must match DEPLOY_STEPS array in the frontend
_DEPLOY_TOTAL_STEPS = 11

# Domain-keyed tracking (key = "domain::env")
_DOMAIN_DEPLOYING:   set[str]       = set()
_DOMAIN_LOCK         = _threading.Lock()
_DOMAIN_STEP:        dict[str, str] = {}
_DOMAIN_STEP_IDX:    dict[str, int] = {}
_DOMAIN_ERROR:       dict[str, str] = {}
_DOMAIN_RUN_ID:      dict[str, int] = {}
# Must match DEPLOY_STEPS array in the frontend
DOMAIN_DEPLOY_STEPS = 9


# ── App settings table ─────────────────────────────────────────

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


# ── Env workspace helpers ──────────────────────────────────────

def _get_env_config_from_db(env: str) -> dict | None:
    _ensure_table()
    response = _execute_sql(f"""
    SELECT workspace_url, catalog, schema_name, warehouse_id, token
    FROM {_table()} WHERE env = '{_esc(env)}'
    """)
    rows = _rows_to_dicts(response)
    return rows[0] if rows else None


class _EnvSession:
    """
    Lightweight HTTP client for env workspace operations.
    Uses requests directly with Bearer token — avoids Databricks SDK which
    picks up DATABRICKS_CLIENT_ID/SECRET from env vars and tries OAuth.
    """

    def __init__(self, host: str, token: str) -> None:
        import requests
        self._host = host.rstrip("/")
        self._s = requests.Session()
        self._s.headers.update({"Authorization": f"Bearer {token}"})

    def _raise(self, r) -> None:
        if not r.ok:
            try:
                detail = r.json().get("message") or r.text
            except Exception:
                detail = r.text
            raise RuntimeError(f"HTTP {r.status_code} {r.reason}: {detail}")

    def _get(self, path: str, **params) -> dict:
        r = self._s.get(f"{self._host}{path}", params=params or None, timeout=60)
        self._raise(r)
        return r.json() if r.content else {}

    def _post(self, path: str, body: dict | None = None) -> dict:
        r = self._s.post(f"{self._host}{path}", json=body or {}, timeout=60)
        self._raise(r)
        return r.json() if r.content else {}

    # ── Unity Catalog — no warehouse required ────────────────────

    def _uc_post(self, path: str, body: dict, label: str) -> None:
        """POST to UC API; skip if already exists (409), raise with detail on error."""
        r = self._s.post(f"{self._host}{path}", json=body, timeout=30)
        if r.status_code in (200, 201, 409):
            return
        try:
            detail = r.json().get("message") or r.text
        except Exception:
            detail = r.text
        raise RuntimeError(f"Falha ao criar {label}: {detail}")

    def uc_ensure_catalog(self, catalog: str) -> None:
        # Check existence first — avoids storage-root errors on Azure metastores
        r = self._s.get(f"{self._host}/api/2.1/unity-catalog/catalogs/{catalog}", timeout=30)
        if r.status_code == 200:
            return
        self._uc_post("/api/2.1/unity-catalog/catalogs", {"name": catalog}, f"catalog '{catalog}'")

    def uc_ensure_schema(self, catalog: str, schema: str) -> None:
        r = self._s.get(f"{self._host}/api/2.1/unity-catalog/schemas/{catalog}.{schema}", timeout=30)
        if r.status_code == 200:
            return
        self._uc_post("/api/2.1/unity-catalog/schemas",
                      {"name": schema, "catalog_name": catalog}, f"schema '{catalog}.{schema}'")

    def uc_ensure_volume(self, catalog: str, schema: str, volume: str) -> None:
        r = self._s.get(f"{self._host}/api/2.1/unity-catalog/volumes/{catalog}.{schema}.{volume}", timeout=30)
        if r.status_code == 200:
            return
        self._uc_post("/api/2.1/unity-catalog/volumes",
                      {"name": volume, "catalog_name": catalog, "schema_name": schema,
                       "volume_type": "MANAGED"}, f"volume '{catalog}.{schema}.{volume}'")

    # ── Warehouses ──────────────────────────────────────────────

    def get_warehouse_id(self) -> str:
        data = self._get("/api/2.0/sql/warehouses")
        warehouses = data.get("warehouses", [])
        if not warehouses:
            raise ValueError("Nenhum SQL Warehouse encontrado no workspace.")
        return warehouses[0]["id"]

    # ── SQL execution ────────────────────────────────────────────

    def exec_sql(self, warehouse_id: str, statement: str) -> None:
        resp = self._post("/api/2.0/sql/statements", {
            "warehouse_id": warehouse_id,
            "statement": statement.strip(),
            "wait_timeout": "50s",
        })
        stmt_id = resp.get("statement_id")
        deadline = _time.time() + 300
        while True:
            state = ((resp.get("status") or {}).get("state") or "UNKNOWN").upper()
            if "SUCCEEDED" in state:
                return
            if any(s in state for s in ("FAILED", "CANCELED", "CLOSED")):
                err = ((resp.get("status") or {}).get("error") or {}).get("message", state)
                raise RuntimeError(f"SQL falhou: {err}")
            if _time.time() > deadline:
                raise RuntimeError(f"SQL timeout (state={state})")
            _time.sleep(2)
            if stmt_id:
                resp = self._get(f"/api/2.0/sql/statements/{stmt_id}")

    # ── Files (Volume) ───────────────────────────────────────────

    def file_exists(self, volume_path: str) -> bool:
        r = self._s.head(f"{self._host}/api/2.0/fs/files{volume_path}", timeout=30)
        return r.status_code == 200

    def file_upload(self, volume_path: str, data, overwrite: bool = True) -> None:
        params = {"overwrite": "true"} if overwrite else {}
        r = self._s.put(
            f"{self._host}/api/2.0/fs/files{volume_path}",
            data=data, params=params, timeout=300,
        )
        r.raise_for_status()

    # ── Workspace notebooks ──────────────────────────────────────

    def workspace_mkdirs(self, path: str) -> None:
        self._post("/api/2.0/workspace/mkdirs", {"path": path})

    def workspace_import_notebook(self, path: str, content_b64: str) -> None:
        self._post("/api/2.0/workspace/import", {
            "path": path, "format": "SOURCE", "language": "PYTHON",
            "content": content_b64, "overwrite": True,
        })

    # ── Clusters ─────────────────────────────────────────────────

    def get_running_uc_cluster_id(self) -> str | None:
        _UC_MODES = {"SINGLE_USER", "USER_ISOLATION"}
        data = self._get("/api/2.0/clusters/list")
        for c in data.get("clusters", []):
            if (c.get("state", "").upper() in ("RUNNING", "RESIZING") and
                    c.get("data_security_mode", "").upper() in _UC_MODES):
                return c["cluster_id"]
        return None

    def get_node_type(self) -> str:
        """Return the first non-deprecated node type available in this workspace."""
        try:
            data = self._get("/api/2.0/clusters/list-node-types")
            for nt in data.get("node_types", []):
                if not nt.get("is_deprecated"):
                    return nt["node_type_id"]
        except Exception:
            pass
        return "Standard_DS3_v2"  # Azure fallback

    # ── Jobs ─────────────────────────────────────────────────────

    def submit_run(self, run_name: str, task_spec: dict) -> int:
        resp = self._post("/api/2.0/jobs/runs/submit", {
            "run_name": run_name,
            "tasks": [{"task_key": "deploy", **task_spec}],
        })
        return resp["run_id"]

    def get_run(self, run_id: int) -> dict:
        return self._get("/api/2.0/jobs/runs/get", run_id=run_id)

    def get_active_deploy_run_id(self, endpoint_name: str) -> int | None:
        """Return run_id of an active deploy job, or None if not found."""
        try:
            data = self._get("/api/2.1/jobs/runs/list", active_only="true")
            for run in data.get("runs", []):
                if run.get("run_name") == f"deploy-{endpoint_name}":
                    return run.get("run_id")
        except Exception:
            pass
        return None

    def has_active_deploy_job(self, endpoint_name: str) -> bool:
        return self.get_active_deploy_run_id(endpoint_name) is not None

    # ── Serving endpoints ─────────────────────────────────────────

    def get_endpoint_state(self, endpoint_name: str) -> str:
        r = self._s.get(
            f"{self._host}/api/2.0/serving-endpoints/{endpoint_name}",
            timeout=30,
        )
        if r.status_code == 404:
            return "NOT_FOUND"
        r.raise_for_status()
        state = ((r.json().get("state") or {}).get("ready") or "")
        return "READY" if str(state).upper() == "READY" else "NOT_READY"

    def list_endpoints(self) -> list[dict]:
        data = self._get("/api/2.0/serving-endpoints")
        result = []
        for ep in data.get("endpoints", []):
            config = ep.get("config") or {}
            served = config.get("served_entities") or config.get("served_models") or []
            entity = served[0] if served else None
            model_name = ""
            if entity:
                model_name = entity.get("entity_name") or entity.get("model_name") or ""
            result.append({
                "name":       ep.get("name", ""),
                "state":      (ep.get("state") or {}).get("ready", "NOT_READY"),
                "creator":    ep.get("creator", ""),
                "model_name": model_name,
            })
        return result


def _get_env_session(env_cfg: dict) -> _EnvSession:
    host  = (env_cfg.get("workspace_url") or "").strip().rstrip("/")
    token = (env_cfg.get("token") or "").strip()
    if not host or not token:
        raise ValueError("workspace_url e token são obrigatórios.")
    return _EnvSession(host, token)


def _check_endpoint_state(sess: _EnvSession, endpoint_name: str) -> str:
    try:
        return sess.get_endpoint_state(endpoint_name)
    except Exception as exc:
        err = str(exc).lower()
        if any(s in err for s in ("does not exist", "not found", "no such", "404")):
            return "NOT_FOUND"
        raise


def _check_active_deploy_job(sess: _EnvSession, endpoint_name: str) -> bool:
    return sess.has_active_deploy_job(endpoint_name)


# ── SQL execution in env workspace ────────────────────────────

def _env_sql(sess: _EnvSession, warehouse_id: str, statement: str) -> None:
    """Execute SQL in the env workspace warehouse (not the app workspace)."""
    try:
        sess.exec_sql(warehouse_id, statement)
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"Erro ao executar SQL: {exc}") from exc


# ── WHL build & upload ─────────────────────────────────────────

def _framework_dir() -> str:
    return _os.path.abspath(
        _os.path.join(_os.path.dirname(__file__), "../../../../corp-agent-framework")
    )


def _build_whl() -> str:
    """Build corp_agent_framework WHL from local source. Returns local .whl path."""
    fdir = _framework_dir()
    if not _os.path.isdir(fdir):
        raise RuntimeError(
            f"corp-agent-framework não encontrado em {fdir}. "
            "Verifique a estrutura do repositório."
        )
    tmpdir = _tempfile.mkdtemp(prefix="corp_whl_")
    result = _subprocess.run(
        [_sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "-q", "-w", tmpdir, fdir],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Falha ao buildar WHL: {result.stderr}")
    whl_files = [f for f in _os.listdir(tmpdir) if f.endswith(".whl")]
    if not whl_files:
        raise RuntimeError("pip wheel não gerou nenhum arquivo .whl")
    return _os.path.join(tmpdir, whl_files[0])


def _get_local_whl() -> str:
    """
    In local_dev: find existing WHL in dist/ without rebuilding.
    Builds once into dist/ if not found there.
    """
    dist_dir = _os.path.join(_framework_dir(), "dist")
    _os.makedirs(dist_dir, exist_ok=True)
    existing = sorted(
        (f for f in _os.listdir(dist_dir) if f.endswith(".whl")),
        reverse=True,
    )
    if existing:
        return _os.path.join(dist_dir, existing[0])
    # First time: build into dist/
    fdir = _framework_dir()
    result = _subprocess.run(
        [_sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "-q", "-w", dist_dir, fdir],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Falha ao buildar WHL: {result.stderr}")
    whl_files = [f for f in _os.listdir(dist_dir) if f.endswith(".whl")]
    if not whl_files:
        raise RuntimeError("pip wheel não gerou nenhum arquivo .whl")
    return _os.path.join(dist_dir, whl_files[0])


# ── Deploy script generator ────────────────────────────────────

def _generate_deploy_script(
    catalog: str, schema: str, warehouse_id: str, environment: str,
    workspace_url: str = "", token: str = "",
) -> str:
    endpoint_name = ENDPOINT_NAME_TPL.format(env=environment)
    model_name    = f"{catalog}.{schema}.{_CORP_MODEL_SUFFIX}_{environment}"
    config_table  = f"{catalog}.{schema}.agents_config"
    tools_table   = f"{catalog}.{schema}.tools_config"
    whl_path      = f"/Volumes/{catalog}/{schema}/libs/corp_agent_framework-{_CORP_WHL_VERSION}-py3-none-any.whl"
    whl_local = f"/tmp/corp_agent_framework-{_CORP_WHL_VERSION}-py3-none-any.whl"
    return f"""\
# Databricks notebook source
# COMMAND ----------

# Copy WHL from UC Volume to local /tmp so %pip can install it on any cluster type
dbutils.fs.cp("{whl_path}", "file://{whl_local}")

# COMMAND ----------

# MAGIC %pip install -q {whl_local} "mlflow>=2.15.0" "databricks-sdk>=0.51.0" "databricks-openai>=0.5.0" "pydantic>=2.5.0"

# COMMAND ----------

# Override system typing_extensions cached at DBR startup — pydantic-core needs Sentinel (>=4.6.0)
import sys as _sys, importlib.util as _ilu
_SYSTEM_TE = "/databricks/python"
for _p in _sys.path:
    _te = __import__("os").path.join(_p, "typing_extensions.py")
    if __import__("os").path.exists(_te) and _SYSTEM_TE not in _te:
        _spec = _ilu.spec_from_file_location("typing_extensions", _te)
        _mod = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        if hasattr(_mod, "Sentinel"):
            _sys.modules["typing_extensions"] = _mod
            break

import os
import corp_agent_framework
import mlflow
from corp_agent_framework.runtime_config_agent import ConfigDrivenAgent
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.serving import EndpointCoreConfigInput, ServedEntityInput

CATALOG       = "{catalog}"
SCHEMA        = "{schema}"
WAREHOUSE_ID  = "{warehouse_id}"
ENVIRONMENT   = "{environment}"
MODEL_NAME    = "{model_name}"
ENDPOINT_NAME = "{endpoint_name}"

agent = ConfigDrivenAgent(
    config_table="{config_table}",
    tools_table="{tools_table}",
    warehouse_id=WAREHOUSE_ID,
    environment=ENVIRONMENT,
)

mlflow.set_registry_uri("databricks-uc")

_pkg_dir = os.path.dirname(corp_agent_framework.__file__)

with mlflow.start_run(run_name=f"{{ENDPOINT_NAME}}-deploy"):
    model_info = mlflow.pyfunc.log_model(
        artifact_path="agent",
        python_model=agent,
        code_paths=[_pkg_dir],
        pip_requirements=[
            "databricks-sdk>=0.51.0",
            "databricks-openai>=0.5.0",
            "mlflow>=2.15.0",
            "pydantic>=2.5.0",
        ],
        registered_model_name=MODEL_NAME,
        resources=[],
    )

# Get registered version: prefer model_info, fall back to MLflow client query
_reg_version = getattr(model_info, "registered_model_version", None)
if _reg_version:
    latest = str(_reg_version)
else:
    _client = mlflow.tracking.MlflowClient()
    _versions = _client.search_model_versions(f"name='{{MODEL_NAME}}'")
    latest = str(max(int(v.version) for v in _versions))
print(f"Model registered: {{MODEL_NAME}} version {{latest}}")

w = WorkspaceClient()

ep_config = EndpointCoreConfigInput(
    served_entities=[
        ServedEntityInput(
            name=ENDPOINT_NAME,
            entity_name=MODEL_NAME,
            entity_version=latest,
            workload_size="Small",
            scale_to_zero_enabled=True,
            environment_vars={{
                # Credentials so the serving container can call the SQL warehouse
                # and read agents_config / tools_config at inference time.
                "DATABRICKS_HOST":  "{workspace_url}",
                "DATABRICKS_TOKEN": "{token}",
            }},
        )
    ],
)

try:
    w.serving_endpoints.create_and_wait(name=ENDPOINT_NAME, config=ep_config)
    print(f"Endpoint created: {{ENDPOINT_NAME}}")
except Exception as exc:
    err = str(exc).lower()
    if "already exists" in err or "currently being updated" in err:
        w.serving_endpoints.wait_get_serving_endpoint_not_updating(name=ENDPOINT_NAME)
        w.serving_endpoints.update_config_and_wait(
            name=ENDPOINT_NAME, served_entities=ep_config.served_entities)
        print(f"Endpoint updated: {{ENDPOINT_NAME}}")
    else:
        raise

print("DEPLOY_DONE")
"""


def _generate_deploy_script_lakebase(
    catalog: str, schema: str, environment: str, domain: str,
    workspace_url: str = "", token: str = "",
    lakebase_host: str = "", lakebase_database: str = "", lakebase_username: str = "",
) -> str:
    """Generate a deploy notebook for the Lakebase-aware corp_agent_framework.

    The serving endpoint reads agent/tool config from Lakebase (PostgreSQL),
    so no Delta catalog/schema/warehouse_id is needed at inference time.
    """
    endpoint_name = DOMAIN_ENDPOINT_NAME_TPL.format(domain=domain, env=environment)
    model_name    = f"{catalog}.{schema}.{_CORP_MODEL_SUFFIX}_{environment}"
    whl_path      = f"/Volumes/{catalog}/{schema}/libs/corp_agent_framework-{_CORP_WHL_VERSION}-py3-none-any.whl"
    whl_local     = f"/tmp/corp_agent_framework-{_CORP_WHL_VERSION}-py3-none-any.whl"
    return f"""\
# Databricks notebook source
# COMMAND ----------

# Copy WHL from UC Volume to local /tmp so %pip can install it on any cluster type
dbutils.fs.cp("{whl_path}", "file://{whl_local}")

# COMMAND ----------

# MAGIC %pip install -q {whl_local} "mlflow>=2.15.0" "databricks-sdk>=0.51.0" "databricks-openai>=0.5.0" "pydantic>=2.5.0" "psycopg[binary]>=3.0" "requests>=2.28.0"

# COMMAND ----------

# Override system typing_extensions cached at DBR startup — pydantic-core needs Sentinel (>=4.6.0)
import sys as _sys, importlib.util as _ilu
_SYSTEM_TE = "/databricks/python"
for _p in _sys.path:
    _te = __import__("os").path.join(_p, "typing_extensions.py")
    if __import__("os").path.exists(_te) and _SYSTEM_TE not in _te:
        _spec = _ilu.spec_from_file_location("typing_extensions", _te)
        _mod = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        if hasattr(_mod, "Sentinel"):
            _sys.modules["typing_extensions"] = _mod
            break

import os
import corp_agent_framework
import mlflow
from corp_agent_framework.runtime_config_agent import ConfigDrivenAgent
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.serving import EndpointCoreConfigInput, ServedEntityInput

CATALOG       = "{catalog}"
SCHEMA        = "{schema}"
ENVIRONMENT   = "{environment}"
DOMAIN        = "{domain}"
MODEL_NAME    = "{model_name}"
ENDPOINT_NAME = "{endpoint_name}"

# Lakebase-aware: table_name/warehouse_id are ignored at inference time (kept for compat)
agent = ConfigDrivenAgent(environment=ENVIRONMENT, domain=DOMAIN)

mlflow.set_registry_uri("databricks-uc")

_pkg_dir = os.path.dirname(corp_agent_framework.__file__)

with mlflow.start_run(run_name=f"{{ENDPOINT_NAME}}-deploy"):
    model_info = mlflow.pyfunc.log_model(
        artifact_path="agent",
        python_model=agent,
        code_paths=[_pkg_dir],
        pip_requirements=[
            "databricks-sdk>=0.51.0",
            "databricks-openai>=0.5.0",
            "mlflow>=2.15.0",
            "pydantic>=2.5.0",
            "psycopg[binary]>=3.0",
            "requests>=2.28.0",
        ],
        registered_model_name=MODEL_NAME,
        resources=[],
    )

# Get registered version: prefer model_info, fall back to MLflow client query
_reg_version = getattr(model_info, "registered_model_version", None)
if _reg_version:
    latest = str(_reg_version)
else:
    _client = mlflow.tracking.MlflowClient()
    _versions = _client.search_model_versions(f"name='{{MODEL_NAME}}'")
    latest = str(max(int(v.version) for v in _versions))
print(f"Model registered: {{MODEL_NAME}} version {{latest}}")

w = WorkspaceClient()

ep_config = EndpointCoreConfigInput(
    served_entities=[
        ServedEntityInput(
            name=ENDPOINT_NAME,
            entity_name=MODEL_NAME,
            entity_version=latest,
            workload_size="Small",
            scale_to_zero_enabled=True,
            environment_vars={{
                # Env workspace credentials — used by agent for Genie/VS calls
                "DATABRICKS_HOST":            "{workspace_url}",
                "DATABRICKS_TOKEN":           "{token}",
                # Lakebase (PostgreSQL) — central store for all environments
                "LAKEBASE_HOST":              "{lakebase_host}",
                "LAKEBASE_DATABASE":          "{lakebase_database}",
                "LAKEBASE_USERNAME":          "{lakebase_username}",
            }},
        )
    ],
)

try:
    w.serving_endpoints.create_and_wait(name=ENDPOINT_NAME, config=ep_config)
    print(f"Endpoint created: {{ENDPOINT_NAME}}")
except Exception as exc:
    err = str(exc).lower()
    if "already exists" in err or "currently being updated" in err:
        w.serving_endpoints.wait_get_serving_endpoint_not_updating(name=ENDPOINT_NAME)
        w.serving_endpoints.update_config_and_wait(
            name=ENDPOINT_NAME, served_entities=ep_config.served_entities)
        print(f"Endpoint updated: {{ENDPOINT_NAME}}")
    else:
        raise

print("DEPLOY_DONE")
"""


# ── Full framework setup + deploy (background thread) ──────────

def _deploy_framework_background(env: str, env_cfg: dict) -> None:
    """
    Full corp_agent_framework setup in the env workspace, in order:
      1.  Create catalog
      2.  Create schema
      3.  Create tools_config table
      4.  Create agents_config table
      5.  Create Volume for libs
      6.  Build corp_agent_framework WHL from local source
      7.  Upload WHL to Volume
      8.  Skip endpoint deploy if already READY
      9.  Upload deploy notebook
     10.  Submit one-shot job
     11.  Poll until job terminates (max 30 min)

    Endpoint deploy (steps 9-11) only runs after all infra steps succeed.
    """

    catalog      = env_cfg.get("catalog") or "corp_agent_framework"
    schema       = env_cfg.get("schema_name") or "agents"
    warehouse_id = (env_cfg.get("warehouse_id") or "").strip()

    _step_counter = [-1]

    def _step(msg: str) -> None:
        _step_counter[0] += 1
        _DEPLOY_STEP[env]     = msg
        _DEPLOY_STEP_IDX[env] = _step_counter[0]

    try:
        if not warehouse_id:
            raise ValueError(
                f"Warehouse ID não configurado para o ambiente '{env}'. "
                "Preencha o campo antes de fazer o deploy."
            )

        sess          = _get_env_session(env_cfg)
        endpoint_name = ENDPOINT_NAME_TPL.format(env=env)

        # 1. Catalog
        _step("Criando catalog...")
        try:
            _env_sql(sess, warehouse_id, f"CREATE CATALOG IF NOT EXISTS {catalog}")
        except RuntimeError:
            pass  # catalog may already exist

        # 2. Schema
        _step("Criando schema...")
        _env_sql(sess, warehouse_id, f"CREATE SCHEMA IF NOT EXISTS {catalog}.{schema}")

        # 3. tools_config
        _step("Criando tabela tools_config...")
        _env_sql(sess, warehouse_id, f"""
        CREATE TABLE IF NOT EXISTS {catalog}.{schema}.tools_config (
          tool_name   STRING NOT NULL,
          kind        STRING,
          ref         STRING,
          description STRING,
          status      STRING,
          owner       STRING,
          created_at  TIMESTAMP,
          updated_at  TIMESTAMP
        )
        USING DELTA
        """)

        # 4. agents_config
        _step("Criando tabela agents_config...")
        _env_sql(sess, warehouse_id, f"""
        CREATE TABLE IF NOT EXISTS {catalog}.{schema}.agents_config (
          agent_id              STRING NOT NULL,
          name                  STRING,
          description           STRING,
          model                 STRING,
          tools_enabled         ARRAY<STRING>,
          prompt_name           STRING,
          prompt_alias          STRING,
          status                STRING,
          environment           STRING,
          created_by            STRING,
          created_at            TIMESTAMP,
          updated_at            TIMESTAMP,
          serving_endpoint_name STRING,
          deploy_status         STRING
        )
        USING DELTA
        """)

        # 5. Volume for libs
        _step("Criando volume para libs...")
        _env_sql(sess, warehouse_id, f"CREATE VOLUME IF NOT EXISTS {catalog}.{schema}.libs")

        # 6–7. WHL — local_dev: skip build, always upload; prod: build + upload
        whl_name        = f"corp_agent_framework-{_CORP_WHL_VERSION}-py3-none-any.whl"
        whl_volume_path = f"/Volumes/{catalog}/{schema}/libs/{whl_name}"

        if settings.local_dev:
            _step("Build WHL (pulando — usando WHL local)...")
            whl_local = _get_local_whl()
        else:
            _step("Buildando WHL do corp_agent_framework...")
            whl_local = _build_whl()
        whl_name = _os.path.basename(whl_local)

        _step(f"Enviando {whl_name} para o Volume...")
        with open(whl_local, "rb") as fh:
                sess.file_upload(whl_volume_path, fh, overwrite=True)

        # 8. Check endpoint state (informational only — always proceed to deploy/update)
        _step("Verificando endpoint existente...")

        # 9. Upload deploy notebook
        _step("Carregando notebook de deploy...")
        script        = _generate_deploy_script(
            catalog, schema, warehouse_id, env,
            workspace_url=env_cfg.get("workspace_url", ""),
            token=env_cfg.get("token", ""),
        )
        notebook_dir  = "/Shared/_corp_client_agent"
        notebook_path = f"{notebook_dir}/deploy_framework_{catalog}_{env}"
        sess.workspace_mkdirs(notebook_dir)
        sess.workspace_import_notebook(notebook_path, _b64.b64encode(script.encode()).decode())

        # 10. Submit one-shot job (prefer UC-enabled running cluster, fall back to new)
        _step("Submetendo job de deploy...")
        task_spec: dict = {"notebook_task": {"notebook_path": notebook_path}}
        cluster_id = sess.get_running_uc_cluster_id()
        if cluster_id:
            task_spec["existing_cluster_id"] = cluster_id
        else:
            task_spec["new_cluster"] = {
                "spark_version":      "16.0.x-scala2.12",
                "node_type_id":       "n2-standard-4",
                "num_workers":        0,
                "spark_conf":         {"spark.master": "local[*, 4]"},
                "data_security_mode": "SINGLE_USER",
            }

        run_id = sess.submit_run(f"deploy-{endpoint_name}", task_spec)

        # 11. Poll for job completion (max 30 min)
        _step("Aguardando conclusão do job de deploy...")
        deadline = _time.time() + 1800
        while run_id and _time.time() < deadline:
            _time.sleep(30)
            try:
                run  = sess.get_run(run_id)
                life = str(run.get("state", {}).get("life_cycle_state", "")).upper()
                if "TERMINATED" in life:
                    break
                if life not in ("RUNNING", "PENDING", "WAITING"):
                    break
            except Exception:
                break

    except Exception as exc:
        step_label = _DEPLOY_STEP.get(env, "inicialização")
        _DEPLOY_ERROR[env] = f"Falha em '{step_label}': {exc}"
        _log.exception("Deploy failed for env=%s at step=%r", env, step_label)
    finally:
        _DEPLOY_STEP.pop(env, None)
        _DEPLOY_STEP_IDX.pop(env, None)
        with _DEPLOYING_LOCK:
            _DEPLOYING_ENVS.discard(env)


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


# ── Routes: workspace config ───────────────────────────────────

@router.get("/workspaces")
def get_workspace_envs():
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
    _ensure_table()
    for cfg in body.envs:
        if cfg.env not in ENVS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Ambiente inválido: '{cfg.env}'. Aceitos: {', '.join(ENVS)}",
            )
        # Apply defaults for catalog and schema_name when not explicitly set
        effective_catalog     = cfg.catalog     or settings.framework_catalog
        effective_schema_name = cfg.schema_name or settings.framework_schema

        _execute_sql(f"""
        MERGE INTO {_table()} AS target
        USING (
          SELECT
            '{_esc(cfg.env)}'                             AS env,
            '{_esc(cfg.workspace_url or "")}'             AS workspace_url,
            '{_esc(effective_catalog)}'                   AS catalog,
            '{_esc(effective_schema_name)}'               AS schema_name,
            '{_esc(cfg.warehouse_id or "")}'              AS warehouse_id,
            '{_esc(cfg.token or "")}'                     AS token,
            '{_esc(cfg.notes or "")}'                     AS notes
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


# ── Routes: framework endpoints ────────────────────────────────

def _fetch_endpoint_for_env(env: str) -> dict:
    endpoint_name = ENDPOINT_NAME_TPL.format(env=env)
    base = {
        "env": env, "endpoint_name": endpoint_name,
        "deploying": env in _DEPLOYING_ENVS,
        "deploy_step": _DEPLOY_STEP.get(env, ""),
        "deploy_step_index": _DEPLOY_STEP_IDX.get(env, 0),
        "deploy_error": _DEPLOY_ERROR.get(env, ""),
    }
    try:
        env_cfg = _get_env_config_from_db(env)
    except Exception as exc:
        return {**base, "endpoint_url": "", "state": "ERROR", "error": str(exc)}

    if not env_cfg or not env_cfg.get("workspace_url") or not env_cfg.get("token"):
        return {**base, "endpoint_url": "", "state": "NOT_CONFIGURED"}

    try:
        sess     = _get_env_session(env_cfg)
        ep_state = _check_endpoint_state(sess, endpoint_name)

        is_deploying = env in _DEPLOYING_ENVS
        if not is_deploying:
            is_deploying = _check_active_deploy_job(sess, endpoint_name)
            if is_deploying:
                with _DEPLOYING_LOCK:
                    _DEPLOYING_ENVS.add(env)
                if env not in _DEPLOY_STEP_IDX:
                    _DEPLOY_STEP[env]     = "Aguardando conclusão do job de deploy..."
                    _DEPLOY_STEP_IDX[env] = _DEPLOY_TOTAL_STEPS - 1

        workspace_url = env_cfg["workspace_url"].rstrip("/")
        endpoint_url  = (
            f"{workspace_url}/serving-endpoints/{endpoint_name}/invocations"
            if ep_state != "NOT_FOUND"
            else ""
        )
        return {
            **base,
            "endpoint_url": endpoint_url, "state": ep_state,
            "deploying": is_deploying,
            "deploy_step": _DEPLOY_STEP.get(env, ""),
            "deploy_step_index": _DEPLOY_STEP_IDX.get(env, 0),
            "deploy_error": _DEPLOY_ERROR.get(env, ""),
        }
    except Exception as exc:
        return {**base, "endpoint_url": "", "state": "ERROR", "error": str(exc)}


@router.get("/framework-endpoints")
def get_framework_endpoints():
    # Legacy env-based endpoints
    order = {e: i for i, e in enumerate(ENVS)}
    with _ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(_fetch_endpoint_for_env, env): env for env in ENVS}
        results = [f.result() for f in _as_completed(futures)]
    results.sort(key=lambda r: order.get(r["env"], 99))

    # Domain-based endpoints
    domain_results: list[dict] = []
    try:
        domain_rows = _get_all_domain_envs()
    except Exception:
        domain_rows = []

    def _fetch_domain_ep(domain: str, env: str, cfg: dict) -> dict:
        key           = f"{domain}::{env}"
        endpoint_name = DOMAIN_ENDPOINT_NAME_TPL.format(domain=domain, env=env)
        workspace_url = (cfg.get("workspace_url") or "").rstrip("/")
        run_id        = _DOMAIN_RUN_ID.get(key)
        base = {
            "domain": domain, "env": env, "endpoint_name": endpoint_name,
            "deploying":         key in _DOMAIN_DEPLOYING,
            "deploy_step":       _DOMAIN_STEP.get(key, ""),
            "deploy_step_index": _DOMAIN_STEP_IDX.get(key, 0),
            "deploy_error":      _DOMAIN_ERROR.get(key, ""),
            "deploy_run_url":    f"{workspace_url}/jobs/runs/{run_id}" if run_id and workspace_url else "",
        }
        if not cfg.get("workspace_url") or not cfg.get("token"):
            return {**base, "endpoint_url": "", "state": "NOT_CONFIGURED"}
        try:
            sess = _get_env_session(cfg)

            # Detect active job not tracked in-memory (e.g. after server restart)
            if key not in _DOMAIN_DEPLOYING:
                active_run_id = sess.get_active_deploy_run_id(endpoint_name)
                if active_run_id:
                    with _DOMAIN_LOCK:
                        _DOMAIN_DEPLOYING.add(key)
                    _DOMAIN_RUN_ID[key]   = active_run_id
                    _DOMAIN_STEP[key]     = "Aguardando conclusão do job de deploy..."
                    _DOMAIN_STEP_IDX[key] = DOMAIN_DEPLOY_STEPS - 1
                    base = {**base,
                            "deploying":         True,
                            "deploy_step":       _DOMAIN_STEP[key],
                            "deploy_step_index": _DOMAIN_STEP_IDX[key],
                            "deploy_run_url":    f"{workspace_url}/jobs/runs/{active_run_id}"}

            ep_state     = _check_endpoint_state(sess, endpoint_name)
            endpoint_url = (
                f"{workspace_url}/serving-endpoints/{endpoint_name}/invocations"
                if ep_state != "NOT_FOUND" else ""
            )
            return {**base, "endpoint_url": endpoint_url, "state": ep_state}
        except Exception as exc:
            return {**base, "endpoint_url": "", "state": "ERROR", "error": str(exc)}

    # Group domain rows by (domain, env)
    seen_pairs: dict[tuple, dict] = {}
    for r in domain_rows:
        seen_pairs[(r["domain"], r["env"])] = r

    if seen_pairs:
        with _ThreadPoolExecutor(max_workers=min(9, len(seen_pairs))) as pool:
            d_futures = {
                pool.submit(_fetch_domain_ep, dom, env, cfg): (dom, env)
                for (dom, env), cfg in seen_pairs.items()
            }
            domain_results = [f.result() for f in _as_completed(d_futures)]

    return {"endpoints": results + domain_results}


@router.post("/framework-endpoints/{env}/deploy")
def deploy_framework_endpoint(env: str):
    """
    Full setup of corp_agent_framework in the env workspace.
    Validates that workspace_url, token, and warehouse_id are all set before starting.
    """
    if env not in ENVS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Ambiente inválido: '{env}'. Aceitos: {', '.join(ENVS)}",
        )

    try:
        env_cfg = _get_env_config_from_db(env)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Erro ao ler configuração do ambiente: {exc}",
        ) from exc

    if not env_cfg or not env_cfg.get("workspace_url") or not env_cfg.get("token"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Workspace do ambiente '{env}' não configurado. Preencha Workspace URL e Token antes de fazer o deploy.",
        )

    if not (env_cfg.get("warehouse_id") or "").strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Warehouse ID do ambiente '{env}' não configurado. Preencha o campo antes de fazer o deploy.",
        )

    with _DEPLOYING_LOCK:
        if env in _DEPLOYING_ENVS:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Deploy do ambiente '{env}' já está em andamento.",
            )
        _DEPLOYING_ENVS.add(env)

    _DEPLOY_ERROR.pop(env, None)
    _DEPLOY_STEP[env]     = "Iniciando..."
    _DEPLOY_STEP_IDX[env] = 0

    _threading.Thread(
        target=_deploy_framework_background,
        args=(env, dict(env_cfg)),
        daemon=True,
    ).start()

    return {"env": env, "deploying": True}


# ── Model approvals ───────────────────────────────────────────

def _models_table() -> str:
    return f"{settings.uc_prefix}.model_approvals"


def _ensure_models_table() -> None:
    _execute_sql(f"""
    CREATE TABLE IF NOT EXISTS {_models_table()} (
      model_name  STRING NOT NULL,
      status      STRING,
      notes       STRING,
      updated_at  TIMESTAMP
    )
    USING DELTA
    """)


class PatchModelStatusRequest(BaseModel):
    new_status: str   # approved | rejected | pending
    notes: str = ""


@router.get("/models")
def list_models():
    """
    Returns serving endpoints from the dev workspace merged with approval status.
    """
    _ensure_models_table()

    # Approval statuses from app workspace
    response = _execute_sql(
        f"SELECT model_name, status, notes, updated_at FROM {_models_table()}"
    )
    approvals = {r["model_name"]: r for r in _rows_to_dicts(response)}

    # Collect endpoints from the dev workspace only
    raw: list[dict] = []
    seen: set[str] = set()

    for env in ("dev",):
        try:
            env_cfg = _get_env_config_from_db(env)
            if not env_cfg or not env_cfg.get("workspace_url") or not env_cfg.get("token"):
                continue
            sess = _get_env_session(env_cfg)
            for ep in sess.list_endpoints():
                if not ep["name"] or ep["name"] in seen:
                    continue
                seen.add(ep["name"])
                raw.append({
                    "name":       ep["name"],
                    "env":        env,
                    "state":      str(ep["state"]),
                    "model_name": ep["model_name"],
                    "creator":    ep["creator"],
                })
        except Exception:
            continue

    models = []
    for ep in raw:
        ap = approvals.get(ep["name"], {})
        models.append({
            **ep,
            "approval_status": ap.get("status") or "pending",
            "notes": ap.get("notes") or "",
            "updated_at": ap.get("updated_at"),
        })

    return {"models": models}


# ── Domain envs — Lakebase (PostgreSQL) ────────────────────────

def _ensure_domain_table_pg() -> None:
    from app.api import lakebase
    lakebase.execute("""
        CREATE TABLE IF NOT EXISTS domain_envs (
            domain        TEXT        NOT NULL,
            env           TEXT        NOT NULL,
            workspace_url TEXT,
            token         TEXT,
            notes         TEXT,
            updated_at    TIMESTAMPTZ DEFAULT NOW(),
            PRIMARY KEY (domain, env)
        )
    """)


def _get_all_domain_envs() -> list[dict]:
    from app.api import lakebase
    _ensure_domain_table_pg()
    return lakebase.execute("""
        SELECT domain, env, workspace_url, token, notes, updated_at
        FROM domain_envs
        ORDER BY domain,
                 CASE env WHEN 'dev' THEN 1 WHEN 'staging' THEN 2 WHEN 'prod' THEN 3 ELSE 4 END
    """)


def _domain_to_schema(domain: str) -> str:
    """Convert domain name (may have hyphens) to a valid SQL identifier."""
    return domain.replace("-", "_")


def _auto_select_warehouse(sess: _EnvSession) -> str:
    """Return any available SQL warehouse ID in the workspace."""
    return sess.get_warehouse_id()


# ── Domain deploy background ────────────────────────────────────

def _deploy_domain_background(domain: str, env: str, env_cfg: dict) -> None:
    key      = f"{domain}::{env}"
    catalog  = "corp_agent_framework"
    schema   = _domain_to_schema(domain)

    _step_counter = [-1]

    def _step(msg: str) -> None:
        _step_counter[0] += 1
        _DOMAIN_STEP[key]     = msg
        _DOMAIN_STEP_IDX[key] = _step_counter[0]

    try:
        sess          = _get_env_session(env_cfg)
        endpoint_name = DOMAIN_ENDPOINT_NAME_TPL.format(domain=domain, env=env)

        # 1. Catalog (UC REST API — no warehouse needed)
        _step("Criando catalog...")
        sess.uc_ensure_catalog(catalog)

        # 2. Schema
        _step("Criando schema...")
        sess.uc_ensure_schema(catalog, schema)

        # 3. Volume
        _step("Criando volume para libs...")
        sess.uc_ensure_volume(catalog, schema, "libs")

        # 4–5. WHL — local_dev: skip build, always upload; prod: build + upload
        whl_name        = f"corp_agent_framework-{_CORP_WHL_VERSION}-py3-none-any.whl"
        whl_volume_path = f"/Volumes/{catalog}/{schema}/libs/{whl_name}"

        if settings.local_dev:
            _step("Build WHL (pulando — usando WHL local)...")
            whl_local = _get_local_whl()
        else:
            _step("Buildando WHL do corp_agent_framework...")
            whl_local = _build_whl()
        _step(f"Enviando {_os.path.basename(whl_local)} para o Volume...")
        with open(whl_local, "rb") as fh:
            sess.file_upload(whl_volume_path, fh, overwrite=True)

        # 6. Check existing endpoint
        _step("Verificando endpoint existente...")

        # 7. Upload notebook (Lakebase-aware deploy script)
        _step("Carregando notebook de deploy...")
        script = _generate_deploy_script_lakebase(
            catalog, schema, env, domain,
            workspace_url=env_cfg.get("workspace_url", ""),
            token=env_cfg.get("token", ""),
            lakebase_host=settings.lakebase_host,
            lakebase_database=settings.lakebase_database,
            lakebase_username=settings.lakebase_username,
        )
        notebook_dir  = "/Shared/_corp_client_agent"
        notebook_path = f"{notebook_dir}/deploy_framework_{domain}_{env}"
        sess.workspace_mkdirs(notebook_dir)
        sess.workspace_import_notebook(notebook_path, _b64.b64encode(script.encode()).decode())

        # 8. Submit job
        _step("Submetendo job de deploy...")
        task_spec: dict = {"notebook_task": {"notebook_path": notebook_path}}
        cluster_id = sess.get_running_uc_cluster_id()
        if cluster_id:
            task_spec["existing_cluster_id"] = cluster_id
        else:
            task_spec["new_cluster"] = {
                "spark_version": "16.0.x-scala2.12", "node_type_id": sess.get_node_type(),
                "num_workers": 0, "spark_conf": {"spark.master": "local[*, 4]"},
                "data_security_mode": "SINGLE_USER",
            }

        run_id = sess.submit_run(f"deploy-{endpoint_name}", task_spec)
        _DOMAIN_RUN_ID[key] = run_id

        # 9. Poll
        _step("Aguardando conclusão do job de deploy...")
        deadline = _time.time() + 1800
        while run_id and _time.time() < deadline:
            _time.sleep(30)
            try:
                run  = sess.get_run(run_id)
                life = str(run.get("state", {}).get("life_cycle_state", "")).upper()
                if "TERMINATED" in life or life not in ("RUNNING", "PENDING", "WAITING"):
                    break
            except Exception:
                break

    except Exception as exc:
        step_label = _DOMAIN_STEP.get(key, "inicialização")
        _DOMAIN_ERROR[key] = f"Falha em '{step_label}': {exc}"
        _log.exception("Domain deploy failed for %s at step=%r", key, step_label)
    finally:
        _DOMAIN_STEP.pop(key, None)
        _DOMAIN_STEP_IDX.pop(key, None)
        _DOMAIN_RUN_ID.pop(key, None)
        with _DOMAIN_LOCK:
            _DOMAIN_DEPLOYING.discard(key)


# ── Pydantic models (domains) ──────────────────────────────────

class DomainEnvConfig(BaseModel):
    env: str
    workspace_url: str = ""
    token: str = ""
    notes: str = ""


class SaveDomainEnvRequest(BaseModel):
    env: str
    workspace_url: str = ""
    token: str = ""
    notes: str = ""


# ── Routes: domains ─────────────────────────────────────────────

@router.get("/domains")
def list_domains():
    if not settings.lakebase_host or not settings.lakebase_username:
        raise HTTPException(
            status_code=503,
            detail="Lakebase não configurado. Defina LAKEBASE_HOST e LAKEBASE_USERNAME no .env.",
        )
    try:
        rows = _get_all_domain_envs()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Erro ao conectar ao Lakebase: {exc}") from exc
    # Group by domain
    domain_map: dict[str, dict] = {}
    for r in rows:
        d = r["domain"]
        if d not in domain_map:
            domain_map[d] = {"domain": d, "envs": []}
        domain_map[d]["envs"].append({
            "env":           r["env"],
            "workspace_url": r.get("workspace_url") or "",
            "token":         r.get("token") or "",
            "notes":         r.get("notes") or "",
            "updated_at":    r.get("updated_at"),
        })
    # Ensure all 3 envs present for each domain
    for d_data in domain_map.values():
        existing_envs = {e["env"] for e in d_data["envs"]}
        for env in ENVS:
            if env not in existing_envs:
                d_data["envs"].append({"env": env, "workspace_url": "", "token": "", "notes": "", "updated_at": None})
        d_data["envs"].sort(key=lambda e: ("dev", "staging", "prod").index(e["env"]) if e["env"] in ("dev", "staging", "prod") else 99)
    return {"domains": list(domain_map.values())}


@router.put("/domains/{domain}/envs/{env}")
def save_domain_env(domain: str, env: str, body: SaveDomainEnvRequest):
    if env not in ENVS:
        raise HTTPException(status_code=422, detail=f"Ambiente inválido: '{env}'")
    if not settings.lakebase_host or not settings.lakebase_username:
        raise HTTPException(status_code=503, detail="Lakebase não configurado.")
    _ensure_domain_table_pg()
    from app.api import lakebase
    lakebase.execute("""
        INSERT INTO domain_envs (domain, env, workspace_url, token, notes, updated_at)
        VALUES (%s, %s, %s, %s, %s, NOW())
        ON CONFLICT (domain, env) DO UPDATE SET
            workspace_url = EXCLUDED.workspace_url,
            token         = EXCLUDED.token,
            notes         = EXCLUDED.notes,
            updated_at    = NOW()
    """, (domain, env, body.workspace_url or "", body.token or "", body.notes or ""))
    return {"domain": domain, "env": env, "saved": True}


@router.delete("/domains/{domain}")
def delete_domain(domain: str):
    from app.api import lakebase
    lakebase.execute("DELETE FROM domain_envs WHERE domain = %s", (domain,))
    return {"domain": domain, "deleted": True}


@router.post("/domains/{domain}/envs/{env}/deploy")
def deploy_domain_env(domain: str, env: str):
    if env not in ENVS:
        raise HTTPException(status_code=400, detail=f"Ambiente inválido: '{env}'")

    from app.api import lakebase
    env_cfg = lakebase.execute_one(
        "SELECT workspace_url, token FROM domain_envs WHERE domain = %s AND env = %s",
        (domain, env),
    )

    if not env_cfg or not env_cfg.get("workspace_url") or not env_cfg.get("token"):
        raise HTTPException(
            status_code=400,
            detail=f"Workspace URL e Token são obrigatórios para fazer o deploy de '{domain}/{env}'.",
        )

    key           = f"{domain}::{env}"
    endpoint_name = DOMAIN_ENDPOINT_NAME_TPL.format(domain=domain, env=env)

    with _DOMAIN_LOCK:
        if key in _DOMAIN_DEPLOYING:
            raise HTTPException(status_code=409, detail=f"Deploy de '{domain}/{env}' já em andamento.")

    # Also check for an active job in the workspace (survives server restarts)
    try:
        sess = _get_env_session(env_cfg)
        active_run_id = sess.get_active_deploy_run_id(endpoint_name)
        if active_run_id:
            # Populate in-memory state so polling shows it as deploying with link
            with _DOMAIN_LOCK:
                _DOMAIN_DEPLOYING.add(key)
            _DOMAIN_RUN_ID[key]  = active_run_id
            _DOMAIN_STEP[key]    = "Aguardando conclusão do job de deploy..."
            _DOMAIN_STEP_IDX[key] = len(DOMAIN_DEPLOY_STEPS) - 1
            raise HTTPException(status_code=409, detail=f"Já existe um job de deploy ativo para '{endpoint_name}'.")
    except HTTPException:
        raise
    except Exception:
        pass  # connectivity issue — let the deploy proceed and fail naturally

    with _DOMAIN_LOCK:
        _DOMAIN_DEPLOYING.add(key)

    _DOMAIN_ERROR.pop(key, None)
    _DOMAIN_STEP[key]     = "Iniciando..."
    _DOMAIN_STEP_IDX[key] = 0

    _threading.Thread(
        target=_deploy_domain_background,
        args=(domain, env, dict(env_cfg)),
        daemon=True,
    ).start()

    return {"domain": domain, "env": env, "deploying": True}


def _ensure_domain_model_approvals_table() -> None:
    from app.api import lakebase
    lakebase.execute("""
        CREATE TABLE IF NOT EXISTS domain_model_approvals (
            domain      TEXT NOT NULL,
            model_name  TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'pending',
            notes       TEXT,
            updated_at  TIMESTAMPTZ DEFAULT NOW(),
            PRIMARY KEY (domain, model_name)
        )
    """)


@router.get("/domains/{domain}/models")
def list_domain_models(domain: str):
    """
    Lists serving endpoint models available for the domain's dev workspace,
    merged with per-domain approval status from Lakebase.
    """
    from app.api import lakebase
    _ensure_domain_model_approvals_table()

    # Per-domain approval statuses
    approval_rows = lakebase.execute(
        "SELECT model_name, status, notes FROM domain_model_approvals WHERE domain = %s",
        (domain,),
    )
    domain_approvals = {r["model_name"]: r for r in approval_rows}

    # Domain dev workspace config
    cfg_rows = lakebase.execute(
        "SELECT workspace_url, token FROM domain_envs WHERE domain = %s AND env = 'dev'",
        (domain,),
    )
    if not cfg_rows or not cfg_rows[0].get("workspace_url") or not cfg_rows[0].get("token"):
        return {"models": []}

    sess = _get_env_session(cfg_rows[0])
    raw = sess.list_endpoints()

    def _is_system_ai(ep: dict) -> bool:
        name       = ep.get("name", "")
        model_name = ep.get("model_name", "")
        return name.startswith("databricks-") or model_name.startswith("system.ai.")

    models = []
    for ep in raw:
        if not ep.get("name") or not _is_system_ai(ep):
            continue
        ap = domain_approvals.get(ep["name"], {})
        models.append({
            "name":            ep["name"],
            "env":             "dev",
            "state":           str(ep.get("state", "")),
            "model_name":      ep.get("model_name", ""),
            "creator":         ep.get("creator", ""),
            "approval_status": ap.get("status") or "pending",
            "notes":           ap.get("notes") or "",
        })
    return {"models": models}


@router.patch("/domains/{domain}/models/{model_name:path}/status")
def update_domain_model_status(domain: str, model_name: str, body: PatchModelStatusRequest):
    """Set per-domain approval status for a model."""
    from app.api import lakebase
    allowed = {"approved", "rejected", "pending"}
    if body.new_status not in allowed:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Status inválido. Valores aceitos: {', '.join(sorted(allowed))}",
        )
    _ensure_domain_model_approvals_table()
    lakebase.execute("""
        INSERT INTO domain_model_approvals (domain, model_name, status, notes, updated_at)
        VALUES (%s, %s, %s, %s, NOW())
        ON CONFLICT (domain, model_name) DO UPDATE SET
            status     = EXCLUDED.status,
            notes      = EXCLUDED.notes,
            updated_at = NOW()
    """, (domain, model_name, body.new_status, body.notes or ""))
    return {"domain": domain, "model_name": model_name, "status": body.new_status}


@router.patch("/models/{model_name:path}/status")
def update_model_status(model_name: str, body: PatchModelStatusRequest):
    """Approve or reject a serving endpoint for use in agent creation."""
    allowed = {"approved", "rejected", "pending"}
    if body.new_status not in allowed:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Status inválido. Valores aceitos: {', '.join(sorted(allowed))}",
        )
    _ensure_models_table()
    _execute_sql(f"""
    MERGE INTO {_models_table()} AS target
    USING (
      SELECT
        '{_esc(model_name)}'      AS model_name,
        '{_esc(body.new_status)}' AS status,
        '{_esc(body.notes)}'      AS notes
    ) AS source
    ON target.model_name = source.model_name
    WHEN MATCHED THEN UPDATE SET
      target.status     = source.status,
      target.notes      = source.notes,
      target.updated_at = current_timestamp()
    WHEN NOT MATCHED THEN INSERT (model_name, status, notes, updated_at)
    VALUES (source.model_name, source.status, source.notes, current_timestamp())
    """)
    return {"model_name": model_name, "status": body.new_status}
