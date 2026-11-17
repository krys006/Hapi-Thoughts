import datetime
import re
from decimal import Decimal
from io import StringIO
from unittest import mock
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.management import call_command
from django.db import connection
from django.template.loader import get_template
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from appointments.models import Appointment
from billing.models import BillingReceipt
from billing.utils import notify_receipt_paid
from medical.models import MedicalRecord, Vaccination
from medical.utils import (
    notify_record_created,
    send_followup_reminder,
    send_vaccination_reminder,
)
from notifications.forms import NotificationPreferenceForm
from notifications.models import Notification, NotificationPreference
from notifications.utils import (
    EMAIL_FAILED,
    EMAIL_NO_ADDRESS,
    EMAIL_OFF,
    EMAIL_SENT,
    NOTIFICATION_ICONS,
    notification_icon,
    notification_target,
    notify,
    short_day,
    when_line,
)
from pets.models import Pet, PetOwner
from pets.utils import notify_deletion_answer

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
            ["Visit tomorrow"],  # 8a-2b: was "Upcoming appointment in 1 day"
        )

    def test_today_is_the_manila_date(self):
        today = self._confirmed(TODAY)
        yesterday = self._confirmed(TODAY - datetime.timedelta(days=1))
        self._run()
        self.assertEqual(
            list(self._reminders(today).values_list("title", flat=True)),
            ["Visit today"],  # 8a-2b: was "Your appointment is today"
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
            ["Visit today"],  # 8a-2b: was "Your appointment is today"
        )
        self.assertIn("Appointment reminders : 1 sent", out.getvalue())


# ─── Slice 8a-2b: compact owner text, targets, icons, the owner page ─────────


class WhenLineTests(SimpleTestCase):
    """when_line and short_day: Manila-style dates, year only when it isn't
    today's year."""

    TODAY = datetime.date(2026, 11, 17)

    def test_same_year_has_no_year(self):
        self.assertEqual(
            when_line(datetime.date(2026, 11, 2), datetime.time(15, 0), self.TODAY),
            "Mon, Nov 2 at 3:00 PM",
        )

    def test_another_year_shows_the_year(self):
        self.assertEqual(
            when_line(datetime.date(2027, 1, 5), datetime.time(15, 0), self.TODAY),
            "Tue, Jan 5, 2027 at 3:00 PM",
        )
        self.assertEqual(
            when_line(datetime.date(2025, 12, 31), datetime.time(9, 30), self.TODAY),
            "Wed, Dec 31, 2025 at 9:30 AM",
        )

    def test_no_time_is_just_the_date(self):
        self.assertEqual(
            when_line(datetime.date(2026, 11, 2), None, self.TODAY), "Mon, Nov 2"
        )
        self.assertEqual(
            when_line(datetime.date(2027, 1, 5), None, self.TODAY), "Tue, Jan 5, 2027"
        )

    def test_morning_noon_and_midnight(self):
        day = datetime.date(2026, 11, 2)
        cases = ((8, 0, "8:00 AM"), (12, 0, "12:00 PM"), (0, 5, "12:05 AM"))
        for hour, minute, text in cases:
            with self.subTest(text=text):
                line = when_line(day, datetime.time(hour, minute), self.TODAY)
                self.assertEqual(line, f"Mon, Nov 2 at {text}")

    def test_short_day(self):
        self.assertEqual(short_day(datetime.date(2026, 11, 9), self.TODAY), "Nov 9")
        self.assertEqual(
            short_day(datetime.date(2027, 11, 9), self.TODAY), "Nov 9, 2027"
        )


class NotifyEmailBodyTests(TestCase):
    """notify(email_body=...): the email says the full text, the stored
    message stays compact; without it the email is the message as before."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="ana_cruz", email="ana@example.com", role=User.PET_OWNER
        )

    def _notify(self, **extra):
        return notify(
            recipient=self.user,
            notification_type=Notification.VACCINATION_REMINDER,
            title="Vaccine due soon",
            message="Bantay · Anti-rabies, due Nov 9",
            email_subject="Vaccination Reminder — Hapi Vet",
            **extra,
        )

    def test_email_body_is_used_and_the_message_stays_compact(self):
        notification = self._notify(email_body="The long sentence for the email.")
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].body, "The long sentence for the email.")
        self.assertEqual(mail.outbox[0].subject, "Vaccination Reminder — Hapi Vet")
        self.assertEqual(notification.message, "Bantay · Anti-rabies, due Nov 9")
        self.assertEqual(
            Notification.objects.get().message, "Bantay · Anti-rabies, due Nov 9"
        )

    def test_no_email_body_sends_the_message_as_before(self):
        self._notify()
        self.assertEqual(mail.outbox[0].body, "Bantay · Anti-rabies, due Nov 9")

    def test_subject_still_defaults_to_the_title(self):
        notify(
            recipient=self.user,
            notification_type=Notification.VACCINATION_REMINDER,
            title="Vaccine due soon",
            message="M",
            email_body="B",
        )
        self.assertEqual(mail.outbox[0].subject, "Vaccine due soon")


class OwnerNotificationTextTests(FrozenRemindersTestBase):
    """Every notification to a pet owner: a short title, a compact message
    and the full sentence in the email. Frozen at 00:30 on Tue, Nov 3, 2026
    (Manila). Notifications to the admin keep their old long text."""

    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_user(
            username="admin",
            email="admin@example.com",
            password="unused-pass-123",
            role=User.ADMIN,
            is_onboarded=True,
        )

    def _visit(self, status=Appointment.PENDING, day=5, hour=14):
        return Appointment.objects.create(
            owner=self.owner,
            pet=self.pet,
            date=datetime.date(2026, 11, day),
            time=datetime.time(hour, 0),
            status=status,
        )

    def _admin_post(self, name, pk, data=None):
        self.client.force_login(self.admin)
        return self.client.post(reverse(name, args=[pk]), data or {})

    def _owner_notice(self, **filters):
        return Notification.objects.get(recipient=self.owner.user, **filters)

    def test_visit_confirmed(self):
        visit = self._visit()
        self._admin_post("admin_appointment_approve", visit.pk)
        notice = self._owner_notice()
        self.assertEqual(notice.notification_type, "appointment_approved")
        self.assertEqual(notice.title, "Visit confirmed")
        self.assertEqual(notice.message, "Bantay · Thu, Nov 5 at 2:00 PM")
        self.assertEqual(
            mail.outbox[0].body,
            "Your appointment for Bantay on Thu, Nov 5 at 2:00 PM has been confirmed.",
        )
        self.assertEqual(mail.outbox[0].subject, "Appointment Confirmed — Hapi Vet")

    def test_visit_request_declined_without_a_clinic_message(self):
        visit = self._visit()
        self._admin_post("admin_appointment_reject", visit.pk)
        notice = self._owner_notice()
        self.assertEqual(notice.notification_type, "appointment_rejected")
        self.assertEqual(notice.title, "Visit request declined")
        self.assertEqual(notice.message, "Bantay · Thu, Nov 5 at 2:00 PM")
        self.assertEqual(
            mail.outbox[0].body,
            "Your appointment request for Bantay on Thu, Nov 5 at 2:00 PM was not "
            "confirmed by the clinic. Please contact the clinic for more information.",
        )
        self.assertEqual(mail.outbox[0].subject, "Appointment Update — Hapi Vet")

    def test_visit_request_declined_with_a_clinic_message(self):
        visit = self._visit()
        reason = "That day is fully booked."
        self._admin_post("admin_appointment_reject", visit.pk, {"message": reason})
        notice = self._owner_notice()
        self.assertEqual(
            notice.message, "Bantay · Thu, Nov 5 at 2:00 PM. That day is fully booked."
        )
        self.assertTrue(
            mail.outbox[0].body.endswith(
                "Message from the clinic: That day is fully booked."
            )
        )

    def test_a_long_clinic_message_is_trimmed_in_the_app_not_the_email(self):
        visit = self._visit()
        self._admin_post("admin_appointment_reject", visit.pk, {"message": "x" * 300})
        notice = self._owner_notice()
        clause = notice.message.split(". ", 1)[1]
        self.assertEqual(len(clause), 100)
        self.assertTrue(clause.endswith("…"))
        self.assertIn("x" * 300, mail.outbox[0].body)

    def test_the_clinic_cancelled_a_visit(self):
        visit = self._visit(Appointment.CONFIRMED)
        self._admin_post(
            "admin_appointment_cancel",
            visit.pk,
            {"cancellation_reason": Appointment.OTHER, "cancellation_detail": "Closed"},
        )
        notice = self._owner_notice()
        self.assertEqual(notice.notification_type, "appointment_cancelled")
        self.assertEqual(notice.title, "The clinic cancelled a visit")
        self.assertEqual(
            notice.message,
            "Bantay · Thu, Nov 5 at 2:00 PM. Please call us for a new time.",
        )
        self.assertEqual(
            mail.outbox[0].body,
            "Your appointment for Bantay on Thu, Nov 5 at 2:00 PM has been "
            "cancelled by the clinic. Please contact the clinic for more information.",
        )
        self.assertEqual(mail.outbox[0].subject, "Appointment Cancelled — Hapi Vet")

    def test_the_clinic_moved_your_visit(self):
        from appointments.views import _notify_moved

        _notify_moved(self._visit(Appointment.CONFIRMED), TODAY)
        notice = self._owner_notice()
        self.assertEqual(notice.notification_type, "appointment_approved")
        self.assertEqual(notice.title, "The clinic moved your visit")
        self.assertEqual(
            notice.message,
            "Bantay · now Thu, Nov 5 at 2:00 PM. The new time is confirmed.",
        )
        self.assertEqual(
            mail.outbox[0].body,
            "Your appointment for Bantay was moved to Thu, Nov 5 at 2:00 PM by the "
            "clinic. The new time is confirmed.",
        )
        self.assertEqual(mail.outbox[0].subject, "Appointment moved — Hapi Vet")

    def test_visit_booked_by_the_clinic(self):
        from appointments.utils import create_walkin_appointment

        create_walkin_appointment(
            self.owner,
            {"pet": self.pet, "date": TODAY, "time": datetime.time(10, 0)},
        )
        notice = self._owner_notice()
        self.assertEqual(notice.title, "Visit booked by the clinic")
        self.assertEqual(
            notice.message, "Bantay · Tue, Nov 3 at 10:00 AM. The time is confirmed."
        )
        self.assertEqual(
            mail.outbox[0].body,
            "The clinic booked a visit for Bantay on Tue, Nov 3 at 10:00 AM. "
            "The time is confirmed.",
        )
        self.assertEqual(mail.outbox[0].subject, "Visit booked — Hapi Vet")

    def test_visit_reminders_today_and_tomorrow(self):
        today_visit = self._visit(Appointment.CONFIRMED, day=3, hour=10)
        tomorrow_visit = self._visit(Appointment.CONFIRMED, day=4, hour=8)
        self._run()
        today_notice = self._owner_notice(related_appointment=today_visit)
        tomorrow_notice = self._owner_notice(related_appointment=tomorrow_visit)
        self.assertEqual(today_notice.title, "Visit today")
        self.assertEqual(today_notice.message, "Bantay · Tue, Nov 3 at 10:00 AM")
        self.assertEqual(tomorrow_notice.title, "Visit tomorrow")
        self.assertEqual(tomorrow_notice.message, "Bantay · Wed, Nov 4 at 8:00 AM")
        bodies = sorted(message.body for message in mail.outbox)
        self.assertEqual(
            bodies,
            [
                "Reminder: Bantay has an appointment on Wed, Nov 4 at 8:00 AM.",
                "Reminder: Bantay has an appointment today at 10:00 AM. "
                "We look forward to seeing you!",
            ],
        )
        self.assertEqual(
            {message.subject for message in mail.outbox},
            {"Appointment Reminder — Hapi Vet"},
        )

    def test_visit_reminder_a_few_days_ahead(self):
        NotificationPreference.objects.filter(user=self.owner.user).update(
            appointment_reminder_days=3
        )
        self._visit(Appointment.CONFIRMED, day=6, hour=9)
        self._run()
        notice = self._owner_notice()
        self.assertEqual(notice.title, "Visit in 3 days")
        self.assertEqual(notice.message, "Bantay · Fri, Nov 6 at 9:00 AM")

    def _dose(self, due_days):
        return Vaccination.objects.create(
            pet=self.pet,
            vaccine_name=Vaccination.ANTI_RABIES,
            date_administered=TODAY - datetime.timedelta(days=300),
            next_due_date=TODAY + datetime.timedelta(days=due_days),
        )

    def test_vaccine_due_soon_from_the_nightly_job(self):
        self._dose(7)
        self._run()
        notice = self._owner_notice()
        self.assertEqual(notice.notification_type, "vaccination_reminder")
        self.assertEqual(notice.title, "Vaccine due soon")
        self.assertEqual(notice.message, "Bantay · Anti-rabies, due Nov 10")
        self.assertEqual(
            mail.outbox[0].body,
            "Bantay's Anti-rabies vaccination is due on Tuesday, November 10, 2026. "
            "Please contact the clinic to schedule an appointment.",
        )
        self.assertEqual(mail.outbox[0].subject, "Vaccination Reminder — Hapi Vet")

    def test_vaccine_due_today_from_the_nightly_job(self):
        self._dose(0)
        self._run()
        notice = self._owner_notice()
        self.assertEqual(notice.title, "Vaccine due today")
        self.assertEqual(notice.message, "Bantay · Anti-rabies, due Nov 3")
        self.assertEqual(
            mail.outbox[0].body,
            "Bantay's Anti-rabies vaccination is due today. "
            "Please contact the clinic to schedule an appointment.",
        )

    def _followup(self, days):
        return MedicalRecord.objects.create(
            pet=self.pet,
            record_date=TODAY - datetime.timedelta(days=10),
            follow_up_required=True,
            follow_up_date=TODAY + datetime.timedelta(days=days),
        )

    def test_follow_up_visit_from_the_nightly_job(self):
        self._followup(3)
        self._run()
        notice = self._owner_notice()
        self.assertEqual(notice.notification_type, "followup_reminder")
        self.assertEqual(notice.title, "Follow-up visit")
        self.assertEqual(notice.message, "Bantay · Follow-up on Nov 6")
        self.assertEqual(
            mail.outbox[0].body,
            "Bantay has a follow-up visit scheduled for Friday, November 6, 2026. "
            "Please contact the clinic to confirm your appointment.",
        )
        self.assertEqual(mail.outbox[0].subject, "Follow-up Reminder — Hapi Vet")

    def test_follow_up_visit_today_from_the_nightly_job(self):
        self._followup(0)
        self._run()
        notice = self._owner_notice()
        self.assertEqual(notice.title, "Follow-up visit today")
        self.assertEqual(notice.message, "Bantay · Follow-up on Nov 3")
        self.assertEqual(
            mail.outbox[0].body,
            "Bantay has a follow-up visit scheduled for today. "
            "Please contact the clinic if you have any questions.",
        )

    def test_the_clinics_send_reminder_buttons(self):
        dose = self._dose(2)
        record = self._followup(4)
        send_vaccination_reminder(dose, TODAY)
        send_followup_reminder(record, TODAY)
        vaccine = self._owner_notice(notification_type="vaccination_reminder")
        followup = self._owner_notice(notification_type="followup_reminder")
        self.assertEqual(vaccine.title, "Vaccine due soon")
        self.assertEqual(vaccine.message, "Bantay · Anti-rabies, due Nov 5")
        self.assertEqual(followup.title, "Follow-up visit")
        self.assertEqual(followup.message, "Bantay · Follow-up on Nov 7")
        self.assertIn("is due on Thursday, November 5, 2026.", mail.outbox[0].body)
        self.assertIn("scheduled for Saturday, November 7, 2026.", mail.outbox[1].body)

    def test_a_follow_up_without_a_date(self):
        record = self._followup(4)
        record.follow_up_date = None
        send_followup_reminder(record, TODAY)
        notice = self._owner_notice()
        self.assertEqual(notice.message, "Bantay · A follow-up visit is recommended")
        self.assertIn("recommended", mail.outbox[0].body)

    def test_new_medical_record(self):
        record = MedicalRecord.objects.create(
            pet=self.pet, record_date=TODAY - datetime.timedelta(days=1)
        )
        notify_record_created(record, TODAY)
        notice = self._owner_notice()
        self.assertEqual(notice.title, "New medical record")
        self.assertEqual(notice.message, "Bantay · Record for Mon, Nov 2")
        self.assertEqual(
            mail.outbox[0].body,
            "A medical record has been created for Bantay on Monday, November 2, "
            "2026. You can view the details in your pet's medical history.",
        )
        self.assertEqual(mail.outbox[0].subject, "Medical Record Created — Hapi Vet")

    def test_pet_removal_answers(self):
        notify_deletion_answer(self.pet, True)
        removed = self._owner_notice(related_pet=None)
        self.assertEqual(removed.title, "Pet removed")
        self.assertEqual(
            removed.message, "Bantay · Removed from your account, as you asked."
        )
        self.assertEqual(
            mail.outbox[0].body,
            "The clinic removed Bantay from your account, as you asked.",
        )
        self.assertEqual(mail.outbox[0].subject, "Pet removed — Hapi Vet")
        notify_deletion_answer(self.pet, False)
        kept = self._owner_notice(related_pet=self.pet)
        self.assertEqual(kept.title, "Pet stays on your account")
        self.assertEqual(
            kept.message,
            "Bantay · The clinic kept your pet. Call us if you still want it removed.",
        )
        self.assertEqual(
            mail.outbox[1].body,
            "The clinic kept Bantay on your account. If you still want it "
            "removed, please contact the clinic.",
        )

    def test_bill_paid(self):
        receipt = BillingReceipt.objects.create(
            receipt_number="HV-2026-0004",
            owner=self.owner,
            pet=self.pet,
            payment_status=BillingReceipt.PAID,
            payment_date=TODAY,
            total_amount=Decimal("1350.00"),
        )
        notify_receipt_paid(receipt)
        notice = self._owner_notice()
        self.assertEqual(notice.notification_type, "billing_generated")
        self.assertEqual(notice.title, "Bill paid: ₱1,350.00")
        self.assertEqual(notice.message, "Bantay · HV-2026-0004. Paid on Tue, Nov 3.")
        self.assertEqual(
            mail.outbox[0].body,
            "Paid ₱1,350.00 on Tue, Nov 3. You can view or print it in Billing.",
        )
        self.assertEqual(mail.outbox[0].subject, "Bill paid: ₱1,350.00 — Hapi Vet")

    def test_notifications_to_the_admin_are_unchanged(self):
        from appointments.utils import notify_admin_of_request

        visit = self._visit()
        notify_admin_of_request(visit)
        notify_admin_of_request(visit, reschedule=True)
        self.client.force_login(self.owner.user)
        self.client.post(
            reverse("owner_appointment_cancel", args=[visit.pk]),
            {"cancellation_reason": Appointment.OTHER, "cancellation_detail": "Busy"},
        )
        rows = {
            row.notification_type: row
            for row in Notification.objects.filter(recipient=self.admin)
        }
        self.assertEqual(rows["appointment_cancelled"].title, "Appointment Cancelled")
        self.assertEqual(
            rows["appointment_cancelled"].message,
            "Ana Cruz has cancelled their appointment for Bantay on "
            "November 05, 2026 at 02:00 PM.",
        )
        requests = Notification.objects.filter(
            recipient=self.admin, notification_type="appointment_requested"
        ).order_by("pk")
        self.assertEqual(
            [(n.title, n.message) for n in requests],
            [
                (
                    "New Appointment Request",
                    "Ana Cruz has requested an appointment for Bantay on "
                    "November 05, 2026 at 02:00 PM.",
                ),
                (
                    "Appointment Reschedule Request",
                    "Ana Cruz has requested to reschedule their appointment for "
                    "Bantay to November 05, 2026 at 02:00 PM.",
                ),
            ],
        )


class NotificationTargetTests(FrozenRemindersTestBase):
    """notification_target: where a notification leads, and notification_open
    still redirects there (with the dashboard as the fallback)."""

    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_user(
            username="admin",
            email="admin@example.com",
            password="unused-pass-123",
            role=User.ADMIN,
            is_onboarded=True,
        )
        self.visit = Appointment.objects.create(
            owner=self.owner,
            pet=self.pet,
            date=TODAY,
            time=datetime.time(10, 0),
            status=Appointment.CONFIRMED,
        )
        self.receipt = BillingReceipt.objects.create(
            receipt_number="HV-2026-0001", owner=self.owner, pet=self.pet
        )

    def _notice(self, user=None, kind=Notification.APPOINTMENT_APPROVED, **related):
        return Notification.objects.create(
            recipient=user or self.owner.user,
            notification_type=kind,
            title="T",
            message="M",
            **related,
        )

    def _target(self, notice):
        # A fresh read, so recipient is loaded the way the page does it
        notice = Notification.objects.select_related("recipient").get(pk=notice.pk)
        return notification_target(notice)

    def test_every_type_leads_to_the_visit_the_bill_or_the_pet(self):
        for kind, _label in Notification.TYPE_CHOICES:
            with self.subTest(kind=kind):
                visit = self._notice(kind=kind, related_appointment=self.visit)
                bill = self._notice(kind=kind, related_billing=self.receipt)
                pet = self._notice(kind=kind, related_pet=self.pet)
                self.assertEqual(
                    self._target(visit),
                    reverse("owner_appointment_detail", args=[self.visit.pk]),
                )
                self.assertEqual(
                    self._target(bill),
                    reverse("owner_receipt_detail", args=[self.receipt.pk]),
                )
                self.assertEqual(
                    self._target(pet), reverse("owner_pet_detail", args=[self.pet.pk])
                )

    def test_the_visit_wins_over_the_bill_and_the_bill_over_the_pet(self):
        both = self._notice(
            related_appointment=self.visit,
            related_billing=self.receipt,
            related_pet=self.pet,
        )
        self.assertEqual(
            self._target(both),
            reverse("owner_appointment_detail", args=[self.visit.pk]),
        )
        bill_and_pet = self._notice(related_billing=self.receipt, related_pet=self.pet)
        self.assertEqual(
            self._target(bill_and_pet),
            reverse("owner_receipt_detail", args=[self.receipt.pk]),
        )

    def test_none_when_it_points_at_nothing(self):
        self.assertIsNone(self._target(self._notice()))
        # A removed pet: the link was cleared when the pet was archived
        removed = self._notice(
            kind=Notification.PET_DELETION_ANSWERED, related_pet=self.pet
        )
        Notification.objects.filter(pk=removed.pk).update(related_pet=None)
        self.assertIsNone(self._target(removed))

    def test_admin_recipients_get_the_admin_pages(self):
        visit = self._notice(self.admin, related_appointment=self.visit)
        bill = self._notice(self.admin, related_billing=self.receipt)
        pet = self._notice(self.admin, related_pet=self.pet)
        self.assertEqual(
            self._target(visit),
            reverse("admin_appointment_detail", args=[self.visit.pk]),
        )
        self.assertEqual(
            self._target(bill), reverse("admin_receipt_detail", args=[self.receipt.pk])
        )
        self.assertEqual(
            self._target(pet), reverse("admin_pet_detail", args=[self.pet.pk])
        )

    def test_notification_open_redirects_as_before_and_marks_read(self):
        self.client.force_login(self.owner.user)
        cases = [
            (
                self._notice(related_appointment=self.visit),
                reverse("owner_appointment_detail", args=[self.visit.pk]),
            ),
            (
                self._notice(related_billing=self.receipt),
                reverse("owner_receipt_detail", args=[self.receipt.pk]),
            ),
            (
                self._notice(related_pet=self.pet),
                reverse("owner_pet_detail", args=[self.pet.pk]),
            ),
            (self._notice(), reverse("owner_dashboard")),
        ]
        for notice, expected in cases:
            with self.subTest(expected=expected):
                response = self.client.get(
                    reverse("notification_open", args=[notice.pk])
                )
                self.assertRedirects(response, expected, fetch_redirect_response=False)
                notice.refresh_from_db()
                self.assertTrue(notice.is_read)

    def test_notification_open_for_the_admin_is_unchanged(self):
        self.client.force_login(self.admin)
        visit = self._notice(self.admin, related_appointment=self.visit)
        nothing = self._notice(self.admin)
        response = self.client.get(reverse("notification_open", args=[visit.pk]))
        self.assertRedirects(
            response,
            reverse("admin_appointment_detail", args=[self.visit.pk]),
            fetch_redirect_response=False,
        )
        response = self.client.get(reverse("notification_open", args=[nothing.pk]))
        self.assertRedirects(
            response, reverse("admin_dashboard"), fetch_redirect_response=False
        )

    def test_someone_elses_notification_is_a_404(self):
        mine = self._notice(self.owner.user, related_pet=self.pet)
        self.client.force_login(self.admin)
        response = self.client.get(reverse("notification_open", args=[mine.pk]))
        self.assertEqual(response.status_code, 404)


class NotificationIconTests(SimpleTestCase):
    """notification_icon: one icon per type, all of them in components/icon.html."""

    EXPECTED = {
        "appointment_approved": "calendar-check",
        "appointment_rejected": "calendar-x",
        "appointment_cancelled": "calendar-x",
        "appointment_reminder": "clock",
        "billing_generated": "receipt",
        "vaccination_reminder": "syringe",
        "followup_reminder": "calendar-clock",
        "pet_deletion_answered": "paw-print",
    }

    def test_the_mapping(self):
        self.assertEqual(NOTIFICATION_ICONS, self.EXPECTED)
        for kind, icon in self.EXPECTED.items():
            with self.subTest(kind=kind):
                notice = Notification(notification_type=kind)
                self.assertEqual(notification_icon(notice), icon)

    def test_other_types_get_the_bell(self):
        for kind in ("appointment_requested", "email_failed", "something_new"):
            with self.subTest(kind=kind):
                notice = Notification(notification_type=kind)
                self.assertEqual(notification_icon(notice), "bell")

    def test_every_icon_exists_in_the_icon_component(self):
        source = get_template("components/icon.html").template.source
        for icon in [*self.EXPECTED.values(), "bell"]:
            with self.subTest(icon=icon):
                self.assertIn(f'name == "{icon}"', source)


class OwnerNotificationsPageTests(FrozenRemindersTestBase):
    """/owner/notifications/ at 00:30 on Tue, Nov 3, 2026 (Manila)."""

    URL = "/owner/notifications/"
    MIDNIGHT = datetime.datetime(2026, 11, 3, 0, 0, tzinfo=MANILA)
    JUST_BEFORE = datetime.datetime(2026, 11, 2, 23, 59, 59, tzinfo=MANILA)

    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_user(
            username="admin",
            email="admin@example.com",
            password="unused-pass-123",
            role=User.ADMIN,
            is_onboarded=True,
        )
        other_user = User.objects.create_user(
            username="other",
            email="other@example.com",
            password="unused-pass-123",
            role=User.PET_OWNER,
            is_onboarded=True,
        )
        self.other = PetOwner.objects.create(
            user=other_user, first_name="Ben", last_name="Reyes"
        )

    def _notice(self, title="Title", user=None, created=None, is_read=False, **fields):
        notice = Notification.objects.create(
            recipient=user or self.owner.user,
            notification_type=fields.pop(
                "notification_type", Notification.APPOINTMENT_APPROVED
            ),
            title=title,
            message=f"{title} message",
            is_read=is_read,
            **fields,
        )
        if created:
            Notification.objects.filter(pk=notice.pk).update(created_at=created)
        return Notification.objects.get(pk=notice.pk)

    def _get(self, **params):
        self.client.force_login(self.owner.user)
        return self.client.get(self.URL, params)

    def test_the_url(self):
        self.assertEqual(reverse("owner_notifications"), self.URL)

    def test_the_owner_sees_the_page(self):
        self._notice()
        response = self._get()
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "owner/notifications/list.html")
        self.assertContains(response, "<h1>Notifications</h1>")

    def test_anonymous_goes_to_login(self):
        response = self.client.get(self.URL)
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response.url)

    def test_the_admin_is_sent_to_the_admin_dashboard(self):
        self.client.force_login(self.admin)
        response = self.client.get(self.URL)
        self.assertRedirects(
            response, reverse("admin_dashboard"), fetch_redirect_response=False
        )

    def test_an_owner_without_a_profile_goes_to_onboarding(self):
        bare = User.objects.create_user(
            username="bare",
            email="bare@example.com",
            password="unused-pass-123",
            role=User.PET_OWNER,
            is_onboarded=True,
        )
        self.client.force_login(bare)
        response = self.client.get(self.URL)
        self.assertRedirects(
            response, reverse("owner_onboarding_step1"), fetch_redirect_response=False
        )

    def test_today_and_earlier_split_at_the_manila_midnight(self):
        # 00:00 Manila is 16:00 UTC the day before: a UTC date would put both
        # of these on the same (earlier) day
        today = self._notice("Today one", created=self.MIDNIGHT)
        earlier = self._notice("Earlier one", created=self.JUST_BEFORE)
        response = self._get()
        self.assertEqual(
            [row["notification"] for row in response.context["today_rows"]], [today]
        )
        self.assertEqual(
            [row["notification"] for row in response.context["earlier_rows"]],
            [earlier],
        )
        self.assertContains(response, "Today</h2>")
        self.assertContains(response, "Earlier</h2>")

    def test_a_section_with_no_rows_is_left_out(self):
        self._notice(created=self.JUST_BEFORE)
        response = self._get()
        self.assertEqual(response.context["today_rows"], [])
        self.assertNotContains(response, "Today</h2>")

    def test_newest_first_inside_each_group(self):
        before = self.JUST_BEFORE
        newest = self._notice("Newest", created=before)
        oldest = self._notice("Oldest", created=before - datetime.timedelta(days=2))
        middle = self._notice("Middle", created=before - datetime.timedelta(hours=1))
        response = self._get()
        self.assertEqual(
            [row["notification"] for row in response.context["earlier_rows"]],
            [newest, middle, oldest],
        )

    def test_a_row_has_everything_the_page_needs(self):
        notice = self._notice(
            "Visit confirmed",
            created=self.MIDNIGHT,
            related_pet=self.pet,
            notification_type=Notification.APPOINTMENT_APPROVED,
        )
        (row,) = self._get().context["today_rows"]
        self.assertEqual(
            set(row),
            {
                "notification",
                "title",
                "message",
                "icon",
                "is_unread",
                "target",
                "created_at",
            },
        )
        self.assertEqual(row["notification"], notice)
        self.assertEqual(row["title"], "Visit confirmed")
        self.assertEqual(row["message"], "Visit confirmed message")
        self.assertEqual(row["icon"], "calendar-check")
        self.assertEqual(row["created_at"], notice.created_at)

    def test_the_unread_flag_and_count(self):
        self._notice("Unread", created=self.MIDNIGHT)
        self._notice("Read", created=self.MIDNIGHT, is_read=True)
        self._notice("Someone else's", user=self.other.user, created=self.MIDNIGHT)
        response = self._get()
        rows = response.context["today_rows"]
        flags = {row["title"]: row["is_unread"] for row in rows}
        self.assertEqual(flags, {"Unread": True, "Read": False})
        self.assertEqual(response.context["unread_count"], 1)
        self.assertContains(response, 'data-unread="true"', count=1)

    def test_the_link_goes_through_notification_open(self):
        notice = self._notice(related_pet=self.pet, created=self.MIDNIGHT)
        response = self._get()
        (row,) = response.context["today_rows"]
        open_url = reverse("notification_open", args=[notice.pk])
        self.assertEqual(row["target"], open_url)
        self.assertContains(response, f'<a href="{open_url}">')
        # Following it marks the notification read and lands on the pet
        followed = self.client.get(open_url)
        self.assertRedirects(
            followed,
            reverse("owner_pet_detail", args=[self.pet.pk]),
            fetch_redirect_response=False,
        )
        notice.refresh_from_db()
        self.assertTrue(notice.is_read)

    def test_no_link_when_there_is_nowhere_to_go(self):
        notice = self._notice("Pet removed", created=self.MIDNIGHT)
        response = self._get()
        (row,) = response.context["today_rows"]
        self.assertIsNone(row["target"])
        self.assertContains(response, "Pet removed")
        self.assertNotContains(response, reverse("notification_open", args=[notice.pk]))

    def test_25_to_a_page(self):
        for number in range(30):
            self._notice(f"N{number}", created=self.MIDNIGHT)
        first = self._get()
        self.assertEqual(len(first.context["today_rows"]), 25)
        self.assertEqual(first.context["next_page_query"], "?page=2")
        self.assertEqual(first.context["prev_page_query"], "")
        second = self._get(page=2)
        self.assertEqual(len(second.context["today_rows"]), 5)
        self.assertEqual(second.context["prev_page_query"], "?page=1")
        self.assertEqual(second.context["next_page_query"], "")
        self.assertContains(first, 'href="?page=2"')
        # Page 2 holds the five oldest, so every title is on exactly one page
        titles = [r["title"] for r in first.context["today_rows"]]
        titles += [r["title"] for r in second.context["today_rows"]]
        self.assertEqual(len(set(titles)), 30)

    def test_the_unread_count_covers_every_page(self):
        for number in range(30):
            self._notice(f"N{number}", created=self.MIDNIGHT)
        self.assertEqual(self._get().context["unread_count"], 30)

    def test_another_users_notifications_are_never_shown(self):
        self._notice("Mine", created=self.MIDNIGHT)
        self._notice("Theirs", user=self.other.user, created=self.MIDNIGHT)
        self._notice("For the clinic", user=self.admin, created=self.MIDNIGHT)
        response = self._get()
        self.assertContains(response, "Mine")
        self.assertNotContains(response, "Theirs")
        self.assertNotContains(response, "For the clinic")

    def test_the_mark_all_form_shows_only_with_unread(self):
        self._notice("Read", created=self.MIDNIGHT, is_read=True)
        self.assertNotContains(self._get(), "Mark all as read")
        self._notice("Unread", created=self.MIDNIGHT)
        response = self._get()
        self.assertContains(response, "Mark all as read")
        self.assertContains(response, 'name="return_to" value="page"')
        self.assertContains(response, "csrfmiddlewaretoken")

    def test_the_empty_page(self):
        response = self._get()
        self.assertContains(response, "No notifications yet")
        self.assertEqual(response.context["unread_count"], 0)

    def test_mark_all_as_read_from_the_page_comes_back_to_it(self):
        mine = self._notice("Mine", created=self.MIDNIGHT)
        theirs = self._notice("Theirs", user=self.other.user, created=self.MIDNIGHT)
        self.client.force_login(self.owner.user)
        response = self.client.post(
            reverse("notification_mark_all_read"), {"return_to": "page"}
        )
        self.assertRedirects(
            response, reverse("owner_notifications"), fetch_redirect_response=False
        )
        mine.refresh_from_db()
        theirs.refresh_from_db()
        self.assertTrue(mine.is_read)
        self.assertIsNotNone(mine.read_at)
        self.assertFalse(theirs.is_read)

    def test_mark_all_without_return_to_keeps_the_panel_behaviour(self):
        mine = self._notice("Mine", created=self.MIDNIGHT)
        self.client.force_login(self.owner.user)
        other_values = ({}, {"return_to": "somewhere"}, {"return_to": "https://x.test"})
        for data in other_values:
            with self.subTest(data=data):
                mine.is_read = False
                mine.save(update_fields=["is_read"])
                response = self.client.post(reverse("notification_mark_all_read"), data)
                self.assertEqual(response.status_code, 200)
                self.assertTemplateUsed(response, "shared/notifications/_panel.html")
                mine.refresh_from_db()
                self.assertTrue(mine.is_read)

    def test_mark_all_still_refuses_a_get(self):
        self.client.force_login(self.owner.user)
        response = self.client.get(reverse("notification_mark_all_read"))
        self.assertEqual(response.status_code, 405)

    def test_the_bell_panel_links_to_the_page(self):
        self.client.force_login(self.owner.user)
        response = self.client.get(reverse("notifications_panel"))
        self.assertContains(
            response,
            f'<a href="{self.URL}" class="notification-mark-all-btn">'
            "See all notifications</a>",
            count=1,
        )
        self.client.force_login(self.admin)
        response = self.client.get(reverse("notifications_panel"))
        self.assertNotContains(response, "See all notifications")

    def _query_count(self):
        self.client.force_login(self.owner.user)
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(self.URL)
        self.assertEqual(response.status_code, 200)
        return len(queries)

    def test_the_query_count_is_flat(self):
        for number in range(3):
            self._notice(f"N{number}", related_pet=self.pet, created=self.MIDNIGHT)
        few = self._query_count()
        for number in range(27):
            self._notice(f"M{number}", related_pet=self.pet, created=self.MIDNIGHT)
        self.assertEqual(self._query_count(), few)

    def test_every_a_tag_is_on_one_line_and_there_is_no_inline_script(self):
        for name in ("owner/notifications/list.html", "owner/notifications/_row.html"):
            source = get_template(name).template.source
            with self.subTest(template=name):
                # No line opens an <a …> tag without closing it
                self.assertEqual(re.findall(r"<a\b[^>\n]*$", source, re.M), [])
                for banned in ("onclick", "hx-confirm", "<script", "<!--"):
                    self.assertNotIn(banned, source)
