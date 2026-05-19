"""
Agents registry API.

agents_config and eval_datasets live in per-domain PostgreSQL schemas
("{domain}".agents_config, "{domain}".eval_datasets).
The `environment` column (dev / staging / prod) is a lifecycle flag — promotion is an
UPDATE, not a record copy. Workspace API calls (MLflow, serving endpoints) still go to
each env's workspace.

Column mapping (framework schema uses 'name', not 'agent_name'):
  DB column  ↔  API/frontend field
  name       ↔  agent_name
"""

import json
import sys
import uuid as _uuid

from databricks.sdk.service.ml import ExperimentTag
import time as _time

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.api import lakebase
from app.api.tools import _env_client, _get_env_config, _sql, _workspace_client_for_env, _promote_genie_space
from app.api.settings import DOMAIN_ENDPOINT_NAME_TPL
from app.config import settings

router = APIRouter(prefix="/api")

ENVS = ("dev", "staging", "prod")

VALID_STATUSES = {"active", "disabled"}

_ENV_VIRTUAL_STATUS = {
    "dev":     "draft",
    "staging": "evaluating",
    "prod":    "approved",
}

_ENV_NEXT = {"dev": "staging", "staging": "prod"}

_FRAMEWORK_ENDPOINT = "corp-config-driven-agent-dev"

_ENV_FRAMEWORK_ENDPOINT = {
    "dev":     "corp-config-driven-agent-dev",
    "staging": "corp-config-driven-agent-staging",
    "prod":    "corp-config-driven-agent",
}


# ── Internal helpers ──────────────────────────────────────────

def _find_agent_domain(agent_id: str) -> str | None:
    """Search all domain schemas for agent_id; return its domain or None."""
    for domain in lakebase.list_domain_schemas():
        row = lakebase.execute_one(
            f'SELECT agent_id FROM "{domain}".agents_config WHERE agent_id = %s',
            (agent_id,),
        )
        if row:
            return domain
    return None


def _find_agent(agent_id: str, domain: str | None = None) -> dict | None:
    """Return the agent row from the domain schema, or None if not found."""
    d = domain or _find_agent_domain(agent_id)
    if d is None:
        return None
    row = lakebase.execute_one(
        f'SELECT * FROM "{d}".agents_config WHERE agent_id = %s', (agent_id,)
    )
    if row is not None:
        row["domain"] = d
    return row


def _row_to_api(row: dict, domain: str | None = None) -> dict:
    """Normalize a Lakebase row for the API: map name→agent_name, compute virtual status."""
    row = dict(row)
    row["agent_name"] = row.pop("name", "") or ""
    # tools_enabled is TEXT[] → already a Python list from psycopg3
    if row.get("tools_enabled") is None:
        row["tools_enabled"] = []
    # Inject domain if not already in row
    if domain and "domain" not in row:
        row["domain"] = domain
    # Map environment to virtual status; keep 'disabled' as-is
    if row.get("status") != "disabled":
        env = row.get("environment", "dev")
        if env == "dev" and row.get("approval_requested") in (True,):
            row["status"] = "pending_approval"
        else:
            row["status"] = _ENV_VIRTUAL_STATUS.get(env, "draft")
    return row


# ── List agents ───────────────────────────────────────────────

@router.get("/agents")
def list_agents(
    env: str | None = Query(default=None),
    domain: str | None = Query(default=None),
):
    """Return agents from domain schema(s), optionally filtered by environment."""
    cols = """agent_id, name, agent_type, owner_principal, description, instructions,
               tools_enabled, model, serving_endpoint_name, eval_profile,
               min_safety_score, min_correctness_score, environment, status,
               runtime_mode, approval_requested, mlflow_experiment_id, mlflow_url,
               eval_run_id, eval_status, created_at, updated_at"""
    order = "ORDER BY CASE environment WHEN 'prod' THEN 0 WHEN 'staging' THEN 1 ELSE 2 END, created_at DESC"

    env_filter = " AND environment = %s" if (env and env in ENVS) else ""
    env_params = (env,) if (env and env in ENVS) else ()

    domains = [domain] if domain else lakebase.list_domain_schemas()
    rows: list[dict] = []
    for d in domains:
        d_rows = lakebase.execute(
            f'SELECT {cols} FROM "{d}".agents_config WHERE 1=1{env_filter} {order}',
            env_params,
        )
        for r in d_rows:
            r["domain"] = d
        rows.extend(d_rows)

    return {"agents": [_row_to_api(r) for r in rows]}


# ── Register agent ────────────────────────────────────────────

class RegisterAgentRequest(BaseModel):
    agent_id: str
    agent_name: str
    domain: str = "default"
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
    """Register a new agent in the domain's Lakebase schema with environment='dev'."""
    lakebase.execute(
        f"""
        INSERT INTO "{body.domain}".agents_config (
            agent_id, name, agent_type, owner_principal, description, instructions,
            tools_enabled, model, serving_endpoint_name, eval_profile,
            min_safety_score, min_correctness_score,
            environment, status, runtime_mode, created_at, created_by, updated_at
        ) VALUES (
            %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s,
            'dev', 'active', %s, NOW(), %s, NOW()
        )
        ON CONFLICT (agent_id) DO UPDATE SET
            name                  = EXCLUDED.name,
            agent_type            = EXCLUDED.agent_type,
            owner_principal       = EXCLUDED.owner_principal,
            description           = EXCLUDED.description,
            instructions          = EXCLUDED.instructions,
            tools_enabled         = EXCLUDED.tools_enabled,
            model                 = EXCLUDED.model,
            serving_endpoint_name = EXCLUDED.serving_endpoint_name,
            eval_profile          = EXCLUDED.eval_profile,
            min_safety_score      = EXCLUDED.min_safety_score,
            min_correctness_score = EXCLUDED.min_correctness_score,
            runtime_mode          = EXCLUDED.runtime_mode,
            status                = 'active',
            updated_at            = NOW()
        """,
        (
            body.agent_id, body.agent_name, body.agent_type, body.owner_principal,
            body.description, body.instructions,
            body.tools_enabled, body.model, _FRAMEWORK_ENDPOINT, body.eval_profile,
            body.min_safety_score, body.min_correctness_score,
            body.runtime_mode, body.owner_principal,
        ),
    )
    return {"agent_id": body.agent_id, "status": "draft", "environment": "dev", "domain": body.domain}


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
def update_agent(agent_id: str, body: UpdateAgentRequest, domain: str | None = Query(default=None)):
    """Update an existing agent in Lakebase."""
    agent = _find_agent(agent_id, domain)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"Agente '{agent_id}' não encontrado.")
    d = agent["domain"]

    field_map = [
        ("agent_name",            "name"),
        ("agent_type",            "agent_type"),
        ("owner_principal",       "owner_principal"),
        ("description",           "description"),
        ("instructions",          "instructions"),
        ("tools_enabled",         "tools_enabled"),
        ("model",                 "model"),
        ("serving_endpoint_name", "serving_endpoint_name"),
        ("eval_profile",          "eval_profile"),
        ("min_safety_score",      "min_safety_score"),
        ("min_correctness_score", "min_correctness_score"),
        ("runtime_mode",          "runtime_mode"),
    ]
    set_clauses, params = [], []
    for body_field, col in field_map:
        val = getattr(body, body_field, None)
        if val is not None:
            set_clauses.append(f"{col} = %s")
            params.append(val)

    if not set_clauses:
        return {"agent_id": agent_id, "environment": agent.get("environment", "dev")}

    set_clauses.append("updated_at = NOW()")
    params.append(agent_id)
    lakebase.execute(
        f'UPDATE "{d}".agents_config SET {", ".join(set_clauses)} WHERE agent_id = %s',
        tuple(params),
    )
    return {"agent_id": agent_id, "environment": agent.get("environment", "dev")}


# ── Update status ─────────────────────────────────────────────

class UpdateStatusRequest(BaseModel):
    new_status: str


@router.patch("/agents/{agent_id}/status")
def update_agent_status(agent_id: str, body: UpdateStatusRequest, domain: str | None = Query(default=None)):
    if body.new_status not in VALID_STATUSES:
        raise HTTPException(status_code=400, detail=f"Status inválido: '{body.new_status}'.")
    agent = _find_agent(agent_id, domain)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"Agente '{agent_id}' não encontrado.")
    d = agent["domain"]
    lakebase.execute(
        f'UPDATE "{d}".agents_config SET status = %s, updated_at = NOW() WHERE agent_id = %s',
        (body.new_status, agent_id),
    )
    return {"agent_id": agent_id, "new_status": body.new_status, "environment": agent.get("environment", "dev")}


# ── Promote agent ─────────────────────────────────────────────

def _sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


@router.post("/agents/{agent_id}/promote")
def promote_agent(agent_id: str, domain: str | None = Query(default=None)):
    """Promote agent to the next environment (dev→staging or staging→prod). Streams SSE progress."""

    def generate():
        agent = _find_agent(agent_id, domain)
        if agent is None:
            yield _sse({"error": f"Agente '{agent_id}' não encontrado."})
            return
        d = agent["domain"]

        env_src  = agent.get("environment") or "dev"
        next_env = _ENV_NEXT.get(env_src)
        if not next_env:
            yield _sse({"error": "Agente já está no ambiente prod."})
            return

        yield _sse({"step": f"Verificando configurações do ambiente {next_env}..."})
        dst_row = lakebase.execute_one(
            "SELECT workspace_url, token FROM app.domain_envs WHERE domain = %s AND env = %s",
            (d, next_env),
        )
        if not dst_row or not dst_row.get("workspace_url"):
            yield _sse({"error": f"Ambiente '{next_env}' não configurado em domain_envs para domínio '{d}'."})
            return
        w_dst = _workspace_client_for_env(dst_row)
        staging_host = dst_row["workspace_url"].rstrip("/") if next_env == "staging" else ""
        catalog_dst = settings.framework_catalog
        wh_dst = ""  # not stored in domain_envs; Genie promotion skipped if empty

        # ── 1. Register MLflow experiment ─────────────────────────
        mlflow_experiment_id: str | None = None
        if next_env == "staging":
            yield _sse({"step": "Criando experimento MLflow no staging..."})
            exp_name = f"/Framework Agents/{agent_id}"
            try:
                try:
                    w_dst.workspace.mkdirs(path="/Framework Agents")
                except Exception:
                    pass
                try:
                    create_resp = w_dst.experiments.create_experiment(
                        name=exp_name,
                        tags=[ExperimentTag(key="mlflow.experimentType", value="GENAI_EXPERIMENT")],
                    )
                    mlflow_experiment_id = create_resp.experiment_id
                except Exception:
                    get_resp = w_dst.experiments.get_by_name(experiment_name=exp_name)
                    if get_resp and get_resp.experiment:
                        mlflow_experiment_id = get_resp.experiment.experiment_id
                        try:
                            w_dst.experiments.set_experiment_tag(
                                experiment_id=mlflow_experiment_id,
                                key="mlflow.experimentType",
                                value="GENAI_EXPERIMENT",
                            )
                        except Exception:
                            pass
                if not mlflow_experiment_id:
                    raise ValueError("experiment_id não retornado após create/get_by_name.")
            except Exception as exc:
                yield _sse({"error": f"Falha ao registrar experimento MLflow: {exc}"})
                return

        mlflow_url_val = (
            f"{staging_host}/ml/experiments/{mlflow_experiment_id}"
            if mlflow_experiment_id and staging_host
            else None
        )
        endpoint = _ENV_FRAMEWORK_ENDPOINT.get(next_env, _FRAMEWORK_ENDPOINT)

        # ── 2. Promote agent record ────────────────────────────────
        yield _sse({"step": "Promovendo agente..."})
        lakebase.execute(
            f"""
            UPDATE "{d}".agents_config
            SET environment           = %s,
                serving_endpoint_name = %s,
                mlflow_experiment_id  = COALESCE(%s, mlflow_experiment_id),
                mlflow_url            = COALESCE(%s, mlflow_url),
                approval_requested    = FALSE,
                status                = 'active',
                updated_at            = NOW()
            WHERE agent_id = %s
            """,
            (next_env, endpoint, mlflow_experiment_id, mlflow_url_val, agent_id),
        )

        # ── 3. Promote tools ───────────────────────────────────────
        tools_enabled: list[str] = agent.get("tools_enabled") or []
        if tools_enabled:
            yield _sse({"step": f"Promovendo {len(tools_enabled)} tool(s)..."})
            src_row = lakebase.execute_one(
                "SELECT workspace_url, token FROM app.domain_envs WHERE domain = %s AND env = %s",
                (d, env_src),
            )
            w_src = _workspace_client_for_env(src_row) if src_row and src_row.get("workspace_url") else None
            catalog_src = settings.framework_catalog
            for i, tool_name in enumerate(tools_enabled, 1):
                yield _sse({"step": f"Tool {i}/{len(tools_enabled)}: {tool_name}..."})
                try:
                    tool = lakebase.execute_one(
                        f'SELECT tool_name, kind, ref FROM "{d}".tools_config WHERE tool_name = %s',
                        (tool_name,),
                    )
                    if tool is None:
                        continue
                    new_ref = tool["ref"]
                    if tool.get("kind") == "mcp_genie":
                        if not w_src or not wh_dst:
                            print(f"[Promote] Genie '{tool_name}' skipped: workspace or warehouse not configured.", file=sys.stderr)
                            continue
                        try:
                            new_ref = _promote_genie_space(
                                src_ref=tool["ref"],
                                src_w=w_src,
                                target_w=w_dst,
                                src_catalog=catalog_src,
                                target_catalog=catalog_dst,
                                target_warehouse=wh_dst,
                                target_host=staging_host,
                            )
                        except HTTPException as exc:
                            print(f"[Promote] Genie space para tool '{tool_name}' falhou: {exc.detail}", file=sys.stderr)
                            continue
                    lakebase.execute(
                        f'UPDATE "{d}".tools_config SET environment = %s, ref = %s, status = \'active\', approved_at = NOW() WHERE tool_name = %s',
                        (next_env, new_ref, tool_name),
                    )
                except Exception:
                    pass  # non-critical

        yield _sse({
            "done": True,
            "agent_id": agent_id,
            "promoted_to": next_env,
            "tools_promoted": tools_enabled,
            "mlflow_experiment_id": mlflow_experiment_id,
        })

    return StreamingResponse(generate(), media_type="text/event-stream")


# ── Approval workflow ─────────────────────────────────────────

@router.post("/agents/{agent_id}/request-approval")
def request_approval(agent_id: str, domain: str | None = Query(default=None)):
    """Mark an agent as pending approval review (dev only)."""
    agent = _find_agent(agent_id, domain)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"Agente '{agent_id}' não encontrado.")
    if agent.get("environment") != "dev":
        raise HTTPException(status_code=400, detail="Apenas agentes dev podem solicitar aprovação.")
    d = agent["domain"]
    lakebase.execute(
        f'UPDATE "{d}".agents_config SET approval_requested = TRUE, updated_at = NOW() WHERE agent_id = %s',
        (agent_id,),
    )
    return {"agent_id": agent_id, "approval_requested": True}


@router.post("/agents/{agent_id}/reject-approval")
def reject_approval(agent_id: str, domain: str | None = Query(default=None)):
    """Reject an approval request, returning agent to draft state."""
    agent = _find_agent(agent_id, domain)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"Agente '{agent_id}' não encontrado.")
    d = agent["domain"]
    lakebase.execute(
        f'UPDATE "{d}".agents_config SET approval_requested = FALSE, updated_at = NOW() WHERE agent_id = %s',
        (agent_id,),
    )
    return {"agent_id": agent_id, "approval_requested": False}


# ── Run eval ──────────────────────────────────────────────────

def _run_eval_task(agent_id: str, staging_cfg: dict, domain: str) -> None:
    """Background task: run mlflow.genai.evaluate() against the staging endpoint."""
    import os
    import mlflow
    from mlflow.genai.scorers import Correctness, Safety, Guidelines

    w_staging = _workspace_client_for_env(staging_cfg)

    try:
        agent = lakebase.execute_one(
            f'SELECT serving_endpoint_name, mlflow_experiment_id FROM "{domain}".agents_config WHERE agent_id = %s',
            (agent_id,),
        )
        if not agent:
            raise ValueError(f"Agente '{agent_id}' não encontrado no Lakebase.")

        experiment_id = (agent.get("mlflow_experiment_id") or "").strip()
        endpoint      = (agent.get("serving_endpoint_name") or "").strip()

        if not experiment_id:
            raise ValueError("mlflow_experiment_id ausente.")

        entries = lakebase.execute(
            f'SELECT request, expected_response FROM "{domain}".eval_datasets WHERE agent_id = %s ORDER BY created_at',
            (agent_id,),
        )
        if not entries:
            raise ValueError("Dataset de avaliação vazio.")

        eval_data = [
            {
                "inputs": {"query": e.get("request") or ""},
                "expectations": {"expected_response": e.get("expected_response") or ""},
            }
            for e in entries
        ]

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

        lakebase.execute(
            f'UPDATE "{domain}".agents_config SET eval_status = \'completed\', eval_run_id = %s, updated_at = NOW() WHERE agent_id = %s',
            (run_id, agent_id),
        )

    except Exception as exc:
        print(f"[Eval] Falha: {exc}", file=sys.stderr)
        try:
            lakebase.execute(
                f'UPDATE "{domain}".agents_config SET eval_status = \'failed\', updated_at = NOW() WHERE agent_id = %s',
                (agent_id,),
            )
        except Exception:
            pass


@router.post("/agents/{agent_id}/run-eval")
def run_eval(agent_id: str, background_tasks: BackgroundTasks, domain: str | None = Query(default=None)):
    """Trigger async evaluation run for a staging agent."""
    agent = _find_agent(agent_id, domain)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"Agente '{agent_id}' não encontrado.")
    if agent.get("environment") != "staging":
        raise HTTPException(status_code=400, detail="Avaliação só pode ser disparada para agentes no staging.")
    d = agent["domain"]

    staging_parts = _env_client("staging")
    if staging_parts is None:
        raise HTTPException(status_code=503, detail="Staging não configurado.")
    _, s_catalog, s_schema, s_wh = staging_parts
    staging_cfg = {**_get_env_config("staging"), "catalog": s_catalog, "schema_name": s_schema, "warehouse_id": s_wh}

    lakebase.execute(
        f'UPDATE "{d}".agents_config SET eval_status = \'running\', eval_run_id = NULL, updated_at = NOW() WHERE agent_id = %s',
        (agent_id,),
    )
    background_tasks.add_task(_run_eval_task, agent_id, staging_cfg, d)
    return {"agent_id": agent_id, "eval_status": "running"}


# ── Chat with agent ───────────────────────────────────────────

class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage]


@router.post("/agents/{agent_id}/chat")
def chat_with_agent(agent_id: str, body: ChatRequest, domain: str | None = Query(default=None)):
    """Send messages to the corp-agent-framework serving endpoint for this agent."""
    agent = _find_agent(agent_id, domain)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"Agente '{agent_id}' não encontrado.")

    d   = agent["domain"]
    env = agent.get("environment") or "dev"

    # Always derive the endpoint from domain+env (matches what Settings deploys)
    endpoint = DOMAIN_ENDPOINT_NAME_TPL.format(domain=d, env=env)

    # Look up workspace credentials from Lakebase domain_envs (domain-specific workspace)
    cfg = lakebase.execute_one(
        "SELECT workspace_url, token FROM app.domain_envs WHERE domain = %s AND env = %s",
        (d, env),
    )
    if cfg and cfg.get("workspace_url"):
        w = _workspace_client_for_env({"workspace_url": cfg["workspace_url"], "token": cfg.get("token") or ""})
    else:
        # Fallback: read from old workspace_envs table (dev defaults)
        parts = _env_client(env)
        if parts is None:
            raise HTTPException(status_code=503, detail=f"Ambiente '{env}' do domínio '{d}' não configurado em domain_envs.")
        w, _, _, _ = parts

    messages = [{"role": m.role, "content": m.content} for m in body.messages]
    try:
        result = w.api_client.do(
            "POST",
            f"/serving-endpoints/{endpoint}/invocations",
            body={"input": messages, "custom_inputs": {"agent_id": agent_id}},
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Erro ao chamar endpoint '{endpoint}': {exc}",
        ) from exc

    tool_calls: list[dict] = []
    reply = ""

    # Extract tool calls and final reply from output array (MLflow ResponsesAgent format)
    for item in (result.get("output") or []):
        if not isinstance(item, dict):
            continue
        if item.get("type") == "tool_use":
            tool_calls.append({"name": item.get("name", ""), "input": item.get("input", {})})
        elif item.get("type") == "function_call":
            tool_calls.append({"name": item.get("name", ""), "input": item.get("arguments", {})})
        elif item.get("type") == "message":
            for c in (item.get("content") or []):
                if isinstance(c, dict) and c.get("type") == "output_text":
                    reply = c.get("text", "")

    if reply:
        return {"reply": reply, "tool_calls": tool_calls}

    # Fallback: OpenAI chat completions format
    choices = result.get("choices") or []
    if choices:
        msg = choices[0].get("message") or {}
        for tc in (msg.get("tool_calls") or []):
            fn = tc.get("function") or {}
            tool_calls.append({"name": fn.get("name", ""), "input": fn.get("arguments", {})})
        content = msg.get("content") or ""
        if content:
            return {"reply": content, "tool_calls": tool_calls}

    return {"reply": str(result), "tool_calls": tool_calls}


# ── Eval dataset ──────────────────────────────────────────────

class EvalEntryBody(BaseModel):
    request: str
    expected_response: str


@router.get("/agents/{agent_id}/eval-dataset")
def list_eval_dataset(agent_id: str, domain: str | None = Query(default=None)):
    d = domain or _find_agent_domain(agent_id)
    if d is None:
        raise HTTPException(status_code=404, detail=f"Agente '{agent_id}' não encontrado.")
    rows = lakebase.execute(
        f'SELECT id, agent_id, request, expected_response, created_at FROM "{d}".eval_datasets WHERE agent_id = %s ORDER BY created_at',
        (agent_id,),
    )
    return {"entries": rows}


@router.post("/agents/{agent_id}/eval-dataset", status_code=status.HTTP_201_CREATED)
def add_eval_entry(agent_id: str, body: EvalEntryBody, domain: str | None = Query(default=None)):
    d = domain or _find_agent_domain(agent_id)
    if d is None:
        raise HTTPException(status_code=404, detail=f"Agente '{agent_id}' não encontrado.")
    entry_id = str(_uuid.uuid4())
    lakebase.execute(
        f'INSERT INTO "{d}".eval_datasets (id, agent_id, request, expected_response, created_at) VALUES (%s, %s, %s, %s, NOW())',
        (entry_id, agent_id, body.request, body.expected_response),
    )
    return {"id": entry_id, "agent_id": agent_id, "request": body.request, "expected_response": body.expected_response}


@router.put("/agents/{agent_id}/eval-dataset/{entry_id}")
def update_eval_entry(agent_id: str, entry_id: str, body: EvalEntryBody, domain: str | None = Query(default=None)):
    d = domain or _find_agent_domain(agent_id)
    if d is None:
        raise HTTPException(status_code=404, detail=f"Agente '{agent_id}' não encontrado.")
    lakebase.execute(
        f'UPDATE "{d}".eval_datasets SET request = %s, expected_response = %s WHERE id = %s AND agent_id = %s',
        (body.request, body.expected_response, entry_id, agent_id),
    )
    return {"id": entry_id, "agent_id": agent_id}


@router.delete("/agents/{agent_id}/eval-dataset/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_eval_entry(agent_id: str, entry_id: str, domain: str | None = Query(default=None)):
    d = domain or _find_agent_domain(agent_id)
    if d is None:
        raise HTTPException(status_code=404, detail=f"Agente '{agent_id}' não encontrado.")
    lakebase.execute(
        f'DELETE FROM "{d}".eval_datasets WHERE id = %s AND agent_id = %s',
        (entry_id, agent_id),
    )


# ── Delete agent ──────────────────────────────────────────────

@router.delete("/agents/{agent_id}")
def delete_agent(agent_id: str, domain: str | None = Query(default=None)):
    agent = _find_agent(agent_id, domain)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"Agente '{agent_id}' não encontrado.")
    d = agent["domain"]
    # eval_datasets cascade-deletes via FK
    lakebase.execute(f'DELETE FROM "{d}".agents_config WHERE agent_id = %s', (agent_id,))
    return {"deleted": agent_id, "environment": agent.get("environment", "dev")}
