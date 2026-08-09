from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from app.agents.models import AgentProvider


COMMAND_KINDS = ("build", "test", "lint", "typecheck", "format")


class ContextStatus(StrEnum):
    GENERATING = "generating"
    READY = "ready"
    FAILED = "failed"


class ContextModule(BaseModel):
    path: str
    purpose: str


class ContextSymbol(BaseModel):
    name: str
    location: str
    role: str


class ResolvedCommand(BaseModel):
    """A proposed command and its controller-confirmed status."""

    kind: str
    command: str
    confirmed: bool
    reason: str


class ContextManifest(BaseModel):
    """Repository pointers and constraints, without copied source code."""

    modules: list[ContextModule] = Field(default_factory=list)
    symbols: list[ContextSymbol] = Field(default_factory=list)
    architecture: list[str] = Field(default_factory=list)
    patterns: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    commands: dict[str, str] = Field(default_factory=dict)


class ImplementationContext(BaseModel):
    id: str
    session_id: str
    sandbox_id: str
    status: ContextStatus
    manifest: ContextManifest | None = None
    commands: list[ResolvedCommand] = Field(default_factory=list)
    inventory: dict[str, Any] | None = None
    provider: AgentProvider | None = None
    model: str | None = None
    error: str | None = None
    created_at: str
    updated_at: str
    settled_at: str | None = None

    @property
    def confirmed_commands(self) -> dict[str, str]:
        return {
            command.kind: command.command
            for command in self.commands
            if command.confirmed
        }


class GenerateContextRequest(BaseModel):
    provider: AgentProvider = AgentProvider.CLAUDE
    model: str | None = Field(default=None, max_length=100)


class GenerateContextOutcome(BaseModel):
    context: ImplementationContext
    accepted: bool
    attempts: int
    validation_errors: list[str] = Field(default_factory=list)
    turn_status: str
    turn_error: str | None = None
    unconfirmed_commands: list[ResolvedCommand] = Field(default_factory=list)
