# HANDOFF.md — Hapi Vet Project State

> Last updated: 2026-11-01, after Slice 3a-2 B3 polish (date wording)
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

### ADMIN UI OVERHAUL (in progress)
Decisions (2026-10-31): Tailwind v4.3.3 standalone CLI with a compiled,
committed `static/css/app.css`; `templates/components/` includes; a new
`layouts/admin.html`, with admin pages migrated area by area (owner pages stay
on `base_dashboard.html` for now). Violet primary, blue links, status colours
unchanged. Light only, no dark mode. Desktop-first but usable on a phone.
Senior-friendly rules in CONVENTIONS §11 (the main user is an elderly vet).
List pages link to full detail pages; the DOM-scraping split panes and
modals in `static/js/admin/*.js` are not ported. Approved mockups: dashboard
(stat cards; today's schedule next to a mini calendar; recent activity next
to needs-approval) and the appointments list.
- Slice 0 foundations: Tailwind build, pinned HTMX 2.0.11 in `static/vendor/`, components — Done 2026-10-31
- Slice 1 shell + dashboard: new `layouts/admin.html` with sidebar badges and logout; top bar search and Notifications; admin notification panel; redesigned dashboard with day schedule and mini calendar; shell context processor; Manila dates; 403/HX-Redirect for HTMX — Done 2026-11-01
- Slice 1.5 — new frame on all admin pages (legacy CSS layer); sidebar trimmed to 9 items in 3 groups: Deletion requests is a tab on Pets, Services a tab on Billing, Clinic info a button in Clinic settings — Done 2026-11-01
- Slice 2 admin login: new `layouts/auth.html`; redesigned admin login (clinic logo/name, large fields, Show password, Caps Lock warning, one generic error); `login.css` retired — Done 2026-11-01
- Slice 3 appointments (list, detail, walk-in, calendar page) — in progress
  - 3a-2 B1 detail backend: approve refuses past dates; `_notify_owner` helper; reschedule message "moved by the clinic… The new time is confirmed."; detail context flags for the redesign; `?as=buttons` slot partial; `long_date` and `species_icon` filters — Done 2026-11-01
  - 3a-2 B2 shared components: icon `stroke`, `pet_avatar`, `owner_avatar`, `dialogs.js`, `topbar_search` block — Done 2026-11-01
  - 3a-2 B3 detail page redesign: `admin/appointments/detail.html` on the new layout (no legacy CSS) with includes in `detail_parts/`; status banner per state, details + internal notes cards, actions card (first on small screens), native dialogs for approve/decline/complete/no-show/cancel/move; move dialog uses `_slot_buttons.html`; dialogs reopen on form errors; context adds `medical_record` and `receipt`; admin form labels ("Reason", "Details", "Reason for the change") and a notes placeholder; `species_icon` matches plurals — Done 2026-11-01. `static/css/admin/appointment_detail.css` stays until B5 (list.html still uses it).
  - 3a-2 B3 polish: `friendly_date` / `long_date` take an optional `"mid"` argument (lowercase today/tomorrow/yesterday inside a sentence; CONVENTIONS §10 "Date filters"), used in the status banners, the approve/decline/complete/no-show/cancel dialogs ("Confirmed for today at 11:00 AM") and the admin flash messages (`_when`, `_too_early_message`: "Confirmed Bantay for today at 9:00 AM."); `long_date` leaves out the year in the current year; `_slot_buttons.html` shows a "Free times on {long_date}" heading ("Free times today, Sunday, November 1") from `slots_date` (slot view) / `reschedule_date` (`_reschedule_slot_context`), none before a date is picked; the move dialog's legend is "New time" — Done 2026-11-01
  - 3b B4 list backend: `admin_appointment_list` rewritten on helpers in `appointments/utils.py` (`tab_counts`, `resolve_list_tab`, `filter_appointments`, `parse_list_date`, `group_by_day`, `list_tabs`, `unfinished_visits`, `list_querystring`, `paging_links`). Tabs `pending` ("Needs approval"), `upcoming` (CONFIRMED from today; `confirmed` is an alias), `completed`, `cancelled` (excludes moved originals), `no_show`, `all`, plus hidden `unfinished` (CONFIRMED before today). Default tab: pending when any exist, else upcoming; unknown values fall back to it. `?q=` (every word matches pet name or owner first/last name) and `?date=` search every status and show "All"; a bad date is ignored. Work tabs sort oldest first, history tabs newest first; 25 per page. Tab counts are one aggregate query, unaffected by search. Rows carry `is_moved` (an Exists subquery) for the "Moved" label. Context for B5: `active_tab`, `tabs`, `day_groups`, `page_obj`, `page_query`, `prev_page_query`/`next_page_query`, `q`, `date_filter` (a date), `is_searching`, `unfinished_count`, `unfinished_visit` (only when exactly one). `appointments` and the HX-Request partial are kept for the old template — Done 2026-11-01. The old `list.html` still renders but its tabs/counts look off until B5.
  - 3b B5 list template redesign — next
- Slice 4 pets and owners (incl. the broken `<a>` tags in deletion_requests)
- Slice 5 medical
- Slice 6 billing (receipt print must stay a direct child of `<body>`, with no transform/filter on any ancestor)
- Slice 7 health + clinic settings (`escapejs` on article titles; server-rendered settings tabs)
- Slice 8 admin mobile pass; then the owner side
- Open: `form_field.html` needs a fieldset/legend branch before a RadioSelect or CheckboxSelectMultiple form is migrated.
- Notes for later slices:
  - Slice 6 (billing): replace `{% url %}` inside `onclick` with `data-*` attributes wired by one script; add `for=` to the Add Item labels; keep typed values when the form shows errors.
  - Owner side: `owner/onboarding/step2.html` and `owner/pets/edit.html` extend no base template (no Tailwind/HTMX/frame). These are real bugs.
  - Clinic info (shared/clinic_info.html) picks its frame by role: admin layout for admins, base_dashboard.html for owners.
- Every slice: rebuild `app.css` before committing; test at 150% zoom; re-run collectstatic locally once a template references `app.css` or vendored HTMX (tests use the manifest).

### SMALL FIXES (no schema change), one commit each
- **SECURITY** — `accounts/management/commands/create_admin.py` has a hardcoded password committed to a public repo. Fix: prompt for the password with `getpass` instead of a literal. Must be fixed before production, and any admin created with this command must get a new password. [backend]
- Normalize (lowercase) emails on save — the walk-in form doesn't (`accounts/forms.py` `WalkInOwnerForm.clean_email`) [backend]
- Flake8 cleanup [backend] — backend/.flake8 added in the docs commit; current count: 51.
- Re-save requirements.txt as UTF-8 [backend] — verified: currently UTF-16 LE with CRLF
- Logout always redirects to owner_login, even for admins [backend] — Done 2026-11-01
- Admin sidebar has no logout link [frontend] → UI overhaul Slice 1 — Done 2026-11-01
- Logout accepts GET; convert to POST form (low priority) [both]
- Pricing on booking page: bring over commit 8b45d23 from backend-dev (no migration) [both]
- UTC dates: remaining `timezone.now().date()` in booking/slot logic and other views — `appointments/utils.py` `get_available_slots`, `is_slot_available`, `get_available_dates`; `appointments/views.py` `owner_appointment_list` (`admin_appointment_list` done 2026-11-01); `dashboard/views.py` `_get_owner_dashboard_context`; `billing/views.py:203`, `:368`; `medical/views.py:87`, `:318`; plus `appointments/forms.py` (`AdminAppointmentRescheduleForm.clean_date` done 2026-11-01), `pets/forms.py`, both `models.py` files and `send_reminders.py` → use `timezone.localdate()`, with tests for the same-day cutoff [backend]
- ClinicSettings is fetched two ways: `get_or_create(pk=1)` on the settings page vs `.first()` everywhere else — add one helper [backend]
- Admin reschedule uses owner booking limits (30-day window, same-day cutoff); same-day slots aren't filtered by the current time [backend]
- Reschedule notification uses type `appointment_approved` (follows the owner's status-email preference) [backend]
- `notification_mark_read` view is unused (dead code) [backend]
- `vaccinations_due` counts corrected vaccinations; `followups_due` includes archived pets [backend]
- DATA_MODELS and HANDOFF §6 say MedicalRecord has `is_corrected`; only Vaccination does — fix the docs [backend]
- owner_login and admin_login ignore ?next=, so after logging in users always land on their dashboard instead of the page they were on [backend]
- `base.html`'s "commented-out" messages loop still runs (Django tags run inside HTML comments), silently using up messages on owner pages that have no loop of their own. Fix on the owner side: delete the block, don't un-comment it. [frontend]
- Admin password reset: the reset flow is owner-branded and ends on the owner login; build an admin version if needed. [both]
- Admin login has no rate limiting or lockout. [backend]
- Password reset email hard-codes `http://` (ignores the protocol). [backend]
- Pet-mismatch guard: `admin_medical_record_create` (`?appointment=` is never checked against the `pet_pk` in the URL) and `admin_receipt_create` (no check that the appointment can take a receipt) — refuse an appointment that belongs to another pet [backend]
- Walk-in same-day cutoff bug: `AdminWalkInAppointmentForm` says walk-ins bypass the same-day cutoff, but its time choices come from `get_available_slots`, which drops today's slots before the cutoff, so a walk-in today before the cutoff can't be booked. Planned for Slice 3c (walk-in) [backend]

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
- Distinct "declined" cancellation reason: a clinic decline and a clinic cancel with reason Other are stored the same way (`cancellation_reason=other`, `cancelled_by`=admin), so the detail page can't tell them apart. Needs a new `CANCEL_CHOICES` value (choices change = new migration) [backend]
- Notes "last saved" timestamp: the detail page can't show when the internal notes were last saved (`updated_at` changes on every save). Needs a new field; low priority [backend]
- Resend failed emails: review b6e0ad3's logic (written for Gmail SMTP before the Brevo switch in 8548613; must catch anymail errors). Model/migration already synced. [both]

### POLISH
- Bell & header: context processor + admin notification panel; the unused header CSS/JS files are deleted [both] → UI overhaul Slice 1 — Done 2026-11-01
- Multi-line `<a>` tags [frontend] — 2026-10-31 audit: 53 in admin/shared templates across 20 files, plus 2 broken tags in `admin/pets/deletion_requests.html`. Fixed slice by slice as files are touched.
- Views over 50 lines (admin_walkin_create = 111) [backend]
- Mobile responsiveness [frontend] → UI overhaul Slice 8

### HOUSEKEEPING
- Consider moving the repo out of OneDrive
- Duplicate remote branches review / review-admin / trial-front; old branch feature/admin-calendar-page has no unique work — delete later — verified: `git log origin/trial-run..feature/admin-calendar-page` is empty
- Local master is behind origin/master — verified: 7 commits behind
- Hosting: Render free tier has uptime problems (cause unconfirmed). Candidates: Railway (Hobby, Asia region), Fly.io, VPS with Coolify/Appliku. On any host change update ALLOWED_HOSTS, CSRF_TRUSTED_ORIGINS, Google OAuth redirect URIs, the Django sites domain and all env vars.
- Production DB: plan is a NEW Supabase project (Singapore) for the clinic handover, not wiping the current one; the current project becomes staging. At cutover: update DATABASE_URL, AWS_*, MEDIA_URL and AWS_S3_CUSTOM_DOMAIN on Render, migrate, and create the admin with the clinic's real email.
- django-tailwind: no longer planned; replaced by the Tailwind standalone CLI (CONVENTIONS §11).
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
(walk-ins are created as CONFIRMED). Admin can cancel PENDING or CONFIRMED
appointments. An admin reschedule cancels the original and creates a new
CONFIRMED appointment linked by `rescheduled_from` (the clinic chose the
slot). Appointment notes are internal (never shown to owners). Only Admin
writes medical records.
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
- Write every `<a>` tag on one line. 53 existing violations
  (admin/shared) are tracked in the §5 backlog (POLISH).
- HTMX swallows 500 errors silently, so check server logs.
- Semantic HTML, no business logic in templates, HTMX partials prefixed
  with `_`, and short comments above complex template logic.
- Styling: migrated admin pages use the compiled `static/css/app.css`
  (Tailwind v4, CONVENTIONS §11) and `templates/components/`; all other
  pages still use the Tailwind CDN plus per-page CSS files in
  `static/css/admin|owner|shared/`. Rebuild `app.css` before every
  frontend commit.
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
