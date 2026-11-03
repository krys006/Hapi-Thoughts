import datetime
from io import StringIO
from unittest import mock
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.management import call_command
from django.test import TestCase

from appointments.models import Appointment
from medical.models import MedicalRecord, Vaccination
from notifications.models import Notification, NotificationPreference
from notifications.utils import (
    EMAIL_FAILED,
    EMAIL_NO_ADDRESS,
    EMAIL_OFF,
    EMAIL_SENT,
    notify,
)
from pets.models import Pet, PetOwner

User = get_user_model()

MANILA = ZoneInfo("Asia/Manila")
# Tue, Nov 3, 2026, 00:30 Manila: still Mon, Nov 2 in UTC
JUST_AFTER_MIDNIGHT = datetime.datetime(2026, 11, 3, 0, 30, tzinfo=MANILA)
TODAY = datetime.date(2026, 11, 3)


class FrozenRemindersTestBase(TestCase):
    """Ana Cruz and her dog Bantay, with time frozen at 00:30 Manila on
    Tue, Nov 3, 2026 (still Nov 2 in UTC)."""

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

    def _run(self):
        call_command("send_reminders", stdout=StringIO())


class SendRemindersManilaDateTests(FrozenRemindersTestBase):
    """send_reminders counts days from the Manila date. The owner keeps the
    default reminder preference (1 day before, plus the day itself)."""

    def _confirmed(self, date):
        return Appointment.objects.create(
            owner=self.owner,
            pet=self.pet,
            date=date,
            time=datetime.time(10, 0),
            status=Appointment.CONFIRMED,
        )

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


class SendRemindersDueRuleTests(FrozenRemindersTestBase):
    """Batch 5a-3: nightly vaccination reminders skip superseded doses and
    archived pets/owners; follow-up reminders skip archived pets/owners.
    Every dose and follow-up here is due today (Manila date), the day-of
    reminder."""

    def _dose(self, pet=None, given_days=-300, due_days=0, **fields):
        fields.setdefault("vaccine_name", Vaccination.ANTI_RABIES)
        return Vaccination.objects.create(
            pet=pet or self.pet,
            date_administered=TODAY + datetime.timedelta(days=given_days),
            next_due_date=TODAY + datetime.timedelta(days=due_days),
            **fields,
        )

    def _followup(self, pet=None):
        return MedicalRecord.objects.create(
            pet=pet or self.pet,
            record_date=TODAY - datetime.timedelta(days=10),
            follow_up_required=True,
            follow_up_date=TODAY,
        )

    def _sent(self, notification_type):
        return list(
            Notification.objects.filter(notification_type=notification_type)
            .order_by("related_pet__name")
            .values_list("related_pet__name", flat=True)
        )

    def test_a_due_dose_and_follow_up_are_sent(self):
        self._dose(is_corrected=True, correction_note="Fixed batch")
        self._followup()
        self._run()
        self.assertEqual(self._sent(Notification.VACCINATION_REMINDER), ["Bantay"])
        self.assertEqual(self._sent(Notification.FOLLOWUP_REMINDER), ["Bantay"])

    def test_superseded_dose_is_skipped(self):
        self._dose()
        # A later Anti-rabies dose, due next year
        self._dose(given_days=-2, due_days=363)
        self._run()
        self.assertEqual(self._sent(Notification.VACCINATION_REMINDER), [])

    def test_a_later_dose_of_another_vaccine_does_not_skip(self):
        self._dose()
        self._dose(given_days=-2, due_days=363, vaccine_name=Vaccination.DEWORMING)
        self._run()
        self.assertEqual(self._sent(Notification.VACCINATION_REMINDER), ["Bantay"])

    def test_archived_pet_is_skipped(self):
        archived = Pet.objects.create(
            owner=self.owner, name="Old", species="Cat", is_archived=True
        )
        self._dose(pet=archived)
        self._followup(pet=archived)
        self._run()
        self.assertEqual(self._sent(Notification.VACCINATION_REMINDER), [])
        self.assertEqual(self._sent(Notification.FOLLOWUP_REMINDER), [])

    def test_archived_owner_is_skipped(self):
        self._dose()
        self._followup()
        self.owner.archive()
        self._run()
        self.assertEqual(self._sent(Notification.VACCINATION_REMINDER), [])
        self.assertEqual(self._sent(Notification.FOLLOWUP_REMINDER), [])


class NotifyOutcomeTests(TestCase):
    """notify() returns the Notification with email_outcome (not saved),
    so a caller can say how the recipient was reached (5a-5)."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="ana_cruz", email="ana@example.com", role=User.PET_OWNER
        )

    def _notify(self, kind=Notification.VACCINATION_REMINDER):
        return notify(
            recipient=self.user, notification_type=kind, title="T", message="M"
        )

    def test_sent(self):
        notification = self._notify()
        self.assertEqual(notification.email_outcome, EMAIL_SENT)
        self.assertTrue(Notification.objects.get().email_sent)
        self.assertEqual(len(mail.outbox), 1)

    def test_no_address(self):
        User.objects.filter(pk=self.user.pk).update(email="")
        self.user.refresh_from_db()
        notification = self._notify()
        self.assertEqual(notification.email_outcome, EMAIL_NO_ADDRESS)
        self.assertEqual(len(mail.outbox), 0)

    def test_no_address_wins_over_a_preference_that_is_off(self):
        NotificationPreference.objects.filter(user=self.user).update(
            email_vaccination=False
        )
        User.objects.filter(pk=self.user.pk).update(email="")
        self.user.refresh_from_db()
        self.assertEqual(self._notify().email_outcome, EMAIL_NO_ADDRESS)

    def test_preference_off(self):
        NotificationPreference.objects.filter(user=self.user).update(
            email_vaccination=False
        )
        self.user.refresh_from_db()
        notification = self._notify()
        self.assertEqual(notification.email_outcome, EMAIL_OFF)
        self.assertEqual(len(mail.outbox), 0)
        # Another type with its preference on still emails
        followup = self._notify(Notification.FOLLOWUP_REMINDER)
        self.assertEqual(followup.email_outcome, EMAIL_SENT)

    def test_failed(self):
        with mock.patch(
            "notifications.utils.send_mail", side_effect=Exception("Brevo down")
        ):
            notification = self._notify()
        self.assertEqual(notification.email_outcome, EMAIL_FAILED)
        saved = Notification.objects.get()
        self.assertTrue(saved.email_failed)
        self.assertEqual(saved.email_error, "Brevo down")

    def test_provider_returned_no_send(self):
        with mock.patch("notifications.utils.send_mail", return_value=0):
            notification = self._notify()
        self.assertEqual(notification.email_outcome, EMAIL_FAILED)

    def test_in_app_notification_always_saved(self):
        User.objects.filter(pk=self.user.pk).update(email="")
        self.user.refresh_from_db()
        notification = self._notify()
        self.assertEqual(Notification.objects.get(), notification)
