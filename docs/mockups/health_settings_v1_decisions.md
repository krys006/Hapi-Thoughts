# Health library and clinic settings (Slice 7): decisions (approved 2026-11-16)

Source: the read-only Slice 7 audit (2026-11-16, Read/Grep/Glob only) and Krys's answers (all recommendations accepted; button label "View public clinic page"; layout option B; the five mockup points approved).
Repo location: `docs/mockups/health_settings_v1_decisions.md` (same folder as `billing_v1_decisions.md`).
Mockup: `docs/mockups/health_settings_v1.html` (10 states, approved 2026-11-16). The layout options page (A–D) stays an artifact only; B was chosen.
No model field change and no migration in Slice 7. Anything that needs a migration goes to the backlog.

## Rules (backend)

1. **Health article delete is removed; unpublish only.** Matches core rule 8 (archiving is a soft delete; data is never permanently deleted). The delete view, its URL and its button go. `DATA_MODELS.md` (deletion table) and `FEATURES.md` 7.5 are updated to match. No migration.
2. **Publish / Unpublish only from the list, each with a confirm dialog** (native `<dialog>`, `dialogs.js`; no `openConfirmModal`). The article form has **no Published checkbox**: `is_published` leaves `HealthArticleForm` (like `payment_status` left the receipt form). The form shows the status as a gray line. A **new article is saved as a draft**; the list flashes "Saved as a draft. Publish it when it's ready." Dialogs: "Unpublish “{title}”?" / "Pet owners will no longer see this article. It stays in the list, and you can publish it again at any time." with "Keep it published" (focused) + red "Yes, unpublish"; "Publish “{title}”?" / "Pet owners will be able to read it." with "Go back" + violet "Yes, publish". The toggle view is POST only.
3. **Blocked dates: hard delete kept.** A blocked date is a schedule setting; no visit or record depends on it. A deliberate exception to the wording of rule 8 (note it in DATA_MODELS.md under BlockedDate).
4. **One helper for ClinicSettings: `get_clinic_settings()`** (appointments/utils.py). It uses `.first()` and never looks up `pk=1`. It creates the default row only when the clinic settings page is opened and no row exists. Every other caller uses the helper (no `get_or_create(pk=1)` anywhere).
5. **Blocked date on a busy day: warn, then confirm.** The first submit lists the CONFIRMED and PENDING visits on that day (time, pet, owner) and asks for a second click ("Go back" / violet "Block anyway"). The second click saves the block. Existing visits are not changed or cancelled.
6. **Changing hours, working days, slot length or the cutoff with visits booked: warn on save.** The first save lists the affected upcoming visits and saves nothing. Buttons: "Go back" (outline, focused; saves nothing, keeps what was typed) and violet "Yes, save the new schedule"; the normal Save changes is hidden while the warning shows. Visits are never cancelled or moved. The overlap fix (booking check compares exact start times only) is a logic change → backlog.
7. **Settings tabs are server-rendered** (`?tab=` links). A failed save reopens the tab that has the error, with a red "1 problem" pill on it and the error summary box. The JS tab builder (`clinic_settings.js`) is removed.
8. **Owner health pages are not in Slice 7.** They move to Slice 8 (they share the owner frame).
9. **`booking_limit_days`: 1 to 365** (form validator; no migration). Help: "Owners can book up to this many days ahead. Between 1 and 365." Errors: "The booking limit must be at least 1 day." / "The booking limit can't be more than 365 days."
10. **Clinic info button label: "View public clinic page"** (as now), in the clinic settings banner.
11. **Logo: JPG, PNG or WebP (any letter case), up to 2 MB** (form check; no migration; SVG refused). Help: "JPG, PNG or WebP, up to 2 MB. It shows on receipts and the clinic page. Choosing a new file replaces the old one." Errors: "This file is 4.3 MB. Please choose one up to 2 MB." (size rounded up to one decimal, the test-file rule's format) / "Only JPG, PNG or WebP files can be used."
12. **Clinic settings layout: option B (clinic banner + icon tabs).** A violet banner (`bg-gradient-to-r from-violet-600 to-violet-500`) shows the logo, clinic name, address, opening hours and phone from the **saved** settings (never the unsaved form), with a white "View public clinic page" button, so Save changes stays the one violet button. Under it, four icon tabs as `?tab=` links, in this order: **Clinic details (house) · Schedule (clock) · Blocked dates (ban) · Vet profile (users)**; the open one `bg-violet-50 ring-2 ring-violet-600` + `aria-current="page"`. Each section starts with an icon tile and a one-line explanation. The banner's hours line ("Mon to Sat, 8:00 AM to 5:00 PM") comes from a server-side helper. Icons: check `components/icon.html` first; copy any missing one (`clock`, `house`, `phone`, `ban`) from lucide-static@1.66.0. Blocked dates is its own tab because it has its own Add and Remove; its violet button is "Block this date". Phone: the tabs become a 2×2 grid and the banner stacks.
13. **Schedule warning lists every upcoming visit** (approved 2026-11-16, 7a-5). When hours, working days, slot length or the same-day cutoff change, the warning lists all CONFIRMED and PENDING visits from today on (not only ones that no longer fit), ordered by date and time, first 10 then "and N more". The admin checks they still fit. A `booking_limit_days` change alone never warns.
14. **Each tab saves only its own fields** (approved 2026-11-16, 7a-5). The POST carries `tab` (details, schedule or vet); the view validates and saves only that tab's fields and redirects to `?tab=<that tab>`. A POST without `tab` (the old page) saves every field, until 7b-2 replaces the page.

## Bugs fixed in Slice 7
- Health delete button broke on apostrophes (`article_list.html:63`, no `escapejs`). Gone with decision 1 (no delete, no inline handler).
- Settings tabs hid errors on tabs other than the first (decision 7).
- Blocked-date form error swapped the whole page into the list (`views.py:122-134`); the success flash showed on the next full page. Fixed in 7a-4: errors return the panel only; the flash goes on a full-page response only.

## Batches
- **7a-1** `get_clinic_settings()` helper + the clinic settings page (create-default only on that page). Tests.
- **7a-2** Other ClinicSettings callers switched to the helper (about 9 files). Tests. No behaviour change.
- **7a-3** Health views: delete view + URL removed, toggle POST only, `is_published` out of the form, new articles saved as drafts, flash texts, stale docstring. Health tests (none exist today).
- **7a-4** Blocked dates: busy-day warning with second click; panel-only error response; flash fix; `booking_limit_days` 1–365. Tests.
- **7a-5** Schedule save warning (second click); `?tab=` and error-tab reopen in the view; the banner hours helper; saving per tab. Tests.
- **7a-6** Logo check (decision 11). Tests.
- **7b-1** Health list and form (states 1–3): dialogs, no `onclick`; delete `article_list.js`, `article_list.css`, `article_form.css` once nothing loads them.
- **7b-2** Clinic settings page, option B (states 4–7); delete `clinic_settings.js` and `clinic_settings.css`; one form per tab posting `tab`.
- **7b-3** Blocked-dates tab (states 8–9): warning step, panel-only error response.
- **7b-4** Clinic info page (state 10): frame by role unchanged; legacy CSS removed from `clinic_info.html` for the admin frame (owners keep `shared/clinic_info.css`).
- **7b-5** Retire the legacy layer: `legacy` out of `assets/tailwind/input.css`, the `legacy_css` block out of `layouts/admin.html`, the admin include of `shared/modals.html` (owner pages keep it), rebuild `app.css`; update CONVENTIONS §10 example, HANDOFF, DATA_MODELS and FEATURES. Widen the health 'no onclick' test back to the whole page.

## Checks before the batches
- `shared/sidebar_admin.html`: still loaded from `base_dashboard.html:117`, with "Clinic Info" and Health Library links. Confirm it is dead for admins, then delete it (the sidebar test expects 10 links).
- `owner/appointments/book.html` around line 223: confirm the ClinicSettings read is inside `{% if clinic %}`.
- `components/flash_messages.html`: confirm titles are not marked safe.

## Backlog (not this slice)
- Overlap check on booking (exact start time today; a slot-length change can allow overlaps).
- Owner health pages (Slice 8).
- Test result files: public S3 URLs (before go-live).
- `create_admin.py` password literal (it was printed in the audit's tool output): switch to `getpass` and re-password any admin made with it before the deploy.
