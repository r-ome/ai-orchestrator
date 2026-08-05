from enum import StrEnum

from pydantic import BaseModel, Field


class AgentProvider(StrEnum):
    CLAUDE = "claude"
    CODEX = "codex"


class CreateAgentRequest(BaseModel):
    project_name: str = Field(min_length=1, max_length=100)
    provider: AgentProvider
    credential_profile: str = Field(
        default="default",
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$",
    )


class ReplaceAgentRequest(BaseModel):
    provider: AgentProvider
    credential_profile: str = Field(
        default="default",
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$",
    )
    confirm: bool = False
    timeout_seconds: int = Field(default=2, ge=1, le=30)


class AgentProviderDetails(BaseModel):
    provider: AgentProvider
    image: str
    command: list[str]
    credential_directory: str
    credential_environment_variable: str


class AgentProvidersResponse(BaseModel):
    providers: list[AgentProviderDetails]


class CodingAgent(BaseModel):
    id: str
    run_id: str
    sandbox_id: str
    short_id: str
    name: str
    provider: AgentProvider
    image: str
    command: list[str]
    status: str
    created_at: str
    project_name: str
    project_volume: str
    credential_profile: str
    credential_volume: str
    workspace: str
    websocket_url: str


class CodingAgentsResponse(BaseModel):
    count: int
    agents: list[CodingAgent]


class StopAgentResponse(BaseModel):
    id: str
    name: str
    stopped: bool


class StopAgentRequest(BaseModel):
    confirm: bool = False
    timeout_seconds: int = Field(default=2, ge=1, le=30)


class CleanupAgentsResponse(BaseModel):
    removed_count: int
