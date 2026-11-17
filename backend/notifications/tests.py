import datetime
from io import StringIO
from unittest import mock
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from appointments.models import Appointment
from medical.models import MedicalRecord, Vaccination
from notifications.forms import NotificationPreferenceForm
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


class SendRemindersSharedDaysTests(FrozenRemindersTestBase):
    """Batch 5b-4: the follow-up lead time lives in medical/utils.py
    (FOLLOW_UP_REMINDER_DAYS, also named on the record form); the command
    reads it and still sends that many days before and on the day itself."""

    def test_follow_up_days_come_from_the_shared_constant(self):
        from medical import utils as medical_utils
        from notifications.management.commands import send_reminders

        self.assertIs(
            send_reminders.FOLLOW_UP_REMINDER_DAYS,
            medical_utils.FOLLOW_UP_REMINDER_DAYS,
        )
        self.assertEqual(medical_utils.FOLLOW_UP_REMINDER_DAYS, 3)
        # One pet per follow-up: the command sends one per pet per day
        for days in range(6):
            pet = Pet.objects.create(owner=self.owner, name=f"In {days}", species="Dog")
            MedicalRecord.objects.create(
                pet=pet,
                record_date=TODAY - datetime.timedelta(days=10),
                follow_up_required=True,
                follow_up_date=TODAY + datetime.timedelta(days=days),
            )

        def sent():
            return sorted(
                Notification.objects.filter(
                    notification_type=Notification.FOLLOWUP_REMINDER
                ).values_list("related_pet__name", flat=True)
            )

        self._run()
        self.assertEqual(sent(), ["In 0", "In 3"])

        Notification.objects.all().delete()
        with mock.patch.object(send_reminders, "FOLLOW_UP_REMINDER_DAYS", 5):
            self._run()
        self.assertEqual(sent(), ["In 0", "In 5"])


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


class ReminderChoiceTests(FrozenRemindersTestBase):
    """Slice 8a-2a: the two reminder timings are fixed choices (the fields
    stay IntegerFields). A saved value outside the list stays selectable."""

    APPOINTMENT_CHOICES = [
        (0, "On the same day"),
        (1, "1 day before"),
        (2, "2 days before"),
        (3, "3 days before"),
    ]
    VACCINATION_CHOICES = [
        (3, "3 days before"),
        (7, "7 days before"),
        (14, "14 days before"),
        (30, "30 days before"),
    ]

    def setUp(self):
        super().setUp()
        self.preference = NotificationPreference.objects.get(user=self.owner.user)

    def _form(self, data=None):
        return NotificationPreferenceForm(data, instance=self.preference)

    def _data(self, **fields):
        data = {"appointment_reminder_days": "1", "vaccination_reminder_days": "7"}
        data.update(fields)
        return data

    def test_choices_and_labels(self):
        form = self._form()
        self.assertEqual(
            list(form.fields["appointment_reminder_days"].choices),
            self.APPOINTMENT_CHOICES,
        )
        self.assertEqual(
            list(form.fields["vaccination_reminder_days"].choices),
            self.VACCINATION_CHOICES,
        )

    def test_the_fields_are_still_integers_and_save_as_integers(self):
        form = self._form(self._data(appointment_reminder_days="3"))
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.preference.refresh_from_db()
        self.assertEqual(self.preference.appointment_reminder_days, 3)
        self.assertIsInstance(self.preference.appointment_reminder_days, int)

    def test_every_listed_value_is_accepted(self):
        for name, choices in (
            ("appointment_reminder_days", self.APPOINTMENT_CHOICES),
            ("vaccination_reminder_days", self.VACCINATION_CHOICES),
        ):
            for days, _label in choices:
                with self.subTest(field=name, days=days):
                    form = self._form(self._data(**{name: str(days)}))
                    self.assertTrue(form.is_valid(), form.errors)

    def test_a_saved_value_outside_the_list_is_one_extra_choice(self):
        NotificationPreference.objects.filter(pk=self.preference.pk).update(
            appointment_reminder_days=5, vaccination_reminder_days=10
        )
        self.preference.refresh_from_db()
        form = self._form()
        self.assertEqual(
            list(form.fields["appointment_reminder_days"].choices),
            [*self.APPOINTMENT_CHOICES, (5, "5 days before (your earlier choice)")],
        )
        self.assertEqual(
            list(form.fields["vaccination_reminder_days"].choices),
            [*self.VACCINATION_CHOICES, (10, "10 days before (your earlier choice)")],
        )

    def test_saving_the_page_keeps_an_earlier_choice(self):
        NotificationPreference.objects.filter(pk=self.preference.pk).update(
            appointment_reminder_days=5
        )
        self.preference.refresh_from_db()
        form = self._form(self._data(appointment_reminder_days="5"))
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.preference.refresh_from_db()
        self.assertEqual(self.preference.appointment_reminder_days, 5)

    def test_the_extra_choice_is_only_for_the_saved_value(self):
        # Nothing saved outside the list: 5 is refused, not offered
        form = self._form(self._data(appointment_reminder_days="5"))
        self.assertFalse(form.is_valid())
        self.assertEqual(len(form.fields["appointment_reminder_days"].choices), 4)

    def test_any_other_value_is_refused(self):
        for name in ("appointment_reminder_days", "vaccination_reminder_days"):
            for value in ("9", "-1", "abc", "1.5", ""):
                with self.subTest(field=name, value=value):
                    form = self._form(self._data(**{name: value}))
                    self.assertFalse(form.is_valid())
                    self.assertEqual(form.errors[name], ["Choose one of the options."])

    def test_the_page_refuses_a_posted_9_and_keeps_the_saved_value(self):
        self.client.force_login(self.owner.user)
        response = self.client.post(
            reverse("owner_notification_preferences"),
            self._data(appointment_reminder_days="9"),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Choose one of the options.")
        self.preference.refresh_from_db()
        self.assertEqual(self.preference.appointment_reminder_days, 1)

    def test_the_page_lists_the_labels(self):
        self.client.force_login(self.owner.user)
        response = self.client.get(reverse("owner_notification_preferences"))
        for _days, label in self.APPOINTMENT_CHOICES + self.VACCINATION_CHOICES:
            with self.subTest(label=label):
                self.assertContains(response, f">{label}</option>")

    def test_day_0_sends_one_reminder_on_the_visit_day(self):
        """send_reminders fires on [reminder_days, 0]. With 0 days both are the
        same day, so one visit still gets one reminder (and no change to the
        command was needed)."""
        NotificationPreference.objects.filter(pk=self.preference.pk).update(
            appointment_reminder_days=0
        )
        visit = Appointment.objects.create(
            owner=self.owner,
            pet=self.pet,
            date=TODAY,
            time=datetime.time(10, 0),
            status=Appointment.CONFIRMED,
        )
        out = StringIO()
        call_command("send_reminders", stdout=out)
        call_command("send_reminders", stdout=StringIO())
        reminders = Notification.objects.filter(
            notification_type=Notification.APPOINTMENT_REMINDER,
            related_appointment=visit,
        )
        self.assertEqual(
            list(reminders.values_list("title", flat=True)),
            ["Your appointment is today"],
        )
        self.assertIn("Appointment reminders : 1 sent", out.getvalue())
