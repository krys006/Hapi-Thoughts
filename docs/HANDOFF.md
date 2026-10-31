# HANDOFF.md — Hapi Vet Project State

> Last updated: 2026-10-31, after the local dev database switch
> (post-break audit: 2026-10-16).
> §3–§5 describe the verified state as of those dates. Anything marked
> "(unverified)" could not be confirmed from the code.
> Where the code and this file disagree, the code wins. Report the
> differences, then propose doc fixes.

---

## 1. Project Summary

Hapi Vet is a veterinary clinic management system for Hapi Tutz Vet. Supplies
(Bognuyan, Gasan, Marinduque, PH). It is a real client deployment for
Dr. Edgar Sadiwa.

**Team:** Krys is the sole developer, handling both backend and frontend
(models, views, migrations, deployment, templates, Tailwind, custom CSS and
JS in `static/`). There is no frontend teammate. Claude Code may write
Tailwind classes and template markup directly.

Reference docs in `docs/`: BRIEF, STACK, FEATURES, DATA_MODELS, CONVENTIONS,
GOOGLE_AUTH_NOTES, SUPABASE_MIGRATION_NOTES. Some are still out of date (see §4).

Repo: `Hapi-Thoughts`. Django code lives in `backend/`. Krys runs commands
from `backend/` with `.\venv\Scripts\python.exe`; Claude Code runs from the
repo root with `backend\venv\Scripts\python.exe` and `backend\` paths (see
CLAUDE.md).

---

## 2. Verification Protocol (run this first, read-only)

> **Last run 2026-10-16. Results are reflected in §3–§5 below.**

**Rules while verifying**
- Do NOT run `makemigrations`, `migrate`, `loaddata`, `flush` or any command
  that writes to the database. Local dev uses the local `hapivet_dev` DB; the
  live deployment uses Supabase. Run the DB host check (§3) first.
- Do NOT edit any file during verification. Produce a report only.
- Never print or commit values from `backend/.env`.
- Run every `python`/`pip` command below with the venv
  (`backend\venv\Scripts\python.exe`, `backend\venv\Scripts\pip.exe`).

**Step 1: Git state**
```bash
git status
git branch -a
git log --oneline -20
git log --oneline origin/rebuild..HEAD
git diff origin/rebuild --stat
```
Report: current branch, uncommitted changes, whether `feature/admin-calendar-page`
exists, and whether it is merged into `trial-run` and `rebuild`.

**Step 2: Environment and health**
```bash
cd backend
python --version
pip freeze | grep -iE "django|allauth|anymail|storages|boto3|gunicorn|whitenoise|pillow|environ|psycopg"
python manage.py check
python manage.py showmigrations
python manage.py makemigrations --check --dry-run
```
Report: versions, check errors, and unapplied migrations. If
`makemigrations --check` shows pending model changes, report them and do
NOT create the migration.

**Step 3: Code vs. docs audit**
For each model in DATA_MODELS.md, compare the code (`models.py`) against the
doc: field names, types, options and relationships. Also compare the
`urls.py` files against CONVENTIONS.md section 5 and the `templates/` tree
against CONVENTIONS.md section 3. Report mismatches in a table
(doc says / code says / file:line).

**Step 4: Check each claim in the table below**

| # | Claim | How to verify |
|---|---|---|
| 1 | Django is 5.2.12 (docs also say 5.1.7 in places) | `pip freeze`, `requirements.txt` |
| 2 | Media uses Supabase Storage (not Cloudinary) | `settings.py` storage block, `requirements.txt` |
| 3 | Email is Brevo via django-anymail (not Gmail SMTP) | `settings.py` EMAIL/ANYMAIL, `requirements.txt` |
| 4 | Role assignment on Google signup is in `accounts/adapters.py` | read the file |
| 5 | `MedicalRecord.appointment` is a OneToOneField | `medical/models.py` |
| 6 | Receipts print via `window.print()` + `@media print` (no PDF library) | grep templates and `requirements.txt` |
| 7 | All views are function-based | grep for `class .*View` and `View)` in views |
| 8 | Medical records and vaccinations have no delete path | grep views/urls for delete |
| 9 | Private notes never reach owner templates | grep `private_notes` in `templates/owner/` and owner views |
| 10 | Receipt is locked once PAID | read billing views |
| 11 | Slot double-booking is prevented | read booking view and `get_available_slots` logic |
| 12 | Role check at top of every view | scan all views for the role guard |
| 13 | Calendar day-click works (fixed missing `import datetime` in `appointments/views.py`) | check import exists, read the day-panel HTMX view |
| 14 | Admin calendar page is in progress, with phase unknown | see "Open Items" below |
| 15 | Every `<a>` tag in templates is single-line | grep for lines with an unclosed `<a` |
| 16 | No hardcoded secrets in the repo | grep for key-like strings, confirm `.env` is gitignored |
| 17 | FEATURES.md `planned` items may actually be done | see list below |

**FEATURES.md items marked `planned` to check against code:** view active
services before booking; admin calendar and list view; calendar day side
panel (admin and dashboard); one medical record per appointment; red dot on
bell icon; failed-email logging, dashboard warning and resend; mobile
responsiveness; admin Google account linking.

**Step 5: Deliver a report** with: confirmed claims, wrong claims, things
found in code that this file does not mention, and a proposed list of doc
updates. Wait for approval before editing anything.

---

## 3. Current State (verified 2026-10-16, updated 2026-10-31)

**Deployment**
- App on Render, auto-deploying from `rebuild` (confirmed in the Render
  dashboard).
- Render build: `pip install -r requirements.txt && python manage.py collectstatic --noinput && python manage.py migrate`
  (unverified — not re-checked in the dashboard). Because the build runs
  `migrate`, merging into `rebuild` applies migrations to the live Supabase DB.
- Render start: `gunicorn hapivet.wsgi:application --bind 0.0.0.0:$PORT`
  (unverified)
- Render keeps its own env vars (Brevo email on).

**Database**
- Local dev: a **local PostgreSQL 18** database named `hapivet_dev`
  (`localhost:5432`).
- Live (Render): Supabase PostgreSQL via the **Session Pooler**
  (`*.pooler.supabase.com`, port 5432).
- The two are no longer shared. The live `DATABASE_URL` is not kept in the
  local `backend/.env`.
- DB host check — run before EVERY `migrate`; for local work it must show
  `localhost | hapivet_dev`:
  ```
  backend\venv\Scripts\python.exe backend\manage.py shell -c "from django.db import connection as c; s = c.settings_dict; print('DB HOST:', s['HOST'], '| NAME:', s['NAME'])"
  ```
  From backend/:
  ```
  .\venv\Scripts\python.exe manage.py shell -c "from django.db import connection as c; s = c.settings_dict; print('DB HOST:', s['HOST'], '| NAME:', s['NAME'])"
  ```

**Local dev**
- `USE_BREVO_EMAIL=False` locally, so the console email backend is active
  (`settings.py:168-191`; `EMAIL_BACKEND` prints
  `django.core.mail.backends.console.EmailBackend`).
- Use `backend\venv\Scripts\python.exe` for all Python commands.

**Local dev setup**
- `.env` flags: `DEBUG=True`, `USE_BREVO_EMAIL=False` (emails print to the
  runserver console), `USE_S3=False` (uploads go to `backend/media/`,
  gitignored).
- Admin account: `createsuperuser` gives `role=pet_owner` and cannot log in
  at `/admin-login/`. Create the admin in `manage.py shell` instead, with
  `User.objects.create_user(..., role=User.ADMIN, is_onboarded=True, is_staff=True)`
  and `getpass()` for the password.
- Google sign-in: credentials come from settings (`SOCIALACCOUNT_PROVIDERS`
  `APP`), so a fresh DB needs no SocialApp row. Do not add one — it causes
  `MultipleObjectsReturned`.

**Branch model**
- `master`: protected stable
- `rebuild`: auto-deploys to Render
- `trial-run`: pushed to origin; the buffer branch between feature/fix
  branches and `rebuild`
- Feature/fix branches are cut from `trial-run`, then PR'd back into it

---

## 4. Known Doc Drift

| Topic | Docs say | Reality (verified) | Fixed by |
|---|---|---|---|
| Django version | 5.1.7 in SYSTEM_INSTRUCTIONS.md and the claude.ai Project instructions (Project instructions: unverified) | 5.2.12 (`requirements.txt:11`; STACK.md already correct) | Batch 3 (FEATURES/STACK/.env.example, plus SYSTEM_INSTRUCTIONS.md and the claude.ai Project instructions) |
| Media storage | Cloudinary (STACK.md) | Supabase Storage via django-storages S3 backend | Batch 3 |
| Email | Gmail SMTP (STACK, FEATURES, BRIEF, SYSTEM_INSTRUCTIONS, `.env.example`) | Brevo + django-anymail (`settings.py:168-191`) | CONVENTIONS §14: fixed in this batch. STACK, FEATURES, BRIEF, SYSTEM_INSTRUCTIONS, `.env.example`: batch 3 |
| Role assignment | Signal / adapter `save_user` | Model default: `User.role` defaults to `pet_owner` (`accounts/models.py:19-23`). The adapter no longer sets the role, and `accounts/signals.py` was deleted (2026-10-31). | GOOGLE_AUTH_NOTES.md updated 2026-10-31 |
| `MedicalRecord.appointment` | ForeignKey | OneToOneField (`medical/models.py:18`). DATA_MODELS.md:312 and :330 already say OneToOne. | Already correct; re-check in batch 2 (DATA_MODELS) |
| PDF receipts | server-side PDF (FEATURES.md:319, SYSTEM_INSTRUCTIONS) | browser print + `@media print` CSS (`shared/billing/_receipt_print.html`) | Batch 3 |
| Deploy branch | `main` (STACK.md; CONVENTIONS §13) | `rebuild` | CONVENTIONS §13: fixed in this batch. STACK.md: batch 3 |
| Team / ownership | Separate frontend teammate owns styling | Krys is the sole developer (backend and frontend) | Fixed (HANDOFF/CONVENTIONS) |
| URL structure and names | `role:app_action`, per-role includes | All apps at `""`, flat snake_case names | This batch (CONVENTIONS §5–§6) |
| Template tree | Old planned tree | Real tree | This batch (CONVENTIONS §3) |

---

## 5. BACKLOG (prioritized)

Tags show which part of the code an item touches, not who does it (Krys
does everything): [backend] = Python (models, views, settings),
[frontend] = templates, Tailwind, CSS, JS, [both] = both.

### DONE (2026-10-31)
- Local dev database: local PostgreSQL 18 `hapivet_dev`, no longer shared with live (see §3) — Done 2026-10-31
- Google sign-in hardening (adapters.py): auto-link only when Google's `email_verified` is true; admins never sign in with Google (linked or not); case-insensitive email match, refusing on multiple matches; inactive users refused; new signups with an unverified Google email refused; `SOCIALACCOUNT_LOGIN_ON_GET` and google `VERIFIED_EMAIL` removed; Google button is a POST form with CSRF — Done 2026-10-31 (see GOOGLE_AUTH_NOTES.md)
- Dead role code removed: `accounts/signals.py` deleted, `ready()` removed from `accounts/apps.py`, adapter `save_user` override removed — Done 2026-10-31
- Receipt lock: one `is_locked` (PAID or CANCELLED); every write view refuses locked receipts (incl. mark-paid on CANCELLED); `payment_status`/`payment_date` removed from `BillingReceiptForm`, so status changes only via the mark-paid/cancel actions — Done 2026-10-31
- ClinicSettings duplicates removed: first `logo` and first `opening_time` (both identical to the kept definitions; migrations only ever had one of each, so no migration expected) — Done 2026-10-31

### DONE (2026-10-16)
- Git tidy + backup branch `backup/pre-audit-2026-10-16` pushed
- Notifications 0003 sync: migration file + model field from b6e0ad3 brought into code; it was already applied to the DB in June from backend-dev (no SQL ran)
- Receipt print styles ported from frontend/admin (41632c5) + centering fix
- CLAUDE.md and .claude/settings.json safety rules (deny push/reset/checkout/switch/migrate etc.)
- Local email switched to console backend (USE_BREVO_EMAIL=False)

### NEXT
- B1: test trial-run and PR into rebuild (deploys frontend merges, 0003 sync, receipt styles)
- C: docs reconciliation (batches 2–3: DATA_MODELS; FEATURES/STACK/.env.example)

### SMALL FIXES (no schema change), one commit each
- **SECURITY** — `accounts/management/commands/create_admin.py` has a hardcoded password committed to a public repo. Fix: prompt for the password with `getpass` instead of a literal. Must be fixed before production, and any admin created with this command must get a new password. [backend]
- Normalize (lowercase) emails on save — the walk-in form doesn't (`accounts/forms.py` `WalkInOwnerForm.clean_email`) [backend]
- Flake8 cleanup [backend] — backend/.flake8 added in the docs commit; current count: 51.
- Re-save requirements.txt as UTF-8 [backend] — verified: currently UTF-16 LE with CRLF
- Logout always redirects to owner_login, even for admins [backend]
- Admin sidebar has no logout link [frontend]
- Logout accepts GET; convert to POST form (low priority) [both]
- Pricing on booking page: bring over commit 8b45d23 from backend-dev (no migration) [both]

### FRONTEND BRANCH REVIEW
- B3: review the `frontend/hannah-user` branch (owner templates) and decide, piece by piece, what to bring in and what to discard. It is no longer under active development. Check form field names and template tags against the current views; base_dashboard.html will conflict. [frontend]

### FEATURE
- Admin calendar page Phase 1 (reuse dashboard grid + day panel on its own page, add status filter). Not started. Start on a fresh branch cut from trial-run. Phases 2 (day timeline) and pet owner calendar are not in FEATURES.md yet — add them there before building. [both]
- Admin Google account linking (FEATURES 1.3), planned — build properly after the Google sign-in hardening: admin links Google from their own settings while logged in. [both]

### SCHEMA CHANGES (each needs a written migration plan first; merging into rebuild migrates the live DB)
- Double-booking: form-only check, no DB constraint or locking — decide constraint vs transaction/row lock [backend] — verified: no `UniqueConstraint`, `unique_together`, `select_for_update` or `atomic` in `appointments/`
- PetDeletionRequest: cancelling deletes the row; should become a CANCELLED status (choices change = new migration) [backend] — verified: `pets/views.py:518`
- Upload filename sanitization (Supabase rejects non-ASCII names); callable upload_to = new migration [backend] — verified: all `upload_to` values are plain strings
- Walk-ins without email are saved with `email=""` (`accounts/views.py:340,348`), which collides on the unique email constraint for the second one — decide nullable email vs. another approach [backend]
- Resend failed emails: review b6e0ad3's logic (written for Gmail SMTP before the Brevo switch in 8548613; must catch anymail errors). Model/migration already synced. [both]

### POLISH
- Bell & header: context processor providing unread_count + clinic (fixes red dot, header logo, admin login logo); then header.css / header_notifications.css / header_notifications.js fixes (Tailwind conflicts, overflow clipping, duplicate click-outside script in _panel.html, stuck active class) [both]
- ~29 multi-line <a> tags in 19 templates [frontend] — verified: 29 in 19 files
- Views over 50 lines (admin_walkin_create = 111) [backend]
- Mobile responsiveness [frontend]

### HOUSEKEEPING
- Consider moving the repo out of OneDrive
- Duplicate remote branches review / review-admin / trial-front; old branch feature/admin-calendar-page has no unique work — delete later — verified: `git log origin/trial-run..feature/admin-calendar-page` is empty
- Local master is behind origin/master — verified: 7 commits behind
- Hosting: Render free tier has uptime problems (cause unconfirmed). Candidates: Railway (Hobby, Asia region), Fly.io, VPS with Coolify/Appliku. On any host change update ALLOWED_HOSTS, CSRF_TRUSTED_ORIGINS, Google OAuth redirect URIs, the Django sites domain and all env vars.
- Production DB: plan is a NEW Supabase project (Singapore) for the clinic handover, not wiping the current one; the current project becomes staging. At cutover: update DATABASE_URL, AWS_*, MEDIA_URL and AWS_S3_CUSTOM_DOMAIN on Render, migrate, and create the admin with the clinic's real email.
- django-tailwind migration (STACK.md phase 2), not started. [frontend]
- Project documentation report: needs screenshots, ERD (Mermaid ERD already generated) and source artifacts. Deadline: ___
- Add .gitattributes (* text=auto) to stop LF/CRLF warnings — deferred until after B1 so line-ending churn doesn't mix with a deploy.

### DECISIONS PENDING
- PrescriptionItem delete path: allowed while editing a record, or immutable as the doc says?
- HealthArticle hard delete: keep (update doc) or switch to unpublish?
- Double-booking approach (see above)
- `frontend/hannah-user`: which parts (if any) are worth keeping, and whether to discard the rest (see B3)

---

## 6. Hard Rules for Claude Code

**Scope (from BRIEF.md and FEATURES.md):** no payment gateways, SMS, chat, AI
features, multi-vet or multi-clinic, staff roles, inventory, analytics,
DRF, WebSockets/Channels/Redis/Celery, Django built-in `/admin`, or
class-based views. If a task touches these, stop and flag it.

**Class-based view exception:** framework-provided auth views (Django's
built-in auth views, the `HapiVetPasswordResetView` subclass, and the root
`RedirectView` in `hapivet/urls.py`) are the only allowed class-based views.

**Business rules:** one owner per pet. Appointments go PENDING → CONFIRMED
(walk-ins are created as CONFIRMED). Only Admin writes medical records.
Medical records and vaccinations are never deleted (corrections use
`is_corrected` + `correction_note`). Private notes are never visible to
owners. Receipts lock when PAID or CANCELLED. No double-booking. Archives are soft
deletes.

**Database:**
- Never run `makemigrations`, `migrate`, `loaddata`, `flush` or any other
  DB-writing command without Krys's explicit approval.
- Never run migrate from a feature branch against the live DB (this caused
  the notifications 0003 situation in June, when the DB was still shared).
- Run the DB host check (§3) before every `migrate`; stop if the host is not
  `localhost`.
- Never put the live `DATABASE_URL` in `backend/.env`.
- Never edit or delete existing migrations.
- Update DATA_MODELS.md BEFORE changing models.
- For the live DB, use the Supabase **Session Pooler** string, not the Direct
  Connection.
- After bulk imports with explicit PKs, reset sequences.

**Templates and HTMX:**
- Write every `<a>` tag on one line. About 29 existing violations are
  tracked in the §5 backlog (POLISH).
- HTMX swallows 500 errors silently, so check server logs.
- Semantic HTML, no business logic in templates, HTMX partials prefixed
  with `_`, and short comments above complex template logic.
- Styling uses Tailwind utility classes plus per-page CSS files in
  `static/css/admin|owner|shared/`.
- Keep views under about 50 lines and move shared logic to `utils.py`.

**Git:**
- Never commit to `master`. Cut feature branches from `trial-run`.
- Commit format: `feat:`, `fix:`, `model:`, `style:`, `docs:`, `refactor:`, `test:`
- Run Black and Flake8 before every commit.
- Never commit `.env`, `media/`, `venv/`, `staticfiles/`.
- Update the relevant md files in the same commit as the code change.

---

## 7. How Krys Works (follow this)

- Audit first. Read the existing code before proposing changes, and don't
  write code until the approach is confirmed.
- Work in small batches: verify → plan → confirm → code → test → commit.
  Don't advance a phase until the current one is confirmed working.
- Ask before running anything non-trivial, and keep instructions minimal.
- Krys is a student, so explain the non-obvious parts briefly. Be more
  detailed when she asks to learn.
- Treat design references from other stacks as inspiration only.

---

## 8. First Task

Work through §5 BACKLOG in order.
