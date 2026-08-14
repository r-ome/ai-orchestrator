from pydantic import BaseModel


class ProjectRegistration(BaseModel):
    """One managed sandbox, resolved by name for the services that run in it.

    This is an internal result, not a response body. `source_path` carries the
    remote project id as `managed:<project id>`; read it with
    `app.projects.service.managed_project_key`.
    """

    sandbox_id: str
    name: str
    source_path: str
    volume_name: str
    created_at: str
    ready: bool


class RemoteProject(BaseModel):
    project_id: str
    remote_url: str
    default_branch: str | None = None
    mirror_volume: str | None = None
    mirror_fetched_at: str | None = None
    sandbox_count: int = 0
    created_at: str


class RemoteProjectsResponse(BaseModel):
    count: int
    projects: list[RemoteProject]


class RegisterRemoteProjectRequest(BaseModel):
    remote_url: str


class RemoveRemoteProjectResponse(BaseModel):
    project_id: str
    removed_mirror_volume: str | None = None
