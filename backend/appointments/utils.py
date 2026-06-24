import datetime
from django.db.models import Count
from django.utils import timezone
from .models import ClinicSettings, BlockedDate, Appointment


def get_available_slots(date):
    """
    Returns a list of available time slots for a given date.

    A slot is available if:
    1. The date is a working day (per ClinicSettings.working_days)
    2. The date is not blocked (BlockedDate)
    3. The date is within the booking window (today → today + booking_limit_days)
    4. The slot is not already taken by a non-cancelled appointment
    5. For today: only slots after the same_day_cutoff_time are shown

    Returns a list of datetime.time objects.
    Returns an empty list if the date is unavailable for any reason.
    """
    clinic = ClinicSettings.objects.first()

    # No settings configured yet — no slots available
    if not clinic:
        return []

    today = timezone.now().date()

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

    # Generate all possible slots between opening and closing time
    slots = []
    slot_delta = datetime.timedelta(minutes=clinic.slot_duration_minutes)

    # Convert TimeField values to datetime for arithmetic
    opening_dt = datetime.datetime.combine(date, clinic.opening_time)
    closing_dt = datetime.datetime.combine(date, clinic.closing_time)

    current = opening_dt
    while current + slot_delta <= closing_dt:
        slots.append(current.time())
        current += slot_delta

    # For today — filter out slots at or before the same-day cutoff time
    if date == today:
        slots = [s for s in slots if s > clinic.same_day_cutoff_time]

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


def is_slot_available(date, time, exclude_appointment_pk=None):
    """
    Checks if a specific date + time slot is available.
    Used for validation on booking and reschedule forms.

    exclude_appointment_pk: pass the current appointment's pk when
    checking a reschedule so it doesn't conflict with itself.
    """
    clinic = ClinicSettings.objects.first()
    if not clinic:
        return False

    today = timezone.now().date()

    if date < today:
        return False

    if date > today + datetime.timedelta(days=clinic.booking_limit_days):
        return False

    if date.weekday() not in clinic.working_days:
        return False

    if BlockedDate.objects.filter(date=date).exists():
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


def get_available_dates(month, year):
    """
    Returns a set of dates in a given month that have at least one
    available slot. Used to highlight bookable days in the calendar.
    """
    import calendar

    clinic = ClinicSettings.objects.first()
    if not clinic:
        return set()

    today = timezone.now().date()
    limit_date = today + datetime.timedelta(days=clinic.booking_limit_days)

    # Get all days in the requested month
    _, days_in_month = calendar.monthrange(year, month)

    available_dates = set()
    for day in range(1, days_in_month + 1):
        date = datetime.date(year, month, day)

        # Skip past dates and dates beyond booking window
        if date < today or date > limit_date:
            continue

        # Check if this date has any available slots
        if get_available_slots(date):
            available_dates.add(date)

    return available_dates


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

    today = timezone.now().date()

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
