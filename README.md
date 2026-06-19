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

All tools are **read-only** — this server never modifies anything in Canvas.

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
