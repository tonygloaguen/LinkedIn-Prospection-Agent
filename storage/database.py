"""Database initialization and connection management."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import AsyncGenerator

import aiosqlite
import structlog

logger = structlog.get_logger(__name__)

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"
_db_lock = asyncio.Lock()


async def init_db(db_path: str) -> None:
    """Initialize the SQLite database, creating tables from schema.sql.

    Args:
        db_path: Filesystem path to the SQLite database file.
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    schema = _SCHEMA_PATH.read_text()

    async with aiosqlite.connect(db_path) as db:
        await db.executescript(schema)
        await db.commit()

    logger.info("database_initialized", db_path=db_path)


async def get_db(db_path: str) -> AsyncGenerator[aiosqlite.Connection, None]:
    """Async context manager yielding a database connection.

    Args:
        db_path: Filesystem path to the SQLite database file.

    Yields:
        An aiosqlite.Connection instance with row_factory set to aiosqlite.Row.
    """
    async with _db_lock:
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA foreign_keys=ON")
            yield db
