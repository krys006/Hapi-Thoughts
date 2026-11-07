from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.utils import timezone

from .models import Service, BillingReceipt, BillingItem
from .forms import ServiceForm, BillingReceiptForm, BillingItemForm
from .utils import (
    LOCKED_MESSAGE,  # noqa: F401 (tests import it from here)
    NO_ITEMS_MESSAGE,
    RECEIPT_EXISTS,
    RECEIPT_NEW_TEMPLATE,
    add_item,
    create_receipt,
    create_visit_receipt,
    form_error_text,
    initial_pet_pick,
    lock_pending_receipt,
    mark_receipt_cancelled,
    mark_receipt_paid,
    notify_receipt_paid,
    owner_for_receipt,
    receipt_items,
    receipt_owner_context,
    receipt_pet_choice,
    receipt_pet_context,
    receipt_pets,
    refuse_locked,
    restore_message,
    save_discount,
    visit_for_receipt,
    visit_receipt_context,
    visit_receipt_stop,
    visit_service,
)

from appointments.models import ClinicSettings

# ── Admin — Services ──────────────────────────────────────────────────────────


@login_required
def admin_service_list(request):
    """Admin view — list all services in the catalog."""
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    status_filter = request.GET.get("status", "")
    services = Service.objects.all()

    if status_filter:
        services = services.filter(status=status_filter)

    counts = {
        "all": Service.objects.count(),
        "draft": Service.objects.filter(status=Service.DRAFT).count(),
        "active": Service.objects.filter(status=Service.ACTIVE).count(),
        "unlisted": Service.objects.filter(status=Service.UNLISTED).count(),
        "closed": Service.objects.filter(status=Service.CLOSED).count(),
    }

    return render(
        request,
        "admin/billing/services_list.html",
        {
            "services": services,
            "status_filter": status_filter,
            "counts": counts,
            "status_choices": Service.STATUS_CHOICES,
        },
    )


@login_required
def admin_service_create(request):
    """Admin view — create a new service."""
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    if request.method == "POST":
        form = ServiceForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Service created.")
            return redirect("admin_service_list")
        else:
            messages.error(request, "Please correct the errors below.")
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

    if request.method == "POST":
        form = ServiceForm(request.POST, instance=service)
        if form.is_valid():
            form.save()
            messages.success(request, "Service updated.")
            return redirect("admin_service_list")
        else:
            messages.error(request, "Please correct the errors below.")
    else:
        form = ServiceForm(instance=service)

    return render(
        request,
        "admin/billing/service_form.html",
        {"form": form, "service": service, "is_edit": True},
    )


# ── Admin — Billing Receipts ──────────────────────────────────────────────────


@login_required
def admin_receipt_list(request):
    """Admin view — list all billing receipts."""
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    status_filter = request.GET.get("status", "")
    receipts = BillingReceipt.objects.select_related("owner", "pet", "appointment")

    if status_filter:
        receipts = receipts.filter(payment_status=status_filter)

    counts = {
        "all": BillingReceipt.objects.count(),
        "pending": BillingReceipt.objects.filter(
            payment_status=BillingReceipt.PENDING
        ).count(),
        "paid": BillingReceipt.objects.filter(
            payment_status=BillingReceipt.PAID
        ).count(),
        "cancelled": BillingReceipt.objects.filter(
            payment_status=BillingReceipt.CANCELLED
        ).count(),
    }

    return render(
        request,
        "admin/billing/receipt_list.html",
        {
            "receipts": receipts,
            "status_filter": status_filter,
            "counts": counts,
        },
    )


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
    return render(request, RECEIPT_NEW_TEMPLATE, receipt_owner_context(request.GET))


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
    context = visit_receipt_context(appointment, service)
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


@login_required
def admin_receipt_detail(request, pk):
    """
    Admin view — view a billing receipt with all line items.
    Also handles adding billing items.
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    receipt = get_object_or_404(
        BillingReceipt.objects.select_related("owner", "pet", "appointment"),
        pk=pk,
    )
    items = receipt_items(receipt)
    item_form = BillingItemForm()

    return render(
        request,
        "admin/billing/receipt_detail.html",
        {
            "receipt": receipt,
            "items": items,
            "item_form": item_form,
            "clinic_settings": ClinicSettings.objects.first(),
        },
    )


@login_required
def admin_receipt_edit(request, pk):
    """
    Admin view — edit a billing receipt header.
    Locked if payment status is PAID or CANCELLED.
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    if request.method == "POST":
        # Row-locked: the discount is checked against the items as they are
        with transaction.atomic():
            receipt = lock_pending_receipt(pk)
            if receipt is None:
                return refuse_locked(request, pk)
            form = BillingReceiptForm(request.POST, instance=receipt)
            if form.is_valid():
                save_discount(form)
                messages.success(request, "Receipt updated.")
                return redirect("admin_receipt_detail", pk=pk)
        # The failed form wrote posted values onto the instance
        receipt.refresh_from_db()
        messages.error(request, "Please correct the errors below.")
    else:
        receipt = get_object_or_404(BillingReceipt, pk=pk)
        if receipt.is_locked:
            return refuse_locked(request, pk)
        form = BillingReceiptForm(instance=receipt)

    items = receipt_items(receipt)
    item_form = BillingItemForm()

    return render(
        request,
        "admin/billing/receipt_form.html",
        {
            "form": form,
            "receipt": receipt,
            "items": items,
            "item_form": item_form,
            "is_edit": True,
        },
    )


@login_required
def admin_billing_item_add(request, receipt_pk):
    """
    Admin view — add a billing line item to a receipt.
    Recalculates receipt totals after adding.
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
        else:
            # The old receipt pages lose the form on redirect: flash the
            # real errors until 6a-2 re-renders the dialog
            messages.error(request, form_error_text(form))

    return redirect("admin_receipt_detail", pk=receipt_pk)


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
    """Pet Owner view — list all their billing receipts."""
    if request.user.role != "pet_owner":
        return redirect("admin_dashboard")

    owner = request.user.petowner
    receipts = BillingReceipt.objects.filter(owner=owner).order_by("-billing_date")

    return render(
        request,
        "owner/billing/list.html",
        {"receipts": receipts},
    )


@login_required
def owner_receipt_detail(request, pk):
    """
    Pet Owner view — view a single billing receipt.
    Owners can only view their own receipts.
    """
    if request.user.role != "pet_owner":
        return redirect("admin_dashboard")

    owner = request.user.petowner
    receipt = get_object_or_404(
        BillingReceipt.objects.select_related("pet", "appointment"),
        pk=pk,
        owner=owner,
    )
    items = receipt_items(receipt)

    return render(
        request,
        "owner/billing/receipt_detail.html",
        {
            "receipt": receipt,
            "items": items,
            "clinic_settings": ClinicSettings.objects.first(),
        },
    )


@login_required
def admin_get_service_details(request):
    """
    HTMX view — returns service name and pricing details for a selected service.
    Used to pre-fill the billing item form when a service is selected.
    Returns JSON consumed by inline JS in the template.
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    from django.http import JsonResponse

    service_pk = request.GET.get("service", "")

    if not service_pk:
        return JsonResponse(
            {"name": "", "price": "", "placeholder": "", "pricing_type": ""}
        )

    try:
        service = Service.objects.get(pk=int(service_pk))
    except (Service.DoesNotExist, ValueError):
        return JsonResponse(
            {"name": "", "price": "", "placeholder": "", "pricing_type": ""}
        )

    data = {
        "name": service.service_name,
        "pricing_type": service.pricing_type,
        "price": "",
        "placeholder": "",
    }

    if service.pricing_type == Service.FIXED:
        data["price"] = str(service.base_price)
        data["placeholder"] = ""
    elif service.pricing_type == Service.RANGE:
        data["price"] = ""
        data["placeholder"] = f"₱{service.min_price:,.2f} – ₱{service.max_price:,.2f}"

    return JsonResponse(data)
