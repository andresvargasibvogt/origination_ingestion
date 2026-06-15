"""Shared infrastructure configuration.

The infra-level settings (Fabric target, Defender staging account, managed
identity) are identical across every source. The promoter — which is
source-agnostic — uses these directly. Source packages keep their own
Settings for their source-specific knobs (user-agent, throttle, relevance
path) and repeat the infra fields with the same env-var names so one ACA
Job env block applies uniformly.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# OneLake ABFS endpoint (constant across all sources/workspaces).
ONELAKE_ACCOUNT_URL: str = "https://onelake.dfs.fabric.microsoft.com"


class CommonSettings(BaseSettings):
    """Infra settings shared by every source + the promoter.

    Field env-var names match the source configs exactly (FABRIC_*, STG_*,
    AZURE_CLIENT_ID) so the same Job env block works everywhere.
    """

    model_config = SettingsConfigDict(
        env_prefix="",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    fabric_workspace_name: str | None = Field(default=None, alias="FABRIC_WORKSPACE_NAME")
    fabric_lakehouse_name: str = Field(default="lh_esp_origination", alias="FABRIC_LAKEHOUSE_NAME")

    stg_account_name: str | None = Field(default=None, alias="STG_ACCOUNT_NAME")
    stg_container_untrusted: str = Field(default="untrusted", alias="STG_CONTAINER_UNTRUSTED")
    stg_container_quarantine: str = Field(default="quarantine", alias="STG_CONTAINER_QUARANTINE")

    azure_client_id: str | None = Field(default=None, alias="AZURE_CLIENT_ID")


def load_common_settings() -> CommonSettings:
    """Construct CommonSettings from the current process environment."""
    return CommonSettings()  # type: ignore[call-arg]
