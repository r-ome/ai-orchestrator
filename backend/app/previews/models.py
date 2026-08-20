import re
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, Field, model_validator

ENVIRONMENT_VARIABLE_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class PreviewMode(StrEnum):
    NATIVE = "native"
    DOCKERFILE = "dockerfile"
    COMPOSE = "compose"
    UNKNOWN = "unknown"


class PreviewRuntime(StrEnum):
    STATIC = "static"
    VITE = "vite"
    ASTRO = "astro"
    NEXTJS = "nextjs"
    FASTAPI = "fastapi"
    UNKNOWN = "unknown"


class PreviewNetworkAccess(StrEnum):
    ISOLATED = "isolated"
    INTERNET = "internet"


class PreviewAction(StrEnum):
    START = "start"
    REUSE = "reuse"
    RESTART = "restart"
    REBUILD = "rebuild"


class PreviewKind(StrEnum):
    """What a preview stack serves, and therefore where its files come from.

    LIVE mounts the sandbox volume itself, so a coding agent's edit reaches the
    browser through hot module replacement with no restart. TASK exports one
    commit into a run-scoped volume, so a human reviews exactly the code that
    would merge and nothing the agent writes afterwards can change it.
    """

    LIVE = "live"
    TASK = "task"


class PreviewServiceType(StrEnum):
    MYSQL = "mysql"
    POSTGRES = "postgres"
    SQLITE = "sqlite"


class PreviewPersistence(StrEnum):
    EPHEMERAL = "ephemeral"
    PERSISTENT = "persistent"


class PreviewSharing(StrEnum):
    """How much of the database a sandbox shares with its sibling sandboxes.

    ISOLATED keeps today's behaviour: one server container per preview run.
    SHARED_SERVER reuses one server per project but gives each sandbox its own
    schema, so a migration in one sandbox stays invisible to the others.
    SHARED_DATA pointed a sandbox at another sandbox's schema. It is historical:
    every managed sandbox owns its schema, so the value only parses rows and
    manifests written before that rule. Nothing can select it.
    """

    ISOLATED = "isolated"
    SHARED_SERVER = "shared_server"
    SHARED_DATA = "shared_data"


class PreviewDependencyService(BaseModel):
    type: PreviewServiceType
    image: str = Field(min_length=1, max_length=255)
    database: str = Field(
        default="atc_preview",
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9_]+$",
    )
    persistence: PreviewPersistence = PreviewPersistence.EPHEMERAL
    sharing: PreviewSharing = PreviewSharing.ISOLATED
    """Sandbox whose schema this sandbox joins. Only for SHARED_DATA."""
    share_target: str = Field(default="", max_length=64, pattern=r"^[A-Za-z0-9]*$")

    @model_validator(mode="after")
    def validate_sharing(self) -> "PreviewDependencyService":
        if self.sharing is PreviewSharing.SHARED_DATA and not self.share_target:
            raise ValueError("Shared database data requires share_target")
        if (
            self.sharing is PreviewSharing.SHARED_DATA
            and self.type is not PreviewServiceType.MYSQL
        ):
            raise ValueError("shared_data is supported only for MySQL databases")
        if self.sharing is not PreviewSharing.SHARED_DATA and self.share_target:
            raise ValueError("share_target applies only to shared_data sharing")
        return self


class PreviewInitialization(BaseModel):
    commands: list[Annotated[str, Field(min_length=1, max_length=2_000)]] = Field(
        default_factory=list,
        max_length=20,
    )


class PreviewEnvironmentSource(BaseModel):
    from_service: str = Field(default="", max_length=100)
    from_secret: str = Field(default="", max_length=100)

    @model_validator(mode="after")
    def validate_source(self) -> "PreviewEnvironmentSource":
        if bool(self.from_service) == bool(self.from_secret):
            raise ValueError(
                "Environment source requires exactly one of from_service or from_secret"
            )
        return self


class PreviewConfiguration(BaseModel):
    mode: PreviewMode
    runtime: PreviewRuntime = PreviewRuntime.UNKNOWN
    image: str = Field(default="", max_length=255)
    install_command: str = Field(default="", max_length=2_000)
    start_command: str = Field(default="", max_length=2_000)
    container_port: int = Field(ge=1, le=65_535)
    host_port: int | None = Field(default=None, ge=1, le=65_535)
    selected_service: str = Field(default="", max_length=100)
    compose_file: str = Field(default="", max_length=255)
    dockerfile: str = Field(default="", max_length=255)
    network_access: PreviewNetworkAccess = PreviewNetworkAccess.ISOLATED
    expiry_minutes: int = Field(default=30, ge=0, le=1_440)
    persistent_volumes: list[str] = Field(default_factory=list, max_length=100)
    services: dict[str, PreviewDependencyService] = Field(
        default_factory=dict,
        max_length=10,
    )
    initialize: PreviewInitialization = Field(default_factory=PreviewInitialization)
    environment: dict[str, PreviewEnvironmentSource] = Field(
        default_factory=dict,
        max_length=100,
    )

    @model_validator(mode="after")
    def validate_mode_settings(self) -> "PreviewConfiguration":
        if self.mode is PreviewMode.UNKNOWN:
            return self
        native_only_environment = {
            variable: source
            for variable, source in self.environment.items()
            if source.from_service
        }
        if self.mode is PreviewMode.NATIVE:
            if not self.image or not self.start_command:
                raise ValueError("Native previews require image and start_command")
        elif self.services or self.initialize.commands or native_only_environment:
            raise ValueError(
                "Controller-managed services are supported only for native previews"
            )
        if self.mode is PreviewMode.DOCKERFILE and not self.dockerfile:
            raise ValueError("Dockerfile previews require dockerfile")
        if self.mode is PreviewMode.COMPOSE and (
            not self.compose_file or not self.selected_service
        ):
            raise ValueError(
                "Compose previews require compose_file and selected_service"
            )
        unsupported_services = sorted(set(self.services) - {"database"})
        if unsupported_services:
            raise ValueError(
                "Unsupported controller-managed services: "
                + ", ".join(unsupported_services)
            )
        for variable, source in self.environment.items():
            if not ENVIRONMENT_VARIABLE_PATTERN.fullmatch(variable):
                raise ValueError(f"Invalid environment variable name: {variable}")
            if source.from_secret:
                continue
            if variable != "DATABASE_URL":
                raise ValueError(
                    "Controller-managed services support only DATABASE_URL"
                )
            if source.from_service not in self.services:
                raise ValueError(
                    f"Environment variable {variable} references an unknown service"
                )
        if "database" in self.services:
            source = self.environment.get("DATABASE_URL")
            if source is None or source.from_service != "database":
                raise ValueError(
                    "Database previews require DATABASE_URL from the database service"
                )
        elif self.initialize.commands:
            raise ValueError("Initialization commands require a database service")
        return self


class ProtectedFileChange(BaseModel):
    path: str
    change: str
    current_hash: str
    baseline_hash: str
    diff: str


class DatabaseSharingState(BaseModel):
    """The database coupling of one sandbox, shown wherever the sandbox is."""

    sandbox_id: str
    sharing: PreviewSharing
    schema_name: str
    owner_sandbox_id: str
    owner_project_name: str
    image: str
    persistence: PreviewPersistence
    server_container: str
    attached_project_names: list[str]


class ProjectDatabaseSharing(BaseModel):
    project_name: str
    sandbox_id: str
    current: DatabaseSharingState | None


class PreviewProposal(BaseModel):
    id: str
    digest: str
    sandbox_id: str
    project_name: str
    detected_mode: PreviewMode
    detected_runtime: PreviewRuntime
    confidence: str
    evidence: list[str]
    available_services: list[str]
    config: PreviewConfiguration
    protected_files: dict[str, str]
    changes: list[ProtectedFileChange]
    approval_required: bool
    created_at: str
    expires_at: str
    required_environment: list[str] = Field(default_factory=list)
    missing_environment: list[str] = Field(default_factory=list)
    configured_environment: list[str] = Field(default_factory=list)


class StartPreviewRequest(BaseModel):
    proposal_id: str = Field(min_length=1)
    proposal_digest: str = Field(min_length=64, max_length=64)
    config: PreviewConfiguration
    action: PreviewAction = PreviewAction.START
    actor: str = Field(default="human", min_length=1, max_length=100)
    save_default: bool = False
    # Naming a task is what makes this a task preview; there is no separate
    # kind field to contradict it.
    task_id: str = Field(default="", pattern=r"^([0-9a-f]{32})?$")


class PreviewContainer(BaseModel):
    id: str
    name: str
    service: str
    status: str


class PreviewRun(BaseModel):
    id: str
    sandbox_id: str
    project_name: str
    proposal_id: str
    mode: PreviewMode
    kind: PreviewKind = PreviewKind.LIVE
    task_id: str | None = None
    commit_sha: str | None = None
    runtime: PreviewRuntime
    status: str
    selected_service: str
    container_port: int
    host_port: int | None
    url: str
    network_access: PreviewNetworkAccess
    created_at: str
    started_at: str
    expires_at: str
    last_activity_at: str
    containers: list[PreviewContainer]
    database_sharing: DatabaseSharingState | None = None


class PreviewActionRequest(BaseModel):
    action: PreviewAction
    confirm: bool = False


class StopPreviewRequest(BaseModel):
    confirm: bool = False
    remove_data_volumes: bool = False


class StopPreviewResponse(BaseModel):
    id: str
    stopped: bool
    removed_containers: int
    removed_networks: int
    removed_volumes: int
    removed_images: int


class KeepAliveRequest(BaseModel):
    expiry_minutes: int = Field(default=30, ge=0, le=1_440)


class PreviewProgressEvent(BaseModel):
    id: int
    level: str
    step: str
    message: str
    created_at: str
    started_at: str | None = None
    duration_ms: int | None = None


class PreviewLogs(BaseModel):
    proposal_id: str
    preview_id: str
    status: str
    events: list[PreviewProgressEvent]
    logs: dict[str, str]


class ProjectSecretName(BaseModel):
    name: str
    updated_at: str


class ProjectSecrets(BaseModel):
    project_name: str
    names: list[ProjectSecretName]


class SetProjectSecretsRequest(BaseModel):
    values: dict[str, str] = Field(max_length=100)

    @model_validator(mode="after")
    def validate_values(self) -> "SetProjectSecretsRequest":
        for name, value in self.values.items():
            if not ENVIRONMENT_VARIABLE_PATTERN.fullmatch(name):
                raise ValueError(f"Invalid environment variable name: {name}")
            if len(value.encode("utf-8")) > 8_192:
                raise ValueError(f"Secret value for {name} exceeds 8 KiB")
        return self


class ImportProjectSecretsResponse(BaseModel):
    project_name: str
    imported: list[str]
    skipped: list[str]
