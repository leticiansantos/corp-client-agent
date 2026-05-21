"""
Run API — proxy chat messages to a framework serving endpoint.

Routes:
  POST /api/run/chat  — send messages to corp-config-driven-agent-{env}
"""

import json

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from app.api import lakebase
from app.api.settings import (
    DOMAIN_ENDPOINT_NAME_TPL,
    _get_env_session,
)

router = APIRouter(prefix="/api/run")

ENVS = ("dev", "staging", "prod")


class ChatMessage(BaseModel):
    role: str   # "user" | "assistant"
    content: str


class ChatRequest(BaseModel):
    env: str
    agent_id: str
    messages: list[ChatMessage]
    domain: str | None = None


@router.post("/chat")
def chat(body: ChatRequest):
    if body.env not in ENVS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Ambiente inválido: '{body.env}'. Aceitos: {', '.join(ENVS)}",
        )

    if not body.agent_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="agent_id é obrigatório.",
        )

    if not body.domain:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="domain é obrigatório.",
        )

    rows = lakebase.execute(
        "SELECT workspace_url, token FROM app.domain_envs WHERE domain = %s AND env = %s",
        (body.domain, body.env),
    )
    if not rows or not rows[0].get("workspace_url") or not rows[0].get("token"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Workspace do domínio '{body.domain}/{body.env}' não configurado. Configure em Configurações.",
        )
    env_cfg = rows[0]
    endpoint_name = DOMAIN_ENDPOINT_NAME_TPL.format(domain=body.domain, env=body.env)

    try:
        sess = _get_env_session(env_cfg)
        result = sess._post(
            f"/serving-endpoints/{endpoint_name}/invocations",
            body={
                "input": [{"role": m.role, "content": m.content} for m in body.messages],
                "custom_inputs": {"agent_id": body.agent_id},
            },
        )
    except Exception as exc:
        import socket
        if "timed out" in str(exc).lower() or isinstance(exc, (TimeoutError, socket.timeout)):
            detail = (
                f"O endpoint '{endpoint_name}' demorou demais para responder. "
                "Pode estar fazendo cold start — tente novamente em alguns segundos."
            )
        else:
            detail = f"Erro ao chamar o endpoint '{endpoint_name}': {exc}"
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail) from exc

    # Format 1: Responses API  {"output": [{"type": "message", "content": [{"type": "output_text", "text": "..."}]}]}
    output = result.get("output") or []
    if output:
        parts = []
        for item in output:
            if item.get("type") == "message":
                for block in (item.get("content") or []):
                    if block.get("type") == "output_text" and block.get("text"):
                        parts.append(block["text"])
        text = "\n".join(parts) if parts else ""
    else:
        # Format 2: Chat completions  {"choices": [{"message": {"content": "..."}}]}
        choices = result.get("choices") or []
        if choices:
            text = ((choices[0].get("message") or {}).get("content") or "")
        else:
            text = json.dumps(result)

    return {"response": text, "raw": result}
