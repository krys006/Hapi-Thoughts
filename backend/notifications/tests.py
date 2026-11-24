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
from django.test import Client, SimpleTestCase, TestCase
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
    EMAIL_PREFERENCE_MAP,
    EMAIL_NO_ADDRESS,
    EMAIL_OFF,
    EMAIL_SENT,
    NOTIFICATION_ICONS,
    notification_icon,
    notification_link_label,
    notification_target,
    notification_when,
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
        # 8a-2c: the subject names the pet (8a-2b: "Pet removed — Hapi Vet")
        self.assertEqual(mail.outbox[0].subject, "Bantay was removed — Hapi Vet")
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
        # 8a-2c (added check): the subject names the pet
        self.assertEqual(
            mail.outbox[1].subject, "Bantay stays on your account — Hapi Vet"
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
            "Paid ₱1,350.00 on Tue, Nov 3. You can view or print it in Bills.",
        )
        # 8a-2c: the subject names the receipt and the pet (8a-2b: "Bill paid:
        # ₱1,350.00 — Hapi Vet")
        self.assertEqual(
            mail.outbox[0].subject, "Bill HV-2026-0004 for Bantay — Hapi Vet"
        )

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
        "bill_sent": "receipt",
        "bill_reminder": "receipt",
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
        # 8b-9a: the h1 has its classes now (was a bare <h1>)
        self.assertContains(
            response,
            '<h1 class="flex-1 text-xl font-bold text-gray-900">Notifications</h1>',
        )

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
                # 8b-9a: the card's link words and its "1 hour ago" text
                "link_label",
                "when",
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
        # 8b-9a: the card's <a> has classes (was a bare href)
        self.assertContains(response, f'<a href="{open_url}" class="')
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

    def test_mark_all_without_return_to_goes_to_the_page(self):
        # Old: 200 with the shared panel. New: the owner never gets a panel.
        mine = self._notice("Mine", created=self.MIDNIGHT)
        self.client.force_login(self.owner.user)
        other_values = ({}, {"return_to": "somewhere"}, {"return_to": "https://x.test"})
        for data in other_values:
            with self.subTest(data=data):
                mine.is_read = False
                mine.save(update_fields=["is_read"])
                response = self.client.post(reverse("notification_mark_all_read"), data)
                self.assertRedirects(response, self.URL, fetch_redirect_response=False)
                mine.refresh_from_db()
                self.assertTrue(mine.is_read)

    def test_mark_all_still_refuses_a_get(self):
        self.client.force_login(self.owner.user)
        response = self.client.get(reverse("notification_mark_all_read"))
        self.assertEqual(response.status_code, 405)

    def test_the_bell_panel_sends_an_owner_to_the_page(self):
        # Old: the owner got a panel with a "See all notifications" link.
        self.client.force_login(self.owner.user)
        response = self.client.get(reverse("notifications_panel"))
        self.assertRedirects(response, self.URL, fetch_redirect_response=False)
        # The admin gets the admin panel, sent once without X-Admin-Layout
        self.client.force_login(self.admin)
        response = self.client.get(reverse("notifications_panel"))
        self.assertTemplateUsed(response, "admin/notifications/_panel.html")
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
        # 8b-9a: _row.html moved to notifications_parts/row.html (a plain
        # include); the card body and the pager are parts of the page now
        for name in (
            "owner/notifications/list.html",
            "owner/notifications_parts/row.html",
            "owner/notifications_parts/card_body.html",
            "owner/notifications_parts/pager.html",
        ):
            source = get_template(name).template.source
            with self.subTest(template=name):
                # No line opens an <a …> tag without closing it
                self.assertEqual(re.findall(r"<a\b[^>\n]*$", source, re.M), [])
                for banned in ("onclick", "hx-confirm", "<script", "<!--"):
                    self.assertNotIn(banned, source)


# ─── Slice 8b-9a: the owner notifications page and Email notifications ───────

NOW = datetime.datetime(2026, 11, 3, 15, 0, tzinfo=MANILA)
INLINE_SCRIPT = r"<script(?![^>]*\ssrc=)[^>]*>"


def _card(html, title):
    """The <li> of the notification card with this title."""
    for item in re.findall(r"(?s)<li\b.*?</li>", html):
        if f">{title}</span>" in item:
            return item
    raise AssertionError(f"No card titled {title!r}")


def _one_line_links(test, names):
    """No <a> … </a> in these templates spans two lines."""
    for name in names:
        source = get_template(name).template.source
        for link in re.findall(r"(?s)<a\b.*?</a>", source):
            with test.subTest(template=name, link=link[:50]):
                test.assertNotIn("\n", link)


class NotificationWhenTests(SimpleTestCase):
    """notification_when: the card's "1 hour ago" text, by the Manila clock."""

    def _when(self, created, now=NOW):
        return notification_when(created, now)

    def test_under_a_minute_is_just_now(self):
        for seconds in (0, 1, 59):
            with self.subTest(seconds=seconds):
                created = NOW - datetime.timedelta(seconds=seconds)
                self.assertEqual(self._when(created), "Just now")

    def test_a_clock_a_little_ahead_is_just_now(self):
        created = NOW + datetime.timedelta(seconds=30)
        self.assertEqual(self._when(created), "Just now")

    def test_minutes(self):
        cases = {60: "1 minute ago", 300: "5 minutes ago", 3599: "59 minutes ago"}
        for seconds, text in cases.items():
            with self.subTest(seconds=seconds):
                created = NOW - datetime.timedelta(seconds=seconds)
                self.assertEqual(self._when(created), text)

    def test_hours_the_same_day(self):
        cases = {1: "1 hour ago", 2: "2 hours ago", 14: "14 hours ago"}
        for hours, text in cases.items():
            with self.subTest(hours=hours):
                created = NOW - datetime.timedelta(hours=hours)
                self.assertEqual(self._when(created), text)

    def test_yesterday(self):
        late = datetime.datetime(2026, 11, 2, 23, 59, tzinfo=MANILA)
        early = datetime.datetime(2026, 11, 2, 0, 0, tzinfo=MANILA)
        self.assertEqual(self._when(late), "Yesterday")
        self.assertEqual(self._when(early), "Yesterday")

    def test_the_day_before_midnight_is_yesterday_not_minutes(self):
        now = datetime.datetime(2026, 11, 3, 0, 30, tzinfo=MANILA)
        created = datetime.datetime(2026, 11, 2, 23, 50, tzinfo=MANILA)
        self.assertEqual(self._when(created, now), "Yesterday")

    def test_the_manila_date_decides_not_the_utc_date(self):
        # 17:00 UTC on Nov 2 is 01:00 on Nov 3 in Manila: this morning
        created = datetime.datetime(2026, 11, 2, 17, 0, tzinfo=datetime.timezone.utc)
        self.assertEqual(self._when(created), "14 hours ago")

    def test_older_days_are_dates_with_the_year_only_when_not_this_year(self):
        this_year = datetime.datetime(2026, 10, 31, 9, 0, tzinfo=MANILA)
        last_year = datetime.datetime(2025, 12, 25, 9, 0, tzinfo=MANILA)
        self.assertEqual(self._when(this_year), "Oct 31")
        self.assertEqual(self._when(last_year), "Dec 25, 2025")


class NotificationLinkLabelTests(SimpleTestCase):
    """notification_link_label: the blue words at the end of a card."""

    def _notice(self, **fields):
        # Unsaved: the recipient's role is all notification_target reads
        recipient = User(role=User.PET_OWNER)
        return Notification(recipient=recipient, **fields)

    def test_a_visit_a_bill_and_a_pet(self):
        cases = (
            ({"related_appointment_id": 4}, "See visit"),
            ({"related_billing_id": 4}, "See bill"),
            ({"related_pet_id": 4}, "See pet"),
        )
        for fields, label in cases:
            with self.subTest(fields=fields):
                notice = self._notice(**fields)
                self.assertEqual(notification_link_label(notice), label)

    def test_the_visit_comes_first_like_the_target(self):
        notice = self._notice(related_appointment_id=1, related_pet_id=2)
        self.assertEqual(notification_link_label(notice), "See visit")

    def test_no_target_no_label(self):
        self.assertIsNone(notification_link_label(self._notice()))

    def test_booking_and_any_other_target(self):
        notice = self._notice(related_pet_id=2)
        booking = reverse("owner_book_appointment")
        with mock.patch("notifications.utils.notification_target") as target:
            target.return_value = booking
            self.assertEqual(notification_link_label(notice), "Book a visit")
            target.return_value = "/somewhere/"
            self.assertEqual(notification_link_label(self._notice()), "Open")


class OwnerNotificationsPageV2Tests(FrozenRemindersTestBase):
    """Slice 8b-9a: /owner/notifications/ on the owner layout, at 00:30 on
    Tue, Nov 3, 2026 (Manila)."""

    URL = "/owner/notifications/"
    MIDNIGHT = datetime.datetime(2026, 11, 3, 0, 0, tzinfo=MANILA)
    JUST_BEFORE = datetime.datetime(2026, 11, 2, 23, 59, 59, tzinfo=MANILA)
    TONES = {
        Notification.APPOINTMENT_APPROVED: "bg-blue-50 text-blue-700",
        Notification.APPOINTMENT_REJECTED: "bg-red-50 text-red-700",
        Notification.APPOINTMENT_CANCELLED: "bg-red-50 text-red-700",
        Notification.APPOINTMENT_REMINDER: "bg-violet-50 text-violet-700",
        Notification.BILLING_GENERATED: "bg-yellow-50 text-yellow-800",
        Notification.VACCINATION_REMINDER: "bg-green-50 text-green-700",
        Notification.FOLLOWUP_REMINDER: "bg-green-50 text-green-700",
        Notification.PET_DELETION_ANSWERED: "bg-gray-100 text-gray-700",
        "appointment_requested": "bg-gray-100 text-gray-700",
    }
    PARTS = [
        "owner/notifications/list.html",
        "owner/notifications_parts/row.html",
        "owner/notifications_parts/card_body.html",
        "owner/notifications_parts/pager.html",
    ]

    def setUp(self):
        super().setUp()
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

    def _get(self, user=None, **params):
        self.client.force_login(user or self.owner.user)
        return self.client.get(self.URL, params)

    def test_the_page_is_on_the_owner_layout_with_a_back_link(self):
        response = self._get()
        self.assertTemplateUsed(response, "layouts/owner.html")
        self.assertTemplateNotUsed(response, "base_dashboard.html")
        home = reverse("owner_dashboard")
        self.assertRegex(
            response.content.decode(),
            rf'<a href="{home}" class="[^"]*" aria-label="Back">',
        )
        self.assertContains(response, ">Notifications</h1>")

    def test_the_tabs_stay_and_none_is_lit(self):
        html = self._get().content.decode()
        nav = re.search(r'(?s)<nav aria-label="Main"[^>]*>(.*?)</nav>', html)
        self.assertIsNotNone(nav)
        self.assertEqual(nav.group(1).count("<a href="), 5)
        self.assertNotIn('aria-current="page"', nav.group(1))

    def test_mark_all_shows_only_with_something_unread(self):
        self._notice("Read", created=self.MIDNIGHT, is_read=True)
        self.assertNotContains(self._get(), "Mark all as read")
        self._notice("Unread", created=self.MIDNIGHT)
        html = self._get().content.decode()
        pattern = r'(?s)<form method="post" action="([^"]+)">(.*?)</form>'
        action, inside = re.search(pattern, html).groups()
        self.assertEqual(action, reverse("notification_mark_all_read"))
        self.assertIn('name="csrfmiddlewaretoken"', inside)
        self.assertIn('<input type="hidden" name="return_to" value="page">', inside)
        self.assertIn("Mark all as read", inside)
        self.assertIn("min-h-11", inside)

    def test_mark_all_is_a_post_that_needs_the_csrf_token(self):
        self._notice("Unread", created=self.MIDNIGHT)
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner.user)
        response = client.post(
            reverse("notification_mark_all_read"), {"return_to": "page"}
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Notification.objects.get().is_read)

    def test_today_and_earlier_are_h2_headings(self):
        self._notice("Today one", created=self.MIDNIGHT)
        self._notice("Earlier one", created=self.JUST_BEFORE)
        html = self._get().content.decode()
        for name, word in (("today", "Today"), ("earlier", "Earlier")):
            with self.subTest(group=word):
                heading = rf'<h2 id="notifications-{name}" class="[^"]*">{word}</h2>'
                self.assertRegex(html, heading)

    def test_an_unread_card(self):
        self._notice("Fresh", created=self.MIDNIGHT)
        card = _card(self._get().content.decode(), "Fresh")
        self.assertIn('data-unread="true"', card)
        self.assertIn("border-violet-200 bg-violet-50 shadow-sm", card)
        self.assertIn("font-bold", card)
        self.assertNotIn("font-semibold", card)
        self.assertIn("bg-violet-600", card)  # the dot
        self.assertIn('<span class="sr-only">New</span>', card)
        self.assertIn("Fresh message", card)

    def test_a_read_card(self):
        self._notice("Old", created=self.MIDNIGHT, is_read=True)
        card = _card(self._get().content.decode(), "Old")
        self.assertNotIn("data-unread", card)
        self.assertIn("border-gray-200 bg-white", card)
        self.assertIn("font-semibold", card)
        self.assertNotIn("font-bold", card)
        self.assertNotIn("violet", card)
        self.assertNotIn("New", card)

    def test_the_tile_colour_follows_the_icon(self):
        for number, kind in enumerate(self.TONES):
            self._notice(
                f"Kind {number}", created=self.MIDNIGHT, notification_type=kind
            )
        html = self._get().content.decode()
        for number, (kind, tone) in enumerate(self.TONES.items()):
            with self.subTest(kind=kind):
                card = _card(html, f"Kind {number}")
                self.assertIn(f"rounded-xl {tone}", card)
                self.assertIn("<svg", card)

    def test_when_text_and_link_label(self):
        visit = Appointment.objects.create(
            owner=self.owner,
            pet=self.pet,
            date=TODAY,
            time=datetime.time(10, 0),
            status=Appointment.CONFIRMED,
        )
        self._notice("A visit", created=self.MIDNIGHT, related_appointment=visit)
        self._notice("A pet", created=self.JUST_BEFORE, related_pet=self.pet)
        old = datetime.datetime(2026, 10, 31, 9, 0, tzinfo=MANILA)
        self._notice("Nowhere", created=old)
        html = self._get().content.decode()
        card = _card(html, "A visit")
        self.assertIn("30 minutes ago", card)
        self.assertRegex(card, r'<time datetime="[^"]+" class="text-gray-500">')
        self.assertIn('<span class="font-medium text-blue-600">See visit</span>', card)
        pet = _card(html, "A pet")
        self.assertIn("Yesterday", pet)
        self.assertIn(">See pet</span>", pet)
        nowhere = _card(html, "Nowhere")
        self.assertIn("Oct 31", nowhere)
        self.assertNotIn("text-blue-600", nowhere)
        self.assertNotIn("See ", nowhere)

    def test_a_card_is_one_link_to_notification_open(self):
        notice = self._notice("Linked", created=self.MIDNIGHT, related_pet=self.pet)
        self._notice("Plain", created=self.MIDNIGHT)
        html = self._get().content.decode()
        open_url = reverse("notification_open", args=[notice.pk])
        card = _card(html, "Linked")
        self.assertRegex(card, rf'(?s)<a href="{open_url}" class="[^"]*">.*</a>')
        self.assertEqual(html.count(f'href="{open_url}"'), 1)
        self.assertNotIn("<a ", _card(html, "Plain"))

    def test_another_owners_notifications_are_never_shown(self):
        self._notice("Mine", created=self.MIDNIGHT)
        self._notice("Theirs", user=self.other.user, created=self.MIDNIGHT)
        self.assertNotContains(self._get(), "Theirs")
        theirs = self._get(user=self.other.user)
        self.assertContains(theirs, "Theirs")
        self.assertNotContains(theirs, "Mine")

    def test_the_empty_page(self):
        response = self._get()
        self.assertContains(response, "No notifications yet")
        self.assertContains(
            response,
            "We'll tell you here when the clinic confirms a visit, records a "
            "payment, or a vaccine is due.",
        )
        for gone in ("adds a bill", "Mark all as read", "Today</h2>", "Earlier</h2>"):
            self.assertNotContains(response, gone)

    def test_the_pager(self):
        for number in range(30):
            self._notice(f"N{number}", created=self.MIDNIGHT)
        first = self._get().content.decode()
        self.assertIn("Page 1 of 2", first)
        self.assertRegex(first, r'<a href="\?page=2" class="[^"]*">Next</a>')
        self.assertNotIn(">Previous</a>", first)
        second = self._get(page=2).content.decode()
        self.assertIn("Page 2 of 2", second)
        self.assertRegex(second, r'<a href="\?page=1" class="[^"]*">Previous</a>')
        self.assertNotIn(">Next</a>", second)

    def test_one_page_has_no_pager(self):
        self._notice(created=self.MIDNIGHT)
        self.assertNotContains(self._get(), 'aria-label="Pages"')

    def test_the_bell_badge_caps_at_9_plus_and_hides_at_zero(self):
        self.client.force_login(self.owner.user)
        bell_page = reverse("owner_account")
        html = self.client.get(bell_page).content.decode()
        self.assertNotIn("bg-red-600", html.split("</header>")[0])
        for number in range(3):
            self._notice(f"N{number}")
        html = self.client.get(bell_page).content.decode()
        self.assertIn('aria-label="Notifications, 3 unread"', html)
        self.assertIn('aria-hidden="true">3</span>', html)
        for number in range(7):
            self._notice(f"M{number}")
        html = self.client.get(bell_page).content.decode()
        self.assertIn('aria-label="Notifications, 10 unread"', html)
        self.assertIn('aria-hidden="true">9+</span>', html)

    def test_every_a_tag_is_on_one_line(self):
        _one_line_links(self, self.PARTS)

    def test_no_inline_script_or_handlers(self):
        self._notice(created=self.MIDNIGHT, related_pet=self.pet)
        html = self._get().content.decode()
        self.assertIsNone(re.search(INLINE_SCRIPT, html))
        self.assertIsNone(re.search(r"\son[a-z]+\s*=", html))
        for name in self.PARTS:
            source = get_template(name).template.source
            for banned in ("<script", "hx-confirm", "<!--"):
                self.assertNotIn(banned, source)
            self.assertIsNone(re.search(r"\son[a-z]+\s*=", source))

    def _query_count(self):
        self.client.force_login(self.owner.user)
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(self.URL)
        self.assertEqual(response.status_code, 200)
        return len(queries)

    def test_the_whole_request_is_flat_for_3_and_25_cards(self):
        for number in range(3):
            self._notice(f"N{number}", related_pet=self.pet, created=self.MIDNIGHT)
        few = self._query_count()
        # session, user, owner, count, page, unread count
        self.assertEqual(few, 6)
        for number in range(22):
            self._notice(f"M{number}", related_pet=self.pet, created=self.MIDNIGHT)
        with self.assertNumQueries(few):
            response = self.client.get(self.URL)
        self.assertEqual(len(response.context["today_rows"]), 25)


class OwnerEmailSettingsPageTests(FrozenRemindersTestBase):
    """Slice 8b-9a: Email notifications on the owner layout (Account tab)."""

    SWITCHES = {
        "email_appointment_reminders": "Visit reminders",
        "email_appointment_status": "Visit updates",
        "email_billing": "Bills",
        "email_vaccination": "Vaccine reminders",
        "email_followup": "Follow-up reminders",
    }
    HIDE = "not-group-has-[input[type=checkbox]:checked]/pref:hidden"

    def setUp(self):
        super().setUp()
        self.url = reverse("owner_notification_preferences")
        self.preference = NotificationPreference.objects.get(user=self.owner.user)
        self.client.force_login(self.owner.user)

    def _html(self):
        return self.client.get(self.url).content.decode()

    def _post(self, **changes):
        data = {"appointment_reminder_days": "1", "vaccination_reminder_days": "7"}
        data.update(changes)
        return self.client.post(self.url, data)

    def test_the_page_is_on_the_owner_layout_with_the_account_tab_lit(self):
        response = self.client.get(self.url)
        self.assertTemplateUsed(response, "layouts/owner.html")
        self.assertTemplateUsed(response, "owner/notifications/preferences.html")
        self.assertTemplateNotUsed(response, "base_dashboard.html")
        html = response.content.decode()
        nav = re.search(r'(?s)<nav aria-label="Main"[^>]*>(.*?)</nav>', html).group(1)
        self.assertEqual(nav.count('aria-current="page"'), 1)
        current = re.search(r'<a href="([^"]+)"[^>]*aria-current="page"', nav)
        self.assertEqual(current.group(1), reverse("owner_account"))
        self.assertIn("Back to Account", html)
        self.assertIn(f'<a href="{reverse("owner_account")}"', html)

    def test_title_intro_and_the_blue_info_box(self):
        html = self._html()
        self.assertIn('<h1 class="text-2xl font-bold text-gray-900">', html)
        self.assertIn(">Email notifications</h1>", html)
        self.assertIn("Choose which emails you get.", html)
        box = re.search(r'(?s)<div class="[^"]*bg-blue-50[^"]*">(.*?)</div>', html)
        self.assertIn("border-blue-200", box.group(0))
        self.assertIn(
            "You'll always see these in the app (the bell). These switches only "
            "turn the <b>emails</b> on or off.",
            box.group(1),
        )

    def test_the_five_rows_in_one_card(self):
        html = self._html()
        card = re.search(r'(?s)<section class="divide-y[^"]*">(.*?)</section>', html)
        self.assertIsNotNone(card)
        rows = (
            ("Visit reminders", "A reminder before each visit."),
            ("Visit updates", "When the clinic confirms, moves or declines a visit."),
            ("Bills", "When the clinic sends a bill or records your payment."),
            ("Vaccine reminders", "When a vaccine is coming due."),
            ("Follow-up reminders", "When the vet asked to see your pet again."),
        )
        for title, text in rows:
            with self.subTest(title=title):
                self.assertIn(f">{title}</span>", card.group(1))
                self.assertIn(f">{text}</span>", card.group(1))
        self.assertEqual(card.group(1).count('class="group/pref py-4"'), 5)
        self.assertNotIn("New bills", html)

    def test_every_switch_is_a_real_checkbox_with_a_role_and_a_name(self):
        html = self._html()
        for name, title in self.SWITCHES.items():
            with self.subTest(field=name):
                tag = re.search(rf'<input[^>]*name="{name}"[^>]*>', html).group(0)
                self.assertIn('type="checkbox"', tag)
                self.assertIn('role="switch"', tag)
                self.assertIn(f'aria-label="{title}"', tag)
                self.assertIn("peer", tag)
                self.assertIn("sr-only", tag)

    def test_the_switch_look_and_a_whole_row_tap_area(self):
        html = self._html()
        self.assertEqual(html.count("h-8 w-14"), 5)
        self.assertEqual(html.count("bg-gray-300 transition-colors"), 5)
        self.assertEqual(html.count("peer-checked:bg-violet-600"), 5)
        self.assertEqual(html.count("peer-checked:translate-x-6"), 5)
        self.assertEqual(html.count("peer-focus-visible:ring-2"), 5)
        self.assertEqual(html.count("bg-white shadow transition-transform"), 5)  # knob
        # The label around the title and the switch is the tap area
        self.assertEqual(html.count('class="flex min-h-14 cursor-pointer'), 5)

    def test_the_timing_selects_and_their_choices(self):
        html = self._html()
        appointment = re.search(
            r'(?s)<select name="appointment_reminder_days".*?</select>', html
        ).group(0)
        vaccine = re.search(
            r'(?s)<select name="vaccination_reminder_days".*?</select>', html
        ).group(0)
        for label in ("On the same day", "1 day before", "2 days before"):
            self.assertIn(f">{label}</option>", appointment)
        self.assertIn(">3 days before</option>", appointment)
        for label in ("3 days before", "7 days before", "14 days before"):
            self.assertIn(f">{label}</option>", vaccine)
        self.assertIn(">30 days before</option>", vaccine)
        self.assertNotIn("On the same day", vaccine)
        self.assertNotIn("day(s) before", html)
        self.assertEqual(html.count(">Send it</label>"), 2)

    def test_the_timing_row_hides_with_css_only_and_shows_when_unsure(self):
        html = self._html()
        self.assertEqual(html.count(f"bg-gray-50 px-3 py-2 {self.HIDE}"), 2)
        # Nothing hides it by itself: the base classes have no hidden token
        for row in re.findall(r'<div class="mt-3 rounded-xl[^"]*">', html):
            self.assertNotRegex(row, r"(?<![\w:/\]-])hidden\b")
        self.assertIsNone(re.search(INLINE_SCRIPT, html))

    def test_a_timing_error_shows_even_with_its_switch_off(self):
        response = self._post(appointment_reminder_days="9")
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertEqual(html.count("Choose one of the options."), 1)
        # Only the vaccine row still hides with its (off) switch
        self.assertEqual(html.count(self.HIDE), 1)
        self.preference.refresh_from_db()
        self.assertEqual(self.preference.appointment_reminder_days, 1)

    def test_saved_values_round_trip(self):
        response = self._post(
            email_appointment_reminders="on",
            email_billing="on",
            appointment_reminder_days="3",
            vaccination_reminder_days="30",
        )
        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        self.preference.refresh_from_db()
        self.assertEqual(
            (
                self.preference.email_appointment_reminders,
                self.preference.email_appointment_status,
                self.preference.email_billing,
                self.preference.email_vaccination,
                self.preference.email_followup,
                self.preference.appointment_reminder_days,
                self.preference.vaccination_reminder_days,
            ),
            (True, False, True, False, False, 3, 30),
        )
        html = self._html()
        for name, title in self.SWITCHES.items():
            tag = re.search(rf'<input[^>]*name="{name}"[^>]*>', html).group(0)
            on = name in ("email_appointment_reminders", "email_billing")
            with self.subTest(field=name):
                self.assertEqual(" checked" in tag, on)
        self.assertIn('<option value="3" selected>3 days before</option>', html)
        self.assertIn('<option value="30" selected>30 days before</option>', html)

    def test_an_earlier_value_outside_the_list_is_still_there(self):
        NotificationPreference.objects.filter(pk=self.preference.pk).update(
            appointment_reminder_days=5, vaccination_reminder_days=10
        )
        html = self._html()
        for days in (5, 10):
            with self.subTest(days=days):
                self.assertIn(
                    f'<option value="{days}" selected>'
                    f"{days} days before (your earlier choice)</option>",
                    html,
                )
        # Saving as it is keeps it
        self._post(appointment_reminder_days="5", vaccination_reminder_days="10")
        self.preference.refresh_from_db()
        self.assertEqual(self.preference.appointment_reminder_days, 5)
        self.assertEqual(self.preference.vaccination_reminder_days, 10)

    def test_the_pinned_save_bar_above_the_tabs(self):
        html = self._html()
        pattern = (
            r'(?s)<div class="fixed inset-x-0 bottom-\[[^"]*" data-bar="tall">'
            r".*?</div>\s*</div>"
        )
        bar = re.search(pattern, html)
        self.assertIsNotNone(bar)
        self.assertIn('type="submit" form="email-settings-form"', bar.group(0))
        self.assertIn("Save settings", bar.group(0))
        self.assertIn("<svg", bar.group(0))
        self.assertIn('<form id="email-settings-form" method="post"', html)
        buttons = re.findall(r"<button\b[^>]*\bbg-violet-600\b[^>]*>", html)
        self.assertEqual(len(buttons), 1)
        self.assertLess(html.index('data-bar="tall"'), html.index('aria-label="Main"'))

    def test_saving_comes_back_with_the_flash(self):
        response = self._post(email_followup="on")
        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        page = self.client.get(self.url)
        self.assertContains(page, "Your email settings are saved.")
        # Shown once, then gone
        self.assertNotContains(self.client.get(self.url), "are saved.")

    def test_only_owners_and_only_their_own_row(self):
        admin = User.objects.create_user(
            username="admin",
            email="admin@example.com",
            password="unused-pass-123",
            role=User.ADMIN,
            is_onboarded=True,
        )
        self.client.force_login(admin)
        response = self.client.get(self.url)
        self.assertRedirects(
            response, reverse("admin_dashboard"), fetch_redirect_response=False
        )
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)

    def test_the_whole_request_is_flat(self):
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        # session, user, preference (the back bar has no logo or bell)
        self.assertEqual(len(queries), 3)
        for number in range(12):
            Notification.objects.create(
                recipient=self.owner.user,
                notification_type=Notification.APPOINTMENT_APPROVED,
                title=f"N{number}",
                message="m",
            )
        with self.assertNumQueries(len(queries)):
            self.client.get(self.url)


class BillSendNotificationTypeTests(FrozenRemindersTestBase):
    """Batch 4b: the bill_sent and bill_reminder types (the admin's Send bill
    / Remind to pay button)."""

    TYPES = (Notification.BILL_SENT, Notification.BILL_REMINDER)

    def setUp(self):
        super().setUp()
        self.receipt = BillingReceipt.objects.create(
            receipt_number="HV-2026-0001", owner=self.owner, pet=self.pet
        )

    def _notify(self, kind):
        return notify(
            recipient=self.owner.user,
            notification_type=kind,
            title="Bill ready: ₱650.00",
            message="Bantay · HV-2026-0001. Please pay in cash at the clinic.",
            related_billing=self.receipt,
            related_pet=self.pet,
        )

    def test_both_types_are_choices(self):
        values = [value for value, _label in Notification.TYPE_CHOICES]
        self.assertEqual(Notification.BILL_SENT, "bill_sent")
        self.assertEqual(Notification.BILL_REMINDER, "bill_reminder")
        for kind in self.TYPES:
            with self.subTest(kind=kind):
                self.assertIn(kind, values)

    def test_both_follow_the_bills_switch(self):
        for kind in self.TYPES:
            with self.subTest(kind=kind):
                self.assertEqual(EMAIL_PREFERENCE_MAP[kind], "email_billing")

    def test_with_the_bills_switch_on_the_email_goes(self):
        for kind in self.TYPES:
            with self.subTest(kind=kind):
                mail.outbox.clear()
                notice = self._notify(kind)
                self.assertEqual(notice.email_outcome, EMAIL_SENT)
                self.assertEqual(len(mail.outbox), 1)

    def test_with_the_bills_switch_off_only_the_in_app_row_is_made(self):
        # Through the user's own preference (the signal cached it on the user)
        preference = self.owner.user.notification_preference
        preference.email_billing = False
        preference.save()
        for kind in self.TYPES:
            with self.subTest(kind=kind):
                notice = self._notify(kind)
                self.assertEqual(notice.email_outcome, EMAIL_OFF)
                self.assertTrue(Notification.objects.filter(pk=notice.pk).exists())
                self.assertFalse(notice.email_sent)
        self.assertEqual(mail.outbox, [])

    def test_both_use_the_receipt_icon(self):
        for kind in self.TYPES:
            with self.subTest(kind=kind):
                self.assertEqual(NOTIFICATION_ICONS[kind], "receipt")
                self.assertEqual(
                    notification_icon(Notification(notification_type=kind)), "receipt"
                )
                self.assertEqual(NotificationIconTests.EXPECTED[kind], "receipt")

    def test_the_notification_opens_the_bill_page_and_has_no_visit(self):
        admin = User.objects.create_user(
            username="admin",
            email="admin@example.com",
            password="unused-pass-123",
            role=User.ADMIN,
            is_onboarded=True,
        )
        for kind in self.TYPES:
            with self.subTest(kind=kind):
                notice = self._notify(kind)
                notice = Notification.objects.select_related("recipient").get(
                    pk=notice.pk
                )
                self.assertIsNone(notice.related_appointment)
                self.assertEqual(notice.related_billing, self.receipt)
                self.assertEqual(
                    notification_target(notice),
                    reverse("owner_receipt_detail", args=[self.receipt.pk]),
                )
                self.assertEqual(notification_link_label(notice), "See bill")
                for_admin = Notification.objects.create(
                    recipient=admin,
                    notification_type=kind,
                    title="T",
                    message="M",
                    related_billing=self.receipt,
                )
                self.assertEqual(
                    notification_target(for_admin),
                    reverse("admin_receipt_detail", args=[self.receipt.pk]),
                )


class OwnerNotificationPanelRedirectTests(FrozenRemindersTestBase):
    """Audit cleanup part 1: a pet owner never gets the bell panel; the admin
    always gets the admin panel, with or without X-Admin-Layout."""

    PAGE = "/owner/notifications/"
    ADMIN_PANEL = "admin/notifications/_panel.html"

    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_user(
            username="admin",
            email="admin@example.com",
            password="unused-pass-123",
            role=User.ADMIN,
            is_onboarded=True,
        )

    def _assert_htmx_redirect(self, response):
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["HX-Redirect"], self.PAGE)
        self.assertEqual(response.content, b"")

    def test_owner_get_redirects_to_the_page(self):
        self.client.force_login(self.owner.user)
        response = self.client.get(reverse("notifications_panel"))
        self.assertRedirects(response, self.PAGE, fetch_redirect_response=False)

    def test_owner_htmx_get_gets_hx_redirect(self):
        self.client.force_login(self.owner.user)
        response = self.client.get(
            reverse("notifications_panel"), HTTP_HX_REQUEST="true"
        )
        self._assert_htmx_redirect(response)

    def test_owner_post_without_return_to_redirects_and_marks_read(self):
        mine = Notification.objects.create(
            recipient=self.owner.user,
            notification_type=Notification.APPOINTMENT_APPROVED,
            title="T",
            message="M",
        )
        self.client.force_login(self.owner.user)
        response = self.client.post(reverse("notification_mark_all_read"))
        self.assertRedirects(response, self.PAGE, fetch_redirect_response=False)
        mine.refresh_from_db()
        self.assertTrue(mine.is_read)

    def test_owner_htmx_post_without_return_to_gets_hx_redirect(self):
        self.client.force_login(self.owner.user)
        response = self.client.post(
            reverse("notification_mark_all_read"), HTTP_HX_REQUEST="true"
        )
        self._assert_htmx_redirect(response)

    def test_admin_panel_without_the_layout_header_is_the_admin_one(self):
        self.client.force_login(self.admin)
        for name in ("notifications_panel",):
            with self.subTest(name=name):
                response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 200)
                self.assertTemplateUsed(response, self.ADMIN_PANEL)

    def test_admin_mark_all_read_without_the_header_is_the_admin_panel(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("notification_mark_all_read"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, self.ADMIN_PANEL)

    def test_admin_clear_without_the_header_is_the_admin_panel(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("notification_clear"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, self.ADMIN_PANEL)

    def test_admin_panel_with_the_header_is_the_same_template(self):
        self.client.force_login(self.admin)
        response = self.client.get(
            reverse("notifications_panel"), HTTP_X_ADMIN_LAYOUT="1"
        )
        self.assertTemplateUsed(response, self.ADMIN_PANEL)

    def test_owner_clear_is_still_forbidden(self):
        self.client.force_login(self.owner.user)
        response = self.client.post(reverse("notification_clear"))
        self.assertEqual(response.status_code, 403)
