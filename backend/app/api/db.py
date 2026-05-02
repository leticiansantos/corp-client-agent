"""
Shared Databricks SQL execution helpers.
Connects to the app's own workspace (fevm-leticia-santos-stable) to read/write settings.
"""

import os
import time

from databricks.sdk import WorkspaceClient
from fastapi import HTTPException, status

from app.config import settings


def _sync_env() -> None:
    """Push app workspace credentials into env vars for SDK auto-detection."""
    if settings.databricks_host:
        os.environ["DATABRICKS_HOST"] = settings.databricks_host
    # OAuth M2M takes priority over PAT
    if settings.databricks_client_id and settings.databricks_client_secret:
        os.environ["DATABRICKS_CLIENT_ID"]     = settings.databricks_client_id
        os.environ["DATABRICKS_CLIENT_SECRET"] = settings.databricks_client_secret
        os.environ.pop("DATABRICKS_TOKEN", None)
    elif settings.databricks_token:
        os.environ["DATABRICKS_TOKEN"] = settings.databricks_token
        os.environ.pop("DATABRICKS_CLIENT_ID",     None)
        os.environ.pop("DATABRICKS_CLIENT_SECRET", None)


def _has_credentials() -> bool:
    if settings.databricks_client_id and settings.databricks_client_secret:
        return True
    return bool(settings.databricks_token)


def _get_workspace_client() -> WorkspaceClient:
    if not _has_credentials():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Credenciais do app workspace não configuradas. "
                "Defina DATABRICKS_CLIENT_ID + DATABRICKS_CLIENT_SECRET ou DATABRICKS_TOKEN no .env."
            ),
        )
    if not settings.databricks_warehouse_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="DATABRICKS_WAREHOUSE_ID não configurado no backend.",
        )
    _sync_env()
    return WorkspaceClient()


def _execute_sql(statement: str):
    """
    Execute SQL against the app workspace warehouse.
    Polls until SUCCEEDED/FAILED.
    """
    w = _get_workspace_client()
    try:
        response = w.statement_execution.execute_statement(
            warehouse_id=settings.databricks_warehouse_id,
            statement=statement.strip(),
            wait_timeout="50s",
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Erro ao conectar ao Databricks: {exc}",
        ) from exc

    statement_id = response.statement_id
    deadline = time.time() + 300  # 5 min max
    while True:
        state = str(response.status.state) if response.status else "UNKNOWN"
        state_up = state.upper()
        if "SUCCEEDED" in state_up:
            return response
        if any(s in state_up for s in ("FAILED", "CANCELED", "CLOSED")):
            error_msg = (
                response.status.error.message
                if (response.status and response.status.error)
                else state
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"SQL falhou (state={state}): {error_msg}",
            )
        if time.time() > deadline:
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                detail=f"SQL timeout após 5 min (state={state})",
            )
        time.sleep(2)
        try:
            response = w.statement_execution.get_statement(statement_id)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Erro ao verificar status do statement: {exc}",
            ) from exc


def _rows_to_dicts(response) -> list[dict]:
    if not response.manifest or not response.manifest.schema:
        return []
    columns = [col.name for col in response.manifest.schema.columns]
    rows = (response.result and response.result.data_array) or []
    return [dict(zip(columns, row)) for row in rows]


def _esc(value: str) -> str:
    return value.replace("'", "''")
