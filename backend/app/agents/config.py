import os
from dataclasses import dataclass
from functools import lru_cache

from app.agents.models import AgentProvider, AgentProviderDetails


@dataclass(frozen=True)
class AgentProviderConfig:
    provider: AgentProvider
    image: str
    command: tuple[str, ...]
    credential_environment_variable: str

    @property
    def credential_directory(self) -> str:
        return "/auth"

    def details(self) -> AgentProviderDetails:
        return AgentProviderDetails(
            provider=self.provider,
            image=self.image,
            command=list(self.command),
            credential_directory=self.credential_directory,
            credential_environment_variable=self.credential_environment_variable,
        )


@dataclass(frozen=True)
class AgentSettings:
    claude_image: str
    codex_image: str
    # A coding agent edits and runs project code, so it gets less headroom
    # than a preview's dev server, but still enough for a language server
    # and a build alongside the agent process itself.
    agent_memory: str = "2g"

    def provider(self, provider: AgentProvider) -> AgentProviderConfig:
        if provider is AgentProvider.CLAUDE:
            return AgentProviderConfig(
                provider=provider,
                image=self.claude_image,
                command=("claude",),
                credential_environment_variable="CLAUDE_CONFIG_DIR",
            )
        return AgentProviderConfig(
            provider=provider,
            image=self.codex_image,
            command=("codex",),
            credential_environment_variable="CODEX_HOME",
        )

    def providers(self) -> list[AgentProviderConfig]:
        return [
            self.provider(AgentProvider.CLAUDE),
            self.provider(AgentProvider.CODEX),
        ]


@lru_cache
def get_agent_settings() -> AgentSettings:
    return AgentSettings(
        claude_image=os.getenv(
            "CLAUDE_AGENT_IMAGE",
            "orchestrator-agent-claude:latest",
        ),
        codex_image=os.getenv(
            "CODEX_AGENT_IMAGE",
            "orchestrator-agent-codex:latest",
        ),
        agent_memory=os.getenv("AGENT_MEMORY", "2g"),
    )
