# CONVENTIONS.md — Hapi Vet Code Conventions

> This file defines all coding conventions for Hapi Vet.
> Follow these rules consistently across all development.
> Update this file if conventions change mid-development.
> Krys is the only developer (backend and frontend). These rules apply to
> all work, including Claude Code sessions.

---

## 1. Repository & Project Structure

```
Hapi-Thoughts/                  ← GitHub repo root
├── CLAUDE.md                   ← Claude Code rules (read first)
├── .claude/                    ← Claude Code settings (settings.json permissions)
├── backend/                    ← entire Django project lives here
│   ├── hapivet/                ← Django project config
│   │   ├── settings.py
│   │   ├── urls.py
│   │   ├── wsgi.py
│   │   └── asgi.py
│   ├── accounts/               ← user auth and onboarding
│   ├── pets/                   ← pet owners and pets
│   ├── appointments/           ← scheduling and booking
│   ├── medical/                ← medical records and vaccinations
│   ├── billing/                ← services, receipts, billing
│   ├── notifications/          ← in-app and email notifications
│   ├── health/                 ← health library articles
│   ├── dashboard/              ← admin and pet owner dashboard views
│   ├── templates/              ← all HTML templates (centralized)
│   ├── static/                 ← all static files (centralized)
│   ├── assets/tailwind/input.css ← Tailwind source for static/css/app.css (outside static/ on purpose)
│   ├── bin/                    ← Tailwind CLI tailwindcss.exe (gitignored, never commit)
│   ├── media/                  ← uploaded files (runtime, never commit)
│   ├── venv/                   ← virtual environment (never commit)
│   ├── .env                    ← environment variables (never commit)
│   ├── .env.example            ← example env file (safe to commit)
│   ├── .flake8                 ← Flake8 config (run Flake8 from backend/)
│   ├── requirements.txt        ← Python dependencies
│   └── manage.py
├── docs/                       ← all project documentation
│   ├── BRIEF.md
│   ├── CONVENTIONS.md
│   ├── DATA_MODELS.md
│   ├── FEATURES.md
│   ├── GOOGLE_AUTH_NOTES.md
│   ├── HANDOFF.md
│   ├── STACK.md
│   ├── SUPABASE_MIGRATION_NOTES.md
│   └── SYSTEM_INSTRUCTIONS.md
└── mobile/                     ← placeholder, unused in v1
```

> The repo is named `Hapi-Thoughts` — this is intentional and does not
> affect the Django project name (`hapivet`) or any code.
> Repo name and Django project name are completely separate.

> All Django commands are run from inside `backend/`:
> ```powershell
> cd backend
> venv\Scripts\Activate.ps1
> python manage.py runserver
> ```

> On Render, set the root directory to `backend/` in deployment settings.

---

## 2. Django App Structure

All apps live inside `backend/`. Each app follows this consistent internal structure.

> **Special case — `dashboard` app:**
> The dashboard app has no models of its own.
> It only contains views that pull and aggregate data from other apps.
> It handles the admin dashboard home and pet owner dashboard home exclusively.

```
app_name/
├── migrations/
├── templates/          ← NOT used (all templates centralized)
├── __init__.py
├── admin.py            ← Minimal — we use custom dashboard
├── apps.py
├── forms.py            ← All forms for this app
├── models.py           ← All models for this app
├── urls.py             ← URL patterns for this app
├── views.py            ← All views for this app
├── utils.py            ← Helper functions (if needed)
├── signals.py          ← Django signals (if needed)
└── tests.py
```

If `views.py` or `forms.py` grows too large, split into a folder:
```
views/
  ├── __init__.py
  ├── admin_views.py
  └── owner_views.py
```

---

## 3. Template Structure

All templates live in the top-level `backend/templates/` folder.
Never use per-app template folders.

- `base_dashboard.html` is the base for every logged-in dashboard page
  (header, bell, sidebar, flash messages).
- `layouts/admin.html` is the base for admin pages migrated to the new UI
  (see §10).
- `layouts/auth.html` is the base for the admin login: a standalone card
  with no sidebar (see §10).
- `base.html` is the base for standalone auth pages (login, register,
  password reset, email verification, onboarding).
- HTMX partials are prefixed with `_` and never extend a base template.
- `components/` holds include-only building blocks for the new admin UI
  (see §11). They have no `_` prefix (that prefix means "HTMX partial")
  and never extend a base template.
- Owner emails are plain-text templates in `owner/emails/*.txt`.

```
templates/
├── base.html                         ← Auth/standalone page base
├── base_dashboard.html               ← Dashboard base (admin + owner)
├── components/                       ← Include-only UI building blocks (§11)
│   ├── icon.html
│   ├── pet_avatar.html
│   ├── owner_avatar.html
│   ├── form_field.html
│   ├── status_badge.html
│   ├── flash_messages.html
│   └── empty_state.html
├── layouts/                          ← New bases (§10, §11)
│   ├── admin.html
│   ├── admin_sidebar.html
│   ├── admin_nav_link.html
│   ├── auth.html
│   └── bell_count.html
├── shared/                           ← Used by both roles
│   ├── clinic_info.html
│   ├── modals.html
│   ├── sidebar_admin.html
│   ├── sidebar_owner.html
│   ├── _global_search_results.html
│   ├── _owner_search_results.html
│   ├── billing/
│   │   └── _receipt_print.html
│   └── notifications/
│       ├── _panel.html
│       └── _notification_row.html
├── admin/
│   ├── login.html
│   ├── walkin_create.html
│   ├── dashboard/
│   │   ├── index.html
│   │   ├── _stats.html
│   │   ├── _day_schedule.html
│   │   └── _mini_calendar.html
│   ├── notifications/
│   │   ├── _panel.html
│   │   └── _notification_row.html
│   ├── appointments/
│   │   ├── list.html
│   │   ├── detail.html
│   │   ├── walkin_form.html
│   │   ├── _appointment_list_partial.html
│   │   ├── _calendar_grid.html
│   │   ├── _calendar_day_panel.html
│   │   ├── _pet_options.html
│   │   └── _slot_options.html
│   ├── pets/
│   │   ├── owners_list.html
│   │   ├── owner_detail.html
│   │   ├── owner_form.html
│   │   ├── owner_add_email.html
│   │   ├── pets_list.html
│   │   ├── pet_detail.html
│   │   ├── pet_form.html
│   │   └── deletion_requests.html
│   ├── medical/
│   │   ├── record_list.html
│   │   ├── record_detail.html
│   │   ├── record_form.html
│   │   ├── pet_history.html
│   │   ├── vaccination_list.html
│   │   ├── vaccination_form.html
│   │   ├── vaccination_edit.html
│   │   └── vaccination_history.html
│   ├── billing/
│   │   ├── receipt_list.html
│   │   ├── receipt_detail.html
│   │   ├── receipt_form.html
│   │   ├── services_list.html
│   │   └── service_form.html
│   ├── health/
│   │   ├── article_list.html
│   │   └── article_form.html
│   └── settings/
│       ├── clinic_settings.html
│       └── _blocked_dates_list.html
└── owner/
    ├── login.html
    ├── register.html
    ├── register_pending.html
    ├── verify_email_invalid.html
    ├── forgot_password.html
    ├── forgot_password_sent.html
    ├── reset_password.html
    ├── reset_password_complete.html
    ├── onboarding.html
    ├── onboarding/
    │   ├── step1.html
    │   └── step2.html
    ├── emails/
    │   ├── claim_account_subject.txt
    │   ├── claim_account_body.txt
    │   ├── password_reset_subject.txt
    │   └── password_reset_email.txt
    ├── dashboard/
    │   ├── index.html
    │   └── _dashboard_partial.html
    ├── appointments/
    │   ├── list.html
    │   ├── book.html
    │   ├── detail.html
    │   └── _slot_options.html
    ├── pets/
    │   ├── list.html
    │   ├── detail.html
    │   ├── add.html
    │   ├── edit.html
    │   └── delete_request.html
    ├── medical/
    │   ├── history.html
    │   └── vaccination_history.html
    ├── billing/
    │   ├── list.html
    │   └── receipt_detail.html
    ├── health/
    │   ├── article_list.html
    │   └── article_detail.html
    └── profile/
        ├── index.html
        ├── edit.html
        └── notification_preferences.html
```

---

## 4. Static Files Structure

All static source files live in `backend/static/` (`STATICFILES_DIRS`).
Per-page CSS and JS files are named after the page they serve
(e.g. `css/admin/receipt_detail.css`).

```
static/
├── css/
│   ├── app.css             ← Compiled Tailwind for new admin pages (generated, committed, never edit by hand)
│   ├── admin/              ← Admin page styles
│   ├── owner/              ← Pet owner page styles
│   └── shared/             ← Styles used by both roles (header, clinic info)
├── js/
│   ├── admin/              ← Admin page scripts
│   ├── owner/              ← Pet owner page scripts
│   └── shared/             ← Scripts used by both roles
├── vendor/
│   └── htmx-2.0.11.min.js  ← Pinned HTMX for new admin pages
└── images/
    ├── clinic/             ← Clinic logo and branding
    └── defaults/           ← Default avatars and placeholders
```

> `backend/staticfiles/` is `collectstatic` output (`STATIC_ROOT`) —
> never commit it.

### Media files (user uploads)
Upload paths come from the `upload_to` values in the models:
```
clinic/                     ← ClinicSettings.logo (appointments/models.py)
owners/                     ← PetOwner.profile_photo (pets/models.py)
pets/                       ← Pet.photo (pets/models.py)
test_results/               ← TestResultFile.file (medical/models.py)
```

> Live (Render) runs with `USE_S3=True`, so these are folders in the
> Supabase Storage bucket. Local dev runs with `USE_S3=False`, so they go
> under `backend/media/`, which is generated at runtime — never commit it.

---

## 5. URL Structure

### Every app is included at `""`
`hapivet/urls.py` includes every app at the empty prefix. Each app's
`urls.py` writes the **full path**, including the role prefix. There are no
URL namespaces (`app_name`).

```python
# hapivet/urls.py (abridged)
urlpatterns = [
    path("", include("health.urls")),
    path("", include("notifications.urls")),
    path("", RedirectView.as_view(url="/login/"), name="home"),
    path("", include("dashboard.urls")),
    path("", include("accounts.urls")),
    path("", include("pets.urls")),
    path("", include("appointments.urls")),
    path("", include("medical.urls")),
    path("", include("billing.urls")),
    path("accounts/", include("allauth.urls")),
    # password reset: forgot-password/, reset-password/... (Django auth views)
]
```

### Path layout
```
/                               ← Redirects to /login/
/login/  /register/             ← Pet Owner auth (accounts)
/admin-login/                   ← Admin login (accounts)
/logout/                        ← Shared logout (accounts)
/clinic-info/                   ← Shared clinic info page (appointments)
/forgot-password/  /reset-password/...  ← Password reset (hapivet/urls.py)
/accounts/...                   ← allauth (Google OAuth)

/owner/dashboard/               ← dashboard
/owner/onboarding/step1/        ← pets
/owner/profile/                 ← pets
/owner/profile/notifications/   ← notifications (preferences)
/owner/pets/                    ← pets
/owner/pets/<pk>/medical/       ← medical
/owner/appointments/            ← appointments
/owner/billing/                 ← billing
/owner/health/                  ← health

/admin/dashboard/               ← dashboard
/admin/search/                  ← dashboard (global search)
/admin/walkin/create/           ← accounts (walk-in owner + pet)
/admin/appointments/            ← appointments
/admin/settings/                ← appointments (clinic settings)
/admin/pets/  /admin/pets/owners/  ← pets
/admin/medical/  /admin/vaccinations/  ← medical
/admin/billing/  /admin/services/  ← billing
/admin/health/                  ← health

/notifications/...              ← Shared by both roles (not per-role)
```

> The custom `/admin/...` paths are the app's own admin pages. Django's
> built-in `/admin` site is not used.

### URL naming convention
URL names are flat `snake_case` with a role prefix: `admin_`, `owner_`, or
none for shared routes. They are **not** `role:app_action` namespaces.

```python
# accounts/urls.py
path("login/", views.owner_login, name="owner_login"),
path("logout/", views.user_logout, name="logout"),

# appointments/urls.py — owner
path("owner/appointments/book/", views.owner_book_appointment, name="owner_book_appointment"),

# appointments/urls.py — admin
path("admin/appointments/", views.admin_appointment_list, name="admin_appointments"),
path("admin/appointments/<int:pk>/approve/", views.admin_appointment_approve, name="admin_appointment_approve"),

# notifications/urls.py — shared (no role prefix)
path("notifications/panel/", views.notification_panel, name="notifications_panel"),
```

---

## 6. Naming Conventions

### Python / Django
| Item | Convention | Example |
|---|---|---|
| Files | snake_case | `pet_forms.py`, `appointment_utils.py` |
| Variables | snake_case | `pet_owner`, `appointment_date` |
| Functions | snake_case | `get_available_slots()`, `send_reminder_email()` |
| Classes | PascalCase | `PetOwner`, `AppointmentForm` |
| Constants | UPPER_SNAKE_CASE | `MAX_BOOKING_DAYS`, `DEFAULT_SLOT_DURATION` |
| Model fields | snake_case | `date_of_birth`, `is_archived` |
| URL names | snake_case with role prefix | `owner_appointments`, `admin_appointment_approve` |
| Template names | snake_case | `appointment_list.html`, `pet_detail.html` |

### HTML / Templates
| Item | Convention | Example |
|---|---|---|
| Template variables | snake_case | `{{ pet_owner.first_name }}` |
| Block names | snake_case | `{% block extra_css %}` |
| Template tags | snake_case | `{% url 'owner_appointments' %}` |
| CSS classes | Tailwind utility classes. Migrated admin pages also use the shared classes in `assets/tailwind/input.css` (§11); other pages still use per-page CSS files in `static/css/admin\|owner\|shared/` | `class="btn btn-primary"`, `class="flex items-center gap-4"` |
| Custom CSS classes | kebab-case | `class="appointment-card"` |
| HTMX attributes | standard htmx attributes | `hx-get`, `hx-target`, `hx-trigger` |

---

## 7. View Conventions

All views are **function-based**. No class-based views.

**Exception:** Framework-provided auth views (Django's built-in auth views,
the `HapiVetPasswordResetView` subclass, and the root `RedirectView` in
`hapivet/urls.py`) are the only allowed class-based views.

### Standard view structure
```python
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from .models import Appointment

@login_required
def appointment_list(request):
    # 1. Permission check
    if request.user.role != 'pet_owner':
        return redirect('admin_dashboard')

    # 2. Query data
    appointments = Appointment.objects.filter(
        owner=request.user.petowner
    ).order_by('-date')

    # 3. Handle POST
    if request.method == 'POST':
        # form handling logic
        pass

    # 4. Return response
    return render(request, 'owner/appointments/list.html', {
        'appointments': appointments,
    })
```

### HTMX views (partial responses)
HTMX views return partial HTML, not full pages.
Use a naming convention to distinguish them:

```python
def appointment_list_partial(request):
    """Returns partial HTML for HTMX polling updates."""
    appointments = Appointment.objects.filter(...)
    return render(request, 'owner/appointments/_list_partial.html', {
        'appointments': appointments,
    })
```

Partial templates are prefixed with underscore: `_list_partial.html`

### Role protection
Always check role at the top of every view:

```python
@login_required
def admin_only_view(request):
    if request.user.role != 'admin':
        return redirect('owner_dashboard')
    # rest of view
```

---

## 8. Model Conventions

### Field ordering in models
Always define fields in this order:
1. Primary key (if custom)
2. ForeignKey / OneToOneField relationships
3. Core data fields
4. Status / boolean flags
5. Timestamps (`created_at`, `updated_at`)

### Standard model template
```python
from django.db import models

class Appointment(models.Model):

    # 1. Status choices defined at top of class
    PENDING = 'pending'
    CONFIRMED = 'confirmed'
    COMPLETED = 'completed'
    CANCELLED = 'cancelled'
    NO_SHOW = 'no_show'
    STATUS_CHOICES = [
        (PENDING, 'Pending'),
        (CONFIRMED, 'Confirmed'),
        (COMPLETED, 'Completed'),
        (CANCELLED, 'Cancelled'),
        (NO_SHOW, 'No Show'),
    ]

    # 2. Relationships
    owner = models.ForeignKey('pets.PetOwner', on_delete=models.CASCADE)
    pet = models.ForeignKey('pets.Pet', on_delete=models.CASCADE)
    service = models.ForeignKey('billing.Service', on_delete=models.SET_NULL, null=True)

    # 3. Core fields
    date = models.DateField()
    time = models.TimeField()
    reason = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=PENDING)

    # 4. Flags
    is_walk_in = models.BooleanField(default=False)

    # 5. Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # 6. Meta
    class Meta:
        ordering = ['-date', '-time']

    # 7. String representation
    def __str__(self):
        return f"{self.pet.name} — {self.date} {self.time}"

    # 8. Properties / helper methods
    @property
    def is_upcoming(self):
        from django.utils import timezone
        return self.date >= timezone.now().date()
```

---

## 9. Form Conventions

All forms live in `forms.py` within their app.
Use Django ModelForms wherever possible.

```python
from django import forms
from .models import Appointment

class AppointmentBookingForm(forms.ModelForm):
    class Meta:
        model = Appointment
        fields = ['pet', 'date', 'time', 'service', 'reason']
        widgets = {
            'date': forms.DateInput(attrs={'type': 'date'}),
            'time': forms.TimeInput(attrs={'type': 'time'}),
            'reason': forms.Textarea(attrs={'rows': 3}),
        }

    def clean(self):
        cleaned_data = super().clean()
        date = cleaned_data.get('date')
        time = cleaned_data.get('time')
        # validation logic here
        return cleaned_data
```

---

## 10. Template Conventions

### Four base templates
| Base | Used by | What it provides |
|---|---|---|
| `layouts/admin.html` | Admin pages migrated to the new UI (e.g. `admin/dashboard/index.html`) | Sidebar with count badges and logout, top bar with search ("Find a pet or owner") and Notifications, flash messages, compiled `app.css`, vendored HTMX, `static/js/admin/dialogs.js`, CSRF + `X-Admin-Layout` via `hx-headers`, confirm modal |
| `layouts/auth.html` | `admin/login.html` | Admin login: standalone card on a violet background, `app.css`, no HTMX |
| `base_dashboard.html` | Logged-in pages not migrated yet, admin and owner (e.g. `admin/billing/receipt_detail.html`, `owner/pets/list.html`, `shared/clinic_info.html`) | Header with search and bell, role-based sidebar, flash messages, Tailwind + HTMX, CSRF header for HTMX, confirm modal |
| `base.html` | Standalone auth pages (e.g. `owner/login.html`, `owner/register.html`, password reset, `owner/onboarding/step1.html`) | Bare page with Tailwind + HTMX, no header or sidebar |

Migrated pages never loop over `messages` (the layout includes
`components/flash_messages.html`) and load no `static/css/admin/` file.

### Blocks each base defines
```html
<!-- layouts/admin.html -->
{% block title %}Hapi Vet Admin{% endblock %}
{% block extra_head %}{% endblock %}  ← extra tags in <head>
{% block topbar_search %}…{% endblock %} ← global search in the top bar; a page
                                         with its own search empties it
{% block page_header %}               ← default header, wraps the three below
  {% block page_title %}{% endblock %}
  {% block page_subtitle %}{% endblock %}
  {% block page_actions %}{% endblock %}
{% endblock %}
{% block content %}{% endblock %}     ← page body, inside <main>
{% block extra_js %}{% endblock %}    ← page JS, end of <body>; the receipt
                                         print partial goes here (it must be
                                         a direct child of <body>)

<!-- layouts/auth.html -->
{% block title %}Hapi Vet{% endblock %}
{% block content %}{% endblock %}     ← the card, centred inside <main>
{% block extra_js %}{% endblock %}    ← page JS, end of <body>

<!-- base_dashboard.html -->
{% block title %}Hapi Vet{% endblock %}
{% block extra_css %}{% endblock %}   ← page CSS link, in <head>
{% block content %}{% endblock %}     ← page body, inside <main>
{% block extra_js %}{% endblock %}    ← page JS, end of <body>

<!-- base.html -->
{% block title %}Hapi Vet{% endblock %}
{% block tailwind %}…{% endblock %}   ← Tailwind CDN script (overridable)
{% block htmx %}…{% endblock %}       ← HTMX script (overridable)
{% block preload_assets %}{% endblock %}
{% block extra_css %}{% endblock %}
{% block content %}{% endblock %}
{% block extra_js %}{% endblock %}
```

### Dialogs (admin layout)
`layouts/admin.html` loads `static/js/admin/dialogs.js` (deferred) on every
page. Use a native `<dialog>` with data attributes, no inline handlers:
```html
<button type="button" class="btn btn-outline" data-dialog-open="move-dialog">Move</button>
<dialog id="move-dialog">
  …<input name="date" autofocus>…
  <button type="button" class="btn btn-outline" data-dialog-close>Keep it</button>
</dialog>
<dialog id="decline-dialog" data-open-on-load>…</dialog>  ← opens on page load (e.g. form errors)
```
- `data-dialog-open="<id>"` opens that dialog with `showModal()`.
- `data-dialog-close` inside a dialog closes it; so does a click on the
  backdrop and the Escape key.
- `data-open-on-load` opens the dialog when the page loads, or when an HTMX
  swap brings it in (once per dialog).
- Focus goes to the element with `autofocus`.
- Clicks are handled once on `document`, so dialogs inside HTMX partials
  need no extra wiring.

### Example dashboard page
From `admin/billing/receipt_detail.html`:
```html
{% extends 'base_dashboard.html' %}

{% load static %}
{% block extra_css %}
  <link rel="stylesheet" href="{% static 'css/admin/receipt_detail.css' %}">
{% endblock %}

{% block title %}{{ receipt.receipt_number }} — Hapi Vet Admin{% endblock %}

{% block content %}
  <!-- page content here -->
{% endblock content %}

{% block extra_js %}
  <!-- page scripts here -->
{% endblock extra_js %}
```

### HTMX partial templates
Partials are prefixed with underscore and never extend a base template.
Real examples:
```
admin/appointments/_calendar_grid.html      ← calendar month grid
admin/appointments/_calendar_day_panel.html ← calendar day side panel
admin/dashboard/_stats.html                 ← self-polling dashboard stats
shared/notifications/_panel.html            ← bell dropdown
```

### Template variable naming
Always use descriptive context variable names:
```python
# Good
return render(request, 'template.html', {
    'appointments': appointments,
    'pending_count': pending_count,
    'selected_pet': pet,
})

# Avoid
return render(request, 'template.html', {
    'data': appointments,
    'count': pending_count,
    'obj': pet,
})
```

---

## 11. Tailwind CSS Conventions

### Two setups during the admin UI overhaul
| Pages | Tailwind | Loaded by |
|---|---|---|
| Admin pages migrated to the new layout (from Slice 1) and the admin login | Compiled `static/css/app.css` (Tailwind v4.3.3, standalone CLI) | `layouts/admin.html`, `layouts/auth.html` |
| Everything else (owner pages, auth pages, admin pages not migrated yet) | Play CDN `https://cdn.tailwindcss.com` | `base.html`, `base_dashboard.html` |

- Never load the CDN script in the new layout, and never load `app.css` from the old bases.
- A migrated page loads no file from `static/css/admin/`.

### Building app.css
The source is `backend/assets/tailwind/input.css`. It lives outside `static/`
on purpose: collectstatic's manifest storage would fail on its
`@import "tailwindcss"`. The CLI binary is `backend/bin/tailwindcss.exe`
(gitignored, about 110 MB): download `tailwindcss-windows-x64.exe` v4.3.3 from
the Tailwind GitHub releases and rename it.

```powershell
# From the repo root. Run before every frontend commit:
backend\bin\tailwindcss.exe -i backend\assets\tailwind\input.css -o backend\static\css\app.css --minify
# While editing templates:
backend\bin\tailwindcss.exe -i backend\assets\tailwind\input.css -o backend\static\css\app.css --watch
```

- `app.css` is committed, so Render needs no Node or Tailwind build. Never edit it by hand.
- The CLI scans only `backend/templates/` and `backend/static/js/`. Write class
  names as whole strings there (no building names like `"bg-" + colour`).
- Shared classes live in `input.css`: `.btn`, `.btn-primary`, `.btn-outline`,
  `.btn-danger`, `.input`, `.card`, `.badge`, `.form-field`. Everything else
  uses utility classes in the template.

### Class ordering convention
Follow this order for Tailwind classes:
```
Layout → Sizing → Spacing → Typography → Colors → Borders → Effects
```

Example:
```html
<div class="flex items-center w-full px-4 py-2 text-base font-medium text-gray-700 bg-white border border-gray-200 rounded-lg shadow-sm hover:bg-gray-50">
```

### Colours
- Primary actions, active navigation, brand accents: `violet-600` (hover `violet-700`); light tints `violet-50` / `violet-100`
- Links: `blue-600`
- Danger: `red-600`; success: `green-*`; warning: `yellow-*`
- Neutral: `gray-*`. Never `gray-400` or lighter for text that carries information.
- Pages not migrated yet keep their per-page colours.

### Senior-friendly rules (admin)
The main admin user is an elderly vet. Every migrated admin page follows these:
- Content text 16px (`text-base`) minimum; secondary text 14px (`text-sm`) minimum; never `text-xs`.
- Click targets at least 44px tall (`min-h-11`; `.btn` already does this).
- Every icon has a visible text label (icons are `aria-hidden`).
- Times read "Today, 3:58 PM" or "Mon, Nov 2", not "12 min ago".
- Risky actions (cancel, decline, mark paid) ask for confirmation in plain words.
- Flash messages stay until closed.
- One primary (violet) button per page, top right.
- Test every slice at 150% browser zoom.

### Components (`templates/components/`)
| Component | Usage |
|---|---|
| `icon.html` | `{% include "components/icon.html" with name="calendar" class="size-5 shrink-0" %}`. Optional `stroke="1.5"` (default 2). Lucide 1.66.0 (ISC), 44 icons. To add one, copy its shapes from the official `lucide-static@1.66.0` SVG file (never from memory). |
| `pet_avatar.html` | `{% include "components/pet_avatar.html" with pet=appointment.pet size="lg" %}`. `lg` 64px (`size-16 rounded-2xl`), `sm` 40px (`size-10 rounded-xl`). The pet's photo, or a violet tile with the `species_icon` icon (dog, cat, rabbit, bird, paw-print) at stroke 1.5, labelled "No photo of {name} yet". |
| `owner_avatar.html` | `{% include "components/owner_avatar.html" with owner=appointment.owner size="xs" %}`. Circle; `sm` 40px, `xs` 32px. The profile photo, or initials (one letter when the last name is blank). Old owner templates still draw initials inline until Slice 4. |
| `form_field.html` | `{% include "components/form_field.html" with field=form.email %}`. Label, control, help text, errors. Needs a fieldset/legend branch before a RadioSelect or CheckboxSelectMultiple field uses it. |
| `status_badge.html` | `{% include "components/status_badge.html" with status=obj.status label=obj.get_status_display %}` |
| `flash_messages.html` | Rendered once by the admin layout; pages must not loop over `messages`. |
| `empty_state.html` | `{% include "components/empty_state.html" with icon="calendar" title="No appointments on this day" text="Pick another date on the calendar." %}` |

### Status badge convention
Use `components/status_badge.html`. Colours: pending yellow, confirmed blue,
completed and paid green, cancelled red, no show gray, as `bg-*-100 text-*-800`
in `text-sm`. Pages not migrated yet may still show the older hand-written badges.

---

## 12. Code Style — Black + Flake8

### What they do
- **Black** — auto-formats Python code consistently on save
- **Flake8** — checks for errors, bad practices, and style issues

### Installation
```bash
pip install black flake8
```

Add to `requirements.txt`:
```
black
flake8
```

### VS Code setup
Add to `.vscode/settings.json`:
```json
{
  "editor.formatOnSave": true,
  "editor.defaultFormatter": "ms-python.black-formatter",
  "python.linting.enabled": true,
  "python.linting.flake8Enabled": true,
  "python.linting.flake8Args": ["--max-line-length=88"],
  "[python]": {
    "editor.defaultFormatter": "ms-python.black-formatter"
  }
}
```

### Flake8 config
The config lives in `backend/.flake8`, and Flake8 is run from `backend/`:
```ini
[flake8]
max-line-length = 88
exclude =
    migrations,
    venv,
    staticfiles,
    media,
    .git,
    __pycache__
ignore = E203, W503
```

```bash
cd backend
venv\Scripts\flake8.exe
```

### Rules
- Black formats automatically on save — never manually adjust spacing
- Fix all Flake8 warnings before committing
- Never commit code with linting errors
- Keep the same VS Code settings on every machine you develop on

---

## 13. Git Conventions

### Branches
```
master            ← protected stable
rebuild           ← auto-deploys to Render; Render's build runs migrate
                    against the live Supabase DB (build command unverified)
trial-run         ← buffer: work is PR'd here and tested before rebuild
feature/<name>    ← new feature, cut from trial-run
fix/<name>        ← bug fix, cut from trial-run
style/<name>      ← styling-only change, cut from trial-run
docs/<name>       ← documentation-only change, cut from trial-run
```

Flow: branch from `trial-run` → PR into `trial-run` → test → PR
`trial-run` into `rebuild` (deploys).

### Commit message format
```
type: short description

Examples:
feat: add appointment booking form
fix: correct slot conflict validation
model: add is_corrected field to Vaccination
style: format views.py with black
docs: update DATA_MODELS.md with TestResultFile changes
```

### Commit types
| Type | When to use |
|---|---|
| `feat` | New feature added |
| `fix` | Bug fixed |
| `model` | Model or migration change |
| `style` | Code formatting only |
| `docs` | Documentation update |
| `refactor` | Code restructured, no behavior change |
| `test` | Tests added or updated |

### Rules
- Never commit directly to `master` or `rebuild`
- Always work on a feature/fix/style/docs branch cut from `trial-run`
- Update relevant md files in the same commit as the code change
- Never commit `backend/.env`
- Never commit `backend/media/` folder
- Never commit `backend/venv/` folder
- Never commit `backend/db.sqlite3`
- Run Black and Flake8 before every commit

---

## 14. Environment Variables

### File locations
```
backend/.env            ← actual env values (never commit)
backend/.env.example    ← template with empty values (safe to commit)
```

### Variables read by settings.py
These are the names `hapivet/settings.py` reads through `django-environ`.
Values are shown empty on purpose. Never put real values in docs.

```env
# Core
SECRET_KEY=
DEBUG=
ALLOWED_HOSTS=
CSRF_TRUSTED_ORIGINS=

# Database
# Local: the local hapivet_dev PostgreSQL URL (localhost:5432).
# Live (Render): the Supabase Session Pooler connection string.
# Never put the live URL in backend/.env.
DATABASE_URL=

# Google OAuth (allauth)
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=

# Email
# USE_BREVO_EMAIL: True sends real email through Brevo; False prints email
# to the console. Defaults to the opposite of DEBUG. Local: False.
USE_BREVO_EMAIL=
DEFAULT_FROM_EMAIL=
BREVO_API_KEY=            # required only when USE_BREVO_EMAIL=True

# Media storage
# USE_S3: True stores uploads in Supabase Storage (S3 API); False uses
# backend/media/. Defaults to False. Local: False. Live (Render): True.
USE_S3=
AWS_ACCESS_KEY_ID=        # the AWS_* and MEDIA_URL values are required
AWS_SECRET_ACCESS_KEY=    # only when USE_S3=True
AWS_STORAGE_BUCKET_NAME=
AWS_S3_ENDPOINT_URL=
AWS_S3_REGION_NAME=
AWS_S3_CUSTOM_DOMAIN=
MEDIA_URL=
```

`backend/.env.example` should list the same names with empty values.

Always update `.env.example` when adding new environment variables.

---

## 15. Quick Reference Checklist

Before starting any feature:
- [ ] Check `FEATURES.md` for the feature scope
- [ ] Check `DATA_MODELS.md` for the relevant models

Before committing:
- [ ] Black formatting applied
- [ ] Flake8 has no errors
- [ ] Relevant md files updated if models or features changed
- [ ] `.env` not included in commit
- [ ] Working on a feature branch, not `master` or `rebuild`

Before changing a model:
- [ ] Update `DATA_MODELS.md` first
- [ ] Write a migration plan (fields added/changed/removed, data risk) and get approval. `makemigrations` only with approval. Before any local `migrate`, run the DB host check (HANDOFF §3); it must show `localhost | hapivet_dev`. Merging into `rebuild` applies migrations to the live Supabase DB, so never migrate from a feature branch against live.
- [ ] Update `FEATURES.md` if feature scope changed
