# DATA_MODELS.md — Hapi Vet Django Models

> This file defines all Django models for Hapi Vet.
> Treat this as the single source of truth for database structure.
> Always update this file BEFORE editing models.py or creating migrations.
> Never modify the database directly — always use Django migrations.

---

## Model Overview

```
accounts/
  └── User

pets/
  ├── PetOwner
  ├── ContactLink
  └── Pet

appointments/
  ├── ClinicSettings
  ├── BlockedDate
  └── Appointment

medical/
  ├── MedicalRecord
  ├── PrescriptionItem
  ├── TestResultFile
  └── Vaccination

billing/
  ├── Service
  ├── BillingReceipt
  └── BillingItem

notifications/
  ├── Notification
  └── NotificationPreference

health/
  └── HealthArticle

dashboard/
  └── (no models — views only, pulls data from other apps)
```

---

## accounts/

### User
Custom user model extending Django's `AbstractUser`.
Must be defined before the first migration.
`AUTH_USER_MODEL = 'accounts.User'`

| Field | Type | Options | Notes |
|---|---|---|---|
| `id` | AutoField | PK | Auto |
| `role` | CharField | max_length=20, choices=ROLE_CHOICES | ADMIN or PET_OWNER |
| `is_onboarded` | BooleanField | default=False | True once pet owner completes onboarding. Always True for Admin |
| `email` | EmailField | unique=True | Used as primary identifier |
| `username` | CharField | inherited | Kept for Django compatibility |
| `date_joined` | DateTimeField | auto | Inherited from AbstractUser |
| `is_active` | BooleanField | default=True | Set to False for archived accounts |

**Role choices:**
```python
ADMIN = 'admin'
PET_OWNER = 'pet_owner'
```

**Rules:**
- Admin account created via Django management command
- Admin is always `is_onboarded = True`
- Pet Owner `is_onboarded` set to `True` after completing onboarding flow
- Every Pet Owner login checks `is_onboarded` — if False, redirect to onboarding
- Archived pet owner accounts have `is_active = False`

---

## pets/

### PetOwner
Stores personal and contact information for registered pet owners.
One-to-one relationship with User.

| Field | Type | Options | Notes |
|---|---|---|---|
| `id` | AutoField | PK | Auto |
| `user` | OneToOneField | User, on_delete=CASCADE | Links to auth account |
| `first_name` | CharField | max_length=50 | — |
| `last_name` | CharField | max_length=50 | — |
| `contact_number` | CharField | max_length=20, blank=True | — |
| `address_line`       | CharField | max_length=255, blank=True | House no., street, subdivision |
| `barangay`           | CharField | max_length=100, blank=True | Barangay |
| `city_municipality`  | CharField | max_length=100, blank=True | City or municipality |
| `province`           | CharField | max_length=100, blank=True | Province |
| `profile_photo` | ImageField | upload_to='owners/', blank=True, null=True | Optional |
| `date_registered` | DateField | auto_now_add=True | — |
| `is_archived` | BooleanField | default=False | Soft delete |
| `archived_at` | DateTimeField | null=True, blank=True | Set when archived |
| `created_at` | DateTimeField | auto_now_add=True | — |
| `updated_at` | DateTimeField | auto_now=True | — |

**Rules:**
- Archived owners have `is_archived = True` and `user.is_active = False`
- Restoring sets both back to False/True respectively
- Address belongs to owner — pets inherit it implicitly
- Profile photo is optional — initials avatar generated in UI if not uploaded
- Full address is generated dynamically via a `full_address` model property

---

### ContactLink
Stores social media and contact links for a pet owner.
Multiple links allowed per owner.

| Field | Type | Options | Notes |
|---|---|---|---|
| `id` | AutoField | PK | Auto |
| `owner` | ForeignKey | PetOwner, on_delete=CASCADE | — |
| `platform` | CharField | max_length=50 | e.g. Facebook, Instagram |
| `url_or_handle` | CharField | max_length=255 | URL or username |
| `created_at` | DateTimeField | auto_now_add=True | — |

---

### Pet
Stores all details for a registered pet.
Each pet belongs to exactly one PetOwner.

| Field | Type | Options | Notes |
|---|---|---|---|
| `id` | AutoField | PK | Auto |
| `owner` | ForeignKey | PetOwner, on_delete=CASCADE | — |
| `name` | CharField | max_length=50 | Required |
| `species` | CharField | max_length=50 | Dropdown + custom input |
| `breed` | CharField | max_length=50, blank=True | Cascading dropdown + custom input |
| `color` | CharField | max_length=50, blank=True | — |
| `gender` | CharField | max_length=10, choices=GENDER_CHOICES | Male / Female / Unknown |
| `date_of_birth` | DateField | null=True, blank=True | Age calculated from this |
| `weight` | DecimalField | max_digits=6, decimal_places=2, null=True | In kg |
| `registration_number` | CharField | max_length=100, blank=True | Government-issued, optional |
| `photo` | ImageField | upload_to='pets/', blank=True, null=True | Optional |
| `is_archived` | BooleanField | default=False | Soft delete after Admin approves deletion request |
| `archived_at` | DateTimeField | null=True, blank=True | — |
| `created_at` | DateTimeField | auto_now_add=True | — |
| `updated_at` | DateTimeField | auto_now=True | — |

**Gender choices:**
```python
MALE = 'male'
FEMALE = 'female'
UNKNOWN = 'unknown'
```

**Rules:**
- Age is calculated dynamically from `date_of_birth` — not stored
- Pet deletion requires Admin approval — soft delete only
- Archived pets hidden from pet owner views but data preserved
- One owner can have unlimited pets

---

### PetDeletionRequest
Tracks pet deletion requests from pet owners pending Admin approval.

| Field | Type | Options | Notes |
|---|---|---|---|
| `id` | AutoField | PK | Auto |
| `pet` | ForeignKey | Pet, on_delete=CASCADE | — |
| `requested_by` | ForeignKey | User, on_delete=CASCADE | Pet owner who requested |
| `reason` | CharField | max_length=50, choices=REASON_CHOICES | Predefined reason |
| `reason_detail` | TextField | blank=True | Used when reason is 'other' |
| `status` | CharField | max_length=20, choices=STATUS_CHOICES | PENDING / APPROVED / REJECTED |
| `reviewed_by` | ForeignKey | User, null=True, on_delete=SET_NULL | Admin who reviewed |
| `reviewed_at` | DateTimeField | null=True, blank=True | — |
| `created_at` | DateTimeField | auto_now_add=True | — |

**Reason choices:**
```python
PASSED_AWAY = 'passed_away'
REHOMED = 'rehomed'
DUPLICATE = 'duplicate'
OTHER = 'other'
```

**Status choices:**
```python
PENDING = 'pending'
APPROVED = 'approved'
REJECTED = 'rejected'
```

**Rules:**
- No deletion allowed — preserved as audit trail
- Admin approves → pet is soft deleted (`is_archived = True`)
- Admin rejects → pet remains active, request closed

## appointments/

### ClinicSettings
Single-row table storing all clinic configuration.
Only one instance exists — Admin edits it through clinic settings page.

| Field | Type | Options | Notes |
|---|---|---|---|
| `id` | AutoField | PK | Auto |
| `clinic_name` | CharField | max_length=100 | — |
| `address` | TextField | — | — |
| `contact_number` | CharField | max_length=20, blank=True | — |
| `email` | EmailField | blank=True | — |
| `logo` | ImageField | upload_to='clinic/', blank=True, null=True | Clinic logo for receipts and UI |
| `opening_time` | TimeField | — | e.g. 08:00 |
| `closing_time` | TimeField | — | e.g. 17:00 |
| `slot_duration_minutes` | IntegerField | default=60 | 30 or 60 minutes |
| `working_days` | JSONField | default=list | List of weekday integers (0=Mon, 6=Sun) |
| `same_day_cutoff_time` | TimeField | — | Same-day booking not allowed after this time |
| `booking_limit_days` | IntegerField | default=30 | How far in advance bookings are allowed |
| `notification_email` | EmailField | blank=True | Admin's preferred notification email |
| `updated_at` | DateTimeField | auto_now=True | — |

**Rules:**
- Only one row ever exists
- Retrieved via `ClinicSettings.objects.first()`
- Created automatically during initial setup

---

### BlockedDate
Dates blocked by Admin (holidays, rest days, etc.)

| Field | Type | Options | Notes |
|---|---|---|---|
| `id` | AutoField | PK | Auto |
| `date` | DateField | unique=True | Blocked date |
| `reason` | CharField | max_length=100, blank=True | Optional note |
| `created_at` | DateTimeField | auto_now_add=True | — |

---

### Appointment
Records all appointment bookings.

| Field | Type | Options | Notes |
|---|---|---|---|
| `id` | AutoField | PK | Auto |
| `owner` | ForeignKey | PetOwner, on_delete=CASCADE | — |
| `pet` | ForeignKey | Pet, on_delete=CASCADE | — |
| `service` | ForeignKey | Service, on_delete=SET_NULL, null=True | Primary service selected |
| `date` | DateField | — | Appointment date |
| `time` | TimeField | — | Appointment time slot |
| `status` | CharField | max_length=20, choices=STATUS_CHOICES | See flow below |
| `reason` | TextField | blank=True | Reason for visit |
| `notes` | TextField | blank=True | Admin notes |
| `is_walk_in` | BooleanField | default=False | True for walk-in appointments |
| `cancellation_reason` | CharField | max_length=50, blank=True, choices=CANCEL_CHOICES | — |
| `cancellation_detail` | TextField | blank=True | Used when reason is 'other' |
| `cancelled_by` | ForeignKey | User, null=True, on_delete=SET_NULL | Who cancelled |
| `reschedule_reason` | TextField | blank=True | Reason for reschedule |
| `rescheduled_from` | ForeignKey | 'self', null=True, on_delete=SET_NULL | Previous appointment if rescheduled |
| `created_at` | DateTimeField | auto_now_add=True | — |
| `updated_at` | DateTimeField | auto_now=True | — |

**Status choices:**
```python
PENDING = 'pending'
CONFIRMED = 'confirmed'
COMPLETED = 'completed'
CANCELLED = 'cancelled'
NO_SHOW = 'no_show'
```

**Status flow:**
```
PENDING → CONFIRMED → COMPLETED
                    → NO_SHOW
       → CANCELLED
```

**Cancellation reason choices:**
```python
CHANGE_OF_PLANS = 'change_of_plans'
PET_UNWELL = 'pet_unwell'
CANNOT_MAKE_TIME = 'cannot_make_time'
OTHER = 'other'
```

**Rules:**
- No two appointments can share the same `date` + `time` combination
- Walk-in appointments created directly as CONFIRMED
- Cancelled slot freed immediately — enforced by checking status on slot query
- Bookings limited to `ClinicSettings.booking_limit_days` in advance
- Same-day bookings only allowed before `ClinicSettings.same_day_cutoff_time`

---

## medical/

### MedicalRecord
Clinical documentation for each pet visit.
Immutable — no deletion allowed.

| Field | Type | Options | Notes |
|---|---|---|---|
| `id` | AutoField | PK | Auto |
| `pet` | ForeignKey | Pet, on_delete=CASCADE | — |
| `appointment` | ForeignKey | Appointment, null=True, blank=True, on_delete=SET_NULL | Null for standalone records |
| `record_date` | DateField | — | Date of record |
| `diagnosis` | TextField | blank=True | — |
| `symptoms` | TextField | blank=True | — |
| `treatment_given` | TextField | blank=True | — |
| `public_notes` | TextField | blank=True | Visible to pet owner |
| `private_notes` | TextField | blank=True | Admin only — strictly hidden |
| `follow_up_required` | BooleanField | default=False | — |
| `follow_up_date` | DateField | null=True, blank=True | — |
| `created_by` | ForeignKey | User, on_delete=SET_NULL, null=True | Admin who created it |
| `created_at` | DateTimeField | auto_now_add=True | — |
| `updated_at` | DateTimeField | auto_now=True | — |

**Rules:**
- Only Admin can create or edit records
- No deletion allowed — enforce at view level
- Pet owners see all fields EXCEPT `private_notes`
- One record per appointment (enforced at view level)

---

### PrescriptionItem
Individual medicine items within a medical record.
Multiple items allowed per record.

| Field | Type | Options | Notes |
|---|---|---|---|
| `id` | AutoField | PK | Auto |
| `record` | ForeignKey | MedicalRecord, on_delete=CASCADE | — |
| `medicine_name` | CharField | max_length=100 | — |
| `dosage` | CharField | max_length=100 | e.g. 500mg |
| `frequency` | CharField | max_length=100 | e.g. Twice a day |
| `duration` | CharField | max_length=100 | e.g. 7 days |
| `notes` | TextField | blank=True | Additional instructions |
| `created_at` | DateTimeField | auto_now_add=True | — |

---

### TestResultFile
File uploads attached to a medical record's test results.
Multiple files allowed per record.

| Field | Type | Options | Notes |
|---|---|---|---|
| `id` | AutoField | PK | Auto |
| `record` | ForeignKey | MedicalRecord, on_delete=CASCADE | — |
| `file` | FileField | upload_to='test_results/' | PDF or image |
| `description` | CharField | max_length=100, blank=True | Optional label |
| `is_archived` | BooleanField | default=False | Soft delete for wrongly uploaded files |
| `archived_at` | DateTimeField | null=True, blank=True | Set when archived |
| `uploaded_at` | DateTimeField | auto_now_add=True | — |

**Rules:**
- Soft delete only — file reference preserved but hidden from views
- Used when a file was uploaded by mistake (wrong file, wrong patient)
- Parent MedicalRecord remains immutable and unaffected

---

### Vaccination
Vaccination records for each pet.
Can be created through an appointment or as a standalone entry.

| Field | Type | Options | Notes |
|---|---|---|---|
| `id` | AutoField | PK | Auto |
| `pet` | ForeignKey | Pet, on_delete=CASCADE | Always required |
| `medical_record` | ForeignKey | MedicalRecord, null=True, blank=True, on_delete=SET_NULL | Null if standalone |
| `appointment` | ForeignKey | Appointment, null=True, blank=True, on_delete=SET_NULL | Null if standalone |
| `vaccine_name` | CharField | max_length=20 | Predefined list + custom input |
| `date_administered` | DateField | — | — |
| `weight_at_vaccination` | DecimalField | max_digits=6, decimal_places=2, null=True | Pet weight at time of vaccination |
| `next_due_date` | DateField | null=True, blank=True | Triggers reminder |
| `batch_number` | CharField | max_length=50, blank=True | Lot number from physical booklet |
| `manufacturer` | CharField | max_length=100, blank=True | — |
| `administered_by` | CharField | max_length=100, blank=True | Veterinarian name |
| `site_of_injection` | CharField | max_length=50, blank=True | — |
| `is_corrected` | BooleanField | default=False | Marks record as corrected |
| `correction_note` | TextField | blank=True | Explains what was corrected and why |
| `created_at` | DateTimeField | auto_now_add=True | — |
| `updated_at` | DateTimeField | auto_now=True | — |

**Predefined vaccine name options:**
```
Deworming
Ivermectin
Anti-rabies
6-in-1
7-in-1
8-in-1
Bordetella
```
Custom input allowed if not in list.

**Rules:**
- If `medical_record` and `appointment` are null — standalone entry
- Both paths update the pet's vaccination history
- `next_due_date` is used to trigger vaccination reminders
- Vaccination records cannot be deleted — ever
- All fields are editable by Admin
- A correction note is required only when clinical fields are changed:
  `vaccine_name`, `custom_vaccine_name`, `date_administered`,
  `next_due_date`, `weight_at_vaccination`, `site_of_injection`
- When a clinical field is changed, `is_corrected` is set to `True` automatically
- Administrative fields (`batch_number`, `manufacturer`, `administered_by`)
  can be edited freely without a correction note
- Corrected records remain visible but are flagged in the UI with the correction note

---

## billing/

### Service
Clinic service catalog managed by Admin.

| Field | Type | Options | Notes |
|---|---|---|---|
| `id` | AutoField | PK | Auto |
| `service_name` | CharField | max_length=100 | — |
| `category` | CharField | max_length=50, blank=True | Optional, Admin-defined |
| `description` | TextField | blank=True | — |
| `pricing_type` | CharField | max_length=10, choices=PRICING_CHOICES | FIXED or RANGE |
| `base_price` | DecimalField | max_digits=10, decimal_places=2 | Used for FIXED pricing |
| `min_price` | DecimalField | max_digits=10, decimal_places=2, null=True | Used for RANGE pricing |
| `max_price` | DecimalField | max_digits=10, decimal_places=2, null=True | Used for RANGE pricing |
| `status` | CharField | max_length=20, choices=STATUS_CHOICES | DRAFT / ACTIVE / UNLISTED / CLOSED |
| `created_at` | DateTimeField | auto_now_add=True | — |
| `updated_at` | DateTimeField | auto_now=True | — |

**Pricing type choices:**
```python
FIXED = 'fixed'
RANGE = 'range'
```

**Status choices:**
```python
DRAFT = 'draft'
ACTIVE = 'active'
UNLISTED = 'unlisted'
CLOSED = 'closed'
```

**Status rules:**
- DRAFT — not visible anywhere
- ACTIVE — visible to pet owners, bookable
- UNLISTED — not visible to pet owners, Admin use only
- CLOSED — visible to pet owners as unavailable, not bookable

---

### BillingReceipt
Header record for each billing transaction.

| Field | Type | Options | Notes |
|---|---|---|---|
| `id` | AutoField | PK | Auto |
| `receipt_number` | CharField | max_length=50, unique=True | Auto-generated e.g. HV-2025-0001 |
| `appointment` | ForeignKey | Appointment, null=True, blank=True, on_delete=SET_NULL | — |
| `owner` | ForeignKey | PetOwner, on_delete=SET_NULL, null=True | — |
| `pet` | ForeignKey | Pet, on_delete=SET_NULL, null=True | — |
| `billing_date` | DateField | auto_now_add=True | — |
| `subtotal` | DecimalField | max_digits=10, decimal_places=2 | Before discount |
| `discount_type` | CharField | max_length=20, choices=DISCOUNT_CHOICES, blank=True | PERCENTAGE or FIXED |
| `discount_value` | DecimalField | max_digits=10, decimal_places=2, default=0 | Amount or percentage |
| `discount_note` | CharField | max_length=100, blank=True | e.g. Loyal client discount |
| `total_amount` | DecimalField | max_digits=10, decimal_places=2 | After discount |
| `payment_method` | CharField | max_length=50, blank=True | e.g. Cash |
| `payment_status` | CharField | max_length=20, choices=STATUS_CHOICES | PENDING / PAID / CANCELLED |
| `payment_date` | DateField | null=True, blank=True | When marked as Paid |
| `created_at` | DateTimeField | auto_now_add=True | — |
| `updated_at` | DateTimeField | auto_now=True | — |

**Payment status choices:**
```python
PENDING = 'pending'
PAID = 'paid'
CANCELLED = 'cancelled'
```

**Discount type choices:**
```python
PERCENTAGE = 'percentage'
FIXED = 'fixed'
```

**Rules:**
- Receipt number auto-generated on creation (format: HV-YYYY-XXXX)
- Editable only while `payment_status` is PENDING
- Locked (read-only) once marked as PAID
- `total_amount` calculated as: subtotal - discount

---

### BillingItem
Individual line items within a billing receipt.
Supports both catalog services and manual items.

| Field | Type | Options | Notes |
|---|---|---|---|
| `id` | AutoField | PK | Auto |
| `receipt` | ForeignKey | BillingReceipt, on_delete=CASCADE | — |
| `service` | ForeignKey | Service, null=True, blank=True, on_delete=SET_NULL | Null if manual item |
| `description` | CharField | max_length=200 | Used for manual items or overrides service name |
| `quantity` | IntegerField | default=1 | — |
| `unit_price` | DecimalField | max_digits=10, decimal_places=2 | Actual billed price set by Admin |
| `subtotal` | DecimalField | max_digits=10, decimal_places=2 | quantity × unit_price |
| `created_at` | DateTimeField | auto_now_add=True | — |

**Rules:**
- If `service` is null — manual item, `description` is required
- If `service` is set — `description` can be auto-filled from service name
- `subtotal` always calculated as `quantity × unit_price`

---

## notifications/

### Notification
Stores all in-app notifications for both Admin and Pet Owner.

| Field | Type | Options | Notes |
|---|---|---|---|
| `id` | AutoField | PK | Auto |
| `recipient` | ForeignKey | User, on_delete=CASCADE | — |
| `notification_type` | CharField | max_length=50, choices=TYPE_CHOICES | See types below |
| `title` | CharField | max_length=100 | Short notification title |
| `message` | TextField | — | Full notification message |
| `is_read` | BooleanField | default=False | — |
| `read_at` | DateTimeField | null=True, blank=True | — |
| `related_appointment` | ForeignKey | Appointment, null=True, blank=True, on_delete=SET_NULL | Optional link |
| `related_pet` | ForeignKey | Pet, null=True, blank=True, on_delete=SET_NULL | Optional link |
| `related_billing` | ForeignKey | BillingReceipt, null=True, blank=True, on_delete=SET_NULL | Optional link |
| `email_sent` | BooleanField | default=False | Whether email was sent |
| `email_failed` | BooleanField | default=False | Whether email failed |
| `email_error` | TextField | blank=True | Error message if failed |
| `created_at` | DateTimeField | auto_now_add=True | — |

**Notification type choices:**
```python
APPOINTMENT_REQUESTED = 'appointment_requested'
APPOINTMENT_APPROVED = 'appointment_approved'
APPOINTMENT_REJECTED = 'appointment_rejected'
APPOINTMENT_REMINDER = 'appointment_reminder'
APPOINTMENT_CANCELLED = 'appointment_cancelled'
BILLING_GENERATED = 'billing_generated'
VACCINATION_REMINDER = 'vaccination_reminder'
FOLLOWUP_REMINDER = 'followup_reminder'
EMAIL_FAILED = 'email_failed'
```

**Rules:**
- In-app notifications always created regardless of email preferences
- `email_sent` and `email_failed` track email delivery status
- Admin warned via EMAIL_FAILED notification type when email fails
- Admin manually clears notifications (all or read only)
- Notifications linked to related objects for easy navigation

---

### NotificationPreference
One row per user storing all notification preferences and timing settings.
Created automatically when a user registers.

| Field | Type | Options | Notes |
|---|---|---|---|
| `id` | AutoField | PK | Auto |
| `user` | OneToOneField | User, on_delete=CASCADE | — |
| `email_appointment_reminders` | BooleanField | default=True | — |
| `email_appointment_status` | BooleanField | default=True | Approved/rejected updates |
| `email_billing` | BooleanField | default=True | — |
| `email_vaccination` | BooleanField | default=True | — |
| `email_followup` | BooleanField | default=True | — |
| `appointment_reminder_days` | IntegerField | default=1 | Days before appointment |
| `vaccination_reminder_days` | IntegerField | default=7 | Days before due date |
| `updated_at` | DateTimeField | auto_now=True | — |

**Rules:**
- Created automatically via Django signal on user registration
- Admin defaults: all toggles True
- Pet Owner defaults: all toggles True, timing defaults as above
- In-app notifications not affected by these toggles — always on

---

## health/

### HealthArticle
Pet health information articles managed by Admin.
Read-only for Pet Owners.

| Field | Type | Options | Notes |
|---|---|---|---|
| `id` | AutoField | PK | Auto |
| `title` | CharField | max_length=100 | Condition name e.g. Bordetella |
| `description` | TextField | — | General overview |
| `causes` | TextField | blank=True | — |
| `symptoms` | TextField | blank=True | — |
| `general_care` | TextField | blank=True | Treatment guidance |
| `is_published` | BooleanField | default=False | Unpublished articles hidden from pet owners |
| `created_by` | ForeignKey | User, on_delete=SET_NULL, null=True | Admin who created it |
| `created_at` | DateTimeField | auto_now_add=True | — |
| `updated_at` | DateTimeField | auto_now=True | — |

**Rules:**
- Only published articles visible to Pet Owners
- Admin can see all articles including unpublished
- Login required to view any article

---

## Relationships Summary

```
User ──────────────── PetOwner (OneToOne)
                         │
                         ├── ContactLink (ForeignKey, many)
                         └── Pet (ForeignKey, many)
                               │
                               ├── Vaccination (ForeignKey, many)
                               ├── MedicalRecord (ForeignKey, many)
                               │     ├── PrescriptionItem (ForeignKey, many)
                               │     ├── TestResultFile (ForeignKey, many)
                               │     └── Vaccination (ForeignKey, optional)
                               └── Appointment (ForeignKey, many)
                                     ├── MedicalRecord (ForeignKey, optional)
                                     ├── Vaccination (ForeignKey, optional)
                                     └── BillingReceipt (ForeignKey, optional)
                                           └── BillingItem (ForeignKey, many)
                                                 └── Service (ForeignKey, optional)

User ──────────────── NotificationPreference (OneToOne)
User ──────────────── Notification (ForeignKey, many)

ClinicSettings (single row)
BlockedDate (many rows)
HealthArticle (many rows)
PetDeletionRequest (ForeignKey to Pet)
```

---

## Django App to Model Mapping

| App | Models |
|---|---|
| `accounts` | User |
| `pets` | PetOwner, ContactLink, Pet, PetDeletionRequest |
| `appointments` | ClinicSettings, BlockedDate, Appointment |
| `medical` | MedicalRecord, PrescriptionItem, TestResultFile, Vaccination |
| `billing` | Service, BillingReceipt, BillingItem |
| `notifications` | Notification, NotificationPreference |
| `health` | HealthArticle |
| `dashboard` | No models — views only, pulls data from other apps |

---

## Deletion & Archive Behavior Summary

| Model | Approach | Mechanism | Reason |
|---|---|---|---|
| User | Soft delete | `is_active = False` | Preserve all linked data |
| PetOwner | Soft delete | `is_archived = True` | Preserve pet, appointment, billing history |
| Pet | Soft delete | `is_archived = True` | Preserve medical and vaccination history |
| MedicalRecord | Immutable | No delete, no archive | Legal and ethical obligation |
| PrescriptionItem | Immutable | Follows MedicalRecord | Part of clinical record |
| TestResultFile | Soft delete | `is_archived = True` | Wrong uploads can be hidden |
| Vaccination | Editable + correction flag | `is_corrected` + `correction_note` | Permanent health history |
| Appointment | Status-based | CANCELLED status | Status tells the full story |
| BillingReceipt | Status-based | CANCELLED status | Financial audit trail |
| BillingItem | No deletion | Locked with receipt | Integrity of billing record |
| Service | Status-based | CLOSED / DRAFT status | History of offered services |
| Notification | True delete | Admin manual clear | No long-term value |
| HealthArticle | Soft delete | `is_published = False` | Draft/unpublish workflow |
| PetDeletionRequest | Immutable | No delete | Audit trail of requests |

---

## Migration Rules

- Never delete migration files
- Never modify an existing migration
- Never edit the database directly
- Always run `makemigrations` and `migrate` after model changes
- Update this file BEFORE editing models.py
- If a change affects features, update FEATURES.md too
