import datetime
from itertools import groupby
from urllib.parse import urlencode

from django.contrib.auth import get_user_model
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Exists, OuterRef, Prefetch, Q
from django.urls import reverse
from django.utils import timezone
from django.utils.dateformat import format as date_format
from django.utils.dateformat import time_format
from django.utils.text import Truncator

from dashboard.templatetags.friendly_dates import (
    MID_SENTENCE,
    friendly_date_on,
    long_date_on,
)
from dashboard.utils import (
    _add_months,
    _last_day,
    _month_value,
    _slot_end,
    booking_limit,
    resolve_month,
    slot_minutes,
)
from notifications.utils import compact_message, notify, when_line
from pets.models import Pet, PetOwner
from .models import ClinicSettings, BlockedDate, Appointment

# The row the clinic settings page creates when none exists yet
CLINIC_SETTINGS_DEFAULTS = {
    "clinic_name": "Hapi Tutz Vet. Supplies",
    "address": "Bognuyan, Gasan, Marinduque",
    "opening_time": datetime.time(8, 0),
    "closing_time": datetime.time(17, 0),
    "same_day_cutoff_time": datetime.time(15, 0),
    "working_days": [0, 1, 2, 3, 4],
}


def get_clinic_settings(create=False):
    """
    The one place ClinicSettings is read: the row with the lowest pk, or
    None when there is none. Only the settings page passes create=True,
    which makes the row from CLINIC_SETTINGS_DEFAULTS when it is missing.
    """
    settings_obj = ClinicSettings.objects.order_by("pk").first()
    if settings_obj is None and create:
        settings_obj = ClinicSettings.objects.create(**CLINIC_SETTINGS_DEFAULTS)
    return settings_obj


# Settings page tabs (?tab=). "blocked" has its own forms; the three others
# save through ClinicSettingsForm, which keeps only that tab's fields.
SETTINGS_TABS = ("details", "schedule", "blocked", "vet")
SETTINGS_TAB_FIELDS = {
    "details": (
        "clinic_name",
        "address",
        "contact_number",
        "email",
        "logo",
        "notification_email",
    ),
    "schedule": (
        "opening_time",
        "closing_time",
        "slot_duration_minutes",
        "working_days",
        "same_day_cutoff_time",
        "booking_limit_days",
    ),
    "vet": (
        "veterinarian_name",
        "veterinarian_license_number",
        "veterinarian_bio",
    ),
}

# Changing any of these with visits booked shows the schedule warning
# (booking_limit_days is not one of them).
SCHEDULE_WARNING_FIELDS = (
    "opening_time",
    "closing_time",
    "working_days",
    "slot_duration_minutes",
    "same_day_cutoff_time",
)
SCHEDULE_WARNING_PHRASES = {
    "opening_time": "the opening hours",
    "closing_time": "the opening hours",
    "working_days": "the working days",
    "same_day_cutoff_time": "the booking cutoff",
}
SCHEDULE_WARNING_LIMIT = 10
DAY_ABBREVIATIONS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def settings_tab(value):
    """The tab named by ?tab= (or the posted tab); anything else is details."""
    return value if value in SETTINGS_TABS else "details"


def problems_label(count):
    """The pill wording: "1 problem", "2 problems"."""
    return f"{count} problem{'' if count == 1 else 's'}"


def settings_tab_errors(form):
    """{tab: number of errors} for the settings form. A form error that
    belongs to no field (closing before opening) is a schedule error."""
    counts = {tab: 0 for tab in SETTINGS_TABS}
    tab_of = {name: tab for tab, names in SETTINGS_TAB_FIELDS.items() for name in names}
    for name, errors in form.errors.items():
        counts[tab_of.get(name, "schedule")] += len(errors)
    return counts


def first_error_tab(tab_errors):
    """The first tab, in tab order, that has an error (details when none)."""
    for tab in SETTINGS_TABS:
        if tab_errors[tab]:
            return tab
    return "details"


def upcoming_visits():
    """CONFIRMED and PENDING visits from today on, by date and time."""
    return (
        Appointment.objects.filter(
            date__gte=timezone.localdate(),
            status__in=[Appointment.CONFIRMED, Appointment.PENDING],
        )
        .select_related("pet", "owner")
        .order_by("date", "time", "pk")
    )


def _visit_line(visit):
    """ "Mon, Nov 23 at 9:00 AM · Bantay (Ana Cruz)"."""
    when = f"{date_format(visit.date, 'D, M j')} at {time_format(visit.time, 'g:i A')}"
    owner = f"{visit.owner.first_name} {visit.owner.last_name}"
    return f"{when} · {visit.pet.name} ({owner})"


def _schedule_change_phrase(form, changed):
    """What the heading says changed: one thing by name, several as "the
    schedule"."""
    phrases = []
    for name in changed:
        if name == "slot_duration_minutes":
            minutes = form.cleaned_data[name]
            phrase = f"the slot length to {minutes} minutes"
        else:
            phrase = SCHEDULE_WARNING_PHRASES[name]
        if phrase not in phrases:
            phrases.append(phrase)
    return phrases[0] if len(phrases) == 1 else "the schedule"


def schedule_warning(form):
    """
    The warning for a valid settings form, or None. It shows when an hours,
    working days, slot length or cutoff field changed and CONFIRMED or PENDING
    visits exist from today on. Every such visit is listed (not only the ones
    that no longer fit): the first 10 as lines, the rest as more_count.
    """
    changed = [n for n in SCHEDULE_WARNING_FIELDS if n in form.changed_data]
    if not changed:
        return None
    visits = upcoming_visits()
    total = visits.count()
    if not total:
        return None
    what = _schedule_change_phrase(form, changed)
    plural = "" if total == 1 else "s"
    return {
        "heading": (f"Changing {what} affects {total} visit{plural} already booked."),
        "lines": [_visit_line(v) for v in visits[:SCHEDULE_WARNING_LIMIT]],
        "more_count": max(total - SCHEDULE_WARNING_LIMIT, 0),
        "count": total,
    }


def clinic_hours_line(settings_obj):
    """The banner line: "Mon to Sat, 8:00 AM to 5:00 PM", "Mon, Wed and Fri,
    8:00 AM to 5:00 PM", "Every day, ..." or "Closed" with no working days."""
    days = sorted({d for d in settings_obj.working_days if 0 <= d <= 6})
    if not days:
        return "Closed"
    names = [DAY_ABBREVIATIONS[d] for d in days]
    if len(days) == 7:
        label = "Every day"
    elif len(days) >= 2 and days[-1] - days[0] == len(days) - 1:
        label = f"{names[0]} to {names[-1]}"
    elif len(names) == 1:
        label = names[0]
    else:
        label = f"{', '.join(names[:-1])} and {names[-1]}"
    opening = time_format(settings_obj.opening_time, "g:i A")
    closing = time_format(settings_obj.closing_time, "g:i A")
    return f"{label}, {opening} to {closing}"


def visits_on_date(date):
    """
    The CONFIRMED and PENDING visits on date, by time, with the pet and owner
    loaded. The busy-day warning on Blocked dates lists these; blocking a day
    never changes them.
    """
    return (
        Appointment.objects.filter(
            date=date, status__in=[Appointment.CONFIRMED, Appointment.PENDING]
        )
        .select_related("pet", "owner")
        .order_by("time", "pk")
    )


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
    clinic = get_clinic_settings()

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
    clinic = get_clinic_settings()
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
    clinic = get_clinic_settings()
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


# ── Late note (Slice 8a-2a) ──────────────────────────────────────────────────

LATE_NOTE_HOURS = 24


def is_within_24h(appointment, now):
    """
    True when the visit's start (date + time, Manila) is less than 24 hours
    after now, including a start that has already passed. Exactly 24 hours
    away is not within. now is an aware datetime. One rule for the late note
    on Cancel and Change time, and for Appointment.is_cancellable_freely.
    """
    start = timezone.make_aware(
        datetime.datetime.combine(appointment.date, appointment.time)
    )
    return start - now < datetime.timedelta(hours=LATE_NOTE_HOURS)


# ── Booking lock (Slice 8a-1b) ───────────────────────────────────────────────

SLOT_NO_LONGER_AVAILABLE = "This slot is no longer available. Please select another."


class SlotTaken(Exception):
    """The chosen slot was booked by someone else after the form was checked."""


def lock_schedule():
    """
    Call inside transaction.atomic(): locks the one ClinicSettings row (the
    lowest pk) with select_for_update(), so two bookings for the same slot
    run one after the other. The second one then re-checks the slot and
    finds it taken. With no settings row there is nothing to lock (and no
    error). The lock lasts until the transaction ends.
    """
    list(ClinicSettings.objects.select_for_update().order_by("pk")[:1])


def slot_is_booked(date, time):
    """True when a non-cancelled appointment holds date + time (walk-ins)."""
    return (
        Appointment.objects.filter(date=date, time=time)
        .exclude(status=Appointment.CANCELLED)
        .exists()
    )


def slot_taken_message(time):
    """The owner's error on the time field when the slot went meanwhile."""
    return (
        f"{date_format(time, 'g:i A')} was just taken. Someone else booked "
        "it a moment ago. Please pick another time."
    )


def form_with_taken_time(form_class, args, post, time):
    """
    The same owner form, bound to the POST with every value but the time
    (a fresh time list for the date, nothing picked), and the "just taken"
    error on the time field. args are the form's leading arguments.
    """
    data = post.copy()
    data["time"] = ""
    form = form_class(*args, data)
    form.fields["time"].required = False
    form.is_valid()
    form.add_error("time", slot_taken_message(time))
    return form


def create_owner_booking(owner, data):
    """
    Owner booking: lock, check the slot again with the owner rules, save
    PENDING. data is the booking form's cleaned_data. Raises SlotTaken
    (nothing saved) when the slot went meanwhile.
    """
    with transaction.atomic():
        lock_schedule()
        if not is_slot_available(data["date"], data["time"]):
            raise SlotTaken(data["time"])
        return Appointment.objects.create(
            owner=owner,
            pet=data["pet"],
            service=data.get("service"),
            date=data["date"],
            time=data["time"],
            reason=data.get("reason", ""),
            status=Appointment.PENDING,
        )


def reschedule_by_owner(user, owner, appointment_pk, data):
    """
    Owner reschedule in one transaction: lock, re-read the original under
    select_for_update(), check the new slot again (the original's own slot
    doesn't count against it), cancel the original and create the new
    PENDING visit. Either both happen or neither.
    Returns the new appointment, or None when the original is no longer
    PENDING or CONFIRMED, or no longer this owner's (nothing changed).
    Raises SlotTaken (nothing changed) when the new slot went meanwhile.
    """
    with transaction.atomic():
        lock_schedule()
        original = (
            Appointment.objects.select_for_update()
            .filter(
                pk=appointment_pk,
                owner=owner,
                status__in=[Appointment.PENDING, Appointment.CONFIRMED],
            )
            .first()
        )
        if original is None:
            return None
        if not is_slot_available(
            data["date"], data["time"], exclude_appointment_pk=original.pk
        ):
            raise SlotTaken(data["time"])
        original.status = Appointment.CANCELLED
        original.cancellation_reason = Appointment.OTHER
        original.cancellation_detail = "Rescheduled by pet owner."
        original.cancelled_by = user
        original.save()
        return Appointment.objects.create(
            owner=owner,
            pet=original.pet,
            service=original.service,
            date=data["date"],
            time=data["time"],
            reason=original.reason,
            reschedule_reason=data["reschedule_reason"],
            rescheduled_from=original,
            status=Appointment.PENDING,
        )


def notify_admin_of_request(appointment, reschedule=False):
    """
    Tells the admin about a new owner request (in the app, and by email per
    notify()). Call after the transaction that saved it. reschedule=True is
    the reschedule wording; a booking goes to the clinic's notification
    email when one is set.
    """
    admin_user = get_user_model().objects.filter(role="admin").first()
    if admin_user is None:
        return
    owner = appointment.owner
    name = f"{owner.first_name} {owner.last_name}"
    when = (
        f"{appointment.date.strftime('%B %d, %Y')} "
        f"at {appointment.time.strftime('%I:%M %p')}"
    )
    extra = {}
    if reschedule:
        title = "Appointment Reschedule Request"
        message = (
            f"{name} has requested to reschedule their appointment for "
            f"{appointment.pet.name} to {when}."
        )
    else:
        title = "New Appointment Request"
        message = (
            f"{name} has requested an appointment for {appointment.pet.name} "
            f"on {when}."
        )
        clinic = get_clinic_settings()
        extra["recipient_email"] = (
            clinic.notification_email
            if clinic and clinic.notification_email
            else admin_user.email
        )
    notify(
        recipient=admin_user,
        notification_type="appointment_requested",
        title=title,
        message=message,
        related_appointment=appointment,
        related_pet=appointment.pet,
        email_subject=f"{title} — Hapi Vet",
        **extra,
    )


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

    clinic = get_clinic_settings()
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


# Period filter (?show=&from=): (id, label) in the "Show" dropdown's order
SHOW_OPTIONS = [
    ("any", "Any date"),
    ("day", "Day"),
    ("week", "Week"),
    ("month", "Month"),
    ("year", "Year"),
]
_PERIOD_KINDS = {kind for kind, _ in SHOW_OPTIONS}


def _period_start(kind, day):
    """First day of the day/week/month/year holding day (weeks start Monday)."""
    if kind == "week":
        return day - datetime.timedelta(days=day.weekday())
    if kind == "month":
        return day.replace(day=1)
    if kind == "year":
        return day.replace(month=1, day=1)
    return day


def _shift_period(kind, start, steps):
    """First day of the period `steps` periods after (negative: before) start."""
    if kind == "week":
        return start + datetime.timedelta(weeks=steps)
    if kind == "month":
        months = start.year * 12 + start.month - 1 + steps
        return datetime.date(months // 12, months % 12 + 1, 1)
    if kind == "year":
        return start.replace(year=start.year + steps)
    return start + datetime.timedelta(days=steps)


def _week_range(start, end, today):
    """'Nov 2 to Nov 8'; the year when it isn't this year, both years when
    the week runs across New Year."""
    if start.year != end.year:
        return f"{date_format(start, 'M j, Y')} to {date_format(end, 'M j, Y')}"
    if start.year != today.year:
        return f"{date_format(start, 'M j')} to {date_format(end, 'M j, Y')}"
    return f"{date_format(start, 'M j')} to {date_format(end, 'M j')}"


def _period_label(kind, start, end, today, style=None):
    """'This week: Nov 2 to Nov 8', 'October 2026', 'Today, Monday, November 2'.
    style "mid" lowercases the leading this/last/next/today/tomorrow."""
    if kind == "day":
        return long_date_on(start, today, style)
    current = _period_start(kind, today)
    word = None
    if kind == "week":
        word = {
            current: "This week",
            _shift_period(kind, current, -1): "Last week",
            _shift_period(kind, current, 1): "Next week",
        }.get(start)
        text = _week_range(start, end, today)
    elif kind == "month":
        word = "This month" if start == current else None
        text = date_format(start, "F Y")
    else:
        word = "This year" if start == current else None
        text = str(start.year)
    if not word:
        return text
    return f"{word.lower() if style == MID_SENTENCE else word}: {text}"


def _short_label(kind, start, today):
    """Heading of the period row: 'Today', 'Sun, Nov 1', 'This week',
    'Week of Nov 16', 'Last month', 'September', 'January 2027', '2024'.
    The year shows only outside the current year."""
    if kind == "day":
        return friendly_date_on(start, today)
    current = _period_start(kind, today)
    word = {
        current: "This",
        _shift_period(kind, current, -1): "Last",
        _shift_period(kind, current, 1): "Next",
    }.get(start)
    if word:
        return f"{word} {kind}"
    if kind == "week":
        # The week's Monday decides the year shown
        fmt = "M j" if start.year == today.year else "M j, Y"
        return f"Week of {date_format(start, fmt)}"
    if kind == "month":
        return date_format(start, "F" if start.year == today.year else "F Y")
    return str(start.year)


def resolve_period(show, from_value, today):
    """
    The period filter as a dict. show is any/day/week/month/year (unknown
    → any). from_value is an ISO date, snapped to the first day of its
    period; missing or bad → the period holding today.
    Keys: kind, start, end (both None for "any"), label, mid_label,
    short_label (the row's heading), current_label ("This week", the
    middle button), is_current, is_past (ends before today), prev_from,
    next_from, current_from, back_to ("Back to this week"), back_to_mid
    ("this week").
    """
    kind = show if show in _PERIOD_KINDS else "any"
    if kind == "any":
        return {"kind": "any", "start": None, "end": None, "is_current": True}

    current = _period_start(kind, today)
    try:
        start = _period_start(kind, parse_list_date(from_value) or today)
        prev_from = _shift_period(kind, start, -1)
        next_from = _shift_period(kind, start, 1)
    except (ValueError, OverflowError):
        # A from in the first or last year a date can hold
        start = current
        prev_from = _shift_period(kind, start, -1)
        next_from = _shift_period(kind, start, 1)
    end = next_from - datetime.timedelta(days=1)
    back_to = "today" if kind == "day" else f"this {kind}"
    return {
        "kind": kind,
        "start": start,
        "end": end,
        "label": _period_label(kind, start, end, today),
        "mid_label": _period_label(kind, start, end, today, MID_SENTENCE),
        "short_label": _short_label(kind, start, today),
        "current_label": "Today" if kind == "day" else f"This {kind}",
        "is_current": start == current,
        "is_past": end < today,
        "prev_from": prev_from,
        "next_from": next_from,
        "current_from": current,
        "back_to": f"Back to {back_to}",
        "back_to_mid": back_to,
    }


def list_period(params, today):
    """
    (period, is_old_date_link) from the request's ?show=&from=. An old
    ?date=YYYY-MM-DD link (no show) means show=day&from=that date.
    """
    old_date = parse_list_date(params.get("date", ""))
    if "show" not in params and old_date:
        return resolve_period("day", old_date.isoformat(), today), True
    return resolve_period(params.get("show"), params.get("from", ""), today), False


def filter_appointments(tab, today, q="", period=None):
    """
    Rows for one tab, sorted for that tab. Every word in q must match the
    pet name or the owner's first or last name (case-insensitive). A
    period (from resolve_period) keeps rows from its start to its end.
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
    if period and period["start"]:
        rows = rows.filter(date__range=(period["start"], period["end"]))

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


def list_querystring(tab, q, period, start=None):
    """status/q/show/from as a query string (no "?"), skipping empty values.
    "Any date" sends neither show nor from. start replaces the period's own
    first day (Previous / Next / Back to links)."""
    params = {"status": tab, "q": q}
    if period["kind"] != "any":
        params["show"] = period["kind"]
        params["from"] = (start or period["start"]).isoformat()
    return urlencode({key: value for key, value in params.items() if value})


def _dates_query(tab, q, kind):
    """status/q/show for one Dates button: no from, and no show for "any"."""
    params = {"status": tab, "q": q, "show": "" if kind == "any" else kind}
    return urlencode({key: value for key, value in params.items() if value})


def list_links(tab, status, q, period):
    """
    Query strings (no "?") for the list's links. Tabs keep the period, not
    the search. The pager and Previous / Next / Back to keep everything.
    Clear search keeps the requested status (not the "All" a search
    forces) and the period. The Dates buttons keep status and q and send
    no from, so a new kind opens on the current period.
    """
    links = {
        "page_query": list_querystring(tab, q, period),
        "tab_period_query": list_querystring("", "", period),
        "clear_search_query": list_querystring(status, "", period),
        "dates_links": [
            {"kind": kind, "label": label, "query": _dates_query(tab, q, kind)}
            for kind, label in SHOW_OPTIONS
        ],
    }
    if period["kind"] != "any":
        for name in ("prev", "next", "current"):
            links[f"{name}_period_query"] = list_querystring(
                tab, q, period, period[f"{name}_from"]
            )
    return links


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


# ── Admin detail: the visit's receipt (Slice 6a-4, rule 26) ──────────────────

# BillingReceipt.payment_status → what the appointment page says about it
# (plain strings: billing.utils imports this module)
VISIT_RECEIPT_STATES = {"pending": "unpaid", "paid": "paid", "cancelled": "cancelled"}


def visit_receipt_state(receipt):
    """None (no receipt yet), "unpaid", "paid" or "cancelled". The banner
    says "All done for this visit" only for "paid"."""
    if receipt is None:
        return None
    return VISIT_RECEIPT_STATES.get(receipt.payment_status)


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


def is_owner_results_swap(request):
    """True for the filter-as-you-type request from an owner step's search
    box (walk-in step 1, New receipt step 1), which only needs the results
    (target #owner-results). A history restore (or a request without HTMX)
    gets the full page."""
    headers = request.headers
    return (
        bool(headers.get("HX-Request"))
        and headers.get("HX-Target") == "owner-results"
        and not headers.get("HX-History-Restore-Request")
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
    notification only). Returns the appointment. The slot is checked again
    under lock_schedule(); SlotTaken when someone booked it meanwhile
    (nothing saved, no notification).
    """
    with transaction.atomic():
        lock_schedule()
        if slot_is_booked(cleaned_data["date"], cleaned_data["time"]):
            raise SlotTaken(cleaned_data["time"])
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
    when = when_line(appointment.date, appointment.time, timezone.localdate())
    notify(
        recipient=owner.user,
        notification_type="appointment_approved",
        title="Visit booked by the clinic",
        message=compact_message(
            appointment.pet.name, f"{when}. The time is confirmed."
        ),
        related_appointment=appointment,
        related_pet=appointment.pet,
        email_subject="Visit booked — Hapi Vet",
        email_body=(
            f"The clinic booked a visit for {appointment.pet.name} on {when}. "
            "The time is confirmed."
        ),
    )
    return appointment


# Longest clinic message kept in the compact "request declined" text
DECLINED_CLAUSE_LENGTH = 100


def notify_request_declined(appointment, clinic_message, today):
    """
    Tells the owner the clinic declined their request: a compact message
    ("Choco · Fri, Oct 30 at 1:00 PM. That day is fully booked." with the
    clinic's own message, trimmed) and the full sentence in the email.
    """
    pet = appointment.pet.name
    when = when_line(appointment.date, appointment.time, today)
    clause = Truncator(clinic_message).chars(DECLINED_CLAUSE_LENGTH)
    email_body = (
        f"Your appointment request for {pet} on {when} was not confirmed by "
        "the clinic. Please contact the clinic for more information."
    )
    if clinic_message:
        email_body += f" Message from the clinic: {clinic_message}"
    notify(
        recipient=appointment.owner.user,
        notification_type="appointment_rejected",
        title="Visit request declined",
        message=compact_message(pet, f"{when}. {clause}" if clause else when),
        related_appointment=appointment,
        related_pet=appointment.pet,
        email_subject="Appointment Update — Hapi Vet",
        email_body=email_body,
    )


# ── Owner booking: pet picked first (Slice 8a-2a) ────────────────────────────


def picked_pet_initial(owner, value):
    """
    Initial data for the booking form from ?pet=: {"pet": pk} when value is
    the pk of one of the owner's non-archived pets, else {} (never an error).
    """
    text = str(value or "")
    # The length keeps a huge number away from the integer lookup
    if not text.isdecimal() or len(text) > 9:
        return {}
    is_theirs = Pet.objects.filter(pk=int(text), owner=owner, is_archived=False)
    return {"pet": int(text)} if is_theirs.exists() else {}


# ── Owner booking calendar (Slice 8a-2a) ─────────────────────────────────────

# Five weeks at least, six when the month needs them (weeks start on Sunday)
OWNER_CALENDAR_MIN_CELLS = 35
OWNER_DAY_PHRASES = {
    "open": "open",
    "full": "full",
    "closed": "clinic closed",
    "past": "not available",
}


def _owner_calendar_months(today, limit_days):
    """Month-first dates from today's month to the month of the last
    bookable day (today + limit_days)."""
    last = (today + datetime.timedelta(days=limit_days)).replace(day=1)
    months = [today.replace(day=1)]
    while months[-1] < last:
        months.append(_add_months(months[-1], 1))
    return months


def _owner_calendar_days(month_first):
    """Every date of the grid: whole weeks, Sunday first, 35 or 42 days."""
    lead = (month_first.weekday() + 1) % 7  # weekday() is 0 = Monday
    needed = lead + _last_day(month_first).day
    cells = max(OWNER_CALENDAR_MIN_CELLS, -(-needed // 7) * 7)
    start = month_first - datetime.timedelta(days=lead)
    return [start + datetime.timedelta(days=n) for n in range(cells)]


def _owner_day_state(day, today, now, clinic, blocked, booked):
    """
    past / closed / full / open for one day, by the rules of
    get_available_slots (owner defaults). blocked is a set of dates; booked
    maps a date to the slot times held by non-cancelled visits.
    """
    if clinic is None:
        return "past" if day < today else "closed"
    if day < today or day > today + datetime.timedelta(days=clinic.booking_limit_days):
        return "past"
    if day.weekday() not in clinic.working_days or day in blocked:
        return "closed"
    slots = _day_slots(clinic, day)
    if day == today:
        slots = [s for s in slots if _slot_open_today(clinic, day, s, now)]
    taken = booked.get(day, ())
    return "open" if any(slot not in taken for slot in slots) else "full"


def _owner_day_rows(days):
    """(blocked dates, {date: slot times held}) for the grid: two queries."""
    window = (days[0], days[-1])
    blocked = set(
        BlockedDate.objects.filter(date__range=window).values_list("date", flat=True)
    )
    booked = {}
    held = (
        Appointment.objects.filter(date__range=window)
        .exclude(status=Appointment.CANCELLED)
        .values_list("date", "time")
    )
    for day, time in held:
        booked.setdefault(day, set()).add(time)
    return blocked, booked


def owner_booking_calendar(month_value, today, now, current_date=None, settings=None):
    """
    The month grid on the owner's booking and change-time pages (read-only).
    month_value is "YYYY-MM", clamped to the months from today to today +
    booking_limit_days; missing or bad gives today's month. today and now
    (aware, Manila) are arguments, so the view reads the clock once.

    Returns month (first day), month_value, month_label ("November 2026"),
    prev_month and next_month ("YYYY-MM", or None at the ends) and cells:
    35 or 42 dicts, Sunday first, each with date, day, in_month, state
    (past / closed / full / open), is_today, is_current (the date equal to
    current_date) and aria_label. Three queries however long the month (two
    when the caller passes the clinic settings it already has as settings).
    """
    now = timezone.localtime(now)
    clinic = settings if settings is not None else get_clinic_settings()
    months = _owner_calendar_months(today, booking_limit(clinic))
    month = resolve_month(month_value, today, months)
    days = _owner_calendar_days(month)
    blocked, booked = _owner_day_rows(days)

    cells = []
    for day in days:
        state = _owner_day_state(day, today, now, clinic, blocked, booked)
        cells.append(
            {
                "date": day,
                "day": day.day,
                "in_month": day.month == month.month,
                "state": state,
                "is_today": day == today,
                "is_current": day == current_date,
                "aria_label": (
                    f"{date_format(day, 'l, F j')}: {OWNER_DAY_PHRASES[state]}"
                ),
            }
        )
    index = months.index(month)
    return {
        "month": month,
        "month_value": _month_value(month),
        "month_label": date_format(month, "F Y"),
        "prev_month": _month_value(months[index - 1]) if index else None,
        "next_month": (
            _month_value(months[index + 1]) if index + 1 < len(months) else None
        ),
        "cells": cells,
    }


# ── Owner booking flow: the session draft (Slice 8b-6) ───────────────────────

# One session key holds the whole draft until Send: pet (pk), service ("none"
# or a pk as text), note, date (ISO), time (ISO), from_pet (started from a pet's
# page) and taken (a time someone else booked first, as text, for the red box).
BOOKING_DRAFT_KEY = "owner_booking"
NOT_SURE = "none"
BOOKING_NOTE_MAX = 1000
MORNING_ENDS = datetime.time(12, 0)

START_MESSAGE = "Please start by choosing a pet."
SERVICE_MISSING_MESSAGE = "Please choose what the visit is for."
SERVICE_GONE_MESSAGE = "That service isn't offered any more. Please choose again."
PET_GONE_MESSAGE = "That pet can't be booked any more. Please choose again."
PICK_AGAIN_MESSAGE = "Please pick a day and a time."
DAY_GONE_MESSAGE = "That day can't be booked any more. Please pick another day."
DAY_FULL_MESSAGE = "That day is fully booked now. Please pick another day."
TIME_GONE_MESSAGE = "That time isn't available. Please pick another time."
DAY_NOT_OPEN_MESSAGE = "That day can't be booked. Please pick a day marked open."


def get_booking_draft(session):
    """The draft dict from the session, or None (never started, or sent)."""
    draft = session.get(BOOKING_DRAFT_KEY)
    return draft if isinstance(draft, dict) else None


def save_booking_draft(session, draft):
    """Puts the draft back, which marks the session as changed."""
    session[BOOKING_DRAFT_KEY] = draft


def clear_booking_draft(session):
    session.pop(BOOKING_DRAFT_KEY, None)


def start_booking_draft(session, owner, pet_value):
    """A fresh draft (a plain visit to step 1), with the pet from ?pet= when
    it is one of the owner's non-archived pets."""
    pet = picked_pet_initial(owner, pet_value).get("pet")
    draft = {"pet": pet, "from_pet": pet is not None}
    save_booking_draft(session, draft)
    return draft


def owner_booking_pets(owner):
    """The pets an owner can book for: their own, not archived, by name."""
    return list(
        Pet.objects.filter(owner=owner, is_archived=False).order_by("name", "pk")
    )


def bookable_services():
    """The services shown on the booking page (Active only, by name)."""
    from billing.models import Service

    return list(Service.objects.filter(status=Service.ACTIVE))


def draft_service(answer):
    """The bookable Service for a draft answer (a pk as text), or None."""
    from billing.models import Service

    text = str(answer or "")
    if not text.isdecimal() or len(text) > 9:
        return None
    return Service.objects.filter(pk=int(text), status=Service.ACTIVE).first()


def draft_pet(owner, draft):
    """The owner's non-archived pet named in the draft, or None."""
    value = (draft or {}).get("pet")
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    return Pet.objects.filter(pk=value, owner=owner, is_archived=False).first()


def _iso_date(value):
    try:
        return datetime.date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _iso_time(value):
    try:
        return datetime.time.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def time_label(time):
    """'2:00 PM'."""
    return date_format(time, "g:i A")


def owner_day_times(day, today, now, settings=None):
    """
    Every time the owner could pick on day, as (time, is_taken) pairs: the
    clinic's slots with the same rules as get_available_slots (working day,
    not blocked, inside the booking window, today's cutoff and slots that
    already started). is_taken is True when a non-cancelled visit holds the
    slot. Empty when the day can't be booked at all. now is aware (Manila).
    settings is the clinic settings when the caller has them (saves a query).
    """
    clinic = settings if settings is not None else get_clinic_settings()
    if clinic is None or day < today:
        return []
    if day > today + datetime.timedelta(days=clinic.booking_limit_days):
        return []
    if day.weekday() not in clinic.working_days:
        return []
    if BlockedDate.objects.filter(date=day).exists():
        return []
    slots = _day_slots(clinic, day)
    if day == today:
        slots = [s for s in slots if _slot_open_today(clinic, day, s, now)]
    held = set(
        Appointment.objects.filter(date=day)
        .exclude(status=Appointment.CANCELLED)
        .values_list("time", flat=True)
    )
    return [(slot, slot in held) for slot in slots]


def _day_time_problem(draft, today, now, settings=None):
    """(2, message, taken time) for a draft whose day or time is gone, else None."""
    day, time = _iso_date(draft.get("date")), _iso_time(draft.get("time"))
    if day is None or time is None:
        return 2, PICK_AGAIN_MESSAGE, ""
    times = owner_day_times(day, today, now, settings)
    if not times:
        return 2, DAY_GONE_MESSAGE, ""
    if (time, False) in times:
        return None
    if all(taken for _time, taken in times):
        return 2, DAY_FULL_MESSAGE, ""
    if (time, True) in times:
        return 2, "", time_label(time)
    return 2, TIME_GONE_MESSAGE, ""


def booking_problem(owner, draft, upto, today, now, settings=None):
    """
    Checks the whole draft up to a step again (nothing is trusted from an
    earlier request): upto 1 = pet and service, upto 2 = also day and time.
    Returns None when all is well, else (step, message, taken): the first
    step that needs an answer, a plain message for it, and the time label
    when the time was taken by someone else (then the message is empty and
    the red box is shown instead).
    """
    if draft is None or draft.get("pet") is None:
        return 1, START_MESSAGE, ""
    if draft_pet(owner, draft) is None:
        return 1, PET_GONE_MESSAGE, ""
    answer = draft.get("service")
    if answer is None:
        return 1, SERVICE_MISSING_MESSAGE, ""
    if answer != NOT_SURE and draft_service(answer) is None:
        return 1, SERVICE_GONE_MESSAGE, ""
    return None if upto < 2 else _day_time_problem(draft, today, now, settings)


def step_one_initial(pets, draft):
    """Step 1's starting values: the draft's pet when it is one of pets, or
    the only pet; the draft's service and note."""
    draft = draft or {}
    ids = [pet.pk for pet in pets]
    pet = draft.get("pet") if draft.get("pet") in ids else None
    if pet is None and len(ids) == 1:
        pet = ids[0]
    return {
        "pet": pet,
        "service": draft.get("service"),
        "reason": draft.get("note", ""),
    }


def booking_cancel_url(draft):
    """Where Cancel goes: the pet's profile when the draft started from a
    pet's page (?pet=), else the dashboard."""
    if draft and draft.get("from_pet") and isinstance(draft.get("pet"), int):
        return reverse("owner_pet_detail", args=[draft["pet"]])
    return reverse("owner_dashboard")


def slot_length_phrase(minutes):
    """'1 hour', '2 hours' or 'N minutes' for the slot length."""
    if minutes == 60:
        return "1 hour"
    if minutes % 60 == 0:
        return f"{minutes // 60} hours"
    return f"{minutes} minutes"


def booking_calendar(month_value, today, now, current_date, settings=None):
    """
    owner_booking_calendar plus one flag for the booking page: past_cutoff is
    True on today's cell when it is "full" only because today's slots are
    over (the cutoff passed or every slot started), not because they are
    booked. That cell gets a ring and its label says no more bookings today.
    """
    calendar = owner_booking_calendar(month_value, today, now, current_date, settings)
    for cell in calendar["cells"]:
        cell["past_cutoff"] = False
        if cell["is_today"] and cell["state"] == "full":
            clinic = settings if settings is not None else get_clinic_settings()
            slots = _day_slots(clinic, today) if clinic else []
            if not any(_slot_open_today(clinic, today, s, now) for s in slots):
                cell["past_cutoff"] = True
                cell["aria_label"] = (
                    f"{date_format(today, 'l, F j')}: no more bookings today"
                )
    return calendar


def time_groups(times, selected):
    """The day's times as Morning / Afternoon groups of dicts (time, value,
    label, taken, checked), empty groups left out. selected is a time."""
    groups = []
    for name, wanted in (("Morning", True), ("Afternoon", False)):
        rows = [
            {
                "value": time.isoformat(),
                "label": time_label(time),
                "taken": taken,
                "checked": time == selected and not taken,
            }
            for time, taken in times
            if (time < MORNING_ENDS) == wanted
        ]
        if rows:
            groups.append({"name": name, "times": rows})
    return groups


def apply_picked_date(draft, value, today, now, settings=None):
    """
    A picked day (?date= or the form's day). A day with a free time goes in
    the draft; a new day clears the time and the "taken" note. Returns the
    error line for any other day, and "" when the day is fine or none was
    asked for.
    """
    if not value:
        return ""
    day = _iso_date(value)
    if day is not None and day.isoformat() == draft.get("date"):
        # The draft's own day was checked when it was stored; the page shows
        # its times (or "no times left") anyway, so skip the second read
        return ""
    times = owner_day_times(day, today, now, settings) if day else []
    if not any(not taken for _time, taken in times):
        return DAY_NOT_OPEN_MESSAGE
    draft["date"] = day.isoformat()
    draft.pop("time", None)
    draft.pop("taken", None)
    return ""


def pick_booking_time(draft, post, today, now, settings=None):
    """
    Step 2 POST: puts the day and time in the draft. Returns the errors as a
    dict, empty when saved: date (no or a closed day), time ("Pick a time.")
    or taken (the time label, when someone booked it a moment ago: the time
    is cleared and the same day is kept).
    """
    date_error = apply_picked_date(draft, post.get("date") or "", today, now, settings)
    if date_error or not post.get("date"):
        return {"date": date_error or "Pick a day first."}
    time = _iso_time(post.get("time"))
    times = owner_day_times(_iso_date(post["date"]), today, now, settings)
    if time is None:
        if not any(not taken for _time, taken in times):
            return {"date": DAY_NOT_OPEN_MESSAGE}
        return {"time": "Pick a time."}
    if (time, False) in times:
        draft["time"] = time.isoformat()
        draft.pop("taken", None)
        return {}
    draft.pop("time", None)
    if (time, True) in times:
        draft["taken"] = time_label(time)
        return {"taken": draft["taken"]}
    if not any(not taken for _time, taken in times):
        # No free time at all (the day closed, or filled up, since it went in
        # the draft): the same error as any closed day
        return {"date": DAY_NOT_OPEN_MESSAGE}
    return {"time": "Pick a time."}


def booking_time_context(draft, month_value, errors, today, now, clinic):
    """Step 2's calendar and times for the draft's day (see
    booking_calendar, time_groups). month_value is ?month=; without it the
    month of the picked day. clinic is the settings the view already read."""
    day = _iso_date(draft.get("date"))
    month_value = month_value or (_month_value(day.replace(day=1)) if day else "")
    times = owner_day_times(day, today, now, clinic) if day else []
    return {
        "calendar": booking_calendar(month_value, today, now, day, clinic),
        "picked_date": day,
        "time_groups": time_groups(times, _iso_time(draft.get("time"))),
        "date_error": errors.get("date", ""),
        "time_error": errors.get("time", ""),
        "taken_time": errors.get("taken") or draft.get("taken", ""),
        "booking_limit_days": booking_limit(clinic),
        "slot_length": slot_length_phrase(slot_minutes(clinic)),
    }


def booking_summary(owner, draft):
    """The draft as the pages show it: pet, service (None for "Not sure
    yet"), the day and time as one line, and the note."""
    day, time = _iso_date(draft.get("date")), _iso_time(draft.get("time"))
    when = ""
    if day and time:
        when = f"{date_format(day, 'D, M j, Y')} at {time_label(time)}"
    return {
        "pet": draft_pet(owner, draft),
        "service": draft_service(draft.get("service")),
        "when": when,
        "note": draft.get("note", ""),
    }


def booking_data(owner, draft):
    """The draft as the data create_owner_booking takes."""
    summary = booking_summary(owner, draft)
    return {
        "pet": summary["pet"],
        "service": summary["service"],
        "date": _iso_date(draft.get("date")),
        "time": _iso_time(draft.get("time")),
        "reason": summary["note"],
    }


# ── Owner visits list, visit page and cancel (Slice 8b-7a) ───────────────────

OWNER_VISITS_PAGE_SIZE = 25
OWNER_LIVE_STATUSES = (Appointment.PENDING, Appointment.CONFIRMED)
NOT_UPDATED_NOTE = "The clinic hasn't updated this visit yet."
CLINIC_CANCEL_NOTE = "Cancelled by the clinic. Please call us if you need a new time."
# The owner's words for each reason: the cancel sheet's radio cards
OWNER_CANCEL_WORDS = {
    Appointment.CHANGE_OF_PLANS: "Change of plans",
    Appointment.PET_UNWELL: "My pet is unwell",
    Appointment.CANNOT_MAKE_TIME: "I can't make it at that time",
    Appointment.OTHER: "Other reason",
}
NO_SERVICE_WORDS = "Not sure yet"


def _slot_open_q(now, minutes):
    """Q for visits whose slot hasn't ended: a slot ends `minutes` after it
    starts, so it is still going when its start is later than now minus the
    slot length (Manila date and time)."""
    cutoff = timezone.localtime(now) - datetime.timedelta(minutes=minutes)
    return Q(date__gt=cutoff.date()) | Q(date=cutoff.date(), time__gt=cutoff.time())


def _cancelled_by_owner(appointment):
    """True when the person who cancelled is a pet owner (not the clinic)."""
    user = appointment.cancelled_by
    return user is not None and user.is_pet_owner


def friendly_when(day, time, today):
    """'Today, Mon, Nov 2 at 3:00 PM', 'Tomorrow, Tue, Nov 3 at 8:00 AM' or
    'Tue, Nov 10 at 12:00 PM' (the year when it isn't today's year)."""
    text = when_line(day, time, today)
    if day == today:
        return f"Today, {text}"
    if day == today + datetime.timedelta(days=1):
        return f"Tomorrow, {text}"
    return text


def _long_when(day, time):
    """'Tuesday, Nov 10, 2026 at 12:00 PM'."""
    return f"{date_format(day, 'l, M j, Y')} at {time_label(time)}"


def _service_words(appointment):
    service = appointment.service
    return service.service_name if service else NO_SERVICE_WORDS


def _cancelled_note(appointment):
    """The note for a cancelled visit: declined, by the owner, or by the clinic."""
    if appointment.cancellation_reason == Appointment.DECLINED:
        return "Declined by the clinic."
    if not _cancelled_by_owner(appointment):
        return CLINIC_CANCEL_NOTE
    reason = appointment.cancellation_reason
    if reason in ("", Appointment.OTHER):
        return "You cancelled this visit."
    return f"You cancelled: {appointment.get_cancellation_reason_display().lower()}."


def _live_note(appointment, today):
    """The note for a Pending or Confirmed visit whose slot hasn't ended."""
    original = appointment.rescheduled_from
    pending = appointment.status == Appointment.PENDING
    if original is not None:
        was = when_line(original.date, original.time, today)
        if not _cancelled_by_owner(original):
            return f"Moved by the clinic. Was {was}."
        if pending:
            return f"You changed the time. Was {was}."
    return "The clinic will confirm this soon." if pending else None


def owner_visit_note(appointment, now, minutes=None):
    """
    The one-line note for a visit's card (and a moved or changed visit's
    page), or None. now is aware. minutes is the clinic's slot length; leave it
    out and it is read from the settings (one query), so a list passes it.
    Load rescheduled_from and cancelled_by (select_related) for a flat count.
    A moved original (cancelled by a reschedule) has its own banner instead.
    """
    status = appointment.status
    if status == Appointment.CANCELLED:
        return _cancelled_note(appointment)
    if status == Appointment.NO_SHOW:
        return "Marked as missed by the clinic."
    if status not in OWNER_LIVE_STATUSES:
        return None  # completed: nothing to say
    if minutes is None:
        minutes = slot_minutes(get_clinic_settings())
    if _slot_end(appointment, minutes) <= now:
        return NOT_UPDATED_NOTE
    return _live_note(appointment, timezone.localtime(now).date())


def _visits_pet(pets, value):
    """The owner's non-archived pet that ?pet= names, or None."""
    text = str(value or "")
    if not text.isdecimal() or len(text) > 9:
        return None
    return next((pet for pet in pets if pet.pk == int(text)), None)


def _visits_query(show, pet):
    """'show=past&pet=3': the list's own params, as a query string."""
    params = {"show": show}
    if pet is not None:
        params["pet"] = pet.pk
    return urlencode(params)


def _visit_card(visit, today, now, minutes):
    """One card of the list: the visit, its note and icon, today flag."""
    note = owner_visit_note(visit, now, minutes)
    moved = bool(note) and note.startswith(("Moved by", "You changed"))
    return {
        "visit": visit,
        "service_name": _service_words(visit),
        "note": note,
        "note_icon": "calendar-clock" if moved else "info",
        "is_today": visit.date == today,
    }


def _month_groups(cards):
    """The cards under month headings ('November 2026'), in their order."""
    groups = []
    for _key, group in groupby(
        cards, key=lambda c: (c["visit"].date.year, c["visit"].date.month)
    ):
        group = list(group)
        label = date_format(group[0]["visit"].date, "F Y")
        groups.append({"label": label, "cards": group})
    return groups


def owner_visits_context(owner, params, today, now):
    """
    Context for the owner's Visits page. ?show=upcoming|past (default and
    unknown: upcoming); ?pet=<pk> (the owner's own non-archived pets only,
    else all). Upcoming is Pending / Confirmed visits whose slot hasn't ended,
    soonest first; past is everything else, newest first, 25 a page. An
    original cancelled by a reschedule is left out of both (its new visit says
    "Was ..."). The counts follow the pet filter. Four queries for upcoming
    (settings, pets, counts, visits), five for past (and the page count).
    """
    minutes = slot_minutes(get_clinic_settings())
    own_pets = Pet.objects.filter(owner=owner, is_archived=False)
    pets = list(own_pets.order_by("name", "pk"))
    pet = _visits_pet(pets, params.get("pet"))
    show = "past" if params.get("show") == "past" else "upcoming"

    live = Q(status__in=OWNER_LIVE_STATUSES) & _slot_open_q(now, minutes)
    moved = Exists(Appointment.objects.filter(rescheduled_from=OuterRef("pk")))
    visits = Appointment.objects.filter(owner=owner).annotate(is_moved=moved)
    visits = visits.filter(~Q(status=Appointment.CANCELLED, is_moved=True))
    if pet is not None:
        visits = visits.filter(pet=pet)
    counts = visits.aggregate(
        upcoming=Count("pk", filter=live), past=Count("pk", filter=~live)
    )

    rows = visits.select_related(
        "pet", "service", "cancelled_by", "rescheduled_from__cancelled_by"
    )
    page_obj, links = None, {"prev_page_query": "", "next_page_query": ""}
    if show == "upcoming":
        rows = list(rows.filter(live).order_by("date", "time", "pk"))
    else:
        past = rows.filter(~live).order_by("-date", "-time", "-pk")
        page_obj = Paginator(past, OWNER_VISITS_PAGE_SIZE).get_page(params.get("page"))
        rows = list(page_obj)
        links = paging_links(page_obj, _visits_query(show, pet))
    cards = [_visit_card(visit, today, now, minutes) for visit in rows]

    segments = [
        {
            "label": f"{label} ({counts[key]})",
            "query": _visits_query(key, pet),
            "is_active": key == show,
        }
        for key, label in (("upcoming", "Upcoming"), ("past", "Past"))
    ]
    all_query = _visits_query(show, None)
    chips = [{"label": "All", "query": all_query, "is_active": pet is None}]
    chips += [
        {
            "label": chip.name,
            "query": _visits_query(show, chip),
            "is_active": chip == pet,
        }
        for chip in pets
    ]
    no_visits = not counts["upcoming"] and not counts["past"]
    return {
        "show": show,
        "pet": pet,
        "has_pets": bool(pets),
        "show_pet_chips": len(pets) >= 2,
        "show_no_pets_page": not pets and no_visits,
        "counts": counts,
        "segments": segments,
        "chips": chips,
        "groups": _month_groups(cards),
        "has_cards": bool(cards),
        "page_obj": page_obj,
        "all_query": all_query,
        "today": today,
        **links,
    }


# ── One visit (Slice 8b-7a) ──────────────────────────────────────────────────


def _banner(tone, icon, title, text, **extra):
    return {"tone": tone, "icon": icon, "title": title, "text": text, **extra}


# Banners that say the same thing for every visit in that state
_STATIC_BANNERS = {
    Appointment.PENDING: _banner(
        "yellow",
        "hourglass",
        "Waiting for the clinic to confirm",
        "You'll get a notification when they do. "
        "Please don't come in until it's confirmed.",
    ),
    "ended": _banner(
        "gray",
        "info",
        "This time has passed",
        "The clinic hasn't updated this visit yet. "
        "Please call them if you have a question.",
    ),
    "declined": _banner(
        "red",
        "circle-x",
        "Declined by the clinic",
        "The clinic couldn't take this request. Please call us or book another time.",
    ),
    "cancelled_clinic": _banner(
        "red",
        "circle-x",
        "Cancelled by the clinic",
        "Sorry about this. Please call us to find a new time, or book another visit.",
    ),
    Appointment.NO_SHOW: _banner(
        "gray",
        "info",
        "Missed",
        "The clinic marked this visit as missed. "
        "If that's a mistake, please call the clinic.",
    ),
}


def _day_words(day, today):
    """'today', 'tomorrow' or 'on Tue, Nov 10', for "See you ... at 8:00 AM"."""
    if day == today:
        return "today"
    if day == today + datetime.timedelta(days=1):
        return "tomorrow"
    return f"on {when_line(day, None, today)}"


# What a decline saves when the clinic typed no message: not a message
NO_CLINIC_MESSAGE = "Rejected by clinic."


def _clinic_message(appointment):
    """The text the clinic wrote for the owner (the admin cancel form and the
    decline dialog both say the owner can see it), or "" when there is none.
    Never the internal notes."""
    detail = appointment.cancellation_detail.strip()
    return "" if detail == NO_CLINIC_MESSAGE else detail


def _visit_banner(state, appointment, today, moved_to, has_more):
    """The coloured banner for the visit's state (a dict: tone, icon, title,
    text, and for a moved visit link_url / link_label)."""
    if state in _STATIC_BANNERS:
        banner = dict(_STATIC_BANNERS[state])
        if state in ("declined", "cancelled_clinic"):
            banner["message"] = _clinic_message(appointment)
    elif state == Appointment.CONFIRMED:
        day = _day_words(appointment.date, today)
        text = f"See you {day} at {time_label(appointment.time)}. "
        banner = _banner(
            "blue",
            "calendar-check",
            "Confirmed",
            text + "Please come 10 minutes early.",
        )
    elif state == Appointment.COMPLETED:
        text = "Thank you for coming in."
        if has_more:
            text += " The vet's notes and the bill are below."
        banner = _banner("green", "circle-check", "Visit done", text)
    elif state == "cancelled_owner":
        detail = appointment.cancellation_detail.strip()
        other = appointment.cancellation_reason == Appointment.OTHER and detail
        text = f"Your reason: {detail}" if other else _cancelled_note(appointment)
        banner = _banner("red", "circle-x", "You cancelled this visit", text)
    else:
        banner = _banner(
            "gray",
            "calendar-clock",
            "This visit was moved",
            f"It's now on {when_line(moved_to.date, moved_to.time, today)}.",
            link_url=reverse("owner_appointment_detail", args=[moved_to.pk]),
            link_label="See the new visit",
        )
    return banner


def _visit_state(appointment, moved_to, ended):
    """pending, confirmed, ended (slot over, not updated), completed, no_show,
    moved (an original cancelled by a reschedule), declined, cancelled_owner
    or cancelled_clinic."""
    status = appointment.status
    if status == Appointment.CANCELLED:
        if moved_to is not None:
            return "moved"
        if appointment.cancellation_reason == Appointment.DECLINED:
            return "declined"
        if _cancelled_by_owner(appointment):
            return "cancelled_owner"
        return "cancelled_clinic"
    if status in OWNER_LIVE_STATUSES and ended:
        return "ended"
    return status


def _visit_record_and_bill(owner, appointment):
    """The pk of the visit's medical record (or None) and the receipt the
    owner may see (Unpaid or Paid, never Cancelled; or None)."""
    # Imported here: these modules import this one
    from billing.utils import owner_receipts
    from medical.models import MedicalRecord

    record_pk = (
        MedicalRecord.objects.filter(appointment=appointment, pet=appointment.pet)
        .values_list("pk", flat=True)
        .first()
    )
    receipt = owner_receipts(owner).filter(appointment=appointment).first()
    return record_pk, receipt


def _visit_links(appointment, record_pk, receipt):
    """The two link cards of a completed visit: the vet's notes (not for an
    archived pet) and the bill."""
    from billing.models import BillingReceipt

    pet, links = appointment.pet, []
    if record_pk is not None and not pet.is_archived:
        links.append(
            {
                "icon": "file-text",
                "title": "Vet's notes and medicines",
                "text": f"What Dr. Aga found and what to give {pet.name}",
                "url": reverse("owner_medical_record", args=[pet.pk, record_pk]),
            }
        )
    if receipt is not None:
        word = "Paid" if receipt.payment_status == BillingReceipt.PAID else "Unpaid"
        links.append(
            {
                "icon": "receipt",
                "title": f"Bill {receipt.receipt_number} · {word}",
                "amount": receipt.total_amount,
                "url": reverse("owner_receipt_detail", args=[receipt.pk]),
            }
        )
    return links


def _visit_buttons(state, pet):
    """Which buttons the page shows: 'change' (Change day or time + Cancel),
    'call_book' (Call + Book again), 'book' (one violet Book), or ''. Also the
    Book link (to the pet, or plain when the pet is archived) and its words."""
    book_url = reverse("owner_book_appointment")
    if not pet.is_archived:
        book_url += f"?pet={pet.pk}"
    if state in ("declined", "cancelled_clinic"):
        return "call_book", "Book again", book_url
    if state in ("completed", "cancelled_owner"):
        return "book", "Book another visit", book_url
    if state == "no_show":
        return "book", "Book a new visit", book_url
    return "", "", book_url


def _visit_sheet(appointment, today, now, service_words):
    """The cancel sheet's words: title, when · service, action, late flag."""
    pending = appointment.status == Appointment.PENDING
    noun = "request" if pending else "visit"
    when = when_line(appointment.date, appointment.time, today)
    return {
        "title": f"Cancel {appointment.pet.name}'s {noun}?",
        "text": f"{when} · {service_words}",
        "action_label": f"Yes, cancel the {noun}",
        "late": is_within_24h(appointment, now),
        "reasons": list(OWNER_CANCEL_WORDS.items()),
    }


def _visit_rows(appointment, state):
    """The detail card's rows that need working out: the date words, the
    cancelled / declined line (the date it was saved, who), the booked date."""
    pet = appointment.pet
    row = {
        "pet_line": pet.species + (f" · {pet.breed}" if pet.breed else ""),
        "pet_url": (
            None if pet.is_archived else reverse("owner_pet_detail", args=[pet.pk])
        ),
        "when_label": "Day and time" if state in OWNER_LIVE_STATUSES else "Was on",
        "when_long": _long_when(appointment.date, appointment.time),
        "booked_on": date_format(timezone.localtime(appointment.created_at), "M j, Y"),
        "cancelled_label": "",
        "cancelled_line": "",
    }
    if state in ("declined", "cancelled_owner", "cancelled_clinic"):
        when = date_format(timezone.localtime(appointment.updated_at), "M j, Y")
        who = "you" if state == "cancelled_owner" else "the clinic"
        row["cancelled_label"] = "Declined" if state == "declined" else "Cancelled"
        row["cancelled_line"] = f"{when} by {who}"
    return row


def owner_visit_detail_context(owner, appointment, today, now):
    """
    Context for the owner's visit page. appointment is the owner's own, loaded
    with pet, service, cancelled_by and rescheduled_from__cancelled_by. Never
    carries the internal notes. Queries: the settings, plus the new visit for a
    cancelled one, plus the record and the receipt for a completed one.
    """
    clinic = get_clinic_settings()
    minutes = slot_minutes(clinic)
    moved_to = None
    if appointment.status == Appointment.CANCELLED:
        moved_to = appointment.rescheduled_to.first()
    ended = _slot_end(appointment, minutes) <= now
    state = _visit_state(appointment, moved_to, ended)

    links = []
    if state == Appointment.COMPLETED:
        record_pk, receipt = _visit_record_and_bill(owner, appointment)
        links = _visit_links(appointment, record_pk, receipt)
    banner = _visit_banner(state, appointment, today, moved_to, bool(links))
    if state in OWNER_LIVE_STATUSES and appointment.rescheduled_from is not None:
        banner["extra"] = owner_visit_note(appointment, now, minutes)

    buttons, book_label, book_url = _visit_buttons(state, appointment.pet)
    # A slot "ends" 0 minutes after it starts: its start. Change / Cancel only
    # while it hasn't started.
    can_change = state in OWNER_LIVE_STATUSES and _slot_end(appointment, 0) > now
    service_words = _service_words(appointment)
    return {
        "appointment": appointment,
        "pet": appointment.pet,
        "state": state,
        "service_words": service_words,
        "when_friendly": friendly_when(appointment.date, appointment.time, today),
        "banner": banner,
        "links": links,
        "details": _visit_rows(appointment, state),
        "buttons": "change" if can_change else buttons,
        "book_label": book_label,
        "book_url": book_url,
        "cancel_label": (
            "Cancel this request"
            if appointment.status == Appointment.PENDING
            else "Cancel this visit"
        ),
        "sheet": _visit_sheet(appointment, today, now, service_words),
        "phone": clinic.contact_number if clinic else "",
        "today": today,
    }


def _notify_admin_of_owner_cancel(appointment):
    """Tells the admin an owner cancelled (in the app, and by email per notify())."""
    admin_user = get_user_model().objects.filter(role="admin").first()
    if admin_user is None:
        return
    owner = appointment.owner
    notify(
        recipient=admin_user,
        notification_type="appointment_cancelled",
        title="Appointment Cancelled",
        message=(
            f"{owner.first_name} {owner.last_name} has cancelled "
            f"their appointment for {appointment.pet.name} on "
            f"{appointment.date.strftime('%B %d, %Y')} "
            f"at {appointment.time.strftime('%I:%M %p')}."
        ),
        related_appointment=appointment,
        related_pet=appointment.pet,
        email_subject="Appointment Cancelled — Hapi Vet",
    )


def cancel_by_owner(user, appointment, post):
    """
    The owner's cancel: only a Pending or Confirmed visit, with a reason from
    the cancel form (Other needs a detail). Saves CANCELLED with the reason,
    the detail and the owner as cancelled_by, then tells the admin. Returns
    (ok, text) for the flash; on a problem nothing changed.
    """
    from .forms import AppointmentCancellationForm

    if appointment.status not in OWNER_LIVE_STATUSES:
        return False, "This appointment cannot be cancelled."
    form = AppointmentCancellationForm(post)
    if not form.is_valid():
        if "cancellation_reason" in form.errors:
            return False, "Choose why you're cancelling."
        return False, "Tell us a little more about the reason."
    pending = appointment.status == Appointment.PENDING
    appointment.status = Appointment.CANCELLED
    appointment.cancellation_reason = form.cleaned_data["cancellation_reason"]
    appointment.cancellation_detail = form.cleaned_data.get("cancellation_detail", "")
    appointment.cancelled_by = user
    appointment.save()
    _notify_admin_of_owner_cancel(appointment)
    return True, "Request cancelled." if pending else "Visit cancelled."
