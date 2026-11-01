import datetime
import re
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.core import mail
from django.db import DatabaseError, connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from django.utils.dateformat import format as date_format

from appointments.models import Appointment, ClinicSettings
from billing.models import BillingReceipt
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
        return [a.pk for a in response.context["appointments"]]

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
        rows = {a.pk: a for a in self._get(status="all").context["appointments"]}
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
        self.assertEqual(len(first.context["appointments"]), 25)
        self.assertEqual(first.context["prev_page_query"], "")
        next_query = first.context["next_page_query"]
        for part in ("status=all", "q=Bantay", f"date={day.isoformat()}", "page=2"):
            self.assertIn(part, next_query)

        second = self.client.get(reverse("admin_appointments") + next_query)
        self.assertEqual(len(second.context["appointments"]), 1)
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

    def test_htmx_returns_partial(self):
        visit = self._appointment(Appointment.PENDING)
        response = self.client.get(
            reverse("admin_appointments"), headers={"HX-Request": "true"}
        )
        self.assertTemplateUsed(
            response, "admin/appointments/_appointment_list_partial.html"
        )
        self.assertTemplateNotUsed(response, "admin/appointments/list.html")
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
