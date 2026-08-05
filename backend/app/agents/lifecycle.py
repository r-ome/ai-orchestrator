import docker
from docker.errors import DockerException

from app.agents.service import cleanup_agents


def cleanup_agent_containers() -> int:
    """Remove agent containers without touching project or credential volumes."""
    client = None
    try:
        client = docker.from_env()
        return cleanup_agents(client).removed_count
    except DockerException:
        # Docker can be unavailable while the API starts or stops. Normal API
        # requests still report that condition as HTTP 503.
        return 0
    finally:
        if client is not None:
            client.close()
