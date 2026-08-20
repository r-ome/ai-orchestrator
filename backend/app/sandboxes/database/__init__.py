"""Database-engine protocol and the existing MySQL implementation.

This module is intentionally small.  Preview orchestration still chooses when a
database starts or stops; an engine owns the database-specific container,
connection, migration, and administrative-SQL details.
"""

from ._engine_ops import (
    _ensure_sqlite_volume as _ensure_sqlite_volume,
)
from ._engine_ops import (
    _read_or_create_server_credentials as _read_or_create_server_credentials,
)
from ._engine_ops import (
    _run_database_command as _run_database_command,
)
from .constants import (
    DEFAULT_MIGRATION_IMAGE as DEFAULT_MIGRATION_IMAGE,
)
from .constants import (
    DEFAULT_MYSQL_IMAGE as DEFAULT_MYSQL_IMAGE,
)
from .constants import (
    DEFAULT_POSTGRES_IMAGE as DEFAULT_POSTGRES_IMAGE,
)
from .constants import (
    SHARED_DATABASE_PREFIX as SHARED_DATABASE_PREFIX,
)
from .constants import (
    SQLITE_DATA_MOUNT_PATH as SQLITE_DATA_MOUNT_PATH,
)
from .constants import (
    SQLITE_DATABASE_PATH as SQLITE_DATABASE_PATH,
)
from .constants import (
    SQLITE_HELPER_IMAGE as SQLITE_HELPER_IMAGE,
)
from .contracts import (
    DatabaseConnectionRequest as DatabaseConnectionRequest,
)
from .contracts import (
    DatabaseDropRequest as DatabaseDropRequest,
)
from .contracts import (
    DatabaseEngine as DatabaseEngine,
)
from .contracts import (
    DatabaseMigrationRequest as DatabaseMigrationRequest,
)
from .contracts import (
    DatabaseProvision as DatabaseProvision,
)
from .contracts import (
    DatabaseProvisionRequest as DatabaseProvisionRequest,
)
from .contracts import (
    DatabaseSchemaProvisionRequest as DatabaseSchemaProvisionRequest,
)
from .contracts import (
    ErrorFactory as ErrorFactory,
)
from .contracts import (
    ProvisionRequest as ProvisionRequest,
)
from .contracts import (
    SandboxDatabaseRuntime as SandboxDatabaseRuntime,
)
from .contracts import (
    sqlite_data_volume as sqlite_data_volume,
)
from .errors import (
    SandboxDatabaseError as SandboxDatabaseError,
)
from .errors import (
    SandboxMigrationError as SandboxMigrationError,
)
from .mysql import MySQLDatabaseEngine as MySQLDatabaseEngine
from .postgres import PostgreSQLDatabaseEngine as PostgreSQLDatabaseEngine
from .provisioning import (
    _connect_database_endpoint as _connect_database_endpoint,
)
from .provisioning import (
    _database_image as _database_image,
)
from .provisioning import (
    _drop_statements as _drop_statements,
)
from .provisioning import (
    _ensure_owned_sqlite_volume as _ensure_owned_sqlite_volume,
)
from .provisioning import (
    _ensure_shared_server as _ensure_shared_server,
)
from .provisioning import (
    _owned_sandbox_network as _owned_sandbox_network,
)
from .provisioning import (
    _provision_statements as _provision_statements,
)
from .provisioning import (
    _run_sandbox_migrations as _run_sandbox_migrations,
)
from .provisioning import (
    _sandbox_database_url as _sandbox_database_url,
)
from .provisioning import (
    _shared_resource as _shared_resource,
)
from .provisioning import (
    _SharedServer as _SharedServer,
)
from .provisioning import (
    _wait_for_server_health as _wait_for_server_health,
)
from .provisioning import (
    drop_sandbox_database as drop_sandbox_database,
)
from .provisioning import (
    provision_sandbox_database as provision_sandbox_database,
)
from .provisioning import (
    sandbox_database_runtime as sandbox_database_runtime,
)
from .registry import (
    DATABASE_ENGINES as DATABASE_ENGINES,
)
from .registry import (
    MYSQL_DATABASE as MYSQL_DATABASE,
)
from .registry import (
    POSTGRES_DATABASE as POSTGRES_DATABASE,
)
from .registry import (
    POSTGRESQL_DATABASE as POSTGRESQL_DATABASE,
)
from .registry import (
    SQLITE_DATABASE as SQLITE_DATABASE,
)
from .registry import (
    database_engine as database_engine,
)
from .shared import (
    _shared_database_locks as _shared_database_locks,
)
from .shared import (
    _shared_database_locks_guard as _shared_database_locks_guard,
)
from .shared import (
    mysql_identifier as mysql_identifier,
)
from .shared import (
    mysql_shared_database_names as mysql_shared_database_names,
)
from .shared import (
    mysql_shared_schema_name as mysql_shared_schema_name,
)
from .shared import (
    mysql_shared_user_name as mysql_shared_user_name,
)
from .shared import (
    postgres_drop_statements as postgres_drop_statements,
)
from .shared import (
    postgres_identifier as postgres_identifier,
)
from .shared import (
    postgres_provision_statements as postgres_provision_statements,
)
from .shared import (
    postgres_shared_database_name as postgres_shared_database_name,
)
from .shared import (
    postgres_shared_database_names as postgres_shared_database_names,
)
from .shared import (
    postgres_shared_role_name as postgres_shared_role_name,
)
from .shared import (
    schema_baseline_hash as schema_baseline_hash,
)
from .shared import (
    shared_database_names as shared_database_names,
)
from .shared import (
    shared_database_server_lock as shared_database_server_lock,
)
from .shared import (
    wait_for_mysql_health as wait_for_mysql_health,
)
from .sqlite import SQLiteDatabaseEngine as SQLiteDatabaseEngine

globals().pop("_engine_ops", None)
globals().pop("constants", None)
globals().pop("contracts", None)
globals().pop("errors", None)
globals().pop("mysql", None)
globals().pop("postgres", None)
globals().pop("provisioning", None)
globals().pop("registry", None)
globals().pop("shared", None)
globals().pop("sqlite", None)
