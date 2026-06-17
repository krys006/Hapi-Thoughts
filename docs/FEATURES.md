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
| Notification preferences settings | `planned` |
| Appointment reminder timing preference | `planned` |
| Vaccination reminder timing preference (days in advance) | `planned` |
| Per-type email notification toggles (on/off) | `planned` |

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
| View pet vaccination history | `planned` |
| View pet medical history (read-only, public notes only) | `planned` |

### Pet Profile Fields
| Field | Required | Notes |
|---|---|---|
| Photo | Optional | Initials avatar as placeholder |
| Registration number | Optional | Government-issued |
| Name | Required | — |
| Species | Required | Dropdown + custom input |
| Breed | Required | Cascading dropdown based on species + custom input |
| Color | Optional | — |
| Gender | Required | Male / Female / Unknown |
| Date of birth | Required | — |
| Age | Auto | Calculated from date of birth |
| Weight | Optional | In kg |

### 2.4 Pet & Owner Management (Admin)
| Feature | Status |
|---|---|
| View all registered pet owners | `complete` |
| Search pet owners (global search) | `complete` |
| Filter pet owners | `complete` |
| View individual pet owner profile and their pets | `complete` |
| Create pet owner account manually (for walk-ins) | `complete` |
| Edit pet owner details | `complete` |
| Archive pet owner account (soft delete) | `complete` |
| Restore archived pet owner account | `complete` |
| View all registered pets | `complete` |
| Search and filter pets | `complete` |
| View individual pet profile | `complete` |
| Add a pet manually | `complete` |
| Edit pet details | `complete` |
| Approve pet deletion request | `complete` |
| Reject pet deletion request | `complete.` |

### 2.5 Walk-in Client Flow
| Feature | Status |
|---|---|
| Admin creates pet owner account with minimum details | `complete` |
| System sends claim account email automatically if email provided | `complete` |
| Pet owner receives one-time link to set their own password | `complete` |
| If no email provided — account saved, claim email sent when email is added later | `complete` |
| Admin creates appointment directly as CONFIRMED for walk-in | `complete` |

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
| Cancellation within 24 hours requires Admin approval | `complete` |
| Predefined cancellation reasons (change of plans, pet unwell, cannot make time, other) | `complete` |
| View appointment history and current status | `complete` |

### 3.3 Appointment Management (Admin)
| Feature | Status |
|---|---|
| View all appointments (calendar and list view) | `planned` |
| Calendar monthly and weekly view toggle | `planned` |
| Colored dot indicators on calendar days with appointments | `planned` |
| Click day to view appointment details in side panel | `planned` |
| Approve appointment request (PENDING → CONFIRMED) | `complete` |
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
       → CANCELLED
```

### 3.5 Appointment Rules
| Rule | Detail |
|---|---|
| Booking limit | 30 days in advance |
| Same-day booking | Allowed before configurable cutoff time |
| Slots | One appointment per slot |
| Cancellation deadline | 24 hours — after that requires Admin approval |
| Appointment type | Derived from service selected |
| Additional services | Admin adds extras at billing stage |

---

## Phase 4 — Medical Records & Vaccination

### 4.1 Medical Records (Admin)
| Feature | Status |
|---|---|
| Create medical record (no timing restriction) | `complete` |
| One medical record per appointment | `planned` |
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
| Set service status: Draft / Active / Unlisted / Closed | `complete` |
| Optional service category (Admin-defined freely) | `complete` |

### Service Status Behavior
| Status | Visible to Pet Owner | Bookable |
|---|---|---|
| **Draft** | No | No |
| **Active** | Yes | Yes |
| **Unlisted** | No | Admin use only |
| **Closed** | Yes (shown as unavailable) | No |

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
| Set payment method (cash, etc.) | `complete` |
| Set payment status: Pending / Paid / Cancelled | `complete` |
| Edit receipt while status is Pending | `complete` |
| Receipt locked once marked as Paid | `complete` |
| View all billing transactions | `complete` |
| View receipt detail | `complete` |

### 5.3 Billing (Pet Owner)
| Feature | Status |
|---|---|
| View billing receipt for completed appointments | `complete` |
| View full billing history | `complete` |
| Download receipt as PDF | `complete` |
| Print receipt | `complete` |
| Receive notification when billing is generated | `complete` |

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
| Red dot on bell icon when unread notifications exist | `planned` |
| Red dot disappears when all notifications are read | `planned` |
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
| Email sent on billing generated (to Pet Owner) | `complete` |
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
| Pet Owner per-type email toggle (billing notifications on/off) | `complete` |
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
| Appointment approved | Pet Owner |
| Appointment rejected | Pet Owner |
| Appointment reminder (configurable + day-of) | Pet Owner |
| Billing generated | Pet Owner |
| Vaccination due (configurable + day-of) | Pet Owner |
| Follow-up due (3 days before + day-of) | Pet Owner |

---

## Phase 7 — Dashboards & Polish

### 7.1 Admin Dashboard
| Feature | Status |
|---|---|
| Summary stats: total appointments today | `planned` |
| Summary stats: pending appointment approvals | `planned` |
| Summary stats: total registered pet owners | `planned` |
| Summary stats: total registered pets | `planned` |
| Summary stats: unpaid bills | `planned` |
| Summary stats: upcoming vaccinations due this week | `planned` |
| Summary stats: upcoming follow-ups due this week | `planned` |
| Calendar view — monthly and weekly toggle | `planned` |
| Calendar dot indicators color-coded by appointment status | `planned` |
| Click calendar day to view appointment details in side panel | `planned` |
| Pending appointment approvals widget | `planned` |
| Recent notifications widget | `planned` |
| HTMX polling for live data updates (10–30 sec) | `planned` |
| Global search across pet owners, pets, appointments, medical records | `planned` |
| Search results grouped by type | `planned` |

### 7.2 Pet Owner Dashboard
| Feature | Status |
|---|---|
| Upcoming appointments section | `planned` |
| My pets summary | `planned` |
| Recent notifications | `planned` |
| Quick book appointment button | `planned` |
| Health Library quick access card | `planned` |
| Clinic info quick access | `planned` |
| Scoped search (own data only) | `planned` |

### 7.3 Clinic Info & About Page
| Feature | Status |
|---|---|
| Dr. Edgar about page (name, license number, bio) | `planned` |
| Clinic name, address, contact numbers | `planned` |
| Clinic operating hours | `planned` |
| Google Maps embed of clinic location | `planned` |
| Book an appointment button on clinic info page | `planned` |
| Visible to both Admin and Pet Owner | `planned` |

### 7.4 Clinic Settings (Admin)
| Feature | Status |
|---|---|
| Edit clinic name, address, contact number, email | `planned` |
| Upload clinic logo | `planned` |
| Edit operating hours and working days | `planned` |
| Edit appointment slot duration | `planned` |
| Set same-day booking cutoff time | `planned` |
| Set admin notification email | `planned` |
| Edit veterinarian profile (Dr. Edgar's details and bio) | `planned` |

### 7.5 Health Library
| Feature | Status |
|---|---|
| Admin can create health articles | `planned` |
| Admin can edit and delete health articles | `planned` |
| Each article: condition name, description, causes, symptoms, general care | `planned` |
| Pet Owner can browse and read articles (read-only) | `planned` |
| Search articles by condition name | `planned` |
| Visible to both Admin and Pet Owner (login required) | `planned` |
| Health Library accessible from Pet Owner dashboard | `planned` |

### 7.6 General UI/UX
| Feature | Status |
|---|---|
| Sidebar-based dashboard layout (Admin and Pet Owner) | `planned` |
| Modal-based forms and confirmations | `planned` |
| Mobile responsive — Pet Owner views prioritized | `planned` |
| Confirmation dialogs for destructive actions | `planned` |
| Success / error flash messages | `planned` |
| Simple empty states with text and action button | `planned` |
| Loading states for HTMX requests | `planned` |
| Cascading species/breed dropdown with custom input option | `planned` |
| Color-coded appointment status badges | `planned` |

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
| Auto-delete notifications | Can add scheduler when hosting plan allows |
| Public health library | Remove login requirement to make articles publicly accessible |
