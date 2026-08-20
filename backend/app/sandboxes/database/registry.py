"""Database engine registry."""

from .contracts import DatabaseEngine, ErrorFactory
from .mysql import MySQLDatabaseEngine
from .postgres import PostgreSQLDatabaseEngine
from .sqlite import SQLiteDatabaseEngine

MYSQL_DATABASE: DatabaseEngine = MySQLDatabaseEngine()
POSTGRESQL_DATABASE: DatabaseEngine = PostgreSQLDatabaseEngine()
# Keep the shorter spelling available to callers that use the engine key.
POSTGRES_DATABASE = POSTGRESQL_DATABASE
SQLITE_DATABASE: DatabaseEngine = SQLiteDatabaseEngine()
DATABASE_ENGINES: dict[str, DatabaseEngine] = {
    "mysql": MYSQL_DATABASE,
    "postgres": POSTGRESQL_DATABASE,
    "sqlite": SQLITE_DATABASE,
}


def database_engine(engine: str, error: ErrorFactory) -> DatabaseEngine:
    """Resolve a confirmed engine name to its protocol implementation."""
    try:
        return DATABASE_ENGINES[engine]
    except KeyError as exc:
        raise error(422, f"Unsupported database engine: {engine}") from exc
