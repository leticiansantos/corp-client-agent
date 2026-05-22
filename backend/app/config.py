from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    cors_origins: list[str] = ["http://localhost:3001"]

    # App's own Databricks workspace (fevm-leticia-santos-stable)
    # Used to store workspace_envs settings table
    databricks_host: str = ""
    databricks_token: str = ""                # PAT token (legacy/dev)
    databricks_client_id: str = ""            # Service Principal OAuth M2M (preferred)
    databricks_client_secret: str = ""        # Service Principal OAuth M2M (preferred)
    databricks_warehouse_id: str = ""         # SQL Warehouse in the app workspace

    # Unity Catalog in the app workspace — stores client settings
    app_catalog: str = "leticia_santos_stable_catalog"
    app_schema: str = "corp_agent_client"

    # Default catalog/schema for corp_agent_framework in each env workspace
    framework_catalog: str = "corp_agent_framework"
    framework_schema: str = "agents"

    # Optional Bearer token required by /api/config/* endpoints (consumed by corp-agent-framework).
    # Leave empty to allow unauthenticated access (suitable for internal deployments).
    corp_config_token: str = ""

    # Public URL of this app — injected as CORP_CLIENT_AGENT_URL into serving endpoints at deploy time.
    # Defaults to the first CORS origin if not explicitly set.
    corp_client_agent_url: str = ""

    @property
    def effective_corp_client_agent_url(self) -> str:
        if self.corp_client_agent_url:
            return self.corp_client_agent_url
        return self.cors_origins[0] if self.cors_origins else ""

    # Set to true when running locally to skip WHL rebuild if already in Volume
    local_dev: bool = False

    @property
    def uc_prefix(self) -> str:
        return f"{self.app_catalog}.{self.app_schema}"


settings = Settings()
