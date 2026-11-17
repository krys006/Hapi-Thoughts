"""Shared billing logic for the receipt views (Slice 6)."""

import calendar
import datetime
from decimal import Decimal
from urllib.parse import urlencode

from django.contrib import messages
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.db.models.functions import Coalesce, Lower
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.text import capfirst

from appointments.models import Appointment
from appointments.utils import (
    WALKIN_PAGE_SIZE,
    WALKIN_SEARCH_LIMIT,
    get_clinic_settings,
    paging_links,
    search_walkin_owners,
    walkin_owner_list,
)
from medical.utils import short_date
from notifications.utils import notify
from pets.models import Pet, PetOwner

from .forms import SERVICE_STATUS_CHOICES, billable_services, peso, service_options
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
    """ "Receipt HV-2026-0012 for Bantay" ("Receipt HV-2026-0012" with no pet)."""
    title = f"Receipt {receipt.receipt_number}"
    return f"{title} for {receipt.pet.name}" if receipt.pet else title


def receipt_paid_message(receipt, today):
    """ "Paid ₱1,450.00 on Fri, Nov 6. You can view or print it in Billing."
    (the year only when the payment date isn't in this year)."""
    return (
        f"Paid {peso(receipt.total_amount)} on "
        f"{short_date(receipt.payment_date, today)}. "
        "You can view or print it in Billing."
    )


def notify_receipt_paid(receipt):
    """The owner's one billing notification, sent when a receipt is marked
    paid (rule 24; nothing is sent on create). Call after the commit."""
    if not (receipt.owner and receipt.owner.user):
        return
    title = receipt_paid_title(receipt)
    notify(
        recipient=receipt.owner.user,
        notification_type="billing_generated",
        title=title,
        message=receipt_paid_message(receipt, timezone.localdate()),
        related_billing=receipt,
        related_pet=receipt.pet,
        email_subject=f"{title} — Hapi Vet",
    )


# Owners see Unpaid and Paid receipts, never Cancelled ones (rule 23)
OWNER_RECEIPT_STATUSES = (BillingReceipt.PENDING, BillingReceipt.PAID)


def owner_receipts(owner):
    """The receipts this owner can see: their own, Unpaid or Paid."""
    return BillingReceipt.objects.filter(
        owner=owner, payment_status__in=OWNER_RECEIPT_STATUSES
    )


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


def owner_bills_context(owner, params, today):
    """
    Context for the owner's Bills page. to_pay is every unpaid receipt, oldest
    first, never filtered (with to_pay_count and to_pay_total). history is the
    page of paid receipts, newest paid first, narrowed by ?q= and ?period=
    (unknown period → all), 25 a page. receipts (every visible receipt, as
    the old page reads it) stays. Cancelled receipts appear nowhere. Three
    queries however many receipts: unpaid, history count, history page.
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
    return {
        "receipts": owner_receipts(owner)
        .select_related("pet")
        .order_by("-billing_date", "-created_at"),
        "to_pay": to_pay,
        "to_pay_count": len(to_pay),
        "to_pay_total": sum((r.total_amount for r in to_pay), Decimal("0")),
        "history": list(page_obj),
        "page_obj": page_obj,
        "q": q,
        "period": period,
        "period_options": owner_period_options(today),
        "is_filtering": bool(q) or period != "all",
        "today": today,
        **paging_links(page_obj, page_query),
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

RECEIPT_EXISTS = "A receipt already exists for this visit."
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

RECEIPT_PAGE_RELATED = ("owner__user", "pet", "appointment")
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
    return {
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


# ── Services list (admin_service_list, Slice 6a-4) ───────────────────────────

# The one-line explanation under each group's title (services_v1.html)
SERVICE_GROUP_TEXT = {
    Service.ACTIVE: (
        "Pet owners see these when they book. You can put them on receipts."
    ),
    Service.UNLISTED: (
        "Hidden from pet owners. You can put them on receipts and pick them "
        "for walk-ins."
    ),
    Service.DRAFT: (
        "Not shown anywhere yet. Finish it, then choose Offered or Clinic use only."
    ),
    Service.CLOSED: (
        "Not shown anywhere. Old visits and receipts keep their names and prices."
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
