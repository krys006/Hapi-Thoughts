"""Shared billing logic for the receipt views (Slice 6)."""

from django.contrib import messages
from django.core.exceptions import NON_FIELD_ERRORS
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone

from notifications.utils import notify

from .models import BillingItem, BillingReceipt

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
