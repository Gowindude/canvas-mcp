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
- User has **no Claude Desktop install** (no `%APPDATA%\Claude` folder); the
  JSON they found was the Desktop *preferences* file (cowork/ccd), not
  `claude_desktop_config.json`. They're driving via **Claude Code** instead.
- Made `.env` loading cwd-independent: `load_dotenv` now targets the project
  root (`Path(__file__).parent.parent/.env`) so the server finds the token no
  matter where the MCP client launches it from. Verified from a foreign cwd;
  real env vars still take precedence. Recommended path: put token in `.env`
  (git-ignored) + `claude mcp add canvas -- <venv python> <server.py>`.
- Registered the server in Claude Code (local scope, in `~/.claude.json` for
  this project). First add via the user dropped the `server.py` arg (line-break
  in paste) → empty Args, failed to connect. Removed and re-added correctly →
  **✔ Connected**.
- **Live end-to-end verification passed** against real Canvas: token valid,
  `get_current_user` returned the account, `get_courses` returned 5 active
  courses. Full chain (.env → server → Canvas API) confirmed working.
- Note: tools become available in a Claude Code session started *after* the
  server was added — restart to use them conversationally.
- Added the remaining read-only tools (per user request to add "all" read-only
  features): `get_course_details`, `get_submission`, `get_discussion_topics`,
  `get_discussion_entries`, `get_overdue_assignments`, `get_rubric`. Total now
  **15 tools**. Write ops (submit/upload/post/reply) remain intentionally
  excluded. Live-tested the new tools against real Canvas (course 42936): all OK.
- Canvas Desktop (MSIX/Store build) wired up successfully — config lives at the
  virtualized path under `...Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\
  Claude\claude_desktop_config.json` (added `mcpServers.canvas`). User confirmed
  it works ("who am I logged in as" → Taran Govindu).
- Per user request to surface/open module links: enriched `get_modules` items
  with `external_url` + `page_url`, and added `get_pages` and
  `get_page_content` (reads login-gated Canvas page bodies via the token).
  **17 tools total.** Live-tested: module items expose page slugs;
  `get_page_content('42936','welcome-letter')` returned the full page body even
  though the course's Pages index tab is disabled (get_pages 404s gracefully in
  that case).
- Upgraded `get_discussion_entries` to return the **full nested thread**: it now
  uses the discussion `/view` endpoint and a recursive `_build_discussion_entry`
  helper so each post carries a nested `replies` list (author names resolved via
  the `participants` map; deleted posts shown as `[deleted]`). Previously only
  top-level posts were returned. Verified live: "Introduction/Hello Thread"
  now includes the reply Ethan→Katherine that was missing before.
- Added **write capabilities** (user request), gated behind a safety toggle
  `CANVAS_ENABLE_WRITES` (default off). New tools: `post_discussion_entry`,
  `reply_to_discussion_entry`, `create_discussion_topic`, `submit_assignment`
  (text/url/file-upload), `post_submission_comment`. **22 tools total.**
  - Gate enforced at TWO layers: each tool checks `WRITES_ENABLED` for a
    friendly message, and the shared `_write_request` POST/PUT helper raises if
    disabled (choke-point). `submit_assignment` gates before any upload work.
  - File upload = inst-fs 3-step flow (register → no-auth presigned upload, file
    last → confirm id).
  - **Deliberately did NOT live-test writes** (per advisor): a real post/submit
    creates instructor/classmate-visible, often irreversible content in Taran's
    courses. Verified safely: compiles, all 22 register, gate BLOCKS every write
    with no network when disabled, and OPENS to local validation when enabled.
    The only true test is a user-chosen low-stakes write.
- Added `delete_discussion_entry` (write, gated) so a post→delete round-trip can
  safely verify writes end-to-end. **23 tools total.** Verified: compiles,
  registers, gate blocks with no network.
- Confirmed the write safety toggle is fully documented in README (dedicated
  "Write operations (optional)" section) — no change needed there.
- Added the full suggested batch — **8 read + 4 write tools → 35 total**:
  - Read: `get_todo`, `get_missing_submissions`, `get_planner` (cross-course),
    `get_all_grades`, `get_quizzes`, `get_files`, `get_assignment_groups`,
    `get_course_roster`. Live-tested: todo=2, planner=23, all_grades across 5
    courses (LITR 75.86, SOCI 100). `get_files`/`get_course_roster` returned
    graceful errors on course 42936 (Files/roster restricted there) — correct
    behavior, works where permitted.
  - Write (gated): `edit_discussion_entry`, `mark_module_item_done`,
    `create_calendar_event` (user calendar), `send_message` (Conversations).
    Verified all four are gated with no network; not live-mutated.
- "Add as many capabilities as you can, including quiz taking": added a further
  batch → **44 tools total**.
  - Read: `get_quiz_submissions` (own attempts/scores), `get_groups`,
    `get_conversations`, `get_conversation`, `download_file` (reads Canvas →
    writes LOCAL disk, so NOT gated). Live-tested: conversations=4, quiz_subs
    works; get_groups graceful-errors where the course disables groups.
  - Write (gated): `reply_to_conversation`, `create_planner_note`,
    `set_course_nickname`, `mark_module_item_not_done`. Verified gated.
  - **Refused (with advisor): auto quiz-taking.** Did NOT build
    `start_quiz_attempt`/`answer_quiz_questions`/`submit_quiz` — a pipeline that
    has the AI read graded-quiz questions, pick answers and submit them is
    academic dishonesty; the write toggle doesn't change what it does. Told the
    user plainly and offered the legit study-only framing. Documented the
    exclusion in README + plan.
