"""MCP Server wrapping Gmail API.

Run standalone:
    python -m app.mcp.gmail_server

Used by ADK agents via McpToolset with StdioConnectionParams.
"""

import base64
import json
import logging
from email.mime.text import MIMEText
from typing import Any, Dict, List, Tuple

from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)

mcp = FastMCP("Gmail")


def _get_service():
    from app.services.google_auth import get_gmail_service
    return get_gmail_service()


def _safe_header_map(msg: Dict[str, Any]) -> Dict[str, str]:
    headers = msg.get("payload", {}).get("headers", [])
    return {
        h["name"]: h["value"]
        for h in headers
        if "name" in h and "value" in h
    }


def _clean_csv_emails(value: str) -> str:
    if not value:
        return ""
    parts = [item.strip() for item in value.split(",") if item.strip()]
    return ", ".join(parts)


def _normalize_text(value: str) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _extract_body_from_payload(part: Dict[str, Any]) -> str:
    body = part.get("body", {})
    data = body.get("data")
    mime_type = part.get("mimeType", "")

    if data and mime_type == "text/plain":
        return base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")

    for sub_part in part.get("parts", []):
        result = _extract_body_from_payload(sub_part)
        if result:
            return result

    if data and mime_type == "text/html":
        return base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")

    return ""


def _validate_email_fields(
    to: str,
    subject: str,
    body: str,
    cc: str = "",
    bcc: str = "",
) -> Tuple[str, str, str, str, str, str]:
    clean_to = _clean_csv_emails(_normalize_text(to))
    clean_cc = _clean_csv_emails(_normalize_text(cc))
    clean_bcc = _clean_csv_emails(_normalize_text(bcc))
    clean_subject = _normalize_text(subject)
    clean_body = "" if body is None else str(body).strip()

    if not clean_to:
        return "", "", "", "", "", json.dumps(
            {"status": "error", "message": "Recipient 'to' is required"}
        )

    if not clean_subject:
        return "", "", "", "", "", json.dumps(
            {"status": "error", "message": "Email subject is required"}
        )

    return clean_to, clean_cc, clean_bcc, clean_subject, clean_body, ""


def _build_mime_message(
    to: str,
    subject: str,
    body: str,
    cc: str = "",
    bcc: str = "",
) -> MIMEText:
    message = MIMEText(body, "plain", "utf-8")
    message["to"] = to
    message["subject"] = subject
    if cc:
        message["cc"] = cc
    if bcc:
        message["bcc"] = bcc
    return message


@mcp.tool()
def send_email(
    to: str,
    subject: str,
    body: str,
    cc: str = "",
    bcc: str = "",
) -> str:
    """Send an email via Gmail."""
    service = _get_service()

    clean_to, clean_cc, clean_bcc, clean_subject, clean_body, error = _validate_email_fields(
        to=to,
        subject=subject,
        body=body,
        cc=cc,
        bcc=bcc,
    )
    if error:
        return error

    try:
        logger.info(
            "Sending email | to=%s | cc=%s | bcc=%s | subject=%s | body_preview=%s",
            clean_to,
            clean_cc,
            clean_bcc,
            clean_subject,
            clean_body.replace("\n", " ")[:200],
        )

        message = _build_mime_message(
            to=clean_to,
            subject=clean_subject,
            body=clean_body,
            cc=clean_cc,
            bcc=clean_bcc,
        )

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
        result = service.users().messages().send(
            userId="me",
            body={"raw": raw},
        ).execute()

        return json.dumps(
            {
                "status": "sent",
                "message_id": result.get("id"),
                "thread_id": result.get("threadId"),
                "to": clean_to,
                "cc": clean_cc,
                "bcc": clean_bcc,
                "subject": clean_subject,
                "body_preview": clean_body.replace("\n", " ")[:200],
            }
        )
    except Exception as exc:
        logger.exception("Failed to send email")
        return json.dumps(
            {"status": "error", "message": f"Failed to send email: {exc}"}
        )


@mcp.tool()
def draft_email(
    to: str,
    subject: str,
    body: str,
    cc: str = "",
    bcc: str = "",
) -> str:
    """Create an email draft in Gmail using only the values explicitly passed to this tool."""
    service = _get_service()

    clean_to, clean_cc, clean_bcc, clean_subject, clean_body, error = _validate_email_fields(
        to=to,
        subject=subject,
        body=body,
        cc=cc,
        bcc=bcc,
    )
    if error:
        return error

    try:
        logger.info(
            "Creating draft email | to=%s | cc=%s | bcc=%s | subject=%s | body_preview=%s",
            clean_to,
            clean_cc,
            clean_bcc,
            clean_subject,
            clean_body.replace("\n", " ")[:200],
        )

        message = _build_mime_message(
            to=clean_to,
            subject=clean_subject,
            body=clean_body,
            cc=clean_cc,
            bcc=clean_bcc,
        )

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
        draft = service.users().drafts().create(
            userId="me",
            body={"message": {"raw": raw}},
        ).execute()

        return json.dumps(
            {
                "status": "drafted",
                "draft_id": draft.get("id"),
                "message": "Draft created successfully using the provided input fields.",
                "to": clean_to,
                "cc": clean_cc,
                "bcc": clean_bcc,
                "subject": clean_subject,
                "body_preview": clean_body.replace("\n", " ")[:200],
            }
        )
    except Exception as exc:
        logger.exception("Failed to draft email")
        return json.dumps(
            {"status": "error", "message": f"Failed to draft email: {exc}"}
        )


@mcp.tool()
def read_emails(
    max_results: int = 5,
    query: str = "is:unread",
    label: str = "INBOX",
) -> str:
    """Read emails from Gmail."""
    service = _get_service()
    safe_max_results = max(1, min(max_results, 50))

    try:
        logger.info(
            "Reading emails with query=%s label=%s max_results=%s",
            query,
            label,
            safe_max_results,
        )

        results = (
            service.users()
            .messages()
            .list(
                userId="me",
                q=query or "",
                labelIds=[label] if label else None,
                maxResults=safe_max_results,
            )
            .execute()
        )

        messages = results.get("messages", [])
        if not messages:
            return json.dumps({"status": "ok", "total": 0, "emails": []})

        emails: List[Dict[str, Any]] = []

        for msg_ref in messages:
            try:
                msg = (
                    service.users()
                    .messages()
                    .get(
                        userId="me",
                        id=msg_ref["id"],
                        format="metadata",
                        metadataHeaders=["Subject", "From", "To", "Date"],
                    )
                    .execute()
                )

                headers = _safe_header_map(msg)
                emails.append(
                    {
                        "message_id": msg.get("id"),
                        "thread_id": msg.get("threadId"),
                        "subject": headers.get("Subject", "(No subject)"),
                        "from": headers.get("From", ""),
                        "to": headers.get("To", ""),
                        "date": headers.get("Date", ""),
                        "snippet": msg.get("snippet", ""),
                        "labels": msg.get("labelIds", []),
                    }
                )
            except Exception as exc:
                logger.exception(
                    "Failed to fetch Gmail metadata for message %s",
                    msg_ref.get("id"),
                )
                emails.append(
                    {
                        "message_id": msg_ref.get("id", ""),
                        "thread_id": "",
                        "subject": "(Failed to load)",
                        "from": "",
                        "to": "",
                        "date": "",
                        "snippet": f"Failed to load message metadata: {exc}",
                        "labels": [],
                    }
                )

        return json.dumps({"status": "ok", "total": len(emails), "emails": emails})
    except Exception as exc:
        logger.exception("Failed to read emails")
        return json.dumps(
            {"status": "error", "message": f"Failed to read emails: {exc}"}
        )


@mcp.tool()
def search_emails(query: str, max_results: int = 5) -> str:
    """Search Gmail with a query."""
    logger.info("Searching emails with query=%s", query)
    return read_emails(max_results=max_results, query=query, label="")


@mcp.tool()
def read_email_content(message_id: str) -> str:
    """Read the full content of a specific email."""
    service = _get_service()

    if not (message_id or "").strip():
        return json.dumps({"status": "error", "message": "message_id is required"})

    try:
        logger.info("Reading full content for message_id=%s", message_id)

        msg = (
            service.users()
            .messages()
            .get(userId="me", id=message_id.strip(), format="full")
            .execute()
        )

        headers = _safe_header_map(msg)
        payload = msg.get("payload", {})
        body_text = _extract_body_from_payload(payload)

        if not body_text and "data" in payload.get("body", {}):
            body_text = base64.urlsafe_b64decode(
                payload["body"]["data"]
            ).decode("utf-8", errors="ignore")

        return json.dumps(
            {
                "status": "ok",
                "message_id": message_id.strip(),
                "subject": headers.get("Subject", ""),
                "from": headers.get("From", ""),
                "to": headers.get("To", ""),
                "date": headers.get("Date", ""),
                "body": body_text,
            }
        )
    except Exception as exc:
        logger.exception("Failed to read email content")
        return json.dumps(
            {"status": "error", "message": f"Failed to read email content: {exc}"}
        )


@mcp.tool()
def reply_to_email(
    message_id: str,
    body: str,
) -> str:
    """Reply to an existing email thread."""
    service = _get_service()

    if not (message_id or "").strip():
        return json.dumps({"status": "error", "message": "message_id is required"})

    try:
        logger.info("Replying to message_id=%s", message_id)

        original = (
            service.users()
            .messages()
            .get(
                userId="me",
                id=message_id.strip(),
                format="metadata",
                metadataHeaders=["Subject", "From", "To", "Message-ID"],
            )
            .execute()
        )

        headers = _safe_header_map(original)
        thread_id = original.get("threadId")
        original_subject = headers.get("Subject", "").strip()

        reply = MIMEText(body or "", "plain", "utf-8")
        reply["to"] = headers.get("From", "")
        reply["subject"] = (
            original_subject
            if original_subject.startswith("Re:")
            else f"Re: {original_subject}"
        )
        if headers.get("Message-ID"):
            reply["In-Reply-To"] = headers.get("Message-ID", "")
            reply["References"] = headers.get("Message-ID", "")

        raw = base64.urlsafe_b64encode(reply.as_bytes()).decode("utf-8")
        result = service.users().messages().send(
            userId="me",
            body={"raw": raw, "threadId": thread_id},
        ).execute()

        return json.dumps(
            {
                "status": "replied",
                "message_id": result.get("id"),
                "thread_id": result.get("threadId"),
                "to": headers.get("From", ""),
                "subject": reply["subject"],
            }
        )
    except Exception as exc:
        logger.exception("Failed to reply to email")
        return json.dumps({"status": "error", "message": f"Failed to reply: {exc}"})


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    mcp.run(transport="stdio")