from __future__ import annotations

from typing import Any, Optional
import uuid

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1)
    session_id: Optional[str] = None


class ChatResponse(BaseModel):
    session_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    response: str
    workflow_steps: list[dict[str, Any]] = Field(default_factory=list)