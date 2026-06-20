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
| `get_discussion_entries` | `GET /courses/{id}/discussion_topics/{topic_id}/view` (full nested thread; `participants` map resolves author names) |
| `get_overdue_assignments` | `GET /courses/{id}/assignments?bucket=overdue&include[]=submission` |
| `get_rubric` | `GET /courses/{id}/rubrics/{rubric_id}` |
| `get_pages` | `GET /courses/{id}/pages` |
| `get_page_content` | `GET /courses/{id}/pages/{page_url}` |
| `get_todo` | `GET /users/self/todo` |
| `get_missing_submissions` | `GET /users/self/missing_submissions` |
| `get_planner` | `GET /planner/items?start_date&end_date` |
| `get_all_grades` | `GET /courses?enrollment_state=active&include[]=total_scores` (reads `computed_current_*`) |
| `get_quizzes` | `GET /courses/{id}/quizzes` |
| `get_files` | `GET /courses/{id}/files` |
| `get_assignment_groups` | `GET /courses/{id}/assignment_groups?include[]=assignments` |
| `get_course_roster` | `GET /courses/{id}/users?include[]=enrollments` |
| `get_quiz_submissions` | `GET /courses/{id}/quizzes/{quiz_id}/submissions` (own attempts/scores) |
| `get_groups` | `GET /courses/{id}/groups` |
| `get_conversations` | `GET /conversations` |
| `get_conversation` | `GET /conversations/{id}` |
| `download_file` | `GET /files/{id}` then GET the file URL → write to local disk (not gated) |

### Write tools (gated behind `CANVAS_ENABLE_WRITES`, default off)

| Tool | Canvas endpoint |
| --- | --- |
| `post_discussion_entry` | `POST /courses/{id}/discussion_topics/{topic_id}/entries` |
| `reply_to_discussion_entry` | `POST /courses/{id}/discussion_topics/{topic_id}/entries/{entry_id}/replies` |
| `create_discussion_topic` | `POST /courses/{id}/discussion_topics` |
| `submit_assignment` | `POST /courses/{id}/assignments/{aid}/submissions` (+ 3-step inst-fs upload for `online_upload`) |
| `post_submission_comment` | `PUT /courses/{id}/assignments/{aid}/submissions/self` |
| `delete_discussion_entry` | `DELETE /courses/{id}/discussion_topics/{topic_id}/entries/{entry_id}` |
| `edit_discussion_entry` | `PUT /courses/{id}/discussion_topics/{topic_id}/entries/{entry_id}` |
| `mark_module_item_done` | `PUT /courses/{id}/modules/{module_id}/items/{item_id}/done` |
| `create_calendar_event` | `POST /calendar_events` (context_code `user_{self}`) |
| `send_message` | `POST /conversations` (recipient ids from `get_course_roster`) |
| `reply_to_conversation` | `POST /conversations/{id}/add_message` |
| `create_planner_note` | `POST /planner_notes` |
| `set_course_nickname` | `PUT /users/self/course_nicknames/{id}` |
| `mark_module_item_not_done` | `DELETE /courses/{id}/modules/{mid}/items/{iid}/done` |

**Deliberately excluded — auto quiz-taking.** Tools that start a graded quiz
attempt, fetch its questions for answering, and submit answers are *not*
implemented: having the AI answer and submit a graded quiz is academic
dishonesty. `get_quiz_submissions` (the student's own past attempts/scores) is
the only quiz write/read kept.

Safety design:
- `CANVAS_ENABLE_WRITES` env var (default `false`). Each write tool checks it for
  a friendly message; the shared `_write_request` POST/PUT helper enforces the
  same gate as a choke-point so no write can bypass it. `submit_assignment`
  checks the gate **before** any file-upload work.
- File upload uses the inst-fs 3-step flow: register → upload bytes to the
  pre-signed URL (no auth header, file field last) → read/confirm the file id.
- Writes are **not idempotent**; the toggle is global (shared `.env`) and read
  once at startup (restart to change).
- Destructive ops (delete/edit) are still excluded.

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
