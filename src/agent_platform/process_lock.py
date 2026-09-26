"""PostgreSQL advisory lock that allows one API process to run."""

import psycopg

# Session advisory lock held for the API process lifetime.
# Operators can see the holder in pg_locks:
#   locktype = 'advisory' AND classid = 1095914057 AND objid = 1214579201
# classid is the ASCII prefix AGRI; objid is HUB plus one.
API_PROCESS_ADVISORY_LOCK_CLASSID = 1_095_914_057
API_PROCESS_ADVISORY_LOCK_OBJID = 1_214_579_201
# Separate from the process lock so migrations can finish before the worker starts.
MIGRATION_ADVISORY_LOCK_CLASSID = 1_095_914_057
MIGRATION_ADVISORY_LOCK_OBJID = 1_214_579_202


class ApiProcessLock:
    """Keep one dedicated connection so the advisory lock survives the lifespan."""

    def __init__(self, database_uri: str) -> None:
        """Remember the database URL. The connection opens in ``acquire``."""
        self.database_uri = database_uri
        self._connection: psycopg.AsyncConnection | None = None

    async def acquire(self) -> None:
        """Take the process lock or fail when another worker already holds it."""
        connection = await psycopg.AsyncConnection.connect(
            self.database_uri,
            autocommit=True,
        )
        try:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    "SELECT pg_try_advisory_lock(%s, %s)",
                    (
                        API_PROCESS_ADVISORY_LOCK_CLASSID,
                        API_PROCESS_ADVISORY_LOCK_OBJID,
                    ),
                )
                row = await cursor.fetchone()
            if row is None or row[0] is not True:
                raise RuntimeError(
                    "another API process already holds the AgriHub run-manager "
                    "advisory lock "
                    f"(classid={API_PROCESS_ADVISORY_LOCK_CLASSID}, "
                    f"objid={API_PROCESS_ADVISORY_LOCK_OBJID}); "
                    "start exactly one worker. "
                    "uvicorn --workers 2 cannot run two independent RunManagers"
                )
        except Exception:
            await connection.close()
            raise
        self._connection = connection

    async def release(self) -> None:
        """Unlock and close the connection that owns the advisory lock."""
        connection = self._connection
        self._connection = None
        if connection is None:
            return
        try:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    "SELECT pg_advisory_unlock(%s, %s)",
                    (
                        API_PROCESS_ADVISORY_LOCK_CLASSID,
                        API_PROCESS_ADVISORY_LOCK_OBJID,
                    ),
                )
        finally:
            await connection.close()
