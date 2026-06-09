from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.utils import timezone

from .models import Service, BillingReceipt, BillingItem
from .forms import ServiceForm, BillingReceiptForm, BillingItemForm


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
    receipts = BillingReceipt.objects.select_related(
        "owner", "pet", "appointment"
    )

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
    Admin view — create a new billing receipt.
    Can be linked to an appointment via ?appointment=<pk>.
    Owner and pet are derived from the appointment if linked.
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    from appointments.models import Appointment
    from pets.models import PetOwner, Pet

    # Check if linking to an appointment
    appointment = None
    owner = None
    pet = None

    appointment_pk = request.GET.get("appointment")
    if appointment_pk:
        appointment = get_object_or_404(Appointment, pk=appointment_pk)
        owner = appointment.owner
        pet = appointment.pet

        # Check if receipt already exists for this appointment
        if hasattr(appointment, "billing_receipt"):
            messages.error(
                request,
                "A billing receipt already exists for this appointment.",
            )
            return redirect(
                "admin_receipt_detail",
                pk=appointment.billing_receipt.pk,
            )

    if request.method == "POST":
        form = BillingReceiptForm(request.POST)

        # Get owner and pet from POST if not from appointment
        if not appointment:
            owner_pk = request.POST.get("owner")
            pet_pk = request.POST.get("pet")
            if owner_pk:
                owner = get_object_or_404(PetOwner, pk=owner_pk)
            if pet_pk:
                pet = get_object_or_404(Pet, pk=pet_pk)

        if form.is_valid():
            receipt = form.save(commit=False)
            receipt.receipt_number = BillingReceipt.generate_receipt_number()
            receipt.appointment = appointment
            receipt.owner = owner
            receipt.pet = pet
            receipt.save()

            messages.success(
                request,
                f"Receipt {receipt.receipt_number} created.",
            )
            return redirect("admin_receipt_detail", pk=receipt.pk)
        else:
            messages.error(request, "Please correct the errors below.")
    else:
        initial = {"billing_date": timezone.now().date()}
        form = BillingReceiptForm(initial=initial)

    # For non-appointment receipts, load owners for manual selection
    owners = PetOwner.objects.filter(is_archived=False).order_by(
        "last_name", "first_name"
    )

    return render(
        request,
        "admin/billing/receipt_form.html",
        {
            "form": form,
            "appointment": appointment,
            "owner": owner,
            "pet": pet,
            "owners": owners,
            "is_edit": False,
        },
    )


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
    items = receipt.billing_items.select_related("service").all()
    item_form = BillingItemForm()

    return render(
        request,
        "admin/billing/receipt_detail.html",
        {
            "receipt": receipt,
            "items": items,
            "item_form": item_form,
        },
    )


@login_required
def admin_receipt_edit(request, pk):
    """
    Admin view — edit a billing receipt header.
    Locked if payment status is PAID.
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    receipt = get_object_or_404(BillingReceipt, pk=pk)

    if receipt.is_locked:
        messages.error(request, "This receipt is locked and cannot be edited.")
        return redirect("admin_receipt_detail", pk=pk)

    if request.method == "POST":
        form = BillingReceiptForm(request.POST, instance=receipt)
        if form.is_valid():
            receipt = form.save()
            # Recalculate totals in case discount changed
            receipt.calculate_totals()
            messages.success(request, "Receipt updated.")
            return redirect("admin_receipt_detail", pk=pk)
        else:
            messages.error(request, "Please correct the errors below.")
    else:
        form = BillingReceiptForm(instance=receipt)

    items = receipt.billing_items.select_related("service").all()
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

    receipt = get_object_or_404(BillingReceipt, pk=receipt_pk)

    if receipt.is_locked:
        messages.error(request, "This receipt is locked and cannot be edited.")
        return redirect("admin_receipt_detail", pk=receipt_pk)

    if request.method == "POST":
        form = BillingItemForm(request.POST)
        if form.is_valid():
            BillingItem.objects.create(
                receipt=receipt,
                service=form.cleaned_data.get("service"),
                description=form.cleaned_data["description"],
                quantity=form.cleaned_data["quantity"],
                unit_price=form.cleaned_data["unit_price"],
                subtotal=0,  # Overwritten by BillingItem.save()
            )
            receipt.calculate_totals()
            messages.success(request, "Item added.")
        else:
            messages.error(request, "Please correct the errors.")

    return redirect("admin_receipt_detail", pk=receipt_pk)


@login_required
def admin_billing_item_delete(request, pk):
    """
    Admin view — remove a billing line item.
    Recalculates receipt totals after removal.
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    item = get_object_or_404(BillingItem, pk=pk)
    receipt = item.receipt

    if receipt.is_locked:
        messages.error(request, "This receipt is locked and cannot be edited.")
        return redirect("admin_receipt_detail", pk=receipt.pk)

    if request.method == "POST":
        item.delete()
        receipt.calculate_totals()
        messages.success(request, "Item removed.")

    return redirect("admin_receipt_detail", pk=receipt.pk)


@login_required
def admin_receipt_mark_paid(request, pk):
    """Admin action — mark a receipt as PAID."""
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    receipt = get_object_or_404(BillingReceipt, pk=pk)

    if request.method == "POST":
        if receipt.payment_status == BillingReceipt.PAID:
            messages.error(request, "Receipt is already marked as paid.")
        else:
            receipt.payment_status = BillingReceipt.PAID
            receipt.payment_date = timezone.now().date()
            receipt.save()
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

    receipt = get_object_or_404(BillingReceipt, pk=pk)

    if request.method == "POST":
        if receipt.is_locked:
            messages.error(
                request, "Paid receipts cannot be cancelled."
            )
        else:
            receipt.payment_status = BillingReceipt.CANCELLED
            receipt.save()
            messages.success(request, "Receipt cancelled.")

    return redirect("admin_receipt_detail", pk=pk)


# ── Pet Owner — Billing ───────────────────────────────────────────────────────

@login_required
def owner_billing_list(request):
    """Pet Owner view — list all their billing receipts."""
    if request.user.role != "pet_owner":
        return redirect("admin_dashboard")

    owner = request.user.petowner
    receipts = BillingReceipt.objects.filter(owner=owner).order_by(
        "-billing_date"
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
    items = receipt.billing_items.select_related("service").all()

    return render(
        request,
        "owner/billing/receipt_detail.html",
        {
            "receipt": receipt,
            "items": items,
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

    import json
    from django.http import JsonResponse

    service_pk = request.GET.get("service", "")

    if not service_pk:
        return JsonResponse({"name": "", "price": "", "placeholder": "", "pricing_type": ""})

    try:
        service = Service.objects.get(pk=int(service_pk))
    except (Service.DoesNotExist, ValueError):
        return JsonResponse({"name": "", "price": "", "placeholder": "", "pricing_type": ""})

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