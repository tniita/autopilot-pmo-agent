import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import quote, urlparse

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ADO_LABEL = "ado"
AGENT365_SCOPE = "ea9ffc3e-8a23-4a7d-836d-234d7c7565c1/.default"
ADO_SCOPE = "https://mcp.dev.azure.com/.default"
AGENT365_BASE = "https://agent365.svc.cloud.microsoft/agents/servers"
_LABEL = r"^[a-z][a-z0-9-]{1,30}$"
TRUSTED_M365_HOSTS = {"agent365.svc.cloud.microsoft", "workiq.svc.cloud.microsoft"}
WORKIQ_SCOPE = "api://workiq.svc.cloud.microsoft/.default"


@dataclass(frozen=True)
class McpServer:
    label: str
    url: str
    scope: str


# Work IQ servers used by the official Foundry autopilot sample, called as the agent user.
DEFAULT_M365_SERVERS = {
    "teams": f"{AGENT365_BASE}/mcp_TeamsServer",
    "calendar": f"{AGENT365_BASE}/mcp_CalendarTools",
}

# Read-only tools only. Unknown or renamed tools are never enabled implicitly.
DEFAULT_ALLOWED_TOOLS = [
    "ado__wit_work_item",
    "ado__wit_query",
    "ado__wit_backlog",
    "teams__GetChat",
    "teams__GetChatMessage",
    "teams__ListChatMessages",
    "teams__ListChats",
    "teams__GetChannel",
    "teams__GetTeam",
    "teams__ListChannelMessages",
    "teams__ListChannelMessageReplies",
    "teams__ListChannels",
    "calendar__ListCalendarView",
    "calendar__ListEvents",
]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", env_prefix="PMO_", populate_by_name=True
    )

    environment: Literal["local", "azure"] = "local"
    foundry_project_endpoint: str = Field(
        validation_alias=AliasChoices(
            "foundry_project_endpoint", "PMO_FOUNDRY_PROJECT_ENDPOINT", "FOUNDRY_PROJECT_ENDPOINT"
        )
    )
    provider: Literal["claude", "gpt"] = "claude"
    anthropic_model: str | None = Field(default=None, min_length=1)
    gpt_model: str | None = Field(default=None, min_length=1)
    ado_organization: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{0,49}$")
    # Optional extra guard. The real boundary is the agent user's Azure DevOps access.
    ado_project: str | None = Field(default=None, min_length=1, max_length=128)
    m365_servers: dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_M365_SERVERS))
    allowed_tools: list[str] = Field(
        default_factory=lambda: list(DEFAULT_ALLOWED_TOOLS), min_length=1
    )
    # Client ID of the identity used for model and Blob access (default agent instance).
    runtime_client_id: str | None = None
    storage_account_name: str = Field(default="", pattern=r"^[a-z0-9]{0,24}$")
    storage_connection_string: str | None = None
    storage_container: str = Field(default="pmo-conversations", pattern=r"^[a-z0-9-]{3,63}$")
    auth_handler: str = "AGENTIC"
    generation_timeout: int = Field(default=240, ge=30, le=600)
    max_turns: int = Field(default=30, ge=5, le=60)

    @field_validator("m365_servers")
    @classmethod
    def validate_servers(cls, servers: dict[str, str]) -> dict[str, str]:
        for label, url in servers.items():
            parsed = urlparse(url)
            if not re.match(_LABEL, label) or label == ADO_LABEL:
                raise ValueError(f"Invalid MCP server label: {label}")
            trusted = parsed.hostname in TRUSTED_M365_HOSTS
            if parsed.scheme != "https" or not trusted or parsed.query or parsed.fragment:
                raise ValueError(f"Untrusted Microsoft 365 MCP endpoint: {url}")
        return servers

    @field_validator("allowed_tools")
    @classmethod
    def validate_tools(cls, tools: list[str]) -> list[str]:
        for name in tools:
            label, _, tool = name.partition("__")
            if not label or not tool or not tool.replace("_", "").isalnum():
                raise ValueError(f"Invalid tool name: {name}")
        return tools

    @model_validator(mode="after")
    def validate_settings(self) -> "Settings":
        if not (self.gpt_model if self.provider == "gpt" else self.anthropic_model):
            raise ValueError(f"Missing model deployment name for {self.provider}")
        endpoint = urlparse(self.foundry_project_endpoint)
        if (
            endpoint.scheme != "https"
            or not endpoint.hostname
            or not endpoint.hostname.endswith(".services.ai.azure.com")
            or endpoint.username
            or endpoint.port
            or endpoint.query
            or endpoint.fragment
            or not endpoint.path.startswith("/api/projects/")
        ):
            raise ValueError("A Foundry project HTTPS endpoint is required")
        labels = {ADO_LABEL, *self.m365_servers}
        unknown = {name.partition("__")[0] for name in self.allowed_tools} - labels
        if unknown:
            raise ValueError(f"Allowed tools reference unknown servers: {sorted(unknown)}")
        if self.environment == "azure":
            if self.storage_connection_string or not self.storage_account_name:
                raise ValueError("Azure mode requires PMO_STORAGE_ACCOUNT_NAME and Entra auth")
        elif not self.storage_connection_string:
            raise ValueError("Local mode requires an Azurite storage connection string")
        return self

    @property
    def servers(self) -> list[McpServer]:
        ado_url = f"https://mcp.dev.azure.com/{quote(self.ado_organization, safe='')}"
        servers = [McpServer(ADO_LABEL, ado_url, ADO_SCOPE)]
        servers += [
            McpServer(label, url, AGENT365_SCOPE if "agent365" in url else WORKIQ_SCOPE)
            for label, url in self.m365_servers.items()
        ]
        return servers

    @property
    def anthropic_base_url(self) -> str:
        return f"https://{urlparse(self.foundry_project_endpoint).hostname}/anthropic"

    @property
    def openai_base_url(self) -> str:
        return f"https://{urlparse(self.foundry_project_endpoint).hostname}/openai/v1/"
