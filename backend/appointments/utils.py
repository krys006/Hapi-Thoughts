import datetime
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