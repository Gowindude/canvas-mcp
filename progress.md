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
- User shared reference repo `lucanardinocchi/canvas-mcp` (TypeScript) — to be
  reviewed for improvements before committing.
