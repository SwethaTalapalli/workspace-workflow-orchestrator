"""Application configuration loaded from environment variables."""

import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic_settings import BaseSettings

# Load .env file
load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Application settings."""

    # ── Gemini / ADK / Vertex AI ──────────────────────────
    google_genai_use_vertexai: bool = (
        os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "true").lower() == "true"
    )
    google_cloud_project: str = os.getenv("GOOGLE_CLOUD_PROJECT", "")
    google_cloud_region: str = (
        os.getenv("GOOGLE_CLOUD_REGION")
        or os.getenv("GOOGLE_CLOUD_LOCATION")
        or "global"
    )

    # Keep for backward compatibility, but not required for Vertex AI flow
    google_api_key: str = os.getenv("GOOGLE_API_KEY", "")

    # ── Gemini Model ──────────────────────────────────────
    gemini_model: str = (
        os.getenv("MODEL")
        or os.getenv("GEMINI_MODEL")
        or "gemini-2.5-flash"
    )
    image_model: str = (
        os.getenv("IMAGE_MODEL")
        or "gemini-2.5-flash-image"
    )

    # ── Database Mode ─────────────────────────────────────
    db_mode: str = os.getenv("DB_MODE", "sqlite")

    # ── AlloyDB via Auth Proxy ────────────────────────────
    alloydb_instance_uri: str = os.getenv("ALLOYDB_INSTANCE_URI", "")
    alloydb_host: str = os.getenv("ALLOYDB_HOST", "127.0.0.1")
    alloydb_port: int = int(os.getenv("ALLOYDB_PORT", "5432"))
    alloydb_db_name: str = os.getenv("ALLOYDB_DB_NAME", "postgres")
    alloydb_db_user: str = os.getenv("ALLOYDB_DB_USER", "postgres")
    alloydb_db_password: str = os.getenv("ALLOYDB_DB_PASSWORD", "")

    # ── Google OAuth ──────────────────────────────────────
    google_oauth_credentials_file: str = str(BASE_DIR / "app" / "credentials.json")
    google_oauth_token_file: str = str(BASE_DIR / "app" / "token.json")

    # ── App ───────────────────────────────────────────────
    app_host: str = os.getenv("APP_HOST", "0.0.0.0")
    app_port: int = int(os.getenv("APP_PORT", "8080"))
    app_env: str = os.getenv("APP_ENV", "development")
    log_level: str = os.getenv("LOG_LEVEL", "INFO")

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"

    @property
    def is_sqlite(self) -> bool:
        return self.db_mode.lower() == "sqlite"

    @property
    def is_alloydb(self) -> bool:
        return self.db_mode.lower() == "alloydb"

    @property
    def sqlite_url(self) -> str:
        return f"sqlite+aiosqlite:///{BASE_DIR / 'productivity.db'}"

    @property
    def alloydb_url(self) -> str:
        """AlloyDB connection URL for Auth Proxy + asyncpg."""
        return (
            f"postgresql+asyncpg://{self.alloydb_db_user}:"
            f"{self.alloydb_db_password}@"
            f"{self.alloydb_host}:{self.alloydb_port}/"
            f"{self.alloydb_db_name}"
        )


settings = Settings()