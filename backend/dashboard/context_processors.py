"""Context for the page shell (header bell, sidebar badges, clinic name)."""

from functools import cached_property

from appointments.models import Appointment, ClinicSettings
from notifications.models import Notification
from pets.models import PetDeletionRequest


class Shell:
    """Values shown in the header and sidebar of logged-in pages.

    Each value runs its query only when a template first reads it, then
    caches it for the rest of the request. HTMX partials that never show
    the header or sidebar therefore run no extra queries.
    """

    def __init__(self, user):
        self.user = user

    @cached_property
    def unread_count(self):
        return Notification.objects.filter(recipient=self.user, is_read=False).count()

    @cached_property
    def clinic(self):
        # .first() is the project rule for ClinicSettings; may be None on a fresh DB
        return ClinicSettings.objects.first()

    @cached_property
    def pending_appointments(self):
        if not self.user.is_admin:
            return 0
        return Appointment.objects.filter(status=Appointment.PENDING).count()

    @cached_property
    def pending_deletions(self):
        if not self.user.is_admin:
            return 0
        return PetDeletionRequest.objects.filter(
            status=PetDeletionRequest.PENDING
        ).count()


def shell(request):
    """Adds `shell` for logged-in users only (nothing for anonymous pages)."""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {}
    return {"shell": Shell(user)}
