"""MCP Server wrapping Google Docs API.

Run standalone:
    python -m app.mcp.docs_server

Used by ADK agents via McpToolset with StdioConnectionParams.
"""

import json
import logging

from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)

mcp = FastMCP("GoogleDocs")


def _get_docs_service():
    from app.services.google_auth import get_docs_service
    return get_docs_service()


def _get_drive_service():
    from app.services.google_auth import get_drive_service
    return get_drive_service()


def _escape_drive_query(text: str) -> str:
    return text.replace("\\", "\\\\").replace("'", "\\'")


def _extract_doc_text(doc: dict) -> str:
    lines = []

    for element in doc.get("body", {}).get("content", []):
        paragraph = element.get("paragraph")
        if not paragraph:
            continue

        para_text = []
        for text_run in paragraph.get("elements", []):
            run = text_run.get("textRun")
            if run and "content" in run:
                para_text.append(run["content"])

        joined = "".join(para_text)
        if joined:
            lines.append(joined)

    return "".join(lines).strip()


@mcp.tool()
def create_document(title: str, body_content: str = "") -> str:
    """Create a new Google Docs document."""
    docs_service = _get_docs_service()

    clean_title = (title or "").strip()
    if not clean_title:
        return json.dumps({"status": "error", "message": "title is required"})

    try:
        doc = docs_service.documents().create(body={"title": clean_title}).execute()
        doc_id = doc.get("documentId")
        doc_url = f"https://docs.google.com/document/d/{doc_id}/edit"

        if body_content:
            requests = [
                {
                    "insertText": {
                        "location": {"index": 1},
                        "text": body_content,
                    }
                }
            ]
            docs_service.documents().batchUpdate(
                documentId=doc_id, body={"requests": requests}
            ).execute()

        return json.dumps(
            {
                "status": "created",
                "document_id": doc_id,
                "title": clean_title,
                "url": doc_url,
            }
        )
    except Exception as exc:
        logger.exception("Failed to create Google Doc")
        return json.dumps(
            {"status": "error", "message": f"Failed to create document: {exc}"}
        )


@mcp.tool()
def read_document(document_id: str) -> str:
    """Read the content of a Google Docs document."""
    docs_service = _get_docs_service()

    if not (document_id or "").strip():
        return json.dumps({"status": "error", "message": "document_id is required"})

    try:
        doc = docs_service.documents().get(documentId=document_id.strip()).execute()
        content = _extract_doc_text(doc)

        return json.dumps(
            {
                "status": "ok",
                "document_id": document_id.strip(),
                "title": doc.get("title", ""),
                "content": content,
            }
        )
    except Exception as exc:
        logger.exception("Failed to read Google Doc")
        return json.dumps({"status": "error", "message": f"Failed to read document: {exc}"})


@mcp.tool()
def append_content(document_id: str, text: str) -> str:
    """Append text content to the end of a Google Docs document."""
    docs_service = _get_docs_service()

    clean_document_id = (document_id or "").strip()
    append_text = text or ""

    if not clean_document_id:
        return json.dumps({"status": "error", "message": "document_id is required"})
    if not append_text:
        return json.dumps({"status": "error", "message": "text is required"})

    try:
        doc = docs_service.documents().get(documentId=clean_document_id).execute()
        body_content = doc.get("body", {}).get("content", [])

        end_index = 1
        if body_content:
            end_index = max(
                1,
                max(
                    (
                        element.get("endIndex", 1)
                        for element in body_content
                        if isinstance(element, dict)
                    ),
                    default=1,
                )
                - 1,
            )

        requests = [
            {
                "insertText": {
                    "location": {"index": end_index},
                    "text": "\n" + append_text,
                }
            }
        ]
        docs_service.documents().batchUpdate(
            documentId=clean_document_id, body={"requests": requests}
        ).execute()

        return json.dumps(
            {
                "status": "updated",
                "document_id": clean_document_id,
                "appended_text_length": len(append_text),
            }
        )
    except Exception as exc:
        logger.exception("Failed to append content to Google Doc")
        return json.dumps(
            {"status": "error", "message": f"Failed to append content: {exc}"}
        )


@mcp.tool()
def list_documents(max_results: int = 10, query: str = "") -> str:
    """List recent Google Docs documents from Google Drive."""
    drive_service = _get_drive_service()

    safe_max_results = max(1, min(max_results, 50))
    q = "mimeType='application/vnd.google-apps.document' and trashed=false"

    if query:
        q += f" and fullText contains '{_escape_drive_query(query)}'"

    try:
        results = (
            drive_service.files()
            .list(
                q=q,
                pageSize=safe_max_results,
                fields="files(id, name, modifiedTime, webViewLink)",
                orderBy="modifiedTime desc",
            )
            .execute()
        )

        files = results.get("files", [])
        docs = []
        for file_obj in files:
            docs.append(
                {
                    "document_id": file_obj["id"],
                    "title": file_obj["name"],
                    "last_modified": file_obj.get("modifiedTime", ""),
                    "url": file_obj.get("webViewLink", ""),
                }
            )

        return json.dumps({"status": "ok", "total": len(docs), "documents": docs})
    except Exception as exc:
        logger.exception("Failed to list Google Docs")
        return json.dumps(
            {"status": "error", "message": f"Failed to list documents: {exc}"}
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    mcp.run(transport="stdio")