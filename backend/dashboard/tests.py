from datetime import date, datetime, time, timedelta
from datetime import timezone as dt_timezone
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.urls import reverse

from appointments.models import Appointment
from dashboard.context_processors import shell
from dashboard.templatetags.friendly_dates import friendly_date, friendly_datetime
from notifications.models import Notification
from pets.models import Pet, PetDeletionRequest, PetOwner

User = get_user_model()

# Noon in Manila on Jun 15 2026 (04:00 UTC). Patched in as "now" so the
# date tests give the same result on any day they run.
FROZEN_NOW = datetime(2026, 6, 15, 4, 0, tzinfo=dt_timezone.utc)


class ShellTestBase(TestCase):
    """One admin, plus one owner with a pet."""

    def setUp(self):
        self.factory = RequestFactory()
        self.admin = User.objects.create_user(
            username="admin",
            email="admin@example.com",
            password="unused-pass-123",
            role=User.ADMIN,
            is_onboarded=True,
        )
        owner_user = User.objects.create_user(
            username="owner",
            email="owner@example.com",
            password="unused-pass-123",
            role=User.PET_OWNER,
            is_onboarded=True,
        )
        self.owner = PetOwner.objects.create(
            user=owner_user, first_name="Ana", last_name="Cruz"
        )
        self.pet = Pet.objects.create(owner=self.owner, name="Bantay", species="Dog")

    # ─── Helpers ──────────────────────────────────────────────────────────────

    def _context(self, user):
        """What the shell context processor returns for this user."""
        request = self.factory.get("/")
        request.user = user
        return shell(request)

    def _shell(self, user):
        return self._context(user)["shell"]

    def _appointment(self, status):
        return Appointment.objects.create(
            owner=self.owner,
            pet=self.pet,
            date=date(2026, 6, 20),
            time=time(9, 0),
            status=status,
        )

    def _deletion_request(self, status):
        return PetDeletionRequest.objects.create(
            pet=self.pet,
            requested_by=self.owner.user,
            reason=PetDeletionRequest.PASSED_AWAY,
            status=status,
        )

    def _notification(self, user, is_read=False):
        return Notification.objects.create(
            recipient=user,
            notification_type=Notification.APPOINTMENT_REQUESTED,
            title="Test",
            message="Test",
            is_read=is_read,
        )


class ShellContextProcessorTests(ShellTestBase):

    def test_anonymous_gets_no_shell(self):
        """Anonymous pages get no "shell" key."""
        self.assertNotIn("shell", self._context(AnonymousUser()))

    def test_no_queries_until_a_value_is_read(self):
        """Building the context runs no queries (HTMX partials stay cheap)."""
        with self.assertNumQueries(0):
            self._context(self.admin)

    def test_owner_sees_zero_admin_counts(self):
        """Owners get 0 for the admin sidebar counts, even with pending rows."""
        self._appointment(Appointment.PENDING)
        self._deletion_request(PetDeletionRequest.PENDING)
        owner_shell = self._shell(self.owner.user)
        self.assertEqual(owner_shell.pending_appointments, 0)
        self.assertEqual(owner_shell.pending_deletions, 0)

    def test_admin_counts_only_pending(self):
        """Admins see pending appointments and pending deletion requests."""
        self._appointment(Appointment.PENDING)
        self._appointment(Appointment.PENDING)
        self._appointment(Appointment.CONFIRMED)
        self._deletion_request(PetDeletionRequest.PENDING)
        self._deletion_request(PetDeletionRequest.REJECTED)
        admin_shell = self._shell(self.admin)
        self.assertEqual(admin_shell.pending_appointments, 2)
        self.assertEqual(admin_shell.pending_deletions, 1)

    def test_unread_count_is_own_unread_only(self):
        """Read notifications and other users' notifications are not counted."""
        self._notification(self.admin)
        self._notification(self.admin)
        self._notification(self.admin, is_read=True)
        self._notification(self.owner.user)
        self.assertEqual(self._shell(self.admin).unread_count, 2)


@mock.patch("django.utils.timezone.now", return_value=FROZEN_NOW)
class FriendlyDateTests(SimpleTestCase):

    def test_friendly_date(self, _now):
        """Relative labels near today, short dates otherwise."""
        today = date(2026, 6, 15)
        cases = {
            today: "Today",
            today - timedelta(days=1): "Yesterday",
            today + timedelta(days=1): "Tomorrow",
            date(2026, 3, 2): "Mon, Mar 2",
            date(2025, 11, 2): "Sun, Nov 2, 2025",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(friendly_date(value), expected)

    def test_empty_values(self, _now):
        """None and "" render as an empty string."""
        for value in (None, ""):
            with self.subTest(value=value):
                self.assertEqual(friendly_date(value), "")
                self.assertEqual(friendly_datetime(value), "")

    def test_just_after_midnight_manila_is_today(self, _now):
        """16:30 UTC on Jun 14 is 00:30 on Jun 15 in Manila."""
        value = datetime(2026, 6, 14, 16, 30, tzinfo=dt_timezone.utc)
        self.assertEqual(friendly_date(value), "Today")
        self.assertEqual(friendly_datetime(value), "Today, 12:30 AM")

    def test_friendly_datetime(self, _now):
        """Aware datetimes are shown in Manila time with a 12-hour clock."""
        cases = {
            # 01:05 UTC = 09:05 Manila
            datetime(2026, 6, 14, 1, 5, tzinfo=dt_timezone.utc): "Yesterday, 9:05 AM",
            # 07:58 UTC = 15:58 Manila
            datetime(2026, 3, 2, 7, 58, tzinfo=dt_timezone.utc): "Mon, Mar 2, 3:58 PM",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(friendly_datetime(value), expected)


class LogoutRedirectTests(ShellTestBase):

    def test_admin_goes_to_admin_login(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("logout"))
        self.assertRedirects(
            response, reverse("admin_login"), fetch_redirect_response=False
        )

    def test_owner_goes_to_owner_login(self):
        self.client.force_login(self.owner.user)
        response = self.client.get(reverse("logout"))
        self.assertRedirects(
            response, reverse("owner_login"), fetch_redirect_response=False
        )
