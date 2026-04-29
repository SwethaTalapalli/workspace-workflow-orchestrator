"""Chat route — user interacts with the orchestrator agent."""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, HTTPException
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from app.agents.orchestrator import orchestrator_agent
from app.schemas.chat import ChatRequest, ChatResponse
from app.services.executive_workflows import handle_premium_workflow

import os
from app.config import settings

# Force Vertex AI usage for Gemini
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "true"
os.environ["GOOGLE_CLOUD_PROJECT"] = settings.google_cloud_project
os.environ["GOOGLE_CLOUD_LOCATION"] = settings.google_cloud_region

logger = logging.getLogger(__name__)

router = APIRouter()

session_service = InMemorySessionService()
runner = Runner(
    agent=orchestrator_agent,
    app_name="productivity_assistant",
    session_service=session_service,
)

_active_sessions: dict[str, str] = {}


async def _get_or_create_session(session_id: str | None) -> tuple[str, object]:
    if session_id and session_id in _active_sessions:
        session = await session_service.get_session(
            app_name="productivity_assistant",
            user_id="default_user",
            session_id=_active_sessions[session_id],
        )
        if session:
            return session_id, session

    new_id = session_id or str(uuid.uuid4())
    session = await session_service.create_session(
        app_name="productivity_assistant",
        user_id="default_user",
    )
    _active_sessions[new_id] = session.id
    return new_id, session


def _extract_text_from_event(event) -> str:
    text_chunks: list[str] = []
    if getattr(event, "content", None) and getattr(event.content, "parts", None):
        for part in event.content.parts:
            if getattr(part, "text", None):
                text_chunks.append(part.text)
    return "".join(text_chunks).strip()


def _normalize_message_for_workflow(message: str) -> str:
    return " ".join((message or "").strip().split())


def _is_greeting(message: str) -> bool:
    normalized = (message or "").strip().lower()
    return normalized in {
        "hi",
        "hello",
        "hey",
        "hii",
        "helo",
        "good morning",
        "good afternoon",
        "good evening",
    }


def _is_vague_workspace_request(message: str) -> bool:
    normalized = " ".join((message or "").strip().lower().split())
    vague_patterns = {
        "help",
        "what can you do",
        "what all can you do",
        "show capabilities",
        "capabilities",
        "assist me",
        "can you help me",
        "do something",
        "do something with my workspace",
        "help with my workspace",
        "manage my workspace",
    }
    return normalized in vague_patterns


def _looks_out_of_scope(message: str) -> bool:
    normalized = " ".join((message or "").strip().lower().split())

    workspace_keywords = [
        "calendar",
        "meeting",
        "meetings",
        "schedule",
        "reschedule",
        "cancel",
        "availability",
        "free",
        "busy",
        "event",
        "events",
        "gmail",
        "email",
        "emails",
        "inbox",
        "unread",
        "document",
        "doc",
        "docs",
        "agenda",
        "briefing",
        "workspace",
    ]

    general_knowledge_starters = [
        "who is",
        "what is",
        "where is",
        "when is",
        "why is",
        "tell me about",
        "explain",
    ]

    if any(keyword in normalized for keyword in workspace_keywords):
        return False

    return any(normalized.startswith(starter) for starter in general_knowledge_starters)


def _greeting_response() -> str:
    return (
        "Hello! I’m your Workspace Workflow Orchestrator.\n\n"
        "I can help you manage meetings, emails, and documents across Google Calendar, Gmail, and Docs.\n\n"
        "Try asking:\n"
        "• “Give me my morning briefing”\n"
        "• “I prefer meetings after 3 PM”\n"
        "• “Check my inbox, prioritize important emails, and auto-schedule follow-ups”\n"
        "• “Create a document with meeting notes”\n\n"
        "What would you like me to do?"
    )


def _guided_workspace_response() -> str:
    return (
        "I can help with specific tasks across your Calendar, Gmail, and Docs.\n\n"
        "For example, you can ask me to:\n"
        "• Schedule or reschedule a meeting\n"
        "• Check your unread emails\n"
        "• Create a document or meeting agenda\n"
        "• Save scheduling preferences and prioritize important emails\n\n"
        "What would you like me to do?"
    )


def _out_of_scope_response() -> str:
    return (
        "I’m designed to help with productivity workflows like managing meetings, emails, and documents.\n\n"
        "Here are a few things you can try:\n"
        "• “Give me my morning briefing”\n"
        "• “I prefer meetings after 3 PM”\n"
        "• “Check my unread emails”\n"
        "• “Check my inbox, prioritize important emails, and auto-schedule follow-ups”\n\n"
        "What would you like me to do?"
    )


@router.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    try:
        session_id, session = await _get_or_create_session(request.session_id)

        normalized_message = _normalize_message_for_workflow(request.message)

        if _is_greeting(normalized_message):
            return ChatResponse(session_id=session_id, response=_greeting_response(), workflow_steps=[])

        if _is_vague_workspace_request(normalized_message):
            return ChatResponse(session_id=session_id, response=_guided_workspace_response(), workflow_steps=[])

        if _looks_out_of_scope(normalized_message):
            return ChatResponse(session_id=session_id, response=_out_of_scope_response(), workflow_steps=[])

        premium = await handle_premium_workflow(
            message=normalized_message,
            session_id=session_id,
            user_id="default_user",
        )
        if premium.handled:
            return ChatResponse(
                session_id=session_id,
                response=premium.response,
                workflow_steps=premium.workflow_steps,
            )

        user_content = types.Content(role="user", parts=[types.Part(text=request.message)])

        response_candidates: list[str] = []
        workflow_steps: list[dict] = []

        async for event in runner.run_async(
            user_id="default_user",
            session_id=session.id,
            new_message=user_content,
        ):
            text = _extract_text_from_event(event)
            if text:
                response_candidates.append(text)

            author = getattr(event, "author", "orchestrator")
            if getattr(event, "actions", None) and getattr(event.actions, "escalate", None):
                workflow_steps.append({"agent": author, "action": "escalate"})

            if getattr(event, "branch", None):
                workflow_steps.append({"agent": author, "action": "branch"})

        response_text = ""
        for candidate in reversed(response_candidates):
            if candidate and "no additional output" not in candidate.lower():
                response_text = candidate
                break

        if not response_text:
            response_text = (
                "I understood your request, but I could not produce a reliable final answer yet. "
                "Try one of these demo-ready commands:\n"
                "- Give me my morning briefing\n"
                "- I prefer meetings after 3 PM\n"
                "- Check my unread emails\n"
                "- Check my inbox, prioritize important emails, and auto-schedule follow-ups\n"
                "- Schedule a Project Sync meeting this week with alice@example.com"
            )

        return ChatResponse(
            session_id=session_id,
            response=response_text,
            workflow_steps=workflow_steps,
        )

    except Exception as exc:
        logger.exception("Error processing chat request")

        fallback_session_id = request.session_id or str(uuid.uuid4())

        fallback_response = (
            "Status: Failed\n\n"
            "Summary: Something went wrong while processing your request.\n\n"
            "Details: The system encountered an unexpected error.\n\n"
            "Next step: Please try again or rephrase your request.\n\n"
            f"Execution Trace:\n"
            f"1. orchestrator.chat → failed ({str(exc)})"
        )

        return ChatResponse(
            session_id=fallback_session_id,
            response=fallback_response,
            workflow_steps=[
                {
                    "agent": "orchestrator",
                    "action": "chat",
                    "status": "failed",
                    "reason": str(exc),
                }
            ],
        )


@router.get("/chat/{session_id}")
async def get_session_history(session_id: str):
    if session_id not in _active_sessions:
        raise HTTPException(status_code=404, detail="Session not found")

    adk_session_id = _active_sessions[session_id]
    session = await session_service.get_session(
        app_name="productivity_assistant",
        user_id="default_user",
        session_id=adk_session_id,
    )
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    messages = []
    for event in session.events:
        text = _extract_text_from_event(event)
        if text:
            messages.append(
                {
                    "role": event.content.role,
                    "text": text,
                    "author": getattr(event, "author", "unknown"),
                }
            )

    return {
        "session_id": session_id,
        "message_count": len(messages),
        "messages": messages,
    }