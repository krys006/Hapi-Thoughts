"""Shared billing logic for the receipt views (Slice 6)."""

from django.contrib import messages
from django.core.exceptions import NON_FIELD_ERRORS
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.text import capfirst

from appointments.models import Appointment
from appointments.utils import (
    WALKIN_PAGE_SIZE,
    WALKIN_SEARCH_LIMIT,
    paging_links,
    search_walkin_owners,
    walkin_owner_list,
)
from medical.utils import short_date
from notifications.utils import notify
from pets.models import Pet, PetOwner

from .models import BillingItem, BillingReceipt, Service

# Shown whenever a write is refused because receipt.is_locked
LOCKED_MESSAGE = "This receipt is locked and can't be changed."
NO_ITEMS_MESSAGE = "Add at least one item first."

# The only payment method recorded (core rule 5: cash only)
CASH = "Cash"

DISCOUNT_FIELDS = ["discount_type", "discount_value", "discount_note"]


def lock_pending_receipt(pk):
    """
    Call inside transaction.atomic(). Loads the receipt with its row locked
    (select_for_update) and returns it while it is still PENDING; returns
    None when it is PAID or CANCELLED, so the view refuses. 404 if missing.
    A second request for the same receipt waits here until the first one
    commits, then sees its new status.
    """
    receipt = get_object_or_404(BillingReceipt.objects.select_for_update(), pk=pk)
    return None if receipt.is_locked else receipt


def refuse_locked(request, pk):
    """The locked flash and back to the receipt page."""
    messages.error(request, LOCKED_MESSAGE)
    return redirect("admin_receipt_detail", pk=pk)


def receipt_items(receipt):
    """A receipt's items, first added first."""
    return receipt.billing_items.select_related("service").order_by("created_at", "pk")


def form_error_text(form):
    """Every error on a form as one line, for a flash message (the old
    receipt pages redirect after a failed item add, so the form is lost).
    Field order, then the form-wide errors."""
    names = [*form.fields, NON_FIELD_ERRORS]
    return " ".join(str(error) for name in names for error in form.errors.get(name, []))


def save_discount(form):
    """Saves a valid BillingReceiptForm's three discount fields (and
    updated_at) only, then recalculates the totals."""
    receipt = form.instance
    for name in DISCOUNT_FIELDS:
        setattr(receipt, name, form.cleaned_data[name])
    receipt.save(update_fields=[*DISCOUNT_FIELDS, "updated_at"])
    receipt.calculate_totals()
    return receipt


def add_item(receipt, data):
    """Adds a valid BillingItemForm's item to the (locked) receipt and
    recalculates the totals."""
    item = BillingItem.objects.create(
        receipt=receipt,
        service=data["service"],
        description=data["description"],
        quantity=data["quantity"],
        unit_price=data["unit_price"],
        subtotal=0,  # Overwritten by BillingItem.save()
    )
    receipt.calculate_totals()
    return item


def mark_receipt_paid(receipt):
    """PAID, paid today (Manila date), in cash. Status fields only."""
    receipt.payment_status = BillingReceipt.PAID
    receipt.payment_date = timezone.localdate()
    receipt.payment_method = CASH
    receipt.save(
        update_fields=["payment_status", "payment_date", "payment_method", "updated_at"]
    )


def mark_receipt_cancelled(receipt):
    """CANCELLED (void). Status field only."""
    receipt.payment_status = BillingReceipt.CANCELLED
    receipt.save(update_fields=["payment_status", "updated_at"])


def notify_receipt_paid(receipt):
    """The owner's paid notification (text unchanged; 6a-3 rewrites it)."""
    if not (receipt.owner and receipt.owner.user):
        return
    notify(
        recipient=receipt.owner.user,
        notification_type="billing_generated",
        title="Payment Confirmed",
        message=(
            f"Your payment for receipt {receipt.receipt_number} "
            f"(₱{receipt.total_amount:,.2f}) has been confirmed. "
            f"Thank you!"
        ),
        related_billing=receipt,
        related_pet=receipt.pet,
        email_subject=f"Payment Confirmed — {receipt.receipt_number}",
    )


def _save_with_new_number(receipt):
    receipt.receipt_number = BillingReceipt.generate_receipt_number()
    with transaction.atomic():
        receipt.save()


def save_new_receipt(receipt):
    """
    Gives a new receipt the next number and saves it. If the save fails
    only because that number was taken in the meantime (IntegrityError and
    the number now exists), it retries once with a fresh number, each try
    in its own savepoint. Any other IntegrityError is raised as before.
    """
    try:
        _save_with_new_number(receipt)
    except IntegrityError:
        taken = BillingReceipt.objects.filter(
            receipt_number=receipt.receipt_number
        ).exists()
        if not taken:
            raise
        _save_with_new_number(receipt)
    return receipt


# ── New receipt (admin_receipt_create, Slice 6a-2) ───────────────────────────

RECEIPT_EXISTS = "A receipt already exists for this visit."
RECEIPT_VISIT_STATUSES = (Appointment.CONFIRMED, Appointment.COMPLETED)
# Services that can be billed (the item form's "What is it?" list)
BILLABLE_SERVICE_STATUSES = (Service.ACTIVE, Service.UNLISTED)
# The "Not for a specific pet" radio's value
NO_PET = "none"
PICK_PET_MESSAGE = "Choose a pet, or “Not for a specific pet”."
RECEIPT_NEW_TEMPLATE = "admin/billing/receipt_new.html"


def _whole_number_or_404(value):
    """value (a string from the URL or a form) as an int; 404 when it
    isn't a whole number."""
    try:
        return int(value)
    except (TypeError, ValueError):
        raise Http404("Not a whole number.")


def restore_message(name):
    """'Restore Ana Cruz first.' (an archived owner or pet)."""
    return f"Restore {name} first."


def visit_for_receipt(value):
    """The appointment from ?appointment= (owner, pet, service loaded);
    404 when it isn't a whole number or doesn't exist."""
    return get_object_or_404(
        Appointment.objects.select_related("owner", "pet", "service"),
        pk=_whole_number_or_404(value),
    )


def visit_receipt(appointment):
    """The receipt already made for this visit (any status), or None."""
    return BillingReceipt.objects.filter(appointment=appointment).first()


def visit_receipt_stop(appointment, today):
    """
    Why no receipt can be made for this visit, as (message level, message,
    url name, pk) for the redirect, or None. A visit that already has a
    receipt (even a cancelled one) opens that receipt. Otherwise back to
    the visit when the owner or pet is archived (owner first), the visit
    isn't confirmed or completed, or it is dated after today (Manila).
    """
    existing = visit_receipt(appointment)
    if existing:
        return (messages.INFO, RECEIPT_EXISTS, "admin_receipt_detail", existing.pk)
    if appointment.owner.is_archived:
        message = restore_message(appointment.owner.full_name)
    elif appointment.pet.is_archived:
        message = restore_message(appointment.pet.name)
    elif appointment.status not in RECEIPT_VISIT_STATUSES:
        message = "A receipt can only be made for a confirmed or completed visit."
    elif appointment.date > today:
        message = (
            "This visit hasn't happened yet. Make the receipt on or after "
            f"{short_date(appointment.date, today)}."
        )
    else:
        return None
    return (messages.ERROR, message, "admin_appointment_detail", appointment.pk)


def visit_service(appointment):
    """The visit's service when it can be the first item: a fixed price,
    Offered or Clinic use only. Otherwise None (no checkbox)."""
    service = appointment.service
    if (
        service
        and service.pricing_type == Service.FIXED
        and service.status in BILLABLE_SERVICE_STATUSES
    ):
        return service
    return None


def create_receipt(owner, pet, appointment=None, first_service=None):
    """
    Saves a new, empty PENDING receipt (billed today, Manila date, in cash)
    with save_new_receipt. With first_service, also its first item (the
    service at its base price) and the totals. All in one transaction: an
    IntegrityError (e.g. the visit got a receipt meanwhile) saves nothing.
    """
    receipt = BillingReceipt(
        owner=owner,
        pet=pet,
        appointment=appointment,
        billing_date=timezone.localdate(),
        payment_method=CASH,
    )
    with transaction.atomic():
        save_new_receipt(receipt)
        if first_service:
            BillingItem.objects.create(
                receipt=receipt,
                service=first_service,
                description=first_service.service_name,
                quantity=1,
                unit_price=first_service.base_price,
                subtotal=0,  # Overwritten by BillingItem.save()
            )
            receipt.calculate_totals()
    return receipt


def create_visit_receipt(appointment, first_service=None):
    """
    (receipt, True) for a new receipt for this visit, or (the visit's
    existing receipt, False) when another save for it got there first
    (the OneToOne raises IntegrityError; nothing of this one is saved).
    """
    owner, pet = appointment.owner, appointment.pet
    try:
        return create_receipt(owner, pet, appointment, first_service), True
    except IntegrityError:
        # Queried directly (not visit_receipt), so a test can patch that
        # check to let the race happen
        existing = BillingReceipt.objects.filter(appointment=appointment).first()
        if existing is None:
            raise
        return existing, False


def _receipts_back():
    return {"back_url": reverse("admin_receipt_list"), "back_label": "Back to receipts"}


def visit_receipt_context(appointment, service):
    """Mode "visit": the summary card and the service checkbox."""
    with_pet_lines([appointment.pet])
    return {
        "mode": "visit",
        "appointment": appointment,
        "visit_service": service,
        "back_url": reverse("admin_appointment_detail", args=[appointment.pk]),
        "back_label": "Back to the visit",
    }


def receipt_owner_context(params):
    """
    Mode "owner" (step 1, "Who is it for?"), from the query string. With
    no q: every active owner, first name A to Z, WALKIN_PAGE_SIZE per page
    (the walk-in list and pager). With q: the walk-in search (every word
    matches first or last name, phone or an active pet's name), at most
    WALKIN_SEARCH_LIMIT, no pager.
    """
    q = params.get("q", "").strip()
    context = {
        "mode": "owner",
        "q": q,
        "is_searching": bool(q),
        "page_obj": None,
        "search_limit": WALKIN_SEARCH_LIMIT,
        "empty_topbar_search": True,
        **_receipts_back(),
    }
    if q:
        owners, context["is_cut"] = search_walkin_owners(q)
        context.update(owners=owners, owner_count=len(owners))
        return context
    paginator = Paginator(walkin_owner_list(), WALKIN_PAGE_SIZE)
    page_obj = paginator.get_page(params.get("page"))
    context.update(
        owners=page_obj.object_list,
        owner_count=paginator.count,
        page_obj=page_obj,
        is_cut=False,
        **paging_links(page_obj, ""),
    )
    return context


def owner_for_receipt(value):
    """The owner from ?owner=; 404 when it isn't a whole number or doesn't
    exist. Archived owners are returned (the view refuses them)."""
    return get_object_or_404(PetOwner, pk=_whole_number_or_404(value))


def with_pet_lines(pets):
    """Gives each pet detail_line: 'Dog · Aspin · 3 yrs', empty parts left
    out (no breed, no birth date)."""
    for pet in pets:
        parts = [capfirst(pet.species), pet.breed, pet.age if pet.date_of_birth else ""]
        pet.detail_line = " · ".join(part for part in parts if part)
    return pets


def receipt_pets(owner):
    """The owner's active pets, by name, each with detail_line."""
    return with_pet_lines(
        list(Pet.objects.filter(owner=owner, is_archived=False).order_by("name", "pk"))
    )


def initial_pet_pick(pets):
    """Step 2's first pick: the only pet, "none" when there are no pets,
    nothing ("") with two or more."""
    if len(pets) == 1:
        return str(pets[0].pk)
    return "" if pets else NO_PET


def receipt_pet_choice(owner, pets, value):
    """
    The posted pet as (pet or None, error text). "none" is no pet; empty
    asks to choose; a pet that isn't one of pets (the owner's active pets:
    another owner's, or archived) gets "Choose one of {owner}'s pets.".
    Not a whole number, or no such pet: 404.
    """
    value = (value or "").strip()
    if value == NO_PET:
        return None, ""
    if not value:
        return None, PICK_PET_MESSAGE
    pet = get_object_or_404(Pet, pk=_whole_number_or_404(value))
    if pet.pk not in {p.pk for p in pets}:
        return None, f"Choose one of {owner.full_name}'s pets."
    return pet, ""


def receipt_pet_context(owner, pets, picked, pet_error=""):
    """Mode "pet" (step 2, "Which pet?")."""
    return {
        "mode": "pet",
        "owner": owner,
        "pets": pets,
        "picked": picked,
        "no_pet": NO_PET,
        "pet_error": pet_error,
        "change_owner_url": reverse("admin_receipt_create"),
        **_receipts_back(),
    }
