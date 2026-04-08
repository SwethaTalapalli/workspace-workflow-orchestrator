"""MCP Server wrapping Google Calendar API."""

import json
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)

mcp = FastMCP("GoogleCalendar")
DEFAULT_TIMEZONE = "Asia/Kolkata"


def _get_service():
    from app.services.google_auth import get_calendar_service
    return get_calendar_service()


def _ensure_timezone_name(timezone_str: str) -> str:
    try:
        ZoneInfo(timezone_str)
        return timezone_str
    except Exception:
        logger.warning(
            "Invalid timezone '%s'; falling back to %s",
            timezone_str,
            DEFAULT_TIMEZONE,
        )
        return DEFAULT_TIMEZONE


def _ensure_aware_iso(dt_str: str, timezone_str: str) -> str:
    tz_name = _ensure_timezone_name(timezone_str)
    tz = ZoneInfo(tz_name)
    parsed = datetime.fromisoformat(dt_str)

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)

    return parsed.isoformat()


def _normalize_text(text: str) -> str:
    return " ".join((text or "").lower().replace("-", " ").split())


def _parse_event_datetime(event_time: dict, fallback_tz: str) -> datetime | None:
    tz = ZoneInfo(_ensure_timezone_name(fallback_tz))

    if not event_time:
        return None

    if event_time.get("dateTime"):
        dt = datetime.fromisoformat(event_time["dateTime"].replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        return dt

    if event_time.get("date"):
        dt = datetime.fromisoformat(f"{event_time['date']}T00:00:00")
        return dt.replace(tzinfo=tz)

    return None


def _today_range_iso(timezone_str: str) -> tuple[str, str]:
    tz_name = _ensure_timezone_name(timezone_str)
    tz = ZoneInfo(tz_name)

    now = datetime.now(tz)
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end_of_day = now.replace(hour=23, minute=59, second=59, microsecond=999999)

    return start_of_day.isoformat(), end_of_day.isoformat()


def _find_overlapping_events(
    service,
    start_iso: str,
    end_iso: str,
    timezone_str: str,
    exclude_event_id: str | None = None,
) -> list[dict]:
    tz_name = _ensure_timezone_name(timezone_str)
    requested_start = datetime.fromisoformat(start_iso)
    requested_end = datetime.fromisoformat(end_iso)

    search_start = (requested_start - timedelta(days=1)).isoformat()
    search_end = (requested_end + timedelta(days=1)).isoformat()

    events_result = (
        service.events()
        .list(
            calendarId="primary",
            timeMin=search_start,
            timeMax=search_end,
            maxResults=250,
            singleEvents=True,
            orderBy="startTime",
            timeZone=tz_name,
        )
        .execute()
    )

    events = events_result.get("items", [])
    overlaps = []

    for event in events:
        if exclude_event_id and event.get("id") == exclude_event_id:
            continue

        if event.get("status") == "cancelled":
            continue

        if event.get("transparency") == "transparent":
            continue

        event_start = _parse_event_datetime(event.get("start", {}), tz_name)
        event_end = _parse_event_datetime(event.get("end", {}), tz_name)

        if not event_start or not event_end:
            continue

        if requested_start < event_end and requested_end > event_start:
            overlaps.append(
                {
                    "event_id": event.get("id"),
                    "summary": event.get("summary", "(No title)"),
                    "start": event.get("start", {}).get(
                        "dateTime", event.get("start", {}).get("date")
                    ),
                    "end": event.get("end", {}).get(
                        "dateTime", event.get("end", {}).get("date")
                    ),
                    "status": event.get("status", ""),
                }
            )

    return overlaps


def _find_events_by_title(
    service,
    summary: str,
    time_min: str = "",
    time_max: str = "",
    timezone_str: str = DEFAULT_TIMEZONE,
) -> list[dict]:
    tz_name = _ensure_timezone_name(timezone_str)
    clean_summary = (summary or "").strip()
    if not clean_summary:
        return []

    now = datetime.now(ZoneInfo(tz_name))

    if not time_min:
        time_min = (now - timedelta(days=1)).isoformat()
    else:
        time_min = _ensure_aware_iso(time_min, tz_name)

    if not time_max:
        time_max = (now + timedelta(days=30)).isoformat()
    else:
        time_max = _ensure_aware_iso(time_max, tz_name)

    events_result = (
        service.events()
        .list(
            calendarId="primary",
            timeMin=time_min,
            timeMax=time_max,
            maxResults=100,
            singleEvents=True,
            orderBy="startTime",
            timeZone=tz_name,
        )
        .execute()
    )

    events = events_result.get("items", [])
    target = _normalize_text(clean_summary)
    matches = []

    for event in events:
        event_summary = event.get("summary", "")
        normalized_event_summary = _normalize_text(event_summary)

        if (
            normalized_event_summary == target
            or target in normalized_event_summary
            or normalized_event_summary in target
        ):
            matches.append(event)

    return matches


def _best_matching_event(matches: list[dict], timezone_str: str) -> dict | None:
    if not matches:
        return None

    tz_name = _ensure_timezone_name(timezone_str)
    now = datetime.now(ZoneInfo(tz_name))

    def event_start_key(event):
        raw = event.get("start", {}).get("dateTime") or event.get("start", {}).get("date")
        try:
            if raw and "T" in raw:
                dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            elif raw:
                dt = datetime.fromisoformat(f"{raw}T00:00:00").replace(
                    tzinfo=ZoneInfo(tz_name)
                )
            else:
                return (True, float("inf"))
            return (dt < now, abs((dt - now).total_seconds()))
        except Exception:
            return (True, float("inf"))

    matches.sort(key=event_start_key)
    return matches[0]


@mcp.tool()
def create_event(
    summary: str,
    start_time: str,
    end_time: str,
    description: str = "",
    location: str = "",
    attendees: list[str] | None = None,
    timezone_str: str = DEFAULT_TIMEZONE,
) -> str:
    """Create a new Google Calendar event after checking for conflicts."""
    service = _get_service()
    tz_name = _ensure_timezone_name(timezone_str)

    clean_summary = (summary or "").strip()
    if not clean_summary:
        return json.dumps({"status": "error", "message": "Event summary is required"})

    try:
        start_iso = _ensure_aware_iso(start_time, tz_name)
        end_iso = _ensure_aware_iso(end_time, tz_name)

        start_dt = datetime.fromisoformat(start_iso)
        end_dt = datetime.fromisoformat(end_iso)

        if end_dt <= start_dt:
            return json.dumps(
                {"status": "error", "message": "end_time must be after start_time"}
            )

        overlapping_events = _find_overlapping_events(
            service=service,
            start_iso=start_iso,
            end_iso=end_iso,
            timezone_str=tz_name,
        )

        if overlapping_events:
            logger.info("Overlapping events found: %s", overlapping_events)
            return json.dumps(
                {
                    "status": "error",
                    "message": "The selected time conflicts with another event.",
                    "summary": clean_summary,
                    "start": start_iso,
                    "end": end_iso,
                    "timezone": tz_name,
                    "conflicting_events": overlapping_events[:10],
                }
            )

        event_body = {
            "summary": clean_summary,
            "description": (description or "").strip(),
            "location": (location or "").strip(),
            "start": {"dateTime": start_iso, "timeZone": tz_name},
            "end": {"dateTime": end_iso, "timeZone": tz_name},
        }

        if attendees:
            clean_attendees = [
                {"email": email.strip()}
                for email in attendees
                if isinstance(email, str) and email.strip()
            ]
            if clean_attendees:
                event_body["attendees"] = clean_attendees

        event = service.events().insert(calendarId="primary", body=event_body).execute()

        return json.dumps(
            {
                "status": "created",
                "event_id": event.get("id"),
                "html_link": event.get("htmlLink"),
                "summary": event.get("summary"),
                "start": event["start"].get("dateTime", ""),
                "end": event["end"].get("dateTime", ""),
                "timezone": tz_name,
            }
        )
    except Exception as exc:
        logger.exception("Failed to create calendar event")
        return json.dumps({"status": "error", "message": f"Failed to create event: {exc}"})


@mcp.tool()
def list_events(
    time_min: str = "",
    time_max: str = "",
    timezone_str: str = DEFAULT_TIMEZONE,
) -> str:
    """List Google Calendar events for a time range."""
    service = _get_service()
    tz_name = _ensure_timezone_name(timezone_str)

    try:
        now = datetime.now(ZoneInfo(tz_name))

        if not time_min:
            time_min = now.isoformat()
        else:
            time_min = _ensure_aware_iso(time_min, tz_name)

        if not time_max:
            time_max = (now + timedelta(days=7)).isoformat()
        else:
            time_max = _ensure_aware_iso(time_max, tz_name)

        events_result = (
            service.events()
            .list(
                calendarId="primary",
                timeMin=time_min,
                timeMax=time_max,
                maxResults=100,
                singleEvents=True,
                orderBy="startTime",
                timeZone=tz_name,
            )
            .execute()
        )

        events = events_result.get("items", [])
        results = []
        for event in events:
            results.append(
                {
                    "event_id": event.get("id"),
                    "summary": event.get("summary", "(No title)"),
                    "start": event["start"].get("dateTime", event["start"].get("date")),
                    "end": event["end"].get("dateTime", event["end"].get("date")),
                    "location": event.get("location", ""),
                    "html_link": event.get("htmlLink", ""),
                    "attendees": [a.get("email") for a in event.get("attendees", [])],
                }
            )

        return json.dumps(
            {
                "status": "ok",
                "timezone": tz_name,
                "total": len(results),
                "events": results,
            }
        )
    except Exception as exc:
        logger.exception("Failed to list calendar events")
        return json.dumps({"status": "error", "message": f"Failed to list events: {exc}"})


@mcp.tool()
def list_events_today(
    timezone_str: str = DEFAULT_TIMEZONE,
) -> str:
    """List today's Google Calendar events in the requested timezone."""
    service = _get_service()
    tz_name = _ensure_timezone_name(timezone_str)

    try:
        time_min, time_max = _today_range_iso(tz_name)

        events_result = (
            service.events()
            .list(
                calendarId="primary",
                timeMin=time_min,
                timeMax=time_max,
                maxResults=100,
                singleEvents=True,
                orderBy="startTime",
                timeZone=tz_name,
            )
            .execute()
        )

        events = events_result.get("items", [])
        results = []
        for event in events:
            results.append(
                {
                    "event_id": event.get("id"),
                    "summary": event.get("summary", "(No title)"),
                    "start": event["start"].get("dateTime", event["start"].get("date")),
                    "end": event["end"].get("dateTime", event["end"].get("date")),
                    "location": event.get("location", ""),
                    "html_link": event.get("htmlLink", ""),
                    "attendees": [a.get("email") for a in event.get("attendees", [])],
                }
            )

        return json.dumps(
            {
                "status": "ok",
                "timezone": tz_name,
                "time_min": time_min,
                "time_max": time_max,
                "total": len(results),
                "events": results,
            }
        )
    except Exception as exc:
        logger.exception("Failed to list today's calendar events")
        return json.dumps({"status": "error", "message": f"Failed to list today's events: {exc}"})


@mcp.tool()
def delete_event(event_id: str) -> str:
    """Delete a Google Calendar event by event_id."""
    service = _get_service()

    clean_event_id = (event_id or "").strip()
    if not clean_event_id:
        return json.dumps({"status": "error", "message": "event_id is required"})

    try:
        service.events().delete(calendarId="primary", eventId=clean_event_id).execute()
        return json.dumps({"status": "deleted", "event_id": clean_event_id})
    except Exception as exc:
        logger.exception("Failed to delete calendar event")
        return json.dumps({"status": "error", "message": f"Failed to delete event: {exc}"})


@mcp.tool()
def delete_event_by_title(
    summary: str,
    time_min: str = "",
    time_max: str = "",
    timezone_str: str = DEFAULT_TIMEZONE,
) -> str:
    """Delete the best matching Google Calendar event by title."""
    service = _get_service()
    tz_name = _ensure_timezone_name(timezone_str)

    clean_summary = (summary or "").strip()
    if not clean_summary:
        return json.dumps({"status": "error", "message": "summary is required"})

    try:
        matches = _find_events_by_title(
            service=service,
            summary=clean_summary,
            time_min=time_min,
            time_max=time_max,
            timezone_str=tz_name,
        )

        if not matches:
            return json.dumps(
                {
                    "status": "not_found",
                    "message": f"No event found matching '{clean_summary}'",
                    "timezone": tz_name,
                }
            )

        match = _best_matching_event(matches, tz_name)
        if not match:
            return json.dumps(
                {
                    "status": "not_found",
                    "message": f"No event found matching '{clean_summary}'",
                    "timezone": tz_name,
                }
            )

        service.events().delete(calendarId="primary", eventId=match["id"]).execute()

        return json.dumps(
            {
                "status": "deleted",
                "event_id": match.get("id"),
                "summary": match.get("summary", "(No title)"),
                "start": match.get("start", {}).get(
                    "dateTime", match.get("start", {}).get("date")
                ),
                "end": match.get("end", {}).get(
                    "dateTime", match.get("end", {}).get("date")
                ),
                "timezone": tz_name,
            }
        )
    except Exception as exc:
        logger.exception("Failed to delete calendar event by title")
        return json.dumps({"status": "error", "message": f"Failed to delete event by title: {exc}"})


@mcp.tool()
def get_event_by_title(
    summary: str,
    time_min: str = "",
    time_max: str = "",
    timezone_str: str = DEFAULT_TIMEZONE,
) -> str:
    """Get the best matching Google Calendar event by title."""
    service = _get_service()
    tz_name = _ensure_timezone_name(timezone_str)

    if not (summary or "").strip():
        return json.dumps({"status": "error", "message": "summary is required"})

    try:
        matches = _find_events_by_title(
            service=service,
            summary=summary,
            time_min=time_min,
            time_max=time_max,
            timezone_str=tz_name,
        )

        if not matches:
            return json.dumps(
                {
                    "status": "not_found",
                    "message": f"No event found matching '{summary}'",
                }
            )

        event = _best_matching_event(matches, tz_name)
        if not event:
            return json.dumps(
                {
                    "status": "not_found",
                    "message": f"No event found matching '{summary}'",
                }
            )

        return json.dumps(
            {
                "status": "ok",
                "event": {
                    "event_id": event.get("id"),
                    "summary": event.get("summary", "(No title)"),
                    "start": event.get("start", {}).get("dateTime", event.get("start", {}).get("date")),
                    "end": event.get("end", {}).get("dateTime", event.get("end", {}).get("date")),
                    "location": event.get("location", ""),
                    "html_link": event.get("htmlLink", ""),
                    "attendees": [a.get("email") for a in event.get("attendees", [])],
                },
            }
        )
    except Exception as exc:
        logger.exception("Failed to get event by title")
        return json.dumps({"status": "error", "message": f"Failed to get event: {exc}"})


@mcp.tool()
def update_event_time(
    event_id: str,
    new_start_time: str,
    new_end_time: str,
    timezone_str: str = DEFAULT_TIMEZONE,
) -> str:
    """Reschedule an existing Google Calendar event by event_id."""
    service = _get_service()
    tz_name = _ensure_timezone_name(timezone_str)

    clean_event_id = (event_id or "").strip()
    if not clean_event_id:
        return json.dumps({"status": "error", "message": "event_id is required"})

    try:
        start_iso = _ensure_aware_iso(new_start_time, tz_name)
        end_iso = _ensure_aware_iso(new_end_time, tz_name)

        start_dt = datetime.fromisoformat(start_iso)
        end_dt = datetime.fromisoformat(end_iso)

        if end_dt <= start_dt:
            return json.dumps({"status": "error", "message": "new_end_time must be after new_start_time"})

        overlapping_events = _find_overlapping_events(
            service=service,
            start_iso=start_iso,
            end_iso=end_iso,
            timezone_str=tz_name,
            exclude_event_id=clean_event_id,
        )

        if overlapping_events:
            return json.dumps(
                {
                    "status": "error",
                    "message": "The selected time conflicts with another event.",
                    "conflicting_events": overlapping_events[:10],
                }
            )

        existing = service.events().get(calendarId="primary", eventId=clean_event_id).execute()

        existing["start"] = {"dateTime": start_iso, "timeZone": tz_name}
        existing["end"] = {"dateTime": end_iso, "timeZone": tz_name}

        updated = service.events().update(
            calendarId="primary",
            eventId=clean_event_id,
            body=existing,
        ).execute()

        return json.dumps(
            {
                "status": "updated",
                "event_id": updated.get("id"),
                "summary": updated.get("summary", "(No title)"),
                "start": updated.get("start", {}).get("dateTime", ""),
                "end": updated.get("end", {}).get("dateTime", ""),
                "html_link": updated.get("htmlLink", ""),
                "attendees": [a.get("email") for a in updated.get("attendees", [])],
            }
        )
    except Exception as exc:
        logger.exception("Failed to update event time")
        return json.dumps({"status": "error", "message": f"Failed to update event: {exc}"})


@mcp.tool()
def check_availability(
    time_min: str,
    time_max: str,
    timezone_str: str = DEFAULT_TIMEZONE,
) -> str:
    """Check whether the user is free or busy for a given time range."""
    service = _get_service()
    tz_name = _ensure_timezone_name(timezone_str)

    try:
        time_min_iso = _ensure_aware_iso(time_min, tz_name)
        time_max_iso = _ensure_aware_iso(time_max, tz_name)

        start_dt = datetime.fromisoformat(time_min_iso)
        end_dt = datetime.fromisoformat(time_max_iso)

        if end_dt <= start_dt:
            return json.dumps(
                {
                    "status": "error",
                    "message": "time_max must be after time_min",
                    "time_min": time_min_iso,
                    "time_max": time_max_iso,
                    "timezone": tz_name,
                }
            )

        overlapping_events = _find_overlapping_events(
            service=service,
            start_iso=time_min_iso,
            end_iso=time_max_iso,
            timezone_str=tz_name,
        )

        return json.dumps(
            {
                "status": "ok",
                "timezone": tz_name,
                "time_min": time_min_iso,
                "time_max": time_max_iso,
                "is_free": len(overlapping_events) == 0,
                "busy_slots": overlapping_events,
            }
        )
    except Exception as exc:
        logger.exception("Failed to check availability")
        return json.dumps({"status": "error", "message": f"Failed to check availability: {exc}"})


@mcp.tool()
def check_availability_today(
    start_time: str,
    end_time: str,
    timezone_str: str = DEFAULT_TIMEZONE,
) -> str:
    """Check availability for today's date using HH:MM or ISO-like local times."""
    tz_name = _ensure_timezone_name(timezone_str)
    tz = ZoneInfo(tz_name)

    try:
        today = datetime.now(tz).date()

        if "T" in start_time:
            start_iso = _ensure_aware_iso(start_time, tz_name)
        else:
            start_iso = datetime.fromisoformat(
                f"{today.isoformat()}T{start_time}"
            ).replace(tzinfo=tz).isoformat()

        if "T" in end_time:
            end_iso = _ensure_aware_iso(end_time, tz_name)
        else:
            end_iso = datetime.fromisoformat(
                f"{today.isoformat()}T{end_time}"
            ).replace(tzinfo=tz).isoformat()

        return check_availability(
            time_min=start_iso,
            time_max=end_iso,
            timezone_str=tz_name,
        )
    except Exception as exc:
        logger.exception("Failed to check today's availability")
        return json.dumps({"status": "error", "message": f"Failed to check today's availability: {exc}"})


@mcp.tool()
def find_free_slots(
    date: str,
    duration_minutes: int = 60,
    working_hours_start: str = "09:00",
    working_hours_end: str = "17:00",
    timezone_str: str = DEFAULT_TIMEZONE,
) -> str:
    """Find available time slots on a given date."""
    service = _get_service()
    tz_name = _ensure_timezone_name(timezone_str)

    try:
        tz = ZoneInfo(tz_name)
        day_start = datetime.fromisoformat(f"{date}T{working_hours_start}:00").replace(
            tzinfo=tz
        )
        day_end = datetime.fromisoformat(f"{date}T{working_hours_end}:00").replace(
            tzinfo=tz
        )

        if day_end <= day_start:
            return json.dumps(
                {
                    "status": "error",
                    "message": "working_hours_end must be after working_hours_start",
                }
            )

        safe_duration = max(15, min(duration_minutes, 24 * 60))
        duration = timedelta(minutes=safe_duration)

        body = {
            "timeMin": day_start.isoformat(),
            "timeMax": day_end.isoformat(),
            "timeZone": tz_name,
            "items": [{"id": "primary"}],
        }
        result = service.freebusy().query(body=body).execute()
        busy_slots = result.get("calendars", {}).get("primary", {}).get("busy", [])

        busy_ranges = []
        for slot in busy_slots:
            busy_start = datetime.fromisoformat(
                slot["start"].replace("Z", "+00:00")
            ).astimezone(tz)
            busy_end = datetime.fromisoformat(
                slot["end"].replace("Z", "+00:00")
            ).astimezone(tz)
            busy_ranges.append((busy_start, busy_end))

        busy_ranges.sort(key=lambda item: item[0])

        free_slots = []
        current = day_start

        while current + duration <= day_end:
            slot_end = current + duration
            overlaps = False

            for busy_start, busy_end in busy_ranges:
                if current < busy_end and slot_end > busy_start:
                    overlaps = True
                    current = max(current + timedelta(minutes=30), busy_end)
                    break

            if not overlaps:
                free_slots.append(
                    {
                        "start": current.isoformat(),
                        "end": slot_end.isoformat(),
                    }
                )
                current += timedelta(minutes=30)

        return json.dumps(
            {
                "status": "ok",
                "date": date,
                "timezone": tz_name,
                "duration_minutes": safe_duration,
                "free_slots": free_slots[:10],
            }
        )
    except Exception as exc:
        logger.exception("Failed to find free slots")
        return json.dumps({"status": "error", "message": f"Failed to find free slots: {exc}"})


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    mcp.run(transport="stdio")