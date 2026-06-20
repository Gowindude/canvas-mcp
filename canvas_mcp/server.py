"""Canvas LMS MCP server.

Exposes a set of read-only tools that let Claude query the Canvas LMS REST API
(courses, assignments, grades, calendar events and announcements) over a local
MCP (stdio) connection.

Run with either:

    fastmcp run canvas_mcp/server.py
    python -m canvas_mcp.server
"""

from __future__ import annotations

import mimetypes
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Union

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from fastmcp import FastMCP

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
    includes all available links: ``html_url`` (open it in Canvas),
    ``external_url`` (the real external website, for external-link/tool items)
    and ``page_url`` (the slug to pass to ``get_page_content`` for Page items).
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


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the MCP server over stdio."""

    mcp.run()


if __name__ == "__main__":
    main()
