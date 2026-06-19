"""Canvas LMS MCP server.

Exposes a set of read-only tools that let Claude query the Canvas LMS REST API
(courses, assignments, grades, calendar events and announcements) over a local
MCP (stdio) connection.

Run with either:

    fastmcp run canvas_mcp/server.py
    python -m canvas_mcp.server
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Union

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Load variables from a local .env file (if present) into the environment.
load_dotenv()

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

# Canvas limits the calendar API to at most 10 context codes per request.
MAX_CONTEXT_CODES = 10

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


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the MCP server over stdio."""

    mcp.run()


if __name__ == "__main__":
    main()
