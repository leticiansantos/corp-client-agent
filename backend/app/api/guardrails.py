"""
Guardrails API — per-domain guardrail configuration.

Routes:
  GET    /api/guardrails?domain=xxx          — list all guardrails for a domain
  POST   /api/guardrails?domain=xxx          — create a new custom guardrail
  PUT    /api/guardrails/{stage}/{name}?domain=xxx — update (custom only)
  PATCH  /api/guardrails/{stage}/{name}?domain=xxx — toggle enabled (custom only)
  DELETE /api/guardrails/{stage}/{name}?domain=xxx — delete (custom only)
"""

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel

from app.api import lakebase

router = APIRouter(prefix="/api")

# Default guardrails auto-inserted at domain creation — cannot be deleted
_DEFAULT_KEYS: set[tuple[str, str]] = {
    ("input",  "pii_detector"),
    ("input",  "prompt_injection"),
    ("output", "pii_scrubber"),
}

_BUILTIN_NAMES = [
    "pii_detector", "prompt_injection", "pii_scrubber", "topic_filter", "max_length",
    "pattern_redact", "pattern_block", "keyword_block", "keyword_redact",
]


# ── Models ─────────────────────────────────────────────────────────────────────

class CreateGuardrailRequest(BaseModel):
    stage: str
    name: str
    action: str = "block"
    params: dict = {}
    description: str = ""
    enabled: bool = True
    priority: int = 0


class ToggleGuardrailRequest(BaseModel):
    enabled: bool


class UpdateGuardrailRequest(BaseModel):
    action: str = "block"
    params: dict = {}
    description: str = ""
    enabled: bool = True
    priority: int = 0


# ── Helpers ────────────────────────────────────────────────────────────────────

def _require_domain(domain: str | None) -> str:
    if not domain:
        raise HTTPException(status_code=400, detail="Query param 'domain' é obrigatório.")
    return domain


def _check_not_default(stage: str, name: str) -> None:
    if (stage, name) in _DEFAULT_KEYS:
        raise HTTPException(
            status_code=400,
            detail=f"Guardrail '{name}' ({stage}) é um default e não pode ser modificado ou removido.",
        )


# ── Routes ─────────────────────────────────────────────────────────────────────

@router.get("/guardrails")
def list_guardrails(domain: str | None = Query(default=None)):
    d = _require_domain(domain)
    lakebase.migrate_domain(d)  # idempotent — creates table + seeds defaults if missing
    rows = lakebase.execute(
        f'SELECT stage, name, action, params, enabled, priority, description, created_at, updated_at '
        f'FROM "{d}".guardrails_defaults ORDER BY stage, priority, name'
    )
    result = []
    for r in rows:
        params = r.get("params") or {}
        result.append({
            "stage":       r["stage"],
            "name":        r["name"],
            "action":      r["action"],
            "params":      params,
            "enabled":     r["enabled"],
            "priority":    r.get("priority", 0),
            "description": r.get("description") or "",
            "created_at":  str(r["created_at"]) if r.get("created_at") else None,
            "is_default":  (r["stage"], r["name"]) in _DEFAULT_KEYS,
        })
    return {"guardrails": result, "builtin_names": _BUILTIN_NAMES}


@router.post("/guardrails", status_code=status.HTTP_201_CREATED)
def create_guardrail(req: CreateGuardrailRequest, domain: str | None = Query(default=None)):
    d = _require_domain(domain)

    if req.stage not in ("input", "output", "tool"):
        raise HTTPException(status_code=400, detail="stage deve ser 'input', 'output' ou 'tool'.")

    if not req.name.strip():
        raise HTTPException(status_code=400, detail="name é obrigatório.")

    import json
    existing = lakebase.execute_one(
        f'SELECT 1 FROM "{d}".guardrails_defaults WHERE stage = %s AND name = %s',
        (req.stage, req.name),
    )
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Guardrail '{req.name}' ({req.stage}) já existe para este domínio.",
        )

    lakebase.execute(
        f'INSERT INTO "{d}".guardrails_defaults (stage, name, action, params, enabled, priority, description) '
        f'VALUES (%s, %s, %s, %s, %s, %s, %s)',
        (req.stage, req.name, req.action, json.dumps(req.params), req.enabled, req.priority, req.description),
    )
    return {"ok": True}


@router.put("/guardrails/{stage}/{name}")
def update_guardrail(stage: str, name: str, req: UpdateGuardrailRequest, domain: str | None = Query(default=None)):
    import json
    d = _require_domain(domain)
    _check_not_default(stage, name)

    lakebase.execute(
        f'UPDATE "{d}".guardrails_defaults '
        f'SET action = %s, params = %s, description = %s, enabled = %s, priority = %s, updated_at = NOW() '
        f'WHERE stage = %s AND name = %s',
        (req.action, json.dumps(req.params), req.description, req.enabled, req.priority, stage, name),
    )
    return {"ok": True}


@router.patch("/guardrails/{stage}/{name}")
def toggle_guardrail(stage: str, name: str, req: ToggleGuardrailRequest, domain: str | None = Query(default=None)):
    d = _require_domain(domain)
    _check_not_default(stage, name)

    rows = lakebase.execute(
        f'UPDATE "{d}".guardrails_defaults SET enabled = %s, updated_at = NOW() '
        f'WHERE stage = %s AND name = %s',
        (req.enabled, stage, name),
    )
    return {"ok": True}


@router.delete("/guardrails/{stage}/{name}", status_code=status.HTTP_204_NO_CONTENT)
def delete_guardrail(stage: str, name: str, domain: str | None = Query(default=None)):
    d = _require_domain(domain)
    _check_not_default(stage, name)

    lakebase.execute(
        f'DELETE FROM "{d}".guardrails_defaults WHERE stage = %s AND name = %s',
        (stage, name),
    )
