"""Display helpers for pets in the admin UI."""

import re

from django import template

register = template.Library()

# Icon name (components/icon.html) and the words in Pet.species that pick it.
# Species is free text, so this matches whole words anywhere in it, ignoring
# case ("Persian cat" is a cat, "catfish" is not).
SPECIES_ICONS = [
    ("dog", ("dog", "puppy")),
    ("cat", ("cat", "kitten")),
    ("rabbit", ("rabbit", "bunny")),
    ("bird", ("bird", "parrot", "chicken", "lovebird")),
]
SPECIES_PATTERNS = [
    (icon, re.compile(r"\b(?:" + "|".join(words) + r")\b", re.IGNORECASE))
    for icon, words in SPECIES_ICONS
]


@register.filter
def species_icon(species):
    """Icon name for a pet with no photo: 'dog', 'cat', 'rabbit', 'bird',
    or 'paw-print' for anything else (or blank)."""
    text = species or ""
    for icon, pattern in SPECIES_PATTERNS:
        if pattern.search(text):
            return icon
    return "paw-print"
