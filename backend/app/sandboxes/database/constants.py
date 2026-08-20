MYSQL_PORT = 3306
POSTGRES_PORT = 5432
SHARED_DATABASE_PREFIX = "orchestrator-shared-db-"
# This path is deliberately not below /workspace.  The workspace is the Git
# clone in every runtime, while this mount always holds sandbox-owned data.
SQLITE_DATA_MOUNT_PATH = "/var/lib/orchestrator/sqlite"
SQLITE_DATABASE_PATH = f"{SQLITE_DATA_MOUNT_PATH}/database.sqlite3"
SQLITE_HELPER_IMAGE = "alpine:3.21"
DEFAULT_MYSQL_IMAGE = "mysql:8.4"
DEFAULT_POSTGRES_IMAGE = "postgres:17"
DEFAULT_MIGRATION_IMAGE = "node:22-bookworm-slim"
# These administrative commands had no Docker timeout before ADR-0006.  They
# now fail after one minute instead of holding a request worker indefinitely.
DATABASE_COMMAND_TIMEOUT_SECONDS = 60
DATABASE_COMMAND_MAX_LOG_BYTES = 1_048_576
