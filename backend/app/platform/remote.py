"""Credential-free canonical identities for Git remotes."""

import re
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid5

_SCP_REMOTE = re.compile(
    r"^(?:[^@/:]+@)?(?P<host>[^/:]+):(?P<path>.+)$"
)


def normalize_remote_url(raw: str) -> str:
    """Return the credential-free canonical URL used as a v1 project key."""
    value = raw.strip()
    if not value:
        raise ValueError("remote URL is required")

    scp_match = _SCP_REMOTE.fullmatch(value) if "://" not in value else None
    if scp_match is not None:
        host = scp_match.group("host").lower()
        path = scp_match.group("path")
    else:
        parsed = urlsplit(value)
        if not parsed.scheme or not parsed.hostname:
            raise ValueError("remote URL must include a host and repository path")
        host = parsed.hostname.lower()
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        path = parsed.path

    path = path.strip("/")
    path = path.removesuffix(".git")
    if not host or not path:
        raise ValueError("remote URL must include a host and repository path")
    # Canonicalizing to HTTPS also removes any username or token from SSH/HTTP input.
    return f"https://{host}/{path}"


def project_id_for_remote(raw: str) -> str:
    normalized = normalize_remote_url(raw)
    return uuid5(NAMESPACE_URL, f"repo:{normalized}").hex
