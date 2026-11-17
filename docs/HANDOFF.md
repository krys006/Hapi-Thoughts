# HANDOFF.md — Hapi Vet Project State

> Last updated: 2026-11-17 (trimmed; full history in HANDOFF_ARCHIVE.md)
> §3–§5 describe the verified state as of the date on each entry. Anything marked
> "(unverified)" could not be confirmed from the code. Where the code and this
> file disagree, the code wins: report the differences, then propose doc fixes.

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

Last run 2026-10-16; the full protocol is in HANDOFF_ARCHIVE.md §2.

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
| PDF receipts | server-side PDF (SYSTEM_INSTRUCTIONS; FEATURES.md said so too, now fixed in 5.3 "Save as PDF from the print window") | browser print + `@media print` CSS (`shared/billing/_receipt_print.html`) | FEATURES: fixed in Slice 6. SYSTEM_INSTRUCTIONS: batch 3 |
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
- Local dev database (local PostgreSQL 18 `hapivet_dev`); Google sign-in hardening (adapters.py); dead role code removed (`accounts/signals.py`, adapter `save_user`); receipt lock (one `is_locked`); ClinicSettings duplicate fields removed — Done 2026-10-31

### DONE (2026-10-16)
- Git tidy + backup branch; notifications 0003 sync; receipt print styles port; CLAUDE.md and `.claude/settings.json` safety rules; local console email backend — Done 2026-10-16

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
modals in `static/js/admin/*.js` are not ported (the billing pages kept theirs
until 6b; none is left since 6b-4). Approved mockups: dashboard
(stat cards; today's schedule next to a mini calendar; recent activity next
to needs-approval) and the appointments list.
- Slice 0 foundations: Tailwind build, pinned HTMX 2.0.11 in `static/vendor/`, components — Done 2026-10-31 (details: archive)
- Slice 1 shell + dashboard: new `layouts/admin.html`, sidebar badges, top bar search, notification panel, redesigned dashboard — Done 2026-11-01 (details: archive)
- Slice 1.5 new frame on all admin pages; sidebar trimmed to 9 items (10 since Services split out 2026-11-16) — Done 2026-11-01 (details: archive)
- Slice 2 admin login: new `layouts/auth.html`, redesigned admin login, `login.css` retired — Done 2026-11-01 (details: archive)
- Slice 3 appointments (list, detail, walk-in, calendar page) — in progress
  - 3a-2 B1 detail backend: approve refuses past dates, `_notify_owner` helper, detail context flags, `?as=buttons` slot partial — Done 2026-11-01 (details: archive)
  - 3a-2 B2 shared components: icon `stroke`, `pet_avatar`, `owner_avatar`, `dialogs.js`, `topbar_search` block — Done 2026-11-01 (details: archive)
  - 3a-2 B3 detail page redesign on the new layout with native dialogs, plus the `"mid"` date-filter polish — Done 2026-11-01 (details: archive)
  - 3b B4 list backend: `admin_appointment_list` rewritten on helpers in `appointments/utils.py` (tabs, search, paging) — Done 2026-11-01 (details: archive)
  - 3b B5 list template redesign: new layout, grouped by day, tabs, search, pager; old JS/CSS deleted — Done 2026-11-01 (details: archive)
  - PR `feature/admin-appointment-detail` into `trial-run` (#12) — Done 2026-11-01
  - Appointments list v4 (mockup `appointments_list_v4.html`): period filter (`?show=&from=`), top-bar search, day cards — Done 2026-11-02 (details: archive)
  - Appointments list v5 design follow-up (mockup `appointments_list_v5.html`): period row as heading, Dates group, light-violet Open button — Done 2026-11-02 (details: archive)
  - Slice 3c walk-in (mockup `walkin_v1.html`): walk-ins default to today, future dates allowed; owner cutoff bug fixed 2026-11-02; nullable `User.email` stays a separate migration (see SCHEMA CHANGES) — decisions 2026-11-01 (details: archive)
    - 3c-1 walk-in backend fixes: `walk_in=True` slot rules, email normalising, `accounts/utils.py`, claim-email failures handled — Done 2026-11-01 (details: archive)
    - 3c-2 walk-in flow: find owner, pick pet and time, review step, `create_walkin_appointment`, new-owner page — Done 2026-11-01 (details: archive)
    - 3c-3 walk-in design pass: step indicator, "Now" slot label, Book walk-in links, plus the step 1 owner list and filter as you type — Done 2026-11-02 (details: archive)
- Dashboard v5 (mockup `dashboard_v5.html`): month-dropdown calendar with count pills, day endpoint, no model changes (batches D1–D5) — Done 2026-11-02
  - Build decisions (schedule rows, "Next", Needs approval, stat links, calendar state) — 2026-11-02 (details: archive; the Recent activity decision no longer applies since D5)
  - D1 backend part 1: `dashboard/utils.py` calendar helpers, `cal_*` keys, stat fixes — Done 2026-11-02 (details: archive)
  - D2 backend part 2: day schedule, pending requests, day endpoint with out-of-band calendar — Done 2026-11-02 (details: archive)
  - D3 templates: header, stat cards, Needs approval, schedule, month calendar; old keys and helpers removed — Done 2026-11-02 (details: archive)
  - D4 Needs approval always shown; calendar legend removed (add it back if Dr. Aga asks what yellow means) — Done 2026-11-02 (details: archive)
  - D5 Recent activity card removed from the admin dashboard — Done 2026-11-02 (details: archive)
- Shell polish: sidebar 224 px, clinic name on two lines, logo links to the dashboard — Done 2026-11-02 (details: archive)
- Slice 4 pets and owners (incl. the broken `<a>` tags in deletion_requests — fixed in 4b-2) — Done 2026-11-03 (4a-1 to 4b-4)
  - Slice 4a-1 owners backend: claim-email handling, resend link, archive/restore, Add owner (`admin_owner_create`), owners list and detail contexts — Done 2026-11-02 (details: archive)
  - Slice 4a-2 owners list template: new layout, search, Active/Archived tabs, "Add owner" button; old JS/CSS deleted — Done 2026-11-02 (details: archive)
  - Slice 4a-3 owner detail: rebuilt page with account card, dialogs, `owner_account_state` — Done 2026-11-02 (details: archive)
  - Slice 4a-4 owner forms: Marinduque town/barangay dropdowns from PSGC data, address rules, photo button — Done 2026-11-02 (details: archive)
  - Slice 4b-1 pets backend: pets list context, deletion requests, Archive/Keep, restore, pet detail context, archived guards — Done 2026-11-03 (details: archive)
  - Slice 4b-2 pets list + deletion requests rebuilt on the new layout; old tabs/JS/CSS deleted — Done 2026-11-03 (details: archive)
  - Slice 4b-3 pet detail rebuilt; direct archive by the clinic (`admin_pet_archive`) — Done 2026-11-03 (details: archive)
  - Slice 4b-4 pet form: species/breed dropdowns (`PET_BREEDS_BY_SPECIES`), photo button, back links — Done 2026-11-03 (details: archive)
  - Owner-side pet forms: same species/breed dropdowns and photo button (owner-side slice).
  - Owner-side pet pages: check how a clinic-made PetDeletionRequest (requested_by = admin) shows to the owner, if at all.
  - Owner-side onboarding/profile and the walk-in New owner page: same address dropdowns (later slice).
  - Owner form hand-typed note — Decided 2026-11-02: shown only for a saved Marinduque address that is off the list (details: archive)
  - Unbalanced `<div>`s in `owner_detail.html` and `pet_detail.html` — Done (fixed by the 4a-3 and 4b-3 rebuilds)
- Slice 5 medical (approved mockups `docs/mockups/medical_lists_v1.html`, `record_detail_v1.html`, `record_form_v1.html`, `vaccination_form_v1.html`, `pet_health_history_v1.html`, all 2026-11-03; decisions in `docs/mockups/medical_v1_decisions.md`) — complete (5a-1 to 5b-5, 2026-11-06; see "Slice 5 complete" below)
  - Slice 5a-1 vaccination rules: edit-form reasons for clinical changes, vaccine name, date checks; Correct view removed — Done 2026-11-03 (details: archive)
  - Slice 5a-2 record, medicine and file rules: record dates, visit checks, same-day medicine removal, file types and size, archive guards — Done 2026-11-03 (details: archive)
  - Slice 5a-3 shared due rule: `vaccinations_due` / `followups_due`, superseded doses, reminder refusals — Done 2026-11-03 (details: archive)
  - Slice 5a-4 list contexts: vaccination and record tabs, search, paging, dashboard links, one query each — Done 2026-11-03 (details: archive)
  - Slice 5a-5 detail, form and reminder backend: dialog error re-renders, honest reminder flashes, last-reminder lines, history contexts — Done 2026-11-03 (details: archive)
  - Slice 5b-1 Vaccinations and Medical records lists rebuilt, with reminder dialogs and `return_to` — Done 2026-11-05 (details: archive)
    - Left for later: The old context keys `search_query` / `total_count` are still returned (5a-4 tests read them) but no template uses them.
  - Slice 5b-2 Medical record detail rebuilt, with medicine and file dialogs; old inline vaccination form and `admin_vaccination_add` removed — Done 2026-11-05 (details: archive)
    - (resolved in 5b-5)
  - Slice 5b-3 Medical record form (new + edit) rebuilt, with follow-up toggle and `record_form.js` — Done 2026-11-06 (details: archive)
  - Slice 5b-4 Vaccination form (new, new on a record, edit) rebuilt, with correction card and shared date wording — Done 2026-11-06 (details: archive)
    - (resolved in 5b-5)
  - Slice 5b-5 Pet medical history + pet vaccinations rebuilt, with highlight, follow-up banner and sidebar `nav_section` — Done 2026-11-06 (details: archive)
  - Slice 5 complete (2026-11-06). Across 5a/5b: one shared due rule (dashboard, lists, pet page, reminders, nightly job: superseded doses and archived pets/owners left out, corrected doses counted); vaccination edits as dated corrections (Correct view removed); record, medicine and file rules (dates, visit checks, same-day medicine removal, PDF/JPG/PNG up to 10 MB, soft-removed files, archived guards); honest reminder flashes, confirm dialogs and `return_to`; per-pet last-reminder lines; and all seven admin medical pages rebuilt on `layouts/admin.html` (Vaccinations and Medical records lists, record page, record form, vaccination form, the pet's medical history and vaccinations), with no medical page on `legacy_css` any more. Still open from Slice 5 (backlog): test files are public URLs (decide before go-live); read-only live count of doses wrongly flagged Corrected by the old edit bug; Record-created notification type (choices migration); upload filename cleaning (`upload_to`, migration); link a reminder Notification to its Vaccination / MedicalRecord (the last-reminder line is per pet until then); move `error_summary.html` to `components/`; the old list context keys `search_query` / `total_count` (no template uses them); the vaccination Send reminder from the history comes back without the highlight; rebuild `app.css`. The branch `feature/admin-medical` is ready for the PR into `trial-run` (after the app.css rebuild and the full suite).
- Slice 6 billing (approved mockups `docs/mockups/receipt_detail_v1.html`, `receipt_list_v1.html`, `receipt_new_v1.html`, `services_v1.html`, all 2026-11-06; decisions in `docs/mockups/billing_v1_decisions.md`; receipt print must stay a direct child of `<body>`, with no transform/filter on any ancestor) — complete (6a-1 and 6a-2 done 2026-11-07, 6a-3 and 6a-4 done 2026-11-08, 6b-1 to 6b-4 done 2026-11-15; see "Slice 6 complete" below)
  - Slice 6a-1 receipt rules: totals and discount rounding, discount and item forms, cash only, row locks, numeric receipt numbers — Done 2026-11-07 (details: archive)
  - Slice 6a-2 New receipt flow: visit, owner and pet modes with guards, plain template — Done 2026-11-07 (details: archive)
  - Slice 6a-3 receipt page + list contexts: tabs, search, paging, dialog error re-renders; old edit page removed — Done 2026-11-08 (details: archive)
  - Slice 6a-4 owner side Unpaid/Paid only, receipt-paid notification, appointment banner, services backend rules — Done 2026-11-08 (details: archive)
  - Slice 6b-1 Receipt list rebuilt, with shared billing tabs — Done 2026-11-15 (details: archive)
  - Slice 6b-2 Receipt page + print rebuilt, with dialogs, `peso` filter and new print partial — Done 2026-11-15 (details: archive)
  - Slice 6b-3 New receipt design pass + filter as you type — Done 2026-11-15 (details: archive)
  - Slice 6b-4 Services list + form rebuilt, with price cards and `service_form.js` — Done 2026-11-15 (details: archive)
  - Slice 6 complete (2026-11-15). Across 6a/6b: the billing rules of the decisions doc (`docs/mockups/billing_v1_decisions.md`, approved 2026-11-06, 31 rules): one receipt per visit, discount / rounding / item rules, cash only, row locks and numeric receipt numbers, the create flow with its guards, the owner side (Unpaid and Paid only, one notification at mark paid), the appointment page banner, and the service rules (unique names, saving clears the other pricing type, a new service starts as Hidden (draft)); and all six admin billing pages rebuilt on `layouts/admin.html` from the four approved mockups (`receipt_list_v1`, `receipt_detail_v1`, `receipt_new_v1`, `services_v1`): the receipt list, the receipt page with its dialogs and print, New receipt with filter as you type, the services list and the service form, with no billing page on `legacy_css` any more. The docs checklist of the decisions doc ("Docs to fix during the slice") was checked in 6b-4: DATA_MODELS, FEATURES, CONVENTIONS and HANDOFF are consistent with the code. Still open from Slice 6 (backlog): cancel reason on receipts (new field, migration); an own notification type for "receipt paid" (choices, migration; it uses `billing_generated`); recording GCash / bank transfer as a payment method (FEATURES expansion point; `payment_method` always saves "Cash"); the admin global search doesn't search receipts (the receipt list has its own search); the owner receipt page still has its own inline `onclick="window.print()"` (owner-side slice); the admin layout's own inline `<script>` and the `shared/modals.html` include (noted after 6b-3: the layout wires the sidebar and Escape / click-outside in a script written in the layout, and the health articles page still calls `openConfirmModal`; move them to a file and drop the include when Slice 7 removes the last `openConfirmModal` call); `form_field.html` still has no fieldset / legend branch (the radio cards are written by hand); `app.css` rebuild + collectstatic locally. Before-live items carried from Slice 5 (do not lose before the deploy): the test result files are public URLs (decide); a read-only count on live of vaccinations wrongly flagged Corrected; and the deploy needs a written migration plan from the `rebuild...trial-run` migrations diff, with a trial-run of the migrations and a Supabase backup first. The branch `feature/admin-billing` is ready for the PR into `trial-run` (after the `app.css` rebuild and the full suite).
  - Services split out of Billing into its own sidebar page "Services and prices" (new `tag` icon; billing tabs now Unpaid, Paid, Cancelled, All) — Done 2026-11-16 (details: archive)
  - Walk-in and New receipt pages centered in one `max-w-…` column (templates and tests only) — Done 2026-11-16 (details: archive)
- Slice 7 health + clinic settings (`escapejs` on article titles; server-rendered settings tabs). Decisions: `docs/mockups/health_settings_v1_decisions.md`. (Sub-batch dates are not recorded in this file.)
  - Slice 7a-1 `get_clinic_settings()` helper and the clinic settings page use it (decision 4) — Done (details: archive)
  - Slice 7a-2 every other ClinicSettings reader uses `get_clinic_settings()`; no behaviour change — Done (details: archive)
  - Slice 7a-3 health library backend: unpublish only, `admin_health_article_delete` removed, drafts on create — Done (details: archive)
  - Slice 7a-4 blocked dates backend: busy-day warning, panel-only errors, booking limit 1 to 365 — Done (details: archive)
  - Slice 7a-5 settings tabs, per-tab saving, schedule warning, banner data — Done (details: archive)
  - Slice 7a-6 clinic logo file check (JPG/PNG/WebP, up to 2 MB) — Done (details: archive)
  - Slice 7b-1 Health library list and article form on the admin layout, with publish dialogs — Done (details: archive)
  - Slice 7b-2 Clinic settings page, option B: banner, `?tab=` tabs, per-tab forms — Done (details: archive)
  - Slice 7b-3 Blocked dates tab in Tailwind, with remove dialogs — Done (details: archive)
  - Slice 7b-4 Clinic info page on the admin frame (owners unchanged) — Done (details: archive)
  - Slice 7b-5 legacy CSS layer retired from the admin side; `shared/modals.html` dropped from the admin layout — Done (details: archive)
  - **Slice 7 done.** Health library (unpublish only, no delete; list and form on the admin layout with publish dialogs), clinic settings (server-rendered `?tab=` tabs, per-tab saves, schedule warning, banner, blocked dates with busy-day warning, logo check, booking limit 1 to 365), `get_clinic_settings()` as the one reader of `ClinicSettings`, the admin clinic info page, and the legacy layer gone from the admin side. Admin pages are all on `layouts/admin.html` with compiled Tailwind and native dialogs. Files deleted over Slice 7 (by Krys): `static/js/admin/article_list.js`, `static/css/admin/article_list.css`, `article_form.css`, `static/js/admin/clinic_settings.js`, `static/css/admin/clinic_settings.css`. Backlog: overlap check on booking (exact start time today; a slot-length change can allow overlaps); owner health pages (Slice 8); public S3 test-result URLs (before go-live); `create_admin.py` hardcoded password (use `getpass`); move the admin layout's inline script to a static file (needs collectstatic).
- Slice 8 owner side (in progress; decisions in docs/mockups/owner_mockups/owner_v1_decisions.md; batches 8a-0 to 8b-10)
  - 8a-0 (2026-11-16): PetDeletionRequest CANCELLED and Appointment cancellation_reason DECLINED added (choices-only, pets/0003, appointments/0004).
  - **Slice 8a-1a owner-side fixes — 2026-11-16** (backend only; no migration, no restyle):
    - Owner-row guard: new `@owner_profile_required` in `accounts/utils.py` (under `@login_required`; other roles pass through; no PetOwner row → redirect to onboarding step 1, `HX-Redirect` for HTMX; else sets `request.owner`). On every owner view in `appointments/views.py`, `billing/views.py`, `medical/views.py`, `dashboard/views.py`, `pets/views.py` (not the onboarding views). Step 1 now skips to the dashboard only when the user is onboarded and has a row (otherwise a loop).
    - Onboarding step 1 re-entry: `pets/views.py` binds the form to the user's PetOwner when there is one (GET shows saved values and links, POST updates); same redirect after success.
    - Registration email: `accounts/views.py` catches a send error, logs it to `accounts.views`, keeps the account, goes to "check your email" with a warning message.
    - Contact links: `save_owner_profile` and `contact_link_initial` in `pets/utils.py`; the profile save and the link delete and re-add run in one `transaction.atomic()` (step 1 and profile edit).
    - Owner pet edit: new `OwnerPetEditForm` in `pets/forms.py` (name, breed, color, weight, photo); `owner_pet_edit` uses it, so species, sex, birthday and registration no. can't change. `owner/pets/edit.html` is untouched (still renders; its locked fields show empty until 8b).
    - Terms and Privacy: `TERMS_URL` and `PRIVACY_URL` in `settings.py`; `owner/register.html` reads `terms_url` and `privacy_url` from the view.
    - allauth pages: `allauth_login_redirect` and `allauth_signup_redirect` in `accounts/views.py`, added in `hapivet/urls.py` before the allauth include; a safe `?next=` is kept.
    - Tests added: accounts `OwnerProfileRequiredTests`, `RegisterEmailFailureTests`, `AllauthRedirectTests`, `TermsPrivacyLinkTests`; pets `OnboardingStepOneReentryTests`, `OwnerPetEditFormTests`, `ContactLinksAtomicTests`.
    - Tests changed: none (no assertion changed, loosened or removed).
  - **Slice 8a-1b appointments and status fixes — 2026-11-17** (backend; no migration; templates touched only for the logout forms):
    - Booking lock: `lock_schedule()` in `appointments/utils.py` (`select_for_update()` on the lowest-pk ClinicSettings row inside `transaction.atomic()`; no row, no lock, no error). Owner booking (`create_owner_booking`), owner reschedule (`reschedule_by_owner`), walk-in (`create_walkin_appointment`) and admin move (`_reschedule_appointment`) lock, check the slot again, then save. A slot taken meanwhile: owner forms come back (200) with every value but the time and the "{time} was just taken…" error on the time field; walk-in and admin move keep their existing taken handling.
    - Owner reschedule is one transaction under that lock: the original is re-read with `select_for_update()`; no longer PENDING or CONFIRMED (or not the owner's) means nothing changes and the owner goes to the visit page with "This visit can't be changed any more." Admin is notified after the block, never inside it.
    - Take back a removal: `owner_pet_cancel_delete_request` is POST only and sets CANCELLED (never deletes; `reviewed_*` stay empty); no PENDING request gives an info message. The admin "Already answered" list and its count show only APPROVED and REJECTED; a clinic answer to a taken-back request is refused with "The owner took this request back."
    - Declined reason: `admin_appointment_reject` saves `DECLINED` (was OTHER). No code guessed "declined" from OTHER, so no fallback was needed; old rows stay OTHER.
    - Logout: `user_logout` logs out on POST only; a GET goes to the user's dashboard (logged out: owner login). The admin sidebar, the owner sidebar and the unused `owner/onboarding.html` now use a POST form with a CSRF token.
    - allauth side doors (`hapivet/urls.py` before the include): password reset pages → forgot-password, confirm-email pages → owner login, `inactive` → owner login with an error, `accounts/logout/` → `user_logout` itself. Google login and callback untouched.
    - Tests added: appointments `ScheduleLockTests`, `SlotTakenAtSaveTests`, `OwnerRescheduleAtomicTests`, `DeclinedReasonInUseTests`; pets `TakeBackRemovalTests`; accounts `LogoutPostTests`, `AllauthSideDoorTests`.
    - `accounts/middleware.py`: `/logout/` added to `PUBLIC_URLS`, so a logged-out visitor gets `user_logout`'s plain redirect to the owner login (before, the middleware sent them to `/login/?next=/logout/`).
    - Tests changed (on purpose): dashboard `LogoutRedirectTests` (2) log out with a POST (were: GET); appointments `RejectTests.test_message_is_stored_and_sent_to_owner` expects `DECLINED` (was: `OTHER`). Re-anchored (same check still runs): pets `AddPetReturnToWalkInTests.test_kept_through_a_failed_post` now picks the first `<form>` that isn't the sidebar's Log out form (the first form on the page is now that one).
  - **Slice 8a-2a owner helpers and contexts — 2026-11-17** (backend; no migration; the only template touched is `owner/pets/add.html`, a hidden `next` field). `app.css` needs no rebuild (no new classes); no new static file, so no collectstatic.
    - 1 Late note: `is_within_24h(appointment, now)` in `appointments/utils.py` (start less than 24 h after now, or already passed); `Appointment.is_within_24h` property (timezone.now()); `is_cancellable_freely` is now `not is_within_24h` (only difference: exactly 24:00 away, was "not free", now "free"). `owner_appointment_cancel` and `owner_appointment_reschedule` pass `is_late` on GET and a failed POST (one `timezone.now()` read per view).
    - 2 Reminder timing: `NotificationPreferenceForm` uses TypedChoiceFields (appointment 0/1/2/3 "On the same day", "N days before"; vaccination 3/7/14/30); a saved value outside the list is one extra choice "N days before (your earlier choice)"; any other value: "Choose one of the options." `send_reminders` needed no change: it fires on `[reminder_days, 0]`, so with 0 days it is one match and one reminder (the day-of dedupe also holds). The old preferences template still prints "day(s) before" after the select, so it reads "1 day before day(s) before" until 8b-9.
    - 3 `follow_up_visit(record)` in `medical/utils.py` (earliest Pending/Confirmed/Completed visit for the pet on or after `follow_up_date`; one query; not used by a page yet).
    - 4 `owner_booking_calendar(month_value, today, now, current_date=None)` in `appointments/utils.py`: keys `month`, `month_value`, `month_label`, `prev_month`, `next_month`, `cells` (35 or 42, Sunday first; each `date`, `day`, `in_month`, `state` past/closed/full/open, `is_today`, `is_current`, `aria_label`). 3 queries for any month (settings, blocked dates, held slots); agrees with `get_available_slots` on every day.
    - 5 `owner_book_appointment` reads `?pet=` through `picked_pet_initial` (own non-archived pet only); `owner_pet_add` takes `?next=book` (or the hidden `next` field) and goes to `owner_book_appointment?pet=<new pk>`; context key `next_value`.
    - 6 `owner_bills_context(owner, params, today)` in `billing/utils.py`, used by `owner_billing_list`: keys `receipts` (old), `to_pay`, `to_pay_count`, `to_pay_total`, `history` (list), `page_obj`, `q`, `period` (all/month/3m/year/last_year), `period_options`, `is_filtering`, `prev_page_query`, `next_page_query`, `today`. History is paid only, newest paid first (payment date, billing date when none), 25 a page. 3 queries (unpaid list, history count, history page); the old page now runs 8 (was 5).
    - Tests added: appointments `IsWithin24hTests`, `OwnerBookingCalendarTests`, `BookWithPetParamTests`; pets `AddPetNextBookTests`; notifications `ReminderChoiceTests`; medical `FollowUpVisitTests`; billing `OwnerBillsContextTests`.
    - Tests changed (on purpose): billing `OwnerBillingTests.test_list_query_count_is_flat` expects `OWNER_LIST_QUERIES = 8` (was 5): the Bills page now also loads the unpaid list and the paid History (count + page). Still flat (3 vs 30 receipts); no assertion removed or loosened.
  - **Slice 8a-2b owner notifications and dashboard backend — 2026-11-17** (backend; no migration; one new plain template `owner/notifications/list.html` plus its row partial `_row.html`, and one link line in the bell panel). `app.css` needs no rebuild (no new classes); no new static file, so no collectstatic.
    - 1 Compact text: `when_line(date, time, today)`, `short_day`, `compact_message` in `notifications/utils.py`; `notify(email_body=)` was already there (nothing changed in `notify()`). Every owner notification now has a short title, a compact message and the full sentence as the email body (friendly dates in every email); admin notifications unchanged. Table (site · type · new title / message · email body; old text is in the tests' "was" comments):
      - `appointments/views.py` `admin_appointment_approve` · appointment_approved · "Visit confirmed" / "Bantay · Thu, Nov 5 at 2:00 PM" · "Your appointment for … has been confirmed."
      - `admin_appointment_reject` (`utils.notify_request_declined`) · appointment_rejected · "Visit request declined" / "Bantay · when" + the clinic's message trimmed to 100 characters · full sentence + "Message from the clinic: …"
      - `admin_appointment_cancel` (`_notify_cancelled`) · appointment_cancelled · "The clinic cancelled a visit" / "Bantay · when. Please call us for a new time." · full sentence
      - `_notify_moved` · appointment_approved · "The clinic moved your visit" / "Bantay · now when. The new time is confirmed." · full sentence
      - `appointments/utils.py` `create_walkin_appointment` · appointment_approved · "Visit booked by the clinic" (kept) / "Bantay · when. The time is confirmed." · full sentence
      - `send_reminders.py` appointment · appointment_reminder · "Visit today" / "Visit tomorrow" / "Visit in N days" / "Bantay · when" · full sentence
      - `send_reminders.py` vaccination · vaccination_reminder · "Vaccine due soon" / "Vaccine due today" / "Bantay · Anti-rabies, due Nov 10" · full sentence
      - `send_reminders.py` follow-up · followup_reminder · "Follow-up visit" / "Follow-up visit today" / "Bantay · Follow-up on Nov 6" · full sentence
      - `medical/utils.py` `send_vaccination_reminder(vaccination, today)` and `send_followup_reminder(record, today)` (Send reminder buttons) · same titles and messages as the nightly ones · full sentence
      - `medical/utils.py` `notify_record_created(record, today)` · appointment_approved · "New medical record" / "Bantay · Record for Mon, Nov 2" · full sentence
      - `pets/utils.py` `notify_deletion_answer` and `archive_pet_directly` · pet_deletion_answered · "Pet removed" / "Bantay · Removed from your account, as you asked." (direct archive: "Bantay · Removed by the clinic (reason).") and "Pet stays on your account" / "Bantay · The clinic kept your pet. Call us if you still want it removed." · full sentence
      - `billing/utils.py` `notify_receipt_paid` · billing_generated · "Bill paid: ₱1,350.00" / "Bantay · HV-2026-0004. Paid on Tue, Nov 3." · "Paid ₱1,350.00 on Tue, Nov 3. You can view or print it in Billing."
      - Titles with no mockup example, written to its pattern: "Visit booked by the clinic", "Visit today / in N days", "Vaccine due today", "Follow-up visit today", "New medical record", "Pet removed", "Pet stays on your account", "Bill paid: …" (the mockup's "New bill" doesn't fit: the notification is sent at mark paid). Email subjects: fixed ones unchanged; the pet-removal and bill-paid subjects follow the new title ("Pet removed — Hapi Vet", "Bill paid: ₱1,350.00 — Hapi Vet").
    - 2 `notification_target(notification)` (moved out of `notification_open`, which uses it; same targets, None for nothing, `notification_open` still falls back to the dashboard) and `notification_icon(notification)` in `notifications/utils.py`. Icons: approved calendar-check, rejected and cancelled calendar-x, reminder clock, billing receipt, vaccination syringe, follow-up calendar-clock (the mockup's stethoscope isn't in `icon.html`), pet answer paw-print, any other type bell.
    - 3 `owner_notifications` at `/owner/notifications/` (`owner_notifications_context` in `notifications/utils.py`: Today / Earlier by Manila date, 25 a page, 3 queries); `notification_mark_all_read` takes POST `return_to=page` (owners only) and goes back to the page; anything else is unchanged. The bell panel has a "See all notifications" link for owners.
    - 4 `owner_dashboard_context(owner, today, now)` in `dashboard/utils.py`: `next_visit`, `is_pending`, `upcoming` (max 3, Pending or Confirmed only), `unpaid_count`, `unpaid_total`, `pets`, `is_new_owner`, `clinic`; 4 queries (5 for an owner with no pets), flat. `owner_dashboard` keeps the old keys; `upcoming_appointments` is now the next visit plus the upcoming ones (no Completed or No show any more). The 30-second polling attributes are gone from `_dashboard_partial.html`; the `owner_dashboard_refresh` view and URL stay but nothing calls them now.
    - Tests added: notifications `WhenLineTests`, `NotifyEmailBodyTests`, `OwnerNotificationTextTests`, `NotificationTargetTests`, `NotificationIconTests`, `OwnerNotificationsPageTests`; dashboard `OwnerDashboardContextTests`.
    - Tests changed (on purpose, new wording; no assertion loosened): notifications `send_reminders` titles ("Visit tomorrow", "Visit today" ×2); appointments reject / reschedule / walk-in / declined-reason notification checks (title and message now compact, the old sentence is asserted on the email body); billing receipt-paid title, message and subjects; medical friendly-date and record-created checks (read from the email body, new title "New medical record"); pets removal-answer titles, messages and subjects.
  - **Slice 8a-2c owner medical record page, account hub — 2026-11-17** (backend plus plain templates; no migration; new templates `owner/medical/record.html` and `owner/account/index.html`, one link line per row in `owner/medical/history.html`, a read-only email block in `owner/profile/edit.html`). `app.css` needs no rebuild (no new classes, no CSS); no new static file, so no collectstatic.
    - 1 `owner_medical_record` at `/owner/pets/<pet_pk>/medical/<pk>/` (`owner_record_context`, `owner_record_queryset`, `owner_test_files`, `owner_follow_up`, `owner_visit_receipt` in `medical/utils.py`): 404 unless the pet is the owner's and not archived and the record is that pet's; `private_notes` deferred; test files as text only (`title`, `added`); doses without `correction_note` ("Corrected by the clinic"); receipt only when Unpaid or Paid; empty sections left out; no print. Context: `record`, `pet`, `medicines`, `test_files`, `vaccinations`, `follow_up` (None or `date`, `days`, `visit`), `visit`, `receipt`, `today`. Five queries in the context however many rows. The history page links each record.
    - 2 `owner_account` at `/owner/account/` (`accounts/views.py`): `owner` in the context; the template lists the links and has the POST Log out form. My details: the profile form already had no email field (nothing to remove); the page now shows the email as text with "To change your email, contact the clinic."; a posted email changes nothing.
    - 3 Email subjects name the pet again, the in-app titles stay: "{pet} was removed — Hapi Vet", "{pet} stays on your account — Hapi Vet" (also for the direct archive: "was removed"), "Receipt {number} for {pet} — Hapi Vet" / "Receipt {number} — Hapi Vet" (`receipt_paid_subject` in `billing/utils.py`; `_send_removal_notice` takes `subject`).
    - 4 Flake8: `send_reminders.py` (unused `import datetime`, three f-strings with no placeholders, the long line near 205 wrapped, its f-prefix dropped); `notifications/utils.py` (unused `settings` import).
    - Tests added: medical `OwnerMedicalRecordPageTests`; accounts `OwnerAccountHubTests`; pets `OwnerProfileEmailReadOnlyTests`; two subject checks added to pets `AnswerDeletionTests` (keep: "stays on your account"; archive: "was removed").
    - Tests changed (on purpose, wording only): pets `AnswerDeletionTests.test_email_sent_when_the_owner_has_one_whatever_the_preferences` and `DirectArchiveTests` (the subject check): subject "Bantay was removed — Hapi Vet" (was "Pet removed — Hapi Vet"); billing receipt-paid with a pet: subject "Receipt {number} for Bantay — Hapi Vet" (was "Bill paid: ₱1,250.00 — Hapi Vet"); without a pet: "Receipt {number} — Hapi Vet" (was "Bill paid: ₱100.00 — Hapi Vet").
  - **Slice 8a-2d resend verification and verify-email guard — 2026-11-17** (backend plus minimal markup on `owner/login.html` and `owner/register_pending.html`; no migration). `app.css` needs no rebuild (no new classes); no new static file, so no collectstatic.
    - 0 Test fix: medical `OwnerMedicalRecordPageTests._counts` read `len(page)` after the next request had cleared the query log, so it printed the context's 5. It now counts right after the page's own `with`. Real total: **11 queries for the whole page** (context 5), flat for 3 and 30 rows.
    - 1 `verification_pending(user)` in `accounts/utils.py`: pet_owner, inactive, never logged in, usable password, no archived PetOwner. (Also `has_archived_owner`, `find_pending_owner`, `is_unconfirmed_login`, `resend_allowed`.)
    - 2 `owner_resend_verification` at `/resend-verification/` (POST only, GET goes to the owner login; CSRF on; in `PUBLIC_URLS`). Sends with `_send_verification_email`, the same function and link as registration. The reply is always the same (redirect to "check your email" + the info message) for a match, no match, active, archived, Google, throttled and send error; an error is logged with the user pk and the exception class only. Throttle: one send per 2 minutes per account, `cache.add("resend-verification:<pk>", 1, 120)` (a failed send still uses the slot). The default locmem cache is per worker process, so with several gunicorn workers the limit is per worker (fine for v1; no new setting).
    - 3 "Check your email" has a POST form with a visible Email address field (the page doesn't know the address: no session key) and "Send the link again".
    - 4 Owner login: the right password on a pending owner renders the login (200, not logged in) with `unconfirmed_email` and the "Please confirm your email first" box + resend form. Every other failure keeps the generic "Invalid email or password." (the old "not verified yet" branch stays but can't be reached: both auth backends refuse inactive users).
    - 5 `owner_verify_email`: a non-owner's link is the invalid-link page; a valid link for an archived owner doesn't activate and goes to the owner login with "This account is closed. Please contact the clinic."; a normal pending owner and an already-active user behave as before.
    - Tests added: accounts `VerificationPendingTests`, `ResendVerificationTests`, `LoginNotConfirmedTests`, `VerifyEmailGuardTests`.
    - Tests changed: medical `OwnerMedicalRecordPageTests.test_query_count_is_flat` (via `_counts`): page count now read straight after the page request (was read late, 5); flatness and the context assertion (5) unchanged.
    - **Slice 8a is complete.** Next: 8b-1 (owner layout).
  - **Slice 8b-1 owner layout, components and error pages — 2026-11-17** (templates, two small JS files, two helpers; no migration, no model change). `app.css` needs a rebuild (new classes); two new static files, so collectstatic.
    - 1 `layouts/owner.html` (top bar, flash block, `<main id="main">`, bottom tabs, skip link; blocks `title`, `extra_head`, `top_bar`, `flash`, `content`, `main_padding`, `bottom_bar`, `extra_js`) with `layouts/owner_parts/tabs.html`, `tab_link.html`, `back_bar.html`. The bell count and the clinic logo come from the existing `shell` context processor (`shell.unread_count`, `shell.clinic.logo`); the processor needed no change. New icon `user` (lucide-static 1.66.0). Tab mapping (`OWNER_TABS` / `owner_tab` in `accounts/utils.py`, read by the `owner_active_tab` tag in `accounts/templatetags/ui_helpers.py`): Home = owner_dashboard, owner_dashboard_refresh; Pets = owner_pet_list, owner_pet_add, owner_pet_detail, owner_pet_edit, owner_pet_delete_request, owner_pet_cancel_delete_request, owner_pet_medical_history, owner_medical_record, owner_vaccination_history; Visits = owner_appointments, owner_book_appointment, owner_appointment_detail, owner_appointment_cancel, owner_appointment_reschedule, owner_get_slots; Bills = owner_billing_list, owner_receipt_detail; Account = owner_account, owner_profile, owner_profile_edit, owner_notification_preferences, clinic_info, owner_health_article_list, owner_health_article_detail. `owner_notifications` (the bell's page) and unknown names: no tab lit.
    - 2 `components/flash_messages.html` (admin, owner, admin login): colour + icon per level, bold first sentence for two or more (`flash_lead` filter), `role` status / alert, Close button (`data-flash-close`, x icon + sr-only "Close", 44 px) in `static/js/shared/flash.js`; no auto-hide. `layouts/auth.html` loads `flash.js` too.
    - 3 `dialogs.js` moved to `static/js/shared/dialogs.js` (same code); `layouts/admin.html` and `layouts/owner.html` load it. Unused: `static/js/admin/dialogs.js` (Krys deletes it; also `staticfiles/js/admin/dialogs.*`).
    - 4 `components/bottom_sheet.html` (native `<dialog>`, parameters: id, title, text, action_url, action_label, action_icon, keep_label).
    - 5 Account page on `layouts/owner.html`, Account tab lit, plain markup kept; Log out opens the "Log out of Hapi Tutz?" sheet whose action is the POST form to `logout`.
    - 6 `templates/404.html`, `403.html`, `500.html` (standalone, `app.css`, "Go to the home page" to "/"). No `handler40x` added: Django's defaults use these files. Note: a logged-out visitor only reaches the 404 page on public paths (the middleware redirects other missing URLs to log in first; unchanged).
    - 7 CLAUDE.md: the fast-hasher and full-suite line.
    - Tests added (accounts): `OwnerLayoutTests`, `BottomSheetTests`, `FlashComponentTests`, `DialogsMovedTests`, `ErrorPageTests`.
    - Tests changed (on purpose): dashboard `TopbarSearchTests.test_layout_loads_dialogs_js` expects `/static/js/shared/dialogs.<hash>.js` (was `js/admin/`); billing services page `test_only_the_layouts_scripts_load` expects `htmx, dialogs.js, flash.js, auto_submit.js` (was without `flash.js`); accounts `OwnerAccountHubTests.test_log_out_is_a_post_form_with_a_csrf_token` now finds the form in the sheet (button "Yes, log out"; was a bare `Log out` submit button); health `TogglePublishTests.test_apostrophe_title_works_and_is_escaped_in_the_flash` (was `assertContains` of the whole two-sentence flash; now the lead sentence inside `<strong class="font-semibold">` followed by the rest; the `assertNotContains` below it unchanged). No assertion removed or loosened.
  - **Slice 8b-2 owner auth pages — 2026-11-17** (templates, one small JS file, small view changes; no migration, no model change). `app.css` needs a rebuild (new classes); new static files `js/shared/password_toggle.js` and the edited `img/google_g.svg`, so collectstatic.
    - 1 `layouts/owner_auth.html` (band + logo tile + overlapping card; blocks `title`, `card_title`, `card_subtitle`, `content`, `below_card`); `layouts/auth.html` and the admin login untouched.
    - 2 `owner/auth_parts/`: `email_field`, `password_field` (eye inside the box), `password_rules`, `google_button` (official look, POST form), `or_line`, `alert_box`. New icon `eye-off` (lucide-static 1.66.0). `static/js/shared/password_toggle.js`.
    - 3 Pages rebuilt from `owner_auth_v1.html`: log in (red box for every generic failure, blue not-confirmed box + resend, green "You're logged out."), create account, check your email (address from session key `pending_email`, set by `owner_register`), forgot password, sent, new password ("For {email}" from the reset form's user, valid link only), done. `owner_login` is now under 50 lines; the unreachable "not verified yet" branch is removed. "Keep me logged in" (`remember`, starts unticked, as in the mockup): unticked sets `set_expiry(0)`, ticked sets 30 days (`REMEMBER_ME_SECONDS`, the old `remember_me` value; `SESSION_COOKIE_AGE` is unset, so Django's 2 weeks applies to Google sign-in).
    - 3b The `shell` context processor now gives anonymous requests `auth_shell` (a lazy `.clinic` from `get_clinic_settings()`, one query, never creates a row), so the sign-in band shows the clinic logo (paw tile when none). Anonymous requests still get no `shell` key. `owner/verify_email_invalid.html` moved onto `owner_auth.html` (red box, "Register again", "Back to log in"); `static/css/owner/auth.css`, `images/clinic/auth-bg.png`, `static/css/owner/google-button.css` and `static/js/owner/auth.js` are now unused (Krys deletes). Tests added: `InvalidVerificationLinkPageTests`, `AuthLogoTests`.
    - 4 allauth overrides in `templates/socialaccount/`: `login.html`, `authentication_error.html`, `login_cancelled.html`.
    - 5 `user_logout` message is now "You're logged out." `google_g.svg` keeps only the coloured G (viewBox `10 10 20 20`).
    - Tests added (accounts): `OwnerAuthLayoutTests`, `LoginPageStatesTests`, `KeepMeLoggedInTests`, `PasswordFieldTests`, `GoogleButtonTests`, `RegisterPageTests`, `CheckEmailPageTests`, `ResetPagesTests`, `SocialaccountTemplateTests`.
    - Tests changed (on purpose): `LogoutPostTests` message "You're logged out." (was "You have been logged out."); `GENERIC_LOGIN_ERROR` is "That email and password don't match" (was "Invalid email or password."); `ResendVerificationTests.test_works_logged_out_and_the_reply_is_shown_on_the_page` and `VerifyEmailGuardTests.test_an_archived_owners_link_does_not_activate` now expect the flash's bold first sentence followed by the rest (the pages used to print the message plainly). No assertion removed or loosened.
  - **Slice 8b-3 owner onboarding and Add pet — 2026-11-17** (templates, shared JS, forms, two owner HTMX endpoints, one new step view; no migration, no model change). `app.css` needs a rebuild (new classes); new static files `js/shared/photo_input.js`, `pet_form.js`, `address_form.js`, so collectstatic.
    - 1 `AddressFieldsMixin` in `pets/forms.py` (address_province / address_town / address_barangay, their choices, clean() rules, initial-mode logic, `address_hand_typed`; `address_required = False`, `address_barangays_url`). `AdminPetOwnerForm` uses it unchanged; the new `OwnerOnboardingForm` (step 1 only, on top of `PetOwnerProfileForm`) sets `address_required = True`: "Choose your town." / "Choose your barangay." (Marinduque), "Type your province." / "Type your town." / "Type your barangay." (other); street optional; PSA spelling saved. The profile edit page still uses `PetOwnerProfileForm` (no address rule there yet). `ErrorCountMixin` holds `error_count`.
    - 2 `owner_address_barangays` (`/owner/address/barangays/`) and `owner_pet_breed_options` (`/owner/pets/breed-options/`): login + role pet_owner, no `owner_profile_required` (step 1 runs before a PetOwner row exists); they render the admin partials; `town_from_params` / `species_from_params` in `pets/utils.py` are used by the admin and owner views alike. The clinic gets the middleware's usual refusal.
    - 3 Shared JS in `static/js/shared/`: `photo_input.js`, `pet_form.js`, `address_form.js` (same code as before). Admin owner form loads `photo_input` + `address_form`; admin pet form `photo_input` + `pet_form`; the admin record page `photo_input`. Unused now (Krys deletes): `static/js/admin/photo_input.js`, `pet_form.js`, `owner_form.js` (it held only address code) and their `staticfiles/` copies.
    - 4 `OwnerPetForm` (`SpeciesBreedFieldsMixin` + `PetBasicsCleanMixin`, shared with `AdminPetForm`, whose behaviour is unchanged). Final fields: species_choice, species (typed), breed_choice, breed (typed), name (required), gender (Sex, "Not sure" default), date_of_birth (optional, "A rough date is fine."), color, weight, registration_number (optional), photo. The owner is never a field: the view sets it. The old `PetForm` is unused (kept).
    - 5 Onboarding on `layouts/owner.html`: `top_bar` = logo + "Hapi Tutz" + "Step N of 2" with a progressbar (no bell), `bottom_bar` = flow buttons (no tabs). Step 1 "About you" (photo, name, mobile, address card, links), step 2 "Your first pet" (Add pet primary; Back and Skip for now outline, same size; skip finishes onboarding as before), step 3 "Pet added" (`owner_onboarding_step3`, new: the pet just added from session key `onboarding_pet_id`, "Add another pet", one primary "I'm done, go to my dashboard"). A saved pet now goes to step 3 (was: dashboard with a flash). Step 2 now has a base layout. Re-entering step 1 still binds the saved PetOwner.
    - 6 Add pet page on `layouts/owner.html`, Pets tab lit, back bar "Back to My Pets" or "Back to booking" (`?next=book`, behaviour from 8a-2a unchanged), same form parts (`templates/owner/pet_form_parts/`), one primary "Add pet". `form_field.html` got `large=True` (help and errors in text-base).
    - 7 `static/js/shared/pet_species_breed.js` is unused now (only `SpeciesBreedListTests` reads it); not deleted, decide in 8b-10.
    - Tests added (pets): `AddressMixinTests`, `OwnerAddressEndpointTests`, `OwnerBreedEndpointTests`, `OwnerPetFormTests`, `OnboardingPagesTests`, `OwnerAddPetPageTests`.
    - Tests changed (on purpose): script paths `js/admin/` to `js/shared/` in pets `OwnerFormPageTests.test_new_layout` (`address_form`), `PetFormPageTests.test_new_layout` and `test_owner_form_loads_the_shared_photo_script`, medical record page `photo_input` check; pets `AddPetNextBookTests.POST` posts `species_choice` (was `species`); pets `OnboardingStepOneReentryTests._post` also posts the address (now required on step 1). No assertion removed or loosened.
- Slice 9 admin mobile pass (incl. dashboard schedule rows keep a 3-column grid that is tight at phone width), record detail tab order on phones, walk-in type-ahead live region, admin layout inline script → static JS file, `form_field.html` fieldset/legend branch
- Open: `form_field.html` needs a fieldset/legend branch before a RadioSelect or CheckboxSelectMultiple form is migrated.
- Notes for later slices:
  - Slice 6 (billing): done (see "Slice 6 complete"). The habits it settled (no `{% url %}` in handlers, `for=` on every label, typed values kept, no inline handlers) apply to the next admin slices too.
  - Owner side: `owner/pets/edit.html` extends no base template (no Tailwind/HTMX/frame). Real bug, fixed in 8b-5. (`owner/onboarding/step2.html` was the same; fixed in 8b-3.)
  - Clinic info: admins get `admin/clinic_info.html` (admin layout); owners get `shared/clinic_info.html` on `base_dashboard.html`.
- Every slice: rebuild `app.css` before committing; test at 150% zoom; re-run collectstatic locally once a template references `app.css` or vendored HTMX (tests use the manifest).

### SMALL FIXES (no schema change), one commit each
- **SECURITY** — `accounts/management/commands/create_admin.py` has a hardcoded password committed to a public repo. Fix: prompt for the password with `getpass` instead of a literal. Must be fixed before production, and any admin created with this command must get a new password. [backend]
- Normalize (lowercase) emails on save (walk-in, add-email, owner edit) — Done 2026-11-01 (3c-1)
- Email data check (read-only, local and live): owner and admin login lowercase the typed email and look it up with an exact match (`User.objects.get(email=email)`), but walk-in, add-email and owner-edit saves did not lowercase before 3c-1, so existing rows may hold mixed-case emails that can't log in with a password (Google sign-in uses `iexact` and still works). Check with `User.objects.annotate(e=Lower("email")).exclude(email=F("e"))` and for case-only duplicates; plan a fix (data migration or `iexact` login lookups) only if rows turn up [backend]
- Resend claim email (resend button on the owner detail page) — Done 2026-11-02 (4a-1, 4a-3)
- Claim email failure in `pets/views.py` no longer a 500 and keeps the saved email — Done 2026-11-02 (4a-1)
- `admin_owner_add_email` `@login_required` — Done 2026-11-02 (4a-1)
- Owner same-day cutoff bug (`_slot_open_today`; `get_available_dates` deleted) — Done 2026-11-02
- Flake8 cleanup [backend] — backend/.flake8 added in the docs commit; current count: 52 (+1: E501 on the correction_reason placeholder in medical/forms.py, left on purpose in 5a-1).
- Re-save requirements.txt as UTF-8 [backend] — verified: currently UTF-16 LE with CRLF
- Logout always redirects to owner_login, even for admins — Done 2026-11-01
- Admin sidebar has no logout link [frontend] → UI overhaul Slice 1 — Done 2026-11-01
- Logout accepts GET; convert to POST form (low priority) [both] — Done 2026-11-17 (8a-1b; the log-out sheet itself comes with 8b)
- Pricing on booking page: bring over commit 8b45d23 from backend-dev (no migration) [both]
- UTC dates: every `timezone.now().date()` in `backend/` is now `timezone.localdate()` — Done 2026-11-02 [backend]
  - `BillingReceipt.generate_receipt_number` uses `timezone.localdate().year` — Done 2026-11-02
  - Left: `BillingReceipt.billing_date` `default=timezone.now` stays (changing the default would need a migration; DateField already stores the Manila date from an aware datetime) [backend]
- ClinicSettings is fetched two ways — Done in Slice 7 (one helper, get_clinic_settings())
- Admin reschedule (Move dialog) has its own slot rule (`move=True`) — Done 2026-11-02
- Reschedule notification uses type `appointment_approved` (follows the owner's status-email preference) [backend]
- `notification_mark_read` view is unused (dead code) [backend]
- Notification messages store raw dates ('November 09, 2026 at 03:00 PM'), so the Notifications panel shows them; format them on display or when created [backend] — Done 2026-11-17 (medical views 2026-11-03, 5a-5; the owner notifications in `appointments/views.py` (approve, reject, reschedule, cancel) and `send_reminders.py` in 8a-2b: compact messages and friendly dates in the email). Left on purpose: the three admin-facing texts (owner cancels, new request, reschedule request in `appointments/views.py` and `appointments/utils.py`) still say 'November 09, 2026 at 03:00 PM'; admin notifications were not changed in 8a-2b
- `vaccinations_due` counts corrected vaccinations; `followups_due` includes archived pets — Done 2026-11-02 (dashboard v5 D1)
- Delete the unused admin calendar files: `admin/appointments/_calendar_grid.html`, `_calendar_day_panel.html`, their views `admin_appointment_calendar_grid` / `admin_appointment_calendar_day` and URLs (no template or JS uses them; found in the dashboard v5 audit) [both]
- DATA_MODELS and HANDOFF §6 `is_corrected` is on Vaccination only — Done 2026-11-03 (5a-1)
- owner_login and admin_login ignore ?next=, so after logging in users always land on their dashboard instead of the page they were on [backend]
- `base.html`'s "commented-out" messages loop still runs (Django tags run inside HTML comments), silently using up messages on owner pages that have no loop of their own. Fix on the owner side: delete the block, don't un-comment it. [frontend]
- Admin password reset: the reset flow is owner-branded and ends on the owner login; build an admin version if needed. [both]
- Admin login has no rate limiting or lockout. [backend]
- Password reset email hard-codes `http://` (ignores the protocol). [backend]
- Pet-mismatch guard: `admin_medical_record_create` — Done 2026-11-03 (5a-2); `admin_receipt_create` — Done 2026-11-07 (6a-2)
- DATA_MODELS says the admin gets an EMAIL_FAILED notification when an email fails; nothing creates it and `related_notification` is never set. Decide: build it or fix the doc. [backend]
- Read-only count on live of vaccinations wrongly flagged Corrected by the old edit bug (before 5a-1 a blank weight or next date counted as a clinical change, so saving without changes set `is_corrected`). Count only; any data fix needs its own plan (from `medical_v1_decisions.md`) [backend]
- Walk-in same-day cutoff bug — Done 2026-11-01 (3c-1, `walk_in=True`)

### FRONTEND BRANCH REVIEW
- B3: review the `frontend/hannah-user` branch (owner templates) and decide, piece by piece, what to bring in and what to discard. It is no longer under active development. Check form field names and template tags against the current views; base_dashboard.html will conflict. [frontend]

### FEATURE
- Admin calendar page Phase 1 (reuse dashboard grid + day panel on its own page, add status filter). Not started. Start on a fresh branch cut from trial-run. Phases 2 (day timeline) and pet owner calendar are not in FEATURES.md yet — add them there before building. [both]
- Admin Google account linking (FEATURES 1.3), planned — build properly after the Google sign-in hardening: admin links Google from their own settings while logged in. [both]

### SCHEMA CHANGES (each needs a written migration plan first; merging into rebuild migrates the live DB)
- Double-booking: form-only check, no DB constraint or locking — decide constraint vs transaction/row lock [backend] — verified: no `UniqueConstraint`, `unique_together`, `select_for_update` or `atomic` in `appointments/`
- PetDeletionRequest: cancelling deletes the row — Done 2026-11-17 (choice added in 8a-0, used since 8a-1b)
- Upload filename sanitization (Supabase rejects non-ASCII names); callable upload_to = new migration [backend] — verified: all `upload_to` values are plain strings
- Walk-ins without email are saved with `email=""` (`accounts/utils.py` `create_walkin_owner`), which collides on the unique email constraint for the second one. Since 3c-1 the admin sees a plain message instead of an error. Decided: nullable `User.email`, as its own migration batch after 3c — write the migration plan first (existing `""` rows → NULL; every `email=` lookup and `if user.email` check) [backend]
- Distinct "declined" cancellation reason — Done 2026-11-17 (choice added in 8a-0, used since 8a-1b)
- Notes "last saved" timestamp: the detail page can't show when the internal notes were last saved (`updated_at` changes on every save). Needs a new field; low priority [backend]
- Link a reminder Notification to its Vaccination / MedicalRecord (the 'last reminder' line is per pet and kind until then; 5a-5 `last_reminders`) [backend]
- Record-created notification type: the medical record created notification is kept (Slice 5) but has no type of its own; a new `Notification` type is a choices change = new migration (from `medical_v1_decisions.md`) [backend]
- Resend failed emails: review b6e0ad3's logic (written for Gmail SMTP before the Brevo switch in 8548613; must catch anymail errors). Model/migration already synced. [both]

### POLISH
- Record detail: on phones the tab order differs from the visual order (order-* classes). [frontend]
- Bell & header (context processor + admin notification panel; unused header CSS/JS deleted) [both] → UI overhaul Slice 1 — Done 2026-11-01
- Multi-line `<a>` tags [frontend] — 2026-10-31 audit: 53 in admin/shared templates across 20 files, plus 2 broken tags in `admin/pets/deletion_requests.html` (fixed 2026-11-03, Slice 4b-2). Fixed slice by slice as files are touched.
- Views over 50 lines (admin_walkin_create: under 50 since 3c-2, save logic in `_save_walkin_owner`) [backend]
- Mobile responsiveness [frontend] → UI overhaul: owner side Slice 8, admin Slice 9
- Walk-in step 1 type-ahead: the result count may not be announced by screen readers because the live region is swapped in; fix with a persistent status line outside #owner-results updated by an hx-swap-oob swap.
- auto_submit.js (since list v5 used only by the appointments list's phone Status dropdown): in Chrome on Windows, arrow keys on a closed <select> fire change, so keyboard users submit on the first arrow press. Acceptable for now (mouse users); revisit if keyboard use matters.

### HOUSEKEEPING
- Keep the species/breed lists in pets/utils.py and pet_species_breed.js in sync (`SpeciesBreedListTests` fails when they differ). Since 8b-3 no page loads the JS file; decide in 8b-10 whether to drop it and that test.
- Refresh `backend/pets/data/psgc_marinduque.json` when the PSA publishes a new PSGC release (check yearly); pick rows by Geographic Level and the Marinduque code `1704000000`, never by typed names
- Consider moving the repo out of OneDrive
- Duplicate remote branches review / review-admin / trial-front; old branch feature/admin-calendar-page has no unique work — delete later — verified: `git log origin/trial-run..feature/admin-calendar-page` is empty
- Local master is behind origin/master — verified: 7 commits behind
- Hosting: Render free tier has uptime problems (cause unconfirmed). Candidates: Railway (Hobby, Asia region), Fly.io, VPS with Coolify/Appliku. On any host change update ALLOWED_HOSTS, CSRF_TRUSTED_ORIGINS, Google OAuth redirect URIs, the Django sites domain and all env vars.
- Production DB: plan is a NEW Supabase project (Singapore) for the clinic handover, not wiping the current one; the current project becomes staging. At cutover: update DATABASE_URL, AWS_*, MEDIA_URL and AWS_S3_CUSTOM_DOMAIN on Render, migrate, and create the admin with the clinic's real email.
- Test result files are public URLs (S3 `public-read`, no signed URLs); archived files keep their link. Decide before the clinic goes live (a storage change affects every upload) (from `medical_v1_decisions.md`)
- Before go-live: real Terms and Privacy pages; replace settings.TERMS_URL / PRIVACY_URL (decision 4).
- django-tailwind: no longer planned; replaced by the Tailwind standalone CLI (CONVENTIONS §11).
- Project documentation report: needs screenshots, ERD (Mermaid ERD already generated) and source artifacts. Deadline: ___
- Add .gitattributes (* text=auto) to stop LF/CRLF warnings — deferred until after B1 so line-ending churn doesn't mix with a deploy.
- error_summary.html lives in pets/owner_form_parts/ but is shared by the medical forms; move it to components/. (5b-4)

### DECISIONS PENDING
- PrescriptionItem delete path — Done 2026-11-03 (decided and built in 5a-2: removable only on the day the record was written)
- HealthArticle hard delete — Done 2026-11-16 (decided: unpublish only; built in 7a-3)
- Double-booking approach (see above). Interim lock built (8a-1b): owner booking, owner reschedule, walk-in and admin move lock the ClinicSettings row; a DB constraint is still open.
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
Medical records and vaccinations are never deleted. Medical records are
edited in place; only Vaccination has `is_corrected` + `correction_note`
(a clinical change needs a reason, added as a dated line; old lines are
never replaced). Private notes are never visible to
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
- Styling: every admin page extends `layouts/admin.html` and uses the
  compiled `static/css/app.css` (Tailwind v4, CONVENTIONS §11); redesigned
  ones use `templates/components/`, the others (e.g. receipt detail)
  import their old `static/css/admin/` file into the `legacy` layer via
  `legacy_css`. Owner pages still use `base_dashboard.html` (only the Account
  page is on `layouts/owner.html` since 8b-1) with the
  Tailwind CDN plus per-page CSS files in `static/css/owner|shared/`. Rebuild `app.css` before every
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
