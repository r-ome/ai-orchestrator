from pathlib import Path
from typing import Any

import pytest
from conftest import register_ready_v1_sandbox

from app.controller.store import ControllerStore
from app.previews.config import get_preview_settings
from app.previews.models import (
    PreviewConfiguration,
    PreviewMode,
    PreviewRuntime,
    StartPreviewRequest,
)
from app.previews.service import start_preview
from app.projects.models import ProjectRegistration


def test_preparing_row_exists_before_any_docker_resource_is_created(
    tmp_path: Path,
    fake_docker_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ControllerStore(tmp_path / "controller.sqlite3")
    store.initialize()
    project = ProjectRegistration(
        sandbox_id="sandbox-1",
        name="sample",
        source_path="managed:project-1",
        volume_name="sample-volume",
        created_at="2026-08-11T00:00:00Z",
        ready=True,
    )
    monkeypatch.setattr(
        "app.previews._shared.inspect_registered_project",
        lambda *_: project,
    )
    config = PreviewConfiguration(
        mode=PreviewMode.NATIVE,
        runtime=PreviewRuntime.STATIC,
        image="alpine:latest",
        start_command="serve",
        container_port=3000,
        host_port=43000,
    )
    digest = "d" * 64
    register_ready_v1_sandbox(
        store,
        sandbox_id=project.sandbox_id,
        project_id="project-1",
        project_name=project.name,
        volume_name=project.volume_name,
        created_at=project.created_at,
    )
    store.create_review(
        review_id="proposal-1",
        sandbox_id=project.sandbox_id,
        proposal_digest=digest,
        detected_mode=PreviewMode.NATIVE.value,
        config=config.model_dump(mode="json"),
        protected_files={},
        changes=[],
        created_at="2026-08-11T00:00:00Z",
        expires_at="9999-12-31T23:59:59Z",
    )

    observed_statuses: list[str] = []
    original_create = fake_docker_client.containers.create

    def create_resource(**kwargs: Any) -> Any:
        active = store.active_preview(project.sandbox_id)
        assert active is not None
        observed_statuses.append(str(active["status"]))
        return original_create(**kwargs)

    monkeypatch.setattr(fake_docker_client.containers, "create", create_resource)
    monkeypatch.setattr(
        "app.previews.service._start_native",
        lambda *_args, **_kwargs: {
            "containers": [],
            "networks": [],
            "volumes": [],
            "images": [],
        },
    )

    run = start_preview(
        fake_docker_client,
        store,
        get_preview_settings(),
        project.name,
        StartPreviewRequest(
            proposal_id="proposal-1",
            proposal_digest=digest,
            config=config,
        ),
    )

    assert observed_statuses
    assert set(observed_statuses) == {"preparing"}
    assert run.status == "running"
