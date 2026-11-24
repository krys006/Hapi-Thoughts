"""Shared billing logic for the receipt views (Slice 6)."""

import calendar
import datetime
import math
from decimal import Decimal
from itertools import groupby
from urllib.parse import urlencode

from django.contrib import messages
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import (
    Count,
    DecimalField,
    Exists,
    F,
    OuterRef,
    Q,
    Sum,
    Value,
)
from django.db.models.functions import Coalesce, Lower, TruncMonth
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.dateformat import format as date_format
from django.utils.text import capfirst

from appointments.models import Appointment
from appointments.utils import (
    WALKIN_PAGE_SIZE,
    WALKIN_SEARCH_LIMIT,
    clinic_phone_link,
    get_clinic_settings,
    paging_links,
    search_walkin_owners,
    walkin_owner_list,
)
from dashboard.templatetags.friendly_dates import friendly_date_on, long_date_on
from medical.utils import short_date
from notifications.models import Notification
from notifications.utils import (
    EMAIL_FAILED,
    EMAIL_NO_ADDRESS,
    EMAIL_OFF,
    EMAIL_SENT,
    _should_send_email,
    compact_message,
    notify,
)
from pets.models import Pet, PetOwner
from pets.utils import owner_account_state

from .forms import SERVICE_STATUS_CHOICES, billable_services, peso, service_options
from .models import BillingItem, BillingReceipt, Service

# Shown whenever a write is refused because receipt.is_locked
LOCKED_MESSAGE = "This bill is locked and can't be changed."
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


def save_discount(form):
    """Saves a valid BillingReceiptForm's three discount fields (and
    updated_at) only, then recalculates the totals."""
    receipt = form.instance
    for name in DISCOUNT_FIELDS:
        setattr(receipt, name, form.cleaned_data[name])
    receipt.save(update_fields=[*DISCOUNT_FIELDS, "updated_at"])
    receipt.calculate_totals()
    return receipt


DISCOUNT_SAVED = "Discount saved."
DISCOUNT_REMOVED = "Discount removed."


def discount_saved_message(form):
    """The flash after a valid Discount dialog: "Discount removed." when
    No discount was posted."""
    return DISCOUNT_SAVED if form.cleaned_data["discount_type"] else DISCOUNT_REMOVED


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


def receipt_paid_title(receipt):
    """The owner notification's short title: "Bill paid: ₱1,450.00"."""
    return f"Bill paid: {peso(receipt.total_amount)}"


def receipt_paid_compact(receipt, today):
    """The compact message: "Bantay · HV-2026-0012. Paid on Fri, Nov 6."
    (no pet: "HV-2026-0012. Paid on Fri, Nov 6."; the year only when the
    payment date isn't in this year)."""
    detail = (
        f"{receipt.receipt_number}. "
        f"Paid on {short_date(receipt.payment_date, today)}."
    )
    return compact_message(receipt.pet.name if receipt.pet else "", detail)


def receipt_paid_message(receipt, today):
    """ "Paid ₱1,450.00 on Fri, Nov 6. You can view or print it in Bills."
    (the year only when the payment date isn't in this year). The email body."""
    return (
        f"Paid {peso(receipt.total_amount)} on "
        f"{short_date(receipt.payment_date, today)}. "
        "You can view or print it in Bills."
    )


def receipt_email_subject(receipt):
    """The email subject, which names the pet: "Bill HV-2026-0012 for
    Bantay — Hapi Vet" ("Bill HV-2026-0012 — Hapi Vet" with no pet). Shared
    by the paid email and the send / remind emails."""
    for_pet = f" for {receipt.pet.name}" if receipt.pet else ""
    return f"Bill {receipt.receipt_number}{for_pet} — Hapi Vet"


def notify_receipt_paid(receipt):
    """The owner's one billing notification, sent when a receipt is marked
    paid (rule 24; nothing is sent on create). Call after the commit."""
    if not (receipt.owner and receipt.owner.user):
        return
    title = receipt_paid_title(receipt)
    today = timezone.localdate()
    notify(
        recipient=receipt.owner.user,
        notification_type="billing_generated",
        title=title,
        message=receipt_paid_compact(receipt, today),
        related_billing=receipt,
        related_pet=receipt.pet,
        email_subject=receipt_email_subject(receipt),
        email_body=receipt_paid_message(receipt, today),
    )


# ── Send bill / Remind to pay (admin bill page) ──────────────────────────────

BILL_SEND_TYPES = (Notification.BILL_SENT, Notification.BILL_REMINDER)
# A second send inside this many seconds is taken for a double click
BILL_SEND_GAP_SECONDS = 60
ALREADY_SENT = "Already sent a moment ago."
PAY_IN_CASH = "Please pay in cash at the clinic."
# Owner account states (pets.utils.owner_account_state) a bill can go to
SENDABLE_STATES = ("active", "unclaimed")

# What notify() did about the email -> (message level, end of the flash)
BILL_FLASH_ENDINGS = {
    EMAIL_SENT: (messages.SUCCESS, "by email and in the app."),
    EMAIL_OFF: (
        messages.SUCCESS,
        "in the app only (the Bills email switch is off).",
    ),
    EMAIL_NO_ADDRESS: (messages.SUCCESS, "in the app only (no email on file)."),
    EMAIL_FAILED: (
        messages.WARNING,
        "in the app. The email did not go through.",
    ),
}


def last_bill_send(receipt):
    """The newest BILL_SENT / BILL_REMINDER notification for this bill, or
    None when it was never sent. One query; "Last sent" reads its created_at."""
    return (
        Notification.objects.filter(
            related_billing=receipt, notification_type__in=BILL_SEND_TYPES
        )
        .order_by("-created_at", "-pk")
        .first()
    )


def bill_reach_line(owner, state, email_on):
    """The send dialog's line saying how the owner will be reached."""
    name = owner.full_name
    if not email_on:
        return (
            f"{name} will get a notification in the app only "
            "(they turned off bill emails)."
        )
    email = owner.user.email
    if state == "unclaimed":
        return f"{name} hasn't logged in yet, so they'll get this by email at {email}."
    return f"{name} will get a notification in the app and an email at {email}."


def bill_first_send_line(owner, state):
    """The helper line under the button before any send."""
    name = owner.full_name
    if state == "unclaimed":
        return f"Not sent yet. {name} hasn't logged in yet, so this goes by email."
    return f"Not sent yet. {name} can already see it in their Bills page."


def bill_send_context(receipt, items):
    """
    Context keys for the Send bill / Remind button, the yellow box and the
    dialog of an Unpaid receipt (RECEIPT_PAGE_RELATED selected). The newest
    send is read for every Unpaid bill (one query), so the page's query
    count does not change when the first item is added. The owner's account
    state (an extra query only for "unclaimed") is read only when the bill
    has items.
    """
    owner = receipt.owner
    last = last_bill_send(receipt)
    state = ""
    if items and owner and owner.user:
        state = owner_account_state(owner)
    can_send = state in SENDABLE_STATES
    context = {
        "send_state": state,
        "can_send_bill": can_send,
        "send_no_email": state == "no_email",
        "send_last": last.created_at if last else None,
    }
    if can_send:
        email_on = _should_send_email(owner.user, Notification.BILL_SENT)
        context["send_reach"] = bill_reach_line(owner, state, email_on)
        context["send_first_line"] = bill_first_send_line(owner, state)
    return context


def bill_send_refusal(receipt, last, now):
    """Why the bill can't be sent now (an error flash), or None. `last` is
    last_bill_send(receipt). The receipt is Unpaid (lock_pending_receipt)."""
    owner = receipt.owner
    if not receipt.billing_items.exists():
        return NO_ITEMS_MESSAGE
    if owner is None or owner.user is None:
        return "This bill has no owner to send it to."
    state = owner_account_state(owner)
    if state == "archived":
        return f"{owner.full_name} is archived, so the bill can't be sent."
    if state == "no_email":
        return f"No email is saved for {owner.full_name}, so the bill can't be sent."
    if last and (now - last.created_at).total_seconds() < BILL_SEND_GAP_SECONDS:
        return ALREADY_SENT
    return None


def bill_send_title(kind, receipt):
    """ "Bill ready: ₱650.00" or "Reminder: bill to pay ₱650.00"."""
    total = peso(receipt.total_amount)
    if kind == Notification.BILL_SENT:
        return f"Bill ready: {total}"
    return f"Reminder: bill to pay {total}"


def bill_send_email_body(kind, receipt):
    """The email body: the total and the cash line; a reminder starts by
    saying it is a reminder."""
    text = f"Your bill is {peso(receipt.total_amount)}. {PAY_IN_CASH}"
    if kind == Notification.BILL_REMINDER:
        return f"This is a reminder about your bill.\n\n{text}"
    return text


def bill_send_flash(kind, owner, notification):
    """(message level, text) saying how the bill reached the owner, from
    what notify() did (email_outcome)."""
    level, ending = BILL_FLASH_ENDINGS[notification.email_outcome]
    lead = "Bill sent" if kind == Notification.BILL_SENT else "Reminder sent"
    return level, f"{lead} to {owner.full_name} {ending}"


def send_bill(receipt):
    """
    Send the bill to its owner, or remind them when it was sent before.
    Call inside transaction.atomic() after lock_pending_receipt. Returns
    (message level, text): an error when the bill can't be sent now, else
    how it reached the owner. Writes BILL_SENT (first time) or BILL_REMINDER
    through notify(), with related_billing and related_pet, never
    related_appointment (the visit would win over the bill).
    """
    last = last_bill_send(receipt)
    refusal = bill_send_refusal(receipt, last, timezone.now())
    if refusal:
        return messages.ERROR, refusal
    kind = Notification.BILL_REMINDER if last else Notification.BILL_SENT
    owner = receipt.owner
    pet_name = receipt.pet.name if receipt.pet else ""
    notification = notify(
        recipient=owner.user,
        notification_type=kind,
        title=bill_send_title(kind, receipt),
        message=compact_message(pet_name, f"{receipt.receipt_number}. {PAY_IN_CASH}"),
        related_billing=receipt,
        related_pet=receipt.pet,
        email_subject=receipt_email_subject(receipt),
        email_body=bill_send_email_body(kind, receipt),
        recipient_email=owner.user.email,
    )
    return bill_send_flash(kind, owner, notification)


# Owners see Unpaid and Paid receipts, never Cancelled ones (rule 23)
OWNER_RECEIPT_STATUSES = (BillingReceipt.PENDING, BillingReceipt.PAID)


def bill_has_items():
    """An Exists() that is true for a receipt with at least one item. One
    rule for the owner pages and the admin Income "Not paid yet" box."""
    return Exists(BillingItem.objects.filter(receipt=OuterRef("pk")))


def owner_receipts(owner):
    """The receipts this owner can see: their own, Unpaid or Paid. An Unpaid
    one only once it has an item (the clinic may still be filling it in); the
    check is part of the same query. Every owner page reads receipts here."""
    return BillingReceipt.objects.filter(
        owner=owner, payment_status__in=OWNER_RECEIPT_STATUSES
    ).filter(Q(payment_status=BillingReceipt.PAID) | bill_has_items())


def owner_visit_bill(owner, appointment):
    """(receipt, preparing) for the owner's visit page, from one query.
    receipt is what owner_receipts allows (Unpaid with an item, or Paid), else
    None. preparing is True for an Unpaid bill with no items yet: the page
    says the bill is being prepared. Cancelled or missing: (None, False)."""
    receipt = (
        BillingReceipt.objects.filter(
            owner=owner,
            appointment=appointment,
            payment_status__in=OWNER_RECEIPT_STATUSES,
        )
        .annotate(has_items=bill_has_items())
        .first()
    )
    if receipt is None:
        return None, False
    if receipt.payment_status == BillingReceipt.PAID or receipt.has_items:
        return receipt, False
    return None, True


# ── Owner bills page (Slice 8a-2a) ───────────────────────────────────────────

OWNER_BILLS_PAGE_SIZE = 25


def owner_period_options(today):
    """The History "Show" options as (value, label), All time first."""
    return [
        ("all", "All time"),
        ("month", "This month"),
        ("3m", "Last 3 months"),
        ("year", f"This year ({today.year})"),
        ("last_year", f"Last year ({today.year - 1})"),
    ]


def _months_ago(day, months):
    """The date `months` calendar months before day (the day number is cut to
    the month's length: May 31 minus 3 months is Feb 28)."""
    year, month_index = divmod(day.year * 12 + day.month - 1 - months, 12)
    last = calendar.monthrange(year, month_index + 1)[1]
    return datetime.date(year, month_index + 1, min(day.day, last))


def _paid_period_filter(period, today):
    """A Q on paid_on for one period value ("all" keeps everything)."""
    if period == "month":
        first = today.replace(day=1)
        last = first.replace(day=calendar.monthrange(first.year, first.month)[1])
        return Q(paid_on__range=(first, last))
    if period == "3m":
        return Q(paid_on__gt=_months_ago(today, 3))
    if period in ("year", "last_year"):
        year = today.year - (period == "last_year")
        first, last = datetime.date(year, 1, 1), datetime.date(year, 12, 31)
        return Q(paid_on__range=(first, last))
    return Q()


def _bills_history(owner, q, period, today):
    """Paid receipts, newest paid first, narrowed by search and period.
    Every word in q must match the receipt number, the pet's name or one of
    the items' descriptions (case-insensitive)."""
    rows = (
        owner_receipts(owner)
        .filter(payment_status=BillingReceipt.PAID)
        .select_related("pet")
        # Paid date; the billing date for a receipt with no payment date
        .annotate(paid_on=Coalesce("payment_date", "billing_date"))
        .filter(_paid_period_filter(period, today))
    )
    for word in q.split():
        # A subquery (not a join on items) keeps one row per receipt
        with_item = BillingItem.objects.filter(description__icontains=word)
        rows = rows.filter(
            Q(receipt_number__icontains=word)
            | Q(pet__name__icontains=word)
            | Q(pk__in=with_item.values("receipt_id"))
        )
    return rows.order_by("-paid_on", "-pk")


def owner_bill_groups(receipts):
    """The history rows (each with paid_on) under month headings
    ("October 2026"), in their order."""
    groups = []
    for _key, rows in groupby(receipts, key=lambda r: r.paid_on.strftime("%Y-%m")):
        rows = list(rows)
        label = date_format(rows[0].paid_on, "F Y")
        groups.append({"label": label, "receipts": rows})
    return groups


def owner_bills_context(owner, params, today):
    """
    Context for the owner's Bills page. to_pay is every unpaid receipt, oldest
    first, never filtered (with to_pay_count and to_pay_total). history is the
    page of paid receipts, newest paid first, narrowed by ?q= and ?period=
    (unknown period → all), 25 a page, and history_groups the same rows under
    month headings. has_history is whether the owner has any paid receipt at
    all, whatever the filter; has_bills adds the unpaid ones. receipts (every
    visible receipt, as the old page reads it) stays. Cancelled receipts appear
    nowhere. Three queries however many receipts: unpaid, history count,
    history page (a filter with no match adds one more to see whether any bill
    is paid at all).
    """
    q = params.get("q", "").strip()
    period = params.get("period")
    if period not in {value for value, _ in owner_period_options(today)}:
        period = "all"

    to_pay = list(
        owner_receipts(owner)
        .filter(payment_status=BillingReceipt.PENDING)
        .select_related("pet")
        .order_by("billing_date", "created_at", "pk")
    )
    page_obj = Paginator(
        _bills_history(owner, q, period, today), OWNER_BILLS_PAGE_SIZE
    ).get_page(params.get("page"))
    kept = {"q": q, "period": "" if period == "all" else period}
    page_query = urlencode({name: v for name, v in kept.items() if v})
    is_filtering = bool(q) or period != "all"
    has_history = bool(page_obj.paginator.count) or (
        is_filtering and _bills_history(owner, "", "all", today).exists()
    )
    return {
        "receipts": owner_receipts(owner)
        .select_related("pet")
        .order_by("-billing_date", "-created_at"),
        "to_pay": to_pay,
        "to_pay_count": len(to_pay),
        "to_pay_total": sum((r.total_amount for r in to_pay), Decimal("0")),
        "history": list(page_obj),
        "history_groups": owner_bill_groups(page_obj),
        "has_history": has_history,
        "has_bills": bool(to_pay) or has_history,
        "page_obj": page_obj,
        "q": q,
        "period": period,
        "period_options": owner_period_options(today),
        "is_filtering": is_filtering,
        "today": today,
        **paging_links(page_obj, page_query),
    }


def owner_discount_text(receipt):
    """What goes in the brackets after "Discount" on the owner's receipt:
    "Loyal client, 10%", "10%", a peso discount's reason, or "" (no discount)."""
    if not receipt.has_discount:
        return ""
    if receipt.discount_type != BillingReceipt.PERCENTAGE:
        return receipt.discount_note
    percent = f"{_plain_number(receipt.discount_value)}%"
    note = receipt.discount_note
    return f"{note}, {percent}" if note else percent


def owner_receipt_visit_line(visit):
    """ "Consultation · Nov 2, 3:00 PM" for the visit a receipt is for, or ""."""
    if visit is None:
        return ""
    service = visit.service.service_name if visit.service else "Visit"
    when = f"{date_format(visit.date, 'M j')}, {date_format(visit.time, 'g:i A')}"
    return f"{service} · {when}"


def owner_receipt_context(receipt):
    """
    Context for the owner's receipt page. receipt comes from
    owner_receipts() with pet, owner and the visit (and its service) loaded.
    The items are one query. is_paid shows the paid banner and Print; the
    visit line is "" when the receipt has no visit. discount_label is the
    printout's wording, discount_text the page's.
    """
    return {
        "receipt": receipt,
        "items": list(receipt_items(receipt)),
        "is_paid": receipt.payment_status == BillingReceipt.PAID,
        "discount_label": discount_label(receipt),
        "discount_text": owner_discount_text(receipt),
        "visit": receipt.appointment,
        "visit_line": owner_receipt_visit_line(receipt.appointment),
    }


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

RECEIPT_EXISTS = "A bill already exists for this visit."
RECEIPT_VISIT_STATUSES = (Appointment.CONFIRMED, Appointment.COMPLETED)
# Services that can be billed (the item form's "What is it?" list)
BILLABLE_SERVICE_STATUSES = (Service.ACTIVE, Service.UNLISTED)
# The "Not for a specific pet" radio's value
NO_PET = "none"
PICK_PET_MESSAGE = "Choose a pet, or “Not for a specific pet”."
RECEIPT_NEW_TEMPLATE = "admin/billing/receipt_new.html"
# Step 1's results alone, for the filter-as-you-type request
RECEIPT_OWNER_RESULTS_TEMPLATE = "admin/billing/_receipt_owner_results.html"


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
        message = "A bill can only be made for a confirmed or completed visit."
    elif appointment.date > today:
        message = (
            "This visit hasn't happened yet. Make the bill on or after "
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


# What start_visit_bill did (see appointments' admin_appointment_complete)
BILL_STARTED = "started"
BILL_EXISTS = "exists"
BILL_ARCHIVED = "archived"


def start_visit_bill(appointment):
    """
    Starts the bill of a visit that was just marked Completed, by the same
    rules as New receipt from a visit (Unpaid, today, cash; a fixed-price
    Offered / Clinic use only service as the first item). Call inside
    transaction.atomic(). Returns (outcome, name):
      BILL_EXISTS   the visit already has a bill (any status): nothing made;
      BILL_ARCHIVED nothing made, the owner (first) or pet is archived: name;
      BILL_STARTED  a bill was made (or another save made it a moment ago
                    and is kept, so a second request never makes two).
    """
    if visit_receipt(appointment):
        return BILL_EXISTS, ""
    if appointment.owner.is_archived:
        return BILL_ARCHIVED, appointment.owner.full_name
    if appointment.pet.is_archived:
        return BILL_ARCHIVED, appointment.pet.name
    _receipt, created = create_visit_receipt(appointment, visit_service(appointment))
    return (BILL_STARTED if created else BILL_EXISTS), ""


def _receipts_back():
    return {"back_url": reverse("admin_receipt_list"), "back_label": "Back to bills"}


def visit_receipt_context(appointment, service, today):
    """Mode "visit": the summary card and the service checkbox. today (the
    view's Manila date) words the visit's date."""
    return {
        "mode": "visit",
        "appointment": appointment,
        "visit_service": service,
        "today": today,
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
        # What error_summary.html reads (one radio group, one error)
        "error_summary": {"error_count": 1 if pet_error else 0},
        "change_owner_url": reverse("admin_receipt_create"),
        **_receipts_back(),
    }


# ── Receipt list (admin_receipt_list, Slice 6a-3) ────────────────────────────

RECEIPT_TABS = ("pending", "paid", "cancelled", "all")
RECEIPT_DEFAULT_TAB = "pending"
RECEIPT_PAGE_SIZE = 25
# Unpaid oldest first; Paid newest paid first; Cancelled and All newest first
RECEIPT_TAB_ORDER = {
    "pending": ("created_at", "pk"),
    "paid": ("-payment_date", "-created_at", "-pk"),
    "cancelled": ("-created_at", "-pk"),
    "all": ("-created_at", "-pk"),
}


def search_receipts(receipts, q):
    """Every word in q must match the receipt number (any part: "0012"
    finds HV-2026-0012), the owner's first or last name or the pet's name."""
    for word in q.split():
        receipts = receipts.filter(
            Q(receipt_number__icontains=word)
            | Q(owner__first_name__icontains=word)
            | Q(owner__last_name__icontains=word)
            | Q(pet__name__icontains=word)
        )
    return receipts


def _receipt_counts():
    """The four tab counts in one query (the search doesn't change them)."""
    return BillingReceipt.objects.aggregate(
        pending_count=Count("pk", filter=Q(payment_status=BillingReceipt.PENDING)),
        paid_count=Count("pk", filter=Q(payment_status=BillingReceipt.PAID)),
        cancelled_count=Count("pk", filter=Q(payment_status=BillingReceipt.CANCELLED)),
        all_count=Count("pk"),
    )


def receipt_list_context(params, today):
    """
    Context for the admin receipt list from the request's GET params:
    ?status=pending|paid|cancelled|all (anything else → pending, so the
    dashboard's ?status=pending keeps working), ?q= and ?page=. The search
    stays inside the tab; the counts ignore it. The pager keeps status and q.
    """
    status = params.get("status")
    active_tab = status if status in RECEIPT_TABS else RECEIPT_DEFAULT_TAB
    q = " ".join(params.get("q", "").split())
    counts = _receipt_counts()

    rows = BillingReceipt.objects.all()
    if active_tab != "all":
        rows = rows.filter(payment_status=active_tab)
    rows = (
        search_receipts(rows, q)
        .select_related("owner", "owner__user", "pet", "appointment")
        .annotate(item_count=Count("billing_items"))
        .order_by(*RECEIPT_TAB_ORDER[active_tab])
    )
    page_obj = Paginator(rows, RECEIPT_PAGE_SIZE).get_page(params.get("page"))
    page_query = urlencode({"status": active_tab, **({"q": q} if q else {})})
    return {
        "page_obj": page_obj,
        "receipts": page_obj.object_list,
        "q": q,
        "is_searching": bool(q),
        "active_tab": active_tab,
        **counts,
        **paging_links(page_obj, page_query),
        "today": today,
        # Old keys no template reads now (the old list's "All" tab was the
        # empty status); the billing tests still read them
        "status_filter": "" if active_tab == "all" else active_tab,
        "counts": {
            "all": counts["all_count"],
            "pending": counts["pending_count"],
            "paid": counts["paid_count"],
            "cancelled": counts["cancelled_count"],
        },
    }


# ── Receipt page (admin_receipt_detail, Slice 6a-3) ──────────────────────────

RECEIPT_PAGE_RELATED = (
    "owner__user",
    # Read by the send dialog (the Bills email switch) without an extra query
    "owner__user__notification_preference",
    "pet",
    "appointment",
)
# Kept as a name here; the labels live on the model (status_label)
RECEIPT_STATUS_LABELS = BillingReceipt.STATUS_LABELS


def receipt_for_page(pk):
    """The receipt with what its page shows loaded (404 if missing)."""
    return get_object_or_404(
        BillingReceipt.objects.select_related(*RECEIPT_PAGE_RELATED), pk=pk
    )


def _plain_number(value):
    """10.00 → "10", 12.50 → "12.5"."""
    text = f"{value:f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def discount_label(receipt):
    """What follows "Discount" on the receipt: a percentage is "10%" or
    "10% · {note}"; a peso discount is just its note, or "" (the amount
    column already shows the capped amount). "" with no discount."""
    if not receipt.has_discount:
        return ""
    if receipt.discount_type != BillingReceipt.PERCENTAGE:
        return receipt.discount_note
    label = f"{_plain_number(receipt.discount_value)}%"
    return f"{label} · {receipt.discount_note}" if receipt.discount_note else label


def items_with_totals(receipt, items):
    """Gives each item amount, from_price_list (it has a service) and
    total_if_removed: the receipt's total without it, with the same
    discount rules (BillingReceipt.total_for)."""
    subtotal = sum((item.subtotal for item in items), Decimal("0"))
    for item in items:
        item.amount = item.subtotal
        item.from_price_list = item.service_id is not None
        item.total_if_removed = receipt.total_for(subtotal - item.subtotal)
    return items


def receipt_detail_context(receipt, today):
    """
    Context for the admin receipt page (6b-2; owner_name is its one added
    key, "The owner" when the owner is gone). receipt needs
    RECEIPT_PAGE_RELATED selected (receipt_for_page). Keeps the old
    template's keys (receipt, items, clinic_settings).
    """
    items = items_with_totals(receipt, list(receipt_items(receipt)))
    owner = receipt.owner
    is_pending = receipt.payment_status == BillingReceipt.PENDING
    send = bill_send_context(receipt, items) if is_pending else {}
    return {
        **send,
        "receipt": receipt,
        "status_label": receipt.status_label,
        "is_pending": is_pending,
        "can_mark_paid": is_pending and bool(items),
        "items": items,
        "item_count": len(items),
        "subtotal": receipt.subtotal,
        "discount_amount": receipt.discount_amount,
        "has_discount": receipt.has_discount,
        "discount_label": discount_label(receipt),
        "total": receipt.total_amount,
        "owner": owner,
        # For the page's sentences ("when {owner_name} pays in cash")
        "owner_name": owner.full_name if owner else "The owner",
        "owner_has_email": bool(owner and owner.user and owner.user.email),
        "owner_phone": owner.contact_number if owner else "",
        "pet": receipt.pet,
        "appointment": receipt.appointment,
        "created_at": receipt.created_at,
        "payment_date": receipt.payment_date,
        "clinic_settings": get_clinic_settings(),
        "service_options": service_options(billable_services()),
        "today": today,
    }


# ── Income summary (admin_income, batch I-1) ─────────────────────────────────
# Read-only. Money comes only from Paid bills, counted on the day they were
# paid (payment_date, else billing_date). Nothing here writes a row.

INCOME_KINDS = ("day", "week", "month", "year")
INCOME_FIRST_DAY = datetime.date(2000, 1, 1)
MONEY = DecimalField(max_digits=14, decimal_places=2)
NO_MONEY = Value(Decimal("0.00"), output_field=MONEY)
# (kind, is the period the current one) -> the word for the marked bar
INCOME_MARKED_WORDS = {
    ("day", True): "Today",
    ("day", False): "This day",
    ("week", True): "Today",
    ("week", False): "Today",
    ("month", True): "Today",
    ("month", False): "Selected",
    ("year", True): "So far",
    ("year", False): "Selected",
}


def paid_bills():
    """Every PAID bill with paid_day (a date): payment_date, else
    billing_date. Unpaid and Cancelled bills never count."""
    return BillingReceipt.objects.filter(payment_status=BillingReceipt.PAID).annotate(
        paid_day=Coalesce("payment_date", "billing_date")
    )


def _money_sum(expression, **options):
    """Sum() that is Decimal("0.00") instead of None when nothing matches."""
    return Coalesce(
        Sum(expression, output_field=MONEY, **options), NO_MONEY, output_field=MONEY
    )


def _bills_words(count):
    """ "1 bill" / "6 bills"."""
    return f"{count} bill" if count == 1 else f"{count} bills"


def _income_url(kind, day):
    return f"{reverse('admin_income')}?{urlencode({'period': kind, 'date': day})}"


def _income_start(kind, day):
    """The first day of the period that holds day."""
    if kind == "week":
        return day - datetime.timedelta(days=day.weekday())
    if kind == "month":
        return day.replace(day=1)
    if kind == "year":
        return day.replace(month=1, day=1)
    return day


def _income_end(kind, start):
    """The last day (inclusive) of the period that starts on start."""
    if kind == "week":
        return start + datetime.timedelta(days=6)
    if kind == "month":
        return start.replace(day=calendar.monthrange(start.year, start.month)[1])
    if kind == "year":
        return start.replace(month=12, day=31)
    return start


def _income_shift(kind, start, step):
    """The start of the period `step` (-1 or 1) periods from start."""
    if kind == "week":
        return start + datetime.timedelta(weeks=step)
    if kind == "month":
        year, month_index = divmod(start.year * 12 + start.month - 1 + step, 12)
        return datetime.date(year, month_index + 1, 1)
    if kind == "year":
        return start.replace(year=start.year + step)
    return start + datetime.timedelta(days=step)


def _income_date(value, today):
    """The ?date= as a date; today for a missing or bad value, a date after
    today or one before 2000-01-01."""
    try:
        day = datetime.date.fromisoformat(value)
    except (TypeError, ValueError):
        return today
    return day if INCOME_FIRST_DAY <= day <= today else today


def _short_day(day, today):
    """ "Nov 23" (the year too when it isn't this year: "Nov 23, 2025")."""
    text = date_format(day, "M j")
    return text if day.year == today.year else f"{text}, {day.year}"


def _range_words(start, end, today):
    """ "Nov 23 to 29", "Nov 30 to Dec 6" (the year only when it isn't this
    year; a range across two years names both)."""
    if start.year != end.year:
        return f"{_short_day(start, today)} to {_short_day(end, today)}"
    if start.month == end.month:
        words = f"{date_format(start, 'M j')} to {end.day}"
    else:
        words = f"{date_format(start, 'M j')} to {date_format(end, 'M j')}"
    return words if start.year == today.year else f"{words}, {start.year}"


def _day_words(p, today):
    label = long_date_on(p["start"], today)
    if p["is_current"]:
        return label, "Today", "Yesterday"
    before = friendly_date_on(p["compare_start"], today)
    when = friendly_date_on(p["start"], today)
    return label, when, f"The day before ({before})"


def _week_words(p, today):
    words = _range_words(p["start"], p["end"], today)
    if p["is_current"]:
        return f"This week: {words}", "This week", "Last week"
    before = _range_words(p["compare_start"], p["compare_end"], today)
    when = f"Week of {_short_day(p['start'], today)}"
    return words, when, f"The week before ({before})"


def _month_words(p, today):
    name = date_format(p["start"], "F Y")
    if p["is_current"]:
        return f"This month: {name}", "This month", "Last month"
    before = date_format(p["compare_start"], "F Y")
    return name, name, f"The month before ({before})"


def _year_words(p, today):
    year = p["start"].year
    if p["is_current"]:
        return f"This year: {year}", f"{year} so far", "Last year"
    return str(year), str(year), f"The year before ({p['compare_start'].year})"


INCOME_WORDS = {
    "day": _day_words,
    "week": _week_words,
    "month": _month_words,
    "year": _year_words,
}


def income_period(params, today):
    """
    The period from the request's ?period=day|week|month|year (anything else
    is day) and ?date= (ISO; a bad value, a date after today or before
    2000-01-01 is today; then moved to the period's start, a week starting
    on Monday). A dict: kind, date (the date asked for, before it was moved),
    start, end (inclusive), is_current, prev_start (None before 2000-01-01),
    next_start (None after today), label, when, compare_label and the
    previous period's compare_start / compare_end. No queries.
    """
    kind = params.get("period")
    if kind not in INCOME_KINDS:
        kind = "day"
    asked = _income_date(params.get("date"), today)
    start = _income_start(kind, asked)
    end = _income_end(kind, start)
    before = _income_shift(kind, start, -1)
    after = _income_shift(kind, start, 1)
    period = {
        "kind": kind,
        "date": asked,
        "start": start,
        "end": end,
        "is_current": start <= today <= end,
        "prev_start": before if before >= INCOME_FIRST_DAY else None,
        "next_start": after if after <= today else None,
        "compare_start": before,
        "compare_end": _income_end(kind, before),
    }
    label, when, compare_label = INCOME_WORDS[kind](period, today)
    return {**period, "label": label, "when": when, "compare_label": compare_label}


def income_totals(start, end, compare_start, compare_end):
    """
    The period's numbers and the previous period's, in ONE query: total,
    count, visits (total of bills with a visit), discount (what was taken
    off), discount_count, over_counter (total - visits), compare_total and
    compare_count. Sums are Decimal and never None.
    """
    here = Q(paid_day__range=(start, end))
    before = Q(paid_day__range=(compare_start, compare_end))
    totals = (
        paid_bills()
        .filter(paid_day__range=(min(start, compare_start), end))
        .aggregate(
            total=_money_sum("total_amount", filter=here),
            count=Count("pk", filter=here),
            visits=_money_sum(
                "total_amount", filter=here & Q(appointment__isnull=False)
            ),
            discount=_money_sum(F("subtotal") - F("total_amount"), filter=here),
            discount_count=Count("pk", filter=here & Q(subtotal__gt=F("total_amount"))),
            compare_total=_money_sum("total_amount", filter=before),
            compare_count=Count("pk", filter=before),
        )
    )
    totals["over_counter"] = totals["total"] - totals["visits"]
    return totals


def income_compare_line(period, totals):
    """ "Yesterday: ₱4,850.00 from 6 bills" or "Yesterday: no bills paid"."""
    label, count = period["compare_label"], totals["compare_count"]
    if not count:
        return f"{label}: no bills paid"
    return f"{label}: {peso(totals['compare_total'])} from {_bills_words(count)}"


def income_rows(period, today):
    """
    One row for every day (week, month; the Day view gives the week that
    holds the day) or every month (year) of the range, in order, empty ones
    too, from ONE grouped query. Each row: day, label, short, count, total,
    state ("future" after today, "marked" for today's row, the looked-at day
    in the Day view, else "") and url (None for a future row).
    """
    kind = period["kind"]
    if kind == "day":
        first = _income_start("week", period["start"])
        last = _income_end("week", first)
    else:
        first, last = period["start"], period["end"]
    by_month = kind == "year"
    found = {
        row["bucket"]: row
        for row in paid_bills()
        .filter(paid_day__range=(first, last))
        .annotate(bucket=TruncMonth("paid_day") if by_month else F("paid_day"))
        .values("bucket")
        .annotate(count=Count("pk"), total=Sum("total_amount"))
        .order_by()
    }
    if by_month:
        days = [datetime.date(first.year, month, 1) for month in range(1, 13)]
        marked, link_kind = today.replace(day=1), "month"
    else:
        span = (last - first).days + 1
        days = [first + datetime.timedelta(days=n) for n in range(span)]
        marked = period["start"] if kind == "day" else today
        link_kind = "day"
    rows = []
    for day in days:
        hit = found.get(day)
        future = day > today
        state = "future" if future else "marked" if day == marked else ""
        rows.append(
            {
                "day": day,
                "label": date_format(day, "F" if by_month else "D, M j"),
                "short": date_format(day, "M" if by_month else "D"),
                "count": hit["count"] if hit else 0,
                "total": hit["total"] if hit else Decimal("0.00"),
                "state": state,
                "url": None if future else _income_url(link_kind, day.isoformat()),
            }
        )
    return rows


def _chart_top(maximum):
    """A round number above the highest bar: ceil(max / 10^k * 2) / 2 * 10^k,
    k = digits of max - 1 (4,850 -> 5,000; 66,900 -> 70,000; 1,200 -> 1,500).
    1,000 when everything is 0."""
    if maximum <= 0:
        return Decimal("1000")
    scale = Decimal(10) ** (len(str(int(maximum))) - 1 if maximum >= 1 else 0)
    return max(Decimal(math.ceil(maximum / scale * 2)) / 2 * scale, Decimal("1"))


def _chart_pct(total, top):
    """A bar's height, a whole number 0 to 100."""
    return min(round(total / top * 100), 100)


def _chart_tick(kind, row):
    """The label under a bar: every bar in a week, days 1, 10, 20 and the last
    in a month, Jan Apr Jul Oct in a year, else ""."""
    day = row["day"]
    if kind in ("day", "week"):
        return row["short"]
    if kind == "year":
        return row["short"] if day.month in (1, 4, 7, 10) else ""
    last = calendar.monthrange(day.year, day.month)[1]
    return str(day.day) if day.day in (1, 10, 20, last) else ""


def _chart_words(period, today):
    """(title, sub) of the chart: "This week, day by day" and the dates."""
    kind, start = period["kind"], period["start"]
    if kind == "year":
        return f"{start.year}, month by month", f"January to December {start.year}"
    if kind == "month":
        name = date_format(start, "F")
        name = name if start.year == today.year else f"{name} {start.year}"
        return f"{name}, day by day", _range_words(start, period["end"], today)
    first = _income_start("week", start)
    last = _income_end("week", first)
    this_week = first <= today <= last
    title = "This week, day by day" if this_week else "That week, day by day"
    return title, _range_words(first, last, today)


def income_chart(period, rows, today):
    """
    The chart's numbers, no queries. Adds pct (0 to 100) and tick to every
    row. Returns top, half, best (the row with the most money, the first on a
    tie), average (money of the days or months with payments / how many),
    average_pct (None without an average), with_payments (rows up to today
    with money), so_far (rows up to today), marked_word, title, sub,
    big_title and show_amounts (False for a month: 31 bars are too small).
    """
    kind = period["kind"]
    up_to_today = [row for row in rows if row["state"] != "future"]
    paid = [row for row in up_to_today if row["total"] > 0]
    top = _chart_top(max((row["total"] for row in up_to_today), default=Decimal(0)))
    best = None
    for row in paid:
        if best is None or row["total"] > best["total"]:
            best = row
    average = None
    if paid:
        average = sum((row["total"] for row in paid), Decimal("0")) / len(paid)
    for row in rows:
        counts = row["state"] != "future" and row["total"] > 0
        row["pct"] = _chart_pct(row["total"], top) if counts else 0
        row["tick"] = _chart_tick(kind, row)
    title, sub = _chart_words(period, today)
    return {
        "top": top,
        "half": top / 2,
        "best": best,
        "average": average,
        "average_pct": None if average is None else _chart_pct(average, top),
        "with_payments": len(paid),
        "so_far": len(up_to_today),
        "marked_word": INCOME_MARKED_WORDS[(kind, period["is_current"])],
        "title": title,
        "sub": sub,
        "big_title": f"Money received each {'month' if kind == 'year' else 'day'}",
        "show_amounts": kind != "month",
    }


def income_day_bills(day):
    """The bills paid on day, oldest first, with owner and pet loaded. Each
    gets from_visit (it has a visit) and discount (subtotal - total; shown
    when above 0)."""
    bills = list(
        paid_bills()
        .filter(paid_day=day)
        .select_related("owner", "pet")
        .order_by("created_at", "pk")
    )
    for bill in bills:
        bill.from_visit = bill.appointment_id is not None
        bill.discount = bill.subtotal - bill.total_amount
    return bills


def unpaid_snapshot():
    """The "Not paid yet" box, ONE query: count and total of the Unpaid bills
    that have at least one item (not tied to the period), and the Billing
    Unpaid tab's url."""
    snapshot = (
        BillingReceipt.objects.filter(payment_status=BillingReceipt.PENDING)
        .filter(bill_has_items())
        .aggregate(count=Count("pk"), total=_money_sum("total_amount"))
    )
    snapshot["url"] = f"{reverse('admin_receipt_list')}?status=pending"
    return snapshot


def income_context(params, today):
    """
    Context for the Income page: period, totals, compare_line, rows, chart,
    bills (the Day view only, else None), unpaid, back_to_today_url (None on
    the current period), prev_url / next_url (None when not allowed) and
    period_links (Day, Week, Month, Year for the same date, with is_active).
    At most four queries however many bills: totals, rows, bills (Day only),
    unpaid.
    """
    period = income_period(params, today)
    kind = period["kind"]
    totals = income_totals(
        period["start"], period["end"], period["compare_start"], period["compare_end"]
    )
    rows = income_rows(period, today)
    prev_start, next_start = period["prev_start"], period["next_start"]
    return {
        "period": period,
        "totals": totals,
        "compare_line": income_compare_line(period, totals),
        "rows": rows,
        "chart": income_chart(period, rows, today),
        "bills": income_day_bills(period["start"]) if kind == "day" else None,
        "unpaid": unpaid_snapshot(),
        "back_to_today_url": (
            None
            if period["is_current"]
            else f"{reverse('admin_income')}?{urlencode({'period': kind})}"
        ),
        "prev_url": _income_url(kind, prev_start) if prev_start else None,
        "next_url": _income_url(kind, next_start) if next_start else None,
        "period_links": [
            {
                "kind": name,
                "label": name.capitalize(),
                "url": _income_url(name, period["date"]),
                "is_active": name == kind,
            }
            for name in INCOME_KINDS
        ],
    }


# ── Services list (admin_service_list, Slice 6a-4) ───────────────────────────

# The one-line explanation under each group's title (services_v1.html)
SERVICE_GROUP_TEXT = {
    Service.ACTIVE: ("Pet owners see these when they book. You can put them on bills."),
    Service.UNLISTED: (
        "Hidden from pet owners. You can put them on bills and pick them "
        "for walk-ins."
    ),
    Service.DRAFT: (
        "Not shown anywhere yet. Finish it, then choose Offered or Clinic use only."
    ),
    Service.CLOSED: (
        "Not shown anywhere. Old visits and bills keep their names and prices."
    ),
}


def service_saved_message(service, created):
    """ "Consultation added." / "Consultation saved." """
    return f"{service.service_name} {'added' if created else 'saved'}."


def services_list_context(params):
    """
    Context for the services page: every service in one query, A to Z, in
    groups Offered, Clinic use only, Hidden (draft), No longer offered (key,
    title, text, services, count) and total_count; each service gets
    is_range. Old keys no template reads now but billing tests still do:
    services (filtered by ?status=), status_filter, counts and
    status_choices.
    """
    services = list(Service.objects.order_by(Lower("service_name"), "pk"))
    for service in services:
        # A range with both prices shows "₱300.00 to ₱800.00"; anything else
        # shows its base price (as the receipt's "What is it?" does)
        service.is_range = (
            service.pricing_type == Service.RANGE
            and service.min_price is not None
            and service.max_price is not None
        )
    groups = []
    for key, title in SERVICE_STATUS_CHOICES:
        rows = [service for service in services if service.status == key]
        groups.append(
            {
                "key": key,
                "title": title,
                "text": SERVICE_GROUP_TEXT[key],
                "services": rows,
                "count": len(rows),
            }
        )
    status_filter = params.get("status", "")
    return {
        "groups": groups,
        "total_count": len(services),
        # The old services_list.html's keys (no template uses them now)
        "services": (
            [s for s in services if s.status == status_filter]
            if status_filter
            else services
        ),
        "status_filter": status_filter,
        "counts": {"all": len(services), **{g["key"]: g["count"] for g in groups}},
        "status_choices": Service.STATUS_CHOICES,
    }


# ── Owner — Services and prices ───────────────────────────────────────────────

OWNER_OTHER_SERVICES = "Other services"
ASK_THE_CLINIC = "Ask the clinic"
# ?from= values the page reads: (URL name, words after "Back to")
OWNER_SERVICES_BACK = {
    "clinic": ("clinic_info", "Clinic info"),
    "account": ("owner_account", "Account"),
}


def owner_services_exist():
    """True when at least one service is Offered (status Active)."""
    return Service.objects.filter(status=Service.ACTIVE).exists()


def owner_service_price(service):
    """(words, is_range) for an owner: a range with both prices reads
    "₱150.00 to ₱400.00"; anything else is the base price, or "Ask the
    clinic" when that is 0 (a range missing a price counts as one price)."""
    if (
        service.pricing_type == Service.RANGE
        and service.min_price is not None
        and service.max_price is not None
    ):
        return f"{peso(service.min_price)} to {peso(service.max_price)}", True
    if service.base_price > 0:
        return peso(service.base_price), False
    return ASK_THE_CLINIC, False


def owner_service_groups(services):
    """Offered services as groups [{"heading", "services"}]. The group key is
    the category without outer spaces, ignoring letter case; its heading is
    the spelling of the group's first service (A to Z by name). Groups are A
    to Z by key, a blank category goes last under "Other services", and when
    no service has a category there is one group with an empty heading."""
    cards = []
    for service in sorted(services, key=lambda s: (s.service_name.lower(), s.pk)):
        words, is_range = owner_service_price(service)
        cards.append(
            {
                "category": service.category.strip(),
                "name": service.service_name,
                "price": words,
                "is_range": is_range,
                "description": service.description.strip(),
            }
        )
    named = {}
    others = []
    for card in cards:
        if card["category"]:
            key = card["category"].lower()
            named.setdefault(key, {"heading": card["category"], "services": []})
            named[key]["services"].append(card)
        else:
            others.append(card)
    if not named:
        return [{"heading": "", "services": others}] if others else []
    groups = [named[key] for key in sorted(named)]
    if others:
        groups.append({"heading": OWNER_OTHER_SERVICES, "services": others})
    return groups


def owner_services_context(params):
    """Context for the owner Services and prices page: the groups (one
    query for the list), has_range (shows the blue note), the back bar from
    ?from= (only "clinic" and "account" are read) and, when nothing is
    Offered, the clinic's phone for the Call link."""
    groups = owner_service_groups(Service.objects.filter(status=Service.ACTIVE))
    back_name, back_label = OWNER_SERVICES_BACK.get(
        params.get("from"), OWNER_SERVICES_BACK["account"]
    )
    context = {
        "groups": groups,
        "has_range": any(c["is_range"] for g in groups for c in g["services"]),
        "back_url": reverse(back_name),
        "back_label": back_label,
    }
    if not groups:
        context["phone_link"] = clinic_phone_link(get_clinic_settings())
    return context
