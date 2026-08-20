from app.platform import labels

EXPECTED_LABELS = {
    "LABEL_MANAGED": "orchestrator.preview.managed",
    "LABEL_DATA_MANAGED": "orchestrator.preview.data-managed",
    "LABEL_CONTROLLER_MANAGED": "orchestrator.managed",
    "LABEL_SANDBOX_ID": "orchestrator.sandbox.id",
    "LABEL_TASK_ID": "orchestrator.task.id",
    "LABEL_RUN_ID": "orchestrator.run.id",
    "LABEL_KIND": "orchestrator.kind",
    "LABEL_SERVICE": "orchestrator.preview.service",
    "LABEL_EXPIRES_AT": "orchestrator.preview.expires-at",
    "LABEL_PERSISTENT": "orchestrator.preview.persistent",
    "LABEL_PROJECT_ID": "orchestrator.project.id",
    "LABEL_SHARED_DATABASE": "orchestrator.shared-database",
    "LABEL_SHARED_DATABASE_IMAGE": "orchestrator.shared-database.image",
    "LABEL_PROJECT_SOURCE": "orchestrator.project.source",
    "LABEL_LIFECYCLE_VERSION": "orchestrator.lifecycle.version",
    "LABEL_PROJECT_MIRROR": "orchestrator.project.mirror",
}


def test_label_values() -> None:
    assert {name: getattr(labels, name) for name in EXPECTED_LABELS} == EXPECTED_LABELS


def test_label_names_are_closed() -> None:
    assert {name for name in dir(labels) if name.startswith("LABEL_")} == set(
        EXPECTED_LABELS
    )
