import datetime
from itertools import groupby
from urllib.parse import urlencode

from django.db.models import Count, Exists, OuterRef, Prefetch, Q
from django.utils import timezone
from django.utils.dateformat import format as date_format

from notifications.utils import notify
from pets.models import Pet, PetOwner
from .models import ClinicSettings, BlockedDate, Appointment


def get_available_slots(date, walk_in=False, move=False):
    """
    Returns a list of available time slots for a given date.

    A slot is available if:
    1. The date is a working day (per ClinicSettings.working_days)
    2. The date is not blocked (BlockedDate)
    3. The date is within the booking window (today → today + booking_limit_days)
    4. The slot is not already taken by a non-cancelled appointment
       (PENDING requests block it too)
    5. For today (owner rules, see _slot_open_today): none at or after
       the same_day_cutoff_time, otherwise only slots that haven't started

    "Today" and "now" are Manila time.

    walk_in=True (admin walk-in form): rule 5 is replaced by "the slot
    hasn't ended yet", so the slot in progress and later ones are offered.
    move=True (admin Move dialog): rule 5 has no cutoff, only slots that
    haven't started. Rules 1-4 are the same for both.

    Returns a list of datetime.time objects.
    Returns an empty list if the date is unavailable for any reason.
    """
    clinic = ClinicSettings.objects.first()

    # No settings configured yet — no slots available
    if not clinic:
        return []

    # One read of the clock, so the date and time always agree
    now = timezone.localtime()
    today = now.date()

    # Date is in the past
    if date < today:
        return []

    # Date is beyond the booking window
    if date > today + datetime.timedelta(days=clinic.booking_limit_days):
        return []

    # Date is not a working day (weekday() returns 0=Mon, 6=Sun)
    if date.weekday() not in clinic.working_days:
        return []

    # Date is blocked by Admin
    if BlockedDate.objects.filter(date=date).exists():
        return []

    slots = _day_slots(clinic, date)
    slot_delta = datetime.timedelta(minutes=clinic.slot_duration_minutes)

    if date == today and walk_in:
        # Walk-ins: keep slots that end after now (naive, like opening_dt)
        now_naive = now.replace(tzinfo=None)
        slots = [
            s
            for s in slots
            if datetime.datetime.combine(date, s) + slot_delta > now_naive
        ]
    elif date == today:
        slots = [s for s in slots if _slot_open_today(clinic, date, s, now, move)]

    # Remove slots already taken by active (non-cancelled) appointments
    booked_times = set(
        Appointment.objects.filter(
            date=date,
        )
        .exclude(status=Appointment.CANCELLED)
        .values_list("time", flat=True)
    )

    slots = [s for s in slots if s not in booked_times]

    return slots


def _day_slots(clinic, date):
    """Every slot start time between opening and closing time on date."""
    slots = []
    slot_delta = datetime.timedelta(minutes=clinic.slot_duration_minutes)

    # Convert TimeField values to datetime for arithmetic
    opening_dt = datetime.datetime.combine(date, clinic.opening_time)
    closing_dt = datetime.datetime.combine(date, clinic.closing_time)

    current = opening_dt
    while current + slot_delta <= closing_dt:
        slots.append(current.time())
        current += slot_delta
    return slots


def slot_in_progress(date):
    """
    The start time of the slot running right now (start <= now < end,
    Manila time), or None when date isn't today, the clinic is closed at
    this moment or there are no clinic settings. Walk-in step 2 labels
    that slot "Now"; the Move dialog never asks.
    """
    clinic = ClinicSettings.objects.first()
    now = timezone.localtime()
    if not clinic or date != now.date():
        return None

    slot_delta = datetime.timedelta(minutes=clinic.slot_duration_minutes)
    now_naive = now.replace(tzinfo=None)  # naive, like the slot times
    for slot in _day_slots(clinic, date):
        start = datetime.datetime.combine(date, slot)
        if start <= now_naive < start + slot_delta:
            return slot
    return None


def _slot_open_today(clinic, date, slot, now, move=False):
    """
    Rule 5 for a slot today; now is Manila time.
    Owner ("Same-day bookings not allowed after this time"): nothing once
    now reaches the cutoff, otherwise only slots that haven't started yet.
    move=True (admin Move dialog): no cutoff, only slots that haven't started.
    """
    if not move and now.time() >= clinic.same_day_cutoff_time:
        return False
    # Naive, like the slot times
    return datetime.datetime.combine(date, slot) > now.replace(tzinfo=None)


def is_slot_available(date, time, exclude_appointment_pk=None, move=False):
    """
    Checks if a specific date + time slot is available, with the same
    rules as get_available_slots: the owner rules (incl. the same-day
    cutoff), or the Move dialog rules with move=True.
    Used for validation on booking and reschedule forms.

    exclude_appointment_pk: pass the current appointment's pk when
    checking a reschedule so it doesn't conflict with itself.
    """
    clinic = ClinicSettings.objects.first()
    if not clinic:
        return False

    now = timezone.localtime()
    today = now.date()

    if date < today:
        return False

    if date > today + datetime.timedelta(days=clinic.booking_limit_days):
        return False

    if date.weekday() not in clinic.working_days:
        return False

    if BlockedDate.objects.filter(date=date).exists():
        return False

    if date == today and not _slot_open_today(clinic, date, time, now, move):
        return False

    # Check for conflicting appointment
    conflict_qs = Appointment.objects.filter(
        date=date,
        time=time,
    ).exclude(status=Appointment.CANCELLED)

    # When rescheduling, exclude the appointment being rescheduled
    if exclude_appointment_pk:
        conflict_qs = conflict_qs.exclude(pk=exclude_appointment_pk)

    return not conflict_qs.exists()


def _format_calendar_week_label(start, end):
    """
    Formats a week range label for the calendar widget header.
    e.g. 'Jun 15 - 21, 2026' or 'Jun 29 - Jul 05, 2026' if the
    week spans two months.
    """
    if start.month == end.month:
        return f"{start.strftime('%b %d')} - {end.strftime('%d, %Y')}"
    return f"{start.strftime('%b %d')} - {end.strftime('%b %d, %Y')}"


def get_admin_calendar_context(view, date_str):
    """
    Builds the calendar grid context for the admin dashboard calendar
    widget. Used by both the initial dashboard page load and the HTMX
    grid-refresh endpoint, so navigation always produces identical output.

    view: "month" or "week" — defaults to "month" on any other value
    date_str: ISO date string (YYYY-MM-DD) to anchor the grid on,
    falls back to today if missing or invalid.

    Returns a dict of calendar_* keys (prefixed to avoid colliding with
    other dashboard context keys when merged together).
    """
    import calendar

    today = timezone.localdate()

    try:
        anchor_date = datetime.date.fromisoformat(date_str) if date_str else today
    except ValueError:
        anchor_date = today

    clinic = ClinicSettings.objects.first()
    working_days = clinic.working_days if clinic else []

    if view == "week":
        # Monday of the anchor date's week (weekday() returns 0=Mon)
        week_start = anchor_date - datetime.timedelta(days=anchor_date.weekday())
        days = [week_start + datetime.timedelta(days=i) for i in range(7)]
        weeks = [days]
        prev_date = week_start - datetime.timedelta(days=7)
        next_date = week_start + datetime.timedelta(days=7)
        period_label = _format_calendar_week_label(days[0], days[-1])
    else:
        view = "month"  # normalize any unexpected query param value

        cal = calendar.Calendar(firstweekday=0)  # Monday-start
        all_days = list(cal.itermonthdates(anchor_date.year, anchor_date.month))
        weeks = [all_days[i : i + 7] for i in range(0, len(all_days), 7)]

        first_of_month = anchor_date.replace(day=1)
        prev_date = (first_of_month - datetime.timedelta(days=1)).replace(day=1)

        next_month = anchor_date.month + 1
        next_year = anchor_date.year
        if next_month > 12:
            next_month = 1
            next_year += 1
        next_date = datetime.date(next_year, next_month, 1)

        period_label = anchor_date.strftime("%B %Y")

    flat_days = [d for week in weeks for d in week]
    start_range = min(flat_days)
    end_range = max(flat_days)

    # One query for all status counts across the visible grid.
    # Cancelled excluded — dots represent active/relevant activity only;
    # the day-detail panel shows cancelled appointments separately.
    status_rows = (
        Appointment.objects.filter(date__range=[start_range, end_range])
        .exclude(status=Appointment.CANCELLED)
        .values("date", "status")
        .annotate(count=Count("id"))
    )
    status_map = {}
    for row in status_rows:
        status_map.setdefault(row["date"], []).append(
            {"status": row["status"], "count": row["count"]}
        )

    blocked_set = set(
        BlockedDate.objects.filter(date__range=[start_range, end_range]).values_list(
            "date", flat=True
        )
    )

    grid_weeks = []
    for week in weeks:
        week_cells = []
        for d in week:
            week_cells.append(
                {
                    "date": d,
                    "day_number": d.day,
                    # Week view has no "outside period" concept — every
                    # day shown belongs to the selected week.
                    "is_current_period": (
                        True if view == "week" else d.month == anchor_date.month
                    ),
                    "is_today": d == today,
                    "is_working_day": d.weekday() in working_days,
                    "is_blocked": d in blocked_set,
                    "statuses": status_map.get(d, []),
                }
            )
        grid_weeks.append(week_cells)

    return {
        "calendar_view": view,
        "anchor_date": anchor_date,
        "calendar_weeks": grid_weeks,
        "calendar_prev_date": prev_date,
        "calendar_next_date": next_date,
        "calendar_today_date": today,
        "calendar_period_label": period_label,
    }


# ── Admin appointments list ──────────────────────────────────────────────────

LIST_PAGE_SIZE = 25

# Visible tabs, in display order: (id used in ?status=, label)
LIST_TABS = [
    ("pending", "Needs approval"),
    ("upcoming", "Upcoming"),
    ("completed", "Completed"),
    ("cancelled", "Cancelled"),
    ("no_show", "No show"),
    ("all", "All"),
]
# "unfinished" has no tab button; the yellow notice links to it.
_LIST_TAB_IDS = {tab_id for tab_id, _ in LIST_TABS} | {"unfinished"}
_LIST_TAB_ALIASES = {"confirmed": "upcoming"}
# Work queues read oldest first; history tabs read newest first.
_ASCENDING_TABS = {"pending", "upcoming", "unfinished"}


def _tab_filters(today):
    """One Q per tab. Needs the is_moved annotation (see _with_moved_flag)."""
    return {
        "pending": Q(status=Appointment.PENDING),
        "upcoming": Q(status=Appointment.CONFIRMED, date__gte=today),
        "unfinished": Q(status=Appointment.CONFIRMED, date__lt=today),
        "completed": Q(status=Appointment.COMPLETED),
        # Moved originals are cancelled too, but only "all" shows them.
        "cancelled": Q(status=Appointment.CANCELLED, is_moved=False),
        "no_show": Q(status=Appointment.NO_SHOW),
        "all": Q(),
    }


def _with_moved_flag(queryset):
    """
    Adds is_moved: True when an admin reschedule replaced this row.
    A subquery (not a join on rescheduled_to) keeps one row per appointment.
    """
    return queryset.annotate(
        is_moved=Exists(Appointment.objects.filter(rescheduled_from=OuterRef("pk")))
    )


def tab_counts(today):
    """
    Totals for every tab (plus "unfinished") in one aggregate query.
    Search does not change them. "pending" matches the sidebar badge.
    """
    counts = {
        tab: Count("pk", filter=tab_filter or None)
        for tab, tab_filter in _tab_filters(today).items()
    }
    return _with_moved_flag(Appointment.objects.all()).aggregate(**counts)


def resolve_list_tab(status, counts):
    """
    Turns ?status= into a tab id. "confirmed" is an alias of "upcoming".
    Missing or unknown values open "pending" when requests are waiting,
    otherwise "upcoming".
    """
    status = _LIST_TAB_ALIASES.get(status, status)
    if status in _LIST_TAB_IDS:
        return status
    return "pending" if counts["pending"] else "upcoming"


def parse_list_date(value):
    """?date= as a date, or None when missing or not YYYY-MM-DD."""
    try:
        return datetime.date.fromisoformat(value) if value else None
    except ValueError:
        return None


def filter_appointments(tab, today, q="", date=None):
    """
    Rows for one tab, sorted for that tab. Every word in q must match the
    pet name or the owner's first or last name (case-insensitive).
    """
    rows = _with_moved_flag(
        Appointment.objects.select_related("pet", "owner", "owner__user", "service")
    ).filter(_tab_filters(today)[tab])

    for word in q.split():
        rows = rows.filter(
            Q(pet__name__icontains=word)
            | Q(owner__first_name__icontains=word)
            | Q(owner__last_name__icontains=word)
        )
    if date:
        rows = rows.filter(date=date)

    # pk breaks ties (e.g. a cancelled row and a new one in the same slot),
    # so pages never repeat or skip a row.
    if tab in _ASCENDING_TABS:
        return rows.order_by("date", "time", "pk")
    return rows.order_by("-date", "-time", "-pk")


def list_tabs(counts):
    """The visible tabs as dicts (id, label, count), in display order."""
    return [
        {"id": tab_id, "label": label, "count": counts[tab_id]}
        for tab_id, label in LIST_TABS
    ]


def unfinished_visits(today, count):
    """
    Context for the yellow notice about confirmed visits whose date has
    passed. count comes from tab_counts; the visit itself is fetched only
    when there is exactly one, so the notice can name it.
    """
    visit = None
    if count == 1:
        visit = (
            Appointment.objects.filter(status=Appointment.CONFIRMED, date__lt=today)
            .select_related("pet", "service")
            .first()
        )
    return {"unfinished_count": count, "unfinished_visit": visit}


def group_by_day(rows):
    """[(date, [rows])] in the rows' own order. Rows must be sorted by date."""
    return [(day, list(day_rows)) for day, day_rows in groupby(rows, lambda a: a.date)]


def list_querystring(tab, q, date):
    """status/q/date as a query string (no "?"), skipping empty values."""
    params = {"status": tab, "q": q, "date": date.isoformat() if date else ""}
    return urlencode({key: value for key, value in params.items() if value})


def paging_links(page_obj, page_query):
    """ "?…&page=N" (or "?page=N" with an empty page_query) for the previous
    and next pages, or "" when there is none."""

    def link(number):
        return f"?{page_query}&page={number}" if page_query else f"?page={number}"

    return {
        "prev_page_query": (
            link(page_obj.previous_page_number()) if page_obj.has_previous() else ""
        ),
        "next_page_query": (
            link(page_obj.next_page_number()) if page_obj.has_next() else ""
        ),
    }


# ── Admin walk-in ────────────────────────────────────────────────────────────

WALKIN_SEARCH_LIMIT = 20
WALKIN_PAGE_SIZE = 25


def _walkin_owner_rows(owners):
    """owners sorted A to Z by first name (then last name), each with
    active_pets (non-archived, by name) prefetched."""
    active_pets = Pet.objects.filter(is_archived=False).order_by("name")
    return owners.order_by("first_name", "last_name", "pk").prefetch_related(
        Prefetch("pets", queryset=active_pets, to_attr="active_pets")
    )


def walkin_owner_list():
    """Step 1 with no search: every non-archived owner, first name A to Z."""
    return _walkin_owner_rows(PetOwner.objects.filter(is_archived=False))


def search_walkin_owners(q):
    """
    Step 1 of the walk-in: (owners, is_cut). Every word in q must match the
    owner's first name, last name or phone, or the name of one of their
    non-archived pets (case-insensitive). Archived owners are left out.
    At most WALKIN_SEARCH_LIMIT owners, first name A to Z; is_cut is True
    when there were more. Each owner gets active_pets, prefetched.
    """
    words = q.split()
    if not words:
        return [], False

    active_pets = Pet.objects.filter(is_archived=False)
    owners = PetOwner.objects.filter(is_archived=False)
    for word in words:
        # A subquery (not a join on pets) keeps one row per owner
        pet_owner_ids = active_pets.filter(name__icontains=word).values("owner_id")
        owners = owners.filter(
            Q(first_name__icontains=word)
            | Q(last_name__icontains=word)
            | Q(contact_number__icontains=word)
            | Q(pk__in=pet_owner_ids)
        )

    owners = _walkin_owner_rows(owners)
    # One extra row tells us whether the list was cut
    found = list(owners[: WALKIN_SEARCH_LIMIT + 1])
    return found[:WALKIN_SEARCH_LIMIT], len(found) > WALKIN_SEARCH_LIMIT


def get_walkin_owner(value):
    """The non-archived PetOwner with pk=value (a string from the URL or a
    form), or None when it is missing, not a number or archived."""
    try:
        pk = int(value)
    except (TypeError, ValueError):
        return None
    return (
        PetOwner.objects.select_related("user").filter(pk=pk, is_archived=False).first()
    )


def walkin_initial(pets, pet_value):
    """
    Step 2 initial values: today's date, and the pet from ?pet= when it is
    one of pets (the owner's non-archived pets), or the only pet. Any other
    ?pet= is ignored.
    """
    pet_ids = {str(pet.pk) for pet in pets}
    if pet_value in pet_ids:
        pet = pet_value
    elif len(pet_ids) == 1:
        pet = pet_ids.pop()
    else:
        pet = ""
    return {"pet": pet, "date": timezone.localdate()}


# Values the review page carries in hidden fields (besides owner and action)
WALKIN_VALUE_FIELDS = ("pet", "date", "time", "service", "reason")


def walkin_values(data, keep_time=True):
    """The step 2 values from a POST, as initial data for an unbound form
    ("Change", or a time that was just taken when keep_time is False)."""
    values = {name: data.get(name, "") for name in WALKIN_VALUE_FIELDS}
    if not keep_time:
        values["time"] = ""
    return values


def walkin_slot_context(form):
    """
    Free times for step 2, for the form's date: the submitted one when it
    is valid, else the initial one (today on a first visit). slots is None
    when there is no usable date (the page asks for one). selected_time
    keeps the chosen time checked after an error or "Change"; date_value is
    the date input's value as typed (or today's, as YYYY-MM-DD). now_slot
    is the slot in progress (labelled "Now"), or None.
    """
    date_value = form["date"].value() or ""
    if isinstance(date_value, datetime.date):
        date_value = date_value.isoformat()
    if form.is_bound:
        # Checking form.errors first runs validation, which fills cleaned_data
        date = None if "date" in form.errors else form.cleaned_data.get("date")
    else:
        date = form.initial.get("date")
        if isinstance(date, str):
            date = parse_list_date(date)
    return {
        "slots": get_available_slots(date, walk_in=True) if date else None,
        "slots_date": date,
        "now_slot": slot_in_progress(date) if date else None,
        "date_value": date_value,
        "selected_time": form["time"].value() or "",
    }


def create_walkin_appointment(owner, cleaned_data):
    """
    Saves a walk-in as CONFIRMED and notifies the owner (in the app, and by
    email per their preferences; owners without an email get the in-app
    notification only). Returns the appointment.
    """
    appointment = Appointment.objects.create(
        owner=owner,
        pet=cleaned_data["pet"],
        service=cleaned_data.get("service"),
        date=cleaned_data["date"],
        time=cleaned_data["time"],
        reason=cleaned_data.get("reason", ""),
        status=Appointment.CONFIRMED,
        is_walk_in=True,
    )
    # Absolute date, since the email may be read on another day.
    notify(
        recipient=owner.user,
        notification_type="appointment_approved",
        title="Visit booked by the clinic",
        message=(
            f"The clinic booked a visit for {appointment.pet.name} on "
            f"{date_format(appointment.date, 'D, M j')} at "
            f"{date_format(appointment.time, 'g:i A')}. The time is confirmed."
        ),
        related_appointment=appointment,
        related_pet=appointment.pet,
        email_subject="Visit booked — Hapi Vet",
    )
    return appointment
