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
| Forgot password / reset password via email | `complete` |
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
| Step 1 — Optional profile photo upload | `complete` |
| Step 1 — Optional contact links (Facebook, other socials) | `complete` |
| Step 2 — Add first pet (name, species, breed, date of birth) | `complete` |
| Option to skip pet addition and add later | `complete` |
| Option to add multiple pets during onboarding | `complete` |
| Redirect to Pet Owner dashboard after onboarding | `complete` |

### 2.2 Pet Owner Profile
| Feature | Status |
|---|---|
| View own profile | `complete` |
| Edit profile (name, address, contact number, email) | `complete` |
| Edit contact links (Facebook, other socials) | `complete` |
| Profile photo upload (optional) | `complete` |
| Auto-generated initials avatar if no photo uploaded | `complete` |
| Notification preferences settings | `complete` |
| Appointment reminder timing preference | `complete` |
| Vaccination reminder timing preference (days in advance) | `complete` |
| Per-type email notification toggles (on/off) | `complete` |

### 2.3 Pet Management (Pet Owner)
| Feature | Status |
|---|---|
| Add a new pet | `complete` |
| Edit minor pet details directly (name, color, weight, breed) | `complete` |
| Pet photo upload (optional) | `complete` |
| Auto-generated initials avatar if no pet photo uploaded | `complete` |
| Request pet deletion with predefined reason | `complete` |
| Predefined deletion reasons (passed away, rehomed, duplicate, other) | `complete` |
| Deletion request goes to Admin for approval | `complete` |
| View pet profile | `complete` |
| View pet vaccination history | `complete` |
| View pet medical history (read-only, public notes only) | `complete` |

### Pet Profile Fields
| Field | Required | Notes |
|---|---|---|
| Photo | Optional | Placeholder when no photo: species icon (dog, cat, rabbit, bird, or a paw) on admin pages; initials avatar on owner pages |
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
| Address: Marinduque towns and barangays from dropdowns (PSA list); other provinces typed (Add owner and Edit details; owner-side forms and the walk-in New owner page still typed) | `complete` |
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
| View appointment history and current status | `complete` |

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
| **Closed** | No (hidden; old visits and receipts keep it) | No |

### 5.2 Billing (Admin)
| Feature | Status |
|---|---|
| Generate billing receipt at any point (not restricted to COMPLETED) | `complete` |
| Add catalog services as billing line items | `complete` |
| Add manual items (medicines, supplies) as billing line items | `complete` |
| Each line item: quantity, description, unit price, subtotal | `complete` |
| System auto-calculates total | `complete` |
| Apply discount per receipt (percentage or fixed amount) | `complete` |
| Discount note field (e.g. "Loyal client discount") | `complete` |
| Payment is cash only: recorded as Cash, with today's date, by Mark as paid (no payment method field; GCash / bank transfer is a future expansion point) | `complete` |
| Set payment status: Pending / Paid / Cancelled, shown as Unpaid / Paid / Cancelled (via Mark as Paid / Cancel Receipt actions only) | `complete` |
| Cancelled receipts are hidden from the pet owner (list, receipt page, search) | `complete` |
| Change a receipt while it is Unpaid: add or remove items and set the discount on the receipt page (no separate edit page) | `complete` |
| Print an Unpaid, Paid or Cancelled receipt from the receipt page (the printout carries a PAID / UNPAID / CANCELLED stamp) | `complete` |
| Receipt locked once marked as Paid or Cancelled | `complete` |
| View all billing transactions | `complete` |
| View receipt detail | `complete` |

### 5.3 Billing (Pet Owner)
| Feature | Status |
|---|---|
| View billing receipt for completed appointments | `complete` |
| View full billing history: Unpaid and Paid receipts only (cancelled ones are never shown) | `complete` |
| Save as PDF from the print window | `complete` |
| Print receipt | `complete` |
| Receive a notification when a receipt is paid | `complete` |

### Receipt Format
```
┌─────────────────────────────────────────┐
│  [Clinic Logo]                          │
│  Hapi Tutz Vet. Supplies                │
│  Bognuyan, Gasan, Marinduque            │
│  Contact: xxxx-xxxx                     │
│  Email: hapitutzvet@gmail.com           │
├─────────────────────────────────────────┤
│  RECEIPT                                │
│  Receipt #: HV-2025-0001                │
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

### 6.2 Email Notifications (Gmail SMTP)
| Feature | Status |
|---|---|
| Email sent on appointment requested (to Admin) | `complete` |
| Email sent on appointment approved (to Pet Owner) | `complete` |
| Email sent on appointment rejected (to Pet Owner) | `complete` |
| Email sent on appointment reminder (to Pet Owner) | `complete` |
| Email sent when a receipt is paid (to Pet Owner) | `complete` |
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
| Pet Owner per-type email toggle ("Email when a receipt is paid." on/off) | `complete` |
| Pet Owner per-type email toggle (vaccination reminders on/off) | `complete` |
| Pet Owner per-type email toggle (follow-up reminders on/off) | `complete` |
| Pet Owner appointment reminder timing preference (days before) | `complete` |
| Pet Owner vaccination reminder timing preference (days before) | `complete` |
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
| Receipt paid (one notification, type `billing_generated`; nothing when a receipt is created) | Pet Owner |
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
| Google Maps embed of clinic location | `complete` |
| Book an appointment button on clinic info page | `complete` |
| Visible to both Admin and Pet Owner | `complete` |

### 7.4 Clinic Settings (Admin)
| Feature | Status |
|---|---|
| Edit clinic name, address, contact number, email | `complete` |
| Upload clinic logo | `complete` |
| Edit operating hours and working days | `complete` |
| Edit appointment slot duration | `complete` |
| Set same-day booking cutoff time | `complete` |
| Set admin notification email | `complete` |
| Edit veterinarian profile (Dr. Edgar's details and bio) | `complete` |
| Settings open on a tab with `?tab=` (details, schedule, blocked, vet); a failed save reopens the tab with the error and shows a "1 problem" count (backend 7a-5; the page itself changes in 7b-2) | `complete` |
| Each tab saves only its own fields: a save that posts `tab` (details, schedule or vet) validates and saves only that tab's fields and returns to `?tab=<that tab>`; a save without `tab` (the current page) still saves every field | `complete` |
| Schedule warning: changing the opening hours, working days, slot length or same-day cutoff while CONFIRMED or PENDING visits exist from today on saves nothing and lists them (date and time order, first 10 then "and N more"); "Yes, save the new schedule" saves, "Go back" saves nothing. Visits are never changed, cancelled or moved. Changing only the booking limit never warns | `complete` |
| Banner hours line from the saved settings (e.g. "Mon to Sat, 8:00 AM to 5:00 PM") | `complete` |

### 7.5 Health Library
| Feature | Status |
|---|---|
| Admin can create health articles | `complete` |
| Admin can edit, publish and unpublish health articles (no delete; a new article starts as a draft) | `complete` |
| Each article: condition name, description, causes, symptoms, general care | `complete` |
| Pet Owner can browse and read articles (read-only) | `complete` |
| Search articles by condition name | `complete` |
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
