from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.orm import declarative_base
from google.cloud.alloydb.connector import Connector
import logging

from app.config import settings

logger = logging.getLogger(__name__)

Base = declarative_base()

_engine = None
_SessionLocal = None
connector = None


async def init_db():
    global _engine, _SessionLocal, connector

    if settings.db_mode == "sqlite":
        db_url = "sqlite+aiosqlite:///./productivity.db"
        _engine = create_async_engine(db_url, echo=False)
    else:
        logger.info("Using AlloyDB connector...")

        connector = Connector()

        async def getconn():
            conn = await connector.connect_async(
                settings.alloydb_instance_uri,
                "asyncpg",
                user=settings.alloydb_db_user,
                password=settings.alloydb_db_password,
                db=settings.alloydb_db_name,
            )
            return conn

        _engine = create_async_engine(
            "postgresql+asyncpg://",
            async_creator=getconn,
            pool_size=5,
            max_overflow=2,
        )

    _SessionLocal = async_sessionmaker(
        bind=_engine,
        expire_on_commit=False,
    )

    logger.info("Database initialized successfully")


async def close_db():
    global _engine, connector

    if _engine:
        await _engine.dispose()

    if connector:
        connector.close()


def get_session():
    return _SessionLocal()