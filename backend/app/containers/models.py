from pydantic import BaseModel


class ContainerPort(BaseModel):
    container_port: int
    protocol: str
    host_ip: str | None
    host_port: int | None


class RunningContainer(BaseModel):
    id: str
    name: str
    image: str
    status: str
    created: str
    ports: list[ContainerPort]


class RunningContainersResponse(BaseModel):
    count: int
    containers: list[RunningContainer]


class AllContainersResponse(BaseModel):
    count: int
    containers: list[RunningContainer]


class ContainerResourceStatus(BaseModel):
    id: str
    name: str
    cpu_percent: float
    memory_usage_bytes: int
    memory_usage: str
    memory_limit_bytes: int
    memory_limit: str
    memory_percent: float
    network_received_bytes: int
    network_sent_bytes: int
    block_read_bytes: int
    block_write_bytes: int
    pids: int
    sampled_at: str


class ContainerStatusResponse(BaseModel):
    count: int
    total_cpu_percent: float
    total_memory_usage_bytes: int
    total_memory_usage: str
    total_network_received_bytes: int
    total_network_sent_bytes: int
    total_block_read_bytes: int
    total_block_write_bytes: int
    total_pids: int
    containers: list[ContainerResourceStatus]


class ContainerMount(BaseModel):
    type: str
    name: str | None
    source: str
    destination: str
    driver: str
    mode: str
    read_write: bool


class ContainerNetwork(BaseModel):
    name: str
    network_id: str
    endpoint_id: str
    gateway: str
    ip_address: str
    mac_address: str


class ContainerDetails(BaseModel):
    id: str
    short_id: str
    name: str
    image: str
    image_id: str
    status: str
    created: str
    started_at: str
    finished_at: str
    restart_count: int
    platform: str
    ports: list[ContainerPort]
    mounts: list[ContainerMount]
    networks: list[ContainerNetwork]
    labels: dict[str, str]


class ContainerProcessesResponse(BaseModel):
    """One `docker top` sample. Columns come from Docker, so `titles` and every
    row in `processes` share the same length and order."""

    container_id: str
    container_name: str
    titles: list[str]
    count: int
    processes: list[list[str]]


class ConfirmAction(BaseModel):
    confirm: bool = False


class StopContainerAction(ConfirmAction):
    timeout_seconds: int = 10


class RemoveContainerResponse(BaseModel):
    id: str
    name: str
    removed: bool
    removed_anonymous_volumes: bool


class PruneContainersResponse(BaseModel):
    deleted: list[str]
    reclaimed_bytes: int
    reclaimed: str


class StopContainerResponse(BaseModel):
    id: str
    name: str
    status: str


class ContainerFileDetails(BaseModel):
    path: str
    name: str
    size_bytes: int
    mode: str
    modified_at: str
    link_target: str
    encoding: str
    content: str


class ContainerFileResponse(BaseModel):
    container_id: str
    container_name: str
    file: ContainerFileDetails
