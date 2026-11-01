import datetime
import re
from unittest import mock
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.core import mail
from django.db import DatabaseError, connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from django.utils.dateformat import format as date_format

from appointments.models import Appointment, BlockedDate, ClinicSettings
from appointments.utils import get_available_slots
from billing.models import BillingReceipt, Service
from dashboard.templatetags.friendly_dates import friendly_date, long_date
from medical.models import MedicalRecord
from notifications.models import Notification
from pets.models import Pet, PetOwner

User = get_user_model()


class AppointmentTestBase(TestCase):
    """One admin, one owner with a pet, and a clinic open every day."""

    def setUp(self):
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
        # Open every day 8:00-17:00 with hourly slots, so near dates have slots.
        ClinicSettings.objects.create(
            clinic_name="Test Clinic",
            address="Gasan, Marinduque",
            opening_time=datetime.time(8, 0),
            closing_time=datetime.time(17, 0),
            slot_duration_minutes=60,
            working_days=[0, 1, 2, 3, 4, 5, 6],
            same_day_cutoff_time=datetime.time(15, 0),
            booking_limit_days=30,
        )
        self.today = timezone.localdate()
        self.client.force_login(self.admin)

    # ─── Helpers ──────────────────────────────────────────────────────────────

    def _appointment(self, status, days_from_today=3, hour=9):
        return Appointment.objects.create(
            owner=self.owner,
            pet=self.pet,
            date=self.today + datetime.timedelta(days=days_from_today),
            time=datetime.time(hour, 0),
            status=status,
        )

    def _post(self, url_name, appointment, data=None):
        return self.client.post(reverse(url_name, args=[appointment.pk]), data or {})

    def _messages(self, response):
        return [str(m) for m in get_messages(response.wsgi_request)]

    def _assert_status(self, appointment, status):
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, status)


class ApproveTests(AppointmentTestBase):

    def test_pending_becomes_confirmed(self):
        appointment = self._appointment(Appointment.PENDING)
        response = self._post("admin_appointment_approve", appointment)
        self._assert_status(appointment, Appointment.CONFIRMED)
        self.assertIn(
            f"Confirmed Bantay for {friendly_date(appointment.date, 'mid')} "
            "at 9:00 AM. The owner has been notified.",
            self._messages(response),
        )

    def test_message_says_today_in_lowercase(self):
        appointment = self._appointment(Appointment.PENDING, days_from_today=0)
        response = self._post("admin_appointment_approve", appointment)
        self.assertIn(
            "Confirmed Bantay for today at 9:00 AM. The owner has been notified.",
            self._messages(response),
        )

    def test_only_from_pending(self):
        for status in (Appointment.CONFIRMED, Appointment.CANCELLED):
            with self.subTest(status=status):
                appointment = self._appointment(status)
                response = self._post("admin_appointment_approve", appointment)
                self._assert_status(appointment, status)
                self.assertIn(
                    "Only pending appointments can be approved.",
                    self._messages(response),
                )

    def test_refused_when_date_has_passed(self):
        """Absolute date ("Thu, Oct 29"), not "Yesterday"."""
        for days in (-1, -3):
            with self.subTest(days=days):
                appointment = self._appointment(
                    Appointment.PENDING, days_from_today=days
                )
                response = self._post("admin_appointment_approve", appointment)
                self._assert_status(appointment, Appointment.PENDING)
                self.assertIn(
                    f"This request was for {date_format(appointment.date, 'D, M j')}, "
                    "which has passed. Move it to another time or decline it.",
                    self._messages(response),
                )
        self.assertFalse(
            Notification.objects.filter(recipient=self.owner.user).exists()
        )


class RejectTests(AppointmentTestBase):

    def _owner_notification(self):
        return Notification.objects.get(
            recipient=self.owner.user, notification_type="appointment_rejected"
        )

    def test_pending_becomes_cancelled(self):
        appointment = self._appointment(Appointment.PENDING)
        response = self._post("admin_appointment_reject", appointment)
        self._assert_status(appointment, Appointment.CANCELLED)
        self.assertEqual(appointment.cancelled_by, self.admin)
        self.assertIn(
            f"Declined Bantay's request for {friendly_date(appointment.date, 'mid')} "
            "at 9:00 AM. The owner has been notified.",
            self._messages(response),
        )

    def test_message_is_stored_and_sent_to_owner(self):
        appointment = self._appointment(Appointment.PENDING)
        self._post(
            "admin_appointment_reject",
            appointment,
            {"message": "  The vet is away that week.  "},
        )
        appointment.refresh_from_db()
        self.assertEqual(appointment.cancellation_reason, Appointment.OTHER)
        self.assertEqual(appointment.cancellation_detail, "The vet is away that week.")
        self.assertIn(
            "Message from the clinic: The vet is away that week.",
            self._owner_notification().message,
        )

    def test_without_message_keeps_default_text(self):
        appointment = self._appointment(Appointment.PENDING)
        self._post("admin_appointment_reject", appointment)
        appointment.refresh_from_db()
        self.assertEqual(appointment.cancellation_detail, "Rejected by clinic.")
        self.assertNotIn("Message from the clinic", self._owner_notification().message)

    def test_long_message_is_trimmed_to_500(self):
        appointment = self._appointment(Appointment.PENDING)
        self._post("admin_appointment_reject", appointment, {"message": "x" * 600})
        appointment.refresh_from_db()
        self.assertEqual(len(appointment.cancellation_detail), 500)


class CompleteAndNoShowTests(AppointmentTestBase):
    """Both actions: only from CONFIRMED, and only on or after the date."""

    ACTIONS = {
        "admin_appointment_complete": Appointment.COMPLETED,
        "admin_appointment_no_show": Appointment.NO_SHOW,
    }

    def test_blocked_before_the_appointment_date(self):
        for url_name in self.ACTIONS:
            with self.subTest(url_name=url_name):
                appointment = self._appointment(
                    Appointment.CONFIRMED, days_from_today=1
                )
                response = self._post(url_name, appointment)
                self._assert_status(appointment, Appointment.CONFIRMED)
                self.assertIn(
                    "You can mark this after the appointment date (tomorrow).",
                    self._messages(response),
                )

    def test_allowed_today_and_for_past_dates(self):
        for url_name, new_status in self.ACTIONS.items():
            for days in (0, -2):
                with self.subTest(url_name=url_name, days=days):
                    appointment = self._appointment(
                        Appointment.CONFIRMED, days_from_today=days
                    )
                    self._post(url_name, appointment)
                    self._assert_status(appointment, new_status)

    def test_only_from_confirmed(self):
        for url_name in self.ACTIONS:
            with self.subTest(url_name=url_name):
                appointment = self._appointment(Appointment.PENDING, days_from_today=0)
                self._post(url_name, appointment)
                self._assert_status(appointment, Appointment.PENDING)

    def test_success_messages(self):
        expected = {
            "admin_appointment_complete": "as completed.",
            "admin_appointment_no_show": "as a no-show.",
        }
        for url_name, ending in expected.items():
            with self.subTest(url_name=url_name):
                appointment = self._appointment(
                    Appointment.CONFIRMED, days_from_today=0
                )
                response = self._post(url_name, appointment)
                self.assertIn(
                    f"Marked Bantay's appointment on today at 9:00 AM {ending}",
                    self._messages(response),
                )


class CancelTests(AppointmentTestBase):

    def test_allowed_from_pending_and_confirmed(self):
        for status in (Appointment.PENDING, Appointment.CONFIRMED):
            with self.subTest(status=status):
                appointment = self._appointment(status)
                response = self._post(
                    "admin_appointment_cancel",
                    appointment,
                    {"cancellation_reason": Appointment.PET_UNWELL},
                )
                self._assert_status(appointment, Appointment.CANCELLED)
                self.assertEqual(appointment.cancelled_by, self.admin)
                self.assertRedirects(
                    response,
                    reverse("admin_appointment_detail", args=[appointment.pk]),
                    fetch_redirect_response=False,
                )

    def test_other_without_detail_shows_the_error(self):
        appointment = self._appointment(Appointment.CONFIRMED)
        response = self._post(
            "admin_appointment_cancel",
            appointment,
            {"cancellation_reason": Appointment.OTHER, "cancellation_detail": ""},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Please provide details when selecting Other.")
        self.assertTrue(response.context["cancel_form"].is_bound)
        self.assertEqual(response.context["open_dialog"], "cancel")
        self._assert_status(appointment, Appointment.CONFIRMED)


class RescheduleTests(AppointmentTestBase):

    def setUp(self):
        super().setUp()
        self.target = self.today + datetime.timedelta(days=5)

    def _data(self, day=None, slot="10:00:00"):
        return {
            "date": (day or self.target).isoformat(),
            "time": slot,
            "reschedule_reason": "The vet is away that day.",
        }

    def test_valid_creates_confirmed_copy_and_cancels_original(self):
        for hour, status in ((10, Appointment.PENDING), (11, Appointment.CONFIRMED)):
            with self.subTest(status=status):
                original = self._appointment(status)
                response = self._post(
                    "admin_appointment_reschedule",
                    original,
                    self._data(slot=f"{hour}:00:00"),
                )
                self._assert_status(original, Appointment.CANCELLED)
                new = Appointment.objects.get(rescheduled_from=original)
                self.assertEqual(new.status, Appointment.CONFIRMED)
                self.assertEqual(new.date, self.target)
                self.assertEqual(new.time, datetime.time(hour, 0))
                self.assertRedirects(
                    response,
                    reverse("admin_appointment_detail", args=[new.pk]),
                    fetch_redirect_response=False,
                )
                self.assertIn(
                    f"Rescheduled Bantay to {friendly_date(self.target, 'mid')} "
                    f"at {hour}:00 AM. The owner has been notified.",
                    self._messages(response),
                )

    def test_past_date_shows_the_error(self):
        original = self._appointment(Appointment.CONFIRMED)
        yesterday = self.today - datetime.timedelta(days=1)
        response = self._post(
            "admin_appointment_reschedule", original, self._data(day=yesterday)
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Cannot reschedule to a past date.")
        self._assert_status(original, Appointment.CONFIRMED)
        self.assertEqual(Appointment.objects.count(), 1)

    def test_taken_slot_is_refused(self):
        original = self._appointment(Appointment.CONFIRMED)
        Appointment.objects.create(
            owner=self.owner,
            pet=self.pet,
            date=self.target,
            time=datetime.time(10, 0),
            status=Appointment.CONFIRMED,
        )
        response = self._post("admin_appointment_reschedule", original, self._data())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["reschedule_form"].errors)
        self._assert_status(original, Appointment.CONFIRMED)
        self.assertEqual(Appointment.objects.count(), 2)

    def test_error_reopens_move_dialog_with_the_chosen_time(self):
        """Taken slot: the dialog reopens with the date's free times and the
        time the admin picked (no longer in the list, so not re-checked)."""
        original = self._appointment(Appointment.CONFIRMED)
        Appointment.objects.create(
            owner=self.owner,
            pet=self.pet,
            date=self.target,
            time=datetime.time(10, 0),
            status=Appointment.CONFIRMED,
        )
        response = self._post("admin_appointment_reschedule", original, self._data())
        self.assertEqual(response.context["open_dialog"], "move")
        self.assertEqual(response.context["selected_time"], "10:00:00")
        slots = response.context["reschedule_slots"]
        self.assertIn(datetime.time(11, 0), slots)
        self.assertNotIn(datetime.time(10, 0), slots)

    def test_past_date_error_has_no_slots(self):
        original = self._appointment(Appointment.CONFIRMED)
        yesterday = self.today - datetime.timedelta(days=1)
        response = self._post(
            "admin_appointment_reschedule", original, self._data(day=yesterday)
        )
        self.assertEqual(response.context["open_dialog"], "move")
        self.assertIsNone(response.context["reschedule_slots"])

    def test_owner_notification_text(self):
        """Absolute date, no leading zeros, says the new time is confirmed."""
        original = self._appointment(Appointment.PENDING)
        self._post(
            "admin_appointment_reschedule", original, self._data(slot="09:00:00")
        )
        notification = Notification.objects.get(recipient=self.owner.user)
        self.assertEqual(notification.notification_type, "appointment_approved")
        self.assertEqual(notification.title, "Appointment moved by the clinic")
        self.assertEqual(
            notification.message,
            f"Your appointment for Bantay was moved to "
            f"{date_format(self.target, 'D, M j')} at 9:00 AM by the clinic. "
            "The new time is confirmed.",
        )
        self.assertNotIn("09:00", notification.message)

    def test_owner_email_subject(self):
        original = self._appointment(Appointment.PENDING)
        self._post("admin_appointment_reschedule", original, self._data())
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].subject, "Appointment moved — Hapi Vet")
        self.assertEqual(mail.outbox[0].to, ["owner@example.com"])

    def test_original_kept_if_creating_the_new_one_fails(self):
        original = self._appointment(Appointment.CONFIRMED)
        with mock.patch.object(
            Appointment.objects, "create", side_effect=DatabaseError("boom")
        ):
            with self.assertRaises(DatabaseError):
                self._post("admin_appointment_reschedule", original, self._data())
        self._assert_status(original, Appointment.CONFIRMED)
        self.assertEqual(Appointment.objects.count(), 1)


class NotesTests(AppointmentTestBase):

    def test_admin_saves_notes(self):
        appointment = self._appointment(Appointment.CONFIRMED)
        response = self._post(
            "admin_appointment_notes", appointment, {"notes": "Use a muzzle."}
        )
        appointment.refresh_from_db()
        self.assertEqual(appointment.notes, "Use a muzzle.")
        self.assertIn("Notes saved.", self._messages(response))

    def test_pet_owner_is_refused(self):
        appointment = self._appointment(Appointment.CONFIRMED)
        self.client.force_login(self.owner.user)
        response = self._post(
            "admin_appointment_notes", appointment, {"notes": "Owner text"}
        )
        self.assertRedirects(
            response, reverse("owner_dashboard"), fetch_redirect_response=False
        )
        appointment.refresh_from_db()
        self.assertEqual(appointment.notes, "")


class DetailContextTests(AppointmentTestBase):
    """Flags the detail page uses to pick its banner and buttons."""

    def _context(self, appointment):
        response = self.client.get(
            reverse("admin_appointment_detail", args=[appointment.pk])
        )
        self.assertEqual(response.status_code, 200)
        return response.context

    def test_date_flags(self):
        cases = (
            # status, days from today, date_has_passed, can_mark_visit
            (Appointment.PENDING, -1, True, False),
            (Appointment.CONFIRMED, -1, True, True),
            (Appointment.CONFIRMED, 0, False, True),
            (Appointment.CONFIRMED, 1, False, False),
        )
        for status, days, passed, can_mark in cases:
            with self.subTest(status=status, days=days):
                context = self._context(self._appointment(status, days, hour=8))
                self.assertEqual(context["date_has_passed"], passed)
                self.assertEqual(context["can_mark_visit"], can_mark)

    def test_owner_email_and_who_cancelled(self):
        appointment = self._appointment(Appointment.CANCELLED)
        appointment.cancelled_by = self.owner.user
        appointment.save()
        context = self._context(appointment)
        self.assertTrue(context["owner_has_email"])
        self.assertTrue(context["cancelled_by_owner"])
        self.assertIsNone(context["open_dialog"])
        self.assertIsNone(context["reschedule_slots"])

        appointment.cancelled_by = self.admin
        appointment.save()
        self.owner.user.email = ""
        self.owner.user.save()
        context = self._context(appointment)
        self.assertFalse(context["owner_has_email"])
        self.assertFalse(context["cancelled_by_owner"])

    def test_moved_to(self):
        original = self._appointment(Appointment.CANCELLED)
        self.assertIsNone(self._context(original)["moved_to"])
        new = Appointment.objects.create(
            owner=self.owner,
            pet=self.pet,
            date=original.date,
            time=datetime.time(11, 0),
            status=Appointment.CONFIRMED,
            rescheduled_from=original,
        )
        self.assertEqual(self._context(original)["moved_to"], new)


class DetailPageTests(AppointmentTestBase):
    """What the redesigned detail page shows for each state."""

    def _get(self, appointment):
        response = self.client.get(
            reverse("admin_appointment_detail", args=[appointment.pk])
        )
        self.assertEqual(response.status_code, 200)
        return response

    def _completed_with(self, record=False, receipt=False):
        appointment = self._appointment(Appointment.COMPLETED, days_from_today=0)
        if record:
            MedicalRecord.objects.create(
                pet=self.pet, appointment=appointment, record_date=self.today
            )
        if receipt:
            BillingReceipt.objects.create(
                appointment=appointment,
                owner=self.owner,
                pet=self.pet,
                receipt_number="R-0001",
            )
        return appointment

    def _assert_dialog_open(self, response, dialog_id, is_open=True):
        opened = re.search(
            rf'<dialog id="{dialog_id}"[^>]*data-open-on-load',
            response.content.decode(),
        )
        self.assertEqual(bool(opened), is_open)

    def test_pending_approve_only_for_future_dates(self):
        future = self._get(self._appointment(Appointment.PENDING, days_from_today=2))
        self.assertContains(future, "Waiting for your approval")
        self.assertContains(future, 'data-dialog-open="approve-dialog"')
        self.assertContains(future, 'id="approve-dialog"')

        past = self._get(self._appointment(Appointment.PENDING, days_from_today=-1))
        self.assertContains(past, "This date has already passed")
        self.assertNotContains(past, "Approve request")
        self.assertNotContains(past, 'id="approve-dialog"')
        self.assertContains(past, "Move to another time")
        self.assertContains(past, "Decline request")

    def test_confirmed_mark_as_completed_from_the_visit_day(self):
        future = self._appointment(Appointment.CONFIRMED, days_from_today=2)
        response = self._get(future)
        self.assertNotContains(response, 'data-dialog-open="complete-dialog"')
        self.assertNotContains(response, 'data-dialog-open="no-show-dialog"')
        day = date_format(future.date, "D, M j")
        self.assertContains(response, f"“Mark as completed” appears here on {day}.")

        response = self._get(self._appointment(Appointment.CONFIRMED, 0, hour=10))
        self.assertContains(response, 'data-dialog-open="complete-dialog"')
        self.assertContains(response, "Pet didn't come (no-show)")
        self.assertNotContains(response, "appears here on")

    def test_confirmed_banner_says_today_in_lowercase(self):
        response = self._get(self._appointment(Appointment.CONFIRMED, 0, hour=11))
        self.assertContains(response, "Confirmed for today at 11:00 AM")
        self.assertNotContains(response, "Confirmed for Today")

        future = self._appointment(Appointment.CONFIRMED, days_from_today=5)
        response = self._get(future)
        day = date_format(future.date, "D, M j")
        self.assertContains(response, f"Confirmed for {day} at 9:00 AM")

    def test_completed_without_record_or_receipt_links_to_create_both(self):
        appointment = self._completed_with()
        response = self._get(appointment)
        record_url = reverse("admin_medical_record_create", args=[self.pet.pk])
        receipt_url = reverse("admin_receipt_create")
        self.assertContains(
            response, f'href="{record_url}?appointment={appointment.pk}"'
        )
        self.assertContains(
            response, f'href="{receipt_url}?appointment={appointment.pk}"'
        )
        self.assertContains(response, "Not added yet")
        self.assertContains(response, "Not created yet")
        self.assertNotContains(response, "All done for this visit")

    def test_completed_with_both_is_all_done(self):
        appointment = self._completed_with(record=True, receipt=True)
        response = self._get(appointment)
        self.assertContains(response, "All done for this visit")
        record = appointment.medical_record
        receipt = appointment.billing_receipt
        self.assertContains(
            response,
            f'href="{reverse("admin_medical_record_detail", args=[record.pk])}"',
        )
        self.assertContains(
            response, f'href="{reverse("admin_receipt_detail", args=[receipt.pk])}"'
        )
        self.assertContains(response, "Open medical record")
        self.assertContains(response, "Open receipt")
        self.assertNotContains(response, "Add medical record")
        self.assertNotContains(response, "Create receipt")
        # Both buttons are outlines: no primary button left
        self.assertNotContains(response, "btn btn-primary")

    def test_moved_original_links_to_the_new_appointment(self):
        original = self._appointment(Appointment.CANCELLED)
        new = Appointment.objects.create(
            owner=self.owner,
            pet=self.pet,
            date=self.today + datetime.timedelta(days=10),
            time=datetime.time(14, 0),
            status=Appointment.CONFIRMED,
            rescheduled_from=original,
        )
        response = self._get(original)
        self.assertContains(response, f"Moved to {long_date(new.date)} at 2:00 PM")
        self.assertContains(
            response,
            f'href="{reverse("admin_appointment_detail", args=[new.pk])}"',
        )
        self.assertContains(response, "Open the new appointment")
        self.assertNotContains(response, "Cancelled by the")

    def test_cancelled_banner_says_who_cancelled(self):
        appointment = self._appointment(Appointment.CANCELLED)
        appointment.cancellation_reason = Appointment.PET_UNWELL
        appointment.cancellation_detail = "Fever since Sunday."
        appointment.cancelled_by = self.owner.user
        appointment.save()
        response = self._get(appointment)
        self.assertContains(response, "Cancelled by the owner")
        self.assertContains(response, "Pet is Unwell")
        self.assertContains(response, "Fever since Sunday.")
        self.assertContains(response, "Nothing more to do")
        self.assertNotContains(response, "data-dialog-open")

        appointment.cancelled_by = self.admin
        appointment.save()
        response = self._get(appointment)
        self.assertContains(response, "Cancelled by the clinic")
        self.assertNotContains(response, "Cancelled by the owner")

    def test_footer_email_or_phone(self):
        appointment = self._appointment(Appointment.PENDING)
        self.assertContains(self._get(appointment), "Ana Cruz will be notified.")

        self.owner.user.email = ""
        self.owner.user.save()
        self.owner.contact_number = "0917 123 4567"
        self.owner.save()
        response = self._get(appointment)
        self.assertContains(
            response,
            "Ana Cruz has no email address. " "Call 0917 123 4567 if anything changes.",
        )
        self.assertNotContains(response, "will be notified")

        self.owner.contact_number = ""
        self.owner.save()
        response = self._get(appointment)
        self.assertContains(response, "Ana Cruz has no email address.")
        self.assertNotContains(response, "Call ")

    def _reschedule(self, original, **data):
        target = self.today + datetime.timedelta(days=5)
        post = {
            "date": target.isoformat(),
            "time": "10:00:00",
            "reschedule_reason": "The vet is away that day.",
        }
        post.update(data)
        return self._post("admin_appointment_reschedule", original, post)

    def test_reschedule_error_reopens_the_move_dialog(self):
        original = self._appointment(Appointment.CONFIRMED)
        Appointment.objects.create(
            owner=self.owner,
            pet=self.pet,
            date=self.today + datetime.timedelta(days=5),
            time=datetime.time(10, 0),
            status=Appointment.CONFIRMED,
        )
        response = self._reschedule(original)
        self._assert_dialog_open(response, "move-dialog")
        self._assert_dialog_open(response, "cancel-dialog", is_open=False)
        self.assertContains(response, "Please correct the errors below.")
        # The taken time is no longer one of the form's choices
        self.assertContains(
            response,
            "Select a valid choice. 10:00:00 is not one of the available choices.",
        )
        self.assertContains(response, "The vet is away that day.</textarea>")
        target = self.today + datetime.timedelta(days=5)
        self.assertContains(response, f'value="{target.isoformat()}"')
        self.assertContains(response, f"Free times on {long_date(target)}</h3>")

    def test_plain_visit_move_dialog_has_no_slot_heading(self):
        response = self._get(self._appointment(Appointment.CONFIRMED))
        self.assertContains(response, "Pick a date to see the free times.")
        self.assertNotContains(response, "Free times")
        self.assertContains(response, ">New time <span")

    def test_reschedule_error_keeps_the_chosen_time_checked(self):
        """Missing reason: the picked time is still free, so it stays checked."""
        original = self._appointment(Appointment.CONFIRMED)
        response = self._reschedule(original, reschedule_reason="")
        self._assert_dialog_open(response, "move-dialog")
        content = response.content.decode()
        self.assertIn('value="10:00:00" class="sr-only" required checked', content)
        self.assertEqual(content.count("required checked"), 1)

    def test_cancel_error_reopens_the_cancel_dialog(self):
        appointment = self._appointment(Appointment.CONFIRMED)
        response = self._post(
            "admin_appointment_cancel",
            appointment,
            {"cancellation_reason": Appointment.OTHER, "cancellation_detail": ""},
        )
        self._assert_dialog_open(response, "cancel-dialog")
        self._assert_dialog_open(response, "move-dialog", is_open=False)
        self.assertContains(response, "Please correct the errors below.")
        self.assertContains(response, "Please provide details when selecting Other.")

    def test_no_dialog_open_on_a_plain_visit(self):
        response = self._get(self._appointment(Appointment.CONFIRMED))
        self.assertNotContains(response, "data-open-on-load")
        self.assertNotContains(response, "Please correct the errors below.")

    def test_loads_no_old_admin_css(self):
        response = self._get(self._appointment(Appointment.PENDING))
        self.assertNotContains(response, "css/admin/")
        self.assertContains(response, "css/app.")


class SlotViewTests(AppointmentTestBase):

    def _get(self, **params):
        return self.client.get(reverse("admin_get_slots_for_date"), params)

    def test_default_returns_options(self):
        day = (self.today + datetime.timedelta(days=5)).isoformat()
        response = self._get(date=day)
        self.assertContains(response, '<option value="10:00:00">10:00 AM</option>')
        self.assertNotContains(response, 'type="radio"')

    def test_buttons_return_radios_and_mark_the_selected_time(self):
        day = (self.today + datetime.timedelta(days=5)).isoformat()
        response = self._get(date=day, selected="10:00:00", **{"as": "buttons"})
        self.assertNotContains(response, "<option")
        content = response.content.decode()
        self.assertIn('type="radio" name="time" value="11:00:00"', content)
        self.assertIn('value="10:00:00" class="sr-only" required checked', content)
        self.assertEqual(content.count(" checked"), 1)

    def test_buttons_empty_state(self):
        past = (self.today - datetime.timedelta(days=1)).isoformat()
        response = self._get(date=past, **{"as": "buttons"})
        self.assertContains(response, "No free times on this day.")

    def test_buttons_heading_names_the_day(self):
        day = self.today + datetime.timedelta(days=5)
        response = self._get(date=day.isoformat(), **{"as": "buttons"})
        self.assertContains(response, f"Free times on {long_date(day)}</h3>")
        self.assertContains(response, 'type="radio"')

    def test_buttons_heading_for_today(self):
        response = self._get(date=self.today.isoformat(), **{"as": "buttons"})
        expected = date_format(self.today, "l, F j")
        self.assertContains(response, f"Free times today, {expected}</h3>")
        self.assertNotContains(response, "Today, ")

    def test_buttons_no_heading_without_a_date(self):
        for params in ({}, {"date": ""}, {"date": "not-a-date"}):
            with self.subTest(params=params):
                response = self._get(**params, **{"as": "buttons"})
                self.assertNotContains(response, "<h3")
                self.assertNotContains(response, "Free times")

    def test_buttons_heading_kept_with_the_empty_message(self):
        past = self.today - datetime.timedelta(days=1)
        response = self._get(date=past.isoformat(), **{"as": "buttons"})
        self.assertContains(response, f"Free times on {long_date(past)}</h3>")
        self.assertContains(response, "No free times on this day.")

    def test_options_have_no_heading(self):
        day = (self.today + datetime.timedelta(days=5)).isoformat()
        response = self._get(date=day)
        self.assertNotContains(response, "Free times")


class AccessTests(AppointmentTestBase):

    ADMIN_URLS = (
        "admin_appointment_detail",
        "admin_appointment_approve",
        "admin_appointment_reject",
        "admin_appointment_complete",
        "admin_appointment_no_show",
        "admin_appointment_notes",
        "admin_appointment_cancel",
        "admin_appointment_reschedule",
    )

    def test_owner_cannot_use_admin_actions(self):
        """Owners: a normal request is redirected, HTMX gets 403."""
        appointment = self._appointment(Appointment.PENDING)
        self.client.force_login(self.owner.user)
        for url_name in self.ADMIN_URLS:
            with self.subTest(url_name=url_name):
                url = reverse(url_name, args=[appointment.pk])
                self.assertRedirects(
                    self.client.post(url),
                    reverse("owner_dashboard"),
                    fetch_redirect_response=False,
                )
                htmx = self.client.post(url, headers={"HX-Request": "true"})
                self.assertEqual(htmx.status_code, 403)
        self._assert_status(appointment, Appointment.PENDING)

    def test_owner_detail_hides_notes(self):
        """Appointment notes are internal (D1)."""
        appointment = self._appointment(Appointment.CONFIRMED)
        appointment.notes = "Needs a muzzle; nervous with strangers."
        appointment.save()
        self.client.force_login(self.owner.user)
        response = self.client.get(
            reverse("owner_appointment_detail", args=[appointment.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Needs a muzzle")
        self.assertNotContains(response, "Admin Notes")
        self.assertNotContains(response, "Internal notes")


class AdminListTests(AppointmentTestBase):

    def setUp(self):
        super().setUp()
        owner_user = User.objects.create_user(
            username="owner2",
            email="owner2@example.com",
            password="unused-pass-123",
            role=User.PET_OWNER,
            is_onboarded=True,
        )
        self.owner2 = PetOwner.objects.create(
            user=owner_user, first_name="Liza", last_name="Santos"
        )
        self.pet2 = Pet.objects.create(owner=self.owner2, name="Kitty", species="Cat")

    # ─── Helpers ──────────────────────────────────────────────────────────────

    def _get(self, **params):
        return self.client.get(reverse("admin_appointments"), params)

    def _ids(self, response):
        return [a.pk for a in response.context["page_obj"].object_list]

    def _html(self, response):
        return response.content.decode()

    def _counts(self, response):
        return {t["id"]: t["count"] for t in response.context["tabs"]}

    def _other(self, status, days_from_today=3, hour=9):
        """An appointment for the second owner's pet (Kitty, Liza Santos)."""
        return Appointment.objects.create(
            owner=self.owner2,
            pet=self.pet2,
            date=self.today + datetime.timedelta(days=days_from_today),
            time=datetime.time(hour, 0),
            status=status,
        )

    def _moved(self):
        """An admin reschedule: cancelled original + new confirmed row."""
        original = self._appointment(Appointment.CANCELLED, 2)
        new = Appointment.objects.create(
            owner=self.owner,
            pet=self.pet,
            date=self.today + datetime.timedelta(days=4),
            time=datetime.time(10, 0),
            status=Appointment.CONFIRMED,
            rescheduled_from=original,
        )
        return original, new

    # ─── Tabs ─────────────────────────────────────────────────────────────────

    def test_pending_tab_oldest_first(self):
        later = self._appointment(Appointment.PENDING, 5, 9)
        second = self._appointment(Appointment.PENDING, 2, 10)
        first = self._appointment(Appointment.PENDING, 2, 9)
        self._appointment(Appointment.CONFIRMED, 1)
        response = self._get(status="pending")
        self.assertEqual(response.context["active_tab"], "pending")
        self.assertEqual(self._ids(response), [first.pk, second.pk, later.pk])

    def test_upcoming_tab_is_confirmed_from_today(self):
        later = self._appointment(Appointment.CONFIRMED, 3)
        today = self._appointment(Appointment.CONFIRMED, 0)
        self._appointment(Appointment.CONFIRMED, -1)  # unfinished
        self._appointment(Appointment.PENDING, 1)
        response = self._get(status="upcoming")
        self.assertEqual(self._ids(response), [today.pk, later.pk])

    def test_confirmed_is_alias_of_upcoming(self):
        visit = self._appointment(Appointment.CONFIRMED, 1)
        self._appointment(Appointment.PENDING, 1, 10)
        response = self._get(status="confirmed")
        self.assertEqual(response.context["active_tab"], "upcoming")
        self.assertEqual(self._ids(response), [visit.pk])

    def test_history_tabs_newest_first(self):
        for status in (
            Appointment.COMPLETED,
            Appointment.CANCELLED,
            Appointment.NO_SHOW,
        ):
            with self.subTest(status=status):
                older = self._appointment(status, -5)
                newer = self._appointment(status, -1)
                response = self._get(status=status)
                self.assertEqual(response.context["active_tab"], status)
                self.assertEqual(self._ids(response), [newer.pk, older.pk])

    def test_all_tab_has_every_status_newest_first(self):
        past = self._appointment(Appointment.COMPLETED, -2)
        pending = self._appointment(Appointment.PENDING, 1)
        future = self._appointment(Appointment.CONFIRMED, 6)
        response = self._get(status="all")
        self.assertEqual(self._ids(response), [future.pk, pending.pk, past.pk])

    def test_unknown_status_falls_back_to_default(self):
        pending = self._appointment(Appointment.PENDING)
        response = self._get(status="bogus")
        self.assertEqual(response.context["active_tab"], "pending")
        self.assertEqual(self._ids(response), [pending.pk])

    def test_default_tab_is_pending_when_requests_wait(self):
        self._appointment(Appointment.PENDING)
        self._appointment(Appointment.CONFIRMED, 1)
        self.assertEqual(self._get().context["active_tab"], "pending")

    def test_default_tab_is_upcoming_when_no_requests(self):
        visit = self._appointment(Appointment.CONFIRMED, 1)
        response = self._get()
        self.assertEqual(response.context["active_tab"], "upcoming")
        self.assertEqual(self._ids(response), [visit.pk])

    def test_tabs_in_display_order_with_counts(self):
        self._appointment(Appointment.PENDING)
        self._appointment(Appointment.COMPLETED, -1)
        tabs = self._get().context["tabs"]
        self.assertEqual(
            [(t["id"], t["label"], t["count"]) for t in tabs],
            [
                ("pending", "Needs approval", 1),
                ("upcoming", "Upcoming", 0),
                ("completed", "Completed", 1),
                ("cancelled", "Cancelled", 0),
                ("no_show", "No show", 0),
                ("all", "All", 2),
            ],
        )

    def test_day_groups_follow_page_order(self):
        a = self._appointment(Appointment.PENDING, 1, 9)
        b = self._appointment(Appointment.PENDING, 1, 11)
        c = self._appointment(Appointment.PENDING, 3, 9)
        groups = self._get(status="pending").context["day_groups"]
        self.assertEqual(
            [(day, [r.pk for r in rows]) for day, rows in groups],
            [(a.date, [a.pk, b.pk]), (c.date, [c.pk])],
        )

    # ─── Moved originals ──────────────────────────────────────────────────────

    def test_cancelled_tab_excludes_moved_originals(self):
        self._moved()
        cancelled = self._appointment(Appointment.CANCELLED, 1)
        response = self._get(status="cancelled")
        self.assertEqual(self._ids(response), [cancelled.pk])
        self.assertEqual(self._counts(response)["cancelled"], 1)
        self.assertEqual(self._counts(response)["all"], 3)

    def test_all_tab_flags_moved_originals(self):
        original, new = self._moved()
        rows = {
            a.pk: a for a in self._get(status="all").context["page_obj"].object_list
        }
        self.assertEqual(set(rows), {original.pk, new.pk})
        self.assertTrue(rows[original.pk].is_moved)
        self.assertFalse(rows[new.pk].is_moved)

    # ─── Unfinished visits ────────────────────────────────────────────────────

    def test_unfinished_tab_and_count(self):
        older = self._appointment(Appointment.CONFIRMED, -3)
        newer = self._appointment(Appointment.CONFIRMED, -1)
        self._appointment(Appointment.CONFIRMED, 0)
        self._appointment(Appointment.PENDING, -1)
        response = self._get(status="unfinished")
        self.assertEqual(response.context["active_tab"], "unfinished")
        self.assertEqual(self._ids(response), [older.pk, newer.pk])
        self.assertEqual(response.context["unfinished_count"], 2)
        self.assertIsNone(response.context["unfinished_visit"])

    def test_single_unfinished_visit_is_named(self):
        visit = self._appointment(Appointment.CONFIRMED, -2)
        response = self._get()
        self.assertEqual(response.context["unfinished_count"], 1)
        self.assertEqual(response.context["unfinished_visit"], visit)

    # ─── Search ───────────────────────────────────────────────────────────────

    def test_search_one_word_all_statuses(self):
        done = self._appointment(Appointment.COMPLETED, -2)
        pending = self._appointment(Appointment.PENDING, 1)
        self._other(Appointment.PENDING, 1, 10)
        response = self._get(status="pending", q="bantay")
        self.assertEqual(response.context["active_tab"], "all")
        self.assertTrue(response.context["is_searching"])
        self.assertEqual(self._ids(response), [pending.pk, done.pk])

    def test_search_matches_owner_names(self):
        kitty = self._other(Appointment.PENDING)
        self._appointment(Appointment.PENDING, 1)
        for q in ("Liza", "SANTOS"):
            with self.subTest(q=q):
                self.assertEqual(self._ids(self._get(q=q)), [kitty.pk])

    def test_search_every_word_must_match(self):
        bantay = self._appointment(Appointment.PENDING)
        self._other(Appointment.PENDING, 1)
        self.assertEqual(self._ids(self._get(q="Bantay cruz")), [bantay.pk])
        self.assertEqual(self._ids(self._get(q="Bantay santos")), [])

    def test_date_filter(self):
        on_day = self._appointment(Appointment.COMPLETED, -2)
        self._appointment(Appointment.COMPLETED, -3)
        response = self._get(date=on_day.date.isoformat())
        self.assertEqual(response.context["active_tab"], "all")
        self.assertEqual(response.context["date_filter"], on_day.date)
        self.assertEqual(self._ids(response), [on_day.pk])

    def test_bad_date_is_ignored(self):
        pending = self._appointment(Appointment.PENDING)
        response = self._get(date="31/10/2026")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["date_filter"])
        self.assertFalse(response.context["is_searching"])
        self.assertEqual(response.context["active_tab"], "pending")
        self.assertEqual(self._ids(response), [pending.pk])

    def test_counts_ignore_search(self):
        self._appointment(Appointment.PENDING)
        self._other(Appointment.PENDING, 1)
        counts = self._counts(self._get(q="kitty"))
        self.assertEqual(counts["pending"], 2)
        self.assertEqual(counts["all"], 2)

    def test_pending_count_matches_sidebar_badge(self):
        self._appointment(Appointment.PENDING)
        self._other(Appointment.PENDING, 1)
        self._moved()
        response = self._get(status="all")
        self.assertEqual(self._counts(response)["pending"], 2)
        self.assertEqual(response.context["shell"].pending_appointments, 2)

    # ─── Paging ───────────────────────────────────────────────────────────────

    def test_pagination_keeps_params(self):
        day = self.today + datetime.timedelta(days=2)
        for i in range(26):
            Appointment.objects.create(
                owner=self.owner,
                pet=self.pet,
                date=day,
                time=datetime.time(8 + i // 4, (i % 4) * 15),
                status=Appointment.PENDING,
            )
        first = self._get(q="Bantay", date=day.isoformat())
        self.assertEqual(first.context["page_obj"].paginator.num_pages, 2)
        self.assertEqual(len(first.context["page_obj"].object_list), 25)
        self.assertEqual(first.context["prev_page_query"], "")
        next_query = first.context["next_page_query"]
        for part in ("status=all", "q=Bantay", f"date={day.isoformat()}", "page=2"):
            self.assertIn(part, next_query)

        second = self.client.get(reverse("admin_appointments") + next_query)
        self.assertEqual(len(second.context["page_obj"].object_list), 1)
        self.assertEqual(second.context["next_page_query"], "")
        self.assertIn("page=1", second.context["prev_page_query"])

    def test_bad_page_is_ignored(self):
        """Like Paginator.get_page: not a number → page 1, too high → last."""
        self._appointment(Appointment.PENDING)
        for page in ("abc", "99"):
            with self.subTest(page=page):
                response = self._get(page=page)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["page_obj"].number, 1)

    # ─── Access and performance ───────────────────────────────────────────────

    def test_owner_is_redirected(self):
        self.client.force_login(self.owner.user)
        self.assertRedirects(
            self._get(), reverse("owner_dashboard"), fetch_redirect_response=False
        )

    def test_htmx_request_gets_the_full_page(self):
        """The old HX-Request partial is gone; the list never swaps in place."""
        visit = self._appointment(Appointment.PENDING)
        response = self.client.get(
            reverse("admin_appointments"), headers={"HX-Request": "true"}
        )
        self.assertTemplateUsed(response, "admin/appointments/list.html")
        self.assertEqual(self._ids(response), [visit.pk])

    def _create_rows(self, count, start=0):
        """count pending rows, each with its own owner, user and pet."""
        for i in range(start, start + count):
            owner_user = User.objects.create_user(
                username=f"perf{i}",
                email=f"perf{i}@example.com",
                password="unused-pass-123",
                role=User.PET_OWNER,
            )
            owner = PetOwner.objects.create(
                user=owner_user, first_name=f"Own{i}", last_name="Er"
            )
            pet = Pet.objects.create(owner=owner, name=f"Pet{i}", species="Dog")
            Appointment.objects.create(
                owner=owner,
                pet=pet,
                date=self.today + datetime.timedelta(days=1 + i),
                time=datetime.time(9, 0),
                status=Appointment.PENDING,
            )

    def test_query_count_is_flat(self):
        self._create_rows(5)
        with CaptureQueriesContext(connection) as small:
            self._get(status="all")
        self._create_rows(15, start=5)
        with self.assertNumQueries(len(small.captured_queries)):
            self._get(status="all")

    # ─── Page (template) ──────────────────────────────────────────────────────

    def _active_tabs(self, response):
        """Labels of the tab links marked as the current page."""
        # Tab labels follow ">" directly; the sidebar link starts with an icon
        html = self._html(response)
        return re.findall(r'aria-current="page">(\w[^<]*)<', html)

    def _service(self, name="Check-up"):
        return Service.objects.create(service_name=name, status=Service.ACTIVE)

    def test_page_uses_admin_layout_with_one_search_and_one_primary(self):
        response = self._get()
        html = self._html(response)
        self.assertTemplateUsed(response, "layouts/admin.html")
        self.assertNotContains(response, "Find a pet or owner")
        self.assertContains(response, 'name="q"', count=1)
        self.assertEqual(len(re.findall(r"\bbtn-primary\b", html)), 1)
        self.assertContains(
            response, f'href="{reverse("admin_walkin_appointment_create")}"'
        )
        self.assertContains(response, "New walk-in")

    def test_each_tab_renders_its_rows(self):
        cases = {
            "pending": self._appointment(Appointment.PENDING, 1),
            "upcoming": self._appointment(Appointment.CONFIRMED, 2),
            "completed": self._appointment(Appointment.COMPLETED, -1),
            "cancelled": self._appointment(Appointment.CANCELLED, -2),
            "no_show": self._appointment(Appointment.NO_SHOW, -3),
        }
        detail = {
            tab: reverse("admin_appointment_detail", args=[a.pk])
            for tab, a in cases.items()
        }
        for tab in cases:
            with self.subTest(tab=tab):
                response = self._get(status=tab)
                for other, url in detail.items():
                    if other == tab:
                        self.assertContains(response, f'href="{url}"')
                    else:
                        self.assertNotContains(response, f'href="{url}"')
        all_tab = self._get(status="all")
        for url in detail.values():
            self.assertContains(all_tab, f'href="{url}"')

    def test_moved_label_under_all_not_under_cancelled(self):
        self._moved()
        self.assertContains(self._get(status="all"), ">Moved</span>")
        self.assertNotContains(self._get(status="cancelled"), ">Moved</span>")

    def test_date_has_passed_and_walk_in_tags(self):
        self._appointment(Appointment.PENDING, -1)
        response = self._get(status="pending")
        self.assertContains(response, "Date has passed")
        self.assertNotContains(response, ">Walk-in</span>")

        Appointment.objects.update(
            status=Appointment.CONFIRMED,
            is_walk_in=True,
            date=self.today + datetime.timedelta(days=1),
        )
        response = self._get(status="upcoming")
        self.assertContains(response, ">Walk-in</span>")
        self.assertNotContains(response, "Date has passed")

    def test_no_service_and_today_heading(self):
        self._appointment(Appointment.CONFIRMED, 0)
        response = self._get(status="upcoming")
        self.assertContains(response, "No service picked")
        heading = f"Today · {date_format(self.today, 'l, F j')}"
        self.assertContains(response, heading)
        self.assertContains(response, "· 1 appointment<")

    def test_notice_one_visit_names_it_and_links_to_detail(self):
        visit = self._appointment(Appointment.CONFIRMED, -2)
        visit.service = self._service()
        visit.save()
        response = self._get(status="upcoming")
        self.assertContains(response, "1 past visit isn't finished.")
        self.assertContains(
            response,
            f"Bantay's Check-up on {friendly_date(visit.date, 'mid')} "
            "is still marked Confirmed.",
        )
        url = reverse("admin_appointment_detail", args=[visit.pk])
        self.assertContains(response, f'href="{url}"')
        self.assertContains(response, "Show it")

    def test_notice_without_service_says_visit(self):
        self._appointment(Appointment.CONFIRMED, -2)
        self.assertContains(self._get(), "Bantay's visit on")

    def test_notice_several_visits_links_to_unfinished_tab(self):
        self._appointment(Appointment.CONFIRMED, -2)
        self._appointment(Appointment.CONFIRMED, -1)
        response = self._get(status="pending")
        self.assertContains(response, "2 past visits aren't finished.")
        self.assertContains(response, "They are still marked Confirmed.")
        self.assertContains(response, "?status=unfinished")
        self.assertContains(response, "Show them")

    def test_notice_hidden_when_searching_and_on_history_tabs(self):
        self._appointment(Appointment.CONFIRMED, -2)
        for params in ({"q": "Bantay"}, {"status": "completed"}, {"status": "all"}):
            with self.subTest(params=params):
                response = self._get(**params)
                self.assertNotContains(response, "isn't finished")

    def test_unfinished_tab_has_back_link_and_no_active_tab(self):
        self._appointment(Appointment.CONFIRMED, -2)
        response = self._get(status="unfinished")
        self.assertContains(response, "Past visits still marked Confirmed")
        self.assertContains(response, "Back to Upcoming")
        self.assertContains(response, "?status=upcoming")
        self.assertEqual(self._active_tabs(response), [])
        self.assertNotContains(response, "isn't finished")

    def test_search_keeps_values_shows_clear_and_all_active(self):
        visit = self._appointment(Appointment.PENDING, 1)
        response = self._get(q="Bantay", date=visit.date.isoformat())
        self.assertContains(response, 'value="Bantay"')
        self.assertContains(response, f'value="{visit.date.isoformat()}"')
        self.assertContains(response, "Clear search")
        self.assertEqual(self._active_tabs(response), ["All"])

    def test_search_form_does_not_send_status(self):
        html = self._html(self._get())
        form = re.search(r'<form[^>]*role="search".*?</form>', html, re.S).group(0)
        self.assertNotIn('name="status"', form)
        self.assertNotIn("btn-primary", form)
        self.assertNotIn("Clear search", form)

    def test_empty_search_states(self):
        response = self._get(q="Bantai")
        self.assertContains(response, "No appointments match “Bantai”")
        self.assertContains(response, "Check the spelling")
        self.assertContains(response, "Clear search")
        day = self.today + datetime.timedelta(days=5)
        response = self._get(date=day.isoformat())
        self.assertContains(response, f"No appointments for {friendly_date(day)}")

    def test_empty_tab_states(self):
        titles = {
            "pending": "No requests waiting",
            "upcoming": "No upcoming visits",
            "completed": "No completed visits yet",
            "cancelled": "No cancelled appointments",
            "no_show": "No missed visits",
            "all": "No appointments yet",
            "unfinished": "All past visits are finished",
        }
        for tab, title in titles.items():
            with self.subTest(tab=tab):
                response = self._get(status=tab)
                self.assertContains(response, title)
                self.assertNotContains(response, "Clear search")

    def test_pager_disabled_previous_and_links_keep_params(self):
        day = self.today + datetime.timedelta(days=2)
        for i in range(26):
            Appointment.objects.create(
                owner=self.owner,
                pet=self.pet,
                date=day,
                time=datetime.time(8 + i // 4, (i % 4) * 15),
                status=Appointment.PENDING,
            )
        response = self._get(q="Bantay")
        html = self._html(response)
        self.assertContains(response, "Showing 1–25 of 26")
        self.assertRegex(html, r'<span class="btn[^"]*" aria-disabled="true">')
        self.assertNotRegex(html, r"<a [^>]*>[^<]*<svg[^>]*>(?:(?!</a>).)*Previous")
        next_link = re.search(r'<a href="([^"]+)" class="btn btn-outline">Next', html)
        self.assertIsNotNone(next_link)
        for part in ("status=all", "q=Bantay", "page=2"):
            self.assertIn(part, next_link.group(1))

    def test_one_page_hides_pager_buttons(self):
        self._appointment(Appointment.PENDING)
        response = self._get()
        self.assertContains(response, "Showing all 1")
        self.assertNotContains(response, "Previous")

    def test_phone_select_lists_six_tabs_with_counts(self):
        self._appointment(Appointment.PENDING)
        self._appointment(Appointment.COMPLETED, -1)
        html = self._html(self._get(status="completed"))
        select = re.search(r'<select[^>]*name="status".*?</select>', html, re.S)
        options = re.findall(
            r'<option value="([^"]+)"( selected)?>([^<]+)<', select.group(0)
        )
        self.assertEqual(
            options,
            [
                ("pending", "", "Needs approval (1)"),
                ("upcoming", "", "Upcoming (0)"),
                ("completed", " selected", "Completed (1)"),
                ("cancelled", "", "Cancelled (0)"),
                ("no_show", "", "No show (0)"),
                ("all", "", "All (2)"),
            ],
        )

    def test_phone_card_is_one_link(self):
        visit = self._appointment(Appointment.PENDING, 1)
        visit.service = self._service("Grooming")
        visit.save()
        html = self._html(self._get())
        url = reverse("admin_appointment_detail", args=[visit.pk])
        card = re.search(rf'<a href="{url}" class="block.*?</a>', html, re.S)
        self.assertIsNotNone(card)
        self.assertIn("Bantay · Grooming", card.group(0))
        self.assertIn("Ana Cruz", card.group(0))

    def test_no_deleted_static_files_or_multiline_links(self):
        self._appointment(Appointment.PENDING)
        self._appointment(Appointment.CONFIRMED, -1)
        html = self._html(self._get(status="all"))
        for name in (
            "appointments_list.js",
            "appointments_list.css",
            "appointment_detail.css",
            "css/admin/",
        ):
            self.assertNotIn(name, html)
        self.assertNotRegex(html, r"<a\b[^>]*\n")

    def test_existing_status_links_land_on_a_valid_tab(self):
        """Dashboard/sidebar links use ?status=pending or no status."""
        self._appointment(Appointment.PENDING)
        for status, label in (
            ("pending", "Needs approval"),
            ("confirmed", "Upcoming"),
            ("no_show", "No show"),
            ("bogus", "Needs approval"),
            ("", "Needs approval"),
        ):
            with self.subTest(status=status):
                response = self._get(status=status) if status else self._get()
                self.assertEqual(response.status_code, 200)
                self.assertEqual(self._active_tabs(response), [label])


# ─── Walk-ins (Slice 3c-1) ────────────────────────────────────────────────────

MANILA = ZoneInfo("Asia/Manila")
# Monday, November 2, 2026 (Manila): "today" in the walk-in tests below.
WALK_IN_DAY = datetime.date(2026, 11, 2)


def _manila(hour, minute=0, day=WALK_IN_DAY):
    """An aware datetime on day at hour:minute, Manila time."""
    return datetime.datetime(day.year, day.month, day.day, hour, minute, tzinfo=MANILA)


class FrozenNowTestBase(AppointmentTestBase):
    """AppointmentTestBase with "now" fixed at NOW (Manila) for every test."""

    NOW = _manila(9, 30)

    def setUp(self):
        self.freeze(self.NOW)
        super().setUp()

    def freeze(self, now):
        patcher = mock.patch("django.utils.timezone.now", return_value=now)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _hours(self, slots):
        return [s.hour for s in slots]


class WalkInSlotTests(FrozenNowTestBase):
    """get_available_slots(walk_in=True). Clinic: 8:00-17:00, hourly slots."""

    def test_today_offers_the_slot_in_progress_and_later(self):
        slots = get_available_slots(WALK_IN_DAY, walk_in=True)
        self.assertEqual(self._hours(slots), [9, 10, 11, 12, 13, 14, 15, 16])

    def test_a_slot_that_just_ended_is_not_offered(self):
        self.freeze(_manila(10, 0))
        slots = get_available_slots(WALK_IN_DAY, walk_in=True)
        self.assertEqual(slots[0], datetime.time(10, 0))

    def test_after_closing_offers_none(self):
        self.freeze(_manila(17, 30))
        self.assertEqual(get_available_slots(WALK_IN_DAY, walk_in=True), [])

    def test_future_date_offers_the_full_day(self):
        day = WALK_IN_DAY + datetime.timedelta(days=3)
        slots = get_available_slots(day, walk_in=True)
        self.assertEqual(self._hours(slots), list(range(8, 17)))

    def test_past_and_beyond_window_offer_none(self):
        for day in (
            WALK_IN_DAY - datetime.timedelta(days=1),
            WALK_IN_DAY + datetime.timedelta(days=31),
        ):
            with self.subTest(day=day):
                self.assertEqual(get_available_slots(day, walk_in=True), [])

    def test_blocked_date_offers_none(self):
        BlockedDate.objects.create(date=WALK_IN_DAY, reason="Holiday")
        self.assertEqual(get_available_slots(WALK_IN_DAY, walk_in=True), [])

    def test_closed_day_offers_none(self):
        clinic = ClinicSettings.objects.get()
        clinic.working_days = [1, 2, 3, 4, 5]  # closed on Mondays
        clinic.save()
        self.assertEqual(get_available_slots(WALK_IN_DAY, walk_in=True), [])

    def test_pending_request_keeps_its_slot(self):
        Appointment.objects.create(
            owner=self.owner,
            pet=self.pet,
            date=WALK_IN_DAY,
            time=datetime.time(11, 0),
            status=Appointment.PENDING,
        )
        slots = get_available_slots(WALK_IN_DAY, walk_in=True)
        self.assertNotIn(datetime.time(11, 0), slots)
        self.assertIn(datetime.time(12, 0), slots)


class OwnerSlotsUnchangedTests(FrozenNowTestBase):
    """The default (owner) rules: today only offers slots after the 15:00
    cutoff, whatever the time is."""

    def test_today_morning_offers_only_slots_after_the_cutoff(self):
        self.assertEqual(self._hours(get_available_slots(WALK_IN_DAY)), [16])

    def test_today_after_the_cutoff_still_offers_the_same_slots(self):
        self.freeze(_manila(15, 30))
        self.assertEqual(self._hours(get_available_slots(WALK_IN_DAY)), [16])

    def test_future_date_offers_the_full_day(self):
        day = WALK_IN_DAY + datetime.timedelta(days=3)
        self.assertEqual(self._hours(get_available_slots(day)), list(range(8, 17)))

    def test_owner_slot_view_uses_the_owner_rules(self):
        self.client.force_login(self.owner.user)
        response = self.client.get(
            reverse("owner_get_slots"), {"date": WALK_IN_DAY.isoformat()}
        )
        self.assertContains(response, 'value="16:00:00"')
        self.assertNotContains(response, 'value="09:00:00"')


class WalkInSlotViewTests(FrozenNowTestBase):
    """admin_get_slots_for_date with and without ?for=walk_in."""

    def _get(self, **params):
        params.setdefault("date", WALK_IN_DAY.isoformat())
        return self.client.get(reverse("admin_get_slots_for_date"), params)

    def test_options_for_walk_in(self):
        response = self._get(**{"for": "walk_in"})
        self.assertContains(response, '<option value="09:00:00">9:00 AM</option>')
        self.assertNotContains(response, '"08:00:00"')

    def test_buttons_for_walk_in(self):
        response = self._get(**{"for": "walk_in", "as": "buttons"})
        self.assertContains(response, 'type="radio" name="time" value="09:00:00"')

    def test_default_keeps_the_cutoff(self):
        for params in ({}, {"as": "buttons"}):
            with self.subTest(params=params):
                response = self._get(**params)
                self.assertNotContains(response, '"09:00:00"')
                self.assertContains(response, '"16:00:00"')

    def test_move_dialog_uses_the_default_rules(self):
        appointment = self._appointment(Appointment.PENDING)
        response = self.client.get(
            reverse("admin_appointment_detail", args=[appointment.pk])
        )
        self.assertContains(response, f'{reverse("admin_get_slots_for_date")}?as=')
        self.assertNotContains(response, "for=walk_in")

    def test_walk_in_visit_asks_for_walk_in_slots(self):
        response = self.client.get(
            reverse("admin_walkin_appointment_visit"), {"owner": self.owner.pk}
        )
        self.assertContains(
            response,
            f'{reverse("admin_get_slots_for_date")}?for=walk_in&amp;as=buttons',
        )


class WalkInAppointmentCreateTests(FrozenNowTestBase):
    """Walk-in "Book walk-in" (admin_walkin_appointment_visit, action=book).
    "Now" is Mon, Nov 2, 9:30 AM."""

    def _post(self, **overrides):
        data = {
            "owner": self.owner.pk,
            "pet": self.pet.pk,
            "date": WALK_IN_DAY.isoformat(),
            "time": "10:00:00",
            "reason": "Limping",
            "action": "book",
        }
        data.update(overrides)
        return self.client.post(reverse("admin_walkin_appointment_visit"), data)

    def _assert_refused(self, response, error=None):
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Appointment.objects.exists())
        if error:
            self.assertIn(error, str(response.context["form"].errors))

    def test_creates_a_confirmed_walk_in_today(self):
        response = self._post()
        appointment = Appointment.objects.get()
        self.assertRedirects(
            response,
            reverse("admin_appointment_detail", args=[appointment.pk]),
            fetch_redirect_response=False,
        )
        self.assertEqual(appointment.status, Appointment.CONFIRMED)
        self.assertTrue(appointment.is_walk_in)
        self.assertEqual(appointment.time, datetime.time(10, 0))

    def test_today_before_the_cutoff_is_allowed(self):
        """The slot in progress (9:00 at 9:30) can be booked."""
        self._post(time="09:00:00")
        self.assertEqual(Appointment.objects.get().time, datetime.time(9, 0))

    def test_future_date_in_the_window_is_allowed(self):
        day = WALK_IN_DAY + datetime.timedelta(days=3)
        self._post(date=day.isoformat())
        appointment = Appointment.objects.get()
        self.assertEqual(appointment.date, day)
        self.assertTrue(appointment.is_walk_in)

    def test_past_date_refused(self):
        day = WALK_IN_DAY - datetime.timedelta(days=1)
        response = self._post(date=day.isoformat())
        self._assert_refused(response, "Cannot create an appointment in the past.")

    def test_date_beyond_the_window_refused(self):
        day = WALK_IN_DAY + datetime.timedelta(days=31)
        response = self._post(date=day.isoformat())
        self._assert_refused(response, "only allowed up to 30 days in advance")

    def test_slot_that_ended_refused(self):
        self._assert_refused(self._post(time="08:00:00"))

    def test_slot_conflict_refused(self):
        taken = Appointment.objects.create(
            owner=self.owner,
            pet=self.pet,
            date=WALK_IN_DAY,
            time=datetime.time(10, 0),
            status=Appointment.PENDING,
        )
        response = self._post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(Appointment.objects.all()), [taken])

    def test_pet_of_another_owner_refused(self):
        other_user = User.objects.create_user(
            username="other", email="other@example.com", role=User.PET_OWNER
        )
        other = PetOwner.objects.create(
            user=other_user, first_name="Ben", last_name="Reyes"
        )
        other_pet = Pet.objects.create(owner=other, name="Muning", species="Cat")
        response = self._post(pet=other_pet.pk)
        self._assert_refused(response, "Select a valid choice.")

    def test_owner_role_refused(self):
        self.client.force_login(self.owner.user)
        response = self._post()
        self.assertRedirects(
            response, reverse("owner_dashboard"), fetch_redirect_response=False
        )
        self.assertFalse(Appointment.objects.exists())

    def test_flash_message_today(self):
        response = self._post()
        self.assertEqual(
            self._messages(response), ["Walk-in booked for Bantay today at 10:00 AM."]
        )

    def test_flash_message_tomorrow(self):
        day = WALK_IN_DAY + datetime.timedelta(days=1)
        response = self._post(date=day.isoformat())
        self.assertEqual(
            self._messages(response),
            ["Walk-in booked for Bantay tomorrow at 10:00 AM."],
        )

    def test_flash_message_later_day(self):
        day = WALK_IN_DAY + datetime.timedelta(days=3)
        response = self._post(date=day.isoformat())
        self.assertEqual(
            self._messages(response),
            ["Walk-in booked for Bantay on Thu, Nov 5 at 10:00 AM."],
        )

    def test_owner_notification_and_email(self):
        self._post()
        notification = Notification.objects.get(recipient=self.owner.user)
        self.assertEqual(notification.notification_type, "appointment_approved")
        self.assertEqual(notification.title, "Visit booked by the clinic")
        self.assertEqual(
            notification.message,
            "The clinic booked a visit for Bantay on Mon, Nov 2 at 10:00 AM. "
            "The time is confirmed.",
        )
        self.assertEqual(notification.related_appointment, Appointment.objects.get())
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].subject, "Visit booked — Hapi Vet")
        self.assertNotIn("November 02, 2026", mail.outbox[0].body)


# ─── Walk-in flow (Slice 3c-2) ────────────────────────────────────────────────


class WalkInFlowTestBase(FrozenNowTestBase):
    """Walk-in steps. "Now" is Mon, Nov 2, 9:30 AM; owner Ana Cruz has Bantay."""

    STEP_1 = "admin_walkin_appointment_create"
    VISIT = "admin_walkin_appointment_visit"

    def _owner(self, first, last, phone="", pets=(), archived=False):
        n = User.objects.count()
        user = User.objects.create_user(
            username=f"user{n}", email=f"user{n}@example.com", role=User.PET_OWNER
        )
        owner = PetOwner.objects.create(
            user=user,
            first_name=first,
            last_name=last,
            contact_number=phone,
            is_archived=archived,
        )
        for name in pets:
            Pet.objects.create(owner=owner, name=name, species="Dog")
        return owner

    def _visit(self, **params):
        return self.client.get(reverse(self.VISIT), params)

    def _post_visit(self, **overrides):
        data = {
            "owner": self.owner.pk,
            "pet": self.pet.pk,
            "date": WALK_IN_DAY.isoformat(),
            "time": "10:00:00",
            "service": "",
            "reason": "Second anti-rabies shot.",
            "action": "review",
        }
        data.update(overrides)
        return self.client.post(reverse(self.VISIT), data)

    def _primary_buttons(self, response):
        return len(re.findall(r"\bbtn-primary\b", response.content.decode()))


class WalkInOwnerSearchTests(WalkInFlowTestBase):
    """search_walkin_owners, through step 1 (?q=)."""

    def _names(self, q):
        response = self.client.get(reverse(self.STEP_1), {"q": q})
        return [owner.full_name for owner in response.context["owners"]]

    def test_matches_owner_name(self):
        self._owner("Rosa", "Dela Cruz")
        self._owner("Liza", "Reyes")
        self.assertEqual(self._names("cruz"), ["Ana Cruz", "Rosa Dela Cruz"])

    def test_matches_pet_name(self):
        self._owner("Liza", "Reyes", pets=["Brownie"])
        self.assertEqual(self._names("brown"), ["Liza Reyes"])

    def test_matches_phone(self):
        self._owner("Liza", "Reyes", phone="09171234567")
        self.assertEqual(self._names("0917123"), ["Liza Reyes"])

    def test_every_word_must_match(self):
        self._owner("Rosa", "Dela Cruz", pets=["Max"])
        self._owner("Ben", "Reyes", pets=["Max"])
        self.assertEqual(self._names("max cruz"), ["Rosa Dela Cruz"])

    def test_case_insensitive(self):
        self.assertEqual(self._names("ANA bAnTaY"), ["Ana Cruz"])

    def test_archived_owner_left_out(self):
        self._owner("Carla", "Cruz", archived=True)
        self.assertEqual(self._names("cruz"), ["Ana Cruz"])

    def test_archived_pet_neither_matches_nor_shows(self):
        owner = self._owner("Liza", "Reyes", pets=["Brownie", "Max"])
        Pet.objects.filter(owner=owner, name="Brownie").update(is_archived=True)
        self.assertEqual(self._names("brownie"), [])
        response = self.client.get(reverse(self.STEP_1), {"q": "liza"})
        self.assertEqual(
            [pet.name for pet in response.context["owners"][0].active_pets], ["Max"]
        )
        self.assertNotContains(response, "Brownie")

    def test_capped_at_20_with_a_note(self):
        for i in range(21):
            self._owner(f"Owner{i:02}", "Santos")
        response = self.client.get(reverse(self.STEP_1), {"q": "santos"})
        self.assertEqual(len(response.context["owners"]), 20)
        self.assertTrue(response.context["is_cut"])
        self.assertContains(response, "Showing the first 20.")

    def test_no_note_when_not_cut(self):
        response = self.client.get(reverse(self.STEP_1), {"q": "cruz"})
        self.assertFalse(response.context["is_cut"])
        self.assertNotContains(response, "Showing the first")

    def test_empty_q_shows_no_results_list(self):
        for q in ("", "   "):
            with self.subTest(q=q):
                response = self.client.get(reverse(self.STEP_1), {"q": q})
                self.assertEqual(response.context["owners"], [])
                self.assertNotContains(response, 'id="results-title"')
                self.assertContains(response, "First visit at the clinic?")
                self.assertContains(
                    response, f'href="{reverse("admin_walkin_create")}"'
                )

    def test_query_count_flat_as_owners_grow(self):
        def count_queries():
            with CaptureQueriesContext(connection) as queries:
                self.client.get(reverse(self.STEP_1), {"q": "santos"})
            return len(queries)

        for i in range(3):
            self._owner(f"A{i}", "Santos", pets=["Max", "Muning"])
        few = count_queries()
        for i in range(10):
            self._owner(f"B{i}", "Santos", pets=["Max", "Muning"])
        self.assertEqual(count_queries(), few)


class WalkInStepOneTests(WalkInFlowTestBase):
    """Step 1 page: "Who is it for?"."""

    def test_page_layout(self):
        response = self.client.get(reverse(self.STEP_1))
        self.assertTemplateUsed(response, "layouts/admin.html")
        self.assertContains(response, "Who is it for?")
        self.assertContains(response, "Been here before? Find the owner")
        # The top-bar search is emptied on this step only
        self.assertNotContains(response, 'id="global-search"')
        self.assertEqual(self._primary_buttons(response), 0)

    def test_result_links_to_step_2(self):
        response = self.client.get(reverse(self.STEP_1), {"q": "ana"})
        self.assertContains(response, f'{reverse(self.VISIT)}?owner={self.owner.pk}"')
        self.assertContains(response, "1 owner matches “ana”")


class WalkInVisitStepTests(WalkInFlowTestBase):
    """Step 2 page: "The visit" (GET)."""

    def _assert_back_to_step_1(self, response):
        self.assertRedirects(
            response, reverse(self.STEP_1), fetch_redirect_response=False
        )
        self.assertEqual(
            self._messages(response),
            ["That owner could not be found. Please search again."],
        )

    def test_bad_owner_goes_back_to_step_1(self):
        for params in ({}, {"owner": "abc"}, {"owner": "99999"}):
            with self.subTest(params=params):
                # A fresh session per subtest: redirects aren't followed, so
                # the previous subtest's message would still be queued
                self.client = self.client_class()
                self.client.force_login(self.admin)
                self._assert_back_to_step_1(self._visit(**params))

    def test_archived_owner_goes_back_to_step_1(self):
        owner = self._owner("Carla", "Cruz", pets=["Max"], archived=True)
        self._assert_back_to_step_1(self._visit(owner=owner.pk))

    def test_page_layout(self):
        response = self._visit(owner=self.owner.pk)
        self.assertTemplateUsed(response, "admin/appointments/walkin_visit.html")
        self.assertContains(response, "The visit")
        self.assertContains(response, 'id="global-search"')
        self.assertEqual(self._primary_buttons(response), 1)
        self.assertContains(response, 'name="action" value="review"')

    def test_pet_param_preselects(self):
        muning = Pet.objects.create(owner=self.owner, name="Muning", species="Cat")
        response = self._visit(owner=self.owner.pk, pet=muning.pk)
        self.assertEqual(response.context["selected_pet"], str(muning.pk))
        self.assertRegex(
            response.content.decode(), rf'name="pet" value="{muning.pk}"[^>]* checked'
        )

    def test_pet_of_another_owner_ignored(self):
        Pet.objects.create(owner=self.owner, name="Muning", species="Cat")
        other = self._owner("Ben", "Reyes", pets=["Max"])
        response = self._visit(owner=self.owner.pk, pet=other.pets.get().pk)
        self.assertEqual(response.context["selected_pet"], "")
        self.assertNotContains(response, "Max")

    def test_archived_pet_ignored_and_not_listed(self):
        archived = Pet.objects.create(
            owner=self.owner, name="Muning", species="Cat", is_archived=True
        )
        response = self._visit(owner=self.owner.pk, pet=archived.pk)
        # Bantay is now the only pet, so it is picked instead
        self.assertEqual(response.context["selected_pet"], str(self.pet.pk))
        self.assertNotContains(response, "Muning")

    def test_only_pet_preselected(self):
        response = self._visit(owner=self.owner.pk)
        self.assertEqual(response.context["selected_pet"], str(self.pet.pk))

    def test_two_pets_none_preselected(self):
        Pet.objects.create(owner=self.owner, name="Muning", species="Cat")
        response = self._visit(owner=self.owner.pk)
        self.assertEqual(response.context["selected_pet"], "")

    def test_owner_with_no_pets(self):
        owner = self._owner("Liza", "Reyes")
        response = self._visit(owner=owner.pk)
        self.assertContains(response, "This owner has no pets yet")
        self.assertContains(
            response, f'href="{reverse("admin_pet_add", args=[owner.pk])}"'
        )
        self.assertNotContains(response, 'name="action"')
        self.assertEqual(self._primary_buttons(response), 0)

    def test_date_defaults_to_today(self):
        response = self._visit(owner=self.owner.pk)
        self.assertEqual(response.context["date_value"], "2026-11-02")
        self.assertContains(response, 'name="date" value="2026-11-02"')

    def test_slot_buttons_rendered_on_load(self):
        response = self._visit(owner=self.owner.pk)
        self.assertTemplateUsed(response, "admin/appointments/_slot_buttons.html")
        self.assertContains(response, "Free times today, Monday, November 2")
        # Walk-in rules: the 9:00 slot in progress is offered, 8:00 has ended
        self.assertContains(response, 'type="radio" name="time" value="09:00:00"')
        self.assertNotContains(response, 'value="08:00:00"')
        self.assertContains(response, ">Time <span")


class WalkInReviewTests(WalkInFlowTestBase):
    """Step 2 POST, action=review: step 3 (nothing saved) or step 2 errors."""

    def test_valid_post_shows_summary_and_saves_nothing(self):
        response = self._post_visit()
        self.assertTemplateUsed(response, "admin/appointments/walkin_review.html")
        self.assertFalse(Appointment.objects.exists())
        self.assertContains(response, "Check and book")
        self.assertContains(response, "Today, Monday, November 2 at 10:00 AM")
        self.assertContains(response, "Bantay")
        self.assertContains(response, "Second anti-rabies shot.")
        self.assertContains(response, ">Confirmed</span>")
        self.assertContains(response, ">Walk-in</span>")
        self.assertEqual(self._primary_buttons(response), 1)
        for name, value in (
            ("owner", self.owner.pk),
            ("pet", self.pet.pk),
            ("date", "2026-11-02"),
            ("time", "10:00:00"),
            ("reason", "Second anti-rabies shot."),
        ):
            self.assertContains(
                response, f'<input type="hidden" name="{name}" value="{value}">'
            )
        self.assertContains(response, 'name="action" value="book"')
        self.assertContains(response, 'name="action" value="change"')

    def test_review_is_the_default_action(self):
        response = self._post_visit(action="")
        self.assertTemplateUsed(response, "admin/appointments/walkin_review.html")
        self.assertFalse(Appointment.objects.exists())

    def test_summary_shows_service_and_phone(self):
        service = Service.objects.create(
            service_name="Vaccination", status=Service.ACTIVE
        )
        PetOwner.objects.filter(pk=self.owner.pk).update(contact_number="0920 444")
        response = self._post_visit(service=service.pk)
        self.assertContains(response, "Vaccination")
        self.assertContains(response, "Ana Cruz · 0920 444")
        self.assertContains(
            response, f'<input type="hidden" name="service" value="{service.pk}">'
        )

    def test_notify_line_with_email(self):
        response = self._post_visit()
        self.assertContains(response, "Ana Cruz will get an email about this visit.")

    def test_notify_line_without_email(self):
        User.objects.filter(pk=self.owner.user.pk).update(email="")
        PetOwner.objects.filter(pk=self.owner.pk).update(contact_number="0917 555 0101")
        response = self._post_visit()
        self.assertContains(
            response,
            "Ana Cruz has no email. Tell them the time in person or call "
            "0917 555 0101.",
        )

    def test_notify_line_without_email_or_phone(self):
        User.objects.filter(pk=self.owner.user.pk).update(email="")
        response = self._post_visit()
        self.assertContains(
            response, "Ana Cruz has no email. Tell them the time in person."
        )

    def test_invalid_post_shows_step_2_with_errors_and_values(self):
        response = self._post_visit(time="", reason="Limping")
        self.assertTemplateUsed(response, "admin/appointments/walkin_visit.html")
        self.assertFalse(Appointment.objects.exists())
        self.assertIn("Pick a time.", response.context["form"].errors["time"])
        self.assertContains(response, "Pick a time.")
        self.assertContains(response, "Limping")
        self.assertContains(response, 'name="date" value="2026-11-02"')
        self.assertEqual(response.context["selected_pet"], str(self.pet.pk))

    def test_invalid_post_keeps_the_chosen_time(self):
        response = self._post_visit(pet="")
        self.assertIn("Pick the pet.", response.context["form"].errors["pet"])
        self.assertRegex(
            response.content.decode(), r'name="time" value="10:00:00"[^>]* checked'
        )

    def test_bad_owner_goes_back_to_step_1(self):
        response = self._post_visit(owner="99999")
        self.assertRedirects(
            response, reverse(self.STEP_1), fetch_redirect_response=False
        )
        self.assertFalse(Appointment.objects.exists())


class WalkInChangeAndBookTests(WalkInFlowTestBase):
    """Step 3 buttons: "Change" (action=change) and "Book walk-in"."""

    def test_change_keeps_every_value(self):
        service = Service.objects.create(
            service_name="Vaccination", status=Service.ACTIVE
        )
        response = self._post_visit(action="change", service=service.pk)
        self.assertTemplateUsed(response, "admin/appointments/walkin_visit.html")
        self.assertFalse(response.context["form"].is_bound)
        self.assertEqual(response.context["selected_pet"], str(self.pet.pk))
        self.assertEqual(response.context["selected_time"], "10:00:00")
        html = response.content.decode()
        self.assertRegex(html, r'name="time" value="10:00:00"[^>]* checked')
        self.assertRegex(html, rf'<option value="{service.pk}" selected>')
        self.assertContains(response, "Second anti-rabies shot.")
        self.assertContains(response, 'name="date" value="2026-11-02"')
        self.assertFalse(Appointment.objects.exists())

    def test_book_saves_and_redirects_with_the_flash(self):
        response = self._post_visit(action="book")
        appointment = Appointment.objects.get()
        self.assertRedirects(
            response,
            reverse("admin_appointment_detail", args=[appointment.pk]),
            fetch_redirect_response=False,
        )
        self.assertEqual(appointment.status, Appointment.CONFIRMED)
        self.assertTrue(appointment.is_walk_in)
        self.assertEqual(appointment.owner, self.owner)
        self.assertEqual(appointment.reason, "Second anti-rabies shot.")
        self.assertEqual(
            self._messages(response), ["Walk-in booked for Bantay today at 10:00 AM."]
        )

    def test_book_when_the_slot_was_taken(self):
        other = self._owner("Ben", "Reyes", pets=["Max"])
        taken = Appointment.objects.create(
            owner=other,
            pet=other.pets.get(),
            date=WALK_IN_DAY,
            time=datetime.time(10, 0),
            status=Appointment.PENDING,
        )
        response = self._post_visit(action="book", reason="Limping")
        self.assertTemplateUsed(response, "admin/appointments/walkin_visit.html")
        self.assertEqual(list(Appointment.objects.all()), [taken])
        self.assertContains(
            response,
            "10:00 AM was just taken. Someone else booked it a moment ago. "
            "Please pick another time.",
        )
        self.assertNotContains(response, "Select a valid choice")
        self.assertEqual(response.context["selected_time"], "")
        self.assertEqual(response.context["selected_pet"], str(self.pet.pk))
        self.assertContains(response, "Limping")
        self.assertContains(response, 'name="date" value="2026-11-02"')

    def test_book_rechecks_everything(self):
        """A time that has ended is refused, but not as 'just taken'."""
        response = self._post_visit(action="book", time="08:00:00")
        self.assertFalse(Appointment.objects.exists())
        self.assertIn("time", response.context["form"].errors)
        self.assertEqual(response.context["taken_message"], "")


class WalkInAccessTests(WalkInFlowTestBase):
    """Owners are refused on every walk-in step."""

    def test_owner_role_refused_on_every_step(self):
        self.client.force_login(self.owner.user)
        visit = reverse(self.VISIT)
        requests = (
            ("get", reverse(self.STEP_1), {"q": "ana"}),
            ("get", visit, {"owner": self.owner.pk}),
            ("post", visit, {"owner": self.owner.pk, "action": "review"}),
            ("post", visit, {"owner": self.owner.pk, "action": "change"}),
            ("post", visit, {"owner": self.owner.pk, "action": "book"}),
            ("get", reverse("admin_walkin_create"), {}),
            ("post", reverse("admin_walkin_create"), {}),
        )
        for method, url, data in requests:
            with self.subTest(method=method, url=url, data=data):
                response = getattr(self.client, method)(url, data)
                self.assertRedirects(
                    response, reverse("owner_dashboard"), fetch_redirect_response=False
                )
        self.assertFalse(Appointment.objects.exists())
