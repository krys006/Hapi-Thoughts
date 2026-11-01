"""Display helpers for pets in the admin UI."""

from django import template

register = template.Library()

# Icon name (components/icon.html) and the words in Pet.species that pick it.
# Species is free text, so this matches words anywhere in it, ignoring case.
SPECIES_ICONS = [
    ("dog", ("dog", "puppy")),
    ("cat", ("cat", "kitten")),
    ("rabbit", ("rabbit", "bunny")),
    ("bird", ("bird", "parrot", "chicken", "lovebird")),
]


@register.filter
def species_icon(species):
    """Icon name for a pet with no photo: 'dog', 'cat', 'rabbit', 'bird',
    or 'paw-print' for anything else (or blank)."""
    text = (species or "").lower()
    for icon, words in SPECIES_ICONS:
        if any(word in text for word in words):
            return icon
    return "paw-print"
