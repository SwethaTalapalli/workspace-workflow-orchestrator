from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from google.cloud.alloydb.connector import Connector
import logging

from app.config import settings
from app.db.models import Base

logger = logging.getLogger(__name__)

_engine = None
_SessionLocal = None
connector = None


async def init_db():
    global _engine, _SessionLocal, connector

    logger.info(f"Initializing database in mode: {settings.db_mode}")

    if settings.db_mode == "sqlite":
        db_url = "sqlite+aiosqlite:///./productivity.db"
        logger.info(f"Using SQLite database: {db_url}")
        _engine = create_async_engine(db_url, echo=False)

    else:
        logger.info("Using AlloyDB connector...")

        if not settings.alloydb_instance_uri:
            raise RuntimeError("Missing AlloyDB configuration: ALLOYDB_INSTANCE_URI")

        connector = Connector()

        async def getconn():
            return await connector.connect_async(
                settings.alloydb_instance_uri,
                "asyncpg",
                user=settings.alloydb_db_user,
                password=settings.alloydb_db_password,
                db=settings.alloydb_db_name,
            )

        _engine = create_async_engine(
            "postgresql+asyncpg://",
            async_creator=getconn,
            pool_size=5,
            max_overflow=2,
            echo=False,
        )

    _SessionLocal = async_sessionmaker(
        bind=_engine,
        expire_on_commit=False,
    )

    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with _engine.connect() as conn:
        await conn.execute(text("SELECT 1"))

    logger.info("Database initialized successfully.")


async def close_db():
    global _engine, connector

    if _engine:
        await _engine.dispose()
        logger.info("Database engine disposed.")

    if connector:
        connector.close()
        logger.info("AlloyDB connector closed.")


def get_session():
    if _SessionLocal is None:
        raise RuntimeError("Database session is not initialized. Call init_db() first.")
    return _SessionLocal()