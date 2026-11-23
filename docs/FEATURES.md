# FEATURES.md — Hapi Vet Feature List

> This file defines all features for Hapi Vet v1.
> Features are organized by module and build phase.
> Update status as development progresses.
> Do NOT add features outside v1 scope without discussion and approval first.

---

## Status Legend

| Status | Meaning |
|---|---|
| `planned` | Defined, not yet started |
| `in progress` | Currently being built |
| `complete` | Built and tested |
| `deferred` | Pushed to future version |

---

## Build Order (Phase by Phase)

```
Phase 1 — Foundation & Auth
Phase 2 — Pet Owner & Pet Management
Phase 3 — Appointment System
Phase 4 — Medical Records & Vaccination
Phase 5 — Billing & Services
Phase 6 — Notifications
Phase 7 — Dashboards & Polish
```

---

## Phase 1 — Foundation & Auth

### 1.1 Django Project Setup
| Feature | Status |
|---|---|
| Django project initialized | `complete ` |
| PostgreSQL connected locally | `complete ` |
| Custom user model (`AbstractUser`) created | `complete ` |
| Role field (`ADMIN` / `PET_OWNER`) on User model | `complete ` |
| Base template created with Tailwind CDN | `complete ` |
| HTMX loaded via CDN | `complete ` |
| `.env` file configured with `django-environ` | `complete ` |
| `accounts` app created | `complete ` |
| django-allauth installed and configured | `complete ` |
| Google OAuth credentials configured | `complete ` |

### 1.2 Authentication — Pet Owner
| Feature | Status |
|---|---|
| Register with email + password | `complete` |
| Email verification on registration (confirmation link sent) | `complete` |
| Account inactive until email verified | `complete` |
| Sign in with Google (skips email verification) | `complete` |
| Login page with both options (email/password + Google) | `complete` |
| Logout | `complete` |
| Logout is a POST (a GET never logs out: a logged-in user goes to their dashboard, a logged-out visitor to the owner login); every logout control is a POST form with a CSRF token. The log-out confirmation sheet comes with the 8b pages. Slice 8a-1b | `complete` |
| Resend the verification link ("Send the link again" on Check your email and on login for an unverified account; at most one send per 2 minutes per account) | `complete` (backend, 2026-11-17; page design in 8b-2) |
| `/accounts/signup/` and `/accounts/login/` redirect to the owner register and login pages (the Google provider and callback URLs keep working) | `complete` |
| Forgot password / reset password via email | `complete` |
| Throttling (SEC-2, `accounts/throttle.py`; no IP, emails hashed, fails open): login locks an email for 15 minutes after 5 wrong tries (owner and admin separately); forgot-password sends at most 3 per email and 60 per site per hour (same "sent" page when over); register at most 30 per site per hour | `complete` |
| Redirect to onboarding flow after first login | `complete` |

### 1.3 Authentication — Admin
| Feature | Status |
|---|---|
| Separate admin login page (`/admin-login/`) | `complete` |
| Initial credentials set via Django management command | `complete` |
| Admin logs in with username + password | `complete` |
| Admin can link Google account in profile settings | `planned` |
| Admin can use Google sign-in after linking | `planned` |
| Forgot password / reset password | `complete` |
| Redirect to admin dashboard after login | `complete` |

### 1.4 Access Control
| Feature | Status |
|---|---|
| Role-based access control enforced on all views | `complete` |
| Login required middleware for all protected routes | `complete` |
| Pet Owner cannot access admin views | `complete` |
| Admin cannot access pet owner views | `complete` |
| Separate login URLs for each role | `complete` |

---

## Phase 2 — Pet Owner & Pet Management

### 2.1 Onboarding Flow (First Login)
| Feature | Status |
|---|---|
| Onboarding triggered after first login | `complete` |
| Step 1 — Enter basic profile info (name, contact number) | `complete` |
| Step 1 — Ask for the address: barangay, town and province required, street optional; Marinduque town list + Other | `complete` (8b-3: Province Marinduque / Another province; the 6 towns and their barangays from the PSA list, or typed) |
| Step 3 — "Pet added" page: who was added, "Add another pet", "I'm done, go to my dashboard" | `complete` (8b-3) |
| Step 1 — Optional profile photo upload | `complete` |
| Step 1 — Optional contact links (Facebook, other socials) | `complete` |
| Step 2 — Add first pet (name, species and breed from lists with "Other (type it)", sex, birthday optional, color, weight, registration no., photo) | `complete` |
| Option to skip pet addition and add later | `complete` |
| Option to add multiple pets during onboarding | `complete` |
| Redirect to Pet Owner dashboard after onboarding | `complete` |

### 2.2 Pet Owner Profile
| Feature | Status |
|---|---|
| View own profile | `complete` |
| Edit profile (name, address, contact number) | `complete` |
| Email is changed only by the clinic (My details shows it read-only: "To change your email, contact the clinic."; the profile form has no email field and a posted email changes nothing) | `complete` (8a-2c; restyled in 8b-9) |
| Account hub: name, email, links to My details, Email notifications, Clinic info and Health library, and a POST Log out form (`/owner/account/`) | `complete` (8b-9a) |
| Edit contact links (Facebook, other socials) | `complete` |
| Profile photo upload (optional) | `complete` |
| Auto-generated initials avatar if no photo uploaded | `complete` |
| Notification preferences settings | `complete` |
| Appointment reminder timing preference (fixed choices: on the same day, 1, 2 or 3 days before) | `complete` |
| Vaccination reminder timing preference (fixed choices: 3, 7, 14 or 30 days before) | `complete` |
| Per-type email notification toggles (on/off) | `complete` |

### 2.3 Pet Management (Pet Owner)
| Feature | Status |
|---|---|
| Add a new pet (the Add pet page on the owner layout, same form as onboarding step 2: species and breed from lists, sex starting on "Not sure", birthday optional; from booking, `?next=book` returns to step 1 with the new pet picked; the pet's owner is always the logged-in owner) | `complete` (8b-3) |
| Edit pet details: name, breed (from the pet's species list), color, weight, photo with Change and Remove photo (species, sex, birthday and registration no. shown locked, with the clinic's phone; the page is on the owner layout since 8b-5) | `complete` (8b-5) |
| Pet photo upload (optional) | `complete` |
| Species icon on a light tile if no pet photo uploaded (no initials for pets) | `complete` (8b-5 on the pet profile and edit page; My Pets since 8b-4) |
| Request pet removal with a predefined reason (owners see "remove a pet"; a bottom sheet with four reasons on the pet profile; it reaches the Admin as a deletion request) | `complete` (8b-5) |
| Take back a removal request (POST only; the request is kept as Cancelled, never deleted, and isn't shown to the clinic as answered; the owner can send a new one). Slice 8a-1b | `complete` |
| Predefined deletion reasons (passed away, rehomed, duplicate, other) | `complete` |
| Deletion request goes to Admin for approval | `complete` |
| View pet profile (photo, facts, Book a visit, About / Vaccines / Records tabs, next visit, a due-vaccine note, a removal-requested box with take back; 8b-5) | `complete` (8b-5) |
| View pet vaccination history (the Vaccines tab; the old page redirects to it) | `complete` (8b-5) |
| View pet medical history (the Records tab: read-only, public notes only; the old page redirects to it) | `complete` (8b-5) |

### Pet Profile Fields
| Field | Required | Notes |
|---|---|---|
| Photo | Optional | Placeholder when no photo: the species icon (dog, cat, rabbit, bird, or a paw) on admin and owner pages; no initials for pets |
| Registration number | Optional | Government-issued |
| Name | Required | — |
| Species | Required | Dropdown (Dog, Cat, Bird, Rabbit, Hamster) with "Other (type it)" for a typed species |
| Breed | Optional | Dropdown for the chosen species ("Not sure" first) with "Other (type it)" for a typed breed; a typed species gets a plain typed breed |
| Color | Optional | — |
| Sex (`gender`) | Required | Not sure / Male / Female (admin form starts on "Not sure"; stored as unknown / male / female) |
| Date of birth | Optional | Can't be in the future |
| Age | Auto | Calculated from date of birth |
| Weight | Optional | In kg |

### 2.4 Pet & Owner Management (Admin)
| Feature | Status |
|---|---|
| View all registered pet owners (Active / Archived tabs; search by name, phone or email inside the open tab; 25 per page) | `complete` |
| Search pet owners (global search) | `complete` |
| Filter pet owners | `complete` |
| View individual pet owner profile: contact details, pets (archived pets folded away), the 5 most recent visits with a link to all of them, and account status (can log in / no email yet / password not set yet / archived) | `complete` |
| Create pet owner account manually (for walk-ins) | `complete` |
| Add owner without booking a visit (owners list; no pet, no appointment; set-password link sent if an email is given) | `complete` |
| Resend the set-password link: "Send the link again" on the owner page, asks first (owner has an email and has never set up the account; Google sign-ups count as set up) | `complete` |
| Edit pet owner details | `complete` |
| Address: Marinduque towns and barangays from dropdowns (PSA list); other provinces typed (Add owner and Edit details, and the owner's onboarding step 1 since 8b-3 (there the address is required); the owner's My details page since 8b-9a (also required); the walk-in New owner page is still typed) | `complete` |
| Archive pet owner account (soft delete; asks first, and warns about visits still to come, which archiving doesn't cancel) | `complete` |
| Restore archived pet owner account (asks first) | `complete` |
| View all registered pets (tabs Active / Archived / Deletion requests, with counts; a pet of an archived owner is on Archived; split rows: the pet with its tags, then the owner with a link and phone number; 25 per page) | `complete` |
| Search and filter pets (every word matches pet name, species, breed or owner name; filter by owner) | `complete` |
| View individual pet profile: pet details, the 3 most recent visits, medical records and vaccinations (each with a link to the full list; no private notes), a yellow line when a vaccination is due within 30 days, the owner, and banners for a waiting deletion request (answer it right there), an archived pet or an archived owner | `complete` |
| Archive a pet directly with a reason (recorded in the deletion history; owner notified) | `complete` |
| Add a pet manually | `complete` |
| Edit pet details | `complete` |
| Approve pet deletion request ("Archive {pet}" on the Deletion requests tab, asks first) | `complete` |
| Reject pet deletion request ("Keep {pet}", asks first); answered requests listed below, newest first, as Archived or Kept | `complete` |
| Owner is notified (in the app, and by email if they have one) when a deletion request is approved or rejected | `complete` |
| Restore an archived pet from the Archived tab of the Pets list or the pet's page (asks first; only while its owner is active; a pet of an archived owner comes back with the owner) | `complete` |
| Archived pets and pets of archived owners can't be edited or get new medical records or vaccinations | `complete` |

### 2.5 Walk-in Client Flow
| Feature | Status |
|---|---|
| Admin creates pet owner account with minimum details | `complete` |
| System sends claim account email automatically if email provided | `complete` |
| Pet owner receives one-time link to set their own password | `complete` |
| If no email provided — account saved, claim email sent when email is added later | `complete` |
| Emails saved lowercase; an email already used by another account (in any letter case) is refused | `complete` |
| Claim email sent after the owner is saved; if sending fails, the owner and pet stay saved and the admin sees a warning ("Use Send the link again on the owner page later.") | `complete` |
| Only one owner without an email can be saved for now (plain message for a second one; nullable email planned) | `complete` |
| Admin creates appointment directly as CONFIRMED for walk-in (see 3.5 Walk-in rules) | `complete` |
| Walk-in in three steps: 1 Who is it for? (search by owner name, pet name or phone, or add a New owner with their pet; the pet is required), 2 The visit (pet, date, time, optional service and reason), 3 Check and book (summary; nothing is saved until "Book walk-in") | `complete` |
| If the chosen time is booked by someone else meanwhile, step 2 comes back with every other value kept and "{time} was just taken…" | `complete` |
| Owner is notified in the app (and by email if they have one): "The clinic booked a visit for Max on Mon, Nov 2 at 10:00 AM." | `complete` |

---

## Phase 3 — Appointment System

### 3.1 Clinic Schedule Configuration (Admin)
| Feature | Status |
|---|---|
| Set clinic opening and closing time | `complete` |
| Set working days per week | `complete` |
| Set appointment slot duration (configurable — 30 or 60 minutes) | `complete` |
| Set same-day booking cutoff time | `complete` |
| System auto-generates available slots based on configuration | `complete` |
| Admin can block specific dates (holidays, rest days) | `complete` |
| Blocking a day that already has CONFIRMED or PENDING visits: the first submit lists them (time, pet, owner) and saves nothing; "Block anyway" saves. Visits are never changed. Errors and the warning swap only the blocked-dates panel; success reloads the page with a flash | `complete` |
| Booking limit (days ahead) is 1 to 365 | `complete` |

### 3.2 Booking (Pet Owner)
| Feature | Status |
|---|---|
| View available appointment slots | `complete` |
| View list of active services and pricing before booking | `planned` |
| Book an appointment (select pet, date, time slot, service, reason) | `complete` |
| Booking limited to 30 days in advance | `complete` |
| Same-day booking allowed before configured cutoff time | `complete` |
| Request reschedule — select new slot and provide reason | `complete` |
| Cancel appointment freely up to 24 hours before | `complete` |
| Cancellation within 24 hours requires Admin approval (not built: owners can currently cancel at any time; late cancellations are only flagged on the owner page) | `planned` |
| Predefined cancellation reasons (change of plans, pet unwell, cannot make time, other) | `complete` |
| Owner reschedule gives up the current visit and makes a new PENDING request in one transaction (both happen or neither; a visit that can't be changed any more says so; a time taken a moment ago comes back as an error on the time field). Slice 8a-1b | `complete` |
| Owner changes day or time (Change day or time page): pick a new day on the calendar, then a time, say why; the visit goes back to waiting for approval, the page says the old time is given up ("Your {time} time is given up. The new time waits for the clinic to confirm."), and a visit less than 24 hours away gets a late-change note with the clinic's phone. Only the owner's own Pending / Confirmed visit whose time hasn't started. Slice 8b-7b | `complete` |
| Booking lock: owner booking, owner reschedule, walk-in and admin move lock the ClinicSettings row, then check the slot again before saving (no migration; a DB constraint stays in the backlog). Slice 8a-1b | `complete` |
| Clinic decline is stored as the `declined` cancellation reason (old declines stay `other`, not backfilled): backend done in 8a-1b. The owner wording "Declined by the clinic" vs "Cancelled by the clinic" on the redesigned visit pages comes with 8b | `complete` |
| View appointment history and current status | `complete` |
| Book a request in 3 steps (pet and reason, date and time, check and send), then a "Request sent" page; the choices wait in a session draft and nothing is saved until Send (always PENDING, through the locked save). Slice 8b-6 | `complete` |
| Booking calendar shows open, full and closed days (month arrows, up to the booking limit); times are grouped Morning / Afternoon, taken times are shown but can't be picked. Slice 8b-6. Colour code (8b-10b-5): open = light violet, picked = solid violet, full / taken = gray crossed out, closed = faded; one hint line "Tap an open box." and no key under the calendar | `complete` |
| "Not sure yet" as the visit reason (saves no service) | `complete` |
| No-pet path: step 1 sends the owner to Add a pet in booking mode and back with the new pet picked. Slice 8b-6 | `complete` |
| Visits list (Visits tab): Upcoming / Past links with counts, a pet filter (chips, from 2 pets), cards grouped by month with a note per visit (what happens next, who moved or cancelled it), Past 25 a page; an original cancelled by a reschedule is left out (its new visit says "Was …"). Slice 8b-7a | `complete` |
| Visit detail for every status with a coloured banner (waiting, confirmed, time passed, done, declined, cancelled by the clinic or by you, moved, missed), the clinic's own message for a cancel or decline, links to the vet's notes and the Unpaid/Paid bill, and the buttons for that state. Slice 8b-7a | `complete` |
| Cancel sheet on the visit page: reasons as radio cards, a "Tell us a little more" box for Other, the orange late note within 24 hours (with the clinic phone), POST only; errors and success come back to the visit page as a flash. Slice 8b-7a | `complete` |

### 3.3 Appointment Management (Admin)
| Feature | Status |
|---|---|
| View all appointments in a list: status tabs (Needs approval, Upcoming, Completed, Cancelled, No show, All) with counts, grouped by day, 25 per page | `complete` |
| Search appointments by pet or owner name and by date (searches every status) | `complete` |
| Notice for past visits still marked Confirmed, with a link to them | `complete` |
| View all appointments on a calendar page | `planned` |
| Calendar monthly and weekly view toggle | `complete` |
| Colored dot indicators on calendar days with appointments | `complete` |
| Click day to view appointment details in side panel | `planned` |
| Approve appointment request (PENDING → CONFIRMED) | `complete` |
| Approve refused once the requested date has passed (move or decline it instead) | `complete` |
| Reject appointment request (PENDING → CANCELLED) | `complete` |
| Mark appointment as COMPLETED | `complete` |
| Mark appointment as NO_SHOW | `complete` |
| Add notes to appointment | `complete` |
| Reschedule appointment on behalf of pet owner with reason | `complete` |
| Cancel appointment on behalf of pet owner with reason | `complete` |
| Create walk-in appointment directly as CONFIRMED | `complete` |
| Slot conflict prevention (no double booking enforced) | `complete` |
| Cancelled slot freed immediately and becomes available | `complete` |

### 3.4 Appointment Status Flow
```
PENDING → CONFIRMED → COMPLETED
                    → NO_SHOW
PENDING or CONFIRMED → CANCELLED
```
- Admin reject: PENDING → CANCELLED.
- Cancel (admin or owner): PENDING or CONFIRMED → CANCELLED.
- Admin reschedule (PENDING or CONFIRMED): the original becomes CANCELLED and
  a new appointment is created as CONFIRMED with `rescheduled_from` = the
  original (the clinic chose the slot). An owner reschedule creates it as PENDING.
- COMPLETED / NO_SHOW: only from CONFIRMED, on or after the appointment date.
- Walk-ins are created as CONFIRMED.

### 3.5 Appointment Rules
| Rule | Detail |
|---|---|
| Booking limit | Set by the admin, 1 to 365 days in advance (default 30) |
| Same-day booking | Allowed before the configurable cutoff time (Manila time). Before the cutoff, today offers only slots that haven't started yet; from the cutoff on, no slots today. Applies to the owner booking and reschedule forms, and is re-checked on submit. |
| Move rules (admin) | Same as owner booking, except today has no same-day cutoff: only slots whose start is after the current Manila time are offered (not the slot in progress). Re-checked on submit. |
| Slots | One appointment per slot |
| Walk-in rules (admin) | Date defaults to today; future dates allowed inside the normal booking window (phone bookings), still saved as walk-ins. Today: no same-day cutoff, the slot in progress and later ones are offered (slot end after the current Manila time). Past dates, blocked dates and closed days have no slots. PENDING requests keep blocking their slot. |
| Cancellation deadline | Planned, not built: 24 hours, after that requires Admin approval. Today owners can cancel at any time; within 24 hours the owner page only shows a warning. |
| Appointment type | Derived from service selected |
| Additional services | Admin adds extras at billing stage |

---

## Phase 4 — Medical Records & Vaccination

### 4.1 Medical Records (Admin)
| Feature | Status |
|---|---|
| Create medical record (no timing restriction) | `complete` |
| One medical record per appointment | `complete` |
| Record: diagnosis, symptoms, treatment given | `complete` |
| Structured prescriptions (list of medicine items) | `complete` |
| Each prescription item: medicine name, dosage, frequency, duration, notes | `complete` |
| Test results — text description | `complete` |
| Test results — file/image upload (PDF, image) | `complete` |
| Public notes (pet owner visible) | `complete` |
| Private notes (Admin only, strictly hidden) | `complete` |
| Follow-up required toggle (boolean) | `complete` |
| Follow-up date field | `complete` |
| Medical records are immutable — no deletion allowed | `complete` |
| View full medical history per pet | `complete` |

### 4.2 Medical Records (Pet Owner)
| Feature | Status |
|---|---|
| View pet medical history (read-only) | `complete` |
| View public notes only | `complete` |
| View prescription details | `complete` |
| View test results (text only, no file access) | `complete` |
| View vaccination records per pet | `complete` |
| Open one medical record (`/owner/pets/<pet>/medical/<record>/`, back to the pet's Records tab): the blue follow-up box first (not booked: "Book the follow-up"; booked: a link to that visit; date passed: "Follow-up was due around…"), what Dr. Aga found, medicines to give at home (how much / how often / for, with the note), test results as text only ("On file. Ask the clinic for a copy."), vaccine given ("Corrected by the clinic", never the correction note), the clinic's public notes, and links to the visit and its bill (never a cancelled one); sections with nothing in them are left out; no private notes, no print | `complete` (8b-8) |

### 4.3 Vaccination Tracking
| Feature | Status |
|---|---|
| Create vaccination record through appointment (linked to medical record) | `complete` |
| Create standalone vaccination record (no appointment needed) | `complete` |
| Both paths update pet vaccination history | `complete` |
| Predefined vaccine name options (Deworming, Ivermectin, Anti-rabies, 6-in-1, 7-in-1, 8-in-1, Bordetella) | `complete` |
| Custom vaccine name input if not in predefined list | `complete` |
| Vaccination fields: vaccine name, date administered, weight at time of vaccination, next due date, batch/lot number, manufacturer, administered by, site of injection | `complete` |
| View complete vaccination history per pet | `complete` |
| Admin can manually trigger vaccination reminder | `complete` |

---

## Phase 5 — Billing & Services

### 5.1 Services and Pricing (Admin)
| Feature | Status |
|---|---|
| Create a new service | `complete` |
| Edit service name, description, and pricing | `complete` |
| Set pricing type: Fixed or Range | `complete` |
| Fixed pricing — single base price | `complete` |
| Range pricing — min price and max price shown to pet owners | `complete` |
| Actual billed amount set by Admin at billing time | `complete` |
| Set service status: Draft / Active / Unlisted / Closed (shown as Hidden (draft) / Offered / Clinic use only / No longer offered); a new service starts as Hidden (draft); no delete, No longer offered retires one | `complete` |
| Service names are unique, ignoring letter case and outer spaces | `complete` |
| Saving clears the other pricing type (One price clears the range; a range sets the base price to its lowest price) | `complete` |
| Optional service category (Admin-defined freely) | `complete` |
| Services page: one list grouped by who sees each service (Offered, Clinic use only, Hidden (draft), No longer offered), A to Z, with the price and "One price" / "Price range"; add and edit on one form (the price boxes follow the chosen pricing type) | `complete` |

### Service Status Behavior
| Status | Visible to Pet Owner | Bookable |
|---|---|---|
| **Draft** | No | No |
| **Active** | Yes | Yes |
| **Unlisted** | No | Admin use only |
| **Closed** | No (hidden; old visits and bills keep it) | No |

### 5.2 Billing (Admin)
| Feature | Status |
|---|---|
| Generate a bill at any point (not restricted to COMPLETED) | `complete` |
| Bill started automatically when a visit is marked Completed (fixed-price service added as the first item) | `complete` |
| Add catalog services as billing line items | `complete` |
| Add manual items (medicines, supplies) as billing line items | `complete` |
| Each line item: quantity, description, unit price, subtotal | `complete` |
| System auto-calculates total | `complete` |
| Apply discount per bill (percentage or fixed amount) | `complete` |
| Discount note field (e.g. "Loyal client discount") | `complete` |
| Payment is cash only: recorded as Cash, with today's date, by Mark as paid (no payment method field; GCash / bank transfer is a future expansion point) | `complete` |
| Set payment status: Pending / Paid / Cancelled, shown as Unpaid / Paid / Cancelled (via Mark as Paid / Cancel bill actions only) | `complete` |
| Cancelled bills are hidden from the pet owner (list, bill page, search) | `complete` |
| Change a bill while it is Unpaid: add or remove items and set the discount on the bill page (no separate edit page) | `complete` |
| Print an Unpaid, Paid or Cancelled bill from the bill page (the printout is titled "Bill", carries a PAID / UNPAID / CANCELLED stamp and ends with "This is not an official BIR invoice.") | `complete` |
| Bill locked once marked as Paid or Cancelled | `complete` |
| View all billing transactions | `complete` |
| View bill detail | `complete` |

### 5.3 Billing (Pet Owner)
| Feature | Status |
|---|---|
| View the bill for completed appointments | `complete` |
| View full billing history: Unpaid and Paid bills only (cancelled ones are never shown); an Unpaid bill appears once it has at least one item | `complete` |
| Bills page: a yellow "To pay" box with the total and count, the Unpaid bills (never hidden by a filter), or a green "You're all paid up" box; "No bills yet" page when there are none | `complete` (8b-8) |
| Bills History filter: search by bill number or pet name plus a period (All time, This month, Last 3 months, this year, last year), a plain GET form, month groups, 25 a page | `complete` (8b-8) |
| Bill page (read-only): banner (please pay at the clinic / paid on), pet, bill date, link to the visit, items, subtotal, discount, total, "Paid by"; "Something looks wrong?" line while Unpaid | `complete` (8b-8) |
| Save as PDF from the print window | `complete` |
| Print bill (the button shows on a Paid bill) | `complete` (8b-8) |
| Receive a notification when a bill is paid | `complete` |

### Bill Format (the printout)
```
┌─────────────────────────────────────────┐
│  [Clinic Logo]                          │
│  Hapi Tutz Vet. Supplies                │
│  Bognuyan, Gasan, Marinduque            │
│  Contact: xxxx-xxxx                     │
│  Email: hapitutzvet@gmail.com           │
├─────────────────────────────────────────┤
│  BILL                                   │
│  Bill no.: HV-2025-0001                 │
│  Date: January 1, 2025                  │
│  Payment Status: Paid                   │
├─────────────────────────────────────────┤
│  BILLED TO                              │
│  Name: Juan Dela Cruz                   │
│  Contact: 09xxxxxxxxx                   │
│  Pet: Brownie                           │
├─────────────────────────────────────────┤
│  SERVICES & ITEMS                       │
│  Qty  Description        Unit    Amount │
│  1    Consultation       ₱300    ₱300   │
│  1    Deworming          ₱150    ₱150   │
│  1    Amoxicillin 500mg  ₱50     ₱50    │
├─────────────────────────────────────────┤
│  Subtotal:               ₱500          │
│  Discount:               -₱50          │
│  TOTAL:                  ₱450          │
├─────────────────────────────────────────┤
│  Thank you for trusting                 │
│  Hapi Tutz Vet. Supplies!              │
│  We care for your pets like our own.   │
└─────────────────────────────────────────┘
```

---

## Phase 6 — Notifications

### 6.1 In-App Notifications
| Feature | Status |
|---|---|
| Notification stored in database on trigger event | `complete` |
| Bell icon in dashboard header | `complete` |
| Red dot on bell icon when unread notifications exist (the top-bar Notifications button shows "N new" when unread notifications exist) | `complete` |
| Red dot disappears when all notifications are read | `complete` |
| Click bell to open notification list | `complete` |
| Click individual notification to view detail and mark as read | `complete` |
| Mark all as read button | `complete` |
| In-app notifications always on regardless of email toggles | `complete` |
| Admin can clear notifications (all, read only) | `complete` |
| Owner notifications page `/owner/notifications/` (backend and a plain page; Slice 8a-2b): newest first, 25 a page, grouped Today / Earlier by Manila date, unread flagged, one link per row through `notification_open` (no link when there is nowhere to go), "Mark all as read" (`return_to=page`), "See all notifications" link in the bell panel | `complete` |
| Owner notifications page design (cards with type icon, violet unread, bell badge capped at 9+; `owner_notifications_v1.html`) | `complete` (8b-9a) |
| Owner notifications use a short title and a compact message ("Visit confirmed" / "Bantay · Mon, Nov 2 at 3:00 PM"); the email keeps the full sentence; old rows keep their long text (decision 9) | `complete` |

### 6.2 Email Notifications (Gmail SMTP)
| Feature | Status |
|---|---|
| Email sent on appointment requested (to Admin) | `complete` |
| Email sent on appointment approved (to Pet Owner) | `complete` |
| Email sent on appointment rejected (to Pet Owner) | `complete` |
| Email sent on appointment reminder (to Pet Owner) | `complete` |
| Email sent when a bill is paid (to Pet Owner; subject "Bill HV-2026-0012 for Bantay — Hapi Vet") | `complete` |
| Email sent on vaccination reminder (to Pet Owner) | `complete` |
| Email sent on follow-up reminder (to Pet Owner) | `complete` |
| Admin configurable notification email in clinic settings | `complete` |
| Failed email logged in system | `planned` |
| Admin warned in dashboard when email fails | `planned` |
| Admin can manually trigger resend of failed email | `planned` |

### 6.3 Notification Preferences
| Feature | Status |
|---|---|
| Pet Owner per-type email toggle (appointment reminders on/off) | `complete` |
| Pet Owner per-type email toggle (appointment status updates on/off) | `complete` |
| Pet Owner per-type email toggle (the "Bills" switch: "When the clinic records your payment." on/off) | `complete` |
| Pet Owner per-type email toggle (vaccination reminders on/off) | `complete` |
| Pet Owner per-type email toggle (follow-up reminders on/off) | `complete` |
| Pet Owner appointment reminder timing preference (fixed choices: same day, 1, 2 or 3 days before; an earlier saved value stays selectable) | `complete` |
| Pet Owner vaccination reminder timing preference (fixed choices: 3, 7, 14 or 30 days before; an earlier saved value stays selectable) | `complete` |
| Admin per-type email toggles (all on by default) | `complete` |

### 6.4 Reminder System
| Feature | Status |
|---|---|
| Appointment reminder — pet owner configurable (default 1 day before) | `complete` |
| Appointment day-of reminder always fires (safety net) | `complete` |
| Vaccination reminder — pet owner configurable (default 7 days before) | `complete` |
| Vaccination day-of reminder always fires (safety net) | `complete` |
| Admin can manually trigger vaccination reminder at any time | `complete` |
| Follow-up reminder auto-triggers 3 days before follow-up date | `complete` |
| Follow-up day-of reminder always fires (safety net) | `complete` |
| Admin can manually trigger follow-up reminder at any time | `complete` |

### 6.5 Notification Trigger Events
| Event | Recipient |
|---|---|
| Appointment requested | Admin |
| Reschedule (time change) requested | Admin |
| Appointment cancelled by owner | Admin |
| Appointment approved | Pet Owner |
| Appointment rejected | Pet Owner |
| Appointment reminder (configurable + day-of) | Pet Owner |
| Bill paid (one notification, type `billing_generated`; nothing when a bill is created) | Pet Owner |
| Vaccination due (configurable + day-of) | Pet Owner |
| Follow-up due (3 days before + day-of) | Pet Owner |
| Pet deletion request answered (PET_DELETION_ANSWERED) | Pet Owner |

---

## Phase 7 — Dashboards & Polish

### 7.1 Admin Dashboard
| Feature | Status |
|---|---|
| Stat cards: waiting for approval, unpaid bills, vaccinations and follow-ups due this week (today to today + 7) | `complete` |
| Month calendar: month dropdown (3 months back to the end of the booking window), Today button, count per day (yellow when a request is waiting) | `complete` |
| Click a day to load it into the schedule | `complete` |
| Stat cards refresh every 60 seconds | `complete` |
| Needs approval list (every waiting request; says so when none are waiting) | `complete` |
| Today's schedule with the next visit marked | `complete` |
| Global search across pet owners, pets, appointments, medical records | `complete` |
| Search results grouped by type | `complete` |

### 7.2 Pet Owner Dashboard
| Feature | Status |
|---|---|
| Upcoming appointments section | `complete` |
| My pets summary | `complete` |
| Recent notifications | `complete` |
| Next visit and the visits after it, Pending or Confirmed only (never Completed or No show); unpaid bills count and total; new-owner state (backend context `owner_dashboard_context`, Slice 8a-2b; no 30-second polling, decision 13) | `complete` |
| Next visit card, upcoming list (max 3), unpaid bills box (only above 0, peso total) and new-owner welcome card on the page, with the violet band and the see-through top bar (`owner_pages_v1.html` frames 1–2; Slice 8b-4) | `complete` |
| My Pets page: two-column photo grid, "Removal requested" tag, search by name and All / Dogs / Cats / Others chips (shown from 4 pets), empty states (`owner_my_pets_v2.html`; Slice 8b-4) | `complete` |
| Quick book appointment button | `complete` |
| Health Library quick access card | `complete` |
| Clinic info quick access | `complete` |
| Scoped search (own data only) | `complete` |

### 7.3 Clinic Info & About Page
| Feature | Status |
|---|---|
| Dr. Edgar about page (name, license number, bio) | `complete` |
| Clinic name, address, contact numbers | `complete` |
| Clinic operating hours | `complete` |
| Google Maps embed of clinic location (admin page only; the owner page has none since 8b-9b) | `complete` |
| Owner Clinic info on the owner layout: logo or paw tile, Call and Email buttons, rows for hours (with "Closed …"), phone, email and address, and a "Your vet" card (`owner_account_v1.html` frame 5; Slice 8b-9b). It has no Book an appointment button (the Visits tab books) | `complete` |
| Visible to both Admin and Pet Owner | `complete` |

### 7.4 Clinic Settings (Admin)
| Feature | Status |
|---|---|
| Edit clinic name, address, contact number, email | `complete` |
| Upload clinic logo | `complete` |
| Logo rule: JPG, PNG or WebP (any letter case), up to 2 MB. SVG and GIF are refused ("Only JPG, PNG or WebP files can be used."); too big shows "This file is 4.3 MB. Please choose one up to 2 MB." (rounded up). The type is checked first. Saving without a new file keeps the logo; the clear checkbox removes it (backend 7a-6; the help text shows on the Clinic details tab) | `complete` |
| Edit operating hours and working days | `complete` |
| Edit appointment slot duration | `complete` |
| Set same-day booking cutoff time | `complete` |
| Set admin notification email | `complete` |
| Edit veterinarian profile (Dr. Edgar's details and bio) | `complete` |
| Settings open on a tab with `?tab=` (details, schedule, blocked, vet); a failed save reopens the tab with the error and shows a "1 problem" count (backend 7a-5; the tabs are server-rendered links since 7b-2) | `complete` |
| Each tab saves only its own fields: a save that posts `tab` (details, schedule or vet) validates and saves only that tab's fields and returns to `?tab=<that tab>`; a save without `tab` (not sent by the page; kept for old clients) still saves every field | `complete` |
| Schedule warning: changing the opening hours, working days, slot length or same-day cutoff while CONFIRMED or PENDING visits exist from today on saves nothing and lists them (date and time order, first 10 then "and N more"); "Yes, save the new schedule" saves, "Go back" saves nothing. Visits are never changed, cancelled or moved. Changing only the booking limit never warns | `complete` |
| Banner hours line from the saved settings (e.g. "Mon to Sat, 8:00 AM to 5:00 PM") | `complete` |

### 7.5 Health Library
| Feature | Status |
|---|---|
| Admin can create health articles | `complete` |
| Admin can edit, publish and unpublish health articles (no delete; a new article starts as a draft) | `complete` |
| Each article: condition name, description, causes, symptoms, general care | `complete` |
| Pet Owner can browse and read articles (read-only) | `complete` |
| Owner Health library list: cards with a one-line summary, A to Z by title, 25 a page, published articles only (`owner_account_v1.html` frames 6 and 6b; Slice 8b-9b) | `complete` |
| Owner article page: each non-empty part under its heading, a "not a diagnosis" box and one Book a visit button; an unpublished or missing article is a 404 (frame 7; Slice 8b-9b) | `complete` |
| Search articles by condition name (owner: GET form, every word in the title, "Clear search", "No guide about that yet"; Slice 8b-9b) | `complete` |
| Visible to both Admin and Pet Owner (login required) | `complete` |
| Health Library accessible from Pet Owner dashboard | `complete` |

### 7.6 General UI/UX
| Feature | Status |
|---|---|
| Sidebar-based dashboard layout (Admin and Pet Owner) | `complete` |
| Modal-based forms and confirmations | `complete` |
| Mobile responsive — Pet Owner views prioritized | `planned` |
| Confirmation dialogs for destructive actions | `complete` |
| Success / error flash messages | `complete` |
| Simple empty states with text and action button | `complete` |
| Loading states for HTMX requests | `complete` |
| Owner slow internet, part 1 (10b-1): sending buttons, opening a page, "taking longer than usual" with Try again after 10 s, offline strip (`owner_loading_states_v1.html` frames 1, 5, 6, 7) | `complete` |
| Owner slow internet, part 2 (10b-2): HTMX lists loading with Try again, late photos (icon tile behind the photo), photo preview in the tile, one-time token for Add pet and onboarding step 2 | `complete` |
| Owner slow internet, part 3 (10b-3): free times swap on booking step 2 and Change day or time (only the times area reloads, grey boxes, "Finding free times for…", Try again after 10 s or an error; `owner_loading_states_v1.html` frames 2 and 6) | `complete` |
| Owner slow internet, part 4 (10b-4): search results swap on My Pets, Bills (History) and the Health library (grey cards while typing, spinner in the box, "Searching…", Try again after 10 s or an error; frame 3 of `owner_loading_states_v1.html`) | `complete` |
| Instant "not a photo" message when a non-image (or a HEIC) is chosen on an owner photo field (future expansion point; today the server refuses it after the upload) | `planned` |
| Cascading species/breed dropdown with custom input option | `complete` |
| Color-coded appointment status badges | `complete` |

---

## Explicitly Out of Scope (v1)

These features will NOT be built in v1 under any circumstances:

- Online payment gateway (GCash, Stripe, PayPal)
- SMS notifications (Semaphore, Twilio)
- Real-time chat or messaging system
- AI-based diagnosis or suggestions
- Multi-veterinarian scheduling
- Multi-clinic / multi-branch support
- Marketplace or e-commerce features
- Receptionist or staff role management
- Inventory management
- Reporting and analytics module
- Mobile application (iOS / Android)

---

## Known Future Expansion Points

| Item | Notes |
|---|---|
| Staff / receptionist role | Add role options to User model |
| Multi-vet support | Veterinarian table in DB, Appointment already has `veterinarian_id` |
| SMS notifications | Add SMS provider config to notification system |
| Reporting module | Billing and appointment data already structured for this |
| Online payments | Billing system designed to accommodate payment status updates |
| Record GCash or bank transfer payments | Payments are cash only today; `payment_method` always saves "Cash" |
| Auto-delete notifications | Can add scheduler when hosting plan allows |
| Public health library | Remove login requirement to make articles publicly accessible |
| Keep the original visit until an owner reschedule is approved | Today the current visit is cancelled and a new PENDING request is made in one transaction |
| Limit open requests per owner | Not in v1 |
| Owner email change | Today only the clinic changes an owner's email (decision 3); My details shows it read-only |
| Owner changes their password while logged in | No page for it yet (the Account hub has no "Sign-in and password" row); needs a design and a page for password owners, and a "set a password" case for Google-only owners |
| Late-cancellation approval | Also planned in 3.2 |
| Owner calendar page | Admin calendar page is planned separately |
| Split dashboard loading | Load dashboard parts separately (`owner_dashboard_loading_v1.html`) only if real users find the single page slow |
| Print a medical record for owners | Owners can print bills only today |
| Prices on the booking page | Also planned in 3.2 |
| Owner account deletion / data export | Not in v1 |
