"""Plain-language dates and times for the admin UI, in Manila time.

friendly_date and long_date take an optional "mid" argument for use inside
a sentence: today/tomorrow/yesterday come out lowercase ("Confirmed for
today at 9:00 AM"); every other output is unchanged.
"""

from datetime import datetime, timedelta

from django import template
from django.utils import timezone
from django.utils.dateformat import format as date_format

register = template.Library()

# Filter argument for a date inside a sentence (lowercase relative labels).
MID_SENTENCE = "mid"


def _label_for_day(day, today):
    """'Today' / 'Yesterday' / 'Tomorrow', or None for other days."""
    if day == today:
        return "Today"
    if day == today - timedelta(days=1):
        return "Yesterday"
    if day == today + timedelta(days=1):
        return "Tomorrow"
    return None


def _cased(label, style):
    """The label lowercased when style is "mid", as is otherwise."""
    return label.lower() if style == MID_SENTENCE else label


@register.filter
def friendly_date(value, style=None):
    """'Today', 'Tomorrow', 'Mon, Nov 2' or 'Mon, Nov 2, 2025'.
    With "mid": 'today', 'tomorrow', 'yesterday'."""
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
        return _cased(label, style)
    if value.year == today.year:
        return date_format(value, "D, M j")
    return date_format(value, "D, M j, Y")


@register.filter
def long_date(value, style=None):
    """'Today, Saturday, October 31', 'Tomorrow, Sunday, November 1',
    'Monday, November 2' (this year) or 'Monday, January 4, 2027'.
    With "mid": 'today, Saturday, October 31'."""
    if not value:
        return ""
    return long_date_on(value, timezone.localdate(), style)


def long_date_on(value, today, style=None):
    """long_date with today passed in (for helpers that take today)."""
    if value == today:
        return f"{_cased('Today', style)}, {date_format(value, 'l, F j')}"
    if value == today + timedelta(days=1):
        return f"{_cased('Tomorrow', style)}, {date_format(value, 'l, F j')}"
    if value.year == today.year:
        return date_format(value, "l, F j")
    return date_format(value, "l, F j, Y")


@register.filter
def friendly_datetime(value):
    """'Today, 3:58 PM', 'Yesterday, 9:05 AM' or 'Mon, Nov 2, 3:58 PM'."""
    if not value:
        return ""
    local = timezone.localtime(value) if timezone.is_aware(value) else value
    return f"{friendly_date(local)}, {date_format(local, 'g:i A')}"
