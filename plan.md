# Implementation Plan

## Goal
A local Python MCP server (FastMCP) exposing read-only Canvas LMS tools to
Claude, published to a public GitHub repo.

## Architecture
- `canvas_mcp/server.py` — single-module FastMCP app (`mcp = FastMCP("Canvas")`).
- Config from environment via `python-dotenv`. Validate `CANVAS_API_TOKEN` and
  `CANVAS_BASE_URL` at import; `RuntimeError` if missing. `API_BASE = base + /api/v1`.
- Shared async HTTP helpers:
  - `_paginate(path, params)` — follows `Link` `rel="next"` via `response.links`,
    returns combined list. `per_page=100` default.
  - `_get_one(path, params)` — single object.
  - Both translate `httpx.HTTPStatusError` / `RequestError` into a `CanvasError`
    carrying a clear message (incl. HTTP status); each tool catches it and
    returns the message string.
- `strip_html()` via BeautifulSoup `html.parser` for every rich-text field.

## Tool → endpoint mapping
| Tool | Canvas endpoint |
| --- | --- |
| `get_courses` | `GET /courses?enrollment_state=active` (enrollment type from `enrollments[]`) |
| `get_assignments` | `GET /courses/{id}/assignments` |
| `get_assignment_details` | `GET /courses/{id}/assignments/{aid}` |
| `get_grades` | `GET /courses/{id}/enrollments?user_id=self` + `GET /courses/{id}/students/submissions?student_ids[]=self&include[]=assignment` |
| `get_upcoming_events` | `GET /calendar_events?type=assignment|event&start_date&end_date&context_codes[]=course_*` (chunked ≤10 codes) |
| `get_announcements` | `GET /courses/{id}/discussion_topics?only_announcements=true&per_page=10` |
| `get_current_user` | `GET /users/self` |
| `get_modules` | `GET /courses/{id}/modules?include[]=items` |
| `search_course_content` | `GET /courses/{id}/assignments?search_term=` + `GET /courses/{id}/modules?search_term=&include[]=items` |
| `get_course_details` | `GET /courses/{id}?include[]=syllabus_body&include[]=term&...` |
| `get_submission` | `GET /courses/{id}/assignments/{aid}/submissions/self?include[]=submission_comments` |
| `get_discussion_topics` | `GET /courses/{id}/discussion_topics` |
| `get_discussion_entries` | `GET /courses/{id}/discussion_topics/{topic_id}/entries` |
| `get_overdue_assignments` | `GET /courses/{id}/assignments?bucket=overdue&include[]=submission` |
| `get_rubric` | `GET /courses/{id}/rubrics/{rubric_id}` |
| `get_pages` | `GET /courses/{id}/pages` |
| `get_page_content` | `GET /courses/{id}/pages/{page_url}` |

Write operations from the reference (submit assignment, file upload, post/reply
discussion) are intentionally **excluded** to keep the server strictly
read-only.

## Key decisions
- Calendar events: fetch active courses first to build `context_codes` and a
  `course_*` → name map; Canvas caps context codes at 10/request, so chunk.
- Announcements via `discussion_topics?only_announcements=true` (reliably newest
  first, supports `per_page`) rather than the global `/announcements` endpoint.
- Errors returned as strings (not exceptions) so Claude sees a usable message.

## Dependencies (pinned)
`fastmcp==2.14.7`, `httpx==0.28.1`, `python-dotenv==1.2.2`, `beautifulsoup4==4.13.4`.

## Repo setup order
git init → write+stage `.gitignore` → write all files → `gh repo create` →
`git add .` → commit → push → `gh repo view --web` → print URL.

## Reference
`https://github.com/lucanardinocchi/canvas-mcp` (TypeScript) — reviewed for
parity/ideas; this implementation is independent Python with improvements
(pagination helper, graceful error strings, HTML stripping, calendar chunking).
