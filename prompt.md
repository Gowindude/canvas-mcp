# Project Prompt & Conventions

This file is the canonical record of what was requested for the **canvas-mcp**
project, plus the working conventions for it. Read this first for context.

## Conventions (read before working in this repo)

- **`prompt.md`** (this file) — the original request and standing conventions.
  Treat it as the source of truth for *what* was asked. Update only if the
  scope/requirements change.
- **`plan.md`** — the implementation plan: the steps, design decisions, and
  open questions. Update it when the approach changes.
- **`progress.md`** — an append-only running log of what has actually been
  done, in order, with outcomes. Add to it throughout the project; do not
  rewrite history.
- **`CLAUDE.md`** — guidance for AI assistants working in this repo (run
  commands, constraints, gotchas).
- **Secrets:** never commit `.env`. Only `.env.example` is tracked. `.gitignore`
  must exist and be staged before any other file at repo creation time.
- **HTTP:** use `httpx` async only (never `requests`). All MCP tools are
  `async def`. All Canvas HTML fields are stripped to plain text before being
  returned to Claude.

## Original request (verbatim intent)

### GitHub repo setup
1. Create a local project directory `canvas-mcp` and `cd` into it.
   - *Deviation (with justification):* the working directory was already named
     `canvas-mcp` and empty, so the repo was initialized in place rather than
     nesting a second `canvas-mcp/` inside it. See `progress.md`.
2. `git init`.
3. Create `.gitignore` (`.env`, `__pycache__/`, `*.pyc`, `*.pyo`, `.venv/`,
   `venv/`, `*.egg-info/`, `dist/`, `build/`, `.DS_Store`) before the first
   commit.
4. Create the GitHub repo:
   `gh repo create canvas-mcp --public --source=. --remote=origin --description="MCP server connecting Claude to Canvas LMS"`
5. After files are written: `git add .`,
   `git commit -m "Initial commit: Canvas MCP server with FastMCP"`,
   `git push -u origin main`.
6. `gh repo view --web` and print the repo URL.

Critical constraints: never commit `.env` (only `.env.example`); `.gitignore`
created/staged before other files; run all shell commands directly; stop and
report on any command failure.

### What to build
A local MCP server (`canvas_mcp/server.py`) using `fastmcp` that connects
Claude to the Canvas LMS REST API (`https://<institution>.instructure.com/api/v1/`,
Bearer-token auth). Tools:

1. `get_courses` — active courses (id, name, course code, enrollment type).
2. `get_assignments(course_id)` — id, name, due date (ISO), points, submission type.
3. `get_assignment_details(course_id, assignment_id)` — full details, HTML-stripped
   description, due/unlock/lock dates, points.
4. `get_grades(course_id)` — current grade/score + graded submissions
   (assignment name, score, points possible, letter grade).
5. `get_upcoming_events(days_ahead=14)` — calendar API: upcoming events &
   assignment due dates within N days (title, type, course name, start/due time).
6. `get_announcements(course_id)` — 10 most recent announcements (title, posted
   date, plain-text body).

### Implementation requirements
- `fastmcp`, `httpx` (async), `python-dotenv`, `beautifulsoup4` (`html.parser`).
- `.env`: `CANVAS_API_TOKEN`, `CANVAS_BASE_URL=https://gatech.instructure.com`.
- Graceful HTTP error handling: non-200 → clear error string incl. status code.
- Reusable async `paginate(url, params)` that follows `Link` `rel="next"`.
- Runs via `fastmcp run canvas_mcp/server.py` or `python -m canvas_mcp.server`.
- App named `mcp = FastMCP("Canvas")`; every tool uses `@mcp.tool()`; all async.
- Raise clear `RuntimeError` on startup if token/URL missing. Pin all deps.

### File structure
`canvas_mcp/__init__.py` (empty), `canvas_mcp/server.py`, `requirements.txt`,
`.env.example`, `README.md`.

### README must include
Token generation steps (Account → Settings → New Access Token), setup, run
command, Claude Desktop config JSON for Windows (`%APPDATA%\Claude\claude_desktop_config.json`),
and verification steps.
