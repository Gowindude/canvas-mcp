# CLAUDE.md — guidance for AI assistants in this repo

## What this is
A local MCP server (FastMCP, Python) connecting Claude to the Canvas LMS REST
API. Single source module: `canvas_mcp/server.py`.

## Context files (read these)
- `prompt.md` — the original request and standing conventions.
- `plan.md` — design and tool→endpoint mapping.
- `progress.md` — append-only log; add an entry when you complete work.

## Run / dev
```bash
pip install -r requirements.txt
cp .env.example .env        # then fill in CANVAS_API_TOKEN + CANVAS_BASE_URL
fastmcp run canvas_mcp/server.py     # or: python -m canvas_mcp.server
```
A running server waits silently on stdio for an MCP client — no output is
normal.

## Hard constraints (do not violate)
- Never commit `.env`. Only `.env.example` is tracked.
- Use `httpx` async only — never `requests`. Every tool is `async def`.
- Only `fastmcp` for MCP. App is `mcp = FastMCP("Canvas")`; tools use `@mcp.tool()`.
- Strip ALL HTML (BeautifulSoup `html.parser`) from any field returned to Claude.
- No hardcoded token/URL — load from environment. Missing config → `RuntimeError`
  at startup.
- Non-200 Canvas responses must yield a clear error string (with HTTP status),
  not an uncaught exception.
- Pagination must follow the `Link` `rel="next"` header (`_paginate` helper).
- Keep dependency versions pinned in `requirements.txt`.
- **Writes are gated.** All state-changing tools must go through `_write_request`
  (which enforces `CANVAS_ENABLE_WRITES`) and also check the toggle themselves.
  Default is read-only. Never live-test write tools against a real course —
  they create real, often irreversible, instructor/classmate-visible content.

## Conventions
- Add a `progress.md` entry for meaningful changes.
- Convert relative dates to absolute when noting them.
