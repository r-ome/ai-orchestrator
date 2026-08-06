from pydantic import BaseModel, Field


class CopyProjectRequest(BaseModel):
    path: str = Field(min_length=1)


class RemoveProjectRequest(BaseModel):
    confirm: bool = False


class RemoveProjectResponse(BaseModel):
    project_name: str
    removed_containers: int
    removed_networks: int
    removed_volumes: int


class ProjectRegistration(BaseModel):
    sandbox_id: str
    name: str
    source_path: str
    volume_name: str
    created_at: str
    copy_mode: str
    file_count: int
    copied_bytes: int
    copied_size: str
    driver: str
    mountpoint: str
    copy_job_id: str
    copy_status: str
    ready: bool
    excluded_directories: list[str]


class ProjectRegistrationsResponse(BaseModel):
    count: int
    projects: list[ProjectRegistration]


class ProjectCopyJobStatus(BaseModel):
    job_id: str
    sandbox_id: str
    project_name: str
    source_path: str
    volume_name: str
    status: str
    docker_status: str
    ready: bool
    created_at: str
    started_at: str
    finished_at: str
    exit_code: int | None
    error: str
    log_tail: str
    status_url: str
    excluded_directories: list[str]


class ProjectCopyJobsResponse(BaseModel):
    count: int
    jobs: list[ProjectCopyJobStatus]


class BrowseEntry(BaseModel):
    name: str
    path: str
    has_children: bool


class BrowseResponse(BaseModel):
    root: str
    path: str
    parent: str | None
    entries: list[BrowseEntry]
