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

_log = _logging.getLogger(__name__)

from databricks.sdk import WorkspaceClient as _WorkspaceClient
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from app.api.db import _execute_sql, _rows_to_dicts, _esc
from app.config import settings

router = APIRouter(prefix="/api/settings")

ENVS = ("dev", "staging", "prod")

ENDPOINT_NAME_TPL = "corp-config-driven-agent-{env}"
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


def _get_env_workspace_client(env_cfg: dict) -> _WorkspaceClient:
    return _WorkspaceClient(
        host=env_cfg["workspace_url"],
        token=env_cfg["token"],
    )


def _check_endpoint_state(w: _WorkspaceClient, endpoint_name: str) -> str:
    try:
        ep = w.serving_endpoints.get(endpoint_name)
        if ep.state and ep.state.ready:
            return "READY" if "READY" in str(ep.state.ready).upper() else "NOT_READY"
        return "NOT_READY"
    except Exception:
        return "NOT_FOUND"


def _check_active_deploy_job(w: _WorkspaceClient, endpoint_name: str) -> bool:
    try:
        for run in w.jobs.list_runs(active_only=True):
            if getattr(run, "run_name", None) == f"deploy-{endpoint_name}":
                return True
    except Exception:
        pass
    return False


# ── SQL execution in env workspace ────────────────────────────

def _env_sql(w: _WorkspaceClient, warehouse_id: str, statement: str) -> None:
    """Execute SQL in the env workspace warehouse (not the app workspace)."""
    try:
        response = w.statement_execution.execute_statement(
            warehouse_id=warehouse_id,
            statement=statement.strip(),
            wait_timeout="50s",
        )
    except Exception as exc:
        raise RuntimeError(f"Erro ao executar SQL: {exc}") from exc

    deadline = _time.time() + 300
    while True:
        state = str(response.status.state) if response.status else "UNKNOWN"
        state_up = state.upper()
        if "SUCCEEDED" in state_up:
            return
        if any(s in state_up for s in ("FAILED", "CANCELED", "CLOSED")):
            error_msg = (
                response.status.error.message
                if (response.status and response.status.error)
                else state
            )
            raise RuntimeError(f"SQL falhou: {error_msg}")
        if _time.time() > deadline:
            raise RuntimeError(f"SQL timeout (state={state})")
        _time.sleep(2)
        try:
            response = w.statement_execution.get_statement(response.statement_id)
        except Exception as exc:
            raise RuntimeError(f"Erro ao verificar statement: {exc}") from exc


# ── WHL build & upload ─────────────────────────────────────────

def _build_whl() -> str:
    """Build corp_agent_framework WHL from local source. Returns local .whl path."""
    framework_dir = _os.path.abspath(
        _os.path.join(_os.path.dirname(__file__), "../../../../corp-agent-framework")
    )
    if not _os.path.isdir(framework_dir):
        raise RuntimeError(
            f"corp-agent-framework não encontrado em {framework_dir}. "
            "Verifique a estrutura do repositório."
        )
    tmpdir = _tempfile.mkdtemp(prefix="corp_whl_")
    result = _subprocess.run(
        [_sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "-q", "-w", tmpdir, framework_dir],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Falha ao buildar WHL: {result.stderr}")
    whl_files = [f for f in _os.listdir(tmpdir) if f.endswith(".whl")]
    if not whl_files:
        raise RuntimeError("pip wheel não gerou nenhum arquivo .whl")
    return _os.path.join(tmpdir, whl_files[0])


# ── Deploy script generator ────────────────────────────────────

def _generate_deploy_script(
    catalog: str, schema: str, warehouse_id: str, environment: str
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

        w             = _get_env_workspace_client(env_cfg)
        endpoint_name = ENDPOINT_NAME_TPL.format(env=env)

        # 1. Catalog
        _step("Criando catalog...")
        try:
            _env_sql(w, warehouse_id, f"CREATE CATALOG IF NOT EXISTS {catalog}")
        except RuntimeError:
            # May fail if catalog already exists and token lacks CREATE CATALOG privilege.
            # Verify the catalog is accessible before re-raising.
            try:
                rows_resp = w.statement_execution.execute_statement(
                    warehouse_id=warehouse_id,
                    statement=f"SHOW CATALOGS LIKE '{catalog}'",
                    wait_timeout="30s",
                )
                cols = [c.name for c in (rows_resp.manifest.schema.columns if rows_resp.manifest and rows_resp.manifest.schema else [])]
                data = (rows_resp.result and rows_resp.result.data_array) or []
                found = any(catalog in (row[cols.index("catalog")] if "catalog" in cols else row[0]) for row in data)
            except Exception:
                found = False
            if not found:
                raise

        # 2. Schema
        _step("Criando schema...")
        _env_sql(w, warehouse_id, f"CREATE SCHEMA IF NOT EXISTS {catalog}.{schema}")

        # 3. tools_config
        _step("Criando tabela tools_config...")
        _env_sql(w, warehouse_id, f"""
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
        _env_sql(w, warehouse_id, f"""
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
        _env_sql(w, warehouse_id, f"CREATE VOLUME IF NOT EXISTS {catalog}.{schema}.libs")

        # 6. Build WHL (skipped in local_dev if WHL already in Volume)
        whl_name        = f"corp_agent_framework-{_CORP_WHL_VERSION}-py3-none-any.whl"
        whl_volume_path = f"/Volumes/{catalog}/{schema}/libs/{whl_name}"
        whl_exists      = False
        if settings.local_dev:
            try:
                w.files.get_metadata(file_path=whl_volume_path)
                whl_exists = True
            except Exception:
                pass

        if whl_exists:
            _step("Build WHL (já existe no Volume, pulando)...")
            _step(f"Upload WHL (já existe no Volume, pulando)...")
        else:
            _step("Buildando WHL do corp_agent_framework...")
            whl_local = _build_whl()
            whl_name  = _os.path.basename(whl_local)

            # 7. Upload WHL to Volume
            _step(f"Enviando {whl_name} para o Volume...")
            with open(whl_local, "rb") as fh:
                w.files.upload(
                    file_path=whl_volume_path,
                    contents=fh,
                    overwrite=True,
                )

        # 8. Check if endpoint already READY — no need to re-deploy
        _step("Verificando endpoint existente...")
        try:
            ep = w.serving_endpoints.get(name=endpoint_name)
            if ep.state and "READY" in str(ep.state.ready).upper():
                return
        except Exception:
            pass  # endpoint not found → proceed to deploy

        # 9. Upload deploy notebook
        _step("Carregando notebook de deploy...")
        script        = _generate_deploy_script(catalog, schema, warehouse_id, env)
        notebook_dir  = "/Shared/_corp_client_agent"
        notebook_path = f"{notebook_dir}/deploy_framework_{catalog}_{env}"
        w.api_client.do(
            "POST", "/api/2.0/workspace/mkdirs",
            body={"path": notebook_dir},
        )
        w.api_client.do(
            "POST", "/api/2.0/workspace/import",
            body={
                "path": notebook_path,
                "format": "SOURCE",
                "language": "PYTHON",
                "content": _b64.b64encode(script.encode()).decode(),
                "overwrite": True,
            },
        )

        # 10. Submit one-shot job (prefer UC-enabled running cluster, fall back to new)
        _step("Submetendo job de deploy...")
        _UC_MODES = {"SINGLE_USER", "USER_ISOLATION"}
        task_spec: dict = {"notebook_task": {"notebook_path": notebook_path}}
        try:
            all_clusters = list(w.clusters.list())
            running = [
                c for c in all_clusters
                if str(getattr(c, "state", "")).upper() in ("RUNNING", "RESIZING")
                and str(getattr(c, "data_security_mode", "")).upper() in _UC_MODES
            ]
            if running:
                task_spec["existing_cluster_id"] = running[0].cluster_id
            else:
                raise ValueError("no UC-enabled running cluster")
        except Exception:
            try:
                node_type_id = w.clusters.select_node_type(local_disk=True)
            except Exception:
                node_type_id = "n2-standard-4"
            task_spec["new_cluster"] = {
                "spark_version":      "16.0.x-scala2.12",
                "node_type_id":       node_type_id,
                "num_workers":        0,
                "spark_conf":         {"spark.master": "local[*, 4]"},
                "data_security_mode": "SINGLE_USER",
            }

        run_resp = w.api_client.do(
            "POST", "/api/2.0/jobs/runs/submit",
            body={
                "run_name": f"deploy-{endpoint_name}",
                "tasks": [{"task_key": "deploy", **task_spec}],
            },
        )
        run_id = run_resp.get("run_id")

        # 11. Poll for job completion (max 30 min)
        _step("Aguardando conclusão do job de deploy...")
        deadline = _time.time() + 1800
        while run_id and _time.time() < deadline:
            _time.sleep(30)
            try:
                run  = w.api_client.do("GET", f"/api/2.0/jobs/runs/get?run_id={run_id}")
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


# ── Routes: framework endpoints ────────────────────────────────

@router.get("/framework-endpoints")
def get_framework_endpoints():
    results = []
    for env in ENVS:
        endpoint_name = ENDPOINT_NAME_TPL.format(env=env)
        try:
            env_cfg = _get_env_config_from_db(env)
        except Exception as exc:
            results.append({
                "env": env, "endpoint_name": endpoint_name,
                "endpoint_url": "", "state": "ERROR",
                "deploying": env in _DEPLOYING_ENVS,
                "deploy_step": _DEPLOY_STEP.get(env, ""),
                "deploy_step_index": _DEPLOY_STEP_IDX.get(env, 0),
                "deploy_error": _DEPLOY_ERROR.get(env, ""),
                "error": str(exc),
            })
            continue

        if not env_cfg or not env_cfg.get("workspace_url") or not env_cfg.get("token"):
            results.append({
                "env": env, "endpoint_name": endpoint_name,
                "endpoint_url": "", "state": "NOT_CONFIGURED",
                "deploying": env in _DEPLOYING_ENVS,
                "deploy_step": _DEPLOY_STEP.get(env, ""),
                "deploy_step_index": _DEPLOY_STEP_IDX.get(env, 0),
                "deploy_error": _DEPLOY_ERROR.get(env, ""),
            })
            continue

        try:
            w          = _get_env_workspace_client(env_cfg)
            ep_state   = _check_endpoint_state(w, endpoint_name)

            is_deploying = env in _DEPLOYING_ENVS
            if not is_deploying:
                is_deploying = _check_active_deploy_job(w, endpoint_name)
                if is_deploying:
                    with _DEPLOYING_LOCK:
                        _DEPLOYING_ENVS.add(env)
                    # Job detected externally (e.g. after server restart) — show last step
                    if env not in _DEPLOY_STEP_IDX:
                        _DEPLOY_STEP[env]     = "Aguardando conclusão do job de deploy..."
                        _DEPLOY_STEP_IDX[env] = _DEPLOY_TOTAL_STEPS - 1

            workspace_url = env_cfg["workspace_url"].rstrip("/")
            endpoint_url  = (
                f"{workspace_url}/serving-endpoints/{endpoint_name}/invocations"
                if ep_state != "NOT_FOUND"
                else ""
            )
            results.append({
                "env": env, "endpoint_name": endpoint_name,
                "endpoint_url": endpoint_url, "state": ep_state,
                "deploying": is_deploying,
                "deploy_step": _DEPLOY_STEP.get(env, ""),
                "deploy_step_index": _DEPLOY_STEP_IDX.get(env, 0),
                "deploy_error": _DEPLOY_ERROR.get(env, ""),
            })
        except Exception as exc:
            results.append({
                "env": env, "endpoint_name": endpoint_name,
                "endpoint_url": "", "state": "ERROR",
                "deploying": env in _DEPLOYING_ENVS,
                "deploy_step": _DEPLOY_STEP.get(env, ""),
                "deploy_step_index": _DEPLOY_STEP_IDX.get(env, 0),
                "deploy_error": _DEPLOY_ERROR.get(env, ""),
                "error": str(exc),
            })

    return {"endpoints": results}


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
