Read docs/HANDOFF.md before any task.

## Team
- Krys is the sole developer. There is no frontend teammate.
- Frontend work is in scope: Claude Code may write Tailwind classes, template markup, custom CSS and JS in static/ directly.
- Keep these template habits: semantic HTML, no business logic in templates, HTMX partials prefixed with `_`, short comments above complex template logic.

## Database and secrets
- Local dev uses the local PostgreSQL database hapivet_dev (localhost:5432). The live Render app uses Supabase. They are not shared.
- Never run makemigrations, migrate, loaddata, flush, or any DB-writing command without asking me first.
- Before any migrate, run the DB host check and stop if HOST is not localhost:
  backend\venv\Scripts\python.exe backend\manage.py shell -c "from django.db import connection as c; s = c.settings_dict; print('DB HOST:', s['HOST'], '| NAME:', s['NAME'])"
- Never run migrate from a feature branch against the live DB.
- Never put the live DATABASE_URL in backend/.env.
- Never read or print backend/.env.

## Commands
- Always run Python and manage.py commands with backend\venv\Scripts\python.exe (the project venv), never plain python.
- Don't run the full test suite; Krys runs it. While working you may run the tests for the app you changed (backend\venv\Scripts\python.exe backend\manage.py test <app>).
- Report exactly which test commands you ran and paste their real output. Never describe a result you didn't see, and never write a "task finished" or "background task" notice yourself.
- Never loosen or remove an assertion to make a test pass. If a test fails, report why and propose a fix; say clearly if an assertion changed.
- Known: before 8 AM Manila, SlotViewTests.test_buttons_empty_state and test_buttons_heading_kept_with_the_empty_message fail because of the UTC-date backlog item. Report them, don't fix them unless asked.

## Git rules
- Never run git commands that change branches, history or the remote. Krys runs all git commands.
