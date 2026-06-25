"""Canvas LMS MCP server.

Exposes a set of read-only tools that let Claude query the Canvas LMS REST API
(courses, assignments, grades, calendar events and announcements) over a local
MCP (stdio) connection.

Run with either:

    fastmcp run canvas_mcp/server.py
    python -m canvas_mcp.server
"""

from __future__ import annotations

import asyncio
import io
import mimetypes
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Union

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.utilities.types import Image
from youtube_transcript_api import YouTubeTranscriptApi

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Load variables from a .env file (if present) into the environment. We look
# for .env next to the project root (the parent of this package) so the server
# finds it no matter what working directory it is launched from (e.g. when an
# MCP client like Claude Desktop / Claude Code spawns it). Real environment
# variables always take precedence over .env values.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")
load_dotenv()  # fallback: also honour a .env in the current working directory

CANVAS_API_TOKEN: Optional[str] = os.getenv("CANVAS_API_TOKEN")
CANVAS_BASE_URL: Optional[str] = os.getenv("CANVAS_BASE_URL")

if not CANVAS_API_TOKEN:
    raise RuntimeError(
        "CANVAS_API_TOKEN is not set. Create a .env file (copy .env.example) and "
        "add a line like 'CANVAS_API_TOKEN=your_token_here'. See the README for "
        "how to generate a Canvas access token."
    )

if not CANVAS_BASE_URL:
    raise RuntimeError(
        "CANVAS_BASE_URL is not set. Create a .env file (copy .env.example) and "
        "add a line like 'CANVAS_BASE_URL=https://gatech.instructure.com'."
    )

# Normalise the base URL and build the versioned API root, e.g.
# "https://gatech.instructure.com" -> "https://gatech.instructure.com/api/v1"
API_BASE: str = CANVAS_BASE_URL.rstrip("/") + "/api/v1"

HEADERS: dict[str, str] = {
    "Authorization": f"Bearer {CANVAS_API_TOKEN}",
    "Accept": "application/json",
}

REQUEST_TIMEOUT = httpx.Timeout(30.0)
# File uploads can be large/slow, so they get a longer timeout.
UPLOAD_TIMEOUT = httpx.Timeout(120.0)

# Canvas limits the calendar API to at most 10 context codes per request.
MAX_CONTEXT_CODES = 10


def _env_truthy(value: Optional[str]) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


# Safety switch for write operations (posting, replying, submitting). Disabled
# by default — read-only. Set CANVAS_ENABLE_WRITES=true in the environment/.env
# (and restart the MCP client) to allow the server to modify Canvas.
WRITES_ENABLED: bool = _env_truthy(os.getenv("CANVAS_ENABLE_WRITES", "false"))

WRITES_DISABLED_MESSAGE = (
    "Error: Write operations are disabled. This server is read-only until you "
    "opt in. Set CANVAS_ENABLE_WRITES=true in your .env file, then fully restart "
    "Claude Desktop / your MCP client, and try again."
)

mcp = FastMCP("Canvas")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class CanvasError(Exception):
    """Raised internally to carry a human-readable Canvas failure message."""


def strip_html(value: Optional[str]) -> str:
    """Return the plain-text content of an HTML string.

    Canvas returns rich-text fields (assignment descriptions, announcement
    bodies, ...) as HTML. We never want raw HTML reaching Claude, so every such
    field is run through this helper.
    """

    if not value:
        return ""
    text = BeautifulSoup(value, "html.parser").get_text(separator=" ", strip=True)
    return text


def _as_list(payload: Any) -> list[Any]:
    """Normalise a JSON payload into a list of records."""

    if isinstance(payload, list):
        return payload
    if payload is None:
        return []
    return [payload]


def _http_error_message(exc: httpx.HTTPStatusError) -> str:
    """Build a clear, Claude-friendly error string for a non-2xx response."""

    status = exc.response.status_code
    reason = exc.response.reason_phrase or ""
    detail = ""
    try:
        body = exc.response.json()
        if isinstance(body, dict):
            errors = body.get("errors") or body.get("message")
            if errors:
                detail = f" Details: {errors}"
    except Exception:  # noqa: BLE001 - body may not be JSON
        detail = ""
    return (
        f"Error: Canvas API request to {exc.request.url} failed with HTTP "
        f"{status} {reason}.{detail}"
    )


def _request_error_message(exc: httpx.RequestError) -> str:
    return f"Error: Could not reach Canvas at {exc.request.url!s} ({exc!s})."


async def _paginate(path: str, params: Optional[dict[str, Any]] = None) -> list[Any]:
    """Fetch every page of a Canvas list endpoint.

    Canvas paginates with an RFC 5988 ``Link`` header. ``httpx`` parses that
    header into ``response.links``; we follow ``rel="next"`` until it is absent
    and return the fully combined list. Raises :class:`CanvasError` on failure.
    """

    merged: dict[str, Any] = {"per_page": 100}
    if params:
        merged.update(params)

    results: list[Any] = []
    try:
        async with httpx.AsyncClient(
            base_url=API_BASE, headers=HEADERS, timeout=REQUEST_TIMEOUT
        ) as client:
            response = await client.get(path, params=merged)
            response.raise_for_status()
            results.extend(_as_list(response.json()))

            next_url = response.links.get("next", {}).get("url")
            while next_url:
                # The "next" link is an absolute URL that already carries its
                # own query string, so we pass it through verbatim.
                response = await client.get(next_url)
                response.raise_for_status()
                results.extend(_as_list(response.json()))
                next_url = response.links.get("next", {}).get("url")
    except httpx.HTTPStatusError as exc:
        raise CanvasError(_http_error_message(exc)) from exc
    except httpx.RequestError as exc:
        raise CanvasError(_request_error_message(exc)) from exc

    return results


async def _get_one(path: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Fetch a single Canvas object. Raises :class:`CanvasError` on failure."""

    try:
        async with httpx.AsyncClient(
            base_url=API_BASE, headers=HEADERS, timeout=REQUEST_TIMEOUT
        ) as client:
            response = await client.get(path, params=params or {})
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as exc:
        raise CanvasError(_http_error_message(exc)) from exc
    except httpx.RequestError as exc:
        raise CanvasError(_request_error_message(exc)) from exc


async def _write_request(
    method: str, path: str, data: Optional[dict[str, Any]] = None
) -> dict[str, Any]:
    """Perform a state-changing Canvas request (POST/PUT) and return its JSON.

    This is the single choke-point for all writes: it refuses to run unless
    ``WRITES_ENABLED`` is set, so no write tool can bypass the safety toggle.
    Raises :class:`CanvasError` on failure.
    """

    if not WRITES_ENABLED:
        raise CanvasError(WRITES_DISABLED_MESSAGE)

    try:
        async with httpx.AsyncClient(
            base_url=API_BASE, headers=HEADERS, timeout=REQUEST_TIMEOUT
        ) as client:
            response = await client.request(method, path, data=data)
            response.raise_for_status()
            if not response.content:
                return {}
            return response.json()
    except httpx.HTTPStatusError as exc:
        raise CanvasError(_http_error_message(exc)) from exc
    except httpx.RequestError as exc:
        raise CanvasError(_request_error_message(exc)) from exc


async def _upload_submission_file(
    course_id: str, assignment_id: str, file_path: str
) -> Any:
    """Upload a local file for an assignment submission; return its Canvas file id.

    Implements Canvas's 3-step upload flow: (1) tell Canvas about the file and
    get a pre-signed upload target, (2) POST the bytes to that target with NO
    Canvas auth header (the file field must come last), (3) read back the new
    file id, following a redirect if inst-fs returns one. Raises
    :class:`CanvasError` on failure.
    """

    path = Path(file_path).expanduser()
    if not path.is_file():
        raise CanvasError(f"Error: file not found at {file_path!s}.")

    size = path.stat().st_size
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"

    try:
        async with httpx.AsyncClient(
            base_url=API_BASE, headers=HEADERS, timeout=UPLOAD_TIMEOUT
        ) as client:
            # Step 1 — register the upload with Canvas.
            init = await client.post(
                f"/courses/{course_id}/assignments/{assignment_id}"
                "/submissions/self/files",
                data={
                    "name": path.name,
                    "size": str(size),
                    "content_type": content_type,
                },
            )
            init.raise_for_status()
            info = init.json()
            upload_url = info.get("upload_url")
            upload_params = info.get("upload_params") or {}
            if not upload_url:
                raise CanvasError(
                    "Error: Canvas did not return a file upload URL."
                )

            # Step 2 — upload the bytes to the pre-signed target. No auth header,
            # and the file field must be sent last (after the upload params).
            async with httpx.AsyncClient(
                timeout=UPLOAD_TIMEOUT, follow_redirects=False
            ) as uploader:
                with path.open("rb") as handle:
                    upload = await uploader.post(
                        upload_url,
                        data={k: str(v) for k, v in upload_params.items()},
                        files={"file": (path.name, handle, content_type)},
                    )

            # Step 3 — inst-fs returns either the file JSON directly, or a 3xx
            # redirect to a confirmation endpoint we must GET (with auth).
            location = upload.headers.get("location")
            if upload.status_code in (301, 302, 303) and location:
                confirm = await client.get(location)
                confirm.raise_for_status()
                return confirm.json().get("id")
            upload.raise_for_status()
            return upload.json().get("id")
    except httpx.HTTPStatusError as exc:
        raise CanvasError(_http_error_message(exc)) from exc
    except httpx.RequestError as exc:
        raise CanvasError(_request_error_message(exc)) from exc


def _enrollment_type(course: dict[str, Any]) -> Optional[str]:
    """Pull the current user's enrollment type out of a course record."""

    enrollments = course.get("enrollments") or []
    if enrollments:
        # e.g. "StudentEnrollment" -> "student"
        raw = enrollments[0].get("type") or enrollments[0].get("role")
        return raw
    return None


async def _active_courses() -> list[dict[str, Any]]:
    """Return the raw course records for the user's active enrollments."""

    return await _paginate(
        "/courses",
        {"enrollment_state": "active", "include[]": "term"},
    )


def _chunk(items: list[Any], size: int) -> list[list[Any]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _build_discussion_entry(
    raw: dict[str, Any], participants: dict[Any, Optional[str]]
) -> dict[str, Any]:
    """Recursively shape a discussion entry from the topic ``/view`` payload.

    The ``/view`` endpoint returns entries with ``user_id`` (resolved against a
    separate participants list) and a nested ``replies`` array, so this builds
    the full threaded tree with plain-text messages.
    """

    deleted = raw.get("deleted")
    return {
        "id": raw.get("id"),
        "author": participants.get(raw.get("user_id")),
        "created_at": raw.get("created_at"),
        "message": "[deleted]" if deleted else strip_html(raw.get("message")),
        "replies": [
            _build_discussion_entry(reply, participants)
            for reply in (raw.get("replies") or [])
        ],
    }


# Matches a YouTube video id inside the common embed/link URL shapes Canvas
# produces (watch?v=, youtu.be/, /embed/, /v/, /shorts/, nocookie variant).
_YOUTUBE_ID_RE = re.compile(
    r"(?:youtube(?:-nocookie)?\.com/(?:watch\?(?:[^ ]*&)?v=|embed/|v/|shorts/)"
    r"|youtu\.be/)([\w-]{11})"
)


def _extract_youtube_id(url: Optional[str]) -> Optional[str]:
    """Pull the 11-char video id out of any YouTube URL, or None."""

    if not url:
        return None
    match = _YOUTUBE_ID_RE.search(url)
    return match.group(1) if match else None


def _harvest_media(html: Optional[str]) -> dict[str, list[dict[str, Any]]]:
    """Extract embedded images and YouTube videos from a raw HTML field.

    Canvas rich-text is stored as HTML; ``strip_html`` would discard every
    ``<img>`` and ``<iframe>``. This runs on the *raw* HTML first so embedded
    media is recoverable: images carry their Canvas ``file_id`` (from the src or
    ``data-api-endpoint``) for use with ``get_file_image``; YouTube embeds carry
    their video id for use with ``get_youtube_transcript``.
    """

    if not html:
        return {"images": [], "youtube": []}

    soup = BeautifulSoup(html, "html.parser")

    images: list[dict[str, Any]] = []
    seen_images: set[str] = set()
    for img in soup.find_all("img"):
        src = img.get("src")
        file_id: Optional[str] = None
        for candidate in (img.get("data-api-endpoint"), src):
            if candidate:
                match = re.search(r"/files/(\d+)", candidate)
                if match:
                    file_id = match.group(1)
                    break
        key = file_id or src
        if not key or key in seen_images:
            continue
        seen_images.add(key)
        images.append(
            {
                "file_id": file_id,
                "alt": (img.get("alt") or "").strip(),
                "src": src,
                "canvas_hosted": file_id is not None,
            }
        )

    youtube: list[dict[str, Any]] = []
    seen_videos: set[str] = set()
    for tag in soup.find_all(["iframe", "a"]):
        video_id = _extract_youtube_id(tag.get("src") or tag.get("href"))
        if video_id and video_id not in seen_videos:
            seen_videos.add(video_id)
            youtube.append(
                {
                    "video_id": video_id,
                    "url": f"https://www.youtube.com/watch?v={video_id}",
                }
            )

    return {"images": images, "youtube": youtube}


def _fetch_youtube_transcript_sync(
    video_id: str, languages: tuple[str, ...]
) -> dict[str, Any]:
    """Blocking transcript fetch (the library uses ``requests`` under the hood).

    Always call via ``asyncio.to_thread`` so the event loop is never blocked.
    """

    fetched = YouTubeTranscriptApi().fetch(video_id, languages=list(languages))
    return {
        "language": getattr(fetched, "language", None),
        "language_code": getattr(fetched, "language_code", None),
        "is_generated": getattr(fetched, "is_generated", None),
        "snippets": [
            {"text": snippet.text, "start": snippet.start}
            for snippet in fetched
        ],
    }


def _parse_page_range(spec: Optional[str], total: int) -> list[int]:
    """Turn a 1-indexed inclusive range like ``"1-5"`` or ``"3"`` into 0-based
    page indices, clamped to ``[0, total)``. ``None`` means all pages."""

    if not spec:
        return list(range(total))
    spec = spec.strip()
    if "-" in spec:
        start_str, _, end_str = spec.partition("-")
        start = int(start_str) if start_str.strip() else 1
        end = int(end_str) if end_str.strip() else total
    else:
        start = end = int(spec)
    start = max(1, start)
    end = min(total, end)
    return [i - 1 for i in range(start, end + 1)]


def _extract_pdf_text(data: bytes, page_range: Optional[str]) -> dict[str, Any]:
    """Extract text from a PDF (blocking; call via ``asyncio.to_thread``)."""

    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    total = len(reader.pages)
    indices = _parse_page_range(page_range, total)
    parts = [reader.pages[i].extract_text() or "" for i in indices]
    return {
        "text": "\n\n".join(parts).strip(),
        "total_pages": total,
        "pages_read": [i + 1 for i in indices],
    }


def _extract_docx_text(data: bytes) -> dict[str, Any]:
    """Extract text from a .docx file (blocking; call via ``asyncio.to_thread``)."""

    from docx import Document

    document = Document(io.BytesIO(data))
    paragraphs = [p.text for p in document.paragraphs if p.text and p.text.strip()]
    return {"text": "\n".join(paragraphs).strip(), "total_pages": None, "pages_read": None}


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def get_courses() -> Union[list[dict[str, Any]], str]:
    """List all active courses the user is enrolled in.

    Returns a list of objects with the course id, name, course code and the
    user's enrollment type (e.g. ``StudentEnrollment``). Returns an error
    string if the Canvas request fails.
    """

    try:
        courses = await _active_courses()
    except CanvasError as exc:
        return str(exc)

    result: list[dict[str, Any]] = []
    for course in courses:
        # Skip placeholder/restricted courses that lack a name.
        if not course.get("id"):
            continue
        result.append(
            {
                "id": course.get("id"),
                "name": course.get("name"),
                "course_code": course.get("course_code"),
                "enrollment_type": _enrollment_type(course),
            }
        )
    return result


@mcp.tool()
async def get_assignments(course_id: str) -> Union[list[dict[str, Any]], str]:
    """List all assignments for a given course.

    Args:
        course_id: The Canvas course id.

    Returns a list of objects with assignment id, name, due date (ISO 8601),
    points possible and submission types. Returns an error string on failure.
    """

    try:
        assignments = await _paginate(f"/courses/{course_id}/assignments")
    except CanvasError as exc:
        return str(exc)

    result: list[dict[str, Any]] = []
    for assignment in assignments:
        result.append(
            {
                "id": assignment.get("id"),
                "name": assignment.get("name"),
                "due_date": assignment.get("due_at"),
                "points_possible": assignment.get("points_possible"),
                "submission_types": assignment.get("submission_types", []),
            }
        )
    return result


@mcp.tool()
async def get_assignment_details(
    course_id: str, assignment_id: str
) -> Union[dict[str, Any], str]:
    """Return the full details of a single assignment.

    Args:
        course_id: The Canvas course id.
        assignment_id: The Canvas assignment id.

    The HTML description is stripped to plain text. Returns an error string on
    failure.
    """

    try:
        assignment = await _get_one(
            f"/courses/{course_id}/assignments/{assignment_id}"
        )
    except CanvasError as exc:
        return str(exc)

    return {
        "id": assignment.get("id"),
        "name": assignment.get("name"),
        "description": strip_html(assignment.get("description")),
        "due_date": assignment.get("due_at"),
        "points_possible": assignment.get("points_possible"),
        "unlock_date": assignment.get("unlock_at"),
        "lock_date": assignment.get("lock_at"),
        "submission_types": assignment.get("submission_types", []),
    }


@mcp.tool()
async def get_grades(course_id: str) -> Union[dict[str, Any], str]:
    """Return the user's current grade/score in a course plus graded submissions.

    Args:
        course_id: The Canvas course id.

    Returns an object containing the current grade and score for the course and
    a list of graded submissions (assignment name, score, points possible and
    letter grade where available). Returns an error string on failure.
    """

    try:
        enrollments = await _paginate(
            f"/courses/{course_id}/enrollments", {"user_id": "self"}
        )
        submissions = await _paginate(
            f"/courses/{course_id}/students/submissions",
            {"student_ids[]": "self", "include[]": "assignment"},
        )
    except CanvasError as exc:
        return str(exc)

    current_grade: Optional[str] = None
    current_score: Optional[float] = None
    for enrollment in enrollments:
        grades = enrollment.get("grades") or {}
        if grades:
            current_grade = grades.get("current_grade")
            current_score = grades.get("current_score")
            break

    graded: list[dict[str, Any]] = []
    for submission in submissions:
        if submission.get("score") is None and submission.get("workflow_state") != "graded":
            continue
        assignment = submission.get("assignment") or {}
        graded.append(
            {
                "assignment_name": assignment.get("name"),
                "score": submission.get("score"),
                "points_possible": assignment.get("points_possible"),
                "letter_grade": submission.get("grade"),
            }
        )

    return {
        "course_id": course_id,
        "current_grade": current_grade,
        "current_score": current_score,
        "graded_submissions": graded,
    }


@mcp.tool()
async def get_upcoming_events(days_ahead: int = 14) -> Union[list[dict[str, Any]], str]:
    """List upcoming calendar events and assignment due dates.

    Args:
        days_ahead: How many days into the future to look (default 14).

    Queries the Canvas calendar API for both assignment due dates and calendar
    events across the user's active courses, returning each item's title, type
    (``assignment`` or ``event``), course name (if applicable) and start/due
    datetime. Returns an error string on failure.
    """

    now = datetime.now(timezone.utc)
    end = now + timedelta(days=days_ahead)
    start_date = now.isoformat()
    end_date = end.isoformat()

    try:
        courses = await _active_courses()
    except CanvasError as exc:
        return str(exc)

    course_names: dict[str, str] = {
        f"course_{course.get('id')}": course.get("name")
        for course in courses
        if course.get("id")
    }
    context_codes = list(course_names.keys())

    events: list[dict[str, Any]] = []
    try:
        for event_type in ("assignment", "event"):
            # Canvas allows at most 10 context codes per calendar request.
            for batch in _chunk(context_codes, MAX_CONTEXT_CODES) or [[]]:
                params: dict[str, Any] = {
                    "type": event_type,
                    "start_date": start_date,
                    "end_date": end_date,
                    "context_codes[]": batch,
                }
                items = await _paginate("/calendar_events", params)
                for item in items:
                    context_code = item.get("context_code")
                    if event_type == "assignment":
                        assignment = item.get("assignment") or {}
                        when = item.get("start_at") or assignment.get("due_at")
                    else:
                        when = item.get("start_at")
                    events.append(
                        {
                            "title": item.get("title"),
                            "type": event_type,
                            "course_name": course_names.get(context_code),
                            "datetime": when,
                        }
                    )
    except CanvasError as exc:
        return str(exc)

    # Sort by datetime, pushing undated items to the end.
    events.sort(key=lambda e: (e["datetime"] is None, e["datetime"] or ""))
    return events


@mcp.tool()
async def get_announcements(course_id: str) -> Union[list[dict[str, Any]], str]:
    """Return the 10 most recent announcements for a course.

    Args:
        course_id: The Canvas course id.

    Each announcement includes its title, posted date and a plain-text body
    (HTML stripped). Returns an error string on failure.
    """

    try:
        announcements = await _paginate(
            f"/courses/{course_id}/discussion_topics",
            {"only_announcements": "true", "per_page": 10},
        )
    except CanvasError as exc:
        return str(exc)

    result: list[dict[str, Any]] = []
    for announcement in announcements[:10]:
        result.append(
            {
                "title": announcement.get("title"),
                "posted_date": announcement.get("posted_at")
                or announcement.get("created_at"),
                "body": strip_html(announcement.get("message")),
            }
        )
    return result


@mcp.tool()
async def get_current_user() -> Union[dict[str, Any], str]:
    """Return the authenticated Canvas user's profile.

    Useful for confirming your token works. Returns id, name, email and login
    id. Returns an error string on failure.
    """

    try:
        user = await _get_one("/users/self")
    except CanvasError as exc:
        return str(exc)

    return {
        "id": user.get("id"),
        "name": user.get("name"),
        "email": user.get("primary_email") or user.get("email"),
        "login_id": user.get("login_id"),
    }


@mcp.tool()
async def get_modules(course_id: str) -> Union[list[dict[str, Any]], str]:
    """List the modules for a course, including each module's items.

    Args:
        course_id: The Canvas course id.

    Returns a list of modules (id, name, state) each with its items. Every item
    includes all available links and ids: ``html_url`` (open it in Canvas),
    ``external_url`` (the real external website, for external-link/tool items),
    ``page_url`` (the slug to pass to ``get_page_content`` for Page items) and
    ``content_id`` — the id of the underlying object, NOT the module item id.
    Use ``content_id`` (with ``type``) to act on the item: for a ``File`` pass
    it to ``read_document`` / ``get_file_image``; for an ``Assignment`` to
    ``get_assignment_details`` / ``submit_assignment``; for a ``Quiz`` to
    ``get_quizzes`` results; for a ``Discussion`` to ``get_discussion_entries``.
    Returns an error string on failure.
    """

    try:
        modules = await _paginate(
            f"/courses/{course_id}/modules", {"include[]": "items"}
        )
    except CanvasError as exc:
        return str(exc)

    result: list[dict[str, Any]] = []
    for module in modules:
        items = [
            {
                "id": item.get("id"),
                "title": item.get("title"),
                "type": item.get("type"),
                "content_id": item.get("content_id"),
                "html_url": item.get("html_url"),
                "external_url": item.get("external_url"),
                "page_url": item.get("page_url"),
            }
            for item in (module.get("items") or [])
        ]
        result.append(
            {
                "id": module.get("id"),
                "name": module.get("name"),
                "state": module.get("state"),
                "items": items,
            }
        )
    return result


@mcp.tool()
async def search_course_content(
    course_id: str, search_term: str
) -> Union[dict[str, Any], str]:
    """Search a course's assignments and modules for a term.

    Args:
        course_id: The Canvas course id.
        search_term: Text to search for (matched by Canvas server-side).

    Returns an object with matching assignments and modules. Returns an error
    string on failure.
    """

    try:
        assignments = await _paginate(
            f"/courses/{course_id}/assignments", {"search_term": search_term}
        )
        modules = await _paginate(
            f"/courses/{course_id}/modules",
            {"search_term": search_term, "include[]": "items"},
        )
    except CanvasError as exc:
        return str(exc)

    return {
        "assignments": [
            {
                "id": a.get("id"),
                "name": a.get("name"),
                "due_date": a.get("due_at"),
            }
            for a in assignments
        ],
        "modules": [
            {"id": m.get("id"), "name": m.get("name")} for m in modules
        ],
    }


@mcp.tool()
async def get_course_details(course_id: str) -> Union[dict[str, Any], str]:
    """Return detailed information about a single course.

    Args:
        course_id: The Canvas course id.

    Includes the syllabus and public description (both HTML-stripped), term and
    start/end dates. Returns an error string on failure.
    """

    try:
        course = await _get_one(
            f"/courses/{course_id}",
            {
                "include[]": [
                    "syllabus_body",
                    "term",
                    "public_description",
                    "total_students",
                ]
            },
        )
    except CanvasError as exc:
        return str(exc)

    term = course.get("term") or {}
    return {
        "id": course.get("id"),
        "name": course.get("name"),
        "course_code": course.get("course_code"),
        "enrollment_type": _enrollment_type(course),
        "syllabus": strip_html(course.get("syllabus_body")),
        "public_description": strip_html(course.get("public_description")),
        "term": term.get("name"),
        "start_date": course.get("start_at") or term.get("start_at"),
        "end_date": course.get("end_at") or term.get("end_at"),
        "total_students": course.get("total_students"),
    }


@mcp.tool()
async def get_submission(
    course_id: str, assignment_id: str
) -> Union[dict[str, Any], str]:
    """Return the user's own submission for a single assignment.

    Args:
        course_id: The Canvas course id.
        assignment_id: The Canvas assignment id.

    Includes the score, letter grade, submission state, late/missing flags and
    any instructor feedback comments (HTML stripped). Returns an error string on
    failure.
    """

    try:
        submission = await _get_one(
            f"/courses/{course_id}/assignments/{assignment_id}/submissions/self",
            {"include[]": "submission_comments"},
        )
    except CanvasError as exc:
        return str(exc)

    comments = [
        {
            "author": comment.get("author_name"),
            "comment": strip_html(comment.get("comment")),
            "created_at": comment.get("created_at"),
        }
        for comment in (submission.get("submission_comments") or [])
    ]

    return {
        "assignment_id": submission.get("assignment_id"),
        "score": submission.get("score"),
        "grade": submission.get("grade"),
        "submitted_at": submission.get("submitted_at"),
        "workflow_state": submission.get("workflow_state"),
        "late": submission.get("late"),
        "missing": submission.get("missing"),
        "excused": submission.get("excused"),
        "attempt": submission.get("attempt"),
        "comments": comments,
    }


@mcp.tool()
async def get_discussion_topics(course_id: str) -> Union[list[dict[str, Any]], str]:
    """List the discussion topics for a course (excluding announcements).

    Args:
        course_id: The Canvas course id.

    Returns each topic's id, title, posted date, last reply date, reply count
    and a plain-text body (HTML stripped). Returns an error string on failure.
    """

    try:
        topics = await _paginate(f"/courses/{course_id}/discussion_topics")
    except CanvasError as exc:
        return str(exc)

    result: list[dict[str, Any]] = []
    for topic in topics:
        result.append(
            {
                "id": topic.get("id"),
                "title": topic.get("title"),
                "posted_date": topic.get("posted_at") or topic.get("created_at"),
                "last_reply_at": topic.get("last_reply_at"),
                "reply_count": topic.get("discussion_subentry_count"),
                "body": strip_html(topic.get("message")),
            }
        )
    return result


@mcp.tool()
async def get_discussion_entries(
    course_id: str, topic_id: str
) -> Union[list[dict[str, Any]], str]:
    """Return the full threaded posts of a discussion topic, with nested replies.

    Args:
        course_id: The Canvas course id.
        topic_id: The Canvas discussion topic id.

    Uses Canvas's discussion "view" so each top-level post includes a nested
    ``replies`` list (replies to replies included, recursively). Every entry has
    its id, author, created date and plain-text message (HTML stripped). Returns
    an error string on failure.
    """

    try:
        data = await _get_one(
            f"/courses/{course_id}/discussion_topics/{topic_id}/view"
        )
    except CanvasError as exc:
        return str(exc)

    participants = {
        person.get("id"): person.get("display_name")
        for person in (data.get("participants") or [])
    }

    return [
        _build_discussion_entry(entry, participants)
        for entry in (data.get("view") or [])
    ]


@mcp.tool()
async def get_overdue_assignments(course_id: str) -> Union[list[dict[str, Any]], str]:
    """List the overdue assignments for a course.

    Args:
        course_id: The Canvas course id.

    Uses Canvas's "overdue" bucket (past-due assignments that can still be
    submitted and have no graded submission). Returns id, name, due date,
    points possible and submission types. Returns an error string on failure.
    """

    try:
        assignments = await _paginate(
            f"/courses/{course_id}/assignments",
            {"bucket": "overdue", "include[]": "submission"},
        )
    except CanvasError as exc:
        return str(exc)

    result: list[dict[str, Any]] = []
    for assignment in assignments:
        result.append(
            {
                "id": assignment.get("id"),
                "name": assignment.get("name"),
                "due_date": assignment.get("due_at"),
                "points_possible": assignment.get("points_possible"),
                "submission_types": assignment.get("submission_types", []),
            }
        )
    return result


@mcp.tool()
async def get_rubric(
    course_id: str, rubric_id: str
) -> Union[dict[str, Any], str]:
    """Return a course rubric and its grading criteria.

    Args:
        course_id: The Canvas course id.
        rubric_id: The Canvas rubric id.

    Returns the rubric title, total points and each criterion (description,
    points and possible ratings), with all descriptions HTML-stripped. Returns
    an error string on failure.
    """

    try:
        rubric = await _get_one(f"/courses/{course_id}/rubrics/{rubric_id}")
    except CanvasError as exc:
        return str(exc)

    criteria = []
    for criterion in rubric.get("data") or []:
        ratings = [
            {
                "description": strip_html(rating.get("description")),
                "points": rating.get("points"),
            }
            for rating in (criterion.get("ratings") or [])
        ]
        criteria.append(
            {
                "description": strip_html(criterion.get("description")),
                "long_description": strip_html(criterion.get("long_description")),
                "points": criterion.get("points"),
                "ratings": ratings,
            }
        )

    return {
        "id": rubric.get("id"),
        "title": rubric.get("title"),
        "points_possible": rubric.get("points_possible"),
        "criteria": criteria,
    }


@mcp.tool()
async def get_pages(course_id: str) -> Union[list[dict[str, Any]], str]:
    """List the wiki/content pages in a course.

    Args:
        course_id: The Canvas course id.

    Returns each page's title, its ``page_url`` slug (pass to
    ``get_page_content`` to read the body), the Canvas ``html_url`` and the last
    updated date. Returns an error string on failure.
    """

    try:
        pages = await _paginate(f"/courses/{course_id}/pages")
    except CanvasError as exc:
        return str(exc)

    return [
        {
            "title": page.get("title"),
            "page_url": page.get("url"),
            "html_url": page.get("html_url"),
            "updated_at": page.get("updated_at"),
        }
        for page in pages
    ]


@mcp.tool()
async def get_page_content(
    course_id: str, page_url: str
) -> Union[dict[str, Any], str]:
    """Read the full text content of a single Canvas page.

    Args:
        course_id: The Canvas course id.
        page_url: The page's slug (the ``page_url`` from ``get_modules`` items
            or ``get_pages``). A numeric page id also works.

    Canvas pages are login-protected, so this fetches the page through the API
    with your token and returns the body as plain text (HTML stripped) — useful
    for pulling a module page's content directly into the conversation. Returns
    an error string on failure.
    """

    try:
        page = await _get_one(f"/courses/{course_id}/pages/{page_url}")
    except CanvasError as exc:
        return str(exc)

    return {
        "title": page.get("title"),
        "html_url": page.get("html_url"),
        "updated_at": page.get("updated_at"),
        "body": strip_html(page.get("body")),
    }


@mcp.tool()
async def get_todo() -> Union[list[dict[str, Any]], str]:
    """List the user's Canvas to-do items across all courses.

    These are the assignments/quizzes Canvas thinks need attention (e.g.
    upcoming things to submit). Returns each item's type, title, course id, due
    date, points and link. Returns an error string on failure.
    """

    try:
        items = await _paginate("/users/self/todo")
    except CanvasError as exc:
        return str(exc)

    result: list[dict[str, Any]] = []
    for item in items:
        assignment = item.get("assignment") or {}
        result.append(
            {
                "type": item.get("type"),
                "title": assignment.get("name") or item.get("type"),
                "course_id": item.get("course_id") or assignment.get("course_id"),
                "due_date": assignment.get("due_at"),
                "points_possible": assignment.get("points_possible"),
                "html_url": item.get("html_url") or assignment.get("html_url"),
            }
        )
    return result


@mcp.tool()
async def get_missing_submissions() -> Union[list[dict[str, Any]], str]:
    """List past-due assignments the user has not submitted, across all courses.

    Returns each assignment's id, name, course id, due date, points and link.
    Returns an error string on failure.
    """

    try:
        assignments = await _paginate("/users/self/missing_submissions")
    except CanvasError as exc:
        return str(exc)

    return [
        {
            "id": a.get("id"),
            "name": a.get("name"),
            "course_id": a.get("course_id"),
            "due_date": a.get("due_at"),
            "points_possible": a.get("points_possible"),
            "html_url": a.get("html_url"),
        }
        for a in assignments
    ]


# Submission types that don't represent an online action the student can take
# here (paper hand-ins, ungraded items, "no submission" placeholders).
_NON_ACTIONABLE_SUBMISSION_TYPES = {"none", "not_graded", "on_paper"}

# Human-readable description of each Canvas module completion requirement.
_REQUIREMENT_LABELS = {
    "must_submit": "Submit this item",
    "must_contribute": "Contribute (e.g. post/reply to a discussion)",
    "must_mark_done": "Mark as done",
    "must_view": "View this item",
    "min_score": "Score at least the required minimum",
}


def _module_item_key(item: dict[str, Any]) -> str:
    """Stable dedupe key for a module item, sharing a namespace with assignments.

    A module item that points at an assignment/quiz must collapse onto the same
    key the assignment source produces (``assignment:{id}``) so a gradable item
    with a ``must_submit`` requirement is listed once, not twice.
    """

    itype = item.get("type")
    content_id = item.get("content_id")
    if itype == "Assignment":
        return f"assignment:{content_id}"
    if itype == "Quiz":
        return f"quiz:{content_id}"
    if itype == "Discussion":
        return f"discussion:{content_id}"
    if itype == "Page":
        return f"page:{item.get('page_url') or content_id}"
    return f"item:{item.get('id')}"


async def _actionable_for_course(
    course_id: str, course_name: Optional[str]
) -> list[dict[str, Any]]:
    """Collect every still-actionable item in one course. Raises CanvasError.

    Two sources, deduped:
      1. Submittable assignments the student hasn't acted on yet (incl. ones
         with no due date — exactly what the calendar/planner miss).
      2. Module items whose completion requirement is not yet met (catches
         non-assignment pages/discussions the instructor marked as required).
    """

    items: list[dict[str, Any]] = []
    seen: set[str] = set()

    # --- Source 1: submittable, unsubmitted, unlocked assignments ----------
    assignments = await _paginate(
        f"/courses/{course_id}/assignments", {"include[]": "submission"}
    )
    for a in assignments:
        sub_types = a.get("submission_types") or []
        if not {t for t in sub_types if t not in _NON_ACTIONABLE_SUBMISSION_TYPES}:
            continue
        if a.get("locked_for_user"):
            continue
        submission = a.get("submission") or {}
        if submission.get("submitted_at") or submission.get("workflow_state") in {
            "submitted",
            "graded",
            "pending_review",
            "complete",
        }:
            continue
        key = f"assignment:{a.get('id')}"
        seen.add(key)
        items.append(
            {
                "course_id": course_id,
                "course_name": course_name,
                "source": "assignment",
                "title": a.get("name"),
                "type": ", ".join(sub_types),
                "what_to_do": f"Submit ({', '.join(sub_types)})",
                "due_date": a.get("due_at"),
                "points_possible": a.get("points_possible"),
                "assignment_id": a.get("id"),
                "html_url": a.get("html_url"),
            }
        )

    # --- Source 2: module items with an unmet completion requirement -------
    modules = await _paginate(
        f"/courses/{course_id}/modules",
        {"include[]": ["items", "content_details"]},
    )
    for module in modules:
        for item in module.get("items") or []:
            requirement = item.get("completion_requirement") or {}
            if not requirement or requirement.get("completed"):
                continue
            key = _module_item_key(item)
            if key in seen:
                continue
            seen.add(key)
            details = item.get("content_details") or {}
            req_type = requirement.get("type")
            items.append(
                {
                    "course_id": course_id,
                    "course_name": course_name,
                    "source": "module_item",
                    "module": module.get("name"),
                    "title": item.get("title"),
                    "type": item.get("type"),
                    "what_to_do": _REQUIREMENT_LABELS.get(req_type, req_type),
                    "due_date": details.get("due_at"),
                    "points_possible": details.get("points_possible"),
                    "content_id": item.get("content_id"),
                    "html_url": item.get("html_url"),
                }
            )

    return items


@mcp.tool()
async def get_actionable_items(
    course_id: Optional[str] = None,
) -> Union[list[dict[str, Any]], str]:
    """List everything you can still act on, including items with no due date.

    Args:
        course_id: Limit to one course. Omit to scan every active course.

    Surfaces work the calendar and planner miss because it isn't dated: open
    assignments you haven't submitted (even with no due date) and module items
    whose completion requirement (submit / contribute / mark-done / view) you
    haven't met yet. Each item carries ``what_to_do`` and a ``html_url`` to open
    it. Returns an error string on failure.
    """

    # Single-course mode: surface the course's error directly, like other
    # per-course tools.
    if course_id:
        try:
            courses = await _active_courses()
        except CanvasError:
            courses = []
        name = next(
            (c.get("name") for c in courses if str(c.get("id")) == str(course_id)),
            None,
        )
        try:
            return await _actionable_for_course(course_id, name)
        except CanvasError as exc:
            return str(exc)

    # Cross-course mode: don't let one restricted course abort the whole scan.
    try:
        courses = await _active_courses()
    except CanvasError as exc:
        return str(exc)

    result: list[dict[str, Any]] = []
    for course in courses:
        cid = course.get("id")
        if not cid:
            continue
        try:
            result.extend(await _actionable_for_course(str(cid), course.get("name")))
        except CanvasError as exc:
            result.append(
                {
                    "course_id": cid,
                    "course_name": course.get("name"),
                    "error": str(exc),
                }
            )
    return result


@mcp.tool()
async def get_planner(days_ahead: int = 14) -> Union[list[dict[str, Any]], str]:
    """List unified Canvas Planner items across all courses for the next N days.

    Args:
        days_ahead: How many days into the future to include (default 14).

    The Planner combines assignments, quizzes, discussions, calendar events and
    to-dos. Returns each item's type, title, course name, date, points and link.
    Returns an error string on failure.
    """

    now = datetime.now(timezone.utc)
    end = now + timedelta(days=days_ahead)

    try:
        items = await _paginate(
            "/planner/items",
            {"start_date": now.isoformat(), "end_date": end.isoformat()},
        )
    except CanvasError as exc:
        return str(exc)

    result: list[dict[str, Any]] = []
    for item in items:
        plannable = item.get("plannable") or {}
        result.append(
            {
                "type": item.get("plannable_type"),
                "title": plannable.get("title") or plannable.get("name"),
                "course_name": item.get("context_name"),
                "date": item.get("plannable_date")
                or plannable.get("due_at")
                or plannable.get("todo_date"),
                "points_possible": plannable.get("points_possible"),
                "html_url": item.get("html_url"),
            }
        )
    return result


@mcp.tool()
async def get_all_grades() -> Union[list[dict[str, Any]], str]:
    """Return the user's current grade and score in every active course.

    A dashboard view across all courses (vs. per-course ``get_grades``). Returns
    each course's id, name, current score and current grade. Returns an error
    string on failure.
    """

    try:
        courses = await _paginate(
            "/courses",
            {"enrollment_state": "active", "include[]": "total_scores"},
        )
    except CanvasError as exc:
        return str(exc)

    result: list[dict[str, Any]] = []
    for course in courses:
        if not course.get("id"):
            continue
        enrollments = course.get("enrollments") or []
        enrollment = enrollments[0] if enrollments else {}
        result.append(
            {
                "course_id": course.get("id"),
                "name": course.get("name"),
                "current_score": enrollment.get("computed_current_score"),
                "current_grade": enrollment.get("computed_current_grade"),
            }
        )
    return result


@mcp.tool()
async def get_quizzes(course_id: str) -> Union[list[dict[str, Any]], str]:
    """List the quizzes in a course.

    Args:
        course_id: The Canvas course id.

    Returns each quiz's id, title, due date, points, question count, time limit
    (minutes), allowed attempts, type and link. Returns an error string on
    failure.
    """

    try:
        quizzes = await _paginate(f"/courses/{course_id}/quizzes")
    except CanvasError as exc:
        return str(exc)

    return [
        {
            "id": q.get("id"),
            "title": q.get("title"),
            "due_date": q.get("due_at"),
            "points_possible": q.get("points_possible"),
            "question_count": q.get("question_count"),
            "time_limit": q.get("time_limit"),
            "allowed_attempts": q.get("allowed_attempts"),
            "quiz_type": q.get("quiz_type"),
            "html_url": q.get("html_url"),
        }
        for q in quizzes
    ]


@mcp.tool()
async def get_files(course_id: str) -> Union[list[dict[str, Any]], str]:
    """List the files available in a course.

    Args:
        course_id: The Canvas course id.

    Returns each file's id, name, content type, size (bytes), a download URL and
    created/updated dates. Returns an error string on failure (e.g. if the
    course's Files area is restricted).
    """

    try:
        files = await _paginate(f"/courses/{course_id}/files")
    except CanvasError as exc:
        return str(exc)

    return [
        {
            "id": f.get("id"),
            "name": f.get("display_name") or f.get("filename"),
            "content_type": f.get("content-type") or f.get("content_type"),
            "size": f.get("size"),
            "url": f.get("url"),
            "created_at": f.get("created_at"),
            "updated_at": f.get("updated_at"),
        }
        for f in files
    ]


@mcp.tool()
async def get_assignment_groups(
    course_id: str,
) -> Union[list[dict[str, Any]], str]:
    """List a course's assignment groups and their grade weights.

    Args:
        course_id: The Canvas course id.

    Shows how the final grade is weighted across categories (e.g. Homework 20%,
    Exams 50%). Returns each group's id, name, weight and its assignments.
    Returns an error string on failure.
    """

    try:
        groups = await _paginate(
            f"/courses/{course_id}/assignment_groups",
            {"include[]": "assignments"},
        )
    except CanvasError as exc:
        return str(exc)

    result: list[dict[str, Any]] = []
    for group in groups:
        assignments = [
            {
                "id": a.get("id"),
                "name": a.get("name"),
                "points_possible": a.get("points_possible"),
            }
            for a in (group.get("assignments") or [])
        ]
        result.append(
            {
                "id": group.get("id"),
                "name": group.get("name"),
                "group_weight": group.get("group_weight"),
                "assignments": assignments,
            }
        )
    return result


@mcp.tool()
async def get_course_roster(course_id: str) -> Union[list[dict[str, Any]], str]:
    """List the people enrolled in a course (classmates and instructors).

    Args:
        course_id: The Canvas course id.

    Returns each person's id, name and role. Returns an error string on failure
    (some courses restrict the roster to staff).
    """

    try:
        users = await _paginate(
            f"/courses/{course_id}/users", {"include[]": "enrollments"}
        )
    except CanvasError as exc:
        return str(exc)

    result: list[dict[str, Any]] = []
    for user in users:
        enrollments = user.get("enrollments") or []
        role = enrollments[0].get("type") if enrollments else None
        result.append(
            {
                "id": user.get("id"),
                "name": user.get("name"),
                "role": role,
            }
        )
    return result


@mcp.tool()
async def get_quiz_submissions(
    course_id: str, quiz_id: str
) -> Union[list[dict[str, Any]], str]:
    """Return the user's own attempts and scores for a quiz.

    Args:
        course_id: The Canvas course id.
        quiz_id: The quiz id (from ``get_quizzes``).

    Returns each attempt's number, score, kept score, state, attempts remaining
    and start/finish times — useful for reviewing how you did. Returns an error
    string on failure.
    """

    try:
        payload = await _get_one(
            f"/courses/{course_id}/quizzes/{quiz_id}/submissions"
        )
    except CanvasError as exc:
        return str(exc)

    submissions = payload.get("quiz_submissions") or []
    return [
        {
            "id": sub.get("id"),
            "attempt": sub.get("attempt"),
            "score": sub.get("score"),
            "kept_score": sub.get("kept_score"),
            "workflow_state": sub.get("workflow_state"),
            "attempts_left": sub.get("attempts_left"),
            "started_at": sub.get("started_at"),
            "finished_at": sub.get("finished_at"),
        }
        for sub in submissions
    ]


@mcp.tool()
async def get_groups(course_id: str) -> Union[list[dict[str, Any]], str]:
    """List the student groups in a course.

    Args:
        course_id: The Canvas course id.

    Returns each group's id, name, member count and description (HTML stripped).
    Returns an error string on failure.
    """

    try:
        groups = await _paginate(f"/courses/{course_id}/groups")
    except CanvasError as exc:
        return str(exc)

    return [
        {
            "id": g.get("id"),
            "name": g.get("name"),
            "members_count": g.get("members_count"),
            "description": strip_html(g.get("description")),
        }
        for g in groups
    ]


@mcp.tool()
async def get_conversations() -> Union[list[dict[str, Any]], str]:
    """List the user's Canvas inbox conversations (most recent first).

    Returns each conversation's id, subject, a plain-text preview of the last
    message, the last message time and the message count. Use
    ``get_conversation`` to read a full thread. Returns an error string on
    failure.
    """

    try:
        conversations = await _paginate("/conversations")
    except CanvasError as exc:
        return str(exc)

    return [
        {
            "id": c.get("id"),
            "subject": c.get("subject"),
            "last_message": strip_html(c.get("last_message")),
            "last_message_at": c.get("last_message_at"),
            "message_count": c.get("message_count"),
            "workflow_state": c.get("workflow_state"),
        }
        for c in conversations
    ]


@mcp.tool()
async def get_conversation(conversation_id: str) -> Union[dict[str, Any], str]:
    """Read a single Canvas inbox conversation thread.

    Args:
        conversation_id: The conversation id (from ``get_conversations``).

    Returns the subject, participants and every message (author, time and
    plain-text body). Returns an error string on failure.
    """

    try:
        convo = await _get_one(f"/conversations/{conversation_id}")
    except CanvasError as exc:
        return str(exc)

    names = {
        person.get("id"): person.get("name")
        for person in (convo.get("participants") or [])
    }
    messages = [
        {
            "author": names.get(msg.get("author_id")),
            "created_at": msg.get("created_at"),
            "body": strip_html(msg.get("body")),
        }
        for msg in (convo.get("messages") or [])
    ]
    return {
        "id": convo.get("id"),
        "subject": convo.get("subject"),
        "participants": list(names.values()),
        "messages": messages,
    }


@mcp.tool()
async def download_file(
    file_id: str, destination_path: str
) -> Union[dict[str, Any], str]:
    """Download a Canvas file to a local path on this machine.

    Args:
        file_id: The Canvas file id (from ``get_files`` or a module item).
        destination_path: Where to save the file on the local filesystem.

    This reads from Canvas and writes to your local disk — it does NOT modify
    anything in Canvas, so it works regardless of the write toggle. Returns the
    saved path and byte count, or an error string on failure.
    """

    try:
        meta = await _get_one(f"/files/{file_id}")
    except CanvasError as exc:
        return str(exc)

    url = meta.get("url")
    if not url:
        return "Error: Canvas did not return a download URL for this file."

    try:
        async with httpx.AsyncClient(
            headers=HEADERS, timeout=UPLOAD_TIMEOUT, follow_redirects=True
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
            content = response.content
    except httpx.HTTPStatusError as exc:
        return _http_error_message(exc)
    except httpx.RequestError as exc:
        return _request_error_message(exc)

    try:
        dest = Path(destination_path).expanduser()
        if dest.parent:
            dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
    except OSError as exc:
        return f"Error: could not write file to {destination_path!s}: {exc}"

    return {
        "status": "downloaded",
        "file_id": file_id,
        "name": meta.get("display_name") or meta.get("filename"),
        "path": str(dest),
        "bytes": len(content),
    }


# Cap image bytes returned inline so a huge file can't blow up the context.
_MAX_IMAGE_BYTES = 8 * 1024 * 1024  # 8 MB


@mcp.tool()
async def get_file_image(file_id: str) -> Union[Image, str]:
    """Fetch a Canvas image file and return it so Claude can actually see it.

    Args:
        file_id: The Canvas file id of an image (from ``get_files`` or a module
            item). Must point at an image file (jpeg/png/gif/webp/...).

    Unlike ``download_file`` (which saves to local disk), this returns the image
    inline as visual content Claude can look at and describe. Use it to read
    diagrams, screenshots or photos embedded in a course. Returns an error
    string if the file isn't an image, is too large, or can't be fetched.
    """

    try:
        meta = await _get_one(f"/files/{file_id}")
    except CanvasError as exc:
        return str(exc)

    content_type = (meta.get("content-type") or meta.get("content_type") or "").lower()
    if not content_type.startswith("image/"):
        return (
            f"Error: file {file_id} is not an image (content-type: "
            f"{content_type or 'unknown'}). Use download_file for non-image files."
        )

    size = meta.get("size")
    if isinstance(size, int) and size > _MAX_IMAGE_BYTES:
        return (
            f"Error: image is {size} bytes, larger than the "
            f"{_MAX_IMAGE_BYTES}-byte inline limit. Use download_file instead."
        )

    url = meta.get("url")
    if not url:
        return "Error: Canvas did not return a download URL for this file."

    try:
        async with httpx.AsyncClient(
            headers=HEADERS, timeout=REQUEST_TIMEOUT, follow_redirects=True
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
            data = response.content
    except httpx.HTTPStatusError as exc:
        return _http_error_message(exc)
    except httpx.RequestError as exc:
        return _request_error_message(exc)

    if len(data) > _MAX_IMAGE_BYTES:
        return (
            f"Error: image is {len(data)} bytes, larger than the "
            f"{_MAX_IMAGE_BYTES}-byte inline limit. Use download_file instead."
        )

    # e.g. "image/jpeg" -> "jpeg"; fall back to png if Canvas omits the subtype.
    image_format = content_type.split("/")[-1] or "png"
    return Image(data=data, format=image_format)


_MEDIA_HINT = (
    "View an image with get_file_image(file_id). Read a video with "
    "get_youtube_transcript(video_id)."
)


@mcp.tool()
async def get_page_media(course_id: str, page_url: str) -> Union[dict[str, Any], str]:
    """List images and YouTube videos embedded in a Canvas page.

    Args:
        course_id: The Canvas course id.
        page_url: The page slug (from ``get_modules`` item ``page_url`` or
            ``get_pages``).

    Canvas pages store rich text as HTML, so embedded media would be lost by the
    usual text extraction. This recovers it: each image includes its Canvas
    ``file_id`` (pass to ``get_file_image`` to see it) and each video includes
    its ``video_id`` (pass to ``get_youtube_transcript`` to read it). Returns an
    error string on failure.
    """

    try:
        page = await _get_one(f"/courses/{course_id}/pages/{page_url}")
    except CanvasError as exc:
        return str(exc)

    media = _harvest_media(page.get("body"))
    return {
        "page": page.get("title"),
        "images": media["images"],
        "youtube": media["youtube"],
        "hint": _MEDIA_HINT,
    }


@mcp.tool()
async def get_discussion_media(
    course_id: str, topic_id: str
) -> Union[dict[str, Any], str]:
    """List images and YouTube videos embedded in a discussion (prompt + posts).

    Args:
        course_id: The Canvas course id.
        topic_id: The Canvas discussion topic id.

    Scans the discussion prompt and every post/reply for embedded media, tagging
    each with where it was found and who posted it. Each image includes its
    ``file_id`` (for ``get_file_image``) and each video its ``video_id`` (for
    ``get_youtube_transcript``). Returns an error string on failure.
    """

    try:
        topic = await _get_one(f"/courses/{course_id}/discussion_topics/{topic_id}")
        data = await _get_one(
            f"/courses/{course_id}/discussion_topics/{topic_id}/view"
        )
    except CanvasError as exc:
        return str(exc)

    participants = {
        person.get("id"): person.get("display_name")
        for person in (data.get("participants") or [])
    }

    # (location, author, raw_html) for the prompt and every non-deleted entry.
    sources: list[tuple[str, Optional[str], Optional[str]]] = [
        ("prompt", None, topic.get("message"))
    ]

    def walk(entries: list[dict[str, Any]]) -> None:
        for entry in entries:
            if not entry.get("deleted"):
                sources.append(
                    (
                        f"post {entry.get('id')}",
                        participants.get(entry.get("user_id")),
                        entry.get("message"),
                    )
                )
            walk(entry.get("replies") or [])

    walk(data.get("view") or [])

    images: list[dict[str, Any]] = []
    youtube: list[dict[str, Any]] = []
    for location, author, html in sources:
        media = _harvest_media(html)
        for image in media["images"]:
            images.append({**image, "found_in": location, "author": author})
        for video in media["youtube"]:
            youtube.append({**video, "found_in": location, "author": author})

    return {
        "topic": topic.get("title"),
        "images": images,
        "youtube": youtube,
        "hint": _MEDIA_HINT,
    }


@mcp.tool()
async def get_assignment_media(
    course_id: str, assignment_id: str
) -> Union[dict[str, Any], str]:
    """List images and YouTube videos embedded in an assignment description.

    Args:
        course_id: The Canvas course id.
        assignment_id: The assignment id (from ``get_assignments``).

    Assignment instructions are stored as HTML, so embedded media would be lost
    by the usual text extraction. This recovers it: each image includes its
    Canvas ``file_id`` (for ``get_file_image``) and each video its ``video_id``
    (for ``get_youtube_transcript``). Returns an error string on failure.
    """

    try:
        assignment = await _get_one(
            f"/courses/{course_id}/assignments/{assignment_id}"
        )
    except CanvasError as exc:
        return str(exc)

    media = _harvest_media(assignment.get("description"))
    return {
        "assignment": assignment.get("name"),
        "images": media["images"],
        "youtube": media["youtube"],
        "hint": _MEDIA_HINT,
    }


@mcp.tool()
async def get_youtube_transcript(
    video: str, languages: Optional[list[str]] = None
) -> Union[dict[str, Any], str]:
    """Fetch the transcript/captions of a YouTube video.

    Args:
        video: A YouTube video id or any YouTube URL (watch, youtu.be, embed).
        languages: Preferred language codes in order (default ``["en"]``). The
            first available is returned; falls back to auto-generated captions.

    Use this to read a lecture/video embedded in a course (find video ids with
    ``get_page_media`` / ``get_discussion_media``). Returns the joined transcript
    text plus timestamped segments, or an error string if the video has no
    captions or can't be reached.
    """

    video_id = _extract_youtube_id(video) or video.strip()
    if not video_id:
        return f"Error: could not determine a YouTube video id from '{video}'."

    preferred = tuple(languages) if languages else ("en",)
    try:
        result = await asyncio.to_thread(
            _fetch_youtube_transcript_sync, video_id, preferred
        )
    except Exception as exc:  # noqa: BLE001 - library raises many specific types
        return (
            f"Error: could not fetch a transcript for video '{video_id}'. "
            f"{type(exc).__name__}: {exc}"
        )

    snippets = result["snippets"]
    return {
        "video_id": video_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "language": result["language"],
        "language_code": result["language_code"],
        "auto_generated": result["is_generated"],
        "segment_count": len(snippets),
        "transcript": " ".join(s["text"] for s in snippets).strip(),
        "segments": snippets,
    }


# Limits so a giant file can't blow up the download or the context window.
_MAX_DOCUMENT_BYTES = 25 * 1024 * 1024  # 25 MB download cap
_MAX_DOCUMENT_CHARS = 100_000  # returned-text cap


@mcp.tool()
async def read_document(
    file_id: str, pages: Optional[str] = None
) -> Union[dict[str, Any], str]:
    """Extract the text of a Canvas document (PDF, Word .docx, or plain text).

    Args:
        file_id: The Canvas file id (from ``get_files`` or a module item).
        pages: For PDFs only, a 1-indexed inclusive page range like ``"1-5"`` or
            ``"3"``. Omit to read the whole document.

    Use this to read a textbook chapter, handout or syllabus PDF so you can
    summarise or study it — unlike ``download_file`` (which only saves bytes to
    disk), this returns the actual text. Long text is truncated to keep the
    response manageable (``truncated: true`` flags this). Returns an error string
    if the file is missing, too large, or an unsupported type.
    """

    try:
        meta = await _get_one(f"/files/{file_id}")
    except CanvasError as exc:
        return str(exc)

    name = meta.get("display_name") or meta.get("filename") or ""
    content_type = (meta.get("content-type") or meta.get("content_type") or "").lower()
    extension = name.lower().rsplit(".", 1)[-1] if "." in name else ""

    size = meta.get("size")
    if isinstance(size, int) and size > _MAX_DOCUMENT_BYTES:
        return (
            f"Error: '{name}' is {size} bytes, larger than the "
            f"{_MAX_DOCUMENT_BYTES}-byte limit. Use download_file instead."
        )

    url = meta.get("url")
    if not url:
        return "Error: Canvas did not return a download URL for this file."

    try:
        async with httpx.AsyncClient(
            headers=HEADERS, timeout=UPLOAD_TIMEOUT, follow_redirects=True
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
            data = response.content
    except httpx.HTTPStatusError as exc:
        return _http_error_message(exc)
    except httpx.RequestError as exc:
        return _request_error_message(exc)

    is_pdf = content_type == "application/pdf" or extension == "pdf"
    is_docx = (
        extension == "docx"
        or content_type
        == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    is_text = content_type.startswith("text/") or extension in {
        "txt",
        "md",
        "markdown",
        "csv",
    }

    try:
        if is_pdf:
            extracted = await asyncio.to_thread(_extract_pdf_text, data, pages)
        elif is_docx:
            extracted = await asyncio.to_thread(_extract_docx_text, data)
        elif is_text:
            extracted = {
                "text": data.decode("utf-8", errors="replace").strip(),
                "total_pages": None,
                "pages_read": None,
            }
        else:
            return (
                f"Error: '{name}' has unsupported type '{content_type or extension}'. "
                "Supported: PDF, Word (.docx), and plain text. Use download_file "
                "for anything else, or get_file_image for images."
            )
    except Exception as exc:  # noqa: BLE001 - parsers raise varied errors
        return f"Error: could not read '{name}'. {type(exc).__name__}: {exc}"

    text = extracted["text"]
    truncated = len(text) > _MAX_DOCUMENT_CHARS
    return {
        "file_id": file_id,
        "name": name,
        "content_type": content_type,
        "total_pages": extracted["total_pages"],
        "pages_read": extracted["pages_read"],
        "truncated": truncated,
        "text": text[:_MAX_DOCUMENT_CHARS],
    }


# ---------------------------------------------------------------------------
# Write tools (gated behind CANVAS_ENABLE_WRITES — read-only by default)
# ---------------------------------------------------------------------------
#
# Every tool below changes Canvas. They refuse to run unless
# CANVAS_ENABLE_WRITES is truthy, and the underlying _write_request helper
# enforces the same gate as a second line of defence. Writes are NOT idempotent
# — a retried call can double-post or double-submit.


@mcp.tool()
async def post_discussion_entry(
    course_id: str, topic_id: str, message: str
) -> Union[dict[str, Any], str]:
    """Post a new top-level entry to a discussion topic. (Write operation.)

    Args:
        course_id: The Canvas course id.
        topic_id: The discussion topic id.
        message: The body of the post (plain text or HTML).

    Requires CANVAS_ENABLE_WRITES=true. Returns the created entry, or an error
    string on failure / when writes are disabled.
    """

    if not WRITES_ENABLED:
        return WRITES_DISABLED_MESSAGE

    try:
        result = await _write_request(
            "POST",
            f"/courses/{course_id}/discussion_topics/{topic_id}/entries",
            {"message": message},
        )
    except CanvasError as exc:
        return str(exc)

    return {
        "status": "posted",
        "id": result.get("id"),
        "created_at": result.get("created_at"),
        "message": strip_html(result.get("message")),
    }


@mcp.tool()
async def reply_to_discussion_entry(
    course_id: str, topic_id: str, entry_id: str, message: str
) -> Union[dict[str, Any], str]:
    """Reply to an existing post within a discussion topic. (Write operation.)

    Args:
        course_id: The Canvas course id.
        topic_id: The discussion topic id.
        entry_id: The id of the post you are replying to (from
            ``get_discussion_entries``).
        message: The reply body (plain text or HTML).

    Requires CANVAS_ENABLE_WRITES=true. Returns the created reply, or an error
    string on failure / when writes are disabled.
    """

    if not WRITES_ENABLED:
        return WRITES_DISABLED_MESSAGE

    try:
        result = await _write_request(
            "POST",
            f"/courses/{course_id}/discussion_topics/{topic_id}"
            f"/entries/{entry_id}/replies",
            {"message": message},
        )
    except CanvasError as exc:
        return str(exc)

    return {
        "status": "posted",
        "id": result.get("id"),
        "parent_id": entry_id,
        "created_at": result.get("created_at"),
        "message": strip_html(result.get("message")),
    }


@mcp.tool()
async def create_discussion_topic(
    course_id: str, title: str, message: str
) -> Union[dict[str, Any], str]:
    """Create a new discussion topic in a course. (Write operation.)

    Args:
        course_id: The Canvas course id.
        title: The topic title.
        message: The opening body of the topic (plain text or HTML).

    Requires CANVAS_ENABLE_WRITES=true. Returns the created topic, or an error
    string on failure / when writes are disabled.
    """

    if not WRITES_ENABLED:
        return WRITES_DISABLED_MESSAGE

    try:
        result = await _write_request(
            "POST",
            f"/courses/{course_id}/discussion_topics",
            {"title": title, "message": message},
        )
    except CanvasError as exc:
        return str(exc)

    return {
        "status": "created",
        "id": result.get("id"),
        "title": result.get("title"),
        "html_url": result.get("html_url"),
    }


@mcp.tool()
async def submit_assignment(
    course_id: str,
    assignment_id: str,
    submission_type: str,
    text: Optional[str] = None,
    url: Optional[str] = None,
    file_path: Optional[str] = None,
) -> Union[dict[str, Any], str]:
    """Submit an assignment on the user's behalf. (Write operation.)

    Args:
        course_id: The Canvas course id.
        assignment_id: The assignment id.
        submission_type: One of ``online_text_entry``, ``online_url`` or
            ``online_upload``.
        text: Required for ``online_text_entry`` — the submission body.
        url: Required for ``online_url`` — the website URL to submit.
        file_path: Required for ``online_upload`` — an absolute path to a local
            file to upload and submit.

    Requires CANVAS_ENABLE_WRITES=true. The assignment must actually accept the
    chosen submission type. Returns the resulting submission, or an error string
    on failure / when writes are disabled.
    """

    # Gate first — before any file-upload work begins.
    if not WRITES_ENABLED:
        return WRITES_DISABLED_MESSAGE

    valid_types = {"online_text_entry", "online_url", "online_upload"}
    if submission_type not in valid_types:
        return (
            f"Error: submission_type must be one of {sorted(valid_types)}, "
            f"got {submission_type!r}."
        )

    data: dict[str, Any] = {"submission[submission_type]": submission_type}
    try:
        if submission_type == "online_text_entry":
            if not text:
                return "Error: 'text' is required for online_text_entry."
            data["submission[body]"] = text
        elif submission_type == "online_url":
            if not url:
                return "Error: 'url' is required for online_url."
            data["submission[url]"] = url
        else:  # online_upload
            if not file_path:
                return "Error: 'file_path' is required for online_upload."
            file_id = await _upload_submission_file(
                course_id, assignment_id, file_path
            )
            if not file_id:
                return "Error: file upload did not return a file id."
            data["submission[file_ids][]"] = str(file_id)

        result = await _write_request(
            "POST",
            f"/courses/{course_id}/assignments/{assignment_id}/submissions",
            data,
        )
    except CanvasError as exc:
        return str(exc)

    return {
        "status": "submitted",
        "assignment_id": result.get("assignment_id"),
        "submitted_at": result.get("submitted_at"),
        "workflow_state": result.get("workflow_state"),
        "submission_type": result.get("submission_type"),
        "preview_url": result.get("preview_url"),
    }


@mcp.tool()
async def post_submission_comment(
    course_id: str, assignment_id: str, comment: str
) -> Union[dict[str, Any], str]:
    """Add a comment to the user's own submission for an assignment. (Write.)

    Args:
        course_id: The Canvas course id.
        assignment_id: The assignment id.
        comment: The comment text.

    Requires CANVAS_ENABLE_WRITES=true. Returns a confirmation, or an error
    string on failure / when writes are disabled.
    """

    if not WRITES_ENABLED:
        return WRITES_DISABLED_MESSAGE

    try:
        result = await _write_request(
            "PUT",
            f"/courses/{course_id}/assignments/{assignment_id}/submissions/self",
            {"comment[text_comment]": comment},
        )
    except CanvasError as exc:
        return str(exc)

    return {
        "status": "commented",
        "assignment_id": result.get("assignment_id"),
        "comment": comment,
    }


@mcp.tool()
async def delete_discussion_entry(
    course_id: str, topic_id: str, entry_id: str
) -> Union[dict[str, Any], str]:
    """Delete one of your own discussion posts or replies. (Write operation.)

    Args:
        course_id: The Canvas course id.
        topic_id: The discussion topic id.
        entry_id: The id of the entry to delete (from
            ``get_discussion_entries``). You can only delete your own posts.

    Requires CANVAS_ENABLE_WRITES=true. This is irreversible. Returns a
    confirmation, or an error string on failure / when writes are disabled.
    """

    if not WRITES_ENABLED:
        return WRITES_DISABLED_MESSAGE

    try:
        result = await _write_request(
            "DELETE",
            f"/courses/{course_id}/discussion_topics/{topic_id}"
            f"/entries/{entry_id}",
        )
    except CanvasError as exc:
        return str(exc)

    return {
        "status": "deleted",
        "id": entry_id,
        "deleted": result.get("deleted", True),
    }


@mcp.tool()
async def edit_discussion_entry(
    course_id: str, topic_id: str, entry_id: str, message: str
) -> Union[dict[str, Any], str]:
    """Edit the text of one of your own discussion posts/replies. (Write.)

    Args:
        course_id: The Canvas course id.
        topic_id: The discussion topic id.
        entry_id: The id of the entry to edit (from
            ``get_discussion_entries``). You can only edit your own posts.
        message: The new message body (replaces the old one).

    Requires CANVAS_ENABLE_WRITES=true. Returns the updated entry, or an error
    string on failure / when writes are disabled.
    """

    if not WRITES_ENABLED:
        return WRITES_DISABLED_MESSAGE

    try:
        result = await _write_request(
            "PUT",
            f"/courses/{course_id}/discussion_topics/{topic_id}"
            f"/entries/{entry_id}",
            {"message": message},
        )
    except CanvasError as exc:
        return str(exc)

    return {
        "status": "edited",
        "id": result.get("id") or entry_id,
        "updated_at": result.get("updated_at"),
        "message": strip_html(result.get("message")),
    }


@mcp.tool()
async def mark_module_item_done(
    course_id: str, module_id: str, item_id: str
) -> Union[dict[str, Any], str]:
    """Mark a module item as done (for modules with completion tracking). (Write.)

    Args:
        course_id: The Canvas course id.
        module_id: The module id (from ``get_modules``).
        item_id: The module item id (from ``get_modules``).

    Requires CANVAS_ENABLE_WRITES=true. Only works on items the course lets you
    manually mark complete. Returns a confirmation, or an error string on
    failure / when writes are disabled.
    """

    if not WRITES_ENABLED:
        return WRITES_DISABLED_MESSAGE

    try:
        await _write_request(
            "PUT",
            f"/courses/{course_id}/modules/{module_id}/items/{item_id}/done",
        )
    except CanvasError as exc:
        return str(exc)

    return {
        "status": "marked_done",
        "module_id": module_id,
        "item_id": item_id,
    }


@mcp.tool()
async def create_calendar_event(
    title: str,
    start_at: str,
    end_at: Optional[str] = None,
    description: Optional[str] = None,
) -> Union[dict[str, Any], str]:
    """Create a personal event on the user's Canvas calendar. (Write operation.)

    Args:
        title: The event title.
        start_at: Start datetime in ISO 8601 (e.g. ``2026-06-25T14:00:00Z``).
        end_at: Optional end datetime in ISO 8601.
        description: Optional event description.

    Requires CANVAS_ENABLE_WRITES=true. Creates the event on your own user
    calendar. Returns the created event, or an error string on failure / when
    writes are disabled.
    """

    if not WRITES_ENABLED:
        return WRITES_DISABLED_MESSAGE

    try:
        me = await _get_one("/users/self")
        data: dict[str, Any] = {
            "calendar_event[context_code]": f"user_{me.get('id')}",
            "calendar_event[title]": title,
            "calendar_event[start_at]": start_at,
        }
        if end_at:
            data["calendar_event[end_at]"] = end_at
        if description:
            data["calendar_event[description]"] = description
        result = await _write_request("POST", "/calendar_events", data)
    except CanvasError as exc:
        return str(exc)

    return {
        "status": "created",
        "id": result.get("id"),
        "title": result.get("title"),
        "start_at": result.get("start_at"),
        "end_at": result.get("end_at"),
        "html_url": result.get("html_url"),
    }


@mcp.tool()
async def send_message(
    recipient_ids: list[str], body: str, subject: Optional[str] = None
) -> Union[dict[str, Any], str]:
    """Send a message via the Canvas inbox (Conversations). (Write operation.)

    Args:
        recipient_ids: Canvas user ids to send to (get them from
            ``get_course_roster``).
        body: The message body.
        subject: Optional subject line.

    Requires CANVAS_ENABLE_WRITES=true. This sends a real message to real
    people. Returns a confirmation, or an error string on failure / when writes
    are disabled.
    """

    if not WRITES_ENABLED:
        return WRITES_DISABLED_MESSAGE

    if not recipient_ids:
        return "Error: at least one recipient id is required."

    data: dict[str, Any] = {
        "recipients[]": [str(r) for r in recipient_ids],
        "body": body,
    }
    if subject:
        data["subject"] = subject

    try:
        result = await _write_request("POST", "/conversations", data)
    except CanvasError as exc:
        return str(exc)

    # POST /conversations returns a list of the created conversation(s).
    conversation = result[0] if isinstance(result, list) and result else result
    conversation = conversation if isinstance(conversation, dict) else {}
    return {
        "status": "sent",
        "conversation_id": conversation.get("id"),
        "recipient_ids": [str(r) for r in recipient_ids],
        "subject": subject,
    }


@mcp.tool()
async def reply_to_conversation(
    conversation_id: str, body: str
) -> Union[dict[str, Any], str]:
    """Reply to an existing Canvas inbox conversation. (Write operation.)

    Args:
        conversation_id: The conversation id (from ``get_conversations``).
        body: The reply text.

    Requires CANVAS_ENABLE_WRITES=true. Sends a real message to everyone on the
    thread. Returns a confirmation, or an error string on failure / when writes
    are disabled.
    """

    if not WRITES_ENABLED:
        return WRITES_DISABLED_MESSAGE

    try:
        result = await _write_request(
            "POST",
            f"/conversations/{conversation_id}/add_message",
            {"body": body},
        )
    except CanvasError as exc:
        return str(exc)

    return {
        "status": "sent",
        "conversation_id": result.get("id") or conversation_id,
    }


@mcp.tool()
async def create_planner_note(
    title: str,
    details: Optional[str] = None,
    todo_date: Optional[str] = None,
    course_id: Optional[str] = None,
) -> Union[dict[str, Any], str]:
    """Create a personal planner to-do note. (Write operation.)

    Args:
        title: The note title.
        details: Optional longer description.
        todo_date: Optional ISO 8601 date/datetime the note is for.
        course_id: Optional course id to associate the note with.

    Requires CANVAS_ENABLE_WRITES=true. Planner notes are private to you.
    Returns the created note, or an error string on failure / when writes are
    disabled.
    """

    if not WRITES_ENABLED:
        return WRITES_DISABLED_MESSAGE

    data: dict[str, Any] = {"title": title}
    if details:
        data["details"] = details
    if todo_date:
        data["todo_date"] = todo_date
    if course_id:
        data["course_id"] = course_id

    try:
        result = await _write_request("POST", "/planner_notes", data)
    except CanvasError as exc:
        return str(exc)

    return {
        "status": "created",
        "id": result.get("id"),
        "title": result.get("title"),
        "todo_date": result.get("todo_date"),
    }


@mcp.tool()
async def set_course_nickname(
    course_id: str, nickname: str
) -> Union[dict[str, Any], str]:
    """Set a personal nickname for a course. (Write operation.)

    Args:
        course_id: The Canvas course id.
        nickname: The nickname to display for the course (only you see it).

    Requires CANVAS_ENABLE_WRITES=true. This is cosmetic and only affects your
    own view. Returns the new nickname, or an error string on failure / when
    writes are disabled.
    """

    if not WRITES_ENABLED:
        return WRITES_DISABLED_MESSAGE

    try:
        result = await _write_request(
            "PUT",
            f"/users/self/course_nicknames/{course_id}",
            {"nickname": nickname},
        )
    except CanvasError as exc:
        return str(exc)

    return {
        "status": "set",
        "course_id": course_id,
        "nickname": result.get("nickname", nickname),
    }


@mcp.tool()
async def mark_module_item_not_done(
    course_id: str, module_id: str, item_id: str
) -> Union[dict[str, Any], str]:
    """Mark a module item as NOT done (undo a completion). (Write operation.)

    Args:
        course_id: The Canvas course id.
        module_id: The module id (from ``get_modules``).
        item_id: The module item id (from ``get_modules``).

    Requires CANVAS_ENABLE_WRITES=true. The inverse of ``mark_module_item_done``.
    Returns a confirmation, or an error string on failure / when writes are
    disabled.
    """

    if not WRITES_ENABLED:
        return WRITES_DISABLED_MESSAGE

    try:
        await _write_request(
            "DELETE",
            f"/courses/{course_id}/modules/{module_id}/items/{item_id}/done",
        )
    except CanvasError as exc:
        return str(exc)

    return {
        "status": "marked_not_done",
        "module_id": module_id,
        "item_id": item_id,
    }


# ---------------------------------------------------------------------------
# Prompts (reusable workflows surfaced to the MCP client)
# ---------------------------------------------------------------------------


@mcp.prompt()
def canvas_prompt_check() -> str:
    """Smoke test: confirm this MCP client can see and run server prompts."""

    return (
        "This is a smoke-test prompt from the Canvas MCP server. If you can see "
        "and run this, then MCP prompts are working. Reply with a one-line "
        "confirmation, then call the get_current_user tool and tell me which "
        "Canvas account I'm logged in as."
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the MCP server over stdio."""

    mcp.run()


if __name__ == "__main__":
    main()
