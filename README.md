# Canvas MCP Server

An [MCP](https://modelcontextprotocol.io/) server, built with
[FastMCP](https://github.com/jlowin/fastmcp), that connects Claude to the
**Canvas LMS** REST API. Once configured, you can ask Claude about your
courses, assignments, grades, upcoming deadlines and announcements in plain
language.

## Tools exposed to Claude

| Tool | Description |
| --- | --- |
| `get_courses` | List all active courses you are enrolled in (id, name, course code, enrollment type). |
| `get_assignments(course_id)` | List all assignments for a course (id, name, due date, points, submission types). |
| `get_assignment_details(course_id, assignment_id)` | Full details of one assignment, including a plain-text description, unlock and lock dates. |
| `get_grades(course_id)` | Your current grade/score in a course plus a list of graded submissions. |
| `get_upcoming_events(days_ahead=14)` | Upcoming assignment due dates and calendar events within the next N days. |
| `get_announcements(course_id)` | The 10 most recent announcements for a course, as plain text. |
| `get_current_user` | The authenticated user's id, name and email — handy for verifying your token. |
| `get_modules(course_id)` | A course's modules and their items (pages, assignments, files, ...). |
| `search_course_content(course_id, search_term)` | Search a course's assignments and modules for a term. |
| `get_course_details(course_id)` | One course's details, including the syllabus and term (HTML stripped). |
| `get_submission(course_id, assignment_id)` | Your submission for one assignment: score, grade, state, late/missing flags and instructor comments. |
| `get_discussion_topics(course_id)` | A course's discussion topics (excluding announcements), as plain text. |
| `get_discussion_entries(course_id, topic_id)` | The full threaded posts of a discussion topic, including nested replies, as plain text. |
| `get_overdue_assignments(course_id)` | Past-due assignments for a course (Canvas "overdue" bucket). |
| `get_rubric(course_id, rubric_id)` | A rubric's grading criteria and ratings. |
| `get_pages(course_id)` | List a course's wiki/content pages (title + slug + link). |
| `get_page_content(course_id, page_url)` | Read a Canvas page's full text (login-gated pages fetched via your token). |
| `get_todo` | Your Canvas to-do items across all courses. |
| `get_missing_submissions` | Past-due assignments you haven't submitted, across all courses. |
| `get_planner(days_ahead=14)` | Unified Planner feed (assignments, quizzes, events) across all courses. |
| `get_all_grades` | Your current grade/score in every active course (dashboard). |
| `get_quizzes(course_id)` | A course's quizzes (due date, points, time limit, attempts). |
| `get_files(course_id)` | A course's files with download URLs (where the Files area is open). |
| `get_assignment_groups(course_id)` | Grade-weighting categories and their assignments. |
| `get_course_roster(course_id)` | People in a course (id, name, role) where permitted. |

`get_modules` items now also expose `external_url` (the real external website
for external-link items) and `page_url` (the slug to feed `get_page_content`),
in addition to the Canvas `html_url` — so module links can actually be opened
or read.

### Write tools (opt-in — off by default)

These **modify** Canvas and are disabled unless you set
`CANVAS_ENABLE_WRITES=true` (see [Write operations](#write-operations-optional)).

| Tool | Description |
| --- | --- |
| `post_discussion_entry(course_id, topic_id, message)` | Post a new top-level entry to a discussion. |
| `reply_to_discussion_entry(course_id, topic_id, entry_id, message)` | Reply to an existing discussion post. |
| `create_discussion_topic(course_id, title, message)` | Create a new discussion topic. |
| `submit_assignment(course_id, assignment_id, submission_type, text?, url?, file_path?)` | Submit an assignment (`online_text_entry`, `online_url`, or `online_upload`). |
| `post_submission_comment(course_id, assignment_id, comment)` | Comment on your own submission. |
| `delete_discussion_entry(course_id, topic_id, entry_id)` | Delete one of your own discussion posts/replies (irreversible). |
| `edit_discussion_entry(course_id, topic_id, entry_id, message)` | Edit one of your own discussion posts/replies. |
| `mark_module_item_done(course_id, module_id, item_id)` | Mark a module item complete (where the course allows it). |
| `create_calendar_event(title, start_at, end_at?, description?)` | Add a personal event to your Canvas calendar. |
| `send_message(recipient_ids, body, subject?)` | Send a Canvas inbox message (recipient ids from `get_course_roster`). |

The read tools are always available. The write tools above do nothing — they
return a "writes are disabled" message — unless you explicitly opt in via
`CANVAS_ENABLE_WRITES`. See [Write operations](#write-operations-optional).

---

## 1. Generate a Canvas API token

You authenticate to Canvas with a personal **access token**. To create one:

1. Log in to your institution's Canvas site (for example,
   `https://gatech.instructure.com`).
2. Click **Account** in the global navigation menu on the far left.
3. Click **Settings**.
4. Scroll down to the **Approved Integrations** section.
5. Click the **+ New Access Token** button.
6. Enter a **Purpose** (e.g. `Claude MCP server`). You may leave the
   **Expires** field blank for a non-expiring token, or set an expiry date.
7. Click **Generate Token**.
8. **Copy the token immediately** and store it somewhere safe — Canvas only
   shows it once. If you lose it you must generate a new one.

> Treat this token like a password. Anyone with it can act as you in Canvas.
> Never commit it to git or share it. This project keeps it in a git-ignored
> `.env` file.

---

## 2. Setup

Copy or clone these files to a folder on your machine, then from inside that
folder:

### a. Create your `.env`

```bash
cp .env.example .env
```

On Windows PowerShell:

```powershell
Copy-Item .env.example .env
```

Open `.env` and fill in your values:

```ini
CANVAS_API_TOKEN=1234~your_real_token_here
CANVAS_BASE_URL=https://gatech.instructure.com
```

Set `CANVAS_BASE_URL` to **your** institution's Canvas URL (no trailing
`/api/v1` — the server appends that for you).

### b. Install dependencies

It is recommended to use a virtual environment:

```bash
python -m venv .venv
# macOS/Linux:
source .venv/bin/activate
# Windows PowerShell:
.\.venv\Scripts\Activate.ps1

pip install -r requirements.txt
```

---

## 3. Run the server

The server speaks MCP over stdio. You can launch it either way:

```bash
fastmcp run canvas_mcp/server.py
```

or

```bash
python -m canvas_mcp.server
```

If `CANVAS_API_TOKEN` or `CANVAS_BASE_URL` is missing, the server raises a
clear `RuntimeError` explaining what to fix.

When run directly, an MCP server waits for a client (such as Claude Desktop)
to connect over stdio — so it is normal for it to appear to "hang" with no
output. That means it started correctly.

---

## 4. Connect it to Claude Desktop (Windows)

Claude Desktop launches the server for you, so you normally do **not** run the
commands above by hand once it is configured.

1. Open the Claude Desktop config file. On Windows it lives at:

   ```text
   %APPDATA%\Claude\claude_desktop_config.json
   ```

   You can paste that path into the Run dialog (`Win + R`) or open it from
   Claude Desktop via **File → Settings → Developer → Edit Config**.

2. Add a `canvas` entry under `mcpServers`. Pass your Canvas credentials
   directly in an **`env`** block — that way Claude Desktop sets them in the
   server's environment and you do **not** need a separate `.env` file. Replace
   the paths and values with **your** own:

   ```json
   {
     "mcpServers": {
       "canvas": {
         "command": "C:\\Users\\YourName\\projects\\canvas-mcp\\.venv\\Scripts\\python.exe",
         "args": [
           "-m",
           "canvas_mcp.server"
         ],
         "cwd": "C:\\Users\\YourName\\projects\\canvas-mcp",
         "env": {
           "CANVAS_BASE_URL": "https://your-school.instructure.com",
           "CANVAS_API_TOKEN": "YOUR_TOKEN_HERE"
         }
       }
     }
   }
   ```

   Notes:
   - Use **double backslashes** (`\\`) in every JSON path on Windows.
   - The `env` block is the recommended way to supply
     `CANVAS_BASE_URL` and `CANVAS_API_TOKEN` — the server reads them straight
     from its environment, so a `.env` file is optional when launched this way.
   - `command` should be the Python interpreter. Using the project's
     `.venv\\Scripts\\python.exe` guarantees the installed dependencies are on
     the path. Plain `"python"` also works **only** if it is on your PATH and
     has the requirements installed.
   - `cwd` must point at the project root (the folder that contains the
     `canvas_mcp` package) so `python -m canvas_mcp.server` can import it.

3. Save the file.

---

## 5. Verify it works

1. **Fully quit** Claude Desktop (right-click the tray icon → Quit) and reopen
   it. Config changes are only picked up on a fresh start.
2. Open a new chat. Click the tools/plug icon in the message box — you should
   see **canvas** listed with its tools.
3. Ask something like:

   > "Who am I logged in as in Canvas?"

   Claude should call `get_current_user` and return your name — a quick token
   check. Then try:

   > "What courses am I enrolled in this semester?"

   Claude should call `get_courses` and respond with your course list.
4. Follow up with, for example:

   > "What assignments are due in the next week?"

   to exercise `get_upcoming_events`.

### Troubleshooting

- **Server not listed / red error in Claude:** open
  **Settings → Developer** in Claude Desktop and view the MCP logs, or check
  `%APPDATA%\Claude\logs\`.
- **`RuntimeError: CANVAS_API_TOKEN is not set`:** the `env` block is missing
  from your `claude_desktop_config.json` (or, if you run it manually, your
  `.env` is missing / not in the `cwd` you configured).
- **HTTP 401 errors in tool output:** the token is wrong, expired, or revoked
  — generate a new one (Section 1).
- **HTTP 404 errors:** double-check the `course_id` and that
  `CANVAS_BASE_URL` matches your institution.

---

## Write operations (optional)

By default this server is **read-only** — it can look at Canvas but never change
it. The write tools (`post_discussion_entry`, `reply_to_discussion_entry`,
`create_discussion_topic`, `submit_assignment`, `post_submission_comment`,
`delete_discussion_entry`, `edit_discussion_entry`, `mark_module_item_done`,
`create_calendar_event`, `send_message`) are disabled until you explicitly turn
them on.

### Enabling writes

1. Set the toggle in your `.env`:

   ```ini
   CANVAS_ENABLE_WRITES=true
   ```

   (Or, if you configure the server via an `env` block in
   `claude_desktop_config.json`, add `"CANVAS_ENABLE_WRITES": "true"` there.)
2. **Fully restart** Claude Desktop / your MCP client — the toggle is read once
   at startup.

While disabled, every write tool simply returns a "writes are disabled" message
and makes no network call.

### Please read before enabling

- ⚠️ **These actions are real and mostly irreversible.** A posted discussion
  reply or a submitted assignment is visible to your instructor and classmates.
  Treat it like clicking "Submit" yourself.
- ⚠️ **Writes are not idempotent.** If a tool call is retried, it can
  double-post or double-submit. Review what Claude is about to do.
- **The toggle is global.** `.env` is shared by every client that launches this
  server (both Claude Desktop and Claude Code), so enabling writes enables them
  everywhere.
- **`submit_assignment`** requires a `submission_type` the assignment actually
  accepts: `online_text_entry` (pass `text`), `online_url` (pass `url`), or
  `online_upload` (pass an absolute `file_path`).

### Testing writes safely

Because a write creates real content, test with something low-stakes that **you
choose** — e.g. post a short reply to a practice/introductions discussion you
don't mind editing, then delete it in Canvas. Don't test by submitting a real
graded assignment.

---

## Project structure

```text
canvas_mcp/
  __init__.py        # package marker (empty)
  server.py          # FastMCP app + all six tools
requirements.txt     # pinned dependencies
.env.example         # template for your .env (copy to .env)
.env                 # your secrets (git-ignored, never committed)
README.md            # this file
```

## License

Provided as-is for personal/educational use. You are responsible for complying
with your institution's Canvas API usage policies.
