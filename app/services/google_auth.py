"""Google OAuth 2.0 credential management for Workspace APIs."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from app.config import settings

logger = logging.getLogger(__name__)

SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive.file",
]

_credentials: Credentials | None = None
_service_cache: Dict[str, object] = {}


# ✅ Resolve credentials file (Cloud Run + local)
def _resolve_credentials_file() -> str:
    cloud_path = "/secrets/creds/credentials.json"
    if os.path.exists(cloud_path):
        return cloud_path
    return settings.google_oauth_credentials_file


# ✅ Resolve token file (Cloud Run + local)
def _resolve_token_file() -> str:
    cloud_path = "/secrets/token/token.json"
    if os.path.exists(cloud_path):
        return cloud_path
    return settings.google_oauth_token_file


def _ensure_parent_dir(file_path: str) -> None:
    Path(file_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


def _load_saved_credentials(token_file: str) -> Credentials | None:
    token_path = Path(token_file).expanduser().resolve()
    if not token_path.exists():
        logger.info("Token file not found: %s", token_path)
        return None

    try:
        return Credentials.from_authorized_user_file(str(token_path), SCOPES)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("Failed to load token from %s: %s", token_path, exc)
        return None


def _save_credentials(creds: Credentials, token_file: str) -> None:
    # 🔥 Cloud Run secrets are read-only → skip writing
    if str(token_file).startswith("/secrets/"):
        logger.info("Skipping token save (read-only secret): %s", token_file)
        return

    _ensure_parent_dir(token_file)
    token_path = Path(token_file).expanduser().resolve()
    with token_path.open("w", encoding="utf-8") as handle:
        handle.write(creds.to_json())

    logger.info("OAuth token saved locally: %s", token_path)


def _load_oauth_config_from_env() -> Dict[str, Any] | None:
    raw = os.getenv("GOOGLE_OAUTH_CREDENTIALS_JSON", "").strip()
    if not raw:
        return None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("Invalid GOOGLE_OAUTH_CREDENTIALS_JSON") from exc

    return data if isinstance(data, dict) else None


def _build_flow() -> InstalledAppFlow:
    oauth_config = _load_oauth_config_from_env()

    if oauth_config:
        logger.info("Using OAuth config from env")
        return InstalledAppFlow.from_client_config(oauth_config, SCOPES)

    credentials_file = Path(_resolve_credentials_file()).expanduser().resolve()

    if not credentials_file.exists():
        raise FileNotFoundError(
            f"OAuth credentials not found at: {credentials_file}"
        )

    logger.info("Using credentials file: %s", credentials_file)

    return InstalledAppFlow.from_client_secrets_file(str(credentials_file), SCOPES)


def _run_manual_oauth_flow() -> Credentials:
    flow = _build_flow()
    flow.redirect_uri = "http://localhost"

    auth_url, _ = flow.authorization_url(
        prompt="consent",
        access_type="offline",
    )

    print("\nAuthorize here:\n", auth_url)

    code = input("\nEnter code: ").strip()
    flow.fetch_token(code=code)

    return flow.credentials


def get_credentials(force_refresh: bool = False) -> Credentials:
    global _credentials

    token_file = _resolve_token_file()

    if force_refresh:
        _credentials = None
        _service_cache.clear()

    if _credentials and _credentials.valid:
        return _credentials

    if _credentials is None:
        _credentials = _load_saved_credentials(token_file)

    if not _credentials or not _credentials.valid:
        if _credentials and _credentials.expired and _credentials.refresh_token:
            try:
                logger.info("Refreshing token")
                _credentials.refresh(Request())
            except Exception:
                logger.warning("Refresh failed → running OAuth flow")
                _credentials = _run_manual_oauth_flow()
        else:
            _credentials = _run_manual_oauth_flow()

        _save_credentials(_credentials, token_file)

    return _credentials


def _get_service(service_name: str, version: str):
    cache_key = f"{service_name}:{version}"

    if cache_key in _service_cache:
        return _service_cache[cache_key]

    creds = get_credentials()

    service = build(service_name, version, credentials=creds, cache_discovery=False)
    _service_cache[cache_key] = service

    return service


def get_calendar_service():
    return _get_service("calendar", "v3")


def get_docs_service():
    return _get_service("docs", "v1")


def get_gmail_service():
    return _get_service("gmail", "v1")


def get_drive_service():
    return _get_service("drive", "v3")