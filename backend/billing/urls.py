from django.urls import path
from . import views

urlpatterns = [
    # ── Admin — Services ──────────────────────────────────────────────────
    path(
        "admin/services/",
        views.admin_service_list,
        name="admin_service_list",
    ),
    path(
        "admin/services/create/",
        views.admin_service_create,
        name="admin_service_create",
    ),
    path(
        "admin/services/<int:pk>/edit/",
        views.admin_service_edit,
        name="admin_service_edit",
    ),
    # ── Admin — Billing Receipts ──────────────────────────────────────────
    path(
        "admin/billing/",
        views.admin_receipt_list,
        name="admin_receipt_list",
    ),
    path(
        "admin/billing/create/",
        views.admin_receipt_create,
        name="admin_receipt_create",
    ),
    path(
        "admin/billing/<int:pk>/",
        views.admin_receipt_detail,
        name="admin_receipt_detail",
    ),
    path(
        "admin/billing/<int:pk>/edit/",
        views.admin_receipt_edit,
        name="admin_receipt_edit",
    ),
    path(
        "admin/billing/<int:pk>/mark-paid/",
        views.admin_receipt_mark_paid,
        name="admin_receipt_mark_paid",
    ),
    path(
        "admin/billing/<int:pk>/send/",
        views.admin_receipt_send,
        name="admin_receipt_send",
    ),
    path(
        "admin/billing/<int:pk>/cancel/",
        views.admin_receipt_mark_cancelled,
        name="admin_receipt_mark_cancelled",
    ),
    # ── Admin — Billing Items ─────────────────────────────────────────────
    path(
        "admin/billing/<int:receipt_pk>/items/add/",
        views.admin_billing_item_add,
        name="admin_billing_item_add",
    ),
    path(
        "admin/billing/items/<int:pk>/delete/",
        views.admin_billing_item_delete,
        name="admin_billing_item_delete",
    ),
    # ── Admin — Income summary (read-only) ────────────────────────────────
    path(
        "admin/income/",
        views.admin_income,
        name="admin_income",
    ),
    # ── Pet Owner — Billing ───────────────────────────────────────────────
    path(
        "owner/billing/",
        views.owner_billing_list,
        name="owner_billing_list",
    ),
    path(
        "owner/billing/<int:pk>/",
        views.owner_receipt_detail,
        name="owner_receipt_detail",
    ),
    # ── Pet Owner — Services and prices ───────────────────────────────────
    path(
        "owner/services/",
        views.owner_services,
        name="owner_services",
    ),
]
