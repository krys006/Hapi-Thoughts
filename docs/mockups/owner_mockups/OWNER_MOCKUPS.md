# Pet owner mockups — index

Approved designs for the pet owner side (mobile first), settled with Krys on
2026-11-02. Open a file in a browser to see it; each file shows several phone
frames (390 × 844) with a caption above each frame.

The mockups are the reference for **look and layout**. The md files
(DATA_MODELS, FEATURES, CONVENTIONS) still win on fields, rules and behaviour.
The JavaScript in the mockups is demo only (switching tabs, filters); don't
copy it into templates.

Decisions after the audit: owner_v1_decisions.md (wins over the mockups).

---

## Shared design rules (apply to every owner page)

- **Light and flat.** Page `bg-gray-50`, white cards with `border-gray-200`,
  dark text. No gradients, glass or white-on-colour text. (The only gradients
  in the files are stand-ins for real pet photos.)
- **Font:** system stack, same as admin (`"Segoe UI", system-ui, sans-serif`).
  No Poppins and no font `@import`.
- **Sizes:** `text-base` minimum, 44px+ tap targets, icons always with words.
- **One violet button per page** (`bg-violet-600`). Selected chips and tabs use
  light violet (`bg-violet-100 text-violet-800`), never a second solid button.
- **Status colours = admin's:**

  | Status | Owner wording | Colour |
  |---|---|---|
  | pending | Waiting for approval | yellow |
  | confirmed | Confirmed | blue |
  | completed / paid | Completed / Paid | green |
  | cancelled | Cancelled | red |
  | no_show | Missed | gray |
  | unpaid bill | Unpaid | yellow |

- **Icons:** Lucide shapes, from `components/icon.html` (copy new ones from
  `lucide-static@1.66.0`, never from memory).
- **Pet picture:** the photo when uploaded, otherwise the species icon on a
  light violet tile (same rule as admin `pet_avatar.html`). Owner initials stay
  in a circle.
- **Confirmations:** cancelling, removing a pet and logging out use a bottom
  sheet, with the "keep" button as big as the action button.

## App shell

- **Top bar (inner pages):** white, logo + "Hapi Tutz" on the left, bell with an
  unread badge (caps at 9+) on the right.
- **Detail pages:** the top bar becomes "← Back to …".
- **Bottom tabs on every logged-in page:** Home, Pets, Visits, Bills, Account.
  They stay pinned to the bottom even on short pages.
- **No hamburger drawer.** Clinic Info, Health Library and settings live under
  the Account tab.
- **Flows** (booking, onboarding, change time) replace the tabs with Back / Next
  at the bottom and show a step progress bar.
- **Dashboard only:** violet band (`bg-violet-700`) with the paw trail at 10%
  opacity. The bar is see-through over the band and turns **white** once the
  band has scrolled away (option B).
- **Flash messages:** under the top bar, stay until closed, colour + icon +
  words, first words bold.

---

## Files

| File | Covers | Key decisions |
|---|---|---|
| `owner_pages_v1.html` | **Dashboard** (frames 1–2: normal, new owner). Frames 3–5 (My Pets) are superseded by `owner_my_pets_v2.html`. | Next visit card first, one "Book an appointment" button, upcoming visits, unpaid-bill box. New owner sees a welcome card with "Add your first pet". |
| `owner_my_pets_v2.html` | My Pets list, no results, one pet, no pets | White bar only (no band). Search by name + type chips (All / Dogs / Cats / Others), shown from 4 pets up. Two-column photo grid. "Add a pet" top right. |
| `owner_pet_profile_v1.html` | Pet profile: About, Vaccines, Records tabs; remove-pet sheet; removal pending | "Book a visit for {pet}" as the one button. Vaccine due-soon note; corrections shown as "Corrected by the clinic". Remove is a quiet link at the bottom; 4 deletion reasons. |
| `owner_edit_pet_shared_v1.html` | Edit pet; flash messages; 404 / 500 / 403 | Owners edit name, breed, color, weight, photo. Species, sex, birthday, registration no. shown locked. Error pages in plain words with one way home. |
| `owner_medical_record_v1.html` | One medical record: full, simple, follow-up booked | Follow-up box first, medicines get the most space (how much / how often / for how long), public notes only, empty sections left out, Print. |
| `owner_visits_v1.html` | Visits list: Upcoming / Past, pet chips, empty states | Grouped by month, today highlighted, each card explains pending / moved / cancelled / missed. |
| `owner_visit_detail_v1.html` | One visit in every status; cancel sheet; change day or time | Coloured banner says what happens next. The late (< 24 h) warning appears **only** inside Cancel and Change time, never on the detail page. Owner reschedule goes back to Waiting for approval. |
| `owner_book_visit_v1.html` | Booking: pet and reason → date and time → check and send → request sent; "time was just taken" | Mirrors admin walk-in steps. "Not sure yet" service option. Calendar shows open / full / closed days. Says clearly it's a request. |
| `owner_book_no_pet_v1.html` | Booking when the owner has no pet | Sends them to the normal Add a pet form in booking mode, then back to step 1 with the new pet picked. |
| `owner_bills_v1.html` | Bills list (search + period filter), all paid, no bills; receipt: unpaid / paid / cancelled (the cancelled receipt frame is dropped, decision 1) | "To pay" box first; filter only affects History, never hides unpaid bills. Read-only receipts, Print on paid. |
| `owner_notifications_v1.html` | Notifications page from the bell | Card per notification with type icon, short title, key details, one link. Unread = light violet + dot. Mark all as read. |
| `owner_account_v1.html` | Account hub, log out sheet, My details, Email notifications, Clinic info, Health library, article | One edit page for details (email changed via the clinic). Switches for email types; timing only shows when on. Big Call / Email on Clinic info. |
| `owner_auth_v1.html` | Log in (+ logged out, wrong password, not confirmed), Create account, Check your email, Forgot password, New password, Done | Band + centred card; login fits one screen. Eye button inside password fields. "Keep me logged in". No staff link. Terms only on Create account. |
| `owner_onboarding_v1.html` | First login: About you → First pet (skippable) → Pet added | Address is asked here (street optional; barangay, town, province required; town list of the 6 Marinduque towns + Other). |
| `owner_loading_states_v1.html` | Slow internet: opening a page, HTMX parts loading, late photos, sending, too long, offline | Buttons can't be tapped twice while sending. After ~10 s show "taking longer than usual" + Try again. Respects reduced motion. |
| `owner_dashboard_loading_v1.html` | Dashboard with parts loading separately | **Not for v1.** Build the dashboard as one normal page first; use this split only if real users find it slow. |

**Exploration only (not specs, keep or delete):**
`owner_dashboard_directions.html`, `owner_dashboard_b_paw.html`,
`owner_header_scroll.html`.

---

## Build notes and open items

No item below needs a model change unless it says so.

**Backend (views, forms, helpers)**
- One helper, e.g. `appointment.is_within_24h` (Manila time), drives the late
  note in both Cancel and Change time.
- Notifications need a short `title` and a compact `message`
  (e.g. "Visit confirmed" / "Browny · Mon, Nov 2 at 3:00 PM"). Change the text
  where notifications are created, not the model.
- Onboarding form makes barangay, town and province required (model stays
  `blank=True`).
- Reminder timing uses fixed choices on the form (appointment 0–3 days,
  vaccination 3/7/14/30 days); fields stay IntegerFields.
- "Follow-up booked" = a visit for that pet on or after the follow-up date.
- Booking from "no pet": Add a pet takes a `next` parameter and returns the new
  pet's id so step 1 pre-selects it.
- Bills filter as a GET form (`?q=&period=`) so it works without JavaScript.

**Already in the HANDOFF schema backlog (needs a migration plan)**
- "Take back my request" on a pet removal currently deletes the row; it should
  become a CANCELLED status. Planned for Slice 8a-0 (choices-only migration).
- A distinct "declined" cancellation reason, so owners see "declined" vs
  "cancelled by the clinic". Planned for Slice 8a-0 (choices-only migration).

**Docs to update**
- FEATURES 2.1: onboarding now asks for the address.
- FEATURES pet profile fields: owner pages use the species icon (not initials)
  when there's no pet photo.
- Breed and date of birth are optional (decision 2); the required stars in the mockups are wrong.

**Check before building**
- Terms and Privacy: decision 4 (placeholder links from settings).
- Google button: already a POST form with CSRF (2026-10-31); only the official button look is left.
- Put the real Hapi logo in the sign-in band and the top bar, inside a white
  rounded tile.
