# Admin medical pages (Slice 5): decisions (approved 2026-11-03)

Mockups (all approved 2026-11-03), in `docs/mockups/`:
`medical_lists_v1.html`, `record_detail_v1.html`, `record_form_v1.html`,
`vaccination_form_v1.html`, `pet_health_history_v1.html`.
Source: the read-only Slice 5 audit (2026-11-03) and Krys's answers to its 13 questions.

## Rules (backend)
- **Correct view**: removed (`admin_vaccination_correct`, its URL and `VaccinationCorrectionForm`). No page linked to it. Correcting is part of Edit.
- **Vaccination edit**:
  - Clinical fields: vaccine_name, custom_vaccine_name, date_administered, next_due_date, weight_at_vaccination, site_of_injection. Label fields (batch_number, manufacturer, administered_by) change freely.
  - A change is worked out on the server: empty and None count as the same value, so a blank weight or next date is not a change.
  - A clinical change needs a new reason (form-only field, never pre-filled). The reason is added to `correction_note` as a new dated line ("Tue, Nov 3, 2026: …"). Earlier lines are never replaced or cleared. `is_corrected` becomes True and stays True.
  - The error names what changed and its old value ("You changed the date given (was Tue, Oct 6). Write why, so the change is on record.").
- **Vaccine name**: a listed vaccine clears `custom_vaccine_name` on save; Other needs a typed name.
- **Dates**: a record date and a date given can't be in the future (Manila date); the next dose must be after the date given; the follow-up date must be after the record date; "Needs a follow-up visit" needs a date; a follow-up date with the box unticked is cleared on save.
- **Record from a visit** (`?appointment=`): the visit must belong to the pet in the URL and be CONFIRMED or COMPLETED; a bad id is a 404, never a 500; a visit that already has a record goes to that record (also when two saves race).
- **Medicines**: can be removed only on the day the record was written (Manila date of `created_at`), with a confirm dialog. After that the list is kept as it is. (Answers HANDOFF "Decisions pending".)
- **Test files**: PDF, JPG or PNG, up to 10 MB. Remove = archive (soft delete); archiving twice changes nothing. The admin sees "Removed files (N)"; owners never see archived files.
- **Archived pet or archived owner**: adding medicines, files, vaccinations and sending reminders is refused (GET and POST); editing records and vaccinations stays allowed (corrections).
- **Reminders**: confirm dialog; refused for archived pets/owners; a vaccination reminder only for a dose with a next due date and no later dose of the same vaccine; the flash says honestly whether it went by app only or app and email; the "last reminder" line is per vaccination / per record.
- **Due rule (one shared helper)**: due = next due date from today to today + 7; overdue = next due date before today; a dose with a later dose of the same vaccine (same vaccine_name + custom_vaccine_name, later date_administered) is left out; corrected doses count; pets that are archived or whose owner is archived are left out. Used by the dashboard card, the Vaccinations list and the pet page. Follow-ups: follow_up_required, date today to today + 7, same archived rule. ⚠️ Changes the pinned dashboard assertion "corrected not counted".
- **Record-created notification**: kept, with a friendly date. Its own notification type is a later schema item.
- **Owner side** (small fixes): the owner history hides archived files and shows a follow-up only when it is required.

## Pages
- **Vaccinations list**: tabs Due in 7 days (default; yellow count) · Overdue (red count) · All. Top-bar search (pet, owner, vaccine incl. typed names), stays inside the tab. Split rows: pet + vaccine, given / next due, pills, owner with phone, Send reminder (Due and Overdue tabs only), Open (→ pet vaccinations with that dose highlighted). Due soonest first; Overdue most recently due first; All newest given first, 25 per page. No violet button. Dashboard card links to the Due tab.
- **Medical records list**: tabs Follow-ups in 7 days · All (default). Top-bar search (pet, owner, diagnosis). Rows: visit date, pet, diagnosis on one line or "No diagnosis written", Follow-up pill, owner, Open; Send reminder on the Follow-ups tab. Never private notes. Newest first, 25 per page. Dashboard card links to the Follow-ups tab.
- **Record detail**: pet page grid. Left: Visit notes with a blue "Note for the owner" box; amber Private notes card (lock, "never shown to {owner}"); Medicines (Remove only on the same day); Test results with "Removed files (N)"; Vaccinations at this visit. Right (sticky): Actions (Edit record = the one violet button; Add medicine dialog, Add file dialog, Add vaccination → vaccination form page with the record), Follow-up card (yellow when set, Send reminder, last reminder), Owner, written / last changed. Archived: only Edit record.
- **Record form** (new + edit): pet form layout. Pet + visit line + Visit date (only required field) · Visit notes · blue Note for the owner · amber Private notes · Follow-up (checkbox shows the date) · Cancel + Save record / Save changes.
- **Vaccination form** (new from pet page, new on a record, edit): cards "The vaccination" (clinical) and "From the vial label"; vaccine select with "Other (type it)" (species pattern); Given by pre-filled from `ClinicSettings.veterinarian_name`; on edit a "Reason for a correction" card with "Corrected before" lines.
- **Pet medical history / pet vaccinations**: shared header (back to pet, avatar, the one violet Add button, tabs Medical records (N) / Vaccinations (N)); sidebar highlights Medical records / Vaccinations. History: follow-up banner, record cards. Vaccinations: dose cards, correction reasons as text, Send reminder only on due/overdue doses with no later dose, Edit on every dose, the opened dose highlighted ("The one you opened").
- **New icons**: lock, eye, pill (copy from lucide-static@1.66.0).

## Batches
- 5a-1 vaccination rules (edit form, reasons, vaccine name, dates, Correct view removal)
- 5a-2 record, medicine and file rules (record dates, follow-up clearing, visit checks, guards, same-day medicine removal, file types/size, archive once, owner fixes)
- 5a-3 shared due rules (dashboard, pet page, lists, reminder rule)
- 5a-4 list contexts (tabs, search, paging, dashboard links)
- 5a-5 detail, form and reminder backend (dialog errors, vaccination form with a record, given-by pre-fill, highlight, honest flash, friendly dates, views under 50 lines)
- 5b-1 lists · 5b-2 record detail · 5b-3 record form · 5b-4 vaccination form · 5b-5 pet history pages

## Backlog (not this slice)
- Test files are public URLs (S3 `public-read`, no signed URLs); archived files keep their link. Decide before the clinic goes live (storage change affects every upload).
- Read-only count on live of vaccinations wrongly flagged Corrected by the old edit bug.
- Record-created notification type (choices migration).
- Upload filename cleaning (callable `upload_to`, migration).
