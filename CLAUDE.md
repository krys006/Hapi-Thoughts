Read docs/HANDOFF.md before any task.

## Team
- Krys is the sole developer. There is no frontend teammate.
- Frontend work is in scope: Claude Code may write Tailwind classes, template markup, custom CSS and JS in static/ directly.
- Keep these template habits: semantic HTML, no business logic in templates, HTMX partials prefixed with `_`, short comments above complex template logic.

## Database and secrets
- Never run makemigrations, migrate, loaddata, flush, or any DB-writing command without asking me first. The Supabase DB is shared with the live Render app.
- Never run migrate from a feature branch. The DB is shared with live.
- Never read or print backend/.env.

## Commands
- Always run Python and manage.py commands with backend\venv\Scripts\python.exe (the project venv), never plain python.

## Git rules
- Never run git commands that change branches, history or the remote. Krys runs all git commands.
