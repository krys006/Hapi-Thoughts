"""Small template helpers for the owner layout and the flash component."""

from django import template

from accounts.utils import owner_tab

register = template.Library()


@register.simple_tag(takes_context=True)
def owner_active_tab(context):
    """Active bottom tab for the current page ("" when none), from the URL name."""
    request = context.get("request")
    match = getattr(request, "resolver_match", None)
    return owner_tab(match.url_name if match else "")


@register.filter
def flash_lead(message):
    """Split a message at its first ". " into (lead, rest).

    One sentence gives (text, ""), so the template shows it all in normal
    weight; two or more give a lead sentence to show in bold.
    """
    text = str(message).strip()
    lead, found, rest = text.partition(". ")
    if not found or not rest.strip():
        return (text, "")
    return (lead + ".", rest.strip())
