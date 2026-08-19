"""The Docker label keys every package reads and writes.

They live here, not in a feature package, so a reader of a label does not
have to import the package that happens to create the container.
"""

LABEL_MANAGED = "orchestrator.preview.managed"
LABEL_DATA_MANAGED = "orchestrator.preview.data-managed"
LABEL_CONTROLLER_MANAGED = "orchestrator.managed"
LABEL_SANDBOX_ID = "orchestrator.sandbox.id"
LABEL_RUN_ID = "orchestrator.run.id"
LABEL_KIND = "orchestrator.kind"
LABEL_SERVICE = "orchestrator.preview.service"
LABEL_EXPIRES_AT = "orchestrator.preview.expires-at"
LABEL_PERSISTENT = "orchestrator.preview.persistent"
LABEL_PROJECT_ID = "orchestrator.project.id"
LABEL_SHARED_DATABASE = "orchestrator.shared-database"
LABEL_SHARED_DATABASE_IMAGE = "orchestrator.shared-database.image"
LABEL_PROJECT_SOURCE = "orchestrator.project.source"
