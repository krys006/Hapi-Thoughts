"""Context for the page shell (header bell, sidebar badges, clinic name)."""

from functools import cached_property

from appointments.models import Appointment
from appointments.utils import get_clinic_settings
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
        # May be None on a fresh DB
        return get_clinic_settings()

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


class AuthShell:
    """What logged-out pages may show: the clinic (for the logo). Read only
    when a template asks, then cached; never creates a ClinicSettings row."""

    @cached_property
    def clinic(self):
        return get_clinic_settings()


def shell(request):
    """Adds `shell` for logged-in users. Anonymous pages get no `shell`, only
    `auth_shell` (the clinic, one lazy query)."""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {"auth_shell": AuthShell()}
    return {"shell": Shell(user)}
