import datetime
from io import StringIO
from unittest import mock
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from appointments.models import Appointment
from notifications.models import Notification
from pets.models import Pet, PetOwner

User = get_user_model()

MANILA = ZoneInfo("Asia/Manila")
# Tue, Nov 3, 2026, 00:30 Manila: still Mon, Nov 2 in UTC
JUST_AFTER_MIDNIGHT = datetime.datetime(2026, 11, 3, 0, 30, tzinfo=MANILA)
TODAY = datetime.date(2026, 11, 3)


class SendRemindersManilaDateTests(TestCase):
    """send_reminders counts days from the Manila date. The owner keeps the
    default reminder preference (1 day before, plus the day itself)."""

    def setUp(self):
        patcher = mock.patch(
            "django.utils.timezone.now", return_value=JUST_AFTER_MIDNIGHT
        )
        patcher.start()
        self.addCleanup(patcher.stop)

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

    def _confirmed(self, date):
        return Appointment.objects.create(
            owner=self.owner,
            pet=self.pet,
            date=date,
            time=datetime.time(10, 0),
            status=Appointment.CONFIRMED,
        )

    def _run(self):
        call_command("send_reminders", stdout=StringIO())

    def _reminders(self, appointment):
        return Notification.objects.filter(
            notification_type=Notification.APPOINTMENT_REMINDER,
            related_appointment=appointment,
        )

    def test_tomorrow_is_the_next_manila_day(self):
        tomorrow = self._confirmed(TODAY + datetime.timedelta(days=1))
        self._run()
        self.assertEqual(
            list(self._reminders(tomorrow).values_list("title", flat=True)),
            ["Upcoming appointment in 1 day"],
        )

    def test_today_is_the_manila_date(self):
        today = self._confirmed(TODAY)
        yesterday = self._confirmed(TODAY - datetime.timedelta(days=1))
        self._run()
        self.assertEqual(
            list(self._reminders(today).values_list("title", flat=True)),
            ["Your appointment is today"],
        )
        self.assertFalse(self._reminders(yesterday).exists())

    def test_a_second_run_the_same_night_sends_nothing_new(self):
        tomorrow = self._confirmed(TODAY + datetime.timedelta(days=1))
        self._run()
        self._run()
        self.assertEqual(self._reminders(tomorrow).count(), 1)
