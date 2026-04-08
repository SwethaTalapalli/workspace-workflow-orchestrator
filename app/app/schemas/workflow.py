"""Pydantic schemas for workflow logs."""

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field


class WorkflowLogResponse(BaseModel):
    """A single workflow step log."""
    id: UUID
    session_id: Optional[UUID]
    workflow_name: Optional[str]
    agent_name: str
    action: str
    input_data: Optional[dict]
    output_data: Optional[dict]
    status: str
    error_message: Optional[str]
    started_at: datetime
    completed_at: Optional[datetime]

    class Config:
        from_attributes = True


class WorkflowStatusResponse(BaseModel):
    """Full workflow execution status."""
    session_id: str
    total_steps: int
    completed_steps: int
    status: str  # running | completed | failed
    logs: list[WorkflowLogResponse]
