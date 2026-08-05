from pydantic import BaseModel


class RunningVolume(BaseModel):
    type: str
    name: str | None
    source: str
    destination: str
    driver: str
    mode: str
    read_write: bool
    container_id: str
    container_name: str


class RunningVolumesResponse(BaseModel):
    count: int
    volumes: list[RunningVolume]


class StorageUsage(BaseModel):
    total_count: int
    active_count: int
    size_bytes: int
    size: str
    reclaimable_bytes: int
    reclaimable: str


class DockerStorageStatusResponse(BaseModel):
    total_size_bytes: int
    total_size: str
    total_reclaimable_bytes: int
    total_reclaimable: str
    images: StorageUsage
    containers: StorageUsage
    volumes: StorageUsage
    build_cache: StorageUsage


class VolumeAttachment(BaseModel):
    container_id: str
    container_name: str
    container_status: str
    destination: str
    read_write: bool


class ManagedVolume(BaseModel):
    name: str
    driver: str
    mountpoint: str
    created_at: str
    scope: str
    labels: dict[str, str] | None
    options: dict[str, str] | None
    attachments: list[VolumeAttachment]


class ManagedVolumesResponse(BaseModel):
    count: int
    volumes: list[ManagedVolume]


class ConfirmAction(BaseModel):
    confirm: bool = False


class StopContainerAction(ConfirmAction):
    timeout_seconds: int = 10


class RemoveVolumeResponse(BaseModel):
    name: str
    removed: bool


class PruneVolumesResponse(BaseModel):
    deleted: list[str]
    reclaimed_bytes: int
    reclaimed: str


class StopAttachedContainerResponse(BaseModel):
    volume_name: str
    container_id: str
    container_name: str
    status: str


class VolumeFileDetails(BaseModel):
    path: str
    name: str
    size_bytes: int
    mode: str
    modified_at: str
    link_target: str
    encoding: str
    content: str


class VolumeFileResponse(BaseModel):
    volume_name: str
    container_id: str
    container_name: str
    container_path: str
    file: VolumeFileDetails
