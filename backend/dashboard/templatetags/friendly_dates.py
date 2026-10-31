"""Plain-language dates and times for the admin UI, in Manila time."""

from datetime import datetime, timedelta

from django import template
from django.utils import timezone
from django.utils.dateformat import format as date_format

register = template.Library()


def _label_for_day(day, today):
    """'Today' / 'Yesterday' / 'Tomorrow', or None for other days."""
    if day == today:
        return "Today"
    if day == today - timedelta(days=1):
        return "Yesterday"
    if day == today + timedelta(days=1):
        return "Tomorrow"
    return None


@register.filter
def friendly_date(value):
    """'Today', 'Tomorrow', 'Mon, Nov 2' or 'Mon, Nov 2, 2025'."""
    if not value:
        return ""
    if isinstance(value, datetime):
        value = (
            timezone.localtime(value).date()
            if timezone.is_aware(value)
            else value.date()
        )
    today = timezone.localdate()
    label = _label_for_day(value, today)
    if label:
        return label
    if value.year == today.year:
        return date_format(value, "D, M j")
    return date_format(value, "D, M j, Y")


@register.filter
def friendly_datetime(value):
    """'Today, 3:58 PM', 'Yesterday, 9:05 AM' or 'Mon, Nov 2, 3:58 PM'."""
    if not value:
        return ""
    local = timezone.localtime(value) if timezone.is_aware(value) else value
    return f"{friendly_date(local)}, {date_format(local, 'g:i A')}"
