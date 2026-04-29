"""High-value deterministic workflows for premium demo reliability."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.db.database import get_session
from app.db.models import UserPreference
from app.mcp.calendar_server import (
    create_event,
    delete_event_by_title,
    get_event_by_title,
    list_events,
    update_event_time,
)
from app.mcp.docs_server import create_document
from app.mcp.gmail_server import draft_email, read_emails, send_email

DEFAULT_TIMEZONE = "Asia/Kolkata"
DEFAULT_USER_ID = "default_user"
IST = ZoneInfo(DEFAULT_TIMEZONE)
DEFAULT_MEETING_DURATION = 60
WORKDAY_START_HOUR = 9
WORKDAY_END_HOUR = 18

logger = logging.getLogger(__name__)
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")

_SESSION_CONTEXTS: dict[str, "ConversationContext"] = {}


@dataclass
class WorkflowResult:
    handled: bool
    response: str
    workflow_steps: list[dict]


@dataclass
class ParsedMeeting:
    start_dt: datetime
    duration_minutes: int
    title: str
    attendees: list[str]


@dataclass
class ParsedEmailRequest:
    to: list[str]
    cc: list[str]
    bcc: list[str]
    subject: str
    body: str


@dataclass
class PlannedTask:
    title: str
    description: str
    due_dt: datetime
    day_index: int


@dataclass
class ConversationContext:
    last_intent: str = ""
    last_action: str = ""
    last_event_id: str = ""
    last_event_title: str = ""
    last_event_start: str = ""
    last_event_duration_minutes: int = 30
    last_event_attendees: list[str] = field(default_factory=list)
    last_event_html_link: str = ""
    last_doc_title: str = ""
    last_doc_url: str = ""
    pending_confirmation: dict[str, Any] = field(default_factory=dict)


def _get_context(session_id: Optional[str]) -> ConversationContext:
    sid = (session_id or "default").strip() or "default"
    if sid not in _SESSION_CONTEXTS:
        _SESSION_CONTEXTS[sid] = ConversationContext()
    return _SESSION_CONTEXTS[sid]


def _local_now() -> datetime:
    return datetime.now(IST)


def _extract_json(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return {"status": "error", "message": str(raw)}


def _extract_emails(text: str) -> list[str]:
    return list(dict.fromkeys(EMAIL_RE.findall(text or "")))


def _append_event_link(body: str, event_link: str) -> str:
    if not event_link:
        return body
    return f"{body}\n\nCalendar event link: {event_link}"


def _append_doc_link(body: str, doc_title: str, doc_url: str) -> str:
    if not doc_url:
        return body
    safe_title = doc_title or "Agenda Document"
    return f"{body}\n\nAgenda document: {safe_title}\nLink: {doc_url}"


def _extract_event_link_from_steps(steps: list[dict]) -> str:
    for step in steps:
        if step.get("html_link"):
            return step.get("html_link")
    return ""


def _extract_doc_info_from_steps(steps: list[dict]) -> tuple[str, str]:
    for step in steps:
        if step.get("agent") == "docs_agent" and step.get("action") == "create_document":
            return step.get("title", ""), step.get("url", "")
    return "", ""


def _format_time(dt: datetime) -> str:
    return dt.astimezone(IST).strftime("%Y-%m-%d %I:%M %p IST")


def _day_range(day_offset: int = 0) -> tuple[str, str]:
    now = _local_now() + timedelta(days=day_offset)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = now.replace(hour=23, minute=59, second=59, microsecond=999999)
    return start.isoformat(), end.isoformat()


def _format_event_lines(events: list[dict]) -> list[str]:
    lines = []
    for event in events:
        start = event.get("start", "Unknown time")
        end = event.get("end", "Unknown time")
        summary = event.get("summary", "(No title)")
        lines.append(f"- {summary} | {start} → {end}")
    return lines


def _format_email_lines(emails: list[dict]) -> list[str]:
    lines = []
    for index, email in enumerate(emails, start=1):
        subject = email.get("subject", "(No subject)")
        sender = email.get("from", "")
        snippet = email.get("snippet", "")
        snippet = snippet[:140] + ("..." if len(snippet) > 140 else "")
        lines.append(f"{index}. {subject}\n   From: {sender}\n   Summary: {snippet}")
    return lines


def _join_or_none(items: list[str]) -> str:
    clean = [item for item in (items or []) if item]
    return ", ".join(clean) if clean else "None"


def _section(title: str) -> str:
    return f"{title}\n" + "-" * len(title)


def _format_meeting_response(
    *,
    status_title: str,
    meeting_title: str,
    start_dt: datetime,
    duration_minutes: int,
    attendees: Optional[list[str]] = None,
    event_link: str = "",
    extra_lines: Optional[list[str]] = None,
) -> str:
    lines = [
        status_title,
        "",
        f"Title: {meeting_title}",
        f"Time: {_format_time(start_dt)}",
        f"Duration: {duration_minutes} minutes",
        f"Attendees: {_join_or_none(attendees or [])}",
    ]
    if event_link:
        lines.append(f"Event link: {event_link}")
    if extra_lines:
        lines.append("")
        lines.extend(extra_lines)
    return "\n".join(lines)


def _format_email_action_response(
    *,
    action_label: str,
    to: list[str],
    cc: list[str],
    bcc: list[str],
    subject: str,
    body: str,
) -> str:
    preview = (body or "").strip()
    if len(preview) > 500:
        preview = preview[:500].rstrip() + "..."

    lines = [
        action_label,
        "",
        f"To: {_join_or_none(to)}",
    ]
    if cc:
        lines.append(f"CC: {_join_or_none(cc)}")
    if bcc:
        lines.append(f"BCC: {_join_or_none(bcc)}")
    lines.extend(
        [
            f"Subject: {subject}",
            "Body preview:",
            preview or "(empty)",
        ]
    )
    return "\n".join(lines)


def _format_unread_email_response(emails: list[dict], max_results: int) -> str:
    lines = [f"Here are your top {len(emails)} unread emails.", ""]
    lines.extend(_format_email_lines(emails))
    lines.append("")
    if len(emails) >= max_results:
        lines.append("Showing the most relevant emails for quick review. Ask me to show more if needed.")
    else:
        lines.append("These are the unread emails currently available for quick review.")
    return "\n".join(lines)


def _format_daily_briefing_response(events: list[dict], emails: list[dict]) -> str:
    response_parts = [f"Morning Briefing ({DEFAULT_TIMEZONE})"]

    response_parts.append("")
    response_parts.append(_section("Calendar"))
    if events:
        response_parts.extend(_format_event_lines(events))
    else:
        response_parts.append("- No meetings scheduled today.")

    response_parts.append("")
    response_parts.append(_section("Unread Emails"))
    if emails:
        response_parts.extend(_format_email_lines(emails))
    else:
        response_parts.append("- No unread emails.")

    priorities: list[str] = []
    if events:
        priorities.append(f'Prepare for: {events[0].get("summary", "(No title)")}.')
    if emails:
        priorities.append("Review unread emails that look time-sensitive.")
    if not priorities:
        priorities.append("You have a relatively clear day.")

    response_parts.append("")
    response_parts.append(_section("Suggested Priorities"))
    response_parts.extend([f"- {item}" for item in priorities[:3]])

    return "\n".join(response_parts)


def _remember_scheduled_meeting(
    context: ConversationContext,
    *,
    title: str,
    start_dt: datetime,
    duration_minutes: int,
    attendees: list[str],
    html_link: str,
    event_id: str,
) -> None:
    context.last_intent = "schedule_meeting"
    context.last_action = "meeting_scheduled"
    context.last_event_title = title or ""
    context.last_event_start = start_dt.isoformat()
    context.last_event_duration_minutes = int(duration_minutes or 30)
    context.last_event_attendees = list(attendees or [])
    context.last_event_html_link = html_link or ""
    context.last_event_id = event_id or ""


def _remember_document(
    context: ConversationContext,
    *,
    title: str,
    url: str,
) -> None:
    context.last_doc_title = title or ""
    context.last_doc_url = url or ""


def _parse_event_datetime(raw_value: str) -> Optional[datetime]:
    if not raw_value:
        return None
    try:
        return datetime.fromisoformat(raw_value).astimezone(IST)
    except Exception:
        return None


def _compute_free_slots_for_day(
    requested_start: datetime,
    duration_minutes: int = DEFAULT_MEETING_DURATION,
) -> list[datetime]:
    """
    Suggest free slots for the same day.
    Preference-aware logic:
    1. Prefer slots after 3 PM
    2. Then fallback to normal business hours
    """
    try:
        from app.mcp.calendar_server import list_events

        day_start = requested_start.replace(hour=9, minute=0, second=0, microsecond=0)
        day_end = requested_start.replace(hour=18, minute=0, second=0, microsecond=0)

        raw = list_events(
            time_min=day_start.isoformat(),
            time_max=day_end.isoformat(),
            timezone_str=DEFAULT_TIMEZONE,
        )
        data = _extract_json(raw)
        events = data.get("events", []) if isinstance(data, dict) else []

        busy_ranges = []
        for event in events:
            start = event.get("start")
            end = event.get("end")
            if not start or not end:
                continue

            try:
                busy_start = datetime.fromisoformat(start.replace("Z", "+00:00"))
                busy_end = datetime.fromisoformat(end.replace("Z", "+00:00"))
                busy_ranges.append((busy_start, busy_end))
            except Exception:
                continue

        preferred_start = requested_start.replace(hour=15, minute=0, second=0, microsecond=0)

        candidate_slots = []

        # First preference: after 3 PM
        current = max(preferred_start, day_start)
        while current + timedelta(minutes=duration_minutes) <= day_end:
            candidate_slots.append(current)
            current += timedelta(minutes=60)

        # Fallback: full working day
        current = day_start
        while current + timedelta(minutes=duration_minutes) <= day_end:
            if current not in candidate_slots:
                candidate_slots.append(current)
            current += timedelta(minutes=60)

        free_slots = []
        for slot in candidate_slots:
            slot_end = slot + timedelta(minutes=duration_minutes)

            has_conflict = any(
                slot < busy_end and slot_end > busy_start
                for busy_start, busy_end in busy_ranges
            )

            if not has_conflict:
                free_slots.append(slot)

        return free_slots

    except Exception:
        logger.exception("Failed to compute free slots")
        return []


def _build_explicit_reschedule_message(message: str, context: ConversationContext) -> str:
    title = (context.last_event_title or "Meeting").strip() or "Meeting"
    compact_message = " ".join((message or "").strip().split())
    return f"reschedule meeting {title} to {compact_message}"


def _parse_duration(message: str) -> int:
    lower = message.lower()

    m = re.search(r"for\s+(\d+)\s*minutes?", lower)
    if m:
        return int(m.group(1))

    m = re.search(r"for\s+(\d+)\s*mins?", lower)
    if m:
        return int(m.group(1))

    m = re.search(r"for\s+(\d+)\s*hours?", lower)
    if m:
        return int(m.group(1)) * 60

    if "half an hour" in lower:
        return 30

    return 30


def _extract_time_from_message(message: str) -> Optional[tuple[int, int, str]]:
    patterns = [
        r"\b(?:at|to|for|by)\s+(\d{1,2})(?::(\d{2}))?\s*(AM|PM)\b",
        r"\b(\d{1,2})(?::(\d{2}))?\s*(AM|PM)\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, message, re.IGNORECASE)
        if match:
            hour = int(match.group(1))
            minute = int(match.group(2) or 0)
            ampm = match.group(3).upper()
            return hour, minute, ampm
    return None


def _contains_schedule_intent(message: str) -> bool:
    lower = message.lower()
    schedule_terms = ["schedule", "book", "set up", "setup", "create"]
    meeting_terms = ["meeting", "call", "session", "kickoff", "sync", "review", "discussion"]
    return any(term in lower for term in schedule_terms) and any(term in lower for term in meeting_terms)


def _extract_meeting_title(message: str, default_title: str = "Meeting") -> str:
    text = " ".join((message or "").strip().split())

    patterns = [
        r'(?i)\bcalled\s+"([^"]+)"',
        r"(?i)\bcalled\s+'([^']+)'",
        r'(?i)\bcalled\s+(.+?)(?=\s+(?:for|at|on|today|tomorrow|next|this)\b|$)',
        r'(?i)\b(?:schedule|set up|setup|book|create)\s+(?:a|an|the)?\s*((?:.+?)\s+meeting)(?=\s+(?:for|at|on|today|tomorrow|next|this)\b|$)',
        r'(?i)\b(?:schedule|set up|setup|book|create)\s+(?:a|an|the)?\s*((?:.+?)\s+call)(?=\s+(?:for|at|on|today|tomorrow|next|this)\b|$)',
        r'(?i)\b(?:schedule|set up|setup|book|create)\s+(?:a|an|the)?\s*((?:.+?)\s+session)(?=\s+(?:for|at|on|today|tomorrow|next|this)\b|$)',
        r'(?i)\b(?:schedule|set up|setup|book|create)\s+(?:a|an|the)?\s*((?:.+?)\s+sync)(?=\s+(?:for|at|on|today|tomorrow|next|this)\b|$)',
        r'(?i)\b(?:schedule|set up|setup|book|create)\s+(?:a|an|the)?\s*((?:.+?)\s+review)(?=\s+(?:for|at|on|today|tomorrow|next|this)\b|$)',
    ]

    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            title = match.group(1).strip().strip('"').strip("'").strip()
            if title:
                return title[0].upper() + title[1:]

    return default_title


def _resolve_next_weekday(base_date: datetime.date, weekday_index: int) -> datetime.date:
    days_ahead = (weekday_index - base_date.weekday()) % 7
    days_ahead = 7 if days_ahead == 0 else days_ahead
    return base_date + timedelta(days=days_ahead)


def _parse_datetime(
    message: str,
    base_date: Optional[datetime.date] = None,
    default_title: str = "Meeting",
) -> Optional[ParsedMeeting]:
    lower = message.lower()
    duration = _parse_duration(message)
    title = _extract_meeting_title(message, default_title=default_title)

    today = _local_now().date()

    if "day after tomorrow" in lower:
        date = today + timedelta(days=2)
    elif "tomorrow" in lower:
        date = today + timedelta(days=1)
    elif "today" in lower:
        date = today
    elif "next monday" in lower:
        date = _resolve_next_weekday(today, 0)
    elif "next tuesday" in lower:
        date = _resolve_next_weekday(today, 1)
    elif "next wednesday" in lower:
        date = _resolve_next_weekday(today, 2)
    elif "next thursday" in lower:
        date = _resolve_next_weekday(today, 3)
    elif "next friday" in lower:
        date = _resolve_next_weekday(today, 4)
    elif "next saturday" in lower:
        date = _resolve_next_weekday(today, 5)
    elif "next sunday" in lower:
        date = _resolve_next_weekday(today, 6)
    elif base_date is not None:
        date = base_date
    else:
        date = today

    extracted_time = _extract_time_from_message(message)
    if not extracted_time:
        return None

    hour, minute, ampm = extracted_time

    if ampm == "PM" and hour != 12:
        hour += 12
    if ampm == "AM" and hour == 12:
        hour = 0

    start_dt = datetime.combine(date, datetime.min.time()).replace(
        hour=hour,
        minute=minute,
        tzinfo=IST,
    )

    return ParsedMeeting(
        start_dt=start_dt,
        duration_minutes=duration,
        title=title,
        attendees=_extract_emails(message),
    )


def _build_email_body(title: str, start_dt: datetime, duration: int, tone: str, link: str) -> str:
    if tone == "join":
        body = (
            f"Hi,\n\n"
            f'I will join the meeting "{title}" scheduled for {_format_time(start_dt)}.\n\n'
            f"Duration: {duration} minutes.\n\n"
            f"Best regards"
        )
    elif tone == "cancel":
        body = (
            f"Hi,\n\n"
            f'The meeting "{title}" scheduled for {_format_time(start_dt)} has been cancelled.\n\n'
            f"Best regards"
        )
    elif tone == "reschedule":
        body = (
            f"Hi,\n\n"
            f'The meeting "{title}" has been rescheduled.\n'
            f"New time: {_format_time(start_dt)}\n\n"
            f"Best regards"
        )
    else:
        body = (
            f"Hi,\n\n"
            f'This is regarding our meeting "{title}".\n'
            f"Date: {_format_time(start_dt)}\n"
            f"Duration: {duration} minutes\n\n"
            f"Best regards"
        )

    return _append_event_link(body, link)


def _generate_agenda_body(meeting: ParsedMeeting) -> str:
    return (
        f"Meeting Title: {meeting.title}\n"
        f"Scheduled Time: {_format_time(meeting.start_dt)}\n"
        f"Duration: {meeting.duration_minutes} minutes\n\n"
        "Agenda\n"
        "1. Introductions\n"
        "2. Objectives and context\n"
        "3. Discussion points\n"
        "4. Decisions and ownership\n"
        "5. Next steps\n"
    )


def _build_agenda_doc_title(meeting: ParsedMeeting) -> str:
    return f"{meeting.title} - Agenda"


def _build_agenda_email_defaults(
    meeting: ParsedMeeting,
    event_link: str,
    doc_title: str,
    doc_url: str,
    *,
    mode: str,
) -> tuple[str, str]:
    if mode == "draft":
        subject = f"Agenda and Meeting Details: {meeting.title}"
    else:
        subject = f"Meeting Scheduled: {meeting.title}"

    body = (
        f"Hi,\n\n"
        f'The meeting "{meeting.title}" has been scheduled for {_format_time(meeting.start_dt)}.\n'
        f"Duration: {meeting.duration_minutes} minutes.\n\n"
        f"Please find the agenda document below.\n"
    )
    body = _append_doc_link(body, doc_title, doc_url)
    body = _append_event_link(body, event_link)
    body += "\n\nBest regards"
    return subject, body


def _event_to_meeting_info(event: dict) -> tuple[str, datetime, int, str, list[str], str]:
    title = event.get("summary", "Meeting")
    start_raw = event.get("start")
    end_raw = event.get("end")
    event_link = event.get("html_link", "")
    attendees = event.get("attendees", []) or []
    event_id = event.get("event_id", "")

    start_dt = datetime.fromisoformat(start_raw)
    end_dt = datetime.fromisoformat(end_raw)
    duration_minutes = int((end_dt - start_dt).total_seconds() // 60)

    return title, start_dt, duration_minutes, event_link, attendees, event_id


def _strip_action_suffixes(text: str) -> str:
    if not text:
        return ""

    cleaned = " ".join(text.strip().split())
    cleaned = re.sub(r'(?i)\s+and\s+notify\s+(?:all\s+)?attendees.*$', "", cleaned)
    cleaned = re.sub(r'(?i)\s+and\s+notify\s+(?:everyone|them).*$', "", cleaned)
    cleaned = re.sub(r'(?i)\s+and\s+send\s+email.*$', "", cleaned)
    cleaned = re.sub(r'(?i)\s+and\s+draft\s+email.*$', "", cleaned)
    cleaned = re.sub(r'(?i)\s+to\s+(?:today|tomorrow|next\s+\w+).*$' , "", cleaned)
    cleaned = re.sub(r'(?i)\s+at\s+\d{1,2}(?::\d{2})?\s*(?:AM|PM).*$' , "", cleaned)
    return cleaned.strip(" ,.;:-")


def _cleanup_extracted_title(title: str) -> str:
    if not title:
        return ""

    cleaned = _strip_action_suffixes(title)
    cleaned = cleaned.strip().strip('"').strip("'").strip()

    bad_values = {
        "",
        "meeting",
        "event",
        "and notify attendees",
        "notify attendees",
        "attendees",
    }
    if cleaned.lower() in bad_values:
        return ""

    return cleaned


def _find_title_for_reschedule_or_cancel(message: str) -> str:
    text = " ".join((message or "").strip().split())

    quoted_patterns = [
        r'(?i)(?:cancel|reschedule)\s+(?:meeting|event)\s+"([^"]+)"',
        r"(?i)(?:cancel|reschedule)\s+(?:meeting|event)\s+'([^']+)'",
        r'(?i)(?:meeting|event)\s+called\s+"([^"]+)"',
        r"(?i)(?:meeting|event)\s+called\s+'([^']+)'",
        r'(?i)(?:meeting|event)\s+"([^"]+)"',
        r"(?i)(?:meeting|event)\s+'([^']+)'",
    ]
    for pattern in quoted_patterns:
        match = re.search(pattern, text)
        if match:
            cleaned = _cleanup_extracted_title(match.group(1))
            if cleaned:
                return cleaned

    # Prefer preserving the full title when the user includes words like
    # "meeting", "call", "session", "sync", or "review" as part of the title.
    full_title_patterns = [
        r'(?i)^cancel\s+meeting\s+(.+?\bmeeting)\b(?=\s+and\s+notify\b|$)',
        r'(?i)^cancel\s+(.+?\bmeeting)\b(?=\s+and\s+notify\b|$)',
        r'(?i)^reschedule\s+(.+?\bmeeting)\b(?=\s+to\b|$)',
        r'(?i)^reschedule\s+(.+?\bcall)\b(?=\s+to\b|$)',
        r'(?i)^reschedule\s+(.+?\bsession)\b(?=\s+to\b|$)',
        r'(?i)^reschedule\s+(.+?\bsync)\b(?=\s+to\b|$)',
        r'(?i)^reschedule\s+(.+?\breview)\b(?=\s+to\b|$)',
    ]
    for pattern in full_title_patterns:
        match = re.search(pattern, text)
        if match:
            cleaned = _cleanup_extracted_title(match.group(1))
            if cleaned:
                return cleaned

    action_patterns = [
        r'(?i)^cancel\s+meeting\s+(.+?)(?=\s+and\s+notify\b|$)',
        r'(?i)^cancel\s+(.+?)(?=\s+meeting\s+and\s+notify\b|$)',
        r'(?i)^cancel\s+(.+?)(?=\s+and\s+notify\b|$)',
        r'(?i)^reschedule\s+meeting\s+(.+?)(?=\s+to\b|$)',
        r'(?i)^reschedule\s+(.+?)(?=\s+meeting\s+to\b|$)',
        r'(?i)^reschedule\s+(.+?)(?=\s+to\b|$)',
    ]
    for pattern in action_patterns:
        match = re.search(pattern, text)
        if match:
            cleaned = _cleanup_extracted_title(match.group(1))
            if cleaned:
                return cleaned

    fallback_patterns = [
        r'(?i)(?:meeting|event)\s+called\s+(.+?)(?=\s+(?:to|at|and)\b|$)',
        r'(?i)(?:meeting|event)\s+(.+?)(?=\s+(?:to|at|and)\b|$)',
    ]
    for pattern in fallback_patterns:
        match = re.search(pattern, text)
        if match:
            cleaned = _cleanup_extracted_title(match.group(1))
            if cleaned:
                return cleaned

    return ""


def _resolve_meeting_title_from_message_or_context(
    message: str,
    context: ConversationContext,
) -> str:
    explicit_title = _find_title_for_reschedule_or_cancel(message)
    if explicit_title:
        logger.info("Using explicit meeting title from message: %s", explicit_title)
        return explicit_title

    fallback_title = (context.last_event_title or "").strip()
    if fallback_title:
        logger.info("Falling back to last meeting title from context: %s", fallback_title)
        return fallback_title

    return ""


def _clean_recipient_value(value: str) -> str:
    if not value:
        return ""

    value = re.sub(r'(?i)\b(?:with\s+)?subject\s*(?::|=|\bas\b).*$', "", value)
    value = re.sub(r'(?i)\b(?:and\s+)?body\s*(?::|=|\bas\b).*$', "", value)
    value = re.sub(r'(?i)\b(?:and\s+)?message\s*(?::|=|\bas\b).*$', "", value)
    value = re.sub(r'(?i)\b(?:and\s+)?content\s*(?::|=|\bas\b).*$', "", value)
    value = re.sub(r'(?i)\b(?:and\s+)?please\b.*$', "", value)
    value = value.strip(" ,.;:-")
    return value.strip()


def _extract_subject(message: str) -> str:
    patterns = [
        r'(?is)\bsubject\s*:\s*"([^"]+)"',
        r"(?is)\bsubject\s*:\s*'([^']+)'",
        r'(?is)\bsubject\s+as\s+"([^"]+)"',
        r"(?is)\bsubject\s+as\s+'([^']+)'",
        r'(?is)\bsubject\s*=\s*"([^"]+)"',
        r"(?is)\bsubject\s*=\s*'([^']+)'",
        r"(?is)\bsubject\s+as\s+(.+?)(?=\s+\b(?:and\s+)?body\b|\s+\bbcc\b|\s+\bcc\b|$)",
        r"(?is)\bsubject\s*:\s*(.+?)(?=\s+\b(?:and\s+)?body\b|\s+\bbcc\b|\s+\bcc\b|$)",
        r"(?is)\bsubject\s*=\s*(.+?)(?=\s+\b(?:and\s+)?body\b|\s+\bbcc\b|\s+\bcc\b|$)",
    ]
    for pattern in patterns:
        match = re.search(pattern, message)
        if match:
            return match.group(1).strip().strip('"').strip("'").strip()
    return ""


def _extract_body(message: str) -> str:
    patterns = [
        r'(?is)\bbody\s*:\s*"([^"]+)"',
        r"(?is)\bbody\s*:\s*'([^']+)'",
        r'(?is)\bbody\s+as\s+"([^"]+)"',
        r"(?is)\bbody\s+as\s+'([^']+)'",
        r'(?is)\bbody\s*=\s*"([^"]+)"',
        r"(?is)\bbody\s*=\s*'([^']+)'",
        r'(?is)\bmessage\s*:\s*"([^"]+)"',
        r"(?is)\bmessage\s*:\s*'([^']+)'",
        r'(?is)\bmessage\s+as\s+"([^"]+)"',
        r"(?is)\bmessage\s+as\s+'([^']+)'",
        r'(?is)\bcontent\s*:\s*"([^"]+)"',
        r"(?is)\bcontent\s*:\s*'([^']+)'",
        r'(?is)\bcontent\s+as\s+"([^"]+)"',
        r"(?is)\bcontent\s+as\s+'([^']+)'",
        r"(?is)\bbody\s+as\s+(.+)$",
        r"(?is)\bbody\s*:\s*(.+)$",
        r"(?is)\bbody\s*=\s*(.+)$",
        r"(?is)\bmessage\s+as\s+(.+)$",
        r"(?is)\bmessage\s*:\s*(.+)$",
        r"(?is)\bcontent\s+as\s+(.+)$",
        r"(?is)\bcontent\s*:\s*(.+)$",
    ]
    for pattern in patterns:
        match = re.search(pattern, message)
        if match:
            return match.group(1).strip().strip('"').strip("'").strip()
    return ""


def _extract_recipients_from_label(message: str, label: str) -> list[str]:
    patterns = [
        rf"(?is)\b{label}\s*:\s*(.+?)(?=\b(?:to|cc|bcc|subject|body|message|content)\b\s*(?::|=|\bas\b)|$)",
        rf"(?is)\b{label}\b\s+(.+?)(?=\b(?:to|cc|bcc|subject|body|message|content)\b\s*(?::|=|\bas\b)|$)",
    ]
    for pattern in patterns:
        match = re.search(pattern, message)
        if match:
            candidate = _clean_recipient_value(match.group(1))
            emails = _extract_emails(candidate)
            if emails:
                return emails
    return []


def _parse_email_recipients(message: str) -> tuple[list[str], list[str], list[str]]:
    to_emails = _extract_recipients_from_label(message, "to")
    cc_emails = _extract_recipients_from_label(message, "cc")
    bcc_emails = _extract_recipients_from_label(message, "bcc")

    labeled_present = bool(to_emails or cc_emails or bcc_emails)
    if not labeled_present:
        to_emails = _extract_emails(message)

    seen: set[str] = set()
    final_to: list[str] = []
    for email in to_emails:
        if email not in seen:
            final_to.append(email)
            seen.add(email)

    final_cc: list[str] = []
    for email in cc_emails:
        if email not in seen:
            final_cc.append(email)
            seen.add(email)

    final_bcc: list[str] = []
    for email in bcc_emails:
        if email not in seen:
            final_bcc.append(email)
            seen.add(email)

    return final_to, final_cc, final_bcc


def _parse_email_subject_body(message: str, default_subject: str, default_body: str) -> tuple[str, str]:
    subject = _extract_subject(message) or default_subject
    body = _extract_body(message) or default_body
    return subject, body


def _build_current_email_request(
    message: str,
    default_subject: str,
    default_body: str,
    fallback_to: Optional[list[str]] = None,
) -> ParsedEmailRequest:
    to_emails, cc_emails, bcc_emails = _parse_email_recipients(message)
    if not to_emails and fallback_to:
        to_emails = list(dict.fromkeys([email for email in fallback_to if email]))
    subject, body = _parse_email_subject_body(message, default_subject, default_body)
    return ParsedEmailRequest(
        to=to_emails,
        cc=cc_emails,
        bcc=bcc_emails,
        subject=subject,
        body=body,
    )


def _safe_event_times(event: dict[str, Any]) -> tuple[Optional[datetime], Optional[datetime]]:
    try:
        start = datetime.fromisoformat(event["start"]).astimezone(IST)
        end = datetime.fromisoformat(event["end"]).astimezone(IST)
        return start, end
    except Exception:
        return None, None


async def _get_user_preference_dict(user_id: str = DEFAULT_USER_ID) -> dict[str, Any]:
    async with get_session() as session:
        result = await session.execute(
            select(UserPreference).where(UserPreference.user_id == user_id)
        )
        pref = result.scalar_one_or_none()

        if pref is None:
            pref = UserPreference(
                user_id=user_id,
                timezone=DEFAULT_TIMEZONE,
                working_hours={"start": "09:00", "end": "17:00"},
                preferences={
                    "preferred_meeting_start_hour": 15,
                    "max_meetings_per_day": 3,
                    "important_contacts": [],
                    "avoid_mornings": True,
                },
            )
            session.add(pref)
            await session.flush()

        return dict(pref.preferences or {})


async def _save_user_preferences(
    *,
    user_id: str,
    updates: dict[str, Any],
) -> dict[str, Any]:
    async with get_session() as session:
        result = await session.execute(
            select(UserPreference).where(UserPreference.user_id == user_id)
        )
        pref = result.scalar_one_or_none()

        if pref is None:
            pref = UserPreference(
                user_id=user_id,
                timezone=DEFAULT_TIMEZONE,
                working_hours={"start": "09:00", "end": "17:00"},
                preferences={},
            )
            session.add(pref)
            await session.flush()

        merged = dict(pref.preferences or {})
        merged.update(updates)
        pref.preferences = merged
        await session.flush()
        return merged


def _extract_important_contacts(message: str) -> list[str]:
    return _extract_emails(message)


async def update_user_intelligence_preferences(
    message: str,
    user_id: str = DEFAULT_USER_ID,
) -> WorkflowResult:
    lower = message.lower()

    updates: dict[str, Any] = {}
    response_parts: list[str] = []

    if "prefer meetings after 3 pm" in lower or "prefer meetings after 3pm" in lower or "meetings after 3 pm" in lower:
        updates["preferred_meeting_start_hour"] = 15
        updates["avoid_mornings"] = True
        response_parts.append("meeting preference set to after 3 PM")

    m = re.search(r"limit me to\s+(\d+)\s+meetings?\s+(?:a|per)\s+day", lower)
    if not m:
        m = re.search(r"(\d+)\s+meetings?\s+(?:a|per)\s+day", lower)
    if m:
        updates["max_meetings_per_day"] = int(m.group(1))
        response_parts.append(f"max meetings per day set to {m.group(1)}")

    if ("mark emails from" in lower or "important contact" in lower or "important emails from" in lower) and _extract_important_contacts(message):
        updates["important_contacts"] = _extract_important_contacts(message)
        response_parts.append("important contacts saved: " + ", ".join(updates["important_contacts"]))

    if not updates:
        return WorkflowResult(False, "", [])

    merged = await _save_user_preferences(user_id=user_id, updates=updates)

    return WorkflowResult(
        handled=True,
        response="User intelligence preferences updated successfully.\n" + "\n".join(f"- {item}" for item in response_parts),
        workflow_steps=[
            {
                "agent": "preference_agent",
                "action": "update_user_preferences",
                "preferences": merged,
            }
        ],
    )


def _meeting_keywords_present(text: str) -> bool:
    lower = (text or "").lower()
    keywords = [
        "meeting",
        "sync",
        "review",
        "discussion",
        "follow-up",
        "follow up",
        "call",
        "schedule",
        "catch up",
    ]
    return any(keyword in lower for keyword in keywords)


def _is_important_email(email: dict, important_contacts: list[str]) -> bool:
    sender = (email.get("from", "") or "").lower()
    subject = (email.get("subject", "") or "").lower()
    snippet = (email.get("snippet", "") or "").lower()

    if any(contact.lower() in sender for contact in important_contacts):
        return True

    priority_terms = ["urgent", "important", "asap", "follow-up", "follow up", "deadline", "review"]
    return any(term in subject or term in snippet for term in priority_terms)


async def _count_meetings_for_day(day_dt: datetime) -> int:
    start_of_day = day_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    end_of_day = day_dt.replace(hour=23, minute=59, second=59, microsecond=999999)
    raw = list_events(
        time_min=start_of_day.isoformat(),
        time_max=end_of_day.isoformat(),
        timezone_str=DEFAULT_TIMEZONE,
    )
    data = _extract_json(raw)
    return len(data.get("events", []))


async def _find_next_preferred_slot(
    *,
    duration_minutes: int,
    preferred_start_hour: int,
    max_meetings_per_day: int,
) -> Optional[datetime]:
    now = _local_now()

    for day_offset in range(0, 5):
        day_dt = (now + timedelta(days=day_offset)).replace(second=0, microsecond=0)

        meeting_count = await _count_meetings_for_day(day_dt)
        if meeting_count >= max_meetings_per_day:
            continue

        slots = _compute_free_slots_for_day(day_dt, duration_minutes)
        preferred_slots = [slot for slot in slots if slot.hour >= preferred_start_hour]

        if preferred_slots:
            return preferred_slots[0]

    return None


async def intelligent_inbox_workflow(
    message: str,
    session_id: Optional[str] = None,
    user_id: str = DEFAULT_USER_ID,
) -> WorkflowResult:
    lower = message.lower()

    trigger_phrases = [
        "handle important emails",
        "prioritize important emails",
        "check my inbox and handle important emails",
        "check my inbox and prioritize important emails",
        "summarize unread emails and suggest meetings",
        "auto-schedule follow-ups",
        "auto schedule follow ups",
        "auto-schedule follow ups",
        "auto schedule follow-ups",
    ]
    if not any(phrase in lower for phrase in trigger_phrases):
        return WorkflowResult(False, "", [])

    prefs = await _get_user_preference_dict(user_id=user_id)
    important_contacts = prefs.get("important_contacts", [])
    preferred_start_hour = int(prefs.get("preferred_meeting_start_hour", 15))
    max_meetings_per_day = int(prefs.get("max_meetings_per_day", 3))

    raw = read_emails(max_results=8, query="is:unread", label="INBOX")
    data = _extract_json(raw)
    emails = data.get("emails", [])

    if not emails:
        return WorkflowResult(
            handled=True,
            response="You have no unread emails to process.",
            workflow_steps=[{"agent": "gmail_agent", "action": "read_unread_emails", "count": 0}],
        )

    important_emails = [email for email in emails if _is_important_email(email, important_contacts)]
    if not important_emails:
        return WorkflowResult(
            handled=True,
            response="I checked your unread emails, but none matched your current importance rules.",
            workflow_steps=[{"agent": "gmail_agent", "action": "prioritize_unread_emails", "count": len(emails)}],
        )

    summary_lines = ["Important unread emails identified:"]
    suggested_followups: list[dict[str, Any]] = []

    for index, email in enumerate(important_emails[:5], start=1):
        subject = email.get("subject", "(No subject)")
        sender = email.get("from", "")
        snippet = (email.get("snippet", "") or "").strip()

        summary_lines.append(
            f"{index}. {subject}\n   From: {sender}\n   Summary: {snippet[:140]}"
        )

        if _meeting_keywords_present(subject + " " + snippet):
            suggested_followups.append(
                {
                    "subject": subject,
                    "sender": sender,
                    "duration_minutes": 30,
                }
            )

    workflow_steps = [
        {"agent": "gmail_agent", "action": "prioritize_unread_emails", "count": len(important_emails)},
    ]

    auto_schedule_requested = "auto-schedule" in lower or "auto schedule" in lower

    if suggested_followups:
        summary_lines.append("")
        summary_lines.append("Suggested meeting follow-ups:")
        for item in suggested_followups:
            summary_lines.append(f"- {item['subject']}")

        if auto_schedule_requested:
            created = 0
            for item in suggested_followups[:2]:
                slot = await _find_next_preferred_slot(
                    duration_minutes=item["duration_minutes"],
                    preferred_start_hour=preferred_start_hour,
                    max_meetings_per_day=max_meetings_per_day,
                )
                if not slot:
                    continue

                end_dt = slot + timedelta(minutes=item["duration_minutes"])
                title = f"Follow-up: {item['subject'][:60]}"
                attendees = _extract_emails(item["sender"])

                raw_create = create_event(
                    summary=title,
                    start_time=slot.isoformat(),
                    end_time=end_dt.isoformat(),
                    attendees=attendees,
                    timezone_str=DEFAULT_TIMEZONE,
                )
                create_data = _extract_json(raw_create)
                if create_data.get("status") == "created":
                    created += 1
                    workflow_steps.append(
                        {
                            "agent": "calendar_agent",
                            "action": "create_event",
                            "title": title,
                            "start": slot.isoformat(),
                        }
                    )

            if created:
                summary_lines.append("")
                summary_lines.append(
                    f"Automatically scheduled {created} follow-up meeting(s) using your saved preferences."
                )
            else:
                summary_lines.append("")
                summary_lines.append("I found follow-up candidates, but no suitable slots were available.")
    else:
        summary_lines.append("")
        summary_lines.append("No meeting follow-ups were suggested from the current unread emails.")

    return WorkflowResult(
        handled=True,
        response="\n".join(summary_lines),
        workflow_steps=workflow_steps,
    )


async def smart_schedule_from_preferences(
    message: str,
    session_id: Optional[str] = None,
    user_id: str = DEFAULT_USER_ID,
) -> WorkflowResult:
    lower = message.lower()

    if not (
        _contains_schedule_intent(message)
        and ("this week" in lower or "next available" in lower or "find a slot" in lower)
    ):
        return WorkflowResult(False, "", [])

    parsed_with_time = _parse_datetime(message)
    if parsed_with_time:
        return WorkflowResult(False, "", [])

    prefs = await _get_user_preference_dict(user_id=user_id)
    preferred_start_hour = int(prefs.get("preferred_meeting_start_hour", 15))
    max_meetings_per_day = int(prefs.get("max_meetings_per_day", 3))

    duration = _parse_duration(message)
    slot = await _find_next_preferred_slot(
        duration_minutes=duration,
        preferred_start_hour=preferred_start_hour,
        max_meetings_per_day=max_meetings_per_day,
    )
    if not slot:
        return WorkflowResult(
            handled=True,
            response="I couldn’t find a meeting slot that matches your saved preferences.",
            workflow_steps=[{"agent": "calendar_agent", "action": "find_preferred_slot_failed"}],
        )

    title = _extract_meeting_title(message, default_title="Meeting")
    attendees = _extract_emails(message)
    end_dt = slot + timedelta(minutes=duration)

    raw = create_event(
        summary=title,
        start_time=slot.isoformat(),
        end_time=end_dt.isoformat(),
        attendees=attendees,
        timezone_str=DEFAULT_TIMEZONE,
    )
    data = _extract_json(raw)

    if data.get("status") != "created":
        return WorkflowResult(
            handled=True,
            response="I found a preferred slot, but I couldn’t create the meeting.",
            workflow_steps=[{"agent": "calendar_agent", "action": "create_event_failed"}],
        )

    _remember_scheduled_meeting(
        _get_context(session_id),
        title=title,
        start_dt=slot,
        duration_minutes=duration,
        attendees=attendees,
        html_link=data.get("html_link", ""),
        event_id=data.get("event_id", ""),
    )

    return WorkflowResult(
        handled=True,
        response=_format_meeting_response(
            status_title="Meeting scheduled using your saved preferences.",
            meeting_title=title,
            start_dt=slot,
            duration_minutes=duration,
            attendees=attendees,
            event_link=data.get("html_link", ""),
            extra_lines=["Your preferred meeting rules were applied automatically."],
        ),
        workflow_steps=[
            {"agent": "preference_agent", "action": "apply_user_preferences"},
            {"agent": "calendar_agent", "action": "create_event", "title": title, "start": slot.isoformat()},
        ],
    )


async def show_events(day_offset: int, label: str) -> WorkflowResult:
    time_min, time_max = _day_range(day_offset)
    raw = list_events(time_min=time_min, time_max=time_max, timezone_str=DEFAULT_TIMEZONE)
    data = _extract_json(raw)
    events = data.get("events", [])

    if not events:
        return WorkflowResult(
            handled=True,
            response=f"No calendar events found for {label}.",
            workflow_steps=[{"agent": "calendar_agent", "action": f"list_events_{label}", "timezone": DEFAULT_TIMEZONE}],
        )

    lines = _format_event_lines(events)
    return WorkflowResult(
        handled=True,
        response=f"Calendar for {label} ({DEFAULT_TIMEZONE}):\n" + "\n".join(lines),
        workflow_steps=[
            {"agent": "calendar_agent", "action": f"list_events_{label}", "count": len(events), "timezone": DEFAULT_TIMEZONE}
        ],
    )


async def show_unread_emails(max_results: int = 5) -> WorkflowResult:
    raw = read_emails(max_results=max_results, query="is:unread", label="INBOX")
    data = _extract_json(raw)
    emails = data.get("emails", [])

    if not emails:
        return WorkflowResult(
            handled=True,
            response="You have no unread emails in your inbox.",
            workflow_steps=[{"agent": "gmail_agent", "action": "read_unread_emails", "count": 0}],
        )

    return WorkflowResult(
        handled=True,
        response=_format_unread_email_response(emails, max_results),
        workflow_steps=[{"agent": "gmail_agent", "action": "read_unread_emails", "count": len(emails)}],
    )


async def create_google_doc_from_prompt(message: str, session_id: Optional[str] = None) -> WorkflowResult:
    match = re.search(
        r"create a document called\s+(?P<title>.+?)\s+with content:\s*(?P<body>.+)$",
        message,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return WorkflowResult(False, "", [])

    title = match.group("title").strip().strip('"')
    body = match.group("body").strip()
    raw = create_document(title=title, body_content=body)
    data = _extract_json(raw)

    if data.get("status") == "error":
        return WorkflowResult(
            handled=True,
            response=f'Document creation failed: {data.get("message", "Unknown error")}',
            workflow_steps=[{"agent": "docs_agent", "action": "create_document_failed", "title": title}],
        )

    if session_id:
        _remember_document(_get_context(session_id), title=data.get("title", title), url=data.get("url", ""))

    return WorkflowResult(
        handled=True,
        response=f'Document created successfully.\nTitle: {data.get("title", title)}\nLink: {data.get("url", "")}',
        workflow_steps=[{"agent": "docs_agent", "action": "create_document", "title": title, "url": data.get("url", "")}],
    )


async def create_agenda_doc_for_meeting(
    meeting: ParsedMeeting,
    session_id: Optional[str] = None,
) -> WorkflowResult:
    title = _build_agenda_doc_title(meeting)
    body = _generate_agenda_body(meeting)

    raw = create_document(title=title, body_content=body)
    data = _extract_json(raw)

    if data.get("status") == "error":
        return WorkflowResult(
            handled=True,
            response=f'Agenda document creation failed: {data.get("message", "Unknown error")}',
            workflow_steps=[{"agent": "docs_agent", "action": "create_document_failed", "title": title}],
        )

    if session_id:
        _remember_document(_get_context(session_id), title=data.get("title", title), url=data.get("url", ""))

    return WorkflowResult(
        handled=True,
        response=f'Agenda document created successfully.\nTitle: {data.get("title", title)}\nLink: {data.get("url", "")}',
        workflow_steps=[
            {
                "agent": "docs_agent",
                "action": "create_document",
                "title": data.get("title", title),
                "url": data.get("url", ""),
            }
        ],
    )


def _is_confirmation_message(message: str) -> bool:
    """Return True when the user confirms a pending suggested action."""
    normalized = " ".join((message or "").lower().strip().split())
    return normalized in {
        "yes",
        "yes please",
        "confirm",
        "confirmed",
        "proceed",
        "go ahead",
        "schedule it",
        "ok",
        "okay",
        "sure",
    }


async def schedule_meeting(message: str, session_id: Optional[str] = None) -> WorkflowResult:
    try:
        if not _contains_schedule_intent(message):
            return WorkflowResult(False, "", [])

        parsed = _parse_datetime(message)
        if not parsed:
            return WorkflowResult(
                True,
                "Status: Failed\n\n"
                "Summary: I couldn’t schedule the meeting because the requested time was not clear.\n\n"
                "Next step: Please specify the date and time, for example: next Tuesday at 10 AM.",
                [
                    {
                        "agent": "calendar_agent",
                        "action": "parse_meeting_time",
                        "status": "failed",
                        "reason": "Requested time was unclear",
                    }
                ],
            )

        end_dt = parsed.start_dt + timedelta(minutes=parsed.duration_minutes)

        raw = create_event(
            summary=parsed.title,
            start_time=parsed.start_dt.isoformat(),
            end_time=end_dt.isoformat(),
            attendees=parsed.attendees,
            timezone_str=DEFAULT_TIMEZONE,
        )
        data = _extract_json(raw)

        if data.get("status") != "created":
            error_message = data.get("message", "")
            conflicting_events = data.get("conflicting_events", [])

            suggested_slots = _compute_free_slots_for_day(
                parsed.start_dt,
                parsed.duration_minutes,
            )
            suggested_slots = [slot for slot in suggested_slots if slot.hour >= 15] or suggested_slots
            suggested_slot = suggested_slots[0] if suggested_slots else None

            conflict_lines = []
            for event in conflicting_events[:3]:
                conflict_lines.append(
                    f"- {event.get('summary', '(No title)')} | "
                    f"{event.get('start', 'Unknown')} → {event.get('end', 'Unknown')}"
                )

            response_lines = [
                "Status: Conflict detected",
                "",
                f"Summary: The requested slot for \"{parsed.title}\" is unavailable.",
                "",
                "Requested time:",
                f"- {_format_time(parsed.start_dt)}",
            ]

            if conflict_lines:
                response_lines.extend(
                    [
                        "",
                        "Conflicting event:",
                        *conflict_lines,
                    ]
                )

            if suggested_slot:
                response_lines.extend(
                    [
                        "",
                        "Suggested alternative:",
                        f"- {_format_time(suggested_slot)}",
                        "",
                        "Next step: Please confirm if I should schedule it at the suggested time.",
                    ]
                )
            else:
                response_lines.extend(
                    [
                        "",
                        "Suggested alternative:",
                        "- No free slot found for the same working day.",
                        "",
                        "Next step: Please choose another day or wider time window.",
                    ]
                )

            if suggested_slot:
                context = _get_context(session_id)
                context.pending_confirmation = {
                    "type": "schedule_suggested_meeting",
                    "title": parsed.title,
                    "start": suggested_slot.isoformat(),
                    "duration_minutes": parsed.duration_minutes,
                    "attendees": parsed.attendees,

                    # 🔥 CRITICAL FIX (ONLY ADDITION)
                    "create_agenda": "agenda" in message.lower() or "document" in message.lower()
                }

            return WorkflowResult(
                True,
                "\n".join(response_lines),
                [
                    {
                        "agent": "calendar_agent",
                        "action": "check_availability",
                        "status": "failed",
                        "reason": error_message or "Time slot conflict detected",
                        "requested_start": parsed.start_dt.isoformat(),
                        "requested_end": end_dt.isoformat(),
                    },
                    {
                        "agent": "calendar_agent",
                        "action": "suggest_alternative_slot",
                        "status": "success" if suggested_slot else "failed",
                        "suggested_start": suggested_slot.isoformat() if suggested_slot else "",
                    },
                    {
                        "agent": "calendar_agent",
                        "action": "create_event",
                        "status": "skipped",
                        "reason": "Meeting creation paused due to conflict",
                    },
                ],
            )

        html_link = data.get("html_link", "")
        event_id = data.get("event_id", "")
        context = _get_context(session_id)

        _remember_scheduled_meeting(
            context,
            title=parsed.title,
            start_dt=parsed.start_dt,
            duration_minutes=parsed.duration_minutes,
            attendees=parsed.attendees,
            html_link=html_link,
            event_id=event_id,
        )

        # 🔥 NEW: Agenda handling for NON-CONFLICT flow
        agenda_steps = []
        agenda_title = ""
        agenda_url = ""

        if "agenda" in message.lower() or "document" in message.lower():
            meeting = ParsedMeeting(
                start_dt=parsed.start_dt,
                duration_minutes=parsed.duration_minutes,
                title=parsed.title,
                attendees=parsed.attendees,
            )

            agenda_result = await create_agenda_doc_for_meeting(
                meeting,
                session_id=session_id,
            )

            if agenda_result.workflow_steps:
                agenda_steps = agenda_result.workflow_steps
                agenda_title, agenda_url = _extract_doc_info_from_steps(agenda_steps)

        extra_lines = []
        if agenda_url:
            extra_lines.extend(
                [
                    "Agenda document:",
                    f"- Title: {agenda_title}",
                    f"- Link: {agenda_url}",
                ]
            )

        return WorkflowResult(
            True,
            _format_meeting_response(
                status_title="Status: Completed\n\nSummary: Meeting scheduled successfully.",
                meeting_title=parsed.title,
                start_dt=parsed.start_dt,
                duration_minutes=parsed.duration_minutes,
                attendees=parsed.attendees,
                event_link=html_link,
                extra_lines=extra_lines,   # ✅ ONLY ADDITION
            ),
            [
                {
                    "agent": "calendar_agent",
                    "action": "check_availability",
                    "status": "success",
                    "requested_start": parsed.start_dt.isoformat(),
                    "requested_end": end_dt.isoformat(),
                },
                {
                    "agent": "calendar_agent",
                    "action": "create_event",
                    "status": "success",
                    "event_id": event_id,
                    "html_link": html_link,
                    "title": parsed.title,
                    "start": parsed.start_dt.isoformat(),
                    "duration": parsed.duration_minutes,
                    "attendees": parsed.attendees,
                },
            ] + agenda_steps,   # ✅ ONLY ADDITION
        )
    except Exception as exc:
        logger.exception("schedule_meeting failed")
        return WorkflowResult(
            True,
            "Status: Failed\n\n"
            "Summary: I couldn’t schedule the meeting due to an internal error.\n\n"
            f"Details: {exc}",
            [
                {
                    "agent": "calendar_agent",
                    "action": "schedule_meeting",
                    "status": "failed",
                    "reason": str(exc),
                }
            ],
    )


async def handle_pending_confirmation(
    message: str,
    session_id: Optional[str] = None,
) -> WorkflowResult:
    """Handle a follow-up confirmation such as yes after suggesting an alternate slot."""
    if not session_id or not _is_confirmation_message(message):
        return WorkflowResult(False, "", [])

    context = _get_context(session_id)
    pending = context.pending_confirmation or {}

    if pending.get("type") == "reschedule_no_slot":
        context.pending_confirmation = {}
        title = pending.get("title") or context.last_event_title or "the meeting"
        return WorkflowResult(
            True,
            f"Status: No available slot\n\nSummary: I could not find a free slot for {title} at or after the requested time.\n\nNext step: Please provide a different time or day, for example: `reschedule to next Monday at 4 PM`.",
            [
                {
                    "agent": "orchestrator",
                    "action": "handle_no_slot_confirmation",
                    "status": "completed",
                    "title": title,
                }
            ],
        )

    if pending.get("type") == "reschedule_suggested_meeting":
        try:
            title = pending.get("title") or "Meeting"
            event_id = pending.get("event_id") or context.last_event_id
            start_raw = pending.get("start")
            if not event_id or not start_raw:
                context.pending_confirmation = {}
                return WorkflowResult(False, "", [])

            start_dt = datetime.fromisoformat(start_raw).astimezone(IST)
            duration_minutes = int(pending.get("duration_minutes") or DEFAULT_MEETING_DURATION)
            end_dt = start_dt + timedelta(minutes=duration_minutes)
            attendees = list(pending.get("attendees") or [])

            update_raw = update_event_time(
                event_id=event_id,
                new_start_time=start_dt.isoformat(),
                new_end_time=end_dt.isoformat(),
                timezone_str=DEFAULT_TIMEZONE,
            )
            update_data = _extract_json(update_raw)

            if update_data.get("status") != "updated":
                return WorkflowResult(
                    True,
                    "Status: Failed\n\n"
                    "Summary: I couldn’t reschedule the suggested meeting slot.\n\n"
                    f"Details: {update_data.get('message', 'Unknown error')}",
                    [
                        {
                            "agent": "calendar_agent",
                            "action": "update_suggested_event",
                            "status": "failed",
                            "reason": update_data.get("message", "Unknown error"),
                        }
                    ],
                )

            context.pending_confirmation = {}

            html_link = update_data.get("html_link", pending.get("html_link", ""))
            attendees = update_data.get("attendees", []) or attendees

            _remember_scheduled_meeting(
                context,
                title=title,
                start_dt=start_dt,
                duration_minutes=duration_minutes,
                attendees=attendees,
                html_link=html_link,
                event_id=event_id,
            )
            context.last_intent = "reschedule_meeting"
            context.last_action = "meeting_rescheduled"

            return WorkflowResult(
                True,
                _format_meeting_response(
                    status_title="Status: Completed\n\nSummary: Suggested reschedule confirmed and meeting updated successfully.",
                    meeting_title=title,
                    start_dt=start_dt,
                    duration_minutes=duration_minutes,
                    attendees=attendees,
                    event_link=html_link,
                ),
                [
                    {
                        "agent": "calendar_agent",
                        "action": "read_pending_reschedule_confirmation",
                        "status": "success",
                    },
                    {
                        "agent": "calendar_agent",
                        "action": "update_suggested_event",
                        "status": "success",
                        "event_id": event_id,
                        "html_link": html_link,
                        "title": title,
                        "start": start_dt.isoformat(),
                        "duration": duration_minutes,
                        "attendees": attendees,
                    },
                ],
            )

        except Exception as exc:
            logger.exception("handle reschedule pending confirmation failed")
            return WorkflowResult(
                True,
                "Status: Failed\n\n"
                "Summary: I couldn’t complete the reschedule confirmation due to an internal error.\n\n"
                f"Details: {exc}",
                [
                    {
                        "agent": "calendar_agent",
                        "action": "handle_reschedule_confirmation",
                        "status": "failed",
                        "reason": str(exc),
                    }
                ],
            )

    if pending.get("type") != "schedule_suggested_meeting":
        return WorkflowResult(False, "", [])

    try:
        title = pending.get("title") or "Meeting"
        start_raw = pending.get("start")
        if not start_raw:
            context.pending_confirmation = {}
            return WorkflowResult(False, "", [])

        start_dt = datetime.fromisoformat(start_raw).astimezone(IST)
        duration_minutes = int(pending.get("duration_minutes") or DEFAULT_MEETING_DURATION)
        end_dt = start_dt + timedelta(minutes=duration_minutes)
        attendees = list(pending.get("attendees") or [])

        raw = create_event(
            summary=title,
            start_time=start_dt.isoformat(),
            end_time=end_dt.isoformat(),
            attendees=attendees,
            timezone_str=DEFAULT_TIMEZONE,
        )
        data = _extract_json(raw)

        if data.get("status") != "created":
            return WorkflowResult(
                True,
                "Status: Failed\n\n"
                "Summary: I couldn’t schedule the suggested meeting slot.\n\n"
                f"Details: {data.get('message', 'Unknown error')}",
                [
                    {
                        "agent": "calendar_agent",
                        "action": "create_suggested_event",
                        "status": "failed",
                        "reason": data.get("message", "Unknown error"),
                    }
                ],
            )

        context.pending_confirmation = {}

        html_link = data.get("html_link", "")
        event_id = data.get("event_id", "")

        _remember_scheduled_meeting(
            context,
            title=title,
            start_dt=start_dt,
            duration_minutes=duration_minutes,
            attendees=attendees,
            html_link=html_link,
            event_id=event_id,
        )

        return WorkflowResult(
            True,
            _format_meeting_response(
                status_title="Status: Completed\n\nSummary: Suggested meeting slot confirmed and scheduled successfully.",
                meeting_title=title,
                start_dt=start_dt,
                duration_minutes=duration_minutes,
                attendees=attendees,
                event_link=html_link,
            ),
            [
                {
                    "agent": "calendar_agent",
                    "action": "read_pending_confirmation",
                    "status": "success",
                },
                {
                    "agent": "calendar_agent",
                    "action": "create_suggested_event",
                    "status": "success",
                    "event_id": event_id,
                    "html_link": html_link,
                    "title": title,
                    "start": start_dt.isoformat(),
                    "duration": duration_minutes,
                    "attendees": attendees,
                },
            ],
        )

    except Exception as exc:
        logger.exception("handle_pending_confirmation failed")
        return WorkflowResult(
            True,
            "Status: Failed\n\n"
            "Summary: I couldn’t complete the pending confirmation due to an internal error.\n\n"
            f"Details: {exc}",
            [
                {
                    "agent": "calendar_agent",
                    "action": "handle_pending_confirmation",
                    "status": "failed",
                    "reason": str(exc),
                }
            ],
        )


async def draft_email_flow(
    message: str,
    meeting: Optional[ParsedMeeting] = None,
    steps: Optional[list[dict]] = None,
    fallback_to: Optional[list[str]] = None,
) -> WorkflowResult:
    if "draft" not in message.lower():
        return WorkflowResult(False, "", [])

    if meeting:
        link = _extract_event_link_from_steps(steps or [])
        doc_title, doc_url = _extract_doc_info_from_steps(steps or [])
        default_body = _build_email_body(meeting.title, meeting.start_dt, meeting.duration_minutes, "inform", link)
        default_body = _append_doc_link(default_body, doc_title, doc_url)
        default_subject = f"Meeting: {meeting.title}"
    else:
        default_subject = "Meeting Update"
        default_body = "Hi,\n\nMeeting details.\n\nBest regards"

    email_request = _build_current_email_request(
        message=message,
        default_subject=default_subject,
        default_body=default_body,
        fallback_to=fallback_to,
    )
    if not email_request.to:
        return WorkflowResult(
            True,
            "Need recipient email address. Please provide it as `to person@example.com` or `to: person@example.com`.",
            steps or [],
        )

    raw = draft_email(
        to=", ".join(email_request.to),
        cc=", ".join(email_request.cc),
        bcc=", ".join(email_request.bcc),
        subject=email_request.subject,
        body=email_request.body,
    )
    data = _extract_json(raw)

    if data.get("status") != "drafted":
        return WorkflowResult(True, f'Could not create draft. Reason: {data.get("message", "Unknown error")}', steps or [])

    merged_steps = list(steps or [])
    merged_steps.append(
        {
            "agent": "gmail_agent",
            "action": "draft_email",
            "to": email_request.to,
            "cc": email_request.cc,
            "bcc": email_request.bcc,
            "subject": email_request.subject,
        }
    )

    return WorkflowResult(
        True,
        _format_email_action_response(
            action_label="Draft created successfully.",
            to=email_request.to,
            cc=email_request.cc,
            bcc=email_request.bcc,
            subject=email_request.subject,
            body=email_request.body,
        ),
        merged_steps,
    )


async def send_email_flow(
    message: str,
    meeting: Optional[ParsedMeeting] = None,
    steps: Optional[list[dict]] = None,
    fallback_to: Optional[list[str]] = None,
) -> WorkflowResult:
    if "send" not in message.lower():
        return WorkflowResult(False, "", [])

    if meeting:
        link = _extract_event_link_from_steps(steps or [])
        doc_title, doc_url = _extract_doc_info_from_steps(steps or [])
        default_body = _build_email_body(meeting.title, meeting.start_dt, meeting.duration_minutes, "join", link)
        default_body = _append_doc_link(default_body, doc_title, doc_url)
        default_subject = f"Joining: {meeting.title}"
    else:
        default_subject = "Update"
        default_body = "Hi,\n\nI will join.\n\nBest regards"

    email_request = _build_current_email_request(
        message=message,
        default_subject=default_subject,
        default_body=default_body,
        fallback_to=fallback_to,
    )
    if not email_request.to:
        return WorkflowResult(
            True,
            "Need recipient email address. Please provide it as `to person@example.com` or `to: person@example.com`.",
            steps or [],
        )

    raw = send_email(
        to=", ".join(email_request.to),
        cc=", ".join(email_request.cc),
        bcc=", ".join(email_request.bcc),
        subject=email_request.subject,
        body=email_request.body,
    )
    data = _extract_json(raw)

    if data.get("status") != "sent":
        return WorkflowResult(True, f'Could not send email. Reason: {data.get("message", "Unknown error")}', steps or [])

    merged_steps = list(steps or [])
    merged_steps.append(
        {
            "agent": "gmail_agent",
            "action": "send_email",
            "to": email_request.to,
            "cc": email_request.cc,
            "bcc": email_request.bcc,
            "subject": email_request.subject,
        }
    )

    return WorkflowResult(
        True,
        _format_email_action_response(
            action_label="Email sent successfully.",
            to=email_request.to,
            cc=email_request.cc,
            bcc=email_request.bcc,
            subject=email_request.subject,
            body=email_request.body,
        ),
        merged_steps,
    )


def _is_reschedule_suggestion_request(message: str) -> bool:
    """Detect safe demo flow: find next meeting and suggest a new time without updating immediately."""
    lower = " ".join((message or "").lower().strip().split())
    if not lower:
        return False

    next_meeting_terms = [
        "next calendar meeting",
        "next meeting",
        "upcoming calendar meeting",
        "upcoming meeting",
    ]
    suggest_terms = ["suggest", "recommend", "propose"]
    reschedule_terms = ["reschedule", "rescheduling", "move", "shift", "change"]

    return (
        any(term in lower for term in next_meeting_terms)
        and any(term in lower for term in suggest_terms)
        and any(term in lower for term in reschedule_terms)
    )


async def suggest_reschedule_next_meeting_flow(
    message: str,
    session_id: Optional[str] = None,
) -> WorkflowResult:
    """Find the next upcoming meeting and suggest a reschedule time without updating until confirmation."""
    if not _is_reschedule_suggestion_request(message):
        return WorkflowResult(False, "", [])

    try:
        parsed = _parse_datetime(message)
        if not parsed:
            return WorkflowResult(
                True,
                "Status: Failed\n\n"
                "Summary: I found a reschedule suggestion request, but the proposed new time was not clear.\n\n"
                "Next step: Please specify a new time, for example: tomorrow at 3 PM.",
                [
                    {
                        "agent": "calendar_agent",
                        "action": "parse_reschedule_suggestion_time",
                        "status": "failed",
                        "reason": "Suggested reschedule time was unclear",
                    }
                ],
            )

        now = _local_now().replace(second=0, microsecond=0)
        search_until = now + timedelta(days=14)

        raw = list_events(
            time_min=now.isoformat(),
            time_max=search_until.isoformat(),
            timezone_str=DEFAULT_TIMEZONE,
        )
        data = _extract_json(raw)
        events = data.get("events", []) if isinstance(data, dict) else []

        upcoming_events: list[dict[str, Any]] = []
        for event in events:
            start_dt, _end_dt = _safe_event_times(event)
            if start_dt and start_dt >= now:
                upcoming_events.append(event)

        upcoming_events.sort(key=lambda item: _safe_event_times(item)[0] or search_until)

        if not upcoming_events:
            return WorkflowResult(
                True,
                "Status: No upcoming meeting found\n\n"
                "Summary: I could not find any upcoming calendar meeting in the next 14 days.\n\n"
                "Next step: Please mention the meeting title if you want me to reschedule a specific event.",
                [
                    {
                        "agent": "calendar_agent",
                        "action": "list_upcoming_events",
                        "status": "success",
                        "count": 0,
                    }
                ],
            )

        event = upcoming_events[0]
        title, old_start, duration_minutes, event_link, attendees, event_id = _event_to_meeting_info(event)

        if not event_id:
            return WorkflowResult(
                True,
                f'Status: Failed\n\nSummary: I found your next meeting "{title}", but its event id is missing, so I cannot prepare a safe reschedule confirmation.',
                [
                    {
                        "agent": "calendar_agent",
                        "action": "read_next_event",
                        "status": "failed",
                        "reason": "event_id missing",
                        "title": title,
                    }
                ],
            )

        requested_start = parsed.start_dt
        suggested_duration = duration_minutes or DEFAULT_MEETING_DURATION

        # 1) First try same day at/after requested time
        free_slots = _compute_free_slots_for_day(
            requested_start=requested_start,
            duration_minutes=suggested_duration,
        )
        candidate_slots = [slot for slot in free_slots if slot >= requested_start]

        # 2) Fallback: if no slot is available at/after requested time,
        #    show other same-day free slots, so the user still gets useful options.
        if not candidate_slots:
            candidate_slots = free_slots

        # 3) Fallback: if same day has no free slots, try next day.
        if not candidate_slots:
            next_day_start = requested_start + timedelta(days=1)
            next_day_slots = _compute_free_slots_for_day(
                requested_start=next_day_start,
                duration_minutes=suggested_duration,
            )
            candidate_slots = next_day_slots

        # 4) Final fallback: try the next 5 working days.
        if not candidate_slots:
            for day_offset in range(2, 6):
                future_day_start = requested_start + timedelta(days=day_offset)
                future_day_slots = _compute_free_slots_for_day(
                    requested_start=future_day_start,
                    duration_minutes=suggested_duration,
                )
                if future_day_slots:
                    candidate_slots = future_day_slots
                    break

        suggested_slots = candidate_slots[:3]
        suggested_start = suggested_slots[0] if suggested_slots else None

        if not suggested_start:
            context = _get_context(session_id)
            context.pending_confirmation = {
                "type": "reschedule_no_slot",
                "event_id": event_id,
                "title": title,
                "old_start": old_start.isoformat(),
                "requested_start": requested_start.isoformat(),
                "duration_minutes": suggested_duration,
                "attendees": attendees,
                "html_link": event_link,
                "create_agenda": "agenda" in message.lower(),
            }

            context.last_intent = "suggest_reschedule_meeting"
            context.last_action = "reschedule_no_slot"
            context.last_event_id = event_id
            context.last_event_title = title
            context.last_event_start = old_start.isoformat()
            context.last_event_duration_minutes = suggested_duration
            context.last_event_attendees = list(attendees or [])
            context.last_event_html_link = event_link or ""

            return WorkflowResult(
                True,
                f'Status: Failed\n\n'
                f'Summary: I found your next meeting "{title}", but I could not find a free slot at the requested time or in the next few working days.\n\n'
                "Next step: Please choose another day or a wider time window.",
                [
                    {
                        "agent": "calendar_agent",
                        "action": "list_upcoming_events",
                        "status": "success",
                        "count": len(upcoming_events),
                    },
                    {
                        "agent": "calendar_agent",
                        "action": "read_next_event",
                        "status": "success",
                        "event_id": event_id,
                        "title": title,
                    },
                    {
                        "agent": "calendar_agent",
                        "action": "find_free_reschedule_slot",
                        "status": "failed",
                        "requested_start": requested_start.isoformat(),
                    },
                ],
            )

        suggested_end = suggested_start + timedelta(minutes=suggested_duration)

        context = _get_context(session_id)
        context.pending_confirmation = {
            "type": "reschedule_suggested_meeting",
            "event_id": event_id,
            "title": title,
            "old_start": old_start.isoformat(),
            "start": suggested_start.isoformat(),
            "duration_minutes": suggested_duration,
            "attendees": attendees,
            "html_link": event_link,
        }

        context.last_intent = "suggest_reschedule_meeting"
        context.last_action = "reschedule_suggestion_pending"
        context.last_event_id = event_id
        context.last_event_title = title
        context.last_event_start = old_start.isoformat()
        context.last_event_duration_minutes = suggested_duration
        context.last_event_attendees = list(attendees or [])
        context.last_event_html_link = event_link or ""

        suggestion_lines = [f"- {_format_time(slot)}" for slot in suggested_slots]

        response = (
            "Status: Suggestion generated\n\n"
            f'Summary: I found your next meeting "{title}" and prepared safe reschedule options.\n\n'
            "Current meeting:\n"
            f"- Time: {_format_time(old_start)}\n"
            f"- Duration: {duration_minutes} minutes\n\n"
            "Suggested alternatives:\n"
            + "\n".join(suggestion_lines)
            + "\n\n"
            "Selected option for confirmation:\n"
            f"- Time: {_format_time(suggested_start)}\n"
            f"- Duration: {suggested_duration} minutes\n\n"
            "Next step: Reply `yes` or `confirm` if you want me to update the meeting to the selected option. I will not update it until you confirm."
        )

        return WorkflowResult(
            True,
            response,
            [
                {
                    "agent": "calendar_agent",
                    "action": "list_upcoming_events",
                    "status": "success",
                    "count": len(upcoming_events),
                },
                {
                    "agent": "calendar_agent",
                    "action": "read_next_event",
                    "status": "success",
                    "event_id": event_id,
                    "title": title,
                    "current_start": old_start.isoformat(),
                    "html_link": event_link,
                },
                {
                    "agent": "calendar_agent",
                    "action": "find_free_reschedule_slot",
                    "status": "success",
                    "requested_start": requested_start.isoformat(),
                    "suggested_options": [slot.isoformat() for slot in suggested_slots],
                },
                {
                    "agent": "orchestrator",
                    "action": "suggest_reschedule",
                    "status": "pending_confirmation",
                    "suggested_start": suggested_start.isoformat(),
                    "suggested_end": suggested_end.isoformat(),
                },
                {
                    "agent": "calendar_agent",
                    "action": "update_event",
                    "status": "skipped",
                    "reason": "Waiting for user confirmation",
                },
            ],
        )

    except Exception as exc:
        logger.exception("suggest_reschedule_next_meeting_flow failed")
        return WorkflowResult(
            True,
            "Status: Failed\n\n"
            "Summary: I couldn’t prepare the reschedule suggestion due to an internal error.\n\n"
            f"Details: {exc}",
            [
                {
                    "agent": "calendar_agent",
                    "action": "suggest_reschedule_next_meeting",
                    "status": "failed",
                    "reason": str(exc),
                }
            ],
        )


def _is_followup_reschedule_request(message: str) -> bool:
    if not message:
        return False

    lower = " ".join(message.lower().strip().split())

    direct_followup_phrases = [
        "move it",
        "move this",
        "move that",
        "reschedule it",
        "reschedule this",
        "reschedule that",
        "change it to",
        "change this to",
        "change that to",
        "shift it to",
        "shift this to",
        "shift that to",
    ]

    direct_reschedule_phrases = [
        "move my meeting",
        "move the meeting",
        "move my next meeting",
        "move next meeting",
        "shift my meeting",
        "shift the meeting",
        "postpone my meeting",
        "postpone the meeting",
        "change my meeting",
        "change the meeting",
        "update my meeting",
        "update the meeting",
    ]

    date_time_signals = [
        "today at",
        "tomorrow at",
        "day after tomorrow at",
        "next monday",
        "next tuesday",
        "next wednesday",
        "next thursday",
        "next friday",
        "next saturday",
        "next sunday",
    ]

    if any(phrase in lower for phrase in direct_followup_phrases):
        return True

    if any(phrase in lower for phrase in direct_reschedule_phrases):
        return True

    if any(signal in lower for signal in date_time_signals):
        scheduling_words = ["schedule", "book", "set up", "setup", "create"]
        if not any(word in lower for word in scheduling_words):
            return True

    return False


async def reschedule_meeting_flow(message: str, session_id: Optional[str] = None) -> WorkflowResult:
    lower = " ".join((message or "").lower().split())

    explicit_reschedule_terms = [
        "reschedule",
        "move my meeting",
        "move the meeting",
        "move my next meeting",
        "move next meeting",
        "shift meeting",
        "shift my meeting",
        "postpone meeting",
        "postpone my meeting",
        "change meeting time",
        "update meeting time",
    ]

    if not any(term in lower for term in explicit_reschedule_terms) and not _is_followup_reschedule_request(message):
        return WorkflowResult(False, "", [])

    context = _get_context(session_id)
    explicit_message = message

    if _is_followup_reschedule_request(message):
        if not context.last_event_title:
            return WorkflowResult(
                True,
                "I couldn’t find the previous meeting to update. Please mention the meeting title.",
                [],
            )
        explicit_message = _build_explicit_reschedule_message(message, context)

    title = _resolve_meeting_title_from_message_or_context(explicit_message, context)
    if not title:
        return WorkflowResult(True, "Please specify which meeting to reschedule.", [])

    logger.info("Reschedule target title resolved to: %s", title)

    event_raw = get_event_by_title(summary=title)
    event_data = _extract_json(event_raw)
    if event_data.get("status") != "ok":
        return WorkflowResult(True, f'Could not find "{title}" to reschedule.', [])

    existing_event = event_data["event"]
    logger.info(
        "Reschedule matched calendar event | requested_title=%s | matched_title=%s | attendees=%s",
        title,
        existing_event.get("summary", ""),
        existing_event.get("attendees", []),
    )

    old_start = datetime.fromisoformat(existing_event["start"]).astimezone(IST)
    event_id = existing_event.get("event_id") or context.last_event_id
    existing_duration = context.last_event_duration_minutes or 30

    parsed = _parse_datetime(explicit_message, base_date=old_start.date(), default_title=title)
    if not parsed:
        return WorkflowResult(True, "Please specify the new time clearly.", [])

    if parsed.duration_minutes == 30 and existing_duration:
        lower_message = explicit_message.lower()
        duration_explicitly_provided = any(
            token in lower_message for token in ["for ", "minutes", "mins", "hour", "half an hour"]
        )
        if not duration_explicitly_provided:
            parsed = ParsedMeeting(
                start_dt=parsed.start_dt,
                duration_minutes=existing_duration,
                title=title,
                attendees=existing_event.get("attendees", []) or context.last_event_attendees,
            )

    if not event_id:
        return WorkflowResult(True, f'I found "{title}", but its event id is missing, so I could not update it.', [])

    new_end = parsed.start_dt + timedelta(minutes=parsed.duration_minutes)
    update_raw = update_event_time(
        event_id=event_id,
        new_start_time=parsed.start_dt.isoformat(),
        new_end_time=new_end.isoformat(),
        timezone_str=DEFAULT_TIMEZONE,
    )
    update_data = _extract_json(update_raw)

    if update_data.get("status") != "updated":
        return WorkflowResult(True, update_data.get("message", "Failed to reschedule"), [])

    html_link = update_data.get("html_link", existing_event.get("html_link", ""))
    attendees = update_data.get("attendees", []) or existing_event.get("attendees", []) or context.last_event_attendees

    _remember_scheduled_meeting(
        context,
        title=title,
        start_dt=parsed.start_dt,
        duration_minutes=parsed.duration_minutes,
        attendees=attendees,
        html_link=html_link,
        event_id=event_id,
    )
    context.last_intent = "reschedule_meeting"
    context.last_action = "meeting_rescheduled"

    return WorkflowResult(
        True,
        _format_meeting_response(
            status_title=f'Meeting "{title}" rescheduled successfully.',
            meeting_title=title,
            start_dt=parsed.start_dt,
            duration_minutes=parsed.duration_minutes,
            attendees=attendees,
            event_link=html_link,
        ),
        [
            {
                "agent": "calendar_agent",
                "action": "reschedule_event",
                "event_id": event_id,
                "title": title,
                "html_link": html_link,
                "start": parsed.start_dt.isoformat(),
                "duration": parsed.duration_minutes,
                "attendees": attendees,
            }
        ],
    )



async def cancel_and_notify_flow(message: str, session_id: Optional[str] = None) -> WorkflowResult:
    lower = message.lower()
    if "cancel" not in lower or "notify" not in lower:
        return WorkflowResult(False, "", [])

    context = _get_context(session_id)
    title = _resolve_meeting_title_from_message_or_context(message, context)
    if not title:
        return WorkflowResult(True, "Please specify which meeting to cancel.", [])

    logger.info("Cancel target title resolved to: %s", title)

    event_raw = get_event_by_title(summary=title)
    event_data = _extract_json(event_raw)
    if event_data.get("status") != "ok":
        return WorkflowResult(True, f'Could not find "{title}" to cancel.', [])

    event = event_data["event"]
    logger.info(
        "Cancel matched calendar event | requested_title=%s | matched_title=%s | attendees=%s",
        title,
        event.get("summary", ""),
        event.get("attendees", []),
    )

    meeting_title, start_dt, duration_minutes, event_link, attendees, _event_id = _event_to_meeting_info(event)

    delete_raw = delete_event_by_title(summary=meeting_title)
    delete_data = _extract_json(delete_raw)
    if delete_data.get("status") != "deleted":
        return WorkflowResult(True, delete_data.get("message", "Failed to cancel meeting"), [])

    attendees = [a for a in attendees if a]
    context.last_intent = "cancel_meeting"
    context.last_action = "meeting_cancelled"

    if attendees:
        body = _build_email_body(meeting_title, start_dt, duration_minutes, "cancel", event_link)
        send_raw = send_email(to=", ".join(attendees), subject=f"Cancelled: {meeting_title}", body=body)
        send_data = _extract_json(send_raw)
        if send_data.get("status") != "sent":
            return WorkflowResult(True, f'Meeting "{meeting_title}" was cancelled, but notifying the attendees failed.', [])
        return WorkflowResult(
            True,
            (
                f'Meeting "{meeting_title}" cancelled successfully.\n\n'
                f"Attendees notified: {_join_or_none(attendees)}"
            ),
            [
                {"agent": "calendar_agent", "action": "cancel_event", "title": meeting_title},
                {"agent": "gmail_agent", "action": "notify_attendees", "to": attendees},
            ],
        )

    return WorkflowResult(
        True,
        f'Meeting "{meeting_title}" cancelled successfully. There were no attendees to notify.',
        [{"agent": "calendar_agent", "action": "cancel_event", "title": meeting_title}],
    )


async def reschedule_and_notify_flow(message: str, session_id: Optional[str] = None) -> WorkflowResult:
    lower = message.lower()
    if "reschedule" not in lower or "notify" not in lower:
        return WorkflowResult(False, "", [])

    reschedule_result = await reschedule_meeting_flow(message, session_id=session_id)
    if not reschedule_result.handled:
        return WorkflowResult(False, "", [])
    if not reschedule_result.workflow_steps:
        return reschedule_result

    context = _get_context(session_id)
    title = context.last_event_title
    if not title:
        return reschedule_result

    event_raw = get_event_by_title(summary=title)
    event_data = _extract_json(event_raw)
    if event_data.get("status") != "ok":
        return WorkflowResult(
            True,
            f'{reschedule_result.response}\n\nThe meeting was updated, but I could not fetch the latest attendee list to notify them.',
            reschedule_result.workflow_steps,
        )

    event = event_data["event"]
    meeting_title, start_dt, duration_minutes, event_link, attendees, _event_id = _event_to_meeting_info(event)
    attendees = [a for a in attendees if a] or [a for a in context.last_event_attendees if a]

    logger.info("Reschedule notify attendees | title=%s | attendees=%s", meeting_title, attendees)

    if not attendees:
        return WorkflowResult(
            True,
            f'{reschedule_result.response}\n\nNo attendees were available to notify.',
            reschedule_result.workflow_steps,
        )

    body = _build_email_body(meeting_title, start_dt, duration_minutes, "reschedule", event_link)
    send_raw = send_email(to=", ".join(attendees), subject=f"Rescheduled: {meeting_title}", body=body)
    send_data = _extract_json(send_raw)

    if send_data.get("status") != "sent":
        return WorkflowResult(
            True,
            f'{reschedule_result.response}\n\nThe meeting was rescheduled, but notifying the attendees failed.',
            reschedule_result.workflow_steps,
        )

    steps = list(reschedule_result.workflow_steps)
    steps.append({"agent": "gmail_agent", "action": "notify_attendees", "to": attendees, "subject": f"Rescheduled: {meeting_title}"})

    return WorkflowResult(
        True,
        f'{reschedule_result.response}\n\nAll attendees have been notified about the updated meeting time.',
        steps,
    )


async def daily_ai_briefing() -> WorkflowResult:
    today_min, today_max = _day_range(0)

    events_raw = list_events(time_min=today_min, time_max=today_max, timezone_str=DEFAULT_TIMEZONE)
    events_data = _extract_json(events_raw)
    events = events_data.get("events", [])

    emails_raw = read_emails(max_results=5, query="is:unread", label="INBOX")
    emails_data = _extract_json(emails_raw)
    emails = emails_data.get("emails", [])

    return WorkflowResult(
        True,
        _format_daily_briefing_response(events, emails),
        [
            {"agent": "calendar_agent", "action": "list_events_today", "count": len(events)},
            {"agent": "gmail_agent", "action": "read_unread_emails", "count": len(emails)},
        ],
    )




def _has_successful_step(steps: list[dict], agent: str, action: str) -> bool:
    """Return True only when the expected agent action completed successfully.

    Some existing steps do not explicitly include status. For backward compatibility,
    a missing status is treated as success only when the agent/action match.
    """
    for step in steps or []:
        if step.get("agent") != agent or step.get("action") != action:
            continue
        status = (step.get("status") or "success").lower()
        return status in {"success", "created", "sent", "drafted", "updated", "deleted", "ok"}
    return False


def _has_failed_step(steps: list[dict]) -> bool:
    return any((step.get("status") or "").lower() == "failed" for step in steps or [])


def _mark_missing_failure_step(
    steps: list[dict],
    *,
    agent: str,
    action: str,
    reason: str,
    **extra: Any,
) -> list[dict]:
    merged_steps = list(steps or [])
    merged_steps.append(
        {
            "agent": agent,
            "action": action,
            "status": "failed",
            "reason": reason,
            **extra,
        }
    )
    return merged_steps


def _format_partial_failure_response(
    *,
    completed_steps: list[str],
    failed_step: str,
    reason: str,
    next_step: str,
) -> str:
    lines = [
        "Status: Partial completion",
        "",
        "Summary: The workflow was not fully completed.",
        "",
        "Completed steps:",
    ]

    if completed_steps:
        lines.extend(f"- {step}" for step in completed_steps)
    else:
        lines.append("- None")

    lines.extend(
        [
            "",
            "Failed step:",
            f"- {failed_step}",
            "",
            "Reason:",
            f"- {reason}",
            "",
            "Next step:",
            f"- {next_step}",
            "",
            "I did not mark the full workflow as completed because one step failed.",
        ]
    )
    return "\n".join(lines)


def _format_execution_trace(workflow_steps: list[dict]) -> str:
    if not workflow_steps:
        return ""

    lines = ["", "Execution Trace:"]
    for index, step in enumerate(workflow_steps, start=1):
        agent = step.get("agent", "unknown_agent")
        action = step.get("action", "unknown_action")
        status = step.get("status", "completed")
        reason = step.get("reason", "")

        line = f"{index}. {agent}.{action} → {status}"
        if reason:
            line += f" ({reason})"

        lines.append(line)

    return "\n".join(lines)


def _with_execution_trace(result: WorkflowResult) -> WorkflowResult:
    if not result.handled or not result.workflow_steps:
        return result

    if "Execution Trace:" in (result.response or ""):
        return result

    return WorkflowResult(
        handled=result.handled,
        response=(result.response or "") + _format_execution_trace(result.workflow_steps),
        workflow_steps=result.workflow_steps,
    )

async def schedule_agenda_and_email_flow(message: str, session_id: Optional[str] = None, mode: str = "send") -> WorkflowResult:
    lower = message.lower()
    if not _contains_schedule_intent(message):
        return WorkflowResult(False, "", [])

    needs_doc = any(token in lower for token in ["agenda", "document", "doc"])
    needs_email = "email" in lower
    needs_send = "send" in lower if mode == "send" else "draft" in lower

    if not (needs_doc and needs_email and needs_send):
        return WorkflowResult(False, "", [])

    # Step 1: Schedule meeting.
    sched = await schedule_meeting(message, session_id=session_id)
    if not sched.handled:
        return WorkflowResult(False, "", [])

    sched_steps = list(sched.workflow_steps or [])

    # IMPORTANT: schedule_meeting can return handled=True for conflicts/failures.
    # Do not continue to document/email steps unless the calendar event was actually created.
    calendar_created = _has_successful_step(sched_steps, "calendar_agent", "create_event")

    if not calendar_created:
        skipped_steps = list(sched_steps)
        skipped_steps.append(
            {
                "agent": "docs_agent",
                "action": "create_document",
                "status": "skipped",
                "reason": "Meeting was not scheduled",
            }
        )
        skipped_steps.append(
            {
                "agent": "gmail_agent",
                "action": "send_email" if mode == "send" else "draft_email",
                "status": "skipped",
                "reason": "Meeting was not scheduled",
            }
        )

        return WorkflowResult(
            True,
            (
                sched.response
                + "\n\nAgenda document creation skipped because the meeting was not scheduled."
                + "\nEmail notification skipped because the meeting was not scheduled."
            ),
            skipped_steps,
        )

    parsed = _parse_datetime(message)
    if not parsed:
        failed_steps = _mark_missing_failure_step(
            sched_steps,
            agent="orchestrator_agent",
            action="parse_meeting_details_for_multistep_workflow",
            reason="Meeting details could not be parsed after calendar creation",
        )
        return WorkflowResult(
            True,
            _format_partial_failure_response(
                completed_steps=["Calendar event created"],
                failed_step="Parse meeting details for agenda/email workflow",
                reason="Meeting details could not be parsed after calendar creation.",
                next_step="Retry the request with a clear date, time, title, and attendee email address.",
            ),
            failed_steps,
        )

    # Step 2: Create agenda document.
    doc_result = await create_agenda_doc_for_meeting(parsed, session_id=session_id)
    doc_steps = list(doc_result.workflow_steps or [])
    all_steps = sched_steps + doc_steps

    if (
        not doc_result.handled
        or not doc_steps
        or _has_failed_step(doc_steps)
        or not _has_successful_step(doc_steps, "docs_agent", "create_document")
    ):
        reason = doc_result.response or "Agenda document creation failed."
        if not doc_steps or not _has_failed_step(doc_steps):
            all_steps = _mark_missing_failure_step(
                all_steps,
                agent="docs_agent",
                action="create_document",
                reason=reason,
                title=_build_agenda_doc_title(parsed),
            )

        return WorkflowResult(
            True,
            _format_partial_failure_response(
                completed_steps=["Calendar event created"],
                failed_step="Agenda document creation",
                reason=reason,
                next_step="Retry only the agenda document creation or create it manually using the meeting details.",
            ),
            all_steps,
        )

    doc_title, doc_url = _extract_doc_info_from_steps(all_steps)
    event_link = _extract_event_link_from_steps(all_steps)
    default_subject, default_body = _build_agenda_email_defaults(parsed, event_link, doc_title, doc_url, mode=mode)

    email_request = _build_current_email_request(
        message=message,
        default_subject=default_subject,
        default_body=default_body,
        fallback_to=parsed.attendees,
    )

    # Step 3A: Draft email.
    if mode == "draft":
        if not email_request.to:
            failed_steps = _mark_missing_failure_step(
                all_steps,
                agent="gmail_agent",
                action="draft_email",
                reason="Recipient email address is missing",
            )
            return WorkflowResult(
                True,
                _format_partial_failure_response(
                    completed_steps=["Calendar event created", "Agenda document created"],
                    failed_step="Draft email",
                    reason="Recipient email address is missing.",
                    next_step="Provide the recipient email address and ask me to draft the email again.",
                ),
                failed_steps,
            )

        draft_result = await draft_email_flow(
            f'draft email to {", ".join(email_request.to)}'
            + (f' cc: {", ".join(email_request.cc)}' if email_request.cc else "")
            + (f' bcc: {", ".join(email_request.bcc)}' if email_request.bcc else "")
            + f' subject: {email_request.subject} body: {email_request.body}',
            meeting=parsed,
            steps=all_steps,
            fallback_to=parsed.attendees,
        )
        final_steps = list(draft_result.workflow_steps or all_steps)

        if _has_successful_step(final_steps, "gmail_agent", "draft_email"):
            return WorkflowResult(
                True,
                "Status: Completed\n\n"
                "Summary: Meeting scheduled, agenda document created, and draft email created successfully.",
                final_steps,
            )

        reason = draft_result.response or "Draft email step failed."
        if not _has_failed_step(final_steps):
            final_steps = _mark_missing_failure_step(
                final_steps,
                agent="gmail_agent",
                action="draft_email",
                reason=reason,
                to=email_request.to,
                subject=email_request.subject,
            )

        return WorkflowResult(
            True,
            _format_partial_failure_response(
                completed_steps=["Calendar event created", "Agenda document created"],
                failed_step="Draft email",
                reason=reason,
                next_step="Retry only the email draft step; the meeting and agenda document are already created.",
            ),
            final_steps,
        )

    # Step 3B: Send email.
    if not email_request.to:
        failed_steps = _mark_missing_failure_step(
            all_steps,
            agent="gmail_agent",
            action="send_email",
            reason="Recipient email address is missing",
        )
        return WorkflowResult(
            True,
            _format_partial_failure_response(
                completed_steps=["Calendar event created", "Agenda document created"],
                failed_step="Send email",
                reason="Recipient email address is missing.",
                next_step="Provide the recipient email address and ask me to send the email again.",
            ),
            failed_steps,
        )

    send_result = await send_email_flow(
        f'send email to {", ".join(email_request.to)}'
        + (f' cc: {", ".join(email_request.cc)}' if email_request.cc else "")
        + (f' bcc: {", ".join(email_request.bcc)}' if email_request.bcc else "")
        + f' subject: {email_request.subject} body: {email_request.body}',
        meeting=parsed,
        steps=all_steps,
        fallback_to=parsed.attendees,
    )
    final_steps = list(send_result.workflow_steps or all_steps)

    if _has_successful_step(final_steps, "gmail_agent", "send_email"):
        return WorkflowResult(
            True,
            "Status: Completed\n\n"
            "Summary: Meeting scheduled, agenda document created, and email sent successfully.",
            final_steps,
        )

    reason = send_result.response or "Email sending step failed."
    if not _has_failed_step(final_steps):
        final_steps = _mark_missing_failure_step(
            final_steps,
            agent="gmail_agent",
            action="send_email",
            reason=reason,
            to=email_request.to,
            subject=email_request.subject,
        )

    return WorkflowResult(
        True,
        _format_partial_failure_response(
            completed_steps=["Calendar event created", "Agenda document created"],
            failed_step="Send email",
            reason=reason,
            next_step="Retry only the email notification step; the meeting and agenda document are already created.",
        ),
        final_steps,
    )

async def handle_premium_workflow(
    message: str,
    session_id: Optional[str] = None,
    user_id: str = DEFAULT_USER_ID,
) -> WorkflowResult:
    raw_lower = (message or "").lower().strip()
    lower = re.sub(r"[!?.,]+$", "", raw_lower).strip()
    normalized = " ".join(lower.split())

    def has_any(text: str, phrases: list[str]) -> bool:
        return any(p in text for p in phrases)

    # NEW: handle "yes / confirm / proceed" after conflict suggestion
    result = await handle_pending_confirmation(message, session_id=session_id)
    if result.handled:
        return _with_execution_trace(result)

    is_briefing = has_any(
        normalized,
        [
            "brief my day",
            "morning briefing",
            "give me my morning briefing",
            "daily ai briefing",
            "give me my morning briefing for today",
            "morning briefing for today",
            "today's priorities",
            "todays priorities",
        ],
    )

    is_today_events = has_any(
        normalized,
        [
            "show my events for today",
            "what meetings do i have today",
            "show my meetings today",
            "what meetings i have for today",
            "today's schedule",
            "todays schedule",
            "whats on my calendar today",
            "what's on my calendar today",
            "show my events today",
            "show calendar today",
            "calendar today",
        ],
    )

    is_tomorrow_events = has_any(
        normalized,
        [
            "show my events for tomorrow",
            "what meetings do i have tomorrow",
            "what meetings i have tomorrow",
            "show my meetings tomorrow",
            "tomorrow's schedule",
            "tomorrows schedule",
            "show my events tomorrow",
        ],
    )

    is_unread_email_query = has_any(
        normalized,
        [
            "show unread emails",
            "triage inbox",
            "check my unread emails",
            "check unread emails",
            "my unread emails",
            "summarize my unread important emails",
            "check my unread emails and summarize them",
            "summarize my unread important emails and tell me which ones need action today",
            "summarize inbox",
            "inbox summary",
            "check my inbox",
            "unread emails",
        ],
    )

    is_cancel_intent = has_any(
        normalized,
        [
            "cancel meeting",
            "cancel the meeting",
            "cancel my meeting",
            "cancel event",
            "delete meeting",
            "remove meeting",
        ],
    )

    is_reschedule_intent = has_any(
        normalized,
        [
            "reschedule",
            "reschedule meeting",
            "reschedule meeting to",
            "move my meeting",
            "move the meeting",
            "move my next meeting",
            "move next meeting",
            "shift meeting",
            "postpone meeting",
            "change meeting time",
            "update meeting time",
            "modify existing calendar event",
            "modify meeting",
        ],
    )

    is_email_intent = has_any(
        normalized,
        [
            "email",
            "gmail",
            "inbox",
            "unread",
            "draft email",
            "send email",
            "summarize my unread",
            "important emails",
            "triage inbox",
        ],
    )

    is_doc_intent = has_any(
        normalized,
        [
            "create a document",
            "create document",
            "create a google doc",
            "create google doc",
            "meeting notes",
            "notes document",
        ],
    )

    is_schedule_intent = (
        _contains_schedule_intent(message)
        or has_any(
            normalized,
            [
                "schedule meeting",
                "create and share a calendar event",
                "book a meeting",
                "set up a meeting",
            ],
        )
    ) and not is_reschedule_intent and not is_cancel_intent

    if normalized == "morning briefing":
        return _with_execution_trace(await daily_ai_briefing())

    if normalized == "triage inbox":
        return _with_execution_trace(await show_unread_emails())

    if normalized in {"schedule meeting", "create and share a calendar event"}:
        return WorkflowResult(
            True,
            "Please provide the meeting details, for example: Schedule a design review meeting tomorrow at 3 PM with alice@example.com",
            [],
        )

    if normalized in {"reschedule meeting", "modify existing calendar event"}:
        return WorkflowResult(
            True,
            "Please tell me which meeting to reschedule and the new time, for example: Reschedule design review meeting to tomorrow at 4 PM and notify attendees",
            [],
        )

    if is_briefing:
        return _with_execution_trace(await daily_ai_briefing())

    if is_today_events:
        return _with_execution_trace(await show_events(0, "today"))

    if is_tomorrow_events:
        return _with_execution_trace(await show_events(1, "tomorrow"))

    if is_unread_email_query:
        return _with_execution_trace(await show_unread_emails())

    result = await suggest_reschedule_next_meeting_flow(message, session_id=session_id)
    if result.handled:
        return _with_execution_trace(result)

    # Keep multi-agent orchestration BEFORE generic email handling
    result = await schedule_agenda_and_email_flow(message, session_id=session_id, mode="send")
    if result.handled:
        return _with_execution_trace(result)

    result = await schedule_agenda_and_email_flow(message, session_id=session_id, mode="draft")
    if result.handled:
        return _with_execution_trace(result)

    result = await update_user_intelligence_preferences(message, user_id=user_id)
    if result.handled:
        return _with_execution_trace(result)

    if is_cancel_intent:
        result = await cancel_and_notify_flow(message, session_id=session_id)
        if result.handled:
            return _with_execution_trace(result)

    if is_reschedule_intent:
        result = await reschedule_and_notify_flow(message, session_id=session_id)
        if result.handled:
            return _with_execution_trace(result)

        result = await reschedule_meeting_flow(message, session_id=session_id)
        if result.handled:
            return _with_execution_trace(result)

    if is_email_intent:
        result = await intelligent_inbox_workflow(message, session_id=session_id, user_id=user_id)
        if result.handled:
            return _with_execution_trace(result)

        res = await draft_email_flow(message)
        if res.handled:
            return _with_execution_trace(res)

        res = await send_email_flow(message)
        if res.handled:
            return _with_execution_trace(res)

    if is_doc_intent:
        res = await create_google_doc_from_prompt(message, session_id=session_id)
        if res.handled:
            return _with_execution_trace(res)

    parsed = _parse_datetime(message)

    if is_schedule_intent and "then draft" in normalized:
        sched = await schedule_meeting(message, session_id=session_id)
        if not sched.handled or not sched.workflow_steps:
            return _with_execution_trace(sched)
        return _with_execution_trace(await draft_email_flow(
            message,
            parsed,
            sched.workflow_steps,
            fallback_to=parsed.attendees if parsed else None,
        ))

    if is_schedule_intent and "then send" in normalized:
        sched = await schedule_meeting(message, session_id=session_id)
        if not sched.handled or not sched.workflow_steps:
            return _with_execution_trace(sched)
        return _with_execution_trace(await send_email_flow(
            message,
            parsed,
            sched.workflow_steps,
            fallback_to=parsed.attendees if parsed else None,
        ))

    if is_schedule_intent:
        result = await smart_schedule_from_preferences(message, session_id=session_id, user_id=user_id)
        if result.handled:
            return _with_execution_trace(result)

        res = await schedule_meeting(message, session_id=session_id)
        if res.handled:
            return _with_execution_trace(res)

    res = await draft_email_flow(message)
    if res.handled:
        return _with_execution_trace(res)

    res = await send_email_flow(message)
    if res.handled:
        return _with_execution_trace(res)

    res = await create_google_doc_from_prompt(message, session_id=session_id)
    if res.handled:
        return _with_execution_trace(res)

    res = await schedule_meeting(message, session_id=session_id)
    if res.handled:
        return _with_execution_trace(res)

    return WorkflowResult(
        True,
        "I couldn’t match that request to a supported workflow. Please try rephrasing it.",
        [],
    )
    