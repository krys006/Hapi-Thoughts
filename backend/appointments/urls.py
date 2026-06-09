from django.urls import path
from . import views

urlpatterns = [
    # ── Admin — Clinic Settings ───────────────────────────────────────────
    path(
        "admin/settings/",
        views.admin_clinic_settings,
        name="admin_clinic_settings",
    ),
    path(
        "admin/settings/blocked-dates/add/",
        views.admin_blocked_date_add,
        name="admin_blocked_date_add",
    ),
    path(
        "admin/settings/blocked-dates/<int:pk>/delete/",
        views.admin_blocked_date_delete,
        name="admin_blocked_date_delete",
    ),

    # ── Admin — Appointments ──────────────────────────────────────────────
    path(
        "admin/appointments/",
        views.admin_appointment_list,
        name="admin_appointments",
    ),
    path(
        "admin/appointments/walk-in/",
        views.admin_walkin_appointment_create,
        name="admin_walkin_appointment_create",
    ),
    path(
        "admin/appointments/<int:pk>/",
        views.admin_appointment_detail,
        name="admin_appointment_detail",
    ),
    path(
        "admin/appointments/<int:pk>/approve/",
        views.admin_appointment_approve,
        name="admin_appointment_approve",
    ),
    path(
        "admin/appointments/<int:pk>/reject/",
        views.admin_appointment_reject,
        name="admin_appointment_reject",
    ),
    path(
        "admin/appointments/<int:pk>/complete/",
        views.admin_appointment_complete,
        name="admin_appointment_complete",
    ),
    path(
        "admin/appointments/<int:pk>/no-show/",
        views.admin_appointment_no_show,
        name="admin_appointment_no_show",
    ),
    path(
        "admin/appointments/<int:pk>/notes/",
        views.admin_appointment_notes,
        name="admin_appointment_notes",
    ),
    path(
        "admin/appointments/<int:pk>/cancel/",
        views.admin_appointment_cancel,
        name="admin_appointment_cancel",
    ),
    path(
        "admin/appointments/<int:pk>/reschedule/",
        views.admin_appointment_reschedule,
        name="admin_appointment_reschedule",
    ),

    # ── Admin — HTMX helpers ──────────────────────────────────────────────
    path(
        "admin/appointments/pets/",
        views.admin_get_pets_for_owner,
        name="admin_get_pets_for_owner",
    ),
    path(
        "admin/appointments/slots/",
        views.admin_get_slots_for_date,
        name="admin_get_slots_for_date",
    ),

    # ── Pet Owner — Appointments ──────────────────────────────────────────
    path(
        "owner/appointments/",
        views.owner_appointment_list,
        name="owner_appointments",
    ),
    path(
        "owner/appointments/book/",
        views.owner_book_appointment,
        name="owner_book_appointment",
    ),
    path(
        "owner/appointments/<int:pk>/",
        views.owner_appointment_detail,
        name="owner_appointment_detail",
    ),
    path(
        "owner/appointments/<int:pk>/cancel/",
        views.owner_appointment_cancel,
        name="owner_appointment_cancel",
    ),
    path(
        "owner/appointments/<int:pk>/reschedule/",
        views.owner_appointment_reschedule,
        name="owner_appointment_reschedule",
    ),

    # ── HTMX — Slot loader (owner) ────────────────────────────────────────
    path(
        "owner/appointments/slots/",
        views.owner_get_slots,
        name="owner_get_slots",
    ),
]