"""Money for the billing templates: {% load billing_extras %}."""

from decimal import Decimal, InvalidOperation

from django import template

register = template.Library()


@register.filter
def peso(value):
    """₱ + comma thousands + 2 decimals: 0 → ₱0.00, 1250.5 → ₱1,250.50.
    "" for no value (or something that isn't a number)."""
    if value is None or value == "":
        return ""
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        return ""
    return f"₱{amount:,.2f}"
