"""FastAPI application entry point."""

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.db.database import close_db, init_db

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan — init DB on startup, close on shutdown."""
    # Startup
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s | %(name)-30s | %(levelname)-7s | %(message)s",
    )
    logger.info("Starting Multi-Agent Productivity Assistant...")

    # Force Vertex AI runtime configuration for ADK / google-genai
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = (
        "true" if settings.google_genai_use_vertexai else "false"
    )

    if settings.google_cloud_project:
        os.environ["GOOGLE_CLOUD_PROJECT"] = settings.google_cloud_project

    if settings.google_cloud_region:
        os.environ["GOOGLE_CLOUD_LOCATION"] = settings.google_cloud_region
        os.environ["GOOGLE_CLOUD_REGION"] = settings.google_cloud_region

    logger.info(
        "Gemini runtime configured | vertexai=%s | project=%s | location=%s | model=%s",
        settings.google_genai_use_vertexai,
        settings.google_cloud_project,
        settings.google_cloud_region,
        settings.gemini_model,
    )

    # Keep backward compatibility only if API key is explicitly provided
    if settings.google_api_key:
        os.environ["GOOGLE_API_KEY"] = settings.google_api_key
        logger.info("GOOGLE_API_KEY detected and exported for compatibility")
    else:
        os.environ.pop("GOOGLE_API_KEY", None)

    # Initialise DB
    await init_db()
    logger.info("Database initialised.")

    yield

    # Shutdown
    await close_db()
    logger.info("Application shut down.")


app = FastAPI(
    title="Multi-Agent Productivity Assistant",
    description=(
        "A multi-agent AI system powered by Google ADK, MCP, AlloyDB, and Cloud Run "
        "that coordinates Google Calendar, Google Docs, and Gmail."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

# CORS for Cloud Shell web preview and local dev
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Register Routes ───────────────────────────────────────────
from app.api.routes.chat import router as chat_router

app.include_router(chat_router, prefix="/api", tags=["Chat"])


# ── Mount Static Frontend ─────────────────────────────────────
app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.get("/", tags=["UI"])
async def root():
    """Serve the frontend UI."""
    return FileResponse("app/static/index.html")


@app.get("/health", tags=["Health"])
async def health():
    """Detailed health check."""
    return {
        "status": "healthy",
        "database": settings.db_mode,
        "deployment": "cloud-run",
        "vertexai": settings.google_genai_use_vertexai,
        "project": settings.google_cloud_project,
        "location": settings.google_cloud_region,
        "model": settings.gemini_model,
    }