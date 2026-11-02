"""Helpers for the admin dashboard (dashboard v5).

Every helper takes `today` (and `now` where the time matters) as an
argument and never reads the clock, so tests can pass any date. The
calendar helpers are pure except month_day_counts; the schedule and Needs
approval helpers query the database.
"""

import calendar
import re
from datetime import date, datetime, timedelta

from django.db.models import Count
from django.utils import timezone
from django.utils.dateformat import format as date_format

from appointments.models import Appointment
from dashboard.templatetags.friendly_dates import long_date_on

# Months shown before today's month in the month dropdown.
CALENDAR_MONTHS_BACK = 3
# Six rows of seven days, so the grid never changes height.
CALENDAR_CELLS = 42
# Used when there is no ClinicSettings row (same as the model defaults).
DEFAULT_BOOKING_LIMIT_DAYS = 30
DEFAULT_SLOT_MINUTES = 60
# Rows shown for a picked day; today shows every visit.
PICKED_DAY_ROWS = 3

# Counted as visits (violet pill). PENDING counts as waiting (yellow pill).
# CANCELLED never counts.
VISIT_STATUSES = (Appointment.CONFIRMED, Appointment.COMPLETED, Appointment.NO_SHOW)
WAITING_STATUS = Appointment.PENDING

MONTH_PATTERN = re.compile(r"^(\d{4})-(\d{2})$")
DAY_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _add_months(month_first, n):
    """The 1st of the month n months after month_first (n may be negative)."""
    index = month_first.year * 12 + month_first.month - 1 + n
    return date(index // 12, index % 12 + 1, 1)


def _last_day(month_first):
    """The last date of month_first's month."""
    days = calendar.monthrange(month_first.year, month_first.month)[1]
    return month_first.replace(day=days)


def _month_value(month_first):
    """'YYYY-MM' (zero-padded, unlike strftime("%Y") for early years)."""
    return f"{month_first.year:04d}-{month_first.month:02d}"


def calendar_month_range(today, booking_limit_days):
    """Month-first dates, oldest first: 3 months back to the month holding
    the last bookable day (today + booking_limit_days)."""
    first = _add_months(today.replace(day=1), -CALENDAR_MONTHS_BACK)
    last = (today + timedelta(days=booking_limit_days)).replace(day=1)
    months = []
    month = first
    while month <= last:
        months.append(month)
        month = _add_months(month, 1)
    return months


def booking_limit(clinic):
    """The clinic's booking window in days, or the default without settings."""
    if clinic is None:
        return DEFAULT_BOOKING_LIMIT_DAYS
    return clinic.booking_limit_days


def slot_minutes(clinic):
    """The clinic's slot length in minutes, or the default without settings."""
    if clinic is None:
        return DEFAULT_SLOT_MINUTES
    return clinic.slot_duration_minutes


def resolve_month(value, today, months):
    """A month-first date from 'YYYY-MM', clamped to months. Missing or bad
    values give today's month. Never raises."""
    match = MONTH_PATTERN.match(value or "")
    month = today.replace(day=1)
    if match:
        try:
            month = date(int(match.group(1)), int(match.group(2)), 1)
        except ValueError:  # month 13, year 0000
            pass
    return min(max(month, months[0]), months[-1])


def resolve_picked(value, today, months):
    """A date from 'YYYY-MM-DD' inside the months shown, else None.
    Today is None too (today is the default, not a pick)."""
    if not DAY_PATTERN.match(value or ""):
        return None
    try:
        picked = date.fromisoformat(value)
    except ValueError:  # impossible dates, e.g. 2026-02-30
        return None
    if picked < months[0] or picked > _last_day(months[-1]) or picked == today:
        return None
    return picked


def picked_day(value, today, clinic):
    """resolve_picked over the calendar's month range for this clinic."""
    months = calendar_month_range(today, booking_limit(clinic))
    return resolve_picked(value, today, months)


def month_day_counts(month_first):
    """{date: {"visits": n, "waiting": n}} for the days of one month that
    have appointments, from one grouped query. Cancelled is left out."""
    rows = (
        Appointment.objects.filter(date__range=[month_first, _last_day(month_first)])
        .exclude(status=Appointment.CANCELLED)
        .values("date", "status")
        .annotate(n=Count("id"))
    )
    counts = {}
    for row in rows:
        day = counts.setdefault(row["date"], {"visits": 0, "waiting": 0})
        if row["status"] in VISIT_STATUSES:
            day["visits"] += row["n"]
        elif row["status"] == WAITING_STATUS:
            day["waiting"] += row["n"]
    return counts


def _plural(n, word):
    return f"1 {word}" if n == 1 else f"{n} {word}s"


def _cell_aria_label(day, today, visits, waiting):
    """'Thursday, November 5: 2 visits, 1 request waiting'."""
    label = f"{long_date_on(day, today)}: "
    label += _plural(visits, "visit") if visits else "no visits"
    if waiting:
        label += f", {_plural(waiting, 'request')} waiting"
    return label


def _day_cell(day, today, picked, counts):
    visits = counts.get(day, {}).get("visits", 0)
    waiting = counts.get(day, {}).get("waiting", 0)
    total = visits + waiting
    if total == 0:
        pill = ""
    elif total > 9:
        pill = "9+"
    else:
        pill = str(total)
    return {
        "date": day,
        "day": day.day,
        "in_month": True,
        "is_today": day == today,
        "is_picked": day == picked,
        "is_weekend": day.weekday() >= 5,
        "visits": visits,
        "waiting": waiting,
        "total": total,
        "has_waiting": waiting > 0,
        "pill": pill,
        "aria_label": _cell_aria_label(day, today, visits, waiting),
    }


def month_cells(month_first, today, picked, counts):
    """Exactly 42 cells, Monday first; days outside the month are blanks."""
    blank = {"in_month": False}
    cells = [blank] * month_first.weekday()
    for n in range(_last_day(month_first).day):
        day = month_first + timedelta(days=n)
        cells.append(_day_cell(day, today, picked, counts))
    cells += [blank] * (CALENDAR_CELLS - len(cells))
    return cells


def month_options(months, selected):
    """Month dropdown options: value 'YYYY-MM', label 'November 2026'."""
    return [
        {
            "value": _month_value(month),
            "label": date_format(month, "F Y"),
            "selected": month == selected,
        }
        for month in months
    ]


def dashboard_calendar_context(today, month_value=None, picked_value=None, clinic=None):
    """cal_* keys for the dashboard calendar. A picked day without a month
    opens on the picked day's month."""
    months = calendar_month_range(today, booking_limit(clinic))
    picked = resolve_picked(picked_value, today, months)
    if picked and not month_value:
        month = picked.replace(day=1)
    else:
        month = resolve_month(month_value, today, months)
    today_month = today.replace(day=1)
    return {
        "cal_month": month,
        "cal_month_value": _month_value(month),
        "cal_month_options": month_options(months, month),
        "cal_cells": month_cells(month, today, picked, month_day_counts(month)),
        "cal_picked": picked,
        "cal_today_month_value": _month_value(today_month),
        "cal_is_home": month == today_month and picked is None,
    }


def _slot_end(row, minutes):
    """When a row's slot ends, as an aware datetime in Manila time."""
    start = timezone.make_aware(datetime.combine(row.date, row.time))
    return start + timedelta(minutes=minutes)


def _next_visit_id(rows, now, minutes):
    """The first Confirmed row whose slot hasn't ended (one in progress
    still counts), or None."""
    for row in rows:
        if row.status == Appointment.CONFIRMED and _slot_end(row, minutes) > now:
            return row.id
    return None


def _schedule_summary(is_today, visits, done, to_go, waiting):
    """'3 visits · 1 done · 2 to go' today; '2 visits · 1 waiting for
    approval' on a picked day; 'No visits' when there are none."""
    parts = [_plural(visits, "visit") if visits else "No visits"]
    if is_today and visits:
        parts += [f"{done} done", f"{to_go} to go"]
    if not is_today and waiting:
        parts.append(f"{waiting} waiting for approval")
    return " · ".join(parts)


def day_schedule(day, today, now, clinic):
    """The schedule card for one day. Rows are Confirmed, Completed and
    No show visits; Pending requests are only counted (waiting). A picked
    day shows its first 3 rows, today shows every row."""
    rows = list(
        Appointment.objects.filter(date=day, status__in=VISIT_STATUSES)
        .select_related("pet", "owner", "service")
        .order_by("time", "id")
    )
    waiting = Appointment.objects.filter(date=day, status=WAITING_STATUS).count()
    is_today = day == today
    to_go = sum(row.status == Appointment.CONFIRMED for row in rows)
    done = len(rows) - to_go
    shown = rows if is_today else rows[:PICKED_DAY_ROWS]
    next_id = _next_visit_id(rows, now, slot_minutes(clinic)) if is_today else None
    return {
        "day": day,
        "is_today": is_today,
        "rows": rows,
        "shown_rows": shown,
        "more_count": len(rows) - len(shown),
        "visit_count": len(rows),
        "done_count": done,
        "to_go_count": to_go,
        "waiting_count": waiting,
        "next_id": next_id,
        "summary": _schedule_summary(is_today, len(rows), done, to_go, waiting),
    }


def pending_requests():
    """Every Pending request, any date, soonest first, no cap. The template
    marks past dates with `appointment.date < today` (the "Date has passed"
    tag), so no flag is set here."""
    return list(
        Appointment.objects.filter(status=WAITING_STATUS)
        .select_related("pet", "owner", "service")
        .order_by("date", "time", "id")
    )
