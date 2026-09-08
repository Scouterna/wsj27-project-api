from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import asyncpg
from asyncpg import Connection, Pool, Record

from .config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)


class WSJ27ProjectError(Exception):
    """Raised for expected application-level errors, e.g. database connection failure."""


pg_pool: Pool | None = None


def db_configured() -> bool:
    """Whether a database is configured at all.

    An empty POSTGRES_DSN is a deliberate "run without a database" mode: the
    app starts, and main.py leaves out the only feature that needs one (the
    /cases router). Everything else works unchanged.
    """
    return bool(settings.POSTGRES_DSN)


async def connect_to_db() -> Pool | None:
    global pg_pool
    if not db_configured():
        logger.warning("POSTGRES_DSN is not set - starting without a database; /cases is disabled")
        return None
    if pg_pool is None:
        logger.info("Connecting to DB")
        try:
            pg_pool = await asyncpg.create_pool(
                dsn=settings.POSTGRES_DSN,
                max_inactive_connection_lifetime=60,
            )
        except ConnectionRefusedError:
            raise WSJ27ProjectError("Can't connect to database")
        logger.debug("Connected to DB")
    return pg_pool


async def close_db_connection() -> None:
    global pg_pool
    if pg_pool is not None:
        logger.info("Closing connection to DB")
        await pg_pool.close()
        pg_pool = None


async def db_execute(query: str, *args) -> None:
    async with pg_pool.acquire() as conn:
        logger.debug(f"PSQL: {query}")
        await conn.execute(query, *args)


async def db_fetch(query: str, *args) -> list[Record] | None:
    async with pg_pool.acquire() as conn:
        logger.debug(f"PSQL: {query}")
        return await conn.fetch(query, *args)


async def db_fetchrow(query: str, *args) -> Record | None:
    async with pg_pool.acquire() as conn:
        logger.debug(f"PSQL: {query}")
        return await conn.fetchrow(query, *args)


@asynccontextmanager
async def db_transaction() -> Connection:
    """Yield a connection with an open transaction, for callers that need several
    statements to commit atomically."""
    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            yield conn
