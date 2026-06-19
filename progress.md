# Progress Log

Append-only. Newest entries at the bottom.

## 2026-06-19

- Inspected working directory: already named `canvas-mcp` and empty. Verified
  tooling: `gh` 2.83.2 (authed as `Gowindude`, `repo` scope), Python 3.13.14,
  git available.
- **Decision/deviation:** initialized git in place (`git init -b main`) instead
  of creating a nested `canvas-mcp/` directory, since the cwd already *is*
  `canvas-mcp` and empty. Avoids `canvas-mcp/canvas-mcp/...`.
- Verified real PyPI versions before pinning: fastmcp 2.14.7, httpx 0.28.1,
  python-dotenv 1.2.2, beautifulsoup4 4.13.4.
- Created and staged `.gitignore` first (before any other file), per constraint.
- Wrote `canvas_mcp/__init__.py` (empty) and `canvas_mcp/server.py` (FastMCP app
  + 6 async tools, pagination helper, error handling, HTML stripping).
- Wrote `requirements.txt`, `.env.example`, `README.md` (token steps, setup,
  run, Windows Claude Desktop config, verification).
- Wrote context files: `prompt.md`, `plan.md`, this `progress.md`, `CLAUDE.md`.
- Verified `server.py` compiles (`py_compile`).
- Created GitHub repo `Gowindude/canvas-mcp` (public) via `gh repo create`.
- **Commit 1 (pushed):** "Initial commit: Canvas MCP server with FastMCP" —
  baseline of all files. `.env` confirmed git-ignored (only `.env.example`
  tracked).
- User asked for frequent-ish commits + shared reference repo
  `lucanardinocchi/canvas-mcp` (TypeScript). Cloned and reviewed it.
- Folded in improvements (read-only only): added `get_current_user`
  (`/users/self`), `get_modules` (modules + items), and
  `search_course_content`. **Deliberately did NOT port** the reference's write
  operations (submit assignment, file upload, post/reply discussion) — they are
  destructive, were not requested, and keeping the server read-only is safer.
- Updated README tool table + verification steps for the new tools.
- Set up `.venv` and installed pinned deps to validate versions and smoke-test
  imports/tool registration. **Smoke test passed:** all 9 tools register, app
  name "Canvas", `API_BASE` builds correctly, `strip_html` works.
- User attempted Node-style steps (`npm install` / `npm run build` /
  `dist/index.js`) that belong to the TS reference repo. Flagged the conflict;
  user confirmed **keep Python**.
- Adopted the reference repo's config "workaround": pass `CANVAS_BASE_URL` /
  `CANVAS_API_TOKEN` via the `env` block in `claude_desktop_config.json` so no
  separate `.env` is needed. Updated README's Claude Desktop section to use an
  `env` block + the venv interpreter path. User's institution:
  `https://bartonline.instructure.com`.
