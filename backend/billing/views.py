from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.utils import timezone
from django.utils.cache import patch_vary_headers

from appointments.utils import is_owner_results_swap

from .models import Service, BillingItem
from .forms import ServiceForm, BillingReceiptForm, BillingItemForm
from .utils import (
    LOCKED_MESSAGE,  # noqa: F401 (tests import it from here)
    NO_ITEMS_MESSAGE,
    RECEIPT_EXISTS,
    RECEIPT_NEW_TEMPLATE,
    RECEIPT_OWNER_RESULTS_TEMPLATE,
    add_item,
    create_receipt,
    create_visit_receipt,
    discount_label,
    discount_saved_message,
    initial_pet_pick,
    lock_pending_receipt,
    mark_receipt_cancelled,
    mark_receipt_paid,
    notify_receipt_paid,
    owner_for_receipt,
    owner_receipts,
    receipt_detail_context,
    receipt_for_page,
    receipt_items,
    receipt_list_context,
    receipt_owner_context,
    receipt_pet_choice,
    receipt_pet_context,
    receipt_pets,
    refuse_locked,
    restore_message,
    save_discount,
    service_saved_message,
    services_list_context,
    visit_for_receipt,
    visit_receipt_context,
    visit_receipt_stop,
    visit_service,
)

from appointments.models import ClinicSettings

# ── Admin — Services ──────────────────────────────────────────────────────────


@login_required
def admin_service_list(request):
    """Admin view — the services, grouped by who sees them
    (services_list_context)."""
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    context = services_list_context(request.GET)
    return render(request, "admin/billing/services_list.html", context)


@login_required
def admin_service_create(request):
    """Admin view — create a new service (starts as Hidden (draft))."""
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    if request.method == "POST":
        form = ServiceForm(request.POST)
        if form.is_valid():
            service = form.save()
            messages.success(request, service_saved_message(service, created=True))
            return redirect("admin_service_list")
    else:
        form = ServiceForm()

    return render(
        request,
        "admin/billing/service_form.html",
        {"form": form, "is_edit": False},
    )


@login_required
def admin_service_edit(request, pk):
    """Admin view — edit an existing service."""
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    service = get_object_or_404(Service, pk=pk)
    # The form changes the instance while it validates; the page's title and
    # note must keep the saved name after a failed save
    saved_name = service.service_name

    if request.method == "POST":
        form = ServiceForm(request.POST, instance=service)
        if form.is_valid():
            form.save()
            messages.success(request, service_saved_message(service, created=False))
            return redirect("admin_service_list")
    else:
        form = ServiceForm(instance=service)

    return render(
        request,
        "admin/billing/service_form.html",
        {"form": form, "saved_name": saved_name, "is_edit": True},
    )


# ── Admin — Billing Receipts ──────────────────────────────────────────────────


@login_required
def admin_receipt_list(request):
    """
    Admin view — the receipt list: tabs (?status=, Unpaid by default),
    search (?q=) and pages (receipt_list_context).
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    context = receipt_list_context(request.GET, timezone.localdate())
    return render(request, "admin/billing/receipt_list.html", context)


@login_required
def admin_receipt_create(request):
    """
    Admin view — New receipt (admin/billing/receipt_new.html), three modes:
    ?appointment=<pk> from a visit; ?owner=<pk> "Which pet?" (step 2);
    neither: "Who is it for?" (step 1, the owner list and search).
    Every new receipt is saved empty (from a visit, optionally with the
    visit's fixed-price service as the first item) and opens its page.
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    if request.GET.get("appointment"):
        appointment = visit_for_receipt(request.GET["appointment"])
        return _new_receipt_from_visit(request, appointment)
    if request.GET.get("owner"):
        owner = owner_for_receipt(request.GET["owner"])
        return _new_receipt_for_owner(request, owner)
    # Step 1: the search box filters as you type (HTMX gets only the results)
    template = (
        RECEIPT_OWNER_RESULTS_TEMPLATE
        if is_owner_results_swap(request)
        else RECEIPT_NEW_TEMPLATE
    )
    response = render(request, template, receipt_owner_context(request.GET))
    # The same URL answers with the partial or the full page
    patch_vary_headers(response, ["HX-Request"])
    return response


def _receipt_created(request, receipt):
    messages.success(request, f"Receipt {receipt.receipt_number} created.")
    return redirect("admin_receipt_detail", pk=receipt.pk)


def _new_receipt_from_visit(request, appointment):
    """Mode "visit". GET and POST both check the visit first."""
    stop = visit_receipt_stop(appointment, timezone.localdate())
    if stop:
        level, message, url_name, pk = stop
        messages.add_message(request, level, message)
        return redirect(url_name, pk=pk)

    service = visit_service(appointment)
    if request.method == "POST":
        first = service if request.POST.get("add_service") == "on" else None
        receipt, created = create_visit_receipt(appointment, first)
        if not created:
            messages.info(request, RECEIPT_EXISTS)
            return redirect("admin_receipt_detail", pk=receipt.pk)
        return _receipt_created(request, receipt)
    context = visit_receipt_context(appointment, service, timezone.localdate())
    return render(request, RECEIPT_NEW_TEMPLATE, context)


def _new_receipt_for_owner(request, owner):
    """Mode "pet". An archived owner goes back to step 1."""
    if owner.is_archived:
        messages.error(request, restore_message(owner.full_name))
        return redirect("admin_receipt_create")

    pets = receipt_pets(owner)
    picked, pet_error = initial_pet_pick(pets), ""
    if request.method == "POST":
        pet, pet_error = receipt_pet_choice(owner, pets, request.POST.get("pet"))
        if not pet_error:
            return _receipt_created(request, create_receipt(owner, pet))
        picked = ""
    context = receipt_pet_context(owner, pets, picked, pet_error)
    return render(request, RECEIPT_NEW_TEMPLATE, context)


def _render_receipt_detail(
    request, receipt, item_form=None, discount_form=None, open_dialog=None
):
    """
    The receipt page. Shared by admin_receipt_detail and the item add /
    discount views, which pass their bound form and the dialog to reopen
    ("add-item" / "discount") when it has errors. receipt comes from
    receipt_for_page (the saved values, not a failed form's).
    """
    return render(
        request,
        "admin/billing/receipt_detail.html",
        {
            **receipt_detail_context(receipt, timezone.localdate()),
            "item_form": item_form or BillingItemForm(),
            "discount_form": discount_form or BillingReceiptForm(instance=receipt),
            "open_dialog": open_dialog,
        },
    )


@login_required
def admin_receipt_detail(request, pk):
    """Admin view — the receipt page (receipt_detail_context)."""
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    return _render_receipt_detail(request, receipt_for_page(pk))


@login_required
def admin_receipt_edit(request, pk):
    """
    Admin view — the Discount dialog's endpoint (POST; the old edit page is
    gone, so a GET opens the receipt page). Refused once PAID or CANCELLED;
    errors re-render the receipt page with the Discount dialog open.
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    if request.method != "POST":
        return redirect("admin_receipt_detail", pk=pk)

    # Row-locked: the discount is checked against the items as they are
    with transaction.atomic():
        receipt = lock_pending_receipt(pk)
        if receipt is None:
            return refuse_locked(request, pk)
        form = BillingReceiptForm(request.POST, instance=receipt)
        if form.is_valid():
            save_discount(form)
            messages.success(request, discount_saved_message(form))
            return redirect("admin_receipt_detail", pk=pk)

    return _render_receipt_detail(
        request, receipt_for_page(pk), discount_form=form, open_dialog="discount"
    )


@login_required
def admin_billing_item_add(request, receipt_pk):
    """
    Admin view — add a billing line item to a receipt (POST) and
    recalculate the totals. Errors re-render the receipt page with the
    Add item dialog open (nothing saved, no flash).
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    if request.method != "POST":
        return redirect("admin_receipt_detail", pk=receipt_pk)

    with transaction.atomic():
        receipt = lock_pending_receipt(receipt_pk)
        if receipt is None:
            return refuse_locked(request, receipt_pk)
        form = BillingItemForm(request.POST)
        if form.is_valid():
            add_item(receipt, form.cleaned_data)
            messages.success(request, "Item added.")
            return redirect("admin_receipt_detail", pk=receipt_pk)

    return _render_receipt_detail(
        request, receipt_for_page(receipt_pk), item_form=form, open_dialog="add-item"
    )


@login_required
def admin_billing_item_delete(request, pk):
    """
    Admin view — remove a billing line item.
    Recalculates receipt totals after removal.
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    receipt_pk = get_object_or_404(
        BillingItem.objects.values_list("receipt_id", flat=True), pk=pk
    )
    if request.method != "POST":
        return redirect("admin_receipt_detail", pk=receipt_pk)

    with transaction.atomic():
        receipt = lock_pending_receipt(receipt_pk)
        if receipt is None:
            return refuse_locked(request, receipt_pk)
        # Only an item of the receipt that is locked (404 if it is gone)
        get_object_or_404(receipt.billing_items, pk=pk).delete()
        receipt.calculate_totals()
        messages.success(request, "Item removed.")

    return redirect("admin_receipt_detail", pk=receipt_pk)


@login_required
def admin_receipt_mark_paid(request, pk):
    """Admin action — mark a receipt as PAID, in cash, today."""
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    if request.method != "POST":
        return redirect("admin_receipt_detail", pk=pk)

    with transaction.atomic():
        receipt = lock_pending_receipt(pk)
        if receipt is None:
            return refuse_locked(request, pk)
        if not receipt.billing_items.exists():
            messages.error(request, NO_ITEMS_MESSAGE)
            return redirect("admin_receipt_detail", pk=pk)
        mark_receipt_paid(receipt)

    # After the commit, so the row lock isn't held while the email sends
    notify_receipt_paid(receipt)
    messages.success(
        request,
        f"Receipt {receipt.receipt_number} marked as paid.",
    )
    return redirect("admin_receipt_detail", pk=pk)


@login_required
def admin_receipt_mark_cancelled(request, pk):
    """Admin action — mark a receipt as CANCELLED."""
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    if request.method != "POST":
        return redirect("admin_receipt_detail", pk=pk)

    with transaction.atomic():
        receipt = lock_pending_receipt(pk)
        if receipt is None:
            return refuse_locked(request, pk)
        mark_receipt_cancelled(receipt)
        messages.success(request, "Receipt cancelled.")

    return redirect("admin_receipt_detail", pk=pk)


# ── Pet Owner — Billing ───────────────────────────────────────────────────────


@login_required
def owner_billing_list(request):
    """Pet Owner view — their Unpaid and Paid receipts (never Cancelled)."""
    if request.user.role != "pet_owner":
        return redirect("admin_dashboard")

    receipts = (
        owner_receipts(request.user.petowner)
        .select_related("pet")
        .order_by("-billing_date", "-created_at")
    )

    return render(
        request,
        "owner/billing/list.html",
        {"receipts": receipts},
    )


@login_required
def owner_receipt_detail(request, pk):
    """
    Pet Owner view — view a single billing receipt.
    Owners can only view their own receipts; a cancelled one is a 404.
    """
    if request.user.role != "pet_owner":
        return redirect("admin_dashboard")

    receipt = get_object_or_404(
        owner_receipts(request.user.petowner).select_related("pet", "appointment"),
        pk=pk,
    )
    items = receipt_items(receipt)

    return render(
        request,
        "owner/billing/receipt_detail.html",
        {
            "receipt": receipt,
            "items": items,
            "discount_label": discount_label(receipt),
            "clinic_settings": ClinicSettings.objects.first(),
        },
    )
