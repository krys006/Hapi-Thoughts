import datetime
import io
import re
import shutil
import tempfile
from decimal import Decimal
from html import unescape
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.core import mail
from django.db import DatabaseError, connection, transaction
from django.core.paginator import Paginator
from django.http import Http404
from django.templatetags.static import static
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from django.utils.dateformat import format as date_format

from appointments.models import Appointment, BlockedDate, ClinicSettings
from appointments.utils import (
    CANNOT_CHANGE_MESSAGE,
    CLINIC_SETTINGS_DEFAULTS,
    DAY_FULL_MESSAGE,
    DAY_GONE_MESSAGE,
    DAY_NOT_OPEN_MESSAGE,
    PET_GONE_MESSAGE,
    PICK_AGAIN_MESSAGE,
    PICK_DAY_FIRST_MESSAGE,
    PICK_NEW_TIME_MESSAGE,
    RESCHEDULE_SENT_MESSAGE,
    SERVICE_GONE_MESSAGE,
    SERVICE_MISSING_MESSAGE,
    START_MESSAGE,
    WHY_MISSING_MESSAGE,
    clinic_hours_line,
    closed_days_line,
    get_available_slots,
    get_clinic_settings,
    is_slot_available,
    is_within_24h,
    list_period,
    lock_schedule,
    owner_booking_calendar,
    owner_can_change,
    owner_reschedule_context,
    owner_visit_note,
    owner_visits_context,
    paging_links,
    reschedule_by_owner,
    resolve_period,
    slot_in_progress,
    time_groups,
    vet_initials,
)
from billing.models import BillingItem, BillingReceipt, Service
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
        # Slice 8a-1b: a decline is stored as DECLINED (was OTHER)
        self.assertEqual(appointment.cancellation_reason, Appointment.DECLINED)
        self.assertEqual(appointment.cancellation_detail, "The vet is away that week.")
        # 8a-2b: the app text is compact and carries the clinic's message as
        # its one clause; "Message from the clinic: …" is now in the email
        self.assertIn("The vet is away that week.", self._owner_notification().message)
        self.assertIn(
            "Message from the clinic: The vet is away that week.",
            mail.outbox[-1].body,
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
        # Complete now also starts the visit's bill, so its flash says so
        # (the "Marked ... as completed." text alone stays for a visit that
        # already has a bill: see AutoBillOnCompleteTests)
        expected = {
            "admin_appointment_complete": (
                "Marked Bantay's appointment on today at 9:00 AM as completed. "
                "A bill was started for it."
            ),
            "admin_appointment_no_show": (
                "Marked Bantay's appointment on today at 9:00 AM as a no-show."
            ),
        }
        for url_name, message in expected.items():
            with self.subTest(url_name=url_name):
                appointment = self._appointment(
                    Appointment.CONFIRMED, days_from_today=0
                )
                response = self._post(url_name, appointment)
                self.assertIn(message, self._messages(response))


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
        # 8a-2b: short title and compact message (were "Appointment moved by
        # the clinic" and the full sentence, which is now the email body)
        self.assertEqual(notification.title, "The clinic moved your visit")
        self.assertEqual(
            notification.message,
            f"Bantay · now {date_format(self.target, 'D, M j')} at 9:00 AM. "
            "The new time is confirmed.",
        )
        self.assertEqual(
            mail.outbox[0].body,
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

    def _completed_with(self, record=False, receipt=None):
        """A completed visit today; receipt is None (no receipt) or the
        receipt's payment_status (6a-4: was True for a PENDING receipt)."""
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
                receipt_number=f"R-{appointment.pk:04d}",
                payment_status=receipt,
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
        """6a-4 (rule 26): "All done" needs a PAID receipt, so this now makes
        a paid one (was a PENDING receipt). Assertions unchanged."""
        appointment = self._completed_with(record=True, receipt=BillingReceipt.PAID)
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
        self.assertContains(response, "Open bill")
        self.assertNotContains(response, "Add medical record")
        self.assertNotContains(response, "Create bill")
        # Both buttons are outlines: no primary button left
        self.assertNotContains(response, "btn btn-primary")

    # ── 6a-4 (rule 26): the visit's receipt on the page ──────────────────────

    def _receipt_link(self, appointment):
        """The banner's one-line link to the visit's receipt."""
        receipt = appointment.billing_receipt
        url = reverse("admin_receipt_detail", args=[receipt.pk])
        return (
            f'<a href="{url}" class="font-medium underline underline-offset-2">'
            f"Open bill {receipt.receipt_number}</a>"
        )

    def test_receipt_state_in_the_context(self):
        cases = [
            (None, None),
            (BillingReceipt.PENDING, "unpaid"),
            (BillingReceipt.PAID, "paid"),
            (BillingReceipt.CANCELLED, "cancelled"),
        ]
        for status, state in cases:
            with self.subTest(status=status):
                response = self._get(self._completed_with(record=True, receipt=status))
                self.assertEqual(response.context["receipt_state"], state)

    def test_unpaid_receipt_is_not_all_done(self):
        for record in (True, False):
            with self.subTest(record=record):
                appointment = self._completed_with(
                    record=record, receipt=BillingReceipt.PENDING
                )
                response = self._get(appointment)
                number = appointment.billing_receipt.receipt_number
                self.assertNotContains(response, "All done for this visit")
                self.assertContains(response, "Visit completed")
                self.assertContains(response, "Bill not paid yet.")
                self.assertContains(response, self._receipt_link(appointment))
                record_line = (
                    "The medical record is added."
                    if record
                    else "Next, add the medical record."
                )
                self.assertContains(response, record_line)
                # Details row: number + status label
                self.assertContains(response, f"· {number} · Unpaid")
                # Actions: open it, never create another
                self.assertContains(response, "Open bill</a>")
                self.assertNotContains(response, "Create bill")

    def test_paid_receipt_without_a_record(self):
        appointment = self._completed_with(receipt=BillingReceipt.PAID)
        response = self._get(appointment)
        number = appointment.billing_receipt.receipt_number
        self.assertNotContains(response, "All done for this visit")
        self.assertContains(
            response, "The bill is paid. One thing left: add the medical record."
        )
        self.assertNotContains(response, "Bill not paid yet.")
        self.assertContains(response, f"· {number} · Paid")
        self.assertContains(response, "Add medical record")
        self.assertNotContains(response, "Create bill")

    def test_paid_receipt_with_a_record_is_all_done(self):
        appointment = self._completed_with(record=True, receipt=BillingReceipt.PAID)
        response = self._get(appointment)
        self.assertContains(response, "All done for this visit")
        self.assertContains(
            response, "The medical record is added and the bill is paid."
        )
        self.assertNotContains(response, self._receipt_link(appointment))

    def test_cancelled_receipt(self):
        appointment = self._completed_with(
            record=True, receipt=BillingReceipt.CANCELLED
        )
        response = self._get(appointment)
        number = appointment.billing_receipt.receipt_number
        self.assertNotContains(response, "All done for this visit")
        self.assertContains(response, "The bill for this visit was cancelled.")
        self.assertContains(response, self._receipt_link(appointment))
        self.assertContains(response, f"· {number} · Cancelled")
        # One receipt per visit: no Create receipt, no link to the create page
        self.assertNotContains(response, "Create bill")
        self.assertNotContains(response, reverse("admin_receipt_create"))
        self.assertContains(response, "Open bill</a>")

    def test_no_receipt_offers_create_only(self):
        appointment = self._completed_with(record=True)
        response = self._get(appointment)
        self.assertIsNone(response.context["receipt_state"])
        self.assertContains(
            response,
            f'href="{reverse("admin_receipt_create")}?appointment={appointment.pk}"',
        )
        self.assertContains(response, "Create bill")
        self.assertNotContains(response, "Open bill")
        self.assertNotContains(response, "Bill not paid yet.")

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
        """An old ?date= link opens that one day on the "All" tab."""
        on_day = self._appointment(Appointment.COMPLETED, -2)
        self._appointment(Appointment.COMPLETED, -3)
        response = self._get(date=on_day.date.isoformat())
        self.assertEqual(response.context["active_tab"], "all")
        self.assertEqual(response.context["period"]["kind"], "day")
        self.assertEqual(response.context["period"]["start"], on_day.date)
        self.assertEqual(self._ids(response), [on_day.pk])

    def test_bad_date_is_ignored(self):
        pending = self._appointment(Appointment.PENDING)
        response = self._get(date="31/10/2026")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["period"]["kind"], "any")
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
        for part in (
            "status=all",
            "q=Bantay",
            "show=day",
            f"from={day.isoformat()}",
            "page=2",
        ):
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

    def test_page_uses_admin_layout_with_two_search_forms_and_one_primary(self):
        """One search form in the top bar (md up), the same one in the page
        body below md."""
        response = self._get()
        html = self._html(response)
        self.assertTemplateUsed(response, "layouts/admin.html")
        self.assertNotContains(response, "Find a pet or owner")
        self.assertContains(response, 'name="q"', count=2)
        topbar, page = re.findall(r'<form [^>]*class="([^"]*)" role="search">', html)
        self.assertEqual(topbar.split()[0], "hidden")
        self.assertIn("md:flex", topbar.split())
        self.assertIn("flex", page.split())
        self.assertIn("md:hidden", page.split())
        header = html[: html.index('id="main-content"')]
        self.assertIn('id="topbar-appointment-search"', header)
        self.assertNotIn('id="page-appointment-search"', header)
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
        heading = f">Today, {date_format(self.today, 'l, F j')}</h2>"
        self.assertContains(response, heading)
        self.assertContains(response, ">1 appointment<")

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
        self.assertContains(response, f'name="from" value="{visit.date.isoformat()}"')
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
        self.assertContains(response, "No appointments in this period")

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
        # Folder plus file stem: manifest storage adds a hash before the
        # extension (appointments_list.<hash>.js)
        for name in (
            "js/admin/appointments_list",
            "css/admin/appointments_list",
            "css/admin/appointment_detail",
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


def _book_through_the_steps(client, pet, day, time, reason="", service="none"):
    """The owner booking in its three steps (Slice 8b-6): step 1 (pet,
    service, note), the day and time on step 2, Send on step 3. Stops at the
    first step that answers with a page instead of a redirect (an error) and
    returns that response; time is "10:00:00"."""
    response = client.post(
        reverse("owner_book_appointment"),
        {"pet": pet.pk, "service": service, "reason": reason},
    )
    if response.status_code == 302:
        response = client.post(
            reverse("owner_book_time"), {"date": day.isoformat(), "time": time}
        )
    if response.status_code == 302:
        response = client.post(reverse("owner_book_check"))
    return response


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


class OwnerSameDayCutoffTests(FrozenNowTestBase):
    """The default (owner) rules for today: no slots once it is 15:00 (the
    cutoff) or later, otherwise only slots that haven't started yet.
    "Now" is Mon, Nov 2, 9:30 AM unless a test freezes another time."""

    TOMORROW = WALK_IN_DAY + datetime.timedelta(days=1)

    def _assert_is_slot_available_agrees(self, day):
        offered = get_available_slots(day)
        for hour in range(8, 17):
            time = datetime.time(hour, 0)
            with self.subTest(day=day, time=time):
                self.assertEqual(is_slot_available(day, time), time in offered)

    def test_morning_offers_slots_that_have_not_started(self):
        self.assertEqual(
            self._hours(get_available_slots(WALK_IN_DAY)), list(range(10, 17))
        )
        self._assert_is_slot_available_agrees(WALK_IN_DAY)

    def test_a_minute_before_the_cutoff_offers_only_later_slots(self):
        self.freeze(_manila(14, 59))
        self.assertEqual(self._hours(get_available_slots(WALK_IN_DAY)), [15, 16])
        self._assert_is_slot_available_agrees(WALK_IN_DAY)

    def test_at_and_after_the_cutoff_offers_none_today(self):
        for now in (_manila(15, 0), _manila(16, 30)):
            with self.subTest(now=now):
                self.freeze(now)
                self.assertEqual(get_available_slots(WALK_IN_DAY), [])
                self._assert_is_slot_available_agrees(WALK_IN_DAY)

    def test_tomorrow_after_the_cutoff_offers_the_full_day(self):
        self.freeze(_manila(16, 30))
        self.assertEqual(
            self._hours(get_available_slots(self.TOMORROW)), list(range(8, 17))
        )
        self._assert_is_slot_available_agrees(self.TOMORROW)

    def test_just_after_midnight_uses_the_manila_date(self):
        # 00:30 Manila on Nov 2 is still Nov 1 in UTC
        self.freeze(_manila(0, 30))
        self.assertEqual(
            self._hours(get_available_slots(WALK_IN_DAY)), list(range(8, 17))
        )
        last_day = WALK_IN_DAY + datetime.timedelta(days=30)
        for day, hours in (
            (WALK_IN_DAY - datetime.timedelta(days=1), []),
            (last_day, list(range(8, 17))),
            (last_day + datetime.timedelta(days=1), []),
        ):
            with self.subTest(day=day):
                self.assertEqual(self._hours(get_available_slots(day)), hours)
                self._assert_is_slot_available_agrees(day)

    def _book(self, hour):
        # Slice 8b-6: the booking is three steps now (was one POST)
        self.client.force_login(self.owner.user)
        return _book_through_the_steps(
            self.client,
            self.pet,
            WALK_IN_DAY,
            datetime.time(hour, 0).isoformat(),
        )

    def test_owner_can_book_a_slot_that_has_not_started(self):
        self._book(10)
        self.assertEqual(Appointment.objects.get().time, datetime.time(10, 0))

    def test_owner_booking_a_started_slot_is_refused(self):
        response = self._book(9)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Appointment.objects.exists())

    def test_owner_booking_after_the_cutoff_is_refused(self):
        self.freeze(_manila(15, 0))
        response = self._book(16)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Appointment.objects.exists())


class MoveDialogSlotTests(FrozenNowTestBase):
    """The admin Move dialog rules for today: no same-day cutoff, only slots
    whose start is after now. "Now" is Mon, Nov 2, 9:30 AM unless a test
    freezes another time."""

    def _assert_is_slot_available_agrees(self, day):
        offered = get_available_slots(day, move=True)
        for hour in range(8, 17):
            time = datetime.time(hour, 0)
            with self.subTest(day=day, time=time):
                self.assertEqual(
                    is_slot_available(day, time, move=True), time in offered
                )

    def test_offers_slots_that_have_not_started(self):
        for now, hours in (
            (_manila(9, 30), list(range(10, 17))),
            (_manila(15, 30), [16]),
            (_manila(16, 30), []),
        ):
            with self.subTest(now=now):
                self.freeze(now)
                slots = get_available_slots(WALK_IN_DAY, move=True)
                self.assertEqual(self._hours(slots), hours)
                self._assert_is_slot_available_agrees(WALK_IN_DAY)

    def test_future_date_offers_the_full_day(self):
        self.freeze(_manila(16, 30))
        day = WALK_IN_DAY + datetime.timedelta(days=1)
        self.assertEqual(
            self._hours(get_available_slots(day, move=True)), list(range(8, 17))
        )

    def test_slot_view_uses_the_move_rules(self):
        self.freeze(_manila(15, 30))
        response = self.client.get(
            reverse("admin_get_slots_for_date"),
            {"date": WALK_IN_DAY.isoformat(), "as": "buttons"},
        )
        self.assertContains(response, 'type="radio" name="time" value="16:00:00"')
        self.assertNotContains(response, '"15:00:00"')

    def _move(self, original, hour):
        return self._post(
            "admin_appointment_reschedule",
            original,
            {
                "date": WALK_IN_DAY.isoformat(),
                "time": datetime.time(hour, 0).isoformat(),
                "reschedule_reason": "The vet is away that day.",
            },
        )

    def test_move_after_the_owner_cutoff_is_allowed(self):
        self.freeze(_manila(15, 30))
        original = self._appointment(Appointment.PENDING)
        self._move(original, 16)
        new = Appointment.objects.get(rescheduled_from=original)
        self.assertEqual((new.date, new.time), (WALK_IN_DAY, datetime.time(16, 0)))

    def test_move_to_a_started_slot_is_refused(self):
        original = self._appointment(Appointment.PENDING)
        self._move(original, 9)
        self._assert_status(original, Appointment.PENDING)
        self.assertFalse(Appointment.objects.filter(rescheduled_from=original).exists())


class SlotViewJustAfterMidnightTests(SlotViewTests):
    """Every SlotViewTests test again at 00:30 Manila, while the UTC date is
    still the previous day."""

    def setUp(self):
        patcher = mock.patch("django.utils.timezone.now", return_value=_manila(0, 30))
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()


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

    def test_default_uses_the_move_rule(self):
        """Without ?for=walk_in: the Move rule, so at 9:30 the started 9:00
        slot is left out and 16:00 is offered."""
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
        # 8a-2b: compact message; the full sentence is now the email body
        self.assertEqual(
            notification.message,
            "Bantay · Mon, Nov 2 at 10:00 AM. The time is confirmed.",
        )
        self.assertEqual(
            mail.outbox[0].body,
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

    def test_empty_q_lists_every_owner(self):
        """No search: the full owner list (since the 3c-3 follow-up), with
        the New owner banner still there."""
        for q in ("", "   "):
            with self.subTest(q=q):
                response = self.client.get(reverse(self.STEP_1), {"q": q})
                self.assertEqual(list(response.context["owners"]), [self.owner])
                self.assertContains(response, 'id="results-title"')
                self.assertContains(response, "All pet owners (1)")
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
        self.assertContains(
            response, "Been to the clinic before? Find the owner below</label>"
        )
        self.assertNotContains(response, "Been here before?")
        self.assertNotContains(response, "The list gets shorter as you type.")
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
            response,
            f'href="{reverse("admin_pet_add", args=[owner.pk])}?return=walkin"',
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
            '<b class="font-semibold">10:00 AM was just taken.</b> '
            "Someone else booked it a moment ago. Please pick another time. "
            "Everything else you entered is kept.",
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
        self.assertEqual(response.context["taken_time"], "")


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


# ─── Walk-in design pass (Slice 3c-3) ─────────────────────────────────────────


class SlotInProgressTests(FrozenNowTestBase):
    """slot_in_progress: the slot with start <= now < end, today only.
    Clinic: 8:00-17:00, hourly slots; "now" is Mon, Nov 2, 9:30 AM."""

    def test_slot_in_progress(self):
        self.assertEqual(slot_in_progress(WALK_IN_DAY), datetime.time(9, 0))

    def test_a_slot_is_in_progress_from_its_start(self):
        self.freeze(_manila(10, 0))
        self.assertEqual(slot_in_progress(WALK_IN_DAY), datetime.time(10, 0))

    def test_not_today(self):
        for day in (
            WALK_IN_DAY - datetime.timedelta(days=1),
            WALK_IN_DAY + datetime.timedelta(days=1),
        ):
            with self.subTest(day=day):
                self.assertIsNone(slot_in_progress(day))

    def test_before_opening(self):
        self.freeze(_manila(7, 59))
        self.assertIsNone(slot_in_progress(WALK_IN_DAY))

    def test_after_closing(self):
        for now in (_manila(17, 0), _manila(17, 30)):
            with self.subTest(now=now):
                self.freeze(now)
                self.assertIsNone(slot_in_progress(WALK_IN_DAY))

    def test_follows_the_clinic_slots(self):
        """90-minute slots start at 8:00, 9:30, 11:00…, so at 10:00 the
        slot in progress is 9:30 (the same slots get_available_slots uses)."""
        clinic = ClinicSettings.objects.get()
        clinic.slot_duration_minutes = 90
        clinic.save()
        self.freeze(_manila(10, 0))
        self.assertEqual(slot_in_progress(WALK_IN_DAY), datetime.time(9, 30))
        self.assertIn(
            datetime.time(9, 30), get_available_slots(WALK_IN_DAY, walk_in=True)
        )

    def test_no_slot_in_progress_in_a_gap_at_closing(self):
        """Closing at 16:30 with hourly slots: the last slot is 15:00-16:00,
        so from 16:00 to 16:30 no slot is in progress."""
        clinic = ClinicSettings.objects.get()
        clinic.closing_time = datetime.time(16, 30)
        clinic.save()
        self.freeze(_manila(16, 15))
        self.assertIsNone(slot_in_progress(WALK_IN_DAY))

    def test_no_clinic_settings(self):
        ClinicSettings.objects.all().delete()
        self.assertIsNone(slot_in_progress(WALK_IN_DAY))


class NowLabelTests(WalkInFlowTestBase):
    """The "Now" label on the slot in progress: walk-in step 2 and the
    walk-in slot view only, never the Move dialog. "Now" is 9:30 AM."""

    NOW_LABEL = re.compile(r'<span class="[^"]*">Now</span>')

    def _slots(self, **params):
        params.setdefault("date", WALK_IN_DAY.isoformat())
        params.setdefault("as", "buttons")
        return self.client.get(reverse("admin_get_slots_for_date"), params)

    def _labelled_now(self, response):
        """The time values whose button carries the "Now" label."""
        buttons = re.findall(
            r'value="(\d\d:\d\d:\d\d)"(.*?)</label>', response.content.decode(), re.S
        )
        return [value for value, rest in buttons if self.NOW_LABEL.search(rest)]

    def test_visit_step_labels_the_slot_in_progress(self):
        response = self._visit(owner=self.owner.pk)
        self.assertEqual(response.context["now_slot"], datetime.time(9, 0))
        self.assertEqual(self._labelled_now(response), ["09:00:00"])
        self.assertEqual(len(self.NOW_LABEL.findall(response.content.decode())), 1)

    def test_walk_in_slot_view_labels_the_slot_in_progress(self):
        response = self._slots(**{"for": "walk_in"})
        self.assertEqual(response.context["now_slot"], datetime.time(9, 0))
        self.assertEqual(self._labelled_now(response), ["09:00:00"])

    def test_walk_in_slot_view_other_day_has_no_label(self):
        day = WALK_IN_DAY + datetime.timedelta(days=1)
        response = self._slots(**{"for": "walk_in", "date": day.isoformat()})
        self.assertIsNone(response.context["now_slot"])
        self.assertContains(response, 'value="09:00:00"')
        self.assertNotRegex(response.content.decode(), self.NOW_LABEL)

    def test_move_slot_view_has_no_label(self):
        """Without ?for=walk_in (the Move dialog) there is no "Now", even at
        a time when the walk-in view shows one."""
        self.freeze(_manila(9, 0))
        response = self._slots()
        self.assertIsNone(response.context["now_slot"])
        self.assertContains(response, 'value="10:00:00"')
        self.assertNotRegex(response.content.decode(), self.NOW_LABEL)
        walk_in = self._slots(**{"for": "walk_in"})
        self.assertEqual(self._labelled_now(walk_in), ["09:00:00"])

    def test_move_dialog_after_an_error_has_no_label(self):
        """The Move dialog reopens with today's free times after a form
        error; they never carry the label."""
        appointment = self._appointment(Appointment.CONFIRMED)
        response = self.client.post(
            reverse("admin_appointment_reschedule", args=[appointment.pk]),
            {"date": WALK_IN_DAY.isoformat(), "time": "", "reschedule_reason": ""},
            follow=True,
        )
        self.assertContains(response, 'id="move-slots"')
        self.assertContains(response, 'value="10:00:00"')
        self.assertNotRegex(response.content.decode(), self.NOW_LABEL)

    def test_booked_slot_in_progress_puts_the_label_nowhere(self):
        """The 9:00 slot in progress is already booked, so it is not offered,
        and no other slot gets the label."""
        other = self._owner("Ben", "Reyes", pets=["Max"])
        Appointment.objects.create(
            owner=other,
            pet=other.pets.get(),
            date=WALK_IN_DAY,
            time=datetime.time(9, 0),
            status=Appointment.CONFIRMED,
        )
        for response in (
            self._visit(owner=self.owner.pk),
            self._slots(**{"for": "walk_in"}),
        ):
            with self.subTest(url=response.wsgi_request.path):
                self.assertNotContains(response, 'value="09:00:00"')
                self.assertContains(response, 'value="10:00:00"')
                self.assertNotRegex(response.content.decode(), self.NOW_LABEL)


class WalkInSidebarTests(WalkInFlowTestBase):
    """Every walk-in page highlights Appointments in the sidebar, and only
    Appointments (the New owner page used to highlight Pet owners)."""

    def _current_nav(self, response):
        html = response.content.decode()
        return re.findall(
            r'aria-current="page">.*?<span class="flex-1">([^<]+)</span>', html, re.S
        )

    def test_walk_in_pages_highlight_appointments(self):
        pages = (
            reverse(self.STEP_1),
            f"{reverse(self.VISIT)}?owner={self.owner.pk}",
            reverse("admin_walkin_create"),
        )
        for url in pages:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(self._current_nav(response), ["Appointments"])

    def test_owner_pages_still_highlight_pet_owners(self):
        response = self.client.get(reverse("admin_owner_list"))
        self.assertEqual(self._current_nav(response), ["Pet owners"])


# ─── Walk-in step 1 owner list (3c-3 follow-up) ───────────────────────────────


class PagingLinksTests(SimpleTestCase):
    """paging_links: the pager's Previous/Next links."""

    def _page(self, number):
        return Paginator(list(range(60)), 25).get_page(number)

    def test_empty_page_query_gives_plain_page_links(self):
        self.assertEqual(
            paging_links(self._page(2), ""),
            {"prev_page_query": "?page=1", "next_page_query": "?page=3"},
        )

    def test_page_query_comes_first(self):
        self.assertEqual(
            paging_links(self._page(2), "status=all"),
            {
                "prev_page_query": "?status=all&page=1",
                "next_page_query": "?status=all&page=3",
            },
        )

    def test_no_link_past_either_end(self):
        self.assertEqual(paging_links(self._page(1), "")["prev_page_query"], "")
        self.assertEqual(paging_links(self._page(3), "")["next_page_query"], "")


class WalkInOwnerListTests(WalkInFlowTestBase):
    """Step 1 with no search: every non-archived owner, first name A to Z,
    25 per page. Ana Cruz (with Bantay) exists from setUp."""

    def _get(self, **params):
        return self.client.get(reverse(self.STEP_1), params)

    def _names(self, response):
        return [owner.full_name for owner in response.context["owners"]]

    def test_listed_by_first_name_without_archived(self):
        self._owner("Zed", "Abad")
        self._owner("Ben", "Reyes")
        self._owner("Ana", "Abad")  # same first name: last name decides
        self._owner("Carla", "Bautista", archived=True)
        response = self._get()
        self.assertEqual(
            self._names(response), ["Ana Abad", "Ana Cruz", "Ben Reyes", "Zed Abad"]
        )
        self.assertNotContains(response, "Carla")
        self.assertContains(response, "All pet owners (4)</h2>")
        self.assertContains(response, "A to Z by first name")

    def test_25_per_page_and_the_rest_on_page_2(self):
        for i in range(30):
            self._owner(f"Owner{i:02}", "Santos")  # 31 owners with Ana Cruz
        first = self._get()
        self.assertEqual(len(first.context["owners"]), 25)
        self.assertEqual(self._names(first)[0], "Ana Cruz")
        self.assertContains(first, "All pet owners (31)")
        self.assertContains(first, "Showing 1–25 of 31")
        self.assertContains(first, 'href="?page=2"')
        second = self._get(page="2")
        self.assertEqual(
            self._names(second), [f"Owner{i:02} Santos" for i in range(24, 30)]
        )
        self.assertContains(second, "Showing 26–31 of 31")
        self.assertContains(second, 'href="?page=1"')

    def test_bad_page_falls_back(self):
        for i in range(30):
            self._owner(f"Owner{i:02}", "Santos")
        for page, expected in (("abc", 1), ("0", 2), ("99", 2), ("-1", 2)):
            with self.subTest(page=page):
                response = self._get(page=page)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["page_obj"].number, expected)

    def test_owner_with_no_pets_still_listed(self):
        self._owner("Liza", "Reyes")
        response = self._get()
        self.assertIn("Liza Reyes", self._names(response))
        self.assertContains(response, "No pets yet")

    def test_cards_show_pets_phone_and_choose(self):
        self._owner("Liza", "Reyes", phone="0917 123 4567", pets=["Brownie"])
        response = self._get()
        self.assertContains(response, "0917 123 4567")
        self.assertContains(response, "No phone number")  # Ana Cruz
        self.assertContains(response, "Brownie</li>")
        self.assertContains(response, f'{reverse(self.VISIT)}?owner={self.owner.pk}"')

    def test_query_count_flat_as_owners_grow(self):
        for i in range(3):
            self._owner(f"A{i}", "Santos", pets=["Max", "Muning"])
        with CaptureQueriesContext(connection) as few:
            self._get()
        for i in range(20):
            self._owner(f"B{i}", "Santos", pets=["Max", "Muning"])
        with self.assertNumQueries(len(few.captured_queries)):
            self._get()

    def test_no_owners_at_all(self):
        PetOwner.objects.all().delete()
        response = self._get()
        self.assertContains(response, "All pet owners (0)</h2>")
        self.assertContains(response, "No pet owners yet")
        self.assertContains(response, "Use New owner above to add the first one.")
        self.assertNotContains(response, 'aria-label="Pages"')


class WalkInStepOneSearchPageTests(WalkInFlowTestBase):
    """Step 1 while searching: first-name order, no pager, empty state,
    Clear search, live regions."""

    def test_results_sorted_by_first_name(self):
        self._owner("Zed", "Acruz")
        self._owner("Ben", "Cruzado")
        response = self.client.get(reverse(self.STEP_1), {"q": "cruz"})
        self.assertEqual(
            [owner.full_name for owner in response.context["owners"]],
            ["Ana Cruz", "Ben Cruzado", "Zed Acruz"],
        )
        self.assertContains(response, "3 owners match “cruz”")

    def test_no_pager_while_searching(self):
        for i in range(30):
            self._owner(f"Owner{i:02}", "Santos")
        response = self.client.get(reverse(self.STEP_1), {"q": "santos"})
        self.assertIsNone(response.context["page_obj"])
        self.assertNotContains(response, 'aria-label="Pages"')
        self.assertNotContains(response, "All pet owners")
        self.assertContains(response, "Showing the first 20.")

    def test_no_match_empty_state(self):
        response = self.client.get(reverse(self.STEP_1), {"q": "zzz"})
        self.assertContains(
            response,
            '<p class="text-lg font-semibold text-gray-900" aria-live="polite">'
            "No owner matches “zzz”</p>",
        )
        self.assertContains(
            response,
            "Check the spelling, or use New owner above if this is their first visit.",
        )

    def test_clear_search_only_with_q(self):
        clear = (
            f'<a href="{reverse(self.STEP_1)}" class="inline-flex min-h-11 '
            "items-center gap-2 text-base font-medium text-blue-600 underline "
            'underline-offset-2">'
        )
        with_q = self.client.get(reverse(self.STEP_1), {"q": "ana"})
        self.assertContains(with_q, clear)
        self.assertContains(with_q, "Clear search</a>")
        for params in ({}, {"q": "  "}):
            with self.subTest(params=params):
                response = self.client.get(reverse(self.STEP_1), params)
                self.assertNotContains(response, "Clear search")

    def test_live_regions_are_the_heading_and_match_line_only(self):
        for params in ({}, {"q": "ana"}):
            with self.subTest(params=params):
                html = self.client.get(reverse(self.STEP_1), params).content.decode()
                self.assertEqual(html.count('aria-live="polite"'), 1)
                self.assertRegex(html, r'<h2 id="results-title"[^>]*aria-live="polite"')
                self.assertNotRegex(html, r'id="owner-results"[^>]*aria-live')


class WalkInStepOneHtmxTests(WalkInFlowTestBase):
    """Filter as you type: the HTMX request for #owner-results gets only
    the partial; everything else gets the full page."""

    PARTIAL = "admin/appointments/_walkin_owner_results.html"
    SWAP = {"HTTP_HX_REQUEST": "true", "HTTP_HX_TARGET": "owner-results"}

    def _get(self, params=None, **headers):
        return self.client.get(reverse(self.STEP_1), params or {"q": "ana"}, **headers)

    def _assert_full_page(self, response):
        self.assertTemplateUsed(response, "layouts/admin.html")
        self.assertTemplateUsed(response, self.PARTIAL)
        self.assertContains(response, 'aria-label="Admin"')  # sidebar nav
        self.assertContains(response, 'id="walkin-q"')

    def test_swap_request_gets_only_the_partial(self):
        for params in ({"q": "ana"}, {"q": ""}):
            with self.subTest(params=params):
                response = self._get(params, **self.SWAP)
                self.assertTemplateUsed(response, self.PARTIAL)
                self.assertTemplateNotUsed(response, "layouts/admin.html")
                self.assertTemplateNotUsed(
                    response, "admin/appointments/walkin_find.html"
                )
                self.assertNotContains(response, 'aria-label="Admin"')
                # The input is outside the swap target, so typing keeps focus
                self.assertNotContains(response, 'id="walkin-q"')
                self.assertContains(response, "Ana Cruz")

    def test_history_restore_gets_the_full_page(self):
        response = self._get(**self.SWAP, HTTP_HX_HISTORY_RESTORE_REQUEST="true")
        self._assert_full_page(response)

    def test_other_htmx_target_gets_the_full_page(self):
        response = self._get(HTTP_HX_REQUEST="true", HTTP_HX_TARGET="main")
        self._assert_full_page(response)

    def test_no_htmx_gets_the_full_page(self):
        self._assert_full_page(self._get())

    def test_response_varies_on_hx_request(self):
        for headers in ({}, self.SWAP):
            with self.subTest(headers=headers):
                self.assertIn("HX-Request", self._get(**headers)["Vary"])

    def test_search_box_filters_as_you_type(self):
        response = self._get()
        self.assertContains(
            response,
            f'hx-get="{reverse(self.STEP_1)}" '
            'hx-trigger="input changed delay:500ms, search" '
            'hx-target="#owner-results" hx-replace-url="true"',
        )
        self.assertNotContains(response, "hx-push-url")
        self.assertContains(response, '<div id="owner-results">')

    def test_results_sit_inside_the_centered_column(self):
        """The partial has no width of its own: after a swap the list is
        still inside the page's centered column."""
        html = self._get().content.decode()
        self.assertLess(
            html.index('id="page-column"'), html.index('id="owner-results"')
        )
        partial = self._get(**self.SWAP).content.decode()
        self.assertNotRegex(partial, r"mx-auto|max-w-(?:xl|2xl|3xl|4xl|5xl|6xl|7xl)")


class WalkInCenteredColumnTests(WalkInFlowTestBase):
    """Walk-in steps 1 to 3: the whole page (back link, h1, steps, cards) is
    one centered column (mx-auto + the page's max width)."""

    def _assert_column(self, response, width):
        html = response.content.decode()
        match = re.search(r'<div id="page-column" class="([^"]*)">', html)
        self.assertIsNotNone(match)
        classes = match.group(1).split()
        self.assertIn("mx-auto", classes)
        self.assertIn(width, classes)
        self.assertGreater(html.index("<h1"), match.end())
        self.assertEqual(html.count("<h1"), 1)

    def test_step_1(self):
        self._assert_column(self.client.get(reverse(self.STEP_1)), "max-w-3xl")

    def test_step_2(self):
        response = self._visit(owner=self.owner.pk)
        self._assert_column(response, "max-w-4xl")

    def test_step_3(self):
        self._assert_column(self._post_visit(), "max-w-3xl")


# ─── Appointments list v4: period, top-bar search, day cards ─────────────────

D = datetime.date
# Monday, November 2, 2026: "today" in the period tests below.
PERIOD_TODAY = D(2026, 11, 2)


class ClinicSettingsHelperTests(AppointmentTestBase):
    """get_clinic_settings(): lowest pk, never pk=1; only create=True creates."""

    def setUp(self):
        super().setUp()
        # Start from no row; each test adds what it needs
        ClinicSettings.objects.all().delete()

    def _row(self, pk, name="Row"):
        return ClinicSettings.objects.create(
            pk=pk,
            clinic_name=name,
            address="Gasan",
            opening_time=datetime.time(8, 0),
            closing_time=datetime.time(17, 0),
            same_day_cutoff_time=datetime.time(15, 0),
            working_days=[0, 1],
        )

    def test_no_row_without_create_returns_none_and_makes_nothing(self):
        self.assertIsNone(get_clinic_settings())
        self.assertEqual(ClinicSettings.objects.count(), 0)

    def test_no_row_with_create_makes_one_row_with_the_defaults(self):
        row = get_clinic_settings(create=True)
        self.assertEqual(ClinicSettings.objects.count(), 1)
        self.assertEqual(row.clinic_name, CLINIC_SETTINGS_DEFAULTS["clinic_name"])
        self.assertEqual(row.address, "Bognuyan, Gasan, Marinduque")
        self.assertEqual(row.opening_time, datetime.time(8, 0))
        self.assertEqual(row.closing_time, datetime.time(17, 0))
        self.assertEqual(row.same_day_cutoff_time, datetime.time(15, 0))
        self.assertEqual(row.working_days, [0, 1, 2, 3, 4])

    def test_a_row_at_pk_5_is_returned_and_nothing_is_created(self):
        self._row(5, "Five")
        row = get_clinic_settings(create=True)
        self.assertEqual(row.pk, 5)
        self.assertEqual(ClinicSettings.objects.count(), 1)

    def test_two_rows_return_the_lowest_pk(self):
        self._row(7, "Seven")
        self._row(3, "Three")
        self.assertEqual(get_clinic_settings().pk, 3)
        self.assertEqual(get_clinic_settings(create=True).pk, 3)

    def test_settings_page_with_no_row_makes_exactly_one(self):
        response = self.client.get(reverse("admin_clinic_settings"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ClinicSettings.objects.count(), 1)

    def test_settings_page_shows_the_row_at_pk_5(self):
        self._row(5, "Pk Five Clinic")
        response = self.client.get(reverse("admin_clinic_settings"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ClinicSettings.objects.count(), 1)
        self.assertContains(response, "Pk Five Clinic")


class BlockedDateTests(AppointmentTestBase):
    """Blocked dates tab (Slice 7a-4): busy-day warning, panel-only responses,
    HX-Redirect with ?tab=blocked on success."""

    HTMX = {"HTTP_HX_REQUEST": "true"}

    def setUp(self):
        super().setUp()
        self.day = self.today + datetime.timedelta(days=3)
        self.add_url = reverse("admin_blocked_date_add")
        self.settings_url = reverse("admin_clinic_settings")
        self.tab_url = f"{self.settings_url}?tab=blocked"

    def _add(self, htmx=True, **extra):
        data = {"date": self.day.isoformat(), "reason": "Training", **extra}
        return self.client.post(self.add_url, data, **(self.HTMX if htmx else {}))

    def _visit(self, status, hour=9, day=None, pet=None):
        return Appointment.objects.create(
            owner=self.owner,
            pet=pet or self.pet,
            date=day or self.day,
            time=datetime.time(hour, 0),
            status=status,
        )

    def _assert_panel_only(self, response):
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "admin/settings/_blocked_dates_panel.html")
        self.assertTemplateNotUsed(response, "layouts/admin.html")
        self.assertNotContains(response, "<html")

    # ─── Busy-day warning ─────────────────────────────────────────────────────

    def test_day_without_visits_saves_on_the_first_submit(self):
        response = self._add()
        self.assertTrue(BlockedDate.objects.filter(date=self.day).exists())
        self.assertEqual(response.status_code, 200)
        self.assertIn("HX-Redirect", response.headers)

    def test_confirmed_visit_warns_then_saves_on_confirm(self):
        visit = self._visit(Appointment.CONFIRMED)
        response = self._add()
        self._assert_panel_only(response)
        self.assertEqual(BlockedDate.objects.count(), 0)
        day = date_format(self.day, "D, M j")
        self.assertContains(response, f"{day} already has 1 visit.")
        self.assertContains(response, "9:00 AM · Bantay (Ana Cruz)")
        self.assertContains(
            response, "They stay booked. Blocking the day only stops new bookings."
        )
        self.assertContains(response, "Go back")
        self.assertContains(response, "Block anyway")
        # The typed date and reason are kept; the confirm flag is in the form
        self.assertContains(response, f'value="{self.day.isoformat()}"')
        self.assertContains(response, 'value="Training"')
        self.assertContains(response, 'name="confirm_busy" value="1"')

        response = self._add(confirm_busy="1")
        self.assertTrue(BlockedDate.objects.filter(date=self.day).exists())
        self.assertEqual(response.headers["HX-Redirect"], self.tab_url)
        before = (visit.status, visit.date, visit.time)
        visit.refresh_from_db()
        self.assertEqual((visit.status, visit.date, visit.time), before)
        self.assertEqual(Appointment.objects.count(), 1)

    def test_warning_lists_visits_by_time_with_pet_and_owner(self):
        other_owner = PetOwner.objects.create(
            user=User.objects.create_user(
                username="owner2",
                email="owner2@example.com",
                password="unused-pass-123",
                role=User.PET_OWNER,
            ),
            first_name="Liza",
            last_name="Reyes",
        )
        brownie = Pet.objects.create(owner=other_owner, name="Brownie", species="Dog")
        self._visit(Appointment.PENDING, hour=14)
        Appointment.objects.create(
            owner=other_owner,
            pet=brownie,
            date=self.day,
            time=datetime.time(10, 0),
            status=Appointment.CONFIRMED,
        )
        response = self._add()
        self.assertContains(response, "already has 2 visits.")
        html = response.content.decode()
        first = html.index("10:00 AM · Brownie (Liza Reyes)")
        second = html.index("2:00 PM · Bantay (Ana Cruz)")
        self.assertLess(first, second)

    def test_pending_visit_triggers_the_warning(self):
        self._visit(Appointment.PENDING)
        response = self._add()
        self.assertContains(response, "Block anyway")
        self.assertEqual(BlockedDate.objects.count(), 0)

    def test_other_statuses_do_not_trigger_the_warning(self):
        for hour, status in enumerate(
            (Appointment.CANCELLED, Appointment.COMPLETED, Appointment.NO_SHOW), 9
        ):
            self._visit(status, hour=hour)
        response = self._add()
        self.assertIn("HX-Redirect", response.headers)
        self.assertTrue(BlockedDate.objects.filter(date=self.day).exists())

    def test_visit_on_another_day_does_not_trigger_the_warning(self):
        self._visit(Appointment.CONFIRMED, day=self.day + datetime.timedelta(days=1))
        response = self._add()
        self.assertIn("HX-Redirect", response.headers)
        self.assertTrue(BlockedDate.objects.filter(date=self.day).exists())

    def test_go_back_returns_the_panel_without_the_warning(self):
        self._visit(Appointment.CONFIRMED)
        response = self.client.get(
            self.add_url,
            {"date": self.day.isoformat(), "reason": "Training", "confirm_busy": "1"},
            **self.HTMX,
        )
        self._assert_panel_only(response)
        self.assertNotContains(response, "Block anyway")
        self.assertNotContains(response, "already has")
        self.assertContains(response, 'value="Training"')
        self.assertContains(response, f'value="{self.day.isoformat()}"')
        self.assertEqual(BlockedDate.objects.count(), 0)

    # ─── Panel-only errors ────────────────────────────────────────────────────

    def test_duplicate_date_returns_the_panel_only_with_the_error(self):
        BlockedDate.objects.create(date=self.day, reason="Fiesta")
        response = self._add()
        self._assert_panel_only(response)
        self.assertContains(
            response,
            f"{date_format(self.day, 'D, M j')} is already blocked. "
            "Pick another date.",
        )
        self.assertEqual(BlockedDate.objects.count(), 1)

    def test_past_date_returns_the_panel_only(self):
        response = self._add(date=(self.today - datetime.timedelta(days=1)).isoformat())
        self._assert_panel_only(response)
        self.assertContains(response, "Cannot block a date in the past.")
        self.assertEqual(BlockedDate.objects.count(), 0)

    # ─── Success: HX-Redirect and the flash ───────────────────────────────────

    def test_htmx_add_redirects_to_the_blocked_tab_and_flashes(self):
        response = self._add()
        self.assertEqual(response.headers["HX-Redirect"], self.tab_url)
        page = self.client.get(self.tab_url)
        self.assertContains(
            page, f"{date_format(self.day, 'D, M j, Y')} is now blocked."
        )

    def test_htmx_remove_redirects_to_the_blocked_tab_and_flashes(self):
        blocked = BlockedDate.objects.create(date=self.day)
        url = reverse("admin_blocked_date_delete", args=[blocked.pk])
        response = self.client.post(url, **self.HTMX)
        self.assertEqual(response.headers["HX-Redirect"], self.tab_url)
        self.assertFalse(BlockedDate.objects.exists())  # hard delete
        page = self.client.get(self.tab_url)
        self.assertContains(
            page, f"{date_format(self.day, 'D, M j, Y')} is open again."
        )

    def test_non_htmx_add_and_remove_use_a_normal_redirect(self):
        response = self._add(htmx=False)
        self.assertRedirects(response, self.tab_url, fetch_redirect_response=False)
        self.assertNotIn("HX-Redirect", response.headers)
        blocked = BlockedDate.objects.get(date=self.day)
        url = reverse("admin_blocked_date_delete", args=[blocked.pk])
        response = self.client.post(url)
        self.assertRedirects(response, self.tab_url, fetch_redirect_response=False)
        self.assertFalse(BlockedDate.objects.exists())

    def test_settings_page_includes_the_panel(self):
        BlockedDate.objects.create(date=self.day, reason="Fiesta")
        response = self.client.get(self.tab_url)
        self.assertContains(response, 'id="blocked-dates-panel"')
        self.assertContains(response, "Fiesta")

    # ─── Access ───────────────────────────────────────────────────────────────

    def test_owner_and_logged_out_users_cannot_reach_the_views(self):
        blocked = BlockedDate.objects.create(date=self.day)
        delete_url = reverse("admin_blocked_date_delete", args=[blocked.pk])
        self.client.force_login(self.owner.user)
        for url in (self.add_url, delete_url):
            with self.subTest(user="owner", url=url):
                response = self.client.post(url, {"date": "2030-01-01"})
                self.assertRedirects(
                    response, reverse("owner_dashboard"), fetch_redirect_response=False
                )
        self.client.logout()
        for url in (self.add_url, delete_url):
            with self.subTest(user="logged out", url=url):
                response = self.client.post(url, {"date": "2030-01-01"})
                self.assertEqual(response.status_code, 302)
                self.assertIn("login", response["Location"])
        self.assertEqual(BlockedDate.objects.count(), 1)


class BlockedPanelMarkupTests(AppointmentTestBase):
    """Blocked dates tab on compiled Tailwind (Slice 7b-3): heading with the
    count, one remove dialog per row, no old handlers or stylesheet."""

    HTMX = {"HTTP_HX_REQUEST": "true"}

    def setUp(self):
        super().setUp()
        self.day = self.today + datetime.timedelta(days=3)
        self.add_url = reverse("admin_blocked_date_add")
        self.tab_url = f"{reverse('admin_clinic_settings')}?tab=blocked"

    def _panel(self, response):
        """The panel's HTML, from its wrapper to the end of <main> (the admin
        layout's own modal markup comes after <main>)."""
        return (
            response.content.decode()
            .split('id="blocked-dates-panel"')[1]
            .split("</main>")[0]
        )

    def _two_blocks(self):
        first = BlockedDate.objects.create(date=self.day, reason="Fiesta")
        second = BlockedDate.objects.create(
            date=self.day + datetime.timedelta(days=5), reason="Training"
        )
        return first, second

    def test_tab_shows_the_heading_with_the_count_and_each_row(self):
        first, second = self._two_blocks()
        response = self.client.get(self.tab_url)
        self.assertContains(response, "Blocked dates (2)")
        self.assertContains(
            response, "The clinic is closed on these days. Owners can't book them."
        )
        for blocked in (first, second):
            with self.subTest(date=blocked.date):
                self.assertContains(response, date_format(blocked.date, "D, M j, Y"))
                self.assertContains(response, blocked.reason)
                self.assertContains(
                    response, f'data-dialog-open="remove-blocked-dialog-{blocked.pk}"'
                )

    def test_empty_state_in_plain_words(self):
        response = self.client.get(self.tab_url)
        self.assertContains(response, "Blocked dates (0)")
        self.assertContains(response, "No dates are blocked.")
        self.assertNotContains(response, "<dialog")

    def test_one_remove_dialog_per_row_with_the_right_url_and_wording(self):
        blocks = self._two_blocks()
        html = self.client.get(self.tab_url).content.decode()
        self.assertEqual(html.count("<dialog"), 2)
        for blocked in blocks:
            with self.subTest(date=blocked.date):
                dialog = html.split(f'id="remove-blocked-dialog-{blocked.pk}"')[
                    1
                ].split("</dialog>")[0]
                label = date_format(blocked.date, "D, M j, Y")
                url = reverse("admin_blocked_date_delete", args=[blocked.pk])
                self.assertIn(f'action="{url}"', dialog)
                self.assertIn('method="post"', dialog)
                self.assertIn("csrfmiddlewaretoken", dialog)
                self.assertIn(f"Remove the block on {label}?", dialog)
                self.assertIn("Owners will be able to book this day again.", dialog)
                self.assertIn("Keep it blocked", dialog)
                self.assertIn("Yes, remove", dialog)
                self.assertIn("data-dialog-close", dialog)
                self.assertIn("autofocus", dialog)
                # The row's button names the date for screen readers
                self.assertIn(f'Remove<span class="sr-only"> {label}</span>', html)

    def test_panel_has_no_hx_confirm_and_no_onclick(self):
        self._two_blocks()
        responses = [
            self.client.get(self.tab_url),
            self.client.post(self.add_url, {"date": self.day.isoformat()}, **self.HTMX),
        ]
        for response in responses:
            panel = self._panel(response)
            self.assertNotIn("hx-confirm", panel)
            self.assertNotIn("onclick", panel)
            self.assertNotIn("<!--", panel)

    def test_duplicate_error_is_the_panel_only_with_the_mockup_wording(self):
        BlockedDate.objects.create(date=self.day, reason="Fiesta")
        response = self.client.post(
            self.add_url, {"date": self.day.isoformat(), "reason": "Again"}, **self.HTMX
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "admin/settings/_blocked_dates_panel.html")
        self.assertTemplateNotUsed(response, "layouts/admin.html")
        self.assertNotContains(response, "<html")
        self.assertContains(
            response,
            f"{date_format(self.day, 'D, M j')} is already blocked. Pick another date.",
        )
        self.assertContains(response, 'value="Again"')
        self.assertContains(response, "Block this date")

    def test_busy_day_warning_hides_block_this_date(self):
        Appointment.objects.create(
            owner=self.owner,
            pet=self.pet,
            date=self.day,
            time=datetime.time(9, 0),
            status=Appointment.CONFIRMED,
        )
        response = self.client.post(
            self.add_url,
            {"date": self.day.isoformat(), "reason": "Training"},
            **self.HTMX,
        )
        self.assertNotContains(response, "Block this date")
        self.assertContains(response, "Go back")
        self.assertContains(response, "Block anyway")
        self.assertContains(response, 'name="confirm_busy" value="1"')
        self.assertContains(response, "readonly")
        self.assertContains(response, 'hx-get="' + self.add_url + '"')
        self.assertContains(response, 'hx-target="#blocked-dates-panel"')
        self.assertContains(response, 'hx-swap="outerHTML"')

    def test_normal_panel_shows_block_this_date_and_no_warning_buttons(self):
        response = self.client.get(self.tab_url)
        self.assertContains(response, "Block this date")
        self.assertNotContains(response, "Block anyway")
        self.assertNotContains(response, 'name="confirm_busy"')
        self.assertContains(response, f'hx-post="{self.add_url}"')

    def test_blocked_tab_no_longer_loads_the_old_stylesheet(self):
        self._two_blocks()
        response = self.client.get(self.tab_url)
        self.assertNotContains(response, "css/admin/clinic_settings")
        self.assertNotContains(response, "css/admin/")
        self.assertNotContains(response, "layer(legacy)")

    def test_plain_post_remove_redirects_to_the_tab_and_flashes(self):
        blocked = BlockedDate.objects.create(date=self.day)
        url = reverse("admin_blocked_date_delete", args=[blocked.pk])
        response = self.client.post(url, follow=True)
        self.assertRedirects(response, self.tab_url)
        self.assertFalse(BlockedDate.objects.exists())
        self.assertContains(
            response, f"{date_format(self.day, 'D, M j, Y')} is open again."
        )


class BookingLimitFormTests(AppointmentTestBase):
    """booking_limit_days is 1 to 365 (form only, no model change)."""

    def _errors(self, value):
        from appointments.forms import ClinicSettingsForm

        form = ClinicSettingsForm(
            {"booking_limit_days": value}, instance=get_clinic_settings()
        )
        return form.errors.get("booking_limit_days", [])

    def test_below_one_is_refused(self):
        self.assertEqual(self._errors(0), ["The booking limit must be at least 1 day."])

    def test_one_and_365_are_accepted(self):
        for value in (1, 365):
            with self.subTest(value=value):
                self.assertEqual(self._errors(value), [])

    def test_above_365_is_refused(self):
        self.assertEqual(
            self._errors(366), ["The booking limit can't be more than 365 days."]
        )

    def test_help_text(self):
        from appointments.forms import ClinicSettingsForm

        self.assertEqual(
            ClinicSettingsForm().fields["booking_limit_days"].help_text,
            "Owners can book up to this many days ahead. Between 1 and 365.",
        )


LOGO_MB = 1024 * 1024
TYPE_MESSAGE = "Only JPG, PNG or WebP files can be used."


def _image_bytes(fmt):
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), "teal").save(buffer, format=fmt)
    return buffer.getvalue()


class ClinicLogoTests(AppointmentTestBase):
    """Logo: JPG, PNG or WebP (any case), up to 2 MB; type checked first."""

    def setUp(self):
        super().setUp()
        self.url = reverse("admin_clinic_settings")
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media, ignore_errors=True)
        override = override_settings(MEDIA_ROOT=self.media)
        override.enable()
        self.addCleanup(override.disable)

    def _data(self, **extra):
        data = {
            "tab": "details",
            "clinic_name": "Test Clinic",
            "address": "Gasan, Marinduque",
            "contact_number": "",
            "email": "",
            "notification_email": "",
        }
        data.update(extra)
        return data

    def _form(self, name, content=b"x", size=None):
        from appointments.forms import ClinicSettingsForm

        upload = SimpleUploadedFile(name, content)
        if size is not None:
            upload.size = size
        return ClinicSettingsForm(
            self._data(),
            {"logo": upload},
            instance=get_clinic_settings(),
            tab="details",
        )

    def _png(self, name="logo.png"):
        return SimpleUploadedFile(name, _image_bytes("PNG"), "image/png")

    # ─── Type ─────────────────────────────────────────────────────────────────

    def test_png_jpg_jpeg_and_webp_are_accepted_in_any_case(self):
        from PIL import features

        cases = [("logo.png", "PNG"), ("logo.JPG", "JPEG"), ("logo.jpeg", "JPEG")]
        if features.check("webp"):
            cases.append(("logo.webp", "WEBP"))
        for name, fmt in cases:
            with self.subTest(name=name):
                form = self._form(name, _image_bytes(fmt))
                self.assertTrue(form.is_valid(), form.errors)

    def test_svg_and_gif_are_refused_with_the_type_message(self):
        for name in ("logo.svg", "logo.gif", "logo.GIF", "logo"):
            with self.subTest(name=name):
                form = self._form(name, b"<svg></svg>")
                self.assertEqual(form.errors["logo"], [TYPE_MESSAGE])

    # ─── Size ─────────────────────────────────────────────────────────────────

    def test_exactly_2_mb_is_accepted_and_one_byte_more_is_refused(self):
        from appointments.forms import check_logo_upload

        for size, ok in ((2 * LOGO_MB, True), (2 * LOGO_MB + 1, False)):
            with self.subTest(size=size):
                upload = SimpleUploadedFile("logo.png", b"x")
                upload.size = size
                if ok:
                    check_logo_upload(upload)
                else:
                    from django.forms import ValidationError

                    with self.assertRaises(ValidationError) as caught:
                        check_logo_upload(upload)
                    self.assertEqual(
                        caught.exception.messages,
                        ["This file is 2.1 MB. Please choose one up to 2 MB."],
                    )

    def test_a_larger_file_shows_the_size_rounded_up(self):
        form = self._form("logo.png", size=int(4.21 * LOGO_MB))
        self.assertEqual(
            form.errors["logo"],
            ["This file is 4.3 MB. Please choose one up to 2 MB."],
        )

    def test_wrong_type_and_too_big_shows_only_the_type_message(self):
        form = self._form("logo.svg", size=5 * LOGO_MB)
        self.assertEqual(form.errors["logo"], [TYPE_MESSAGE])

    def test_a_non_image_with_an_allowed_name_keeps_djangos_message(self):
        form = self._form("logo.png", b"not an image")
        self.assertEqual(
            form.errors["logo"],
            [
                "Upload a valid image. The file you uploaded was either not an "
                "image or a corrupted image."
            ],
        )

    def test_help_text(self):
        from appointments.forms import ClinicSettingsForm

        self.assertEqual(
            ClinicSettingsForm().fields["logo"].help_text,
            "JPG, PNG or WebP, up to 2 MB. It shows on bills and the clinic "
            "page. Choosing a new file replaces the old one.",
        )

    # ─── Saving ───────────────────────────────────────────────────────────────

    def test_a_valid_logo_saves_on_the_details_tab(self):
        response = self.client.post(self.url, self._data(logo=self._png()))
        self.assertRedirects(response, f"{self.url}?tab=details")
        self.assertTrue(get_clinic_settings().logo.name.startswith("clinic/"))

    def test_saving_details_with_no_new_file_keeps_the_logo(self):
        self.client.post(self.url, self._data(logo=self._png()))
        kept = get_clinic_settings().logo.name
        response = self.client.post(self.url, self._data(clinic_name="Renamed"))
        self.assertRedirects(response, f"{self.url}?tab=details")
        saved = get_clinic_settings()
        self.assertEqual(saved.clinic_name, "Renamed")
        self.assertEqual(saved.logo.name, kept)

    def test_the_clear_checkbox_still_removes_the_logo(self):
        self.client.post(self.url, self._data(logo=self._png()))
        self.client.post(self.url, self._data(**{"logo-clear": "on"}))
        self.assertFalse(get_clinic_settings().logo)

    def test_a_refused_logo_saves_nothing_and_reopens_details(self):
        response = self.client.post(
            self.url,
            self._data(
                clinic_name="Should Not Save",
                logo=SimpleUploadedFile("logo.svg", b"<svg></svg>"),
            ),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["active_tab"], "details")
        self.assertEqual(response.context["tab_errors"]["details"], 1)
        saved = get_clinic_settings()
        self.assertEqual(saved.clinic_name, "Test Clinic")
        self.assertFalse(saved.logo)


class SettingsTabTests(AppointmentTestBase):
    """?tab=, saving per tab, the tab errors and the saved_settings banner
    data (Slice 7a-5)."""

    def setUp(self):
        super().setUp()
        self.url = reverse("admin_clinic_settings")

    def _data(self, **extra):
        data = {
            "clinic_name": "Test Clinic",
            "address": "Gasan, Marinduque",
            "contact_number": "",
            "email": "",
            "notification_email": "",
            "veterinarian_name": "",
            "veterinarian_license_number": "",
            "veterinarian_bio": "",
            "opening_time": "08:00",
            "closing_time": "17:00",
            "slot_duration_minutes": "60",
            "working_days": ["0", "1", "2", "3", "4", "5", "6"],
            "same_day_cutoff_time": "15:00",
            "booking_limit_days": "30",
        }
        data.update(extra)
        return data

    def _saved(self):
        return get_clinic_settings()

    # ─── GET ──────────────────────────────────────────────────────────────────

    def test_get_each_tab_value_opens_that_tab(self):
        for tab in ("details", "schedule", "blocked", "vet"):
            with self.subTest(tab=tab):
                response = self.client.get(self.url, {"tab": tab})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["active_tab"], tab)

    def test_unknown_or_missing_tab_falls_back_to_details(self):
        for query in ({"tab": "bogus"}, {"tab": ""}, {}):
            with self.subTest(query=query):
                response = self.client.get(self.url, query)
                self.assertEqual(response.context["active_tab"], "details")

    def test_get_has_no_tab_errors_and_the_banner_data(self):
        response = self.client.get(self.url)
        self.assertEqual(
            response.context["tab_errors"],
            {"details": 0, "schedule": 0, "blocked": 0, "vet": 0},
        )
        self.assertEqual(response.context["tab_problems"], {})
        self.assertEqual(response.context["saved_settings"].pk, self._saved().pk)
        self.assertEqual(
            response.context["hours_line"], "Every day, 8:00 AM to 5:00 PM"
        )
        self.assertIsNone(response.context["schedule_warning"])

    # ─── Saving per tab ───────────────────────────────────────────────────────

    def test_vet_tab_saves_only_the_vet_fields(self):
        response = self.client.post(
            self.url,
            self._data(
                tab="vet",
                veterinarian_name="Dr. Reyes",
                veterinarian_license_number="PRC 123",
                veterinarian_bio="Small animals.",
                # Other tabs' values are ignored on a vet save
                clinic_name="Ignored Name",
                opening_time="09:30",
                slot_duration_minutes="30",
            ),
        )
        self.assertRedirects(response, f"{self.url}?tab=vet")
        saved = self._saved()
        self.assertEqual(saved.veterinarian_name, "Dr. Reyes")
        self.assertEqual(saved.veterinarian_license_number, "PRC 123")
        self.assertEqual(saved.veterinarian_bio, "Small animals.")
        self.assertEqual(saved.clinic_name, "Test Clinic")
        self.assertEqual(saved.opening_time, datetime.time(8, 0))
        self.assertEqual(saved.slot_duration_minutes, 60)

    def test_schedule_tab_saves_only_the_schedule_fields(self):
        response = self.client.post(
            self.url,
            self._data(
                tab="schedule",
                booking_limit_days="60",
                clinic_name="Ignored Name",
                veterinarian_name="Ignored Vet",
            ),
        )
        self.assertRedirects(response, f"{self.url}?tab=schedule")
        saved = self._saved()
        self.assertEqual(saved.booking_limit_days, 60)
        self.assertEqual(saved.clinic_name, "Test Clinic")
        self.assertEqual(saved.veterinarian_name, "")

    def test_details_tab_saves_only_the_details_fields(self):
        response = self.client.post(
            self.url,
            self._data(
                tab="details",
                clinic_name="New Name",
                contact_number="0917",
                booking_limit_days="60",
            ),
        )
        self.assertRedirects(response, f"{self.url}?tab=details")
        saved = self._saved()
        self.assertEqual(saved.clinic_name, "New Name")
        self.assertEqual(saved.contact_number, "0917")
        self.assertEqual(saved.booking_limit_days, 30)

    def test_post_without_tab_saves_every_field_and_opens_details(self):
        response = self.client.post(
            self.url,
            self._data(
                clinic_name="New Name",
                veterinarian_name="Dr. Reyes",
                booking_limit_days="60",
            ),
        )
        self.assertRedirects(response, f"{self.url}?tab=details")
        saved = self._saved()
        self.assertEqual(saved.clinic_name, "New Name")
        self.assertEqual(saved.veterinarian_name, "Dr. Reyes")
        self.assertEqual(saved.booking_limit_days, 60)
        self.assertIn("Clinic settings updated successfully.", self._messages(response))

    def test_unknown_posted_tab_is_the_old_all_fields_save(self):
        response = self.client.post(
            self.url, self._data(tab="bogus", clinic_name="New Name")
        )
        self.assertRedirects(response, f"{self.url}?tab=details")
        self.assertEqual(self._saved().clinic_name, "New Name")

    # ─── Errors ───────────────────────────────────────────────────────────────

    def test_details_error_reopens_details_with_tab_errors(self):
        response = self.client.post(
            self.url, self._data(tab="details", clinic_name="", booking_limit_days="0")
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["active_tab"], "details")
        # The schedule field is not part of a details save, so only 1 error
        self.assertEqual(
            response.context["tab_errors"],
            {"details": 1, "schedule": 0, "blocked": 0, "vet": 0},
        )
        self.assertEqual(response.context["tab_problems"], {"details": "1 problem"})
        self.assertEqual(self._saved().clinic_name, "Test Clinic")

    def test_post_without_tab_opens_the_tab_of_the_first_error(self):
        response = self.client.post(self.url, self._data(booking_limit_days="0"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["active_tab"], "schedule")
        self.assertEqual(response.context["tab_errors"]["schedule"], 1)
        self.assertEqual(response.context["tab_problems"], {"schedule": "1 problem"})
        self.assertEqual(self._saved().booking_limit_days, 30)

    def test_first_error_tab_follows_tab_order_and_counts_problems(self):
        response = self.client.post(
            self.url,
            self._data(
                veterinarian_name="x" * 101,
                booking_limit_days="0",
                opening_time="",
            ),
        )
        self.assertEqual(response.context["active_tab"], "schedule")
        self.assertEqual(response.context["tab_errors"]["schedule"], 2)
        self.assertEqual(response.context["tab_errors"]["vet"], 1)
        self.assertEqual(
            response.context["tab_problems"],
            {"schedule": "2 problems", "vet": "1 problem"},
        )
        response = self.client.post(
            self.url, self._data(clinic_name="", veterinarian_name="x" * 101)
        )
        self.assertEqual(response.context["active_tab"], "details")

    def test_a_closing_time_error_counts_on_the_schedule_tab(self):
        response = self.client.post(
            self.url, self._data(opening_time="10:00", closing_time="09:00")
        )
        self.assertEqual(response.context["active_tab"], "schedule")
        self.assertEqual(response.context["tab_errors"]["schedule"], 1)

    def test_saved_settings_shows_the_saved_name_after_a_failed_post(self):
        response = self.client.post(
            self.url, self._data(clinic_name="Typed Name", booking_limit_days="0")
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["saved_settings"].clinic_name, "Test Clinic")
        self.assertEqual(response.context["settings_obj"].clinic_name, "Test Clinic")
        # The form keeps what was typed
        self.assertEqual(response.context["form"]["clinic_name"].value(), "Typed Name")

    def test_hours_line_comes_from_the_saved_settings_not_the_typed_ones(self):
        response = self.client.post(
            self.url,
            self._data(
                opening_time="09:00",
                working_days=["0", "1"],
                booking_limit_days="0",
            ),
        )
        self.assertEqual(
            response.context["hours_line"], "Every day, 8:00 AM to 5:00 PM"
        )

    # ─── Schedule warning ─────────────────────────────────────────────────────

    def _schedule_post(self, **extra):
        return self.client.post(self.url, self._data(tab="schedule", **extra))

    def test_slot_change_with_a_confirmed_visit_warns_then_saves_on_confirm(self):
        visit = self._appointment(Appointment.CONFIRMED)
        before = (visit.status, visit.date, visit.time)
        response = self._schedule_post(slot_duration_minutes="30")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._saved().slot_duration_minutes, 60)
        self.assertEqual(response.context["active_tab"], "schedule")
        day = date_format(visit.date, "D, M j")
        self.assertContains(
            response,
            "Changing the slot length to 30 minutes affects 1 visit already booked.",
        )
        self.assertContains(response, f"{day} at 9:00 AM · Bantay (Ana Cruz)")
        self.assertContains(
            response,
            "They stay booked. Nothing is cancelled or moved. Check that these "
            "times still fit the new slots, then save again to confirm.",
        )
        self.assertContains(response, "Go back")
        self.assertContains(response, "Yes, save the new schedule")
        self.assertNotContains(response, "Save changes")
        self.assertNotContains(response, "form-actions")
        self.assertNotContains(response, "and 0 more")

        response = self._schedule_post(slot_duration_minutes="30", confirm_schedule="1")
        self.assertRedirects(response, f"{self.url}?tab=schedule")
        self.assertEqual(self._saved().slot_duration_minutes, 30)
        visit.refresh_from_db()
        self.assertEqual((visit.status, visit.date, visit.time), before)

    def test_confirm_is_still_validated(self):
        self._appointment(Appointment.CONFIRMED)
        response = self._schedule_post(
            slot_duration_minutes="30", confirm_schedule="1", booking_limit_days="0"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._saved().slot_duration_minutes, 60)
        self.assertEqual(response.context["tab_errors"]["schedule"], 1)

    def test_warning_without_a_posted_tab_shows_on_the_old_page(self):
        self._appointment(Appointment.CONFIRMED)
        response = self.client.post(self.url, self._data(slot_duration_minutes="30"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["active_tab"], "schedule")
        self.assertContains(response, "Yes, save the new schedule")
        self.assertEqual(self._saved().slot_duration_minutes, 60)
        response = self.client.post(
            self.url, self._data(slot_duration_minutes="30", confirm_schedule="1")
        )
        self.assertRedirects(response, f"{self.url}?tab=details")
        self.assertEqual(self._saved().slot_duration_minutes, 30)

    def test_pending_visit_warns(self):
        self._appointment(Appointment.PENDING)
        response = self._schedule_post(slot_duration_minutes="30")
        self.assertContains(response, "affects 1 visit already booked.")
        self.assertEqual(self._saved().slot_duration_minutes, 60)

    def test_a_visit_today_warns(self):
        self._appointment(Appointment.CONFIRMED, days_from_today=0)
        response = self._schedule_post(slot_duration_minutes="30")
        self.assertContains(response, "affects 1 visit already booked.")

    def test_past_and_finished_visits_do_not_warn(self):
        self._appointment(Appointment.CONFIRMED, days_from_today=-2)
        self._appointment(Appointment.PENDING, days_from_today=-1)
        self._appointment(Appointment.CANCELLED, days_from_today=3)
        self._appointment(Appointment.COMPLETED, days_from_today=4)
        self._appointment(Appointment.NO_SHOW, days_from_today=5)
        response = self._schedule_post(slot_duration_minutes="30")
        self.assertRedirects(response, f"{self.url}?tab=schedule")
        self.assertEqual(self._saved().slot_duration_minutes, 30)

    def test_no_visits_saves_at_once(self):
        response = self._schedule_post(slot_duration_minutes="30")
        self.assertRedirects(response, f"{self.url}?tab=schedule")
        self.assertEqual(self._saved().slot_duration_minutes, 30)

    def test_an_unchanged_schedule_does_not_warn(self):
        self._appointment(Appointment.CONFIRMED)
        response = self._schedule_post(booking_limit_days="31")
        self.assertRedirects(response, f"{self.url}?tab=schedule")

    def test_booking_limit_change_alone_never_warns(self):
        self._appointment(Appointment.CONFIRMED)
        response = self._schedule_post(booking_limit_days="60")
        self.assertRedirects(response, f"{self.url}?tab=schedule")
        self.assertEqual(self._saved().booking_limit_days, 60)

    def test_other_tabs_never_warn(self):
        self._appointment(Appointment.CONFIRMED)
        for tab, extra in (
            ("details", {"clinic_name": "New Name"}),
            ("vet", {"veterinarian_name": "Dr. Reyes"}),
        ):
            with self.subTest(tab=tab):
                response = self.client.post(self.url, self._data(tab=tab, **extra))
                self.assertRedirects(response, f"{self.url}?tab={tab}")

    def test_go_back_saves_nothing_shows_no_warning_and_keeps_typed_values(self):
        self._appointment(Appointment.CONFIRMED)
        response = self._schedule_post(slot_duration_minutes="30", go_back="1")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["schedule_warning"])
        self.assertNotContains(response, "already booked")
        self.assertNotContains(response, "Yes, save the new schedule")
        self.assertContains(response, "Save changes")
        self.assertEqual(response.context["active_tab"], "schedule")
        self.assertEqual(
            response.context["form"]["slot_duration_minutes"].value(), "30"
        )
        self.assertEqual(self._saved().slot_duration_minutes, 60)
        self.assertEqual(self._messages(response), [])

    def test_heading_names_one_changed_field(self):
        self._appointment(Appointment.CONFIRMED)
        cases = (
            ({"opening_time": "09:00"}, "the opening hours"),
            ({"closing_time": "16:00"}, "the opening hours"),
            (
                {"opening_time": "09:00", "closing_time": "16:00"},
                "the opening hours",
            ),
            (
                {"working_days": ["0", "1", "2", "3", "4", "5"]},
                "the working days",
            ),
            ({"same_day_cutoff_time": "14:00"}, "the booking cutoff"),
            ({"slot_duration_minutes": "30"}, "the slot length to 30 minutes"),
        )
        for extra, phrase in cases:
            with self.subTest(extra=extra):
                response = self._schedule_post(**extra)
                self.assertEqual(
                    response.context["schedule_warning"]["heading"],
                    f"Changing {phrase} affects 1 visit already booked.",
                )

    def test_heading_for_several_fields_says_the_schedule(self):
        self._appointment(Appointment.CONFIRMED)
        response = self._schedule_post(slot_duration_minutes="30", opening_time="09:00")
        self.assertContains(
            response, "Changing the schedule affects 1 visit already booked."
        )

    def test_heading_counts_visits(self):
        for days in (3, 4, 5):
            self._appointment(Appointment.CONFIRMED, days_from_today=days)
        response = self._schedule_post(slot_duration_minutes="30")
        self.assertContains(
            response,
            "Changing the slot length to 30 minutes affects 3 visits already booked.",
        )

    def test_visits_are_listed_by_date_and_time(self):
        late = self._appointment(Appointment.CONFIRMED, days_from_today=5, hour=9)
        afternoon = self._appointment(Appointment.PENDING, days_from_today=2, hour=14)
        morning = self._appointment(Appointment.CONFIRMED, days_from_today=2, hour=9)
        response = self._schedule_post(slot_duration_minutes="30")
        expected = [
            f"{date_format(v.date, 'D, M j')} at {t} · Bantay (Ana Cruz)"
            for v, t in (
                (morning, "9:00 AM"),
                (afternoon, "2:00 PM"),
                (late, "9:00 AM"),
            )
        ]
        self.assertEqual(response.context["schedule_warning"]["lines"], expected)

    def test_more_than_ten_visits_show_and_n_more(self):
        for days in range(1, 13):
            self._appointment(Appointment.CONFIRMED, days_from_today=days)
        response = self._schedule_post(slot_duration_minutes="30")
        warning = response.context["schedule_warning"]
        self.assertEqual(len(warning["lines"]), 10)
        self.assertEqual(warning["more_count"], 2)
        self.assertContains(response, "affects 12 visits already booked.")
        self.assertContains(response, "and 2 more")
        exactly_ten = Appointment.objects.filter(
            date__gt=self.today + datetime.timedelta(days=10)
        )
        exactly_ten.delete()
        response = self._schedule_post(slot_duration_minutes="30")
        self.assertEqual(response.context["schedule_warning"]["more_count"], 0)
        self.assertNotContains(response, " more</li>")

    # ─── Access ───────────────────────────────────────────────────────────────

    def test_an_owner_cannot_reach_or_save_the_page(self):
        self.client.force_login(self.owner.user)
        response = self.client.get(self.url)
        self.assertRedirects(
            response, reverse("owner_dashboard"), fetch_redirect_response=False
        )
        response = self.client.post(self.url, self._data(clinic_name="Hacked"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._saved().clinic_name, "Test Clinic")

    def test_a_logged_out_user_cannot_reach_or_save_the_page(self):
        self.client.logout()
        for method in (self.client.get, self.client.post):
            with self.subTest(method=method.__name__):
                response = method(self.url)
                self.assertEqual(response.status_code, 302)
                self.assertIn("login", response["Location"])
        self.assertEqual(self._saved().clinic_name, "Test Clinic")


class SettingsPageMarkupTests(AppointmentTestBase):
    """The clinic settings page on the admin layout (Slice 7b-2): banner,
    ?tab= links, one section and one form per tab."""

    TABS = ("details", "schedule", "blocked", "vet")

    def setUp(self):
        super().setUp()
        self.url = reverse("admin_clinic_settings")

    def _data(self, **extra):
        data = {
            "tab": "schedule",
            "opening_time": "08:00",
            "closing_time": "17:00",
            "slot_duration_minutes": "60",
            "working_days": ["0", "1", "2", "3", "4", "5", "6"],
            "same_day_cutoff_time": "15:00",
            "booking_limit_days": "30",
        }
        data.update(extra)
        return data

    def _get(self, tab):
        return self.client.get(self.url, {"tab": tab})

    def _banner(self, response):
        html = response.content.decode()
        return html.split('id="clinic-banner"')[1].split("</section>")[0]

    def _tab_link(self, response, tab):
        """The whole <a>...</a> of one tab link."""
        match = re.search(
            rf'<a href="[^"]*\?tab={tab}".*?</a>',
            response.content.decode(),
            re.DOTALL,
        )
        self.assertIsNotNone(match, f"no link for tab {tab}")
        return match.group(0)

    # ─── Tabs ─────────────────────────────────────────────────────────────────

    def test_each_tab_shows_its_own_section_only(self):
        for tab in self.TABS:
            with self.subTest(tab=tab):
                response = self._get(tab)
                for other in self.TABS:
                    marker = f'id="settings-{other}"'
                    if other == tab:
                        self.assertContains(response, marker)
                    else:
                        self.assertNotContains(response, marker)

    def test_open_tab_has_aria_current_and_the_violet_ring(self):
        for tab in self.TABS:
            with self.subTest(tab=tab):
                response = self._get(tab)
                # The sidebar marks "Clinic settings" too, so count in the tab nav
                nav = (
                    response.content.decode()
                    .split('aria-label="Clinic settings sections"')[1]
                    .split("</nav>")[0]
                )
                self.assertEqual(nav.count('aria-current="page"'), 1)
                for other in self.TABS:
                    link = self._tab_link(response, other)
                    opening = link.split(">")[0]
                    if other == tab:
                        self.assertIn('aria-current="page"', opening)
                        self.assertIn("bg-violet-50", opening)
                        self.assertIn("ring-2 ring-violet-600", opening)
                    else:
                        self.assertNotIn("aria-current", opening)
                        self.assertNotIn("ring-violet-600", opening)

    def test_tabs_are_in_order_with_their_labels(self):
        html = self._get("details").content.decode()
        nav = html.split('aria-label="Clinic settings sections"')[1].split("</nav>")[0]
        positions = [
            nav.index(label)
            for label in ("Clinic details", "Schedule", "Blocked dates", "Vet profile")
        ]
        self.assertEqual(positions, sorted(positions))
        self.assertContains(self._get("details"), "grid-cols-2")

    def test_unknown_tab_opens_details(self):
        response = self._get("bogus")
        self.assertContains(response, 'id="settings-details"')

    # ─── Forms ────────────────────────────────────────────────────────────────

    def test_each_form_posts_a_hidden_tab(self):
        for tab in ("details", "schedule", "vet"):
            with self.subTest(tab=tab):
                response = self._get(tab)
                self.assertContains(
                    response, f'<input type="hidden" name="tab" value="{tab}">', count=1
                )
                self.assertContains(response, 'method="post"')
                self.assertContains(response, "csrfmiddlewaretoken")
                self.assertContains(response, "Save changes", count=1)

    def test_only_the_details_form_is_multipart(self):
        self.assertContains(
            self._get("details"), 'enctype="multipart/form-data"', count=1
        )
        for tab in ("schedule", "vet"):
            with self.subTest(tab=tab):
                self.assertNotContains(self._get(tab), "multipart/form-data")

    def test_vet_tab_has_the_given_by_note(self):
        self.assertContains(
            self._get("vet"),
            "The vet name and profile show on the clinic page and as the vet "
            "on new vaccination records.",
        )

    def test_working_days_are_large_checkboxes_and_checked_when_open(self):
        settings_obj = get_clinic_settings()
        settings_obj.working_days = [0, 2]
        settings_obj.save()
        html = self._get("schedule").content.decode()
        boxes = re.findall(r'<input type="checkbox" name="working_days"[^>]*>', html)
        self.assertEqual(len(boxes), 7)
        for box in boxes:
            self.assertIn("size-6", box)
            self.assertIn("accent-violet-600", box)
        checked = [b for b in boxes if " checked" in b]
        self.assertEqual(len(checked), 2)

    # ─── Errors ───────────────────────────────────────────────────────────────

    def test_schedule_error_shows_the_pill_and_the_summary(self):
        response = self.client.post(self.url, self._data(booking_limit_days="0"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="settings-schedule"')
        self.assertContains(response, "1 problem", count=1)
        self.assertIn("1 problem", self._tab_link(response, "schedule"))
        for other in ("details", "blocked", "vet"):
            self.assertNotIn("problem", self._tab_link(response, other))
        self.assertContains(response, 'role="alert"')
        self.assertContains(response, "Please fix the 1 item below.")
        self.assertContains(response, "Nothing was saved yet.")
        self.assertContains(response, "The booking limit must be at least 1 day.")

    def test_two_errors_say_items_and_problems(self):
        response = self.client.post(
            self.url, self._data(booking_limit_days="0", opening_time="")
        )
        self.assertContains(response, "Please fix the 2 items below.")
        self.assertIn("2 problems", self._tab_link(response, "schedule"))

    def test_closing_before_opening_is_listed_in_the_summary(self):
        response = self.client.post(
            self.url, self._data(opening_time="10:00", closing_time="09:00")
        )
        self.assertContains(response, "Closing time must be after opening time.")
        self.assertContains(response, "Please fix the 1 item below.")

    def test_a_tab_without_errors_has_no_summary(self):
        self.assertNotContains(self._get("schedule"), "Please fix the")

    def test_banner_keeps_the_saved_name_after_a_failed_post(self):
        response = self.client.post(
            self.url,
            {
                "tab": "details",
                "clinic_name": "Typed Name",
                "address": "Typed Address",
                "email": "not-an-email",
            },
        )
        self.assertEqual(response.status_code, 200)
        banner = self._banner(response)
        self.assertIn("Test Clinic", banner)
        self.assertIn("Gasan, Marinduque", banner)
        self.assertNotIn("Typed Name", banner)
        self.assertNotIn("Typed Address", banner)
        # The typed values are still in the form below the banner
        self.assertContains(response, 'value="Typed Name"')
        self.assertIn("1 problem", self._tab_link(response, "details"))

    # ─── Schedule warning ─────────────────────────────────────────────────────

    def test_warning_shows_go_back_and_confirm_and_hides_save_changes(self):
        self._appointment(Appointment.CONFIRMED)
        response = self.client.post(self.url, self._data(slot_duration_minutes="30"))
        self.assertContains(response, "Go back")
        self.assertContains(response, "Yes, save the new schedule")
        self.assertNotContains(response, "Save changes")
        html = response.content.decode()
        self.assertEqual(html.count("autofocus"), 1)
        self.assertRegex(html, r'<button[^>]*name="go_back" value="1"[^>]*autofocus')
        self.assertContains(response, 'name="confirm_schedule" value="1"')

    def test_warning_buttons_post_every_schedule_field_and_the_tab(self):
        self._appointment(Appointment.CONFIRMED)
        response = self.client.post(self.url, self._data(slot_duration_minutes="30"))
        forms = response.content.decode().split("<form")
        form = next(chunk for chunk in forms if 'name="go_back"' in chunk)
        form = form.split("</form>")[0]
        self.assertIn('name="confirm_schedule"', form)
        self.assertIn('<input type="hidden" name="tab" value="schedule">', form)
        for name in (
            "opening_time",
            "closing_time",
            "slot_duration_minutes",
            "working_days",
            "same_day_cutoff_time",
            "booking_limit_days",
        ):
            with self.subTest(field=name):
                self.assertIn(f'name="{name}"', form)

    def test_schedule_tab_without_a_warning_shows_save_changes(self):
        response = self._get("schedule")
        self.assertContains(response, "Save changes")
        self.assertNotContains(response, "Yes, save the new schedule")

    # ─── Banner ───────────────────────────────────────────────────────────────

    def test_banner_has_the_hours_line_and_the_public_page_link(self):
        banner = self._banner(self._get("details"))
        self.assertIn("Every day, 8:00 AM to 5:00 PM", banner)
        self.assertIn("View public clinic page", banner)
        self.assertIn(f'href="{reverse("clinic_info")}"', banner)
        self.assertIn("bg-gradient-to-r from-violet-600 to-violet-500", banner)

    def test_banner_shows_phone_only_when_there_is_one(self):
        self.assertNotIn("0917 000 0000", self._banner(self._get("details")))
        settings_obj = get_clinic_settings()
        settings_obj.contact_number = "0917 000 0000"
        settings_obj.save()
        self.assertIn("0917 000 0000", self._banner(self._get("details")))

    def test_banner_shows_the_logo_or_a_plain_tile(self):
        self.assertNotIn("<img", self._banner(self._get("details")))
        settings_obj = get_clinic_settings()
        settings_obj.logo = "clinic/logo.png"
        settings_obj.save()
        banner = self._banner(self._get("details"))
        self.assertIn("<img", banner)
        self.assertIn("clinic/logo.png", banner)

    # ─── Old files ────────────────────────────────────────────────────────────

    def test_the_page_never_loads_the_old_script(self):
        for tab in self.TABS:
            with self.subTest(tab=tab):
                self.assertNotContains(self._get(tab), "js/admin/clinic_settings")

    def test_no_tab_loads_the_old_stylesheet(self):
        for tab in self.TABS:
            with self.subTest(tab=tab):
                response = self._get(tab)
                self.assertNotContains(response, "css/admin/clinic_settings")
                self.assertNotContains(response, "layer(legacy)")

    # ─── Logo ─────────────────────────────────────────────────────────────────

    def test_logo_clear_checkbox_is_labelled_remove_the_logo(self):
        settings_obj = get_clinic_settings()
        settings_obj.logo = "clinic/logo.png"
        settings_obj.save()
        response = self._get("details")
        self.assertContains(response, 'name="logo-clear"')
        self.assertContains(response, '<label for="logo-clear_id"')
        self.assertContains(response, "<span>Remove the logo</span>")
        self.assertContains(response, "Current clinic logo")

    def test_no_clear_checkbox_without_a_saved_logo(self):
        response = self._get("details")
        self.assertNotContains(response, "logo-clear")
        self.assertNotContains(response, "Remove the logo")

    def test_logo_input_is_labelled_and_shows_the_form_help(self):
        response = self._get("details")
        self.assertContains(response, '<label for="id_logo"')
        self.assertContains(response, "Choose a new logo")
        self.assertContains(
            response,
            "JPG, PNG or WebP, up to 2 MB. It shows on bills and the clinic "
            "page. Choosing a new file replaces the old one.",
        )
        self.assertContains(response, 'type="file" name="logo" id="id_logo"')

    def test_a_refused_logo_shows_its_message_under_the_input(self):
        response = self.client.post(
            self.url,
            {
                "tab": "details",
                "clinic_name": "Test Clinic",
                "address": "Gasan, Marinduque",
                "logo": SimpleUploadedFile("logo.svg", b"<svg></svg>"),
            },
        )
        self.assertContains(response, "Only JPG, PNG or WebP files can be used.")
        self.assertContains(response, "Please fix the 1 item below.")

    # ─── Blocked tab and the form field override ──────────────────────────────

    def test_only_the_blocked_tab_includes_the_blocked_panel(self):
        for tab in self.TABS:
            with self.subTest(tab=tab):
                response = self._get(tab)
                if tab == "blocked":
                    self.assertContains(response, 'id="blocked-dates-panel"')
                else:
                    self.assertNotContains(response, 'id="blocked-dates-panel"')

    def test_field_labels_use_plain_wording(self):
        schedule = self._get("schedule")
        for label in (
            "Appointment slot length",
            "Same-day booking cutoff",
            "Booking limit (days ahead)",
        ):
            self.assertContains(schedule, label)
        self.assertNotContains(schedule, "Slot duration minutes")
        self.assertContains(
            schedule,
            "Owners can book up to this many days ahead. Between 1 and 365.",
        )
        details = self._get("details")
        self.assertContains(details, "Admin notification email")
        self.assertContains(details, "Blank means the admin account email.")

    def test_page_has_no_inline_handlers_in_the_settings_content(self):
        for tab in self.TABS:
            with self.subTest(tab=tab):
                main = self._get(tab).content.decode().split('id="main-content"')[1]
                main = main.split("</main>")[0]
                self.assertNotIn("onclick", main)
                self.assertNotIn("<!--", main.split('id="blocked-dates-panel"')[0])


class ClinicInfoPageTests(AppointmentTestBase):
    """Clinic info page: admin frame (7b-4, mockup state 10) and owner frame."""

    def setUp(self):
        super().setUp()
        self.url = reverse("clinic_info")
        row = get_clinic_settings()
        row.contact_number = "0917 000 0000"
        row.email = "clinic@example.com"
        row.veterinarian_name = "Dr. Edgar Sadiwa"
        row.veterinarian_license_number = "PRC-VET-0000"
        row.veterinarian_bio = "Cares for pets of Gasan."
        row.save()

    def _owner_get(self):
        self.client.force_login(self.owner.user)
        return self.client.get(self.url)

    def test_admin_gets_the_admin_page_with_its_content(self):
        response = self.client.get(self.url)
        self.assertTemplateUsed(response, "admin/clinic_info.html")
        self.assertTemplateUsed(response, "layouts/admin.html")
        self.assertTemplateNotUsed(response, "shared/clinic_info.html")
        self.assertContains(response, "Every day, 8:00 AM to 5:00 PM")
        self.assertContains(response, "Dr. Edgar Sadiwa")
        self.assertContains(response, "PRC-VET-0000")
        self.assertContains(response, "Cares for pets of Gasan.")
        self.assertContains(response, "0917 000 0000")
        self.assertContains(response, "clinic@example.com")
        self.assertContains(response, "Test Clinic")

    def test_admin_page_has_the_map_and_a_labelled_map_link(self):
        response = self.client.get(self.url)
        self.assertContains(
            response, "https://www.google.com/maps?q=Gasan%2C%20Marinduque"
        )
        self.assertContains(response, "Open in Google Maps")
        self.assertContains(response, 'title="Map of the clinic"')

    def test_admin_page_has_the_back_link(self):
        response = self.client.get(self.url)
        self.assertContains(response, f'href="{reverse("admin_clinic_settings")}"')
        self.assertContains(response, "Back to clinic settings")

    def test_admin_page_loads_no_old_files(self):
        response = self.client.get(self.url)
        # Static URLs carry a hash (clinic_info.<hash>.css), so match the folder
        self.assertNotContains(response, "css/shared/clinic_info")
        self.assertNotContains(response, "js/shared/clinic_info")
        self.assertNotContains(response, "layer(legacy)")
        self.assertNotContains(response, "clinic-info-page")

    def test_admin_page_has_no_violet_button_and_no_inline_js(self):
        html = self.client.get(self.url).content.decode()
        main = html.split('id="main-content"')[1].split("</main>")[0]
        self.assertNotIn("bg-violet-600", main)
        self.assertNotIn("onclick", main)
        self.assertNotIn("<!--", main)

    def test_admin_page_without_an_address_has_no_map(self):
        row = get_clinic_settings()
        row.address = ""
        row.save()
        response = self.client.get(self.url)
        self.assertNotContains(response, "Open in Google Maps")
        self.assertContains(response, "No address is set")

    def test_owner_gets_the_owner_layout_page(self):
        # 8b-9b: owner/clinic/info.html on layouts/owner.html (was the shared
        # page on base_dashboard.html with its own CSS and JS, and a
        # "Book an Appointment" button)
        response = self._owner_get()
        self.assertTemplateUsed(response, "owner/clinic/info.html")
        self.assertTemplateUsed(response, "layouts/owner.html")
        self.assertTemplateNotUsed(response, "base_dashboard.html")
        self.assertTemplateNotUsed(response, "shared/clinic_info.html")
        self.assertTemplateNotUsed(response, "admin/clinic_info.html")
        self.assertNotContains(response, "css/shared/clinic_info")
        self.assertNotContains(response, "js/shared/clinic_info")
        self.assertContains(response, "Dr. Edgar Sadiwa")
        self.assertNotContains(response, "Back to clinic settings")

    def test_logged_out_goes_to_the_login_page(self):
        self.client.logout()
        response = self.client.get(self.url)
        self.assertRedirects(
            response, f"/login/?next={self.url}", fetch_redirect_response=False
        )


class ClinicHoursLineTests(SimpleTestCase):
    """The banner's hours line (decision 12)."""

    def _line(self, days, opening=(8, 0), closing=(17, 0)):
        row = ClinicSettings(
            working_days=days,
            opening_time=datetime.time(*opening),
            closing_time=datetime.time(*closing),
        )
        return clinic_hours_line(row)

    def test_days_in_a_row(self):
        self.assertEqual(
            self._line([0, 1, 2, 3, 4, 5]), "Mon to Sat, 8:00 AM to 5:00 PM"
        )
        self.assertEqual(self._line([0, 1]), "Mon to Tue, 8:00 AM to 5:00 PM")
        self.assertEqual(self._line([2, 3, 4, 5, 6]), "Wed to Sun, 8:00 AM to 5:00 PM")

    def test_days_not_in_a_row(self):
        self.assertEqual(self._line([0, 2, 4]), "Mon, Wed and Fri, 8:00 AM to 5:00 PM")
        self.assertEqual(self._line([0, 2]), "Mon and Wed, 8:00 AM to 5:00 PM")
        self.assertEqual(
            self._line([0, 1, 2, 4]), "Mon, Tue, Wed and Fri, 8:00 AM to 5:00 PM"
        )

    def test_one_day(self):
        self.assertEqual(self._line([3]), "Thu, 8:00 AM to 5:00 PM")

    def test_every_day(self):
        self.assertEqual(
            self._line([0, 1, 2, 3, 4, 5, 6]), "Every day, 8:00 AM to 5:00 PM"
        )

    def test_no_days_is_closed(self):
        self.assertEqual(self._line([]), "Closed")

    def test_unsorted_days_and_minutes(self):
        self.assertEqual(
            self._line([5, 0, 3, 1, 2, 4], opening=(8, 30), closing=(17, 30)),
            "Mon to Sat, 8:30 AM to 5:30 PM",
        )

    def test_noon_and_midnight_style_times(self):
        self.assertEqual(
            self._line([0], opening=(9, 0), closing=(12, 0)),
            "Mon, 9:00 AM to 12:00 PM",
        )


class ResolvePeriodTests(SimpleTestCase):

    def _period(self, show, from_value="", today=PERIOD_TODAY):
        return resolve_period(show, from_value, today)

    def test_any_and_unknown_show(self):
        for show in (None, "", "any", "bogus", "Week"):
            with self.subTest(show=show):
                period = self._period(show, "2026-11-05")
                self.assertEqual(period["kind"], "any")
                self.assertIsNone(period["start"])
                self.assertIsNone(period["end"])

    def test_day_labels(self):
        cases = {
            "2026-11-02": ("Today, Monday, November 2", True),
            "2026-11-01": ("Sunday, November 1", False),
            "2026-11-03": ("Tomorrow, Tuesday, November 3", False),
            "2027-01-04": ("Monday, January 4, 2027", False),
        }
        for from_value, (label, is_current) in cases.items():
            with self.subTest(from_value=from_value):
                period = self._period("day", from_value)
                self.assertEqual(period["label"], label)
                self.assertEqual(period["is_current"], is_current)
                self.assertEqual(period["start"], period["end"])
        period = self._period("day", "2026-11-02")
        self.assertEqual(period["mid_label"], "today, Monday, November 2")
        self.assertEqual(period["prev_from"], D(2026, 11, 1))
        self.assertEqual(period["next_from"], D(2026, 11, 3))
        self.assertEqual(period["back_to"], "Back to today")
        self.assertEqual(period["back_to_mid"], "today")

    def test_week_labels(self):
        cases = {
            "2026-11-02": "This week: Nov 2 to Nov 8",
            "2026-10-26": "Last week: Oct 26 to Nov 1",
            "2026-11-09": "Next week: Nov 9 to Nov 15",
            "2026-11-16": "Nov 16 to Nov 22",
            "2025-12-01": "Dec 1 to Dec 7, 2025",
        }
        for from_value, label in cases.items():
            with self.subTest(from_value=from_value):
                self.assertEqual(self._period("week", from_value)["label"], label)
        period = self._period("week", "2026-11-02")
        self.assertEqual(period["end"], D(2026, 11, 8))
        self.assertEqual(period["mid_label"], "this week: Nov 2 to Nov 8")
        self.assertEqual(period["prev_from"], D(2026, 10, 26))
        self.assertEqual(period["next_from"], D(2026, 11, 9))
        self.assertEqual(period["back_to"], "Back to this week")

    def test_week_across_new_year(self):
        period = self._period("week", "2026-12-30")
        self.assertEqual(period["start"], D(2026, 12, 28))
        self.assertEqual(period["end"], D(2027, 1, 3))
        self.assertEqual(period["label"], "Dec 28, 2026 to Jan 3, 2027")
        this_week = self._period("week", "", today=D(2026, 12, 31))
        self.assertEqual(this_week["label"], "This week: Dec 28, 2026 to Jan 3, 2027")

    def test_month_labels_and_steps(self):
        period = self._period("month", "2026-11-02")
        self.assertEqual(period["label"], "This month: November 2026")
        self.assertEqual(period["mid_label"], "this month: November 2026")
        self.assertEqual(period["back_to"], "Back to this month")
        october = self._period("month", "2026-10-15")
        self.assertEqual(october["label"], "October 2026")
        self.assertEqual(october["start"], D(2026, 10, 1))
        self.assertEqual(october["end"], D(2026, 10, 31))
        self.assertEqual(october["prev_from"], D(2026, 9, 1))
        self.assertEqual(october["next_from"], D(2026, 11, 1))
        self.assertEqual(
            self._period("month", "2026-12-01")["next_from"], D(2027, 1, 1)
        )
        self.assertEqual(
            self._period("month", "2027-01-01")["prev_from"], D(2026, 12, 1)
        )

    def test_leap_year_february(self):
        leap = self._period("month", "2028-02-15")
        self.assertEqual(leap["start"], D(2028, 2, 1))
        self.assertEqual(leap["end"], D(2028, 2, 29))
        self.assertEqual(leap["label"], "February 2028")
        self.assertEqual(self._period("month", "2027-02-10")["end"], D(2027, 2, 28))

    def test_year_labels(self):
        period = self._period("year", "2026-11-02")
        self.assertEqual(period["label"], "This year: 2026")
        self.assertEqual(period["start"], D(2026, 1, 1))
        self.assertEqual(period["end"], D(2026, 12, 31))
        last = self._period("year", "2025-01-01")
        self.assertEqual(last["label"], "2025")
        self.assertEqual(last["next_from"], D(2026, 1, 1))
        self.assertEqual(last["back_to"], "Back to this year")

    def test_mid_period_from_snaps_to_its_start(self):
        cases = {
            "week": ("2026-11-05", D(2026, 11, 2)),
            "month": ("2026-11-19", D(2026, 11, 1)),
            "year": ("2025-07-04", D(2025, 1, 1)),
        }
        for kind, (from_value, start) in cases.items():
            with self.subTest(kind=kind):
                self.assertEqual(self._period(kind, from_value)["start"], start)
        self.assertTrue(self._period("week", "2026-11-08")["is_current"])

    def test_bad_or_missing_from_means_the_current_period(self):
        for from_value in ("", None, "31/10/2026", "2026-02-30", "soon"):
            with self.subTest(from_value=from_value):
                period = self._period("week", from_value)
                self.assertEqual(period["start"], D(2026, 11, 2))
                self.assertTrue(period["is_current"])
        # A step past the first or last date Python can hold falls back too
        self.assertEqual(self._period("year", "9999-06-01")["start"], D(2026, 1, 1))
        self.assertEqual(self._period("day", "0001-01-01")["start"], PERIOD_TODAY)

    def test_short_labels(self):
        cases = {
            "day": {
                "2026-11-02": "Today",
                "2026-11-03": "Tomorrow",
                "2026-11-01": "Yesterday",
                "2026-10-31": "Sat, Oct 31",
                "2027-01-04": "Mon, Jan 4, 2027",
            },
            "week": {
                "2026-11-02": "This week",
                "2026-10-26": "Last week",
                "2026-11-09": "Next week",
                "2026-11-16": "Week of Nov 16",
                "2026-12-28": "Week of Dec 28",
                "2025-12-01": "Week of Dec 1, 2025",
                "2027-01-04": "Week of Jan 4, 2027",
            },
            "month": {
                "2026-11-01": "This month",
                "2026-10-01": "Last month",
                "2026-12-01": "Next month",
                "2026-09-01": "September",
                "2027-01-01": "January 2027",
                "2025-11-01": "November 2025",
            },
            "year": {
                "2026-01-01": "This year",
                "2025-01-01": "Last year",
                "2027-01-01": "Next year",
                "2024-01-01": "2024",
            },
        }
        for kind, labels in cases.items():
            for from_value, label in labels.items():
                with self.subTest(kind=kind, from_value=from_value):
                    period = self._period(kind, from_value)
                    self.assertEqual(period["short_label"], label)

    def test_short_label_for_the_week_of_dec_28_seen_from_january(self):
        """The week's Monday decides the year: seen from Jan 2, 2027 the week
        of Dec 28, 2026 is this week; the one before has its year added."""
        today = D(2027, 1, 2)
        self.assertEqual(
            self._period("week", "2026-12-28", today)["short_label"], "This week"
        )
        self.assertEqual(
            self._period("week", "2026-12-14", today)["short_label"],
            "Week of Dec 14, 2026",
        )

    def test_current_label(self):
        for kind, label in (
            ("day", "Today"),
            ("week", "This week"),
            ("month", "This month"),
            ("year", "This year"),
        ):
            with self.subTest(kind=kind):
                self.assertEqual(
                    self._period(kind, "2020-05-05")["current_label"], label
                )

    def test_is_past(self):
        self.assertTrue(self._period("week", "2026-10-26")["is_past"])
        self.assertFalse(self._period("week", "2026-11-02")["is_past"])

    def test_old_date_param(self):
        period, is_old = list_period({"date": "2026-11-05"}, PERIOD_TODAY)
        self.assertTrue(is_old)
        self.assertEqual((period["kind"], period["start"]), ("day", D(2026, 11, 5)))
        period, is_old = list_period(
            {"date": "2026-11-05", "show": "month"}, PERIOD_TODAY
        )
        self.assertFalse(is_old)
        self.assertEqual(period["kind"], "month")
        period, is_old = list_period({"date": "bad"}, PERIOD_TODAY)
        self.assertEqual((period["kind"], is_old), ("any", False))


class ListPeriodViewTests(FrozenNowTestBase):
    """The list page with "now" at 00:30 on Monday, Nov 2, 2026 (Manila),
    still Sunday in UTC."""

    NOW = _manila(0, 30)

    def _get(self, **params):
        # "from" is a Python keyword, so tests pass it as from_
        if "from_" in params:
            params["from"] = params.pop("from_")
        return self.client.get(reverse("admin_appointments"), params)

    def _html(self, response):
        return response.content.decode()

    def _ids(self, response):
        return [a.pk for a in response.context["page_obj"].object_list]

    def _counts(self, response):
        return {t["id"]: t["count"] for t in response.context["tabs"]}

    def _form_with(self, html, control_id):
        """The <form> holding the element with id control_id."""
        pattern = rf'<form [^>]*>(?:(?!</form>).)*id="{control_id}".*?</form>'
        return re.search(pattern, html, re.S).group(0)

    def _status_nav(self, html):
        pattern = r'<nav [^>]*aria-label="Filter by status">.*?</nav>'
        return re.search(pattern, html, re.S).group(0)

    def _heading(self, html):
        """The period row's heading (day card headings have an id first)."""
        match = re.search(
            r'<h2 class="text-xl font-semibold text-gray-900[^"]*">([^<]+)</h2>', html
        )
        return match.group(1)

    def _period_row(self, html):
        """The period row up to the Dates group."""
        pattern = (
            r'<div class="mb-4 flex flex-wrap items-center gap-x-6 gap-y-3">'
            r'.*?aria-label="Dates"'
        )
        return re.search(pattern, html, re.S).group(0)

    def _dates_group(self, html):
        pattern = r'<div role="group" aria-label="Dates" class="([^"]*)">(.*?)</div>'
        return re.search(pattern, html, re.S).groups()

    # ─── Period ───────────────────────────────────────────────────────────────

    def test_today_is_the_manila_date(self):
        response = self._get(show="day")
        self.assertEqual(response.context["period"]["start"], WALK_IN_DAY)
        self.assertEqual(self._heading(self._html(response)), "Today")

    def test_period_filters_inside_the_tab(self):
        today = self._appointment(Appointment.CONFIRMED, 0)
        thursday = self._appointment(Appointment.CONFIRMED, 3)
        self._appointment(Appointment.CONFIRMED, 7)
        self._appointment(Appointment.PENDING, 1)
        response = self._get(status="upcoming", show="week")
        self.assertEqual(response.context["active_tab"], "upcoming")
        self.assertEqual(self._ids(response), [today.pk, thursday.pk])
        self.assertEqual(self._heading(self._html(response)), "This week")

    def test_tab_counts_ignore_period_and_search(self):
        self._appointment(Appointment.CONFIRMED, 0)
        self._appointment(Appointment.CONFIRMED, 10)
        self._appointment(Appointment.PENDING, 1)
        plain = self._counts(self._get())
        for params in (
            {"show": "week"},
            {"show": "day", "from_": "2026-11-20"},
            {"q": "Bantay", "show": "month", "from_": "2025-01-01"},
            {"q": "nobody"},
        ):
            with self.subTest(params=params):
                self.assertEqual(self._counts(self._get(**params)), plain)

    def test_upcoming_with_a_past_week_is_empty_and_says_why(self):
        self._appointment(Appointment.CONFIRMED, -5)
        response = self._get(status="upcoming", show="week", from_="2026-10-26")
        self.assertEqual(self._ids(response), [])
        self.assertContains(response, "No appointments in this period")
        self.assertContains(
            response,
            "Upcoming shows visits from today on. Pick another period, "
            "or go back to this week.",
        )
        self.assertContains(
            response, 'href="?status=upcoming&amp;show=week&amp;from=2026-11-02"'
        )
        self.assertContains(response, "Back to this week")

    def test_empty_other_period_and_current_period(self):
        response = self._get(status="completed", show="month", from_="2026-09-01")
        self.assertContains(response, "Pick another period, or go back to this month.")
        self.assertNotContains(response, "Upcoming shows visits")
        response = self._get(status="completed", show="month")
        self.assertContains(response, "Pick another period.")
        self.assertNotContains(response, "Back to this month")

    def test_old_date_link_opens_that_day(self):
        pending = self._appointment(Appointment.PENDING, 3)
        confirmed = self._appointment(Appointment.CONFIRMED, 3, hour=10)
        self._appointment(Appointment.CONFIRMED, 4)
        response = self._get(date="2026-11-05")
        self.assertEqual(response.context["active_tab"], "all")
        self.assertEqual(response.context["period"]["kind"], "day")
        self.assertEqual(set(self._ids(response)), {pending.pk, confirmed.pk})
        self.assertContains(response, "Thursday, November 5")
        _, links = self._dates_group(self._html(response))
        self.assertEqual(
            re.findall(r'aria-current="true"[^>]*>([^<]+)<', links), ["Day"]
        )
        response = self._get(date="2026-11-05", status="pending")
        self.assertEqual(response.context["active_tab"], "pending")
        self.assertEqual(self._ids(response), [pending.pk])

    def test_previous_next_and_back_links_keep_params(self):
        response = self._get(status="pending", show="week", from_="2026-11-16")
        context = response.context
        self.assertEqual(
            context["prev_period_query"], "status=pending&show=week&from=2026-11-09"
        )
        self.assertEqual(
            context["next_period_query"], "status=pending&show=week&from=2026-11-23"
        )
        self.assertEqual(
            context["current_period_query"], "status=pending&show=week&from=2026-11-02"
        )
        row = self._period_row(self._html(response))
        self.assertRegex(
            row,
            r'(?s)<a href="\?status=pending&amp;show=week&amp;from=2026-11-09" '
            r'class="inline-flex min-h-11[^"]*">(?:(?!</a>).)*Previous',
        )
        self.assertRegex(
            row,
            r'<a href="\?status=pending&amp;show=week&amp;from=2026-11-23" '
            r'class="inline-flex min-h-11[^"]*">Next',
        )
        # The middle button links back to the current period
        self.assertRegex(
            row,
            r'<a href="\?status=pending&amp;show=week&amp;from=2026-11-02" '
            r'class="[^"]*text-blue-700[^"]*">This week</a>',
        )
        searching = self._get(q="Bantay", show="month")
        self.assertEqual(
            searching.context["prev_period_query"],
            "status=all&q=Bantay&show=month&from=2026-10-01",
        )

    def test_all_dates_heading_and_no_control_on_any_date(self):
        html = self._html(self._get())
        self.assertEqual(self._heading(html), "All dates")
        self.assertNotIn('aria-label="Pick a period"', html)
        self.assertNotIn("Previous<span", html)
        html = self._html(self._get(show="week"))
        self.assertEqual(self._heading(html), "This week")
        self.assertIn('aria-label="Pick a period"', html)
        self.assertIn(
            '<h2 class="text-xl font-semibold text-gray-900 md:whitespace-nowrap">',
            html,
        )

    def test_middle_button_is_disabled_on_the_current_period(self):
        for show, label in (
            ("day", "Today"),
            ("week", "This week"),
            ("month", "This month"),
            ("year", "This year"),
        ):
            with self.subTest(show=show):
                row = self._period_row(self._html(self._get(show=show)))
                middle = re.search(
                    r'<span aria-disabled="true" title="You are on [^"]+" '
                    rf'class="([^"]*)">{label}</span>',
                    row,
                )
                self.assertIsNotNone(middle)
                self.assertIn("bg-gray-100", middle.group(1).split())
                self.assertIn("text-gray-600", middle.group(1).split())
                self.assertNotIn(f">{label}</a>", row)
        row = self._period_row(self._html(self._get(show="month", from_="2026-09-01")))
        self.assertNotIn('aria-disabled="true"', row)
        self.assertRegex(
            row,
            r'<a href="\?status=upcoming&amp;show=month&amp;from=2026-11-01" '
            r'class="[^"]*text-blue-700[^"]*">This month</a>',
        )

    def test_no_back_to_link_in_the_period_row(self):
        for params in (
            {"show": "week", "from_": "2026-10-26"},
            {"show": "day", "from_": "2026-11-20"},
        ):
            with self.subTest(params=params):
                self.assertNotIn(
                    "Back to", self._period_row(self._html(self._get(**params)))
                )

    def test_dates_links(self):
        html = self._html(
            self._get(status="upcoming", show="month", from_="2026-10-01")
        )
        group_class, links = self._dates_group(html)
        found = re.findall(
            r'<a href="\?([^"]*)"( aria-current="true")? class="([^"]*)">([^<]+)</a>',
            links,
        )
        self.assertEqual(
            [(query, bool(current), label) for query, current, _, label in found],
            [
                ("status=upcoming", False, "Any date"),
                ("status=upcoming&amp;show=day", False, "Day"),
                ("status=upcoming&amp;show=week", False, "Week"),
                ("status=upcoming&amp;show=month", True, "Month"),
                ("status=upcoming&amp;show=year", False, "Year"),
            ],
        )
        self.assertNotIn("from=", links)
        active = found[3][2].split()
        for name in (
            "bg-violet-50",
            "font-semibold",
            "text-violet-800",
            "ring-2",
            "ring-inset",
            "ring-violet-600",
        ):
            self.assertIn(name, active)
        for _, _, classes, _ in found:
            self.assertIn("min-h-11", classes.split())
            self.assertIn("px-1", classes.split())
            self.assertIn("sm:px-4", classes.split())
        # Five equal columns on phones, one joined group from sm up
        for name in ("grid", "w-full", "grid-cols-5", "sm:inline-flex", "sm:w-auto"):
            self.assertIn(name, group_class.split())
        # A search keeps q (and the "All" tab it forces)
        _, links = self._dates_group(self._html(self._get(q="Bantay", show="week")))
        self.assertIn('href="?status=all&amp;q=Bantay&amp;show=week"', links)
        # Only the phone Status dropdown still auto-submits
        self.assertEqual(html.count("data-auto-submit"), 1)
        self.assertIn(
            'id="status-select" name="status" class="input font-semibold" '
            "data-auto-submit",
            html,
        )
        # static() gives the URL the active storage renders (hashed with manifest)
        auto_submit = static("js/admin/auto_submit.js")
        self.assertIn(f'<script src="{auto_submit}" defer></script>', html)

    def test_pager_keeps_every_param(self):
        day = WALK_IN_DAY + datetime.timedelta(days=2)
        for i in range(26):
            Appointment.objects.create(
                owner=self.owner,
                pet=self.pet,
                date=day,
                time=datetime.time(8 + i // 4, (i % 4) * 15),
                status=Appointment.PENDING,
            )
        next_query = self._get(q="Bantay", show="week").context["next_page_query"]
        for part in (
            "status=all",
            "q=Bantay",
            "show=week",
            "from=2026-11-02",
            "page=2",
        ):
            self.assertIn(part, next_query)

    # ─── Search ───────────────────────────────────────────────────────────────

    def test_search_with_a_period(self):
        in_week = self._appointment(Appointment.PENDING, 3)
        self._appointment(Appointment.PENDING, 10)
        response = self._get(q="Bantay", show="week")
        self.assertEqual(response.context["active_tab"], "all")
        self.assertEqual(self._ids(response), [in_week.pk])
        self.assertContains(
            response,
            'Results for <strong class="font-semibold">“Bantay”</strong> '
            "in all appointments, this week: Nov 2 to Nov 8",
        )

    def test_search_with_a_period_and_no_match(self):
        response = self._get(q="Bantai", show="week")
        self.assertContains(response, "No appointments match “Bantai”")
        self.assertContains(
            response, "Check the spelling, pick another period, or clear the search."
        )

    def test_tab_links_keep_the_period_not_the_search(self):
        url = reverse("admin_appointments")
        response = self._get(q="Bantay", show="week")
        self.assertContains(
            response, f'href="{url}?status=pending&amp;show=week&amp;from=2026-11-02"'
        )
        self.assertNotIn("q=", self._status_nav(self._html(response)))

    def test_clear_search_keeps_the_period(self):
        url = reverse("admin_appointments")
        response = self._get(q="Bantay", show="week", from_="2026-11-09")
        self.assertEqual(
            response.context["clear_search_query"], "show=week&from=2026-11-09"
        )
        self.assertContains(response, f'href="{url}?show=week&amp;from=2026-11-09"')
        self.assertContains(response, "Clear search")
        response = self._get(q="Bantay", status="upcoming", show="day")
        self.assertEqual(
            response.context["clear_search_query"],
            "status=upcoming&show=day&from=2026-11-02",
        )

    def test_search_forms_carry_the_period(self):
        html = self._html(self._get(show="week", from_="2026-11-12"))
        forms = re.findall(r'<form [^>]*role="search">.*?</form>', html, re.S)
        self.assertEqual(len(forms), 2)
        for form in forms:
            self.assertIn('placeholder="Search appointments: pet or owner name"', form)
            self.assertIn('<input type="hidden" name="show" value="week">', form)
            self.assertIn('<input type="hidden" name="from" value="2026-11-09">', form)
            self.assertNotIn('name="status"', form)
            self.assertIn('class="sr-only">Search appointments</label>', form)

    def test_other_admin_pages_keep_the_global_search(self):
        response = self.client.get(reverse("admin_dashboard"))
        self.assertContains(response, 'id="global-search"')
        self.assertContains(response, "Find a pet or owner")
        self.assertNotContains(response, "Search appointments: pet or owner name")

    def test_phone_dropdown_is_labelled_status(self):
        html = self._html(self._get(show="week"))
        self.assertIn(
            '<label for="status-select" class="mb-1.5 block text-base font-medium '
            'text-gray-800">Status</label>',
            html,
        )
        form = self._form_with(html, "status-select")
        self.assertIn(
            '<select id="status-select" name="status" class="input font-semibold" '
            "data-auto-submit>",
            form,
        )
        self.assertIn('<input type="hidden" name="show" value="week">', form)
        self.assertIn(
            '<noscript><button type="submit" class="btn btn-outline">Apply</button>'
            "</noscript>",
            form,
        )

    # ─── Day cards and tabs ───────────────────────────────────────────────────

    def _card_classes(self, html, day):
        pattern = rf'<section class="([^"]*)" aria-labelledby="day-{day.isoformat()}">'
        match = re.search(pattern, html)
        self.assertIsNotNone(match)
        return match.group(1).split()

    def test_day_cards(self):
        self._appointment(Appointment.CONFIRMED, 0)
        self._appointment(Appointment.CONFIRMED, 0, hour=10)
        self._appointment(Appointment.CONFIRMED, 3)
        html = self._html(self._get(status="upcoming"))
        today_card = self._card_classes(html, WALK_IN_DAY)
        other_card = self._card_classes(html, D(2026, 11, 5))
        for name in ("border-violet-300", "ring-2", "ring-violet-200"):
            self.assertIn(name, today_card)
            self.assertNotIn(name, other_card)
        self.assertIn("border-gray-200", other_card)
        self.assertIn(
            '<h2 id="day-2026-11-02" class="text-xl font-semibold text-gray-900">'
            "Today, Monday, November 2</h2>",
            html,
        )
        self.assertIn(">Thursday, November 5</h2>", html)
        self.assertIn('<p class="text-base text-gray-700">2 appointments</p>', html)
        self.assertIn('<p class="text-base text-gray-700">1 appointment</p>', html)
        # One table per card with a hidden header; column names once above
        self.assertEqual(html.count('<table class="hidden w-full table-fixed'), 2)
        self.assertEqual(html.count('<span class="sr-only">Pet</span></th>'), 2)
        self.assertEqual(
            html.count('<div class="mb-2 hidden px-px md:block" aria-hidden="true">'), 1
        )

    def test_column_names_line_up_with_the_card_rows(self):
        self._appointment(Appointment.CONFIRMED, 0)
        self._appointment(Appointment.CONFIRMED, 3)
        html = self._html(self._get(status="upcoming"))
        # Every table (column names + 2 cards) is table-fixed with the same
        # colgroup, so the Open column has the same fixed width everywhere
        colgroup = re.findall(r"<colgroup>.*?</colgroup>", html, re.S)
        self.assertEqual(len(colgroup), 3)
        self.assertEqual(len(set(colgroup)), 1)
        self.assertEqual(
            re.findall(r'<col(?: class="([^"]*)")?>', colgroup[0]),
            ["w-32", "", "", "w-[18%]", "w-48", "w-40"],
        )
        self.assertEqual(html.count("table-fixed"), 3)
        # px-px gives the column-name table the cards' 1px border inset
        header = re.search(
            r'<div class="mb-2 hidden px-px md:block" aria-hidden="true">.*?</table>',
            html,
            re.S,
        ).group(0)
        self.assertEqual(header.count('<td class="px-5">'), 6)

    def test_open_button_is_light_violet(self):
        visit = self._appointment(Appointment.CONFIRMED, 1)
        html = self._html(self._get(status="upcoming"))
        url = reverse("admin_appointment_detail", args=[visit.pk])
        open_classes = (
            "inline-flex min-h-11 items-center justify-center gap-2 whitespace-nowrap "
            "rounded-lg border border-violet-300 bg-violet-50 px-5 text-base "
            "font-semibold text-violet-800"
        )
        self.assertIn(
            f'<a href="{url}" class="{open_classes} hover:border-violet-400 '
            'hover:bg-violet-100">Open<span class="sr-only">',
            html,
        )
        card = re.search(rf'<a href="{url}" class="block group.*?</a>', html, re.S)
        self.assertIsNotNone(card)
        self.assertIn(
            f'<span class="mt-3 {open_classes} group-hover:border-violet-400 '
            'group-hover:bg-violet-100">Open',
            card.group(0),
        )
        # The chevron is gone; the label has the arrow-right icon
        self.assertNotIn("m9 18 6-6-6-6", card.group(0))
        self.assertIn("m12 5 7 7-7 7", card.group(0))
        # New walk-in stays the only solid violet button
        self.assertEqual(len(re.findall(r"\bbtn-primary\b", html)), 1)
        self.assertNotIn("bg-violet-600 text-white", card.group(0))

    def test_day_card_in_another_year_shows_the_year(self):
        Appointment.objects.create(
            owner=self.owner,
            pet=self.pet,
            date=D(2025, 12, 1),
            time=datetime.time(9, 0),
            status=Appointment.COMPLETED,
        )
        self.assertContains(
            self._get(status="completed"), ">Monday, December 1, 2025</h2>"
        )

    def test_no_solid_violet_on_any_tab(self):
        self._appointment(Appointment.PENDING, 1)
        for status in ("pending", "upcoming", "all"):
            with self.subTest(status=status):
                nav = self._status_nav(self._html(self._get(status=status)))
                self.assertNotIn("bg-violet-600", nav)
                self.assertIn("ring-2 ring-violet-600", nav)
        self.assertRegex(
            self._html(self._get(status="upcoming")),
            r'aria-current="page">Upcoming<span class="[^"]*bg-violet-100 '
            r"text-violet-800",
        )
        # Needs approval's count stays yellow while requests wait
        self.assertRegex(
            self._html(self._get(status="pending")),
            r'aria-current="page">Needs approval<span class="[^"]*bg-yellow-200',
        )


class DeclinedReasonChoiceTests(SimpleTestCase):
    """Slice 8a-0: DECLINED, "Declined by the clinic", is a cancellation reason
    the clinic's decline will set (8a-1); no cancel form offers it."""

    def test_constant_value(self):
        self.assertEqual(Appointment.DECLINED, "declined")

    def test_label(self):
        appointment = Appointment(cancellation_reason=Appointment.DECLINED)
        self.assertEqual(
            appointment.get_cancellation_reason_display(), "Declined by the clinic"
        )

    def test_it_is_the_last_choice(self):
        self.assertEqual(
            Appointment.CANCEL_CHOICES[-1],
            (Appointment.DECLINED, "Declined by the clinic"),
        )

    def test_cancel_forms_do_not_offer_declined(self):
        from appointments.forms import (
            AdminAppointmentCancelForm,
            AppointmentCancellationForm,
        )

        for form_class in (AppointmentCancellationForm, AdminAppointmentCancelForm):
            with self.subTest(form=form_class.__name__):
                field = form_class().fields["cancellation_reason"]
                values = [value for value, _label in field.choices]
                self.assertNotIn(Appointment.DECLINED, values)
                self.assertEqual(
                    values,
                    [
                        Appointment.CHANGE_OF_PLANS,
                        Appointment.PET_UNWELL,
                        Appointment.CANNOT_MAKE_TIME,
                        Appointment.OTHER,
                    ],
                )


# ─── Slice 8a-1b: booking lock, atomic owner reschedule, declined reason ─────


class _OwnerRequestBase(AppointmentTestBase):
    """Helpers for the owner booking / reschedule / move / walk-in posts."""

    def setUp(self):
        super().setUp()
        self.day = self.today + datetime.timedelta(days=4)

    def _booking_data(self, **overrides):
        data = {
            "pet": self.pet.pk,
            "date": self.day.isoformat(),
            "time": "10:00:00",
            "reason": "Limping",
        }
        data.update(overrides)
        return data

    def _reschedule_data(self, **overrides):
        data = {
            "date": self.day.isoformat(),
            "time": "10:00:00",
            "reschedule_reason": "Need to move it",
        }
        data.update(overrides)
        return data

    def _owner_books(self, **overrides):
        # Slice 8b-6: the booking is three steps now (was one POST)
        self.client.force_login(self.owner.user)
        data = self._booking_data(**overrides)
        return _book_through_the_steps(
            self.client,
            self.pet,
            datetime.date.fromisoformat(data["date"]),
            data["time"],
            reason=data["reason"],
        )

    def _owner_reschedules(self, appointment, **overrides):
        self.client.force_login(self.owner.user)
        return self.client.post(
            reverse("owner_appointment_reschedule", args=[appointment.pk]),
            self._reschedule_data(**overrides),
        )

    def _admin_moves(self, appointment):
        return self.client.post(
            reverse("admin_appointment_reschedule", args=[appointment.pk]),
            self._reschedule_data(),
        )

    def _walk_in(self):
        return self.client.post(
            reverse("admin_walkin_appointment_visit"),
            {
                "owner": self.owner.pk,
                "pet": self.pet.pk,
                "date": self.day.isoformat(),
                "time": "10:00:00",
                "reason": "Limping",
                "action": "book",
            },
        )


class ScheduleLockTests(_OwnerRequestBase):
    """Every path that makes a PENDING or CONFIRMED visit runs lock_schedule()
    inside its transaction, before it checks the slot again."""

    def test_owner_booking_locks(self):
        with mock.patch("appointments.utils.lock_schedule") as lock:
            self._owner_books()
        lock.assert_called_once_with()
        self.assertEqual(Appointment.objects.count(), 1)

    def test_owner_reschedule_locks(self):
        original = self._appointment(Appointment.PENDING)
        with mock.patch("appointments.utils.lock_schedule") as lock:
            self._owner_reschedules(original)
        lock.assert_called_once_with()
        self.assertEqual(Appointment.objects.count(), 2)

    def test_walk_in_locks(self):
        with mock.patch("appointments.utils.lock_schedule") as lock:
            self._walk_in()
        lock.assert_called_once_with()
        self.assertTrue(Appointment.objects.get().is_walk_in)

    def test_admin_move_locks(self):
        original = self._appointment(Appointment.CONFIRMED)
        with mock.patch("appointments.views.lock_schedule") as lock:
            self._admin_moves(original)
        lock.assert_called_once_with()
        self.assertEqual(Appointment.objects.count(), 2)

    def test_lock_is_a_select_for_update_on_the_settings_row(self):
        with transaction.atomic(), CaptureQueriesContext(connection) as queries:
            lock_schedule()
        locking = [q["sql"] for q in queries if "FOR UPDATE" in q["sql"]]
        self.assertEqual(len(locking), 1)
        self.assertIn("appointments_clinicsettings", locking[0])

    def test_no_settings_row_means_no_lock_and_no_error(self):
        ClinicSettings.objects.all().delete()
        with transaction.atomic():
            lock_schedule()  # must not raise


class SlotTakenAtSaveTests(_OwnerRequestBase):
    """The form's check passed, but the slot is gone by the time the save
    runs under the lock: patching the re-check in appointments.utils (the form
    keeps its own, real check) is that moment."""

    MESSAGE = (
        "10:00 AM was just taken. Someone else booked it a moment ago. "
        "Please pick another time."
    )

    def _taken(self):
        return mock.patch("appointments.utils.is_slot_available", return_value=False)

    def test_owner_booking_comes_back_with_the_time_error(self):
        # Slice 8b-6: Send comes back to step 2 with the red "was just taken"
        # box (was: the one form again, with the error on its time field).
        # Same behaviour: every value kept but the time, nothing saved, the
        # admin not told.
        with self._taken():
            response = self._owner_books()
        self.assertRedirects(
            response, reverse("owner_book_time"), fetch_redirect_response=False
        )
        page = self.client.get(reverse("owner_book_time"))
        self.assertContains(page, "10:00 AM was just taken")
        self.assertContains(
            page,
            "Someone booked it a moment ago. Please pick another time. "
            "Your other answers are kept.",
        )
        draft = self.client.session["owner_booking"]
        self.assertEqual(draft["pet"], self.pet.pk)
        self.assertEqual(draft["date"], self.day.isoformat())
        self.assertEqual(draft["note"], "Limping")
        self.assertNotIn("time", draft)
        self.assertFalse(Appointment.objects.exists())
        self.assertFalse(Notification.objects.filter(recipient=self.admin).exists())

    def test_owner_reschedule_comes_back_with_the_time_error(self):
        original = self._appointment(Appointment.CONFIRMED)
        with self._taken():
            response = self._owner_reschedules(original)
        # Slice 8b-7b: the page has no Django form any more. Was: the form's
        # time error was the long MESSAGE and its data held the day, the text
        # and an empty time. Now: the red "was just taken" box, the day kept,
        # the note kept and no time picked. Same behaviour.
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["taken_time"], "10:00 AM")
        self.assertContains(response, "10:00 AM was just taken")
        self.assertContains(response, "Your note is kept.")
        self.assertEqual(response.context["picked_date"], self.day)
        self.assertEqual(response.context["reason_text"], "Need to move it")
        self.assertEqual(response.context["new_time"], "")
        # The original is untouched, and nothing new exists
        self._assert_status(original, Appointment.CONFIRMED)
        self.assertEqual(original.cancellation_reason, "")
        self.assertIsNone(original.cancelled_by)
        self.assertEqual(Appointment.objects.count(), 1)

    def test_walk_in_keeps_its_taken_handling(self):
        with mock.patch("appointments.utils.slot_is_booked", return_value=True):
            response = self._walk_in()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["taken_time"], "10:00 AM")
        self.assertFalse(Appointment.objects.exists())
        self.assertFalse(
            Notification.objects.filter(recipient=self.owner.user).exists()
        )

    def test_admin_move_keeps_its_slot_error(self):
        original = self._appointment(Appointment.CONFIRMED)
        with mock.patch("appointments.views.is_slot_available", return_value=False):
            response = self._admin_moves(original)
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "This slot is no longer available. Please select another.",
            str(response.context["reschedule_form"].errors),
        )
        self._assert_status(original, Appointment.CONFIRMED)
        self.assertEqual(Appointment.objects.count(), 1)


class OwnerRescheduleAtomicTests(_OwnerRequestBase):
    """The owner's reschedule cancels the original and creates the new visit
    in one transaction: both happen or neither."""

    def test_the_fields_are_the_same_as_before(self):
        original = self._appointment(Appointment.CONFIRMED)
        service = Service.objects.create(service_name="Checkup", status=Service.ACTIVE)
        original.service = service
        original.reason = "Itchy skin"
        original.save()
        response = self._owner_reschedules(original)
        new = Appointment.objects.exclude(pk=original.pk).get()
        self.assertRedirects(
            response,
            reverse("owner_appointment_detail", args=[new.pk]),
            fetch_redirect_response=False,
        )
        original.refresh_from_db()
        self.assertEqual(original.status, Appointment.CANCELLED)
        self.assertEqual(original.cancellation_reason, Appointment.OTHER)
        self.assertEqual(original.cancellation_detail, "Rescheduled by pet owner.")
        self.assertEqual(original.cancelled_by, self.owner.user)
        self.assertEqual(new.status, Appointment.PENDING)
        self.assertEqual(new.owner, self.owner)
        self.assertEqual(new.pet, self.pet)
        self.assertEqual(new.service, service)
        self.assertEqual((new.date, new.time), (self.day, datetime.time(10, 0)))
        self.assertEqual(new.reason, "Itchy skin")
        self.assertEqual(new.reschedule_reason, "Need to move it")
        self.assertEqual(new.rescheduled_from, original)
        self.assertEqual(
            self._messages(response),
            # Slice 8b-7b: was "Reschedule request submitted. You will be
            # notified once it is confirmed."
            ["New time sent. The clinic will confirm it soon."],
        )
        self.assertTrue(
            Notification.objects.filter(
                recipient=self.admin,
                title="Appointment Reschedule Request",
                related_appointment=new,
            ).exists()
        )

    def test_a_failed_create_leaves_the_original_as_it_was(self):
        for status, hour in ((Appointment.PENDING, 9), (Appointment.CONFIRMED, 11)):
            with self.subTest(status=status):
                original = self._appointment(status, hour=hour)
                with mock.patch.object(
                    Appointment.objects, "create", side_effect=RuntimeError("boom")
                ):
                    with self.assertRaises(RuntimeError):
                        self._owner_reschedules(original)
                self._assert_status(original, status)
                self.assertEqual(original.cancellation_reason, "")
                self.assertIsNone(original.cancelled_by)
                self.assertFalse(
                    Appointment.objects.filter(rescheduled_from=original).exists()
                )

    def test_an_original_cancelled_meanwhile_changes_nothing(self):
        original = self._appointment(Appointment.PENDING)

        def cancelled_by_someone_else():
            Appointment.objects.filter(pk=original.pk).update(
                status=Appointment.CANCELLED
            )

        with mock.patch(
            "appointments.utils.lock_schedule", side_effect=cancelled_by_someone_else
        ):
            response = self._owner_reschedules(original)
        self.assertRedirects(
            response,
            reverse("owner_appointment_detail", args=[original.pk]),
            fetch_redirect_response=False,
        )
        self.assertEqual(
            self._messages(response), ["This visit can't be changed any more."]
        )
        self.assertEqual(Appointment.objects.count(), 1)
        self.assertFalse(Notification.objects.filter(recipient=self.admin).exists())

    def test_nothing_is_created_when_the_original_is_not_changeable(self):
        data = {
            "date": self.day,
            "time": datetime.time(10, 0),
            "reschedule_reason": "x",
        }
        other_user = User.objects.create_user(
            username="other", email="other@example.com", password="unused-pass-123"
        )
        other = PetOwner.objects.create(
            user=other_user, first_name="Bo", last_name="Li"
        )
        for status in (
            Appointment.CANCELLED,
            Appointment.COMPLETED,
            Appointment.NO_SHOW,
        ):
            with self.subTest(status=status):
                original = self._appointment(status)
                self.assertIsNone(
                    reschedule_by_owner(self.owner.user, self.owner, original.pk, data)
                )
        # Not this owner's visit
        pending = self._appointment(Appointment.PENDING, hour=14)
        self.assertIsNone(reschedule_by_owner(other_user, other, pending.pk, data))
        self._assert_status(pending, Appointment.PENDING)
        self.assertFalse(Appointment.objects.filter(rescheduled_from__isnull=False))


class DeclinedReasonInUseTests(_OwnerRequestBase):
    """The clinic's decline is stored as DECLINED; the owner reads
    "Declined by the clinic". Old rows (reason Other) keep reading Other."""

    def _decline(self, **data):
        appointment = self._appointment(Appointment.PENDING)
        self._post("admin_appointment_reject", appointment, data)
        appointment.refresh_from_db()
        return appointment

    def test_decline_saves_declined(self):
        appointment = self._decline(message="The vet is away.")
        self.assertEqual(appointment.status, Appointment.CANCELLED)
        self.assertEqual(appointment.cancellation_reason, Appointment.DECLINED)
        self.assertEqual(appointment.cancellation_detail, "The vet is away.")
        self.assertEqual(appointment.cancelled_by, self.admin)

    def test_default_detail_and_notification_are_unchanged(self):
        appointment = self._decline()
        self.assertEqual(appointment.cancellation_detail, "Rejected by clinic.")
        notification = Notification.objects.get(
            recipient=self.owner.user, notification_type="appointment_rejected"
        )
        # 8a-2b: title was "Appointment Not Confirmed"; "was not confirmed by
        # the clinic." moved to the email body, the app text is compact
        self.assertEqual(notification.title, "Visit request declined")
        self.assertIn("Bantay · ", notification.message)
        self.assertIn("was not confirmed by the clinic.", mail.outbox[-1].body)

    def test_owner_visit_page_says_declined_by_the_clinic(self):
        appointment = self._decline()
        self.client.force_login(self.owner.user)
        response = self.client.get(
            reverse("owner_appointment_detail", args=[appointment.pk])
        )
        self.assertContains(response, "Declined by the clinic")

    def test_a_clinic_cancel_and_an_old_row_do_not_say_declined(self):
        cancelled = self._appointment(Appointment.CONFIRMED, hour=11)
        self._post(
            "admin_appointment_cancel",
            cancelled,
            {
                "cancellation_reason": Appointment.OTHER,
                "cancellation_detail": "Closed",
            },
        )
        old = self._appointment(Appointment.CANCELLED, hour=13)
        old.cancellation_reason = Appointment.OTHER  # a decline from before 8a-1b
        old.cancelled_by = self.admin
        old.save()
        self.client.force_login(self.owner.user)
        for appointment in (cancelled, old):
            with self.subTest(pk=appointment.pk):
                response = self.client.get(
                    reverse("owner_appointment_detail", args=[appointment.pk])
                )
                self.assertNotContains(response, "Declined by the clinic")
                # Slice 8b-7a: the new visit page says who cancelled, not the
                # reason's label ("Other"); the check is the same: not declined
                self.assertContains(response, "Cancelled by the clinic")


# ─── Slice 8a-2a: owner late note, booking calendar, pet picked ──────────────

# 00:30 Manila on Mon, Nov 2, 2026: still Sun, Nov 1 in UTC
JUST_AFTER_MIDNIGHT = _manila(0, 30)


class JustAfterMidnightBase(FrozenNowTestBase):
    NOW = JUST_AFTER_MIDNIGHT

    def _owner_logs_in(self):
        self.client.force_login(self.owner.user)


class IsWithin24hTests(JustAfterMidnightBase):
    """The one late rule: a start less than 24 hours after now (or already
    passed) is within; exactly 24 hours away is not."""

    def _visit(self, minutes_from_now):
        start = JUST_AFTER_MIDNIGHT + datetime.timedelta(minutes=minutes_from_now)
        return Appointment(
            owner=self.owner, pet=self.pet, date=start.date(), time=start.time()
        )

    def test_the_edges(self):
        cases = [
            (24 * 60 - 1, True),  # 23:59 away
            (24 * 60, False),  # 24:00 away
            (24 * 60 + 1, False),  # 24:01 away
            (0, True),  # starts right now
            (-30, True),  # already started
            (-3 * 24 * 60, True),  # days ago
        ]
        for minutes, expected in cases:
            with self.subTest(minutes=minutes):
                visit = self._visit(minutes)
                self.assertIs(is_within_24h(visit, timezone.now()), expected)

    def test_the_property_and_is_cancellable_freely_agree(self):
        for minutes in (24 * 60 - 1, 24 * 60, 24 * 60 + 1, -30):
            with self.subTest(minutes=minutes):
                visit = self._visit(minutes)
                expected = is_within_24h(visit, timezone.now())
                self.assertIs(visit.is_within_24h, expected)
                self.assertIs(visit.is_cancellable_freely, not expected)

    def _is_late(self, url_name, appointment, **post):
        self._owner_logs_in()
        url = reverse(url_name, args=[appointment.pk])
        if post:
            response = self.client.post(url, post["data"], follow=True)
        else:
            response = self.client.get(url, follow=True)
        # Slice 8b-7a: Cancel is a POST-only sheet on the visit page (a GET or
        # a failed POST goes back to it), so its late flag is read from that
        # page's sheet. Change time still renders its own page with is_late.
        if url_name == "owner_appointment_cancel":
            return response.context["sheet"]["late"]
        return response.context["is_late"]

    def test_cancel_and_reschedule_pass_is_late_on_get(self):
        soon = self._appointment(Appointment.CONFIRMED, days_from_today=0, hour=9)
        far = self._appointment(Appointment.CONFIRMED, days_from_today=3, hour=9)
        for name in ("owner_appointment_cancel", "owner_appointment_reschedule"):
            with self.subTest(view=name):
                self.assertIs(self._is_late(name, soon), True)
                self.assertIs(self._is_late(name, far), False)

    def test_cancel_and_reschedule_pass_is_late_on_a_failed_post(self):
        soon = self._appointment(Appointment.CONFIRMED, days_from_today=0, hour=9)
        far = self._appointment(Appointment.CONFIRMED, days_from_today=3, hour=9)
        for name in ("owner_appointment_cancel", "owner_appointment_reschedule"):
            with self.subTest(view=name):
                self.assertIs(self._is_late(name, soon, data={}), True)
                self.assertIs(self._is_late(name, far, data={}), False)


class OwnerBookingCalendarTests(JustAfterMidnightBase):
    """owner_booking_calendar on Mon, Nov 2, 2026 at 00:30 Manila. The clinic
    is open every day 8:00-17:00 (hourly), cutoff 15:00, window 30 days."""

    def _cal(self, month="2026-11", current=None):
        return owner_booking_calendar(
            month, timezone.localdate(), timezone.localtime(), current
        )

    def _cell(self, calendar, day):
        return next(c for c in calendar["cells"] if c["date"] == day)

    def _state(self, day, month="2026-11"):
        return self._cell(self._cal(month), day)["state"]

    def _fill(self, day, status=Appointment.CONFIRMED, hours=range(8, 17)):
        for hour in hours:
            Appointment.objects.create(
                owner=self.owner,
                pet=self.pet,
                date=day,
                time=datetime.time(hour, 0),
                status=status,
            )

    def test_weeks_start_on_sunday(self):
        for month in ("2026-11", "2026-12"):
            with self.subTest(month=month):
                cells = self._cal(month)["cells"]
                self.assertEqual(cells[0]["date"].weekday(), 6)
                self.assertEqual(len(cells) % 7, 0)
        # Nov 1, 2026 is a Sunday: the grid opens on the 1st
        self.assertEqual(self._cal()["cells"][0]["date"], datetime.date(2026, 11, 1))
        # Dec 1 is a Tuesday: the grid opens on Sun, Nov 29, out of the month
        first = self._cal("2026-12")["cells"][0]
        self.assertEqual(first["date"], datetime.date(2026, 11, 29))
        self.assertFalse(first["in_month"])

    def test_35_or_42_cells(self):
        ClinicSettings.objects.update(booking_limit_days=365)
        sizes = {
            month: len(self._cal(month)["cells"])
            for month in ("2026-11", "2026-12", "2027-01", "2027-02")
        }
        # Jan 2027 starts on a Friday and has 31 days: six weeks
        self.assertEqual(
            sizes, {"2026-11": 35, "2026-12": 35, "2027-01": 42, "2027-02": 35}
        )

    def test_cell_keys_and_month_label(self):
        calendar = self._cal()
        self.assertEqual(calendar["month_label"], "November 2026")
        self.assertEqual(calendar["month_value"], "2026-11")
        cell = self._cell(calendar, datetime.date(2026, 11, 4))
        self.assertEqual(
            set(cell),
            {
                "date",
                "day",
                "in_month",
                "state",
                "is_today",
                "is_current",
                "aria_label",
            },
        )
        self.assertTrue(cell["in_month"])

    def test_past_days_and_days_after_the_window(self):
        self.assertEqual(self._state(datetime.date(2026, 11, 1)), "past")
        self.assertEqual(self._state(datetime.date(2026, 12, 2), "2026-12"), "open")
        self.assertEqual(self._state(datetime.date(2026, 12, 3), "2026-12"), "past")

    def test_closed_for_a_day_off_and_a_blocked_date(self):
        ClinicSettings.objects.update(working_days=[0, 1, 2, 3, 4, 5])  # no Sunday
        BlockedDate.objects.create(date=datetime.date(2026, 11, 10))
        self.assertEqual(self._state(datetime.date(2026, 11, 8)), "closed")
        self.assertEqual(self._state(datetime.date(2026, 11, 10)), "closed")
        self.assertEqual(self._state(datetime.date(2026, 11, 9)), "open")

    def test_full_when_every_slot_is_held(self):
        day = datetime.date(2026, 11, 12)
        self._fill(day, Appointment.PENDING, hours=range(8, 16))
        self.assertEqual(self._state(day), "open")  # the 16:00 slot is free
        self._fill(day, Appointment.PENDING, hours=[16])
        self.assertEqual(self._state(day), "full")

    def test_a_cancelled_visit_frees_its_slot(self):
        day = datetime.date(2026, 11, 12)
        self._fill(day)
        Appointment.objects.filter(date=day, time=datetime.time(10, 0)).update(
            status=Appointment.CANCELLED
        )
        self.assertEqual(self._state(day), "open")

    def test_today_follows_the_same_day_cutoff(self):
        today = datetime.date(2026, 11, 2)
        self.assertEqual(self._state(today), "open")
        self.freeze(_manila(14, 30))  # slots at 15:00 and 16:00 still start later
        self.assertEqual(self._state(today), "open")
        self.freeze(_manila(15, 0))  # the cutoff
        self.assertEqual(self._state(today), "full")

    def test_today_is_full_when_every_slot_has_started(self):
        ClinicSettings.objects.update(same_day_cutoff_time=datetime.time(17, 0))
        self.freeze(_manila(16, 30))
        self.assertEqual(self._state(datetime.date(2026, 11, 2)), "full")

    def test_aria_labels(self):
        ClinicSettings.objects.update(working_days=[0, 1, 2, 3, 4, 5])
        self._fill(datetime.date(2026, 11, 12))
        calendar = self._cal()
        expected = {
            datetime.date(2026, 11, 12): "Thursday, November 12: full",
            datetime.date(2026, 11, 8): "Sunday, November 8: clinic closed",
            datetime.date(2026, 11, 4): "Wednesday, November 4: open",
        }
        for day, label in expected.items():
            with self.subTest(day=day):
                self.assertEqual(self._cell(calendar, day)["aria_label"], label)

    def test_is_today_and_is_current(self):
        calendar = self._cal(current=datetime.date(2026, 11, 12))
        self.assertEqual(
            [c["date"] for c in calendar["cells"] if c["is_current"]],
            [datetime.date(2026, 11, 12)],
        )
        self.assertEqual(
            [c["date"] for c in calendar["cells"] if c["is_today"]],
            [datetime.date(2026, 11, 2)],
        )
        self.assertFalse(any(c["is_current"] for c in self._cal()["cells"]))

    def test_the_month_is_clamped_and_bad_values_open_today_s_month(self):
        cases = {
            "2026-11": "2026-11",
            "2026-12": "2026-12",
            "2026-01": "2026-11",  # before today's month
            "2030-05": "2026-12",  # after the last bookable month
            "2026-13": "2026-11",
            "2026-00": "2026-11",
            "0000-01": "2026-11",
            "2026-1": "2026-11",
            " 2026-12": "2026-11",
            "garbage": "2026-11",
            "": "2026-11",
            None: "2026-11",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(self._cal(value)["month_value"], expected)

    def test_prev_and_next_are_none_at_the_ends(self):
        first, last = self._cal("2026-11"), self._cal("2026-12")
        self.assertIsNone(first["prev_month"])
        self.assertEqual(first["next_month"], "2026-12")
        self.assertEqual(last["prev_month"], "2026-11")
        self.assertIsNone(last["next_month"])
        ClinicSettings.objects.update(booking_limit_days=120)
        middle = self._cal("2026-12")
        self.assertEqual(
            (middle["prev_month"], middle["next_month"]), ("2026-11", "2027-01")
        )

    def test_every_day_agrees_with_get_available_slots(self):
        ClinicSettings.objects.update(working_days=[0, 1, 2, 3, 4, 5])
        BlockedDate.objects.create(date=datetime.date(2026, 11, 10))
        self._fill(datetime.date(2026, 11, 12))
        self._fill(datetime.date(2026, 11, 13), Appointment.CANCELLED)
        self._fill(datetime.date(2026, 11, 14), Appointment.PENDING, hours=range(8, 16))
        self._fill(datetime.date(2026, 11, 2), hours=range(10, 17))
        for now in (_manila(0, 30), _manila(9, 30), _manila(14, 59), _manila(15, 30)):
            self.freeze(now)
            for month in ("2026-11", "2026-12"):
                for cell in self._cal(month)["cells"]:
                    with self.subTest(now=now.time(), day=cell["date"]):
                        has_slots = bool(get_available_slots(cell["date"]))
                        self.assertEqual(cell["state"] == "open", has_slots)

    def test_the_query_count_is_flat(self):
        def queries():
            with CaptureQueriesContext(connection) as captured:
                self._cal()
            return len(captured)

        empty = queries()
        for day in range(2, 28):
            self._fill(datetime.date(2026, 11, day))
        BlockedDate.objects.create(date=datetime.date(2026, 11, 30))
        self.assertEqual(queries(), empty)
        self.assertEqual(empty, 3)  # settings, blocked dates, held slots


class BookWithPetParamTests(JustAfterMidnightBase):
    """owner_book_appointment starts the form with ?pet= when it is one of
    the owner's non-archived pets; anything else is ignored."""

    def setUp(self):
        super().setUp()
        self.second = Pet.objects.create(
            owner=self.owner, name="Mingming", species="Cat"
        )
        self._owner_logs_in()

    def _picked(self, value):
        response = self.client.get(reverse("owner_book_appointment"), {"pet": value})
        self.assertEqual(response.status_code, 200)
        return response.context["form"]["pet"].value()

    def test_own_pet_is_picked(self):
        self.assertEqual(self._picked(self.second.pk), self.second.pk)

    def test_without_the_parameter_no_pet_is_picked(self):
        response = self.client.get(reverse("owner_book_appointment"))
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["form"]["pet"].value())

    def test_another_owners_pet_is_ignored(self):
        other_user = User.objects.create_user(
            username="other",
            email="other@example.com",
            password="unused-pass-123",
            role=User.PET_OWNER,
            is_onboarded=True,
        )
        other = PetOwner.objects.create(
            user=other_user, first_name="Ben", last_name="Reyes"
        )
        theirs = Pet.objects.create(owner=other, name="Rex", species="Dog")
        self.assertIsNone(self._picked(theirs.pk))

    def test_an_archived_pet_is_ignored(self):
        # Slice 8b-6: a lone pet is pre-picked now, so the owner gets one more
        # active pet: two remain, and nothing is picked
        Pet.objects.create(owner=self.owner, name="Choco", species="Dog")
        Pet.objects.filter(pk=self.second.pk).update(is_archived=True)
        self.assertIsNone(self._picked(self.second.pk))

    def test_a_lone_pet_is_pre_picked_and_an_archived_one_never_is(self):
        Pet.objects.filter(pk=self.second.pk).update(is_archived=True)
        for value in (None, self.second.pk, "junk"):
            with self.subTest(value=value):
                params = {} if value is None else {"pet": value}
                response = self.client.get(reverse("owner_book_appointment"), params)
                self.assertEqual(response.context["form"]["pet"].value(), self.pet.pk)

    def test_junk_is_ignored(self):
        for value in ("abc", "", "-1", "0", "1.5", "99999999999999999999", "999999"):
            with self.subTest(value=value):
                self.assertIsNone(self._picked(value))


# ─── Slice 8b-6: owner booking in three steps ────────────────────────────────

VIOLET_BUTTON = r"<(?:a|button)\b[^>]*\bbg-violet-600\b[^>]*>"
# Tuesday, Nov 10, 2026: eight days after the frozen "today" (Mon, Nov 2)
BOOK_DAY = datetime.date(2026, 11, 10)


class _OwnerBookingBase(JustAfterMidnightBase):
    """Mon, Nov 2, 2026 at 00:30 Manila; the clinic is open every day,
    8:00-17:00 hourly, for 30 days. The owner is logged in and has one pet
    (Bantay) and one Active service."""

    def setUp(self):
        super().setUp()
        self._owner_logs_in()
        self.service = Service.objects.create(
            service_name="Consultation", status=Service.ACTIVE
        )
        self.step1 = reverse("owner_book_appointment")
        self.step1_back = f"{self.step1}?back=1"
        self.step2 = reverse("owner_book_time")
        self.step3 = reverse("owner_book_check")

    def _set_draft(self, **changes):
        """Writes a full draft into the session (a key set to None is left
        out) and returns it."""
        draft = {
            "pet": self.pet.pk,
            "service": str(self.service.pk),
            "note": "Limping",
            "date": BOOK_DAY.isoformat(),
            "time": "12:00:00",
        }
        draft.update(changes)
        draft = {key: value for key, value in draft.items() if value is not None}
        session = self.client.session
        session["owner_booking"] = draft
        session.save()
        return draft

    def _draft(self):
        return self.client.session.get("owner_booking")

    def _html(self, url, **params):
        return self.client.get(url, params).content.decode()

    def _other_owners_pet(self):
        user = User.objects.create_user(
            username="other",
            email="other@example.com",
            password="unused-pass-123",
            role=User.PET_OWNER,
            is_onboarded=True,
        )
        other = PetOwner.objects.create(user=user, first_name="Ben", last_name="Reyes")
        return Pet.objects.create(owner=other, name="Rex", species="Dog")

    def _hold(self, day, hours, status=Appointment.CONFIRMED):
        """Visits (by the owner's pet) holding the slots of day at hours."""
        for hour in hours:
            Appointment.objects.create(
                owner=self.owner,
                pet=self.pet,
                date=day,
                time=datetime.time(hour, 0),
                status=status,
            )


class OwnerBookingDraftTests(_OwnerBookingBase):
    """The session draft: fresh on a plain GET of step 1, kept with ?back=1,
    and every step checks the whole draft again."""

    def _assert_back(self, response, url, message):
        self.assertRedirects(response, url, fetch_redirect_response=False)
        self.assertEqual(self._messages(response), [message])
        # A page that shows the flash uses it up, so the next check starts clean
        self.client.get(reverse("owner_dashboard"))

    # ── Starting and keeping ────────────────────────────────────────────────

    def test_a_plain_get_starts_a_fresh_draft(self):
        self._set_draft()
        self.client.get(self.step1)
        self.assertEqual(self._draft(), {"pet": None, "from_pet": False})

    def test_pet_param_picks_an_own_pet(self):
        self.client.get(self.step1, {"pet": self.pet.pk})
        self.assertEqual(self._draft(), {"pet": self.pet.pk, "from_pet": True})

    def test_pet_param_of_another_owner_or_an_archived_pet_is_ignored(self):
        theirs = self._other_owners_pet()
        archived = Pet.objects.create(
            owner=self.owner, name="Old", species="Cat", is_archived=True
        )
        for value in (theirs.pk, archived.pk, "junk"):
            with self.subTest(value=value):
                self.client.get(self.step1, {"pet": value})
                self.assertEqual(self._draft(), {"pet": None, "from_pet": False})

    def test_back_keeps_the_draft_and_the_form_shows_it(self):
        draft = self._set_draft(note="Keep this")
        response = self.client.get(self.step1, {"back": "1"})
        self.assertEqual(self._draft(), draft)
        form = response.context["form"]
        self.assertEqual(form["pet"].value(), self.pet.pk)
        self.assertEqual(form["service"].value(), str(self.service.pk))
        self.assertEqual(form["reason"].value(), "Keep this")

    def test_back_without_a_draft_starts_one(self):
        self.client.get(self.step1, {"back": "1"})
        self.assertEqual(self._draft(), {"pet": None, "from_pet": False})

    def test_step_1_post_saves_the_answers_and_keeps_the_day_and_time(self):
        self._set_draft()
        response = self.client.post(
            self.step1, {"pet": self.pet.pk, "service": "none", "reason": "New"}
        )
        self.assertRedirects(response, self.step2, fetch_redirect_response=False)
        draft = self._draft()
        self.assertEqual((draft["service"], draft["note"]), ("none", "New"))
        self.assertEqual(draft["date"], BOOK_DAY.isoformat())
        self.assertEqual(draft["time"], "12:00:00")
        self.assertFalse(Appointment.objects.exists())

    # ── Every step checks the whole draft again ─────────────────────────────

    def test_no_draft_starts_at_step_1(self):
        for url in (self.step2, self.step3):
            with self.subTest(url=url):
                response = self.client.get(url)
                self._assert_back(response, self.step1_back, START_MESSAGE)

    def test_another_owners_pet_and_an_archived_pet_go_back_to_step_1(self):
        archived = Pet.objects.create(
            owner=self.owner, name="Old", species="Cat", is_archived=True
        )
        for pet in (self._other_owners_pet(), archived):
            for url in (self.step2, self.step3):
                with self.subTest(pet=pet.name, url=url):
                    self._set_draft(pet=pet.pk)
                    response = self.client.get(url)
                    self._assert_back(response, self.step1_back, PET_GONE_MESSAGE)

    def test_a_service_that_is_not_bookable_goes_back_to_step_1(self):
        for status in (Service.DRAFT, Service.UNLISTED, Service.CLOSED):
            Service.objects.filter(pk=self.service.pk).update(status=status)
            for url in (self.step2, self.step3):
                with self.subTest(status=status, url=url):
                    self._set_draft()
                    response = self.client.get(url)
                    self._assert_back(response, self.step1_back, SERVICE_GONE_MESSAGE)

    def test_no_service_answer_goes_back_to_step_1(self):
        self._set_draft(service=None)
        response = self.client.get(self.step2)
        self._assert_back(response, self.step1_back, SERVICE_MISSING_MESSAGE)

    def test_a_day_that_cannot_be_booked_goes_back_to_step_2(self):
        ClinicSettings.objects.update(working_days=[0, 2, 3, 4, 5, 6])  # no Tuesday
        closed = (BOOK_DAY.isoformat(), DAY_GONE_MESSAGE)
        blocked_day = datetime.date(2026, 11, 11)
        BlockedDate.objects.create(date=blocked_day)
        full_day = datetime.date(2026, 11, 12)
        self._hold(full_day, range(8, 17))
        cases = {
            "closed": closed,
            "blocked": (blocked_day.isoformat(), DAY_GONE_MESSAGE),
            "full": (full_day.isoformat(), DAY_FULL_MESSAGE),
            "past": ("2026-11-01", DAY_GONE_MESSAGE),
            "beyond the limit": ("2026-12-03", DAY_GONE_MESSAGE),
            "not a date": ("junk", PICK_AGAIN_MESSAGE),
        }
        for name, (day, message) in cases.items():
            with self.subTest(case=name):
                self._set_draft(date=day)
                self._assert_back(self.client.get(self.step3), self.step2, message)
                self._assert_back(self.client.post(self.step3), self.step2, message)
        self.assertFalse(Appointment.objects.filter(status="pending").exists())

    def test_no_time_goes_back_to_step_2(self):
        self._set_draft(time=None)
        response = self.client.get(self.step3)
        self._assert_back(response, self.step2, PICK_AGAIN_MESSAGE)

    def test_a_taken_time_goes_back_to_step_2_with_the_red_box(self):
        self._hold(BOOK_DAY, [12])
        self._set_draft()
        response = self.client.get(self.step3)
        self.assertRedirects(response, self.step2, fetch_redirect_response=False)
        self.assertEqual(self._messages(response), [])
        draft = self._draft()
        self.assertEqual(draft["taken"], "12:00 PM")
        self.assertNotIn("time", draft)
        self.assertContains(self.client.get(self.step2), "12:00 PM was just taken")

    def test_a_time_that_is_not_a_slot_goes_back_to_step_2(self):
        self._set_draft(time="12:17:00")
        response = self.client.get(self.step3)
        self.assertRedirects(response, self.step2, fetch_redirect_response=False)
        self.assertEqual(len(self._messages(response)), 1)


class OwnerBookingStepPagesTests(_OwnerBookingBase):
    """The three step pages: the flow frame, step 1 cards, the calendar and
    times, the check page, field errors, markup rules and query counts."""

    def _bar(self, html):
        return re.search(r'(?s)<header class="sticky.*?</header>', html).group(0)

    def _pages(self):
        """(name, html) for each booking page with a full draft."""
        self._set_draft()
        return [
            ("step 1", self._html(self.step1, back="1")),
            ("step 2", self._html(self.step2)),
            ("step 3", self._html(self.step3)),
        ]

    # ── The flow frame ──────────────────────────────────────────────────────

    def test_flow_bar_step_names_and_progress_values(self):
        self._set_draft()
        cases = [
            (self.step1_back, 1, "Pet and reason"),
            (self.step2, 2, "Date and time"),
            (self.step3, 3, "Check and send"),
        ]
        for url, step, name in cases:
            with self.subTest(step=step):
                html = self.client.get(url).content.decode()
                self.assertIn(f"Step {step} of 3</span> · {name}", html)
                self.assertIn('role="progressbar"', html)
                self.assertIn('aria-valuemin="1" aria-valuemax="3"', html)
                self.assertIn(f'aria-valuenow="{step}"', html)
                bar = re.search(r'(?s)role="progressbar".*?</div>', html).group(0)
                self.assertEqual(bar.count("bg-violet-600"), step)

    def test_back_and_cancel_links(self):
        self._set_draft()
        step1 = self._bar(self._html(self.step1, back="1"))
        step2 = self._bar(self._html(self.step2))
        step3 = self._bar(self._html(self.step3))
        dashboard = reverse("owner_dashboard")
        pet_page = reverse("owner_pet_detail", args=[self.pet.pk])
        self.assertIn(f'<a href="{dashboard}" aria-label="Back"', step1)
        self.assertIn(f'<a href="{self.step1_back}" aria-label="Back"', step2)
        self.assertIn(f'<a href="{self.step2}?back=1" aria-label="Back"', step3)
        for bar in (step1, step2, step3):
            self.assertIn(f'<a href="{dashboard}" class=', bar)
            self.assertIn(">Cancel</a>", bar)
        # Started from a pet's page: Cancel goes back there
        self._set_draft(from_pet=True)
        bar = self._bar(self._html(self.step2))
        self.assertIn(f'<a href="{pet_page}" class=', bar)
        self.assertIn(">Cancel</a>", bar)

    def test_the_bottom_bar_has_back_and_one_violet_next(self):
        for name, html in self._pages()[1:]:
            with self.subTest(page=name):
                bar = re.search(r'(?s)<div class="fixed inset-x-0.*', html).group(0)
                self.assertRegex(bar, r"(?s)<svg.*?</svg>\s*Back</a>")
                self.assertEqual(len(re.findall(VIOLET_BUTTON, bar)), 1)

    def test_no_bottom_tabs_during_the_flow(self):
        self._set_draft()
        urls = [self.step1_back, self.step2, self.step3]
        for url in urls:
            with self.subTest(url=url):
                html = self.client.get(url).content.decode()
                self.assertNotIn('<nav aria-label="Main"', html)
                self.assertNotIn(reverse("owner_billing_list"), html)

    def test_one_violet_button_on_every_page(self):
        for name, html in self._pages():
            with self.subTest(page=name):
                self.assertEqual(len(re.findall(VIOLET_BUTTON, html)), 1)

    def test_one_line_links_no_inline_script_no_htmx_attributes(self):
        for name, html in self._pages():
            with self.subTest(page=name):
                self.assertIsNone(re.search(r"<a\b[^>]*\n", html))
                for tag in re.findall(r"<script\b[^>]*>", html):
                    self.assertIn(" src=", tag)
                self.assertIsNone(re.search(r"\son[a-z]+=", html))
                for word in ("hx-confirm", "hx-post", "hx-trigger"):
                    self.assertNotIn(word, html)
                # Slice 8b-10b-3: on step 2 only the day links and month arrows
                # (<a>) carry hx-get (was: no page had hx-get at all)
                tags = set(re.findall(r"<(\w+)\b[^>]*\bhx-get=", html))
                self.assertEqual(tags, {"a"} if name == "step 2" else set())
                self.assertNotIn("<!--", html)

    # ── Step 1 ──────────────────────────────────────────────────────────────

    def test_step_1_shows_each_pet_as_a_card_and_picks_one(self):
        Pet.objects.filter(pk=self.pet.pk).update(breed="Aspin")
        mingming = Pet.objects.create(owner=self.owner, name="Mingming", species="Cat")
        archived = Pet.objects.create(
            owner=self.owner, name="Ghostpet", species="Dog", is_archived=True
        )
        theirs = self._other_owners_pet()
        html = self._html(self.step1, pet=mingming.pk)
        self.assertIn("Which pet is this for?", html)
        self.assertIn("Dog · Aspin", html)
        self.assertIn("Cat</span>", html)
        for pet in (archived, theirs):
            self.assertNotIn(pet.name, html)
        radios = re.findall(r'<input type="radio" name="pet"[^>]*>', html)
        self.assertEqual(len(radios), 2)
        checked = [r for r in radios if " checked" in r]
        self.assertEqual(len(checked), 1)
        self.assertIn(f'value="{mingming.pk}"', checked[0])
        add_pet = f'<a href="{reverse("owner_pet_add")}?next=book"'
        self.assertIn(add_pet, html)
        self.assertIn("Add another pet</a>", html)

    def test_with_one_pet_that_pet_is_picked_and_is_the_only_choice(self):
        html = self._html(self.step1)
        radios = re.findall(r'<input type="radio" name="pet"[^>]*>', html)
        self.assertEqual(len(radios), 1)
        self.assertIn(" checked", radios[0])

    def test_with_no_pet_only_the_pet_question_and_a_disabled_next(self):
        Pet.objects.all().delete()
        html = self._html(self.step1)
        self.assertIn("You haven't added a pet yet", html)
        self.assertIn("Which pet is this for?", html)
        self.assertNotIn("What is the visit for?", html)
        self.assertNotIn('id="booking-form"', html)
        add_pet = f'<a href="{reverse("owner_pet_add")}?next=book"'
        found = re.findall(VIOLET_BUTTON, html)
        self.assertEqual(len(found), 1)
        self.assertIn(add_pet, found[0])
        self.assertIn("Add a pet</a>", html)
        self.assertRegex(html, r'<button type="button" disabled[^>]*>\s*Next')
        self.assertIn("Add a pet to continue", html)
        self.assertIn("The rest of the booking", html)

    def test_step_1_lists_bookable_services_then_not_sure_yet(self):
        for name, status in (
            ("Grooming", Service.ACTIVE),
            ("Secret draft", Service.DRAFT),
            ("Unlisted one", Service.UNLISTED),
            ("Closed one", Service.CLOSED),
        ):
            Service.objects.create(service_name=name, status=status)
        html = self._html(self.step1)
        names = re.findall(
            r'<input type="radio" name="service" value="[^"]*"[^>]*>\s*'
            r'<span class="flex min-h-14[^>]*>([^<]*)</span>',
            html,
        )
        self.assertEqual(names, ["Consultation", "Grooming", "Not sure yet"])
        self.assertIn('name="service" value="none"', html)
        self.assertIn('Not sure? Pick "Not sure yet" and Dr. Aga will check.', html)

    def test_step_1_has_the_optional_note(self):
        html = self._html(self.step1)
        self.assertIn("Anything the vet should know?", html)
        self.assertIn("(optional)", html)
        self.assertIn('<textarea id="booking-reason" name="reason"', html)
        self.assertIn("For example: not eating since yesterday", html)

    def test_missing_answers_are_field_errors_and_next_stays_enabled(self):
        no_service = ["Choose what the visit is for."]
        cases = [
            ({}, ["Choose a pet.", "Choose what the visit is for."]),
            ({"pet": self.pet.pk}, no_service),
            ({"service": "none"}, ["Choose a pet."]),
            ({"pet": self.pet.pk, "service": "999"}, no_service),
        ]
        for data, errors in cases:
            with self.subTest(data=data):
                response = self.client.post(self.step1, data)
                self.assertEqual(response.status_code, 200)
                html = response.content.decode()
                for error in errors:
                    self.assertContains(response, error)
                # Slice 8b-10b-1: the loading part's slow box is a role="alert"
                # outside <main>; only the page's own alerts are counted
                main = html.split("<main", 1)[1].split("</main>", 1)[0]
                self.assertEqual(main.count('role="alert"'), len(errors))
                self.assertNotIn('<button type="button" disabled', html)
                self.assertIn('form="booking-form"', html)
        self.assertFalse(Appointment.objects.exists())

    def test_another_owners_pet_cannot_be_posted(self):
        theirs = self._other_owners_pet()
        response = self.client.post(
            self.step1, {"pet": theirs.pk, "service": "none", "reason": ""}
        )
        self.assertContains(response, "Choose a pet.")
        self.assertNotEqual((self._draft() or {}).get("pet"), theirs.pk)

    def test_a_long_note_is_refused(self):
        response = self.client.post(
            self.step1, {"pet": self.pet.pk, "service": "none", "reason": "x" * 1001}
        )
        self.assertContains(response, "Please keep the note short")

    # ── Step 2 ──────────────────────────────────────────────────────────────

    def _cell(self, html, day):
        """The tag of one calendar day, found by its date in the aria-label."""
        label = date_format(day, "l, F j")
        return re.search(rf'<(?:a|span)\b[^>]*aria-label="{label}: [^"]*"[^>]*>', html)

    def test_step_2_summary_and_the_pick_a_day_state(self):
        self._set_draft(date=None, time=None, service="none")
        html = self._html(self.step2)
        self.assertIn("Bantay", html)
        self.assertIn("Not sure yet", html)
        self.assertIn("Pick a day", html)
        self.assertIn("You can book up to 30 days ahead.", html)
        self.assertIn("Pick a day to see the times.", html)
        self.assertNotIn("Times on", html)

    def test_calendar_days_by_state_with_their_labels(self):
        ClinicSettings.objects.update(working_days=[0, 1, 2, 3, 4, 5])  # no Sunday
        self._hold(datetime.date(2026, 11, 12), range(8, 17))
        self._set_draft(date=None, time=None)
        html = self._html(self.step2)
        open_day = self._cell(html, datetime.date(2026, 11, 4)).group(0)
        self.assertTrue(open_day.startswith("<a href="))
        self.assertIn(f'href="{self.step2}?date=2026-11-04"', open_day)
        self.assertIn('aria-label="Wednesday, November 4: open"', open_day)
        full = self._cell(html, datetime.date(2026, 11, 12)).group(0)
        self.assertTrue(full.startswith("<span"))
        self.assertIn("bg-gray-100", full)
        self.assertIn("line-through", full)
        self.assertIn("Thursday, November 12: full", full)
        for day, phrase in (
            (datetime.date(2026, 11, 8), "clinic closed"),
            (datetime.date(2026, 11, 1), "not available"),
        ):
            with self.subTest(day=day):
                plain = self._cell(html, day).group(0)
                self.assertTrue(plain.startswith("<span"))
                self.assertIn(phrase, plain)
                self.assertNotIn("href=", plain)
        # 8b-10b-5: the weekday heads stay; the bottom key (Open / Full /
        # Closed) is gone, the states are told by colour, shape and aria-label
        for word in ("Sun", "Mon", "Sat"):
            self.assertIn(word, html)
        for word in (">Open<", ">Full<", ">Closed<"):
            self.assertNotIn(word, html)

    def test_today_past_the_cutoff_is_a_ring_not_a_link(self):
        self.freeze(_manila(15, 30))
        self._set_draft(date=None, time=None)
        html = self._html(self.step2)
        today = self._cell(html, WALK_IN_DAY).group(0)
        self.assertTrue(today.startswith("<span"))
        self.assertIn("ring-2", today)
        self.assertNotIn("line-through", today)
        self.assertIn("Monday, November 2: no more bookings today", today)

    def test_the_picked_day_is_violet_and_not_a_link(self):
        self._set_draft()
        html = self._html(self.step2)
        picked = self._cell(html, BOOK_DAY).group(0)
        self.assertTrue(picked.startswith("<span"))
        self.assertIn("bg-violet-600", picked)
        self.assertIn('aria-current="date"', picked)

    def test_month_arrows_are_links_with_disabled_spans_at_the_edges(self):
        self._set_draft(date=None, time=None)
        first = self._html(self.step2, month="2026-11")
        self.assertIn(
            f'<a href="{self.step2}?month=2026-12" aria-label="Next month"', first
        )
        disabled = r'<span aria-disabled="true" aria-label="'
        self.assertRegex(first, disabled + "Previous month")
        self.assertNotIn(f'<a href="{self.step2}?month=2026-10"', first)
        self.assertIn("November 2026", first)
        last = self._html(self.step2, month="2026-12")
        self.assertIn(
            f'<a href="{self.step2}?month=2026-11" aria-label="Previous month"', last
        )
        self.assertRegex(last, disabled + "Next month")
        self.assertIn("December 2026", last)
        # Before this month and past the limit are clamped, junk opens today's
        self.assertIn("November 2026", self._html(self.step2, month="2026-01"))
        self.assertIn("December 2026", self._html(self.step2, month="2030-05"))
        self.assertIn("November 2026", self._html(self.step2, month="junk"))

    def test_days_after_the_booking_limit_are_not_links(self):
        self._set_draft(date=None, time=None)
        html = self._html(self.step2, month="2026-12")
        self.assertIn("?date=2026-12-02", html)
        self.assertNotIn("?date=2026-12-03", html)
        self.assertIn("Thursday, December 3: not available", html)

    def test_times_grouped_morning_and_afternoon_with_taken_not_selectable(self):
        self._hold(BOOK_DAY, [10])
        self._set_draft(time="12:00:00")
        html = self._html(self.step2)
        self.assertIn("Times on Tue, Nov 10", html)
        self.assertIn("Each visit is 1 hour.", html)
        morning, afternoon = html.index("Morning"), html.index("Afternoon")
        self.assertLess(morning, afternoon)
        values = re.findall(r'<input type="radio" name="time" value="([^"]+)"', html)
        self.assertEqual(
            values,
            [f"{h:02d}:00:00" for h in (8, 9, 11, 12, 13, 14, 15, 16)],
        )
        self.assertLess(html.index('value="11:00:00"'), afternoon)
        self.assertGreater(html.index('value="12:00:00"'), afternoon)
        taken = r'(?s)<span class="line-through">10:00 AM</span>.*?Taken'
        self.assertRegex(html, taken)
        self.assertNotIn('value="10:00:00"', html)
        checked = re.findall(r'<input type="radio" name="time"[^>]*checked>', html)
        self.assertEqual(len(checked), 1)
        self.assertIn('value="12:00:00"', checked[0])
        grid = re.findall(r'<div class="grid grid-cols-3 gap-2">', html)
        self.assertEqual(len(grid), 2)

    def test_the_slot_length_wording(self):
        self._set_draft()
        for minutes, phrase in ((60, "1 hour"), (30, "30 minutes"), (120, "2 hours")):
            with self.subTest(minutes=minutes):
                ClinicSettings.objects.update(slot_duration_minutes=minutes)
                self.assertIn(f"Each visit is {phrase}.", self._html(self.step2))

    def test_a_picked_day_goes_in_the_draft_and_clears_the_time(self):
        self._set_draft()
        other = datetime.date(2026, 11, 11)
        html = self._html(self.step2, date=other.isoformat())
        self.assertIn("Times on Wed, Nov 11", html)
        draft = self._draft()
        self.assertEqual(draft["date"], other.isoformat())
        self.assertNotIn("time", draft)

    def test_a_day_that_is_not_open_in_the_url_shows_the_calendar_and_an_error(self):
        self._hold(datetime.date(2026, 11, 12), range(8, 17))
        BlockedDate.objects.create(date=datetime.date(2026, 11, 13))
        draft = self._set_draft()
        for value in ("2026-11-01", "2026-11-12", "2026-11-13", "2026-12-03", "junk"):
            with self.subTest(value=value):
                response = self.client.get(self.step2, {"date": value})
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "That day can&#x27;t be booked.")
                self.assertContains(response, "November 2026")
                self.assertEqual(self._draft(), draft)

    def test_step_2_post_without_a_time_or_a_day_is_a_field_error(self):
        self._set_draft(date=None, time=None)
        response = self.client.post(self.step2, {"date": BOOK_DAY.isoformat()})
        self.assertContains(response, "Pick a time.")
        response = self.client.post(self.step2, {"time": "12:00:00"})
        self.assertContains(response, "Pick a day first.")
        self.assertIn('form="time-form"', response.content.decode())
        self.assertNotIn("time", self._draft())

    def test_step_2_post_saves_the_day_and_time_and_goes_to_step_3(self):
        self._set_draft(date=None, time=None)
        response = self.client.post(
            self.step2, {"date": BOOK_DAY.isoformat(), "time": "09:00:00"}
        )
        self.assertRedirects(response, self.step3, fetch_redirect_response=False)
        draft = self._draft()
        self.assertEqual(draft["date"], BOOK_DAY.isoformat())
        self.assertEqual(draft["time"], "09:00:00")
        self.assertFalse(Appointment.objects.exists())

    def test_step_2_post_of_a_taken_time_shows_the_box_and_keeps_the_day(self):
        self._hold(BOOK_DAY, [9])
        self._set_draft(time=None)
        response = self.client.post(
            self.step2, {"date": BOOK_DAY.isoformat(), "time": "09:00:00"}
        )
        self.assertContains(response, "9:00 AM was just taken")
        self.assertContains(response, "Times on Tue, Nov 10")
        self.assertNotIn("time", self._draft())

    # ── Step 3 ──────────────────────────────────────────────────────────────

    def test_step_3_rows_change_links_and_the_info_box(self):
        self._set_draft()
        html = self._html(self.step3)
        self.assertIn("Check your request", html)
        rows = re.findall(r"<dt[^>]*>([^<]*)</dt><dd[^>]*>([^<]*)</dd>", html)
        self.assertEqual(
            rows,
            [
                ("Pet", "Bantay"),
                ("Visit for", "Consultation"),
                ("Day and time", "Tue, Nov 10, 2026 at 12:00 PM"),
                ("Note for the vet", "Limping"),
            ],
        )
        page = f'<a href="{self.step1}?back=1"'
        self.assertEqual(html.count(page), 3)  # pet, service, note
        # Day and time, plus the two Back links
        self.assertEqual(html.count(f'<a href="{self.step2}?back=1"'), 3)
        self.assertEqual(html.count("Change<span"), 4)
        self.assertIn("This is a <b>request</b>.", html)
        self.assertIn("The clinic will confirm it", html)
        self.assertIn('form="send-form"', html)
        self.assertIn("Send request", html)

    def test_step_3_not_sure_yet_and_no_note(self):
        self._set_draft(service="none", note="")
        html = self._html(self.step3)
        rows = re.findall(r"<dt[^>]*>([^<]*)</dt><dd[^>]*>([^<]*)</dd>", html)
        self.assertIn(("Visit for", "Not sure yet"), rows)
        self.assertNotIn("Note for the vet", html)
        self.assertEqual(html.count("Change<span"), 3)

    # ── Query counts ────────────────────────────────────────────────────────

    def _count(self, url, **params):
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(url, params)
        self.assertEqual(response.status_code, 200)
        return len(queries)

    def test_every_step_has_a_flat_query_count(self):
        self._set_draft()
        urls = {
            "step 1": (self.step1, {"back": "1"}),
            "step 2": (self.step2, {"date": BOOK_DAY.isoformat()}),
            "step 3": (self.step3, {}),
        }
        few = {name: self._count(url, **q) for name, (url, q) in urls.items()}
        for number in range(6):
            Pet.objects.create(owner=self.owner, name=f"Pet {number}", species="Cat")
            Service.objects.create(service_name=f"S{number}", status=Service.ACTIVE)
        self._hold(BOOK_DAY, [8, 9, 10, 14])
        self._hold(datetime.date(2026, 11, 20), range(8, 17))
        many = {name: self._count(url, **q) for name, (url, q) in urls.items()}
        print(f"BOOKING QUERIES (whole request, flat): {many}")
        self.assertEqual(few, many)


class OwnerBookingFixesTests(_OwnerBookingBase):
    """Fixes after the browser check: the picked look of the radio cards, the
    room under <main> on every step, and a closed draft day on a step 2 POST."""

    CARD = (
        "peer-checked:border-violet-500",
        "peer-checked:bg-violet-50",
        "peer-checked:ring-2",
        "peer-checked:ring-violet-200",
        "peer-focus-visible:ring-2",
        "peer-focus-visible:ring-violet-600",
    )
    CIRCLE = (
        "peer-checked:border-violet-600",
        "peer-checked:bg-violet-600",
        "peer-checked:text-white",
    )
    TIME = (
        "peer-checked:bg-violet-600",
        "peer-checked:text-white",
        "peer-checked:ring-violet-600",
        "peer-focus-visible:ring-2",
    )

    def _card(self, html, name, value, checked=True):
        """The classes of the card span that follows a radio inside its label
        (the check circle's classes too, when it has one)."""
        flag = " checked" if checked else ""
        found = re.search(
            rf'<label class="relative block cursor-pointer">\s*'
            rf'<input type="radio" name="{name}" value="{value}" '
            rf'class="peer sr-only"{flag}>\s*<span class="([^"]*)">'
            rf"(?s:.*?)</label>",
            html,
        )
        self.assertIsNotNone(found, (name, value, checked))
        return found.group(1), found.group(0)

    def _assert_all(self, classes, wanted):
        for name in wanted:
            self.assertIn(name, classes)

    def test_a_server_checked_pet_and_service_are_picked_with_no_script(self):
        self._set_draft()
        html = self._html(self.step1, back="1")
        pet, markup = self._card(html, "pet", self.pet.pk)
        self._assert_all(pet, self.CARD)
        self._assert_all(markup, self.CIRCLE)
        service, markup = self._card(html, "service", self.service.pk)
        self._assert_all(service, self.CARD)
        self._assert_all(markup, self.CIRCLE)

    def test_a_card_that_is_not_picked_has_no_checked_attribute(self):
        mingming = Pet.objects.create(owner=self.owner, name="Mingming", species="Cat")
        self._set_draft(service="none")
        html = self._html(self.step1, back="1")
        self._card(html, "pet", self.pet.pk)
        self._card(html, "pet", mingming.pk, checked=False)
        self._card(html, "service", self.service.pk, checked=False)
        self._card(html, "service", "none")

    def test_the_pet_in_the_url_shows_as_picked(self):
        mingming = Pet.objects.create(owner=self.owner, name="Mingming", species="Cat")
        html = self._html(self.step1, pet=mingming.pk)
        classes, _markup = self._card(html, "pet", mingming.pk)
        self._assert_all(classes, self.CARD)
        self._card(html, "pet", self.pet.pk, checked=False)

    def test_the_picked_time_is_filled_violet_and_the_others_are_not(self):
        self._set_draft(time="12:00:00")
        html = self._html(self.step2)
        classes, _markup = self._card(html, "time", "12:00:00")
        self._assert_all(classes, self.TIME)
        self._card(html, "time", "13:00:00", checked=False)

    def test_every_step_leaves_room_for_the_bottom_bar(self):
        self._set_draft()
        one = (
            "group-has-[[data-bar=one]]/body:"
            "pb-[calc(8rem+env(safe-area-inset-bottom))]"
        )
        for url in (self.step1_back, self.step2, self.step3):
            with self.subTest(url=url):
                html = self._html(url)
                main = re.search(r'<main id="main" class="([^"]*)"', html).group(1)
                self.assertIn("pb-28", main)
                self.assertIn(one, main)
                self.assertRegex(html, r'<body class="group/body ')
                self.assertRegex(
                    html, r'<div class="fixed inset-x-0 [^"]*"[^>]*data-bar="one"'
                )
                self.assertNotRegex(main, r"\bpb-40\b")

    def test_a_post_for_a_draft_day_that_has_since_closed_shows_the_not_open_error(
        self,
    ):
        self._set_draft(time=None)
        BlockedDate.objects.create(date=BOOK_DAY)
        draft = self._draft()
        cases = (
            {"date": BOOK_DAY.isoformat()},
            {"date": BOOK_DAY.isoformat(), "time": "12:00:00"},
        )
        for data in cases:
            with self.subTest(data=data):
                response = self.client.post(self.step2, data)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "That day can&#x27;t be booked.")
                self.assertContains(response, "November 2026")
                self.assertNotContains(response, "Pick a time.")
                self.assertEqual(self._draft(), draft)

    def test_a_post_without_a_time_on_an_open_day_still_says_pick_a_time(self):
        self._set_draft(time=None)
        response = self.client.post(self.step2, {"date": BOOK_DAY.isoformat()})
        self.assertContains(response, "Pick a time.")
        self.assertNotContains(response, "That day can&#x27;t be booked.")


class OwnerBookingSendTests(_OwnerBookingBase):
    """Send: one PENDING visit through the locked create, a taken slot comes
    back to step 2, a second tap makes nothing, and the sent page."""

    def _send(self):
        return self.client.post(self.step3)

    def test_send_creates_exactly_one_pending_visit_and_clears_the_draft(self):
        self._set_draft()
        response = self._send()
        visit = Appointment.objects.get()
        self.assertRedirects(
            response,
            reverse("owner_book_sent", args=[visit.pk]),
            fetch_redirect_response=False,
        )
        self.assertEqual(visit.status, Appointment.PENDING)
        self.assertEqual(visit.owner, self.owner)
        self.assertEqual(visit.pet, self.pet)
        self.assertEqual(visit.service, self.service)
        self.assertEqual(visit.date, BOOK_DAY)
        self.assertEqual(visit.time, datetime.time(12, 0))
        self.assertEqual(visit.reason, "Limping")
        self.assertFalse(visit.is_walk_in)
        self.assertIsNone(self._draft())

    def test_not_sure_yet_saves_no_service(self):
        self._set_draft(service="none", note="")
        self._send()
        visit = Appointment.objects.get()
        self.assertIsNone(visit.service)
        self.assertEqual(visit.reason, "")

    def test_the_admin_is_told_as_before(self):
        self._set_draft()
        self._send()
        visit = Appointment.objects.get()
        notice = Notification.objects.get(recipient=self.admin)
        self.assertEqual(notice.related_appointment, visit)
        self.assertEqual(
            Notification.objects.filter(recipient=self.owner.user).count(), 0
        )

    def test_send_goes_through_the_lock_and_checks_the_slot_again(self):
        self._set_draft()
        real = is_slot_available
        with mock.patch("appointments.utils.lock_schedule") as lock:
            with mock.patch(
                "appointments.utils.is_slot_available", side_effect=real
            ) as check:
                self._send()
        lock.assert_called_once_with()
        check.assert_called_once_with(BOOK_DAY, datetime.time(12, 0))
        self.assertEqual(Appointment.objects.count(), 1)

    def test_a_slot_taken_at_the_lock_goes_back_to_step_2_with_the_box(self):
        self._set_draft()
        with mock.patch("appointments.utils.is_slot_available", return_value=False):
            response = self._send()
        self.assertRedirects(response, self.step2, fetch_redirect_response=False)
        self.assertFalse(Appointment.objects.exists())
        self.assertFalse(Notification.objects.filter(recipient=self.admin).exists())
        page = self.client.get(self.step2)
        self.assertContains(page, "12:00 PM was just taken")
        self.assertContains(page, "Times on Tue, Nov 10")
        draft = self._draft()
        self.assertNotIn("time", draft)
        self.assertEqual(
            (draft["pet"], draft["service"], draft["note"], draft["date"]),
            (self.pet.pk, str(self.service.pk), "Limping", BOOK_DAY.isoformat()),
        )

    def test_a_slot_booked_by_someone_else_before_send_makes_nothing(self):
        self._set_draft()
        self._hold(BOOK_DAY, [12])
        response = self._send()
        self.assertRedirects(response, self.step2, fetch_redirect_response=False)
        self.assertEqual(Appointment.objects.count(), 1)  # only theirs

    def test_a_second_post_makes_no_second_visit(self):
        self._set_draft()
        self._send()
        response = self._send()
        self.assertRedirects(
            response, reverse("owner_appointments"), fetch_redirect_response=False
        )
        self.assertEqual(
            self._messages(response),
            ["Your request was already sent. It's in your visits."],
        )
        self.assertEqual(Appointment.objects.count(), 1)

    def test_a_stale_draft_sent_again_finds_its_own_slot_taken(self):
        draft = self._set_draft()
        self._send()
        self._set_draft(**draft)  # the second tap read the draft before it went
        response = self._send()
        self.assertRedirects(response, self.step2, fetch_redirect_response=False)
        self.assertEqual(Appointment.objects.count(), 1)

    # ── The sent page ───────────────────────────────────────────────────────

    def _visit(self, **fields):
        data = {
            "owner": self.owner,
            "pet": self.pet,
            "service": self.service,
            "date": BOOK_DAY,
            "time": datetime.time(12, 0),
            "status": Appointment.PENDING,
        }
        data.update(fields)
        return Appointment.objects.create(**data)

    def test_the_sent_page(self):
        visit = self._visit(notes="Internal: bite risk")
        response = self.client.get(reverse("owner_book_sent", args=[visit.pk]))
        html = response.content.decode()
        self.assertContains(response, "Request sent")
        self.assertIn("Bantay · Consultation", html)
        self.assertIn('<b class="text-gray-900">Tue, Nov 10 at 12:00 PM</b>', html)
        self.assertIn("bg-yellow-100 text-yellow-800", html)
        self.assertIn("Waiting for approval", html)
        self.assertIn("We'll let you know when the clinic confirms.", html)
        found = re.findall(VIOLET_BUTTON, html)
        self.assertEqual(len(found), 1)
        self.assertIn(f'href="{reverse("owner_appointments")}"', found[0])
        self.assertIn("See my visits</a>", html)
        self.assertIn(f'<a href="{reverse("owner_dashboard")}"', html)
        self.assertIn("Back to home</a>", html)
        self.assertNotIn('<nav aria-label="Main"', html)
        self.assertNotIn(">Back</a>", html)
        self.assertNotIn('aria-label="Back"', html)
        self.assertNotIn("Internal: bite risk", html)
        self.assertIsNone(re.search(r"<a\b[^>]*\n", html))

    def test_the_sent_page_without_a_service(self):
        visit = self._visit(service=None)
        html = self._html(reverse("owner_book_sent", args=[visit.pk]))
        self.assertIn("Bantay · Not sure yet", html)

    def test_the_sent_page_shows_the_status_it_has_now(self):
        visit = self._visit()
        url = reverse("owner_book_sent", args=[visit.pk])
        cases = {
            Appointment.CONFIRMED: ("Confirmed", "bg-blue-100"),
            Appointment.CANCELLED: ("Cancelled", "bg-red-100"),
            Appointment.COMPLETED: ("Completed", "bg-green-100"),
            Appointment.NO_SHOW: ("Missed", "bg-gray-100"),
        }
        for status, (word, colour) in cases.items():
            with self.subTest(status=status):
                Appointment.objects.filter(pk=visit.pk).update(status=status)
                html = self._html(url)
                badge = rf'<span class="badge [^"]*{colour}[^"]*">{word}</span>'
                self.assertRegex(html, badge)
                self.assertNotIn("Waiting for approval", html)

    def test_another_owners_visit_is_a_404(self):
        theirs = self._other_owners_pet()
        visit = self._visit(owner=theirs.owner, pet=theirs)
        response = self.client.get(reverse("owner_book_sent", args=[visit.pk]))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            self.client.get(reverse("owner_book_sent", args=[99999])).status_code, 404
        )

    def test_the_clinic_is_sent_to_its_own_dashboard(self):
        visit = self._visit()
        self.client.force_login(self.admin)
        response = self.client.get(reverse("owner_book_sent", args=[visit.pk]))
        self.assertRedirects(
            response, reverse("admin_dashboard"), fetch_redirect_response=False
        )

    def test_the_sent_page_query_count_is_flat(self):
        visit = self._visit()
        url = reverse("owner_book_sent", args=[visit.pk])

        def count():
            with CaptureQueriesContext(connection) as queries:
                self.client.get(url)
            return len(queries)

        few = count()
        for number in range(6):
            self._visit(time=datetime.time(8 + number, 0))
        print(f"BOOKING QUERIES (sent page): {few}")
        self.assertEqual(count(), few)


# ─── Slice 8b-7a: owner visits list, visit page and cancel sheet ─────────────

PHONE = "0954 288 9774"
# state: (colour classes, bold title, the one line under it)
BANNERS = {
    "pending": (
        "bg-yellow-50 text-yellow-900",
        "Waiting for the clinic to confirm",
        "You'll get a notification when they do. "
        "Please don't come in until it's confirmed.",
    ),
    "confirmed": (
        "bg-blue-50 text-blue-900",
        "Confirmed",
        "See you on Tue, Nov 10 at 12:00 PM. Please come 10 minutes early.",
    ),
    "ended": (
        "bg-gray-100 text-gray-900",
        "This time has passed",
        "The clinic hasn't updated this visit yet. "
        "Please call them if you have a question.",
    ),
    "completed": (
        "bg-green-50 text-green-900",
        "Visit done",
        "Thank you for coming in.",
    ),
    "declined": (
        "bg-red-50 text-red-900",
        "Declined by the clinic",
        "The clinic couldn't take this request. Please call us or book another time.",
    ),
    "cancelled_clinic": (
        "bg-red-50 text-red-900",
        "Cancelled by the clinic",
        "Sorry about this. Please call us to find a new time, or book another visit.",
    ),
    "cancelled_owner": (
        "bg-red-50 text-red-900",
        "You cancelled this visit",
        "You cancelled: pet is unwell.",
    ),
    "moved": (
        "bg-gray-100 text-gray-900",
        "This visit was moved",
        "It's now on Tue, Nov 10 at 12:00 PM.",
    ),
    "no_show": (
        "bg-gray-100 text-gray-900",
        "Missed",
        "The clinic marked this visit as missed. "
        "If that's a mistake, please call the clinic.",
    ),
}


class _OwnerVisitsBase(FrozenNowTestBase):
    """Mon, Nov 2, 2026 at 9:30 Manila. The owner (Ana) is logged in with one
    pet (Bantay) and an Active service; slots are 60 minutes, so a 9:00 visit
    is still going today and an 8:00 one is over."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner.user)
        self.service = Service.objects.create(
            service_name="Consultation", status=Service.ACTIVE
        )
        ClinicSettings.objects.update(contact_number=PHONE)

    def _visit(self, days=3, hour=9, status=Appointment.CONFIRMED, **fields):
        data = {
            "owner": self.owner,
            "pet": self.pet,
            "service": self.service,
            "date": self.today + datetime.timedelta(days=days),
            "time": datetime.time(hour, 0),
            "status": status,
        }
        data.update(fields)
        return Appointment.objects.create(**data)

    def _cancelled(self, by, reason=Appointment.OTHER, **fields):
        return self._visit(
            status=Appointment.CANCELLED,
            cancelled_by=by,
            cancellation_reason=reason,
            **fields,
        )

    def _moved(self, by, status):
        """An original cancelled by a reschedule (Mon, Nov 9 at 3:00 PM) and
        the new visit on Tue, Nov 10 at 12:00 PM."""
        original = self._cancelled(by, days=7, hour=15)
        new = self._visit(days=8, hour=12, status=status, rescheduled_from=original)
        return original, new

    def _second_pet(self, name="Mingming"):
        return Pet.objects.create(owner=self.owner, name=name, species="Cat")

    def _other_owner(self):
        user = User.objects.create_user(
            username="someone",
            email="someone@example.com",
            password="unused-pass-123",
            role=User.PET_OWNER,
            is_onboarded=True,
        )
        other = PetOwner.objects.create(user=user, first_name="Ben", last_name="Reyes")
        return other, Pet.objects.create(owner=other, name="Rex", species="Dog")

    def _html(self, url_name="owner_appointments", args=None, **params):
        """The page as text: entities decoded, so apostrophes and & read plain."""
        response = self.client.get(reverse(url_name, args=args), params)
        return unescape(response.content.decode())

    def _violet(self, html):
        return re.findall(VIOLET_BUTTON, html)

    def _queries(self, url_name="owner_appointments", args=None, **params):
        with CaptureQueriesContext(connection) as queries:
            self.client.get(reverse(url_name, args=args), params)
        return len(queries)


class OwnerVisitNoteTests(_OwnerVisitsBase):
    """owner_visit_note at Mon, Nov 2, 2026 9:30 Manila, 60-minute slots."""

    def _note(self, visit):
        return owner_visit_note(visit, self.NOW, 60)

    def test_pending_says_the_clinic_will_confirm(self):
        visit = self._visit(status=Appointment.PENDING)
        self.assertEqual(self._note(visit), "The clinic will confirm this soon.")

    def test_confirmed_and_completed_have_no_note(self):
        self.assertIsNone(self._note(self._visit()))
        self.assertIsNone(self._note(self._visit(days=-1, status="completed")))

    def test_a_visit_made_by_a_clinic_move(self):
        _original, new = self._moved(self.admin, Appointment.CONFIRMED)
        self.assertEqual(
            self._note(new), "Moved by the clinic. Was Mon, Nov 9 at 3:00 PM."
        )

    def test_a_visit_made_by_an_owner_change_while_pending(self):
        _original, new = self._moved(self.owner.user, Appointment.PENDING)
        self.assertEqual(
            self._note(new), "You changed the time. Was Mon, Nov 9 at 3:00 PM."
        )

    def test_an_owner_change_the_clinic_confirmed_has_no_note(self):
        _original, new = self._moved(self.owner.user, Appointment.CONFIRMED)
        self.assertIsNone(self._note(new))

    def test_cancelled_by_the_owner(self):
        cases = {
            Appointment.CHANGE_OF_PLANS: "You cancelled: change of plans.",
            Appointment.PET_UNWELL: "You cancelled: pet is unwell.",
            Appointment.CANNOT_MAKE_TIME: "You cancelled: cannot make the time.",
            Appointment.OTHER: "You cancelled this visit.",
            "": "You cancelled this visit.",
        }
        for reason, expected in cases.items():
            with self.subTest(reason=reason):
                visit = self._cancelled(self.owner.user, reason)
                self.assertEqual(self._note(visit), expected)

    def test_declined_by_the_clinic(self):
        visit = self._cancelled(self.admin, Appointment.DECLINED)
        self.assertEqual(self._note(visit), "Declined by the clinic.")

    def test_cancelled_by_the_clinic(self):
        expected = "Cancelled by the clinic. Please call us if you need a new time."
        for by in (self.admin, None):
            with self.subTest(by=by):
                self.assertEqual(self._note(self._cancelled(by)), expected)

    def test_missed(self):
        visit = self._visit(days=-1, status=Appointment.NO_SHOW)
        self.assertEqual(self._note(visit), "Marked as missed by the clinic.")

    def test_a_slot_that_has_ended_was_not_updated(self):
        ended = "The clinic hasn't updated this visit yet."
        for status in (Appointment.PENDING, Appointment.CONFIRMED):
            with self.subTest(status=status):
                over = self._visit(days=0, hour=8, status=status)
                yesterday = self._visit(days=-1, hour=15, status=status)
                self.assertEqual(self._note(over), ended)
                self.assertEqual(self._note(yesterday), ended)
        # a moved visit whose slot is over says this, not "Moved by the clinic"
        original = self._cancelled(self.admin, days=-3)
        old = self._visit(days=0, hour=8, rescheduled_from=original)
        self.assertEqual(self._note(old), ended)

    def test_a_slot_in_progress_has_not_ended(self):
        visit = self._visit(days=0, hour=9, status=Appointment.PENDING)
        self.assertEqual(self._note(visit), "The clinic will confirm this soon.")

    def test_without_minutes_it_reads_the_settings(self):
        over = self._visit(days=0, hour=8)
        note = owner_visit_note(over, self.NOW)
        self.assertEqual(note, "The clinic hasn't updated this visit yet.")


class OwnerVisitsContextTests(_OwnerVisitsBase):
    def _ctx(self, **params):
        return owner_visits_context(self.owner, params, self.today, self.NOW)

    def _ids(self, ctx):
        return [card["visit"].pk for group in ctx["groups"] for card in group["cards"]]

    def test_upcoming_and_past_split_at_the_slot_end(self):
        over = self._visit(days=0, hour=8)
        going = self._visit(days=0, hour=9)
        later = self._visit(days=0, hour=10)
        yesterday = self._visit(days=-1, hour=15)
        done = self._visit(days=2, hour=9, status=Appointment.COMPLETED)
        cancelled = self._visit(days=2, hour=10, status=Appointment.CANCELLED)
        upcoming = self._ctx()
        self.assertEqual(self._ids(upcoming), [going.pk, later.pk])
        past = self._ctx(show="past")
        self.assertEqual(
            self._ids(past), [cancelled.pk, done.pk, over.pk, yesterday.pk]
        )
        self.assertEqual(past["counts"], {"upcoming": 2, "past": 4})

    def test_upcoming_is_soonest_first(self):
        c = self._visit(days=5, hour=9)
        a = self._visit(days=1, hour=14)
        b = self._visit(days=1, hour=16, status=Appointment.PENDING)
        self.assertEqual(self._ids(self._ctx()), [a.pk, b.pk, c.pk])

    def test_an_original_cancelled_by_a_reschedule_is_left_out(self):
        original, new = self._moved(self.admin, Appointment.CONFIRMED)
        plain = self._cancelled(self.admin, days=-2)
        past = self._ctx(show="past")
        self.assertEqual(self._ids(self._ctx()), [new.pk])
        self.assertEqual(self._ids(past), [plain.pk])
        self.assertNotIn(original.pk, self._ids(past))
        self.assertEqual(past["counts"], {"upcoming": 1, "past": 1})

    def test_show_is_upcoming_unless_it_says_past(self):
        cases = [
            (None, "upcoming"),
            ("", "upcoming"),
            ("upcoming", "upcoming"),
            ("bogus", "upcoming"),
            ("PAST", "upcoming"),
            ("past", "past"),
        ]
        for value, expected in cases:
            with self.subTest(show=value):
                params = {} if value is None else {"show": value}
                self.assertEqual(self._ctx(**params)["show"], expected)

    def test_pet_is_one_of_the_owners_current_pets_or_all(self):
        second = self._second_pet()
        archived = self._second_pet("Old")
        archived.is_archived = True
        archived.save()
        _other, theirs = self._other_owner()
        self.assertEqual(self._ctx(pet=str(second.pk))["pet"], second)
        for value in ("", "abc", "-1", "9" * 30, str(theirs.pk), str(archived.pk)):
            with self.subTest(pet=value):
                self.assertIsNone(self._ctx(pet=value)["pet"])
        self.assertIsNone(self._ctx()["pet"])

    def test_the_pet_filter_and_the_counts(self):
        second = self._second_pet()
        mine = self._visit(days=1)
        self._visit(days=2, pet=second)
        self._visit(days=3, pet=second, status=Appointment.PENDING)
        self._visit(days=-1, pet=second, status=Appointment.COMPLETED)
        self.assertEqual(self._ctx()["counts"], {"upcoming": 3, "past": 1})
        picked = self._ctx(pet=str(second.pk))
        self.assertEqual(picked["counts"], {"upcoming": 2, "past": 1})
        self.assertNotIn(mine.pk, self._ids(picked))
        only = self._ctx(pet=str(self.pet.pk))
        self.assertEqual(only["counts"], {"upcoming": 1, "past": 0})
        self.assertEqual(self._ids(only), [mine.pk])

    def test_month_groups(self):
        nov_2 = self._visit(days=0, hour=10)
        nov_6 = self._visit(days=4)
        dec_2 = self._visit(days=30)
        jan_11 = self._visit(days=70)  # 2027
        groups = self._ctx()["groups"]
        self.assertEqual(
            [g["label"] for g in groups],
            ["November 2026", "December 2026", "January 2027"],
        )
        self.assertEqual(
            [[card["visit"].pk for card in g["cards"]] for g in groups],
            [[nov_2.pk, nov_6.pk], [dec_2.pk], [jan_11.pk]],
        )

    def test_is_today_and_the_card_parts(self):
        today = self._visit(days=0, hour=10)
        tomorrow = self._visit(days=1, service=None, status=Appointment.PENDING)
        cards = {c["visit"].pk: c for g in self._ctx()["groups"] for c in g["cards"]}
        self.assertTrue(cards[today.pk]["is_today"])
        self.assertFalse(cards[tomorrow.pk]["is_today"])
        self.assertEqual(cards[today.pk]["service_name"], "Consultation")
        self.assertEqual(cards[tomorrow.pk]["service_name"], "Not sure yet")
        self.assertEqual(
            cards[tomorrow.pk]["note"], "The clinic will confirm this soon."
        )
        self.assertEqual(cards[tomorrow.pk]["note_icon"], "info")
        self.assertIsNone(cards[today.pk]["note"])

    def test_a_clinic_move_and_an_owner_change_get_the_calendar_icon(self):
        _a, by_clinic = self._moved(self.admin, Appointment.CONFIRMED)
        _b, by_owner = self._moved(self.owner.user, Appointment.PENDING)
        cards = {c["visit"].pk: c for g in self._ctx()["groups"] for c in g["cards"]}
        self.assertEqual(cards[by_clinic.pk]["note_icon"], "calendar-clock")
        self.assertEqual(cards[by_owner.pk]["note_icon"], "calendar-clock")

    def test_past_pages_hold_25(self):
        for number in range(30):
            self._visit(days=-1 - number, status=Appointment.COMPLETED)
        first = self._ctx(show="past")
        self.assertEqual(len(self._ids(first)), 25)
        self.assertEqual(first["page_obj"].paginator.num_pages, 2)
        self.assertEqual(first["prev_page_query"], "")
        self.assertEqual(first["next_page_query"], "?show=past&page=2")
        second = self._ctx(show="past", page="2")
        self.assertEqual(len(self._ids(second)), 5)
        self.assertEqual(second["prev_page_query"], "?show=past&page=1")
        self.assertEqual(second["next_page_query"], "")
        self.assertEqual(len(self._ids(self._ctx(show="past", page="99"))), 5)
        self.assertIsNone(self._ctx()["page_obj"])

    def test_paging_keeps_the_pet(self):
        second = self._second_pet()
        for number in range(30):
            self._visit(days=-1 - number, pet=second, status=Appointment.COMPLETED)
        ctx = self._ctx(show="past", pet=str(second.pk), page="2")
        self.assertEqual(ctx["prev_page_query"], f"?show=past&pet={second.pk}&page=1")

    def test_pet_chips_need_two_current_pets(self):
        self.assertFalse(self._ctx()["show_pet_chips"])
        self.assertTrue(self._ctx()["has_pets"])
        archived = self._second_pet("Old")
        archived.is_archived = True
        archived.save()
        self.assertFalse(self._ctx()["show_pet_chips"])
        self._second_pet()
        chips = self._ctx()["chips"]
        self.assertTrue(self._ctx()["show_pet_chips"])
        self.assertEqual([c["label"] for c in chips], ["All", "Bantay", "Mingming"])
        self.assertEqual([c["is_active"] for c in chips], [True, False, False])

    def test_segments_carry_the_counts_and_keep_the_pet(self):
        second = self._second_pet()
        self._visit(days=1, pet=second)
        ctx = self._ctx(show="past", pet=str(second.pk))
        self.assertEqual(
            [(s["label"], s["is_active"]) for s in ctx["segments"]],
            [("Upcoming (1)", False), ("Past (0)", True)],
        )
        self.assertEqual(
            [s["query"] for s in ctx["segments"]],
            [f"show=upcoming&pet={second.pk}", f"show=past&pet={second.pk}"],
        )
        self.assertEqual(ctx["chips"][0]["query"], "show=past")

    def test_no_pets_page_only_with_no_pets_and_no_visits(self):
        self.assertFalse(self._ctx()["show_no_pets_page"])
        visit = self._visit()
        self.pet.is_archived = True
        self.pet.save()
        ctx = self._ctx()
        self.assertFalse(ctx["has_pets"])
        self.assertFalse(ctx["show_no_pets_page"])
        visit.delete()
        self.assertTrue(self._ctx()["show_no_pets_page"])

    def test_another_owners_visits_never_show(self):
        other, theirs = self._other_owner()
        Appointment.objects.create(
            owner=other,
            pet=theirs,
            date=self.today + datetime.timedelta(days=2),
            time=datetime.time(9, 0),
            status=Appointment.CONFIRMED,
        )
        mine = self._visit(days=1)
        for show in ("upcoming", "past"):
            ctx = self._ctx(show=show, pet=str(theirs.pk))
            self.assertIsNone(ctx["pet"])
            self.assertEqual(ctx["counts"], {"upcoming": 1, "past": 0})
        self.assertEqual(self._ids(self._ctx()), [mine.pk])

    def _count(self, **params):
        with CaptureQueriesContext(connection) as queries:
            owner_visits_context(self.owner, params, self.today, self.NOW)
        return len(queries)

    def _rows(self, number):
        for step in range(number):
            original, _new = self._moved(self.admin, Appointment.PENDING)
            self._cancelled(self.owner.user, Appointment.PET_UNWELL, days=-1 - step)
            self._visit(days=-1 - step, hour=14, status=Appointment.COMPLETED)

    def test_queries_are_flat(self):
        self._second_pet()
        self._rows(1)
        few = {show: self._count(show=show) for show in ("upcoming", "past")}
        self._rows(10)
        many = {show: self._count(show=show) for show in ("upcoming", "past")}
        print(f"VISITS QUERIES (context 3 vs 33 visits): {few} {many}")
        self.assertEqual(few, many)


class OwnerVisitsPageTests(_OwnerVisitsBase):
    def test_layout_title_and_the_visits_tab(self):
        self._visit()
        response = self.client.get(reverse("owner_appointments"))
        self.assertTemplateUsed(response, "layouts/owner.html")
        self.assertTemplateNotUsed(response, "base_dashboard.html")
        html = unescape(response.content.decode())
        self.assertIn(">Visits</h1>", html)
        nav = re.search(r'(?s)<nav aria-label="Main"[^>]*>(.*?)</nav>', html).group(1)
        current = re.search(r'<a href="([^"]+)"[^>]*aria-current="page"', nav)
        self.assertEqual(current.group(1), reverse("owner_appointments"))

    def test_one_violet_button_to_book(self):
        self._visit()
        found = self._violet(self._html())
        self.assertEqual(len(found), 1)
        self.assertIn(f'href="{reverse("owner_book_appointment")}"', found[0])
        self.assertIn("Book a visit</a>", self._html())

    def test_segments_and_chips_are_links_that_keep_the_other_param(self):
        second = self._second_pet()
        self._visit(days=2)
        self._visit(days=3, pet=second)
        self._visit(days=-2, status=Appointment.COMPLETED)
        base = reverse("owner_appointments")
        html = self._html()
        segments = re.search(
            r'(?s)<nav aria-label="Which visits to show".*?</nav>', html
        ).group(0)
        self.assertEqual(segments.count('aria-current="page"'), 1)
        self.assertRegex(segments, r'aria-current="page"[^>]*>Upcoming \(2\)</a>')
        self.assertIn("Past (1)</a>", segments)
        picked = self._html(show="past", pet=second.pk)
        for query in (f"show=upcoming&pet={second.pk}", f"show=past&pet={second.pk}"):
            self.assertIn(f'<a href="{base}?{query}"', picked)
        chips = re.search(r'(?s)<nav aria-label="Show visits for".*?</nav>', picked)
        chips = chips.group(0)
        self.assertEqual(chips.count('aria-current="true"'), 1)
        self.assertRegex(chips, r'aria-current="true"[^>]*>Mingming</a>')
        self.assertIn(f'<a href="{base}?show=past"', chips)
        self.assertIn(f'<a href="{base}?show=past&pet={self.pet.pk}"', chips)
        self.assertIn("bg-violet-100 font-semibold text-violet-800", chips)
        self.assertIn("Upcoming (1)</a>", picked)
        self.assertIn("Past (0)</a>", picked)

    def test_no_chips_with_one_pet(self):
        self._visit()
        self.assertNotIn('aria-label="Show visits for"', self._html())

    def test_badge_words_and_colours(self):
        self._visit(days=2, status=Appointment.PENDING)
        self._visit(days=3)
        self._visit(days=-1, status=Appointment.COMPLETED)
        self._visit(days=-2, status=Appointment.CANCELLED)
        self._visit(days=-3, status=Appointment.NO_SHOW)
        upcoming, past = self._html(), self._html(show="past")
        cases = [
            (upcoming, "yellow", "Waiting for approval"),
            (upcoming, "blue", "Confirmed"),
            (past, "green", "Completed"),
            (past, "red", "Cancelled"),
            (past, "gray", "Missed"),
        ]
        for html, colour, word in cases:
            with self.subTest(word=word):
                classes = f"badge bg-{colour}-100 text-{colour}-800"
                self.assertIn(f'<span class="{classes}">{word}</span>', html)

    def test_each_card_is_one_link_with_the_notes(self):
        self._visit(days=2, status=Appointment.PENDING)
        self._moved(self.admin, Appointment.CONFIRMED)
        self._moved(self.owner.user, Appointment.PENDING)
        upcoming = self._html()
        for note in (
            "The clinic will confirm this soon.",
            "Moved by the clinic. Was Mon, Nov 9 at 3:00 PM.",
            "You changed the time. Was Mon, Nov 9 at 3:00 PM.",
        ):
            with self.subTest(note=note):
                self.assertIn(note, upcoming)
        self.assertEqual(upcoming.count("M16 14v2.2l1.6 1"), 2)
        self.assertNotIn("Rescheduled", upcoming)
        # three cards: the request and the two new visits (the originals are left out)
        links = re.findall(r'<a href="/owner/appointments/\d+/"', upcoming)
        self.assertEqual(len(links), 3)

    def test_past_notes(self):
        self._cancelled(self.owner.user, Appointment.CHANGE_OF_PLANS, days=-1)
        self._cancelled(self.admin, Appointment.DECLINED, days=-2)
        self._cancelled(self.admin, days=-3)
        self._visit(days=-4, status=Appointment.NO_SHOW)
        self._visit(days=0, hour=8, status=Appointment.PENDING)
        past = self._html(show="past")
        for note in (
            "You cancelled: change of plans.",
            "Declined by the clinic.",
            "Cancelled by the clinic. Please call us if you need a new time.",
            "Marked as missed by the clinic.",
            "The clinic hasn't updated this visit yet.",
        ):
            with self.subTest(note=note):
                self.assertIn(note, past)
        self.assertNotIn("M16 14v2.2l1.6 1", past)

    def test_the_card_parts(self):
        self._visit(days=8, hour=12, service=None, status=Appointment.PENDING)
        html = self._html()
        self.assertIn(">Nov</span>", html)
        self.assertIn(">10</span>", html)
        self.assertIn(">Tue</span>", html)
        self.assertIn("Bantay</span>", html)
        self.assertIn("Not sure yet · 12:00 PM", html)
        self.assertIn(">November 2026</h2>", html)

    def test_today_is_a_violet_tile_and_a_ringed_card(self):
        self._visit(days=0, hour=10)
        self._visit(days=1)
        html = self._html()
        self.assertEqual(html.count("border-violet-300 ring-1 ring-violet-200"), 1)
        self.assertEqual(html.count(">Today</span>"), 1)
        self.assertEqual(len(self._violet(html)), 1)

    def test_no_upcoming_visits(self):
        self._visit(days=-2, status=Appointment.COMPLETED)
        html = self._html()
        self.assertIn("No upcoming visits", html)
        self.assertIn(
            "When you book a visit, it will show here until the day comes.", html
        )
        self.assertIn("Past (1)", html)

    def test_no_past_visits(self):
        self._visit(days=2)
        html = self._html(show="past")
        self.assertIn("No past visits yet", html)
        self.assertIn("Your visits will show here after the day.", html)

    def test_a_pet_filter_with_no_visits(self):
        second = self._second_pet()
        self._visit(days=2)
        html = self._html(pet=second.pk)
        self.assertIn("No visits for this pet here", html)
        self.assertIn("Pick another pet, or All.", html)
        self.assertEqual(html.count(">All</a>"), 2)
        self.assertIn(f'<a href="{reverse("owner_appointments")}?show=upcoming"', html)

    def test_no_pets_at_all(self):
        self.pet.delete()
        response = self.client.get(reverse("owner_appointments"))
        html = unescape(response.content.decode())
        self.assertTemplateUsed(response, "layouts/owner.html")
        self.assertIn("No visits yet", html)
        self.assertIn(
            "Add your pet first. Then you can book a visit with Dr. Aga.", html
        )
        found = self._violet(html)
        self.assertEqual(len(found), 1)
        add = f'href="{reverse("owner_pet_add")}?next=book"'
        self.assertIn(add, found[0])
        self.assertIn("Add a pet</a>", html)
        self.assertNotIn("Which visits to show", html)
        self.assertNotIn("Book a visit", html)

    def test_the_past_pager(self):
        for number in range(30):
            self._visit(days=-1 - number, status=Appointment.COMPLETED)
        first = self._html(show="past")
        self.assertIn("Page 1 of 2", first)
        self.assertIn('<a href="?show=past&page=2"', first)
        cards = re.findall(r'<a href="/owner/appointments/\d+/"', first)
        self.assertEqual(len(cards), 25)
        second = self._html(show="past", page=2)
        self.assertIn("Page 2 of 2", second)
        self.assertIn('<a href="?show=past&page=1"', second)
        self.assertNotIn("Page 1 of", self._html())  # one page: no pager

    def test_markup_rules(self):
        second = self._second_pet()
        self._visit(days=0, hour=10)
        self._visit(days=2, pet=second, status=Appointment.PENDING)
        self._cancelled(self.owner.user, Appointment.PET_UNWELL, days=-1)
        for params in ({}, {"show": "past"}, {"pet": second.pk}):
            with self.subTest(params=params):
                html = self._html(**params)
                self.assertIsNone(re.search(r"<a\b[^>]*\n", html))
                scripts = re.findall(r"<script\b[^>]*>", html)
                self.assertTrue(all(" src=" in tag for tag in scripts))
                self.assertIsNone(re.search(r"<[a-z][^>]*\son[a-z]+=", html))
                self.assertNotIn("hx-confirm", html)

    def test_the_list_query_count_is_flat(self):
        self._second_pet()
        self._visit(days=2)
        self._visit(days=-2, status=Appointment.COMPLETED)
        few = (self._queries(), self._queries(show="past"))
        for number in range(10):
            self._moved(self.admin, Appointment.CONFIRMED)
            self._cancelled(self.owner.user, Appointment.PET_UNWELL, days=-3 - number)
            self._visit(days=-3 - number, hour=14, status=Appointment.COMPLETED)
        many = (self._queries(), self._queries(show="past"))
        print(f"VISITS QUERIES (page: upcoming, past): {few} then {many}")
        self.assertEqual(few, many)

    def test_the_clinic_is_sent_to_its_own_dashboard(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("owner_appointments"))
        self.assertRedirects(
            response, reverse("admin_dashboard"), fetch_redirect_response=False
        )


class OwnerVisitDetailPageTests(_OwnerVisitsBase):
    def _make(self, state):
        """A visit in each state of the page (the moved one is the original)."""
        if state in ("pending", "confirmed"):
            return self._visit(days=8, hour=12, status=state)
        if state == "ended":
            return self._visit(days=0, hour=8)
        if state in ("completed", "no_show"):
            return self._visit(days=-1, status=state)
        if state == "declined":
            return self._cancelled(self.admin, Appointment.DECLINED)
        if state == "cancelled_clinic":
            return self._cancelled(self.admin)
        if state == "cancelled_owner":
            return self._cancelled(self.owner.user, Appointment.PET_UNWELL)
        return self._moved(self.admin, Appointment.CONFIRMED)[0]

    def _page(self, visit):
        return self._html("owner_appointment_detail", [visit.pk])

    def _receipt(self, visit, status, number="HV-2026-0003"):
        receipt = BillingReceipt.objects.create(
            appointment=visit,
            owner=self.owner,
            pet=self.pet,
            receipt_number=number,
            payment_status=status,
            total_amount=Decimal("850.00"),
        )
        # An owner only sees an Unpaid bill that has an item (total stays 850)
        BillingItem.objects.create(
            receipt=receipt,
            description="Consultation",
            quantity=1,
            unit_price=Decimal("850.00"),
            subtotal=0,  # Overwritten by BillingItem.save()
        )
        receipt.calculate_totals()
        return receipt

    def test_layout_back_bar_and_the_top(self):
        visit = self._visit(days=8, hour=12)
        response = self.client.get(reverse("owner_appointment_detail", args=[visit.pk]))
        self.assertTemplateUsed(response, "layouts/owner.html")
        self.assertTemplateNotUsed(response, "base_dashboard.html")
        html = unescape(response.content.decode())
        visits = reverse("owner_appointments")
        self.assertRegex(html, rf'<a href="{visits}"[^>]*>(?s:.*?)Back to Visits</a>')
        self.assertIn("Bantay · Consultation</h1>", html)
        nav = re.search(r'(?s)<nav aria-label="Main"[^>]*>(.*?)</nav>', html).group(1)
        current = re.search(r'<a href="([^"]+)"[^>]*aria-current="page"', nav)
        self.assertEqual(current.group(1), reverse("owner_appointments"))
        self.assertNotIn("Waiting for approval", html)

    def test_the_friendly_when(self):
        cases = [
            (dict(days=0, hour=10), "Today, Mon, Nov 2 at 10:00 AM"),
            (dict(days=1, hour=8), "Tomorrow, Tue, Nov 3 at 8:00 AM"),
            (dict(days=8, hour=12), "Tue, Nov 10 at 12:00 PM"),
            (dict(days=100, hour=12), "Wed, Feb 10, 2027 at 12:00 PM"),
        ]
        for fields, expected in cases:
            with self.subTest(expected=expected):
                self.assertIn(expected, self._page(self._visit(**fields)))

    def test_the_banner_for_each_state(self):
        for state, (colours, title, text) in BANNERS.items():
            with self.subTest(state=state):
                html = self._page(self._make(state))
                self.assertIn(colours, html)
                self.assertIn(f">{title}</p>", html)
                self.assertIn(f">{text}</p>", html)

    def test_confirmed_says_when_to_come(self):
        cases = [
            (dict(days=0, hour=10), "See you today at 10:00 AM."),
            (dict(days=1, hour=8), "See you tomorrow at 8:00 AM."),
            (dict(days=8, hour=12), "See you on Tue, Nov 10 at 12:00 PM."),
        ]
        for fields, expected in cases:
            with self.subTest(expected=expected):
                html = self._page(self._visit(**fields))
                self.assertIn(f"{expected} Please come 10 minutes early.", html)

    def test_a_moved_original_links_to_the_new_visit(self):
        original, new = self._moved(self.admin, Appointment.CONFIRMED)
        html = self._page(original)
        link = reverse("owner_appointment_detail", args=[new.pk])
        self.assertRegex(html, rf'<a href="{link}"[^>]*>See the new visit</a>')
        self.assertEqual(self._violet(html), [])
        for words in ("Book again", "Book another visit", "Book a new visit"):
            self.assertNotIn(words, html)

    def test_a_clinic_move_and_an_owner_change_say_what_was_moved(self):
        _a, by_clinic = self._moved(self.admin, Appointment.CONFIRMED)
        _b, by_owner = self._moved(self.owner.user, Appointment.PENDING)
        self.assertIn(
            "Moved by the clinic. Was Mon, Nov 9 at 3:00 PM.", self._page(by_clinic)
        )
        self.assertIn(
            "You changed the time. Was Mon, Nov 9 at 3:00 PM.", self._page(by_owner)
        )

    def test_completed_without_a_record_or_a_bill(self):
        html = self._page(self._make("completed"))
        self.assertNotIn("The vet's notes and the bill are below.", html)
        self.assertNotIn("Vet's notes and medicines", html)
        self.assertNotIn("Bill ", html)

    def test_completed_with_a_record_and_a_bill(self):
        visit = self._make("completed")
        record = MedicalRecord.objects.create(
            pet=self.pet, appointment=visit, record_date=self.today
        )
        receipt = self._receipt(visit, "pending")
        html = self._page(visit)
        self.assertIn(
            "Thank you for coming in. The vet's notes and the bill are below.", html
        )
        notes = reverse("owner_medical_record", args=[self.pet.pk, record.pk])
        bill = reverse("owner_receipt_detail", args=[receipt.pk])
        self.assertRegex(html, rf'<a href="{notes}"[^>]*>')
        self.assertIn("Vet's notes and medicines", html)
        self.assertIn("What Dr. Aga found and what to give Bantay", html)
        self.assertRegex(html, rf'<a href="{bill}"[^>]*>')
        self.assertIn("Bill HV-2026-0003 · Unpaid", html)
        self.assertIn("₱850.00", html)
        BillingReceipt.objects.filter(pk=receipt.pk).update(payment_status="paid")
        self.assertIn("Bill HV-2026-0003 · Paid", self._page(visit))

    def test_a_cancelled_receipt_is_never_shown(self):
        visit = self._make("completed")
        self._receipt(visit, "cancelled")
        html = self._page(visit)
        self.assertNotIn("Bill HV-2026-0003", html)
        self.assertNotIn("₱850.00", html)
        self.assertNotIn("the bill are below", html)

    def test_only_a_record_or_only_a_bill_still_says_below(self):
        only_record = self._make("completed")
        MedicalRecord.objects.create(
            pet=self.pet, appointment=only_record, record_date=self.today
        )
        below = "The vet's notes and the bill are below."
        self.assertIn(below, self._page(only_record))
        only_bill = self._visit(days=-2, status=Appointment.COMPLETED)
        self._receipt(only_bill, "paid", "HV-2026-0009")
        html = self._page(only_bill)
        self.assertIn(below, html)
        self.assertNotIn("Vet's notes and medicines", html)

    def test_an_archived_pet_has_no_notes_link_and_a_plain_book_link(self):
        visit = self._make("completed")
        MedicalRecord.objects.create(
            pet=self.pet, appointment=visit, record_date=self.today
        )
        self.pet.is_archived = True
        self.pet.save()
        html = self._page(visit)
        self.assertNotIn("Vet's notes and medicines", html)
        pet_link = reverse("owner_pet_detail", args=[self.pet.pk])
        self.assertNotIn(f'href="{pet_link}"', html)
        found = self._violet(html)
        self.assertEqual(len(found), 1)
        self.assertIn(f'href="{reverse("owner_book_appointment")}"', found[0])

    def test_the_detail_rows(self):
        Pet.objects.filter(pk=self.pet.pk).update(breed="Aspin")
        visit = self._visit(days=8, hour=12, reason="Not eating since yesterday.")
        html = self._page(visit)
        pet_link = reverse("owner_pet_detail", args=[self.pet.pk])
        self.assertRegex(html, rf'<a href="{pet_link}"[^>]*>Bantay</a> · Dog · Aspin')
        for label, value in (
            ("Visit for", "Consultation"),
            ("Day and time", "Tuesday, Nov 10, 2026 at 12:00 PM"),
            ("Your note for the vet", "Not eating since yesterday."),
            ("Booked on", "Nov 2, 2026"),
        ):
            with self.subTest(label=label):
                self.assertRegex(
                    html, rf"<dt[^>]*>{label}</dt>\s*<dd[^>]*>{value}</dd>"
                )
        self.assertNotIn(">Cancelled</dt>", html)

    def test_the_note_row_is_left_out_when_empty(self):
        html = self._page(self._visit(days=8))
        self.assertNotIn("Your note for the vet", html)

    def test_a_past_visit_says_was_on(self):
        html = self._page(self._make("completed"))
        self.assertIn(">Was on</dt>", html)
        self.assertNotIn(">Day and time</dt>", html)

    def test_cancelled_rows(self):
        cases = {
            "cancelled_owner": ("Cancelled", "Nov 2, 2026 by you"),
            "cancelled_clinic": ("Cancelled", "Nov 2, 2026 by the clinic"),
            "declined": ("Declined", "Nov 2, 2026 by the clinic"),
        }
        for state, (label, value) in cases.items():
            with self.subTest(state=state):
                html = self._page(self._make(state))
                self.assertRegex(
                    html, rf"<dt[^>]*>{label}</dt>\s*<dd[^>]*>{value}</dd>"
                )
                self.assertIn(">Was on</dt>", html)

    def test_the_internal_notes_never_show(self):
        for state in BANNERS:
            with self.subTest(state=state):
                visit = self._make(state)
                Appointment.objects.filter(pk=visit.pk).update(
                    notes="Internal: bite risk"
                )
                self.assertNotIn("Internal: bite risk", self._page(visit))
                self.assertNotIn("Admin Notes", self._page(visit))

    def test_the_buttons_for_each_state(self):
        change = {"pending", "confirmed"}
        book = {
            "completed": "Book another visit",
            "cancelled_owner": "Book another visit",
            "no_show": "Book a new visit",
        }
        call_book = {"declined", "cancelled_clinic"}
        for state in BANNERS:
            with self.subTest(state=state):
                html = self._page(self._make(state))
                violet = self._violet(html)
                has_change = state in change
                self.assertEqual("Change day or time" in html, has_change)
                self.assertEqual("Need to change it?" in html, has_change)
                self.assertEqual('data-dialog-open="cancel-sheet"' in html, has_change)
                self.assertEqual('href="tel:09542889774"' in html, state in call_book)
                if state in book:
                    self.assertEqual(len(violet), 1)
                    self.assertIn(f"{book[state]}</a>", html)
                elif state in call_book:
                    self.assertEqual(len(violet), 1)
                    self.assertIn("Book again</a>", html)
                else:
                    self.assertEqual(violet, [])

    def test_change_and_cancel_buttons(self):
        pending = self._visit(days=8, status=Appointment.PENDING)
        confirmed = self._visit(days=8)
        html = self._page(pending)
        reschedule = reverse("owner_appointment_reschedule", args=[pending.pk])
        change = rf'<a href="{reschedule}"[^>]*>(?s:.*?)Change day or time</a>'
        self.assertRegex(html, change)
        cancel = r'<button type="button" data-dialog-open="cancel-sheet"[^>]*>'
        self.assertRegex(html, cancel + r"(?s:.*?)Cancel this request</button>")
        self.assertIn("Cancel this visit</button>", self._page(confirmed))
        self.assertNotIn("Cancel this request", self._page(confirmed))

    def test_a_visit_that_has_started_cannot_be_changed(self):
        html = self._page(self._visit(days=0, hour=9))
        self.assertNotIn("Need to change it?", html)
        self.assertNotIn("cancel-sheet", html)

    def test_book_links_go_to_the_pet(self):
        book = f'{reverse("owner_book_appointment")}?pet={self.pet.pk}'
        for state in ("completed", "declined", "no_show"):
            with self.subTest(state=state):
                found = self._violet(self._page(self._make(state)))
                self.assertIn(f'href="{book}"', found[0])

    def test_the_call_row_without_a_phone(self):
        ClinicSettings.objects.update(contact_number="")
        html = self._page(self._make("declined"))
        self.assertNotIn("tel:", html)
        self.assertNotIn(">Call</a>", html)
        self.assertIn("Book again</a>", html)
        self.assertEqual(len(self._violet(html)), 1)

    def test_the_call_row_with_a_phone(self):
        html = self._page(self._make("cancelled_clinic"))
        self.assertRegex(html, r'<a href="tel:09542889774"[^>]*>(?s:.*?)Call</a>')

    def test_the_cancel_sheet_title_text_and_actions(self):
        confirmed = self._visit(days=8, hour=12)
        pending = self._visit(days=8, hour=12, status=Appointment.PENDING)
        html = self._page(confirmed)
        self.assertIn("Cancel Bantay's visit?</h2>", html)
        self.assertIn("Tue, Nov 10 at 12:00 PM · Consultation", html)
        self.assertIn("Yes, cancel the visit", html)
        self.assertIn(">Keep the visit</button>", html)
        html = self._page(pending)
        self.assertIn("Cancel Bantay's request?</h2>", html)
        self.assertIn("Yes, cancel the request", html)
        self.assertIn(">Keep the visit</button>", html)

    def test_the_late_note_only_within_24_hours(self):
        tomorrow = self._visit(days=1, hour=8)  # 22.5 hours away
        far = self._visit(days=3, hour=9)
        late = (
            "It's less than 24 hours away, "
            "so the clinic will see this as a late cancel."
        )
        html = self._page(tomorrow)
        self.assertIn(late, html)
        self.assertIn("If you can, call them instead:", html)
        self.assertIn(PHONE, html)
        self.assertIn("bg-orange-50", html)
        self.assertNotIn("late cancel", self._page(far))

    def test_the_late_note_without_a_phone(self):
        ClinicSettings.objects.update(contact_number="")
        html = self._page(self._visit(days=1, hour=8))
        self.assertIn("as a late cancel.</p>", html)
        self.assertNotIn("call them instead", html)

    def test_the_late_note_is_never_on_the_page_itself(self):
        html = self._page(self._visit(days=1, hour=8))
        before = html.split('<dialog id="cancel-sheet"')[0]
        self.assertNotIn("late cancel", before)

    def test_the_sheet_reasons_and_the_other_box(self):
        visit = self._visit(days=8)
        html = self._page(visit)
        self.assertIn(">Why are you cancelling?</legend>", html)
        for value, words in (
            ("change_of_plans", "Change of plans"),
            ("pet_unwell", "My pet is unwell"),
            ("cannot_make_time", "I can't make it at that time"),
            ("other", "Other reason"),
        ):
            with self.subTest(value=value):
                radio = (
                    r'<label[^>]*><input type="radio" name="cancellation_reason" '
                    rf'value="{value}" class="peer sr-only"><span[^>]*>{words}</span>'
                )
                self.assertRegex(html, radio)
        self.assertNotIn('value="declined"', html)
        self.assertIn(">Tell us a little more</label>", html)
        self.assertIn('<textarea id="cancel-detail" name="cancellation_detail"', html)
        self.assertIn("not-group-has-[input[value=other]:checked]/why:hidden", html)
        self.assertNotIn("checked>", html.split('<dialog id="cancel-sheet"')[1])

    def test_the_sheet_is_a_post_form_with_a_csrf_token(self):
        visit = self._visit(days=8)
        html = self._page(visit)
        url = reverse("owner_appointment_cancel", args=[visit.pk])
        form = (
            rf'<form method="post" action="{url}"[^>]*>\s*'
            r'<input type="hidden" name="csrfmiddlewaretoken" value="[^"]+">'
        )
        self.assertRegex(html, form)
        self.assertNotIn(f'href="{url}"', html)
        self.assertEqual(len(self._violet(html)), 0)

    def test_no_sheet_for_other_states(self):
        for state in BANNERS:
            if state in ("pending", "confirmed"):
                continue
            with self.subTest(state=state):
                self.assertNotIn("cancel-sheet", self._page(self._make(state)))

    def test_another_owners_visit_is_a_404(self):
        other, theirs = self._other_owner()
        visit = Appointment.objects.create(
            owner=other,
            pet=theirs,
            date=self.today + datetime.timedelta(days=2),
            time=datetime.time(9, 0),
            status=Appointment.CONFIRMED,
        )
        for pk in (visit.pk, 999999):
            response = self.client.get(reverse("owner_appointment_detail", args=[pk]))
            self.assertEqual(response.status_code, 404)

    def test_the_clinic_is_sent_to_its_own_dashboard(self):
        visit = self._visit()
        self.client.force_login(self.admin)
        response = self.client.get(reverse("owner_appointment_detail", args=[visit.pk]))
        self.assertRedirects(
            response, reverse("admin_dashboard"), fetch_redirect_response=False
        )

    def test_markup_rules(self):
        for state in BANNERS:
            with self.subTest(state=state):
                html = self._page(self._make(state))
                self.assertIsNone(re.search(r"<a\b[^>]*\n", html))
                scripts = re.findall(r"<script\b[^>]*>", html)
                self.assertTrue(all(" src=" in tag for tag in scripts))
                self.assertIsNone(re.search(r"<[a-z][^>]*\son[a-z]+=", html))
                self.assertNotIn("hx-confirm", html)
                self.assertLessEqual(len(self._violet(html)), 1)

    def test_the_detail_query_count_is_flat_per_state(self):
        totals = {}
        visits = {state: self._make(state) for state in BANNERS}
        for state, visit in visits.items():
            if state == "completed":
                self._receipt(visit, "paid")
                MedicalRecord.objects.create(
                    pet=self.pet, appointment=visit, record_date=self.today
                )
            totals[state] = self._queries("owner_appointment_detail", [visit.pk])
        for number in range(6):
            self._visit(days=-5 - number, status=Appointment.COMPLETED)
            self._cancelled(self.admin, days=-5 - number)
        for state, visit in visits.items():
            with self.subTest(state=state):
                count = self._queries("owner_appointment_detail", [visit.pk])
                self.assertEqual(count, totals[state])
        print(f"VISIT DETAIL QUERIES (whole request, per state): {totals}")

    # ── The clinic's message, the owner's own reason, a missing canceller ────

    MESSAGE = "Message from the clinic:</strong>"

    def test_the_clinic_message_shows_for_a_clinic_cancel_and_a_decline(self):
        cases = {
            "cancelled_clinic": self._cancelled(
                self.admin, cancellation_detail="We are closed that day.\nSorry."
            ),
            "declined": self._cancelled(
                self.admin,
                Appointment.DECLINED,
                cancellation_detail="No vet on Friday <b>please</b> rebook.",
            ),
        }
        for state, visit in cases.items():
            with self.subTest(state=state):
                html = self._page(visit)
                self.assertEqual(html.count(self.MESSAGE), 1)
                self.assertIn(">Message from the clinic:</strong> ", html)
        clinic = self._page(cases["cancelled_clinic"])
        self.assertIn("We are closed that day.<br>Sorry.", clinic)
        url = reverse("owner_appointment_detail", args=[cases["declined"].pk])
        raw = self.client.get(url).content.decode()  # not decoded: still escaped
        self.assertIn("No vet on Friday &lt;b&gt;please&lt;/b&gt; rebook.", raw)

    def test_the_clinic_message_is_left_out_when_empty(self):
        placeholder = self._cancelled(
            self.admin, Appointment.DECLINED, cancellation_detail="Rejected by clinic."
        )
        plain = (self._make("cancelled_clinic"), self._make("declined"))
        for visit in plain + (placeholder,):
            with self.subTest(pk=visit.pk):
                self.assertNotIn("Message from the clinic", self._page(visit))

    def test_a_moved_original_never_shows_the_message(self):
        original, _new = self._moved(self.admin, Appointment.CONFIRMED)
        Appointment.objects.filter(pk=original.pk).update(
            cancellation_detail="Rescheduled by clinic."
        )
        html = self._page(original)
        self.assertNotIn("Message from the clinic", html)
        self.assertNotIn("Rescheduled by clinic.", html)

    def test_the_internal_notes_never_show_even_with_a_message(self):
        visit = self._cancelled(self.admin, cancellation_detail="See you soon.")
        Appointment.objects.filter(pk=visit.pk).update(notes="Internal: bite risk")
        html = self._page(visit)
        self.assertIn("See you soon.", html)
        self.assertNotIn("Internal: bite risk", html)

    def test_other_shows_the_owners_reason_once(self):
        visit = self._cancelled(
            self.owner.user, Appointment.OTHER, cancellation_detail="Moving away."
        )
        html = self._page(visit)
        self.assertEqual(html.count("You cancelled this visit"), 1)
        self.assertEqual(html.count("Your reason: Moving away."), 1)
        self.assertNotIn("Message from the clinic", html)
        self.assertIn(">Cancelled</dt>", html)

    def test_other_without_a_detail_falls_back_to_the_plain_line(self):
        html = self._page(self._cancelled(self.owner.user, Appointment.OTHER))
        self.assertEqual(html.count("You cancelled this visit"), 2)
        self.assertNotIn("Your reason:", html)

    def test_cancelled_with_nobody_recorded_counts_as_the_clinic(self):
        visit = self._cancelled(None, cancellation_detail="Closed.")
        html = self._page(visit)
        self.assertIn(">Cancelled by the clinic</p>", html)
        self.assertIn("Closed.", html)
        self.assertIn("Nov 2, 2026 by the clinic", html)
        # and a move whose original has nobody recorded is "Moved by the clinic"
        original = self._cancelled(None, days=7, hour=15)
        new = self._visit(days=8, hour=12, rescheduled_from=original)
        self.assertIn("Moved by the clinic. Was Mon, Nov 9", self._page(new))


class OwnerCancelSheetPostTests(_OwnerVisitsBase):
    def _url(self, visit):
        return reverse("owner_appointment_cancel", args=[visit.pk])

    def _detail(self, visit):
        return reverse("owner_appointment_detail", args=[visit.pk])

    def _cancel(self, visit, **data):
        # Show a page first, so flashes left by an earlier post in the same
        # test are used up and _messages sees only this post's
        self.client.get(reverse("owner_appointments"))
        return self.client.post(self._url(visit), data)

    def _admin_notices(self):
        return Notification.objects.filter(
            recipient=self.admin, notification_type="appointment_cancelled"
        )

    def _assert_untouched(self, visit, status):
        visit.refresh_from_db()
        self.assertEqual(visit.status, status)
        self.assertEqual(visit.cancellation_reason, "")
        self.assertEqual(visit.cancellation_detail, "")
        self.assertIsNone(visit.cancelled_by)
        self.assertFalse(self._admin_notices().exists())

    def test_a_get_goes_back_to_the_visit(self):
        visit = self._visit()
        response = self.client.get(self._url(visit))
        self.assertRedirects(
            response, self._detail(visit), fetch_redirect_response=False
        )
        self.assertEqual(self._messages(response), [])
        self._assert_untouched(visit, Appointment.CONFIRMED)

    def test_no_reason_is_an_error_flash_and_nothing_changes(self):
        bad = ("", "declined", "nonsense")
        for data in [{}] + [{"cancellation_reason": value} for value in bad]:
            with self.subTest(data=data):
                visit = self._visit()
                response = self._cancel(visit, **data)
                self.assertRedirects(
                    response, self._detail(visit), fetch_redirect_response=False
                )
                self.assertEqual(
                    self._messages(response), ["Choose why you're cancelling."]
                )
                self._assert_untouched(visit, Appointment.CONFIRMED)

    def test_other_without_a_detail_is_an_error_flash(self):
        for detail in ("", "   "):
            with self.subTest(detail=detail):
                visit = self._visit()
                response = self._cancel(
                    visit, cancellation_reason="other", cancellation_detail=detail
                )
                self.assertEqual(
                    self._messages(response),
                    ["Tell us a little more about the reason."],
                )
                self._assert_untouched(visit, Appointment.CONFIRMED)

    def test_a_valid_cancel_saves_what_it_always_did(self):
        for reason in ("change_of_plans", "pet_unwell", "cannot_make_time"):
            with self.subTest(reason=reason):
                visit = self._visit()
                response = self._cancel(visit, cancellation_reason=reason)
                self.assertRedirects(
                    response, self._detail(visit), fetch_redirect_response=False
                )
                self.assertEqual(self._messages(response), ["Visit cancelled."])
                visit.refresh_from_db()
                self.assertEqual(visit.status, Appointment.CANCELLED)
                self.assertEqual(visit.cancellation_reason, reason)
                self.assertEqual(visit.cancellation_detail, "")
                self.assertEqual(visit.cancelled_by, self.owner.user)

    def test_other_with_a_detail(self):
        visit = self._visit()
        self._cancel(
            visit, cancellation_reason="other", cancellation_detail="Moving away."
        )
        visit.refresh_from_db()
        self.assertEqual(visit.status, Appointment.CANCELLED)
        self.assertEqual(visit.cancellation_reason, Appointment.OTHER)
        self.assertEqual(visit.cancellation_detail, "Moving away.")
        self.assertEqual(visit.cancelled_by, self.owner.user)

    def test_a_pending_request_says_request_cancelled(self):
        visit = self._visit(status=Appointment.PENDING)
        response = self._cancel(visit, cancellation_reason="change_of_plans")
        self.assertEqual(self._messages(response), ["Request cancelled."])
        visit.refresh_from_db()
        self.assertEqual(visit.status, Appointment.CANCELLED)

    def test_the_admin_is_told_as_before(self):
        visit = self._visit()
        self._cancel(visit, cancellation_reason="pet_unwell")
        notice = self._admin_notices().get()
        self.assertEqual(notice.title, "Appointment Cancelled")
        self.assertEqual(
            notice.message,
            "Ana Cruz has cancelled their appointment for Bantay on "
            "November 05, 2026 at 09:00 AM.",
        )
        self.assertEqual(notice.related_appointment, visit)

    def test_a_late_cancel_still_works(self):
        visit = self._visit(days=1, hour=8)
        self._cancel(visit, cancellation_reason="pet_unwell")
        visit.refresh_from_db()
        self.assertEqual(visit.status, Appointment.CANCELLED)

    def test_the_visit_page_after_the_cancel_shows_the_flash_and_the_banner(self):
        visit = self._visit()
        response = self.client.post(
            self._url(visit), {"cancellation_reason": "pet_unwell"}, follow=True
        )
        html = unescape(response.content.decode())
        self.assertIn("Visit cancelled.", html)
        self.assertIn("You cancelled this visit", html)
        self.assertIn("You cancelled: pet is unwell.", html)

    def test_only_a_pending_or_confirmed_visit_can_be_cancelled(self):
        for status in (
            Appointment.COMPLETED,
            Appointment.CANCELLED,
            Appointment.NO_SHOW,
        ):
            with self.subTest(status=status):
                visit = self._visit(days=-1, status=status)
                response = self._cancel(visit, cancellation_reason="pet_unwell")
                self.assertRedirects(
                    response, self._detail(visit), fetch_redirect_response=False
                )
                self.assertEqual(
                    self._messages(response), ["This appointment cannot be cancelled."]
                )
                self._assert_untouched(visit, status)

    def test_another_owners_visit_is_a_404_and_unchanged(self):
        other, theirs = self._other_owner()
        visit = Appointment.objects.create(
            owner=other,
            pet=theirs,
            date=self.today + datetime.timedelta(days=2),
            time=datetime.time(9, 0),
            status=Appointment.CONFIRMED,
        )
        response = self._cancel(visit, cancellation_reason="pet_unwell")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.client.get(self._url(visit)).status_code, 404)
        self._assert_untouched(visit, Appointment.CONFIRMED)

    def test_the_clinic_cannot_use_the_owner_cancel(self):
        visit = self._visit()
        self.client.force_login(self.admin)
        response = self._cancel(visit, cancellation_reason="pet_unwell")
        self.assertRedirects(
            response, reverse("admin_dashboard"), fetch_redirect_response=False
        )
        self._assert_untouched(visit, Appointment.CONFIRMED)


# ─── Slice 8b-7b: owner Change day or time ───────────────────────────────────

# Thu, Nov 12, 2026: ten days after the frozen "today" (Mon, Nov 2)
CHANGE_DAY = datetime.date(2026, 11, 12)


class _OwnerRescheduleBase(_OwnerVisitsBase):
    """Mon, Nov 2, 2026 at 9:30 Manila. The owner's Confirmed visit is Tue,
    Nov 10 at 12:00 PM (self.visit); the day the tests move it to is Thu,
    Nov 12. The clinic is open every day 8:00-17:00, hourly."""

    def setUp(self):
        super().setUp()
        self.visit = self._visit(days=8, hour=12)
        self.url = reverse("owner_appointment_reschedule", args=[self.visit.pk])
        self.detail = reverse("owner_appointment_detail", args=[self.visit.pk])

    def _loaded(self, visit):
        return Appointment.objects.select_related("pet", "service").get(pk=visit.pk)

    def _hold(self, day, hours):
        """Visits (the owner's pet) holding the slots of day at hours."""
        for hour in hours:
            self._visit(days=(day - self.today).days, hour=hour)

    def _rows(self, context):
        return [row for group in context["time_groups"] for row in group["times"]]


class OwnerRescheduleContextTests(_OwnerRescheduleBase):
    """owner_reschedule_context: the calendar, the picked day, its times."""

    def _context(self, visit=None, **params):
        visit = self._loaded(visit or self.visit)
        return owner_reschedule_context(
            self.owner, visit, params, self.today, timezone.now()
        )

    def test_the_calendar_marks_the_visits_own_day(self):
        cells = self._context()["calendar"]["cells"]
        self.assertEqual([c["date"] for c in cells if c["is_current"]], [BOOK_DAY])

    def test_the_month_follows_month_then_date_then_the_visit(self):
        self.assertEqual(self._context()["calendar"]["month_value"], "2026-11")
        context = self._context(month="2026-12")
        self.assertEqual(context["calendar"]["month_value"], "2026-12")
        context = self._context(date="2026-12-01")
        self.assertEqual(context["calendar"]["month_value"], "2026-12")
        self.assertEqual(context["picked_date"], datetime.date(2026, 12, 1))

    def test_an_open_day_is_picked_with_its_times(self):
        context = self._context(date=CHANGE_DAY.isoformat())
        self.assertEqual(context["picked_date"], CHANGE_DAY)
        self.assertEqual(context["date_error"], "")
        self.assertEqual(context["new_line"], "Thu, Nov 12")
        names = [group["name"] for group in context["time_groups"]]
        self.assertEqual(names, ["Morning", "Afternoon"])
        self.assertEqual(len(self._rows(context)), 9)

    def test_no_day_asked_for_is_no_error(self):
        context = self._context()
        self.assertIsNone(context["picked_date"])
        self.assertIsNone(context["new_line"])
        self.assertEqual(context["time_groups"], [])
        self.assertEqual(context["date_error"], "")

    def test_a_day_that_is_not_open_is_none_with_an_error(self):
        BlockedDate.objects.create(date=CHANGE_DAY, reason="Holiday")
        full = CHANGE_DAY + datetime.timedelta(days=1)
        self._hold(full, range(8, 17))
        cases = {
            "closed": CHANGE_DAY.isoformat(),
            "full": full.isoformat(),
            "past": "2026-11-01",
            "beyond the limit": "2026-12-03",
            "garbage": "nonsense",
            "not a date": "2026-13-45",
        }
        for name, value in cases.items():
            with self.subTest(case=name):
                context = self._context(date=value)
                self.assertIsNone(context["picked_date"])
                self.assertIsNone(context["new_line"])
                self.assertEqual(context["time_groups"], [])
                self.assertEqual(context["date_error"], DAY_NOT_OPEN_MESSAGE)

    def test_the_own_slot_is_is_own_on_its_own_day_only(self):
        self._hold(BOOK_DAY, [10])  # someone else holds 10:00 that day
        rows = self._rows(self._context(date=BOOK_DAY.isoformat()))
        own = [row for row in rows if row.get("is_own")]
        self.assertEqual([row["label"] for row in own], ["12:00 PM"])
        self.assertFalse(own[0]["taken"])
        ten = next(row for row in rows if row["label"] == "10:00 AM")
        self.assertTrue(ten["taken"])
        self.assertFalse(ten["is_own"])
        other = self._rows(self._context(date=CHANGE_DAY.isoformat()))
        self.assertFalse(any(row.get("is_own") for row in other))

    def test_time_groups_only_add_is_own_when_asked(self):
        times = [(datetime.time(9, 0), False), (datetime.time(12, 0), True)]
        plain = time_groups(times, None)
        self.assertTrue(all("is_own" not in r for g in plain for r in g["times"]))
        marked = time_groups(times, None, own=datetime.time(12, 0))
        rows = [row for group in marked for row in group["times"]]
        self.assertEqual([r["is_own"] for r in rows], [False, True])
        self.assertEqual([r["taken"] for r in rows], [False, False])

    def test_is_late_inside_24_hours_and_not_beyond(self):
        soon = self._visit(days=0, hour=15)  # five and a half hours away
        far = self._visit(days=3, hour=9)
        self.assertIs(self._context(soon)["is_late"], True)
        self.assertIs(self._context(far)["is_late"], False)
        self.assertIs(self._context()["is_late"], False)

    def test_the_lines_and_the_phone(self):
        context = self._context()
        self.assertEqual(context["now_line"], "Tue, Nov 10 at 12:00 PM")
        self.assertEqual(context["phone"], PHONE)
        self.assertEqual(context["service_words"], "Consultation")

    def test_a_posted_time_is_kept_unless_it_was_taken(self):
        post = {"date": CHANGE_DAY.isoformat(), "time": "09:00:00"}
        visit = self._loaded(self.visit)
        now = timezone.now()
        kept = owner_reschedule_context(self.owner, visit, post, self.today, now)
        self.assertEqual(kept["new_time"], "9:00 AM")
        taken = owner_reschedule_context(
            self.owner, visit, post, self.today, now, {"taken": "9:00 AM"}
        )
        self.assertEqual(taken["new_time"], "")
        self.assertEqual(taken["taken_time"], "9:00 AM")

    def test_another_owners_visit_is_never_given(self):
        other, theirs = self._other_owner()
        visit = Appointment.objects.create(
            owner=other,
            pet=theirs,
            date=BOOK_DAY,
            time=datetime.time(9, 0),
            status=Appointment.CONFIRMED,
        )
        with self.assertRaises(Http404):
            owner_reschedule_context(
                self.owner, self._loaded(visit), {}, self.today, timezone.now()
            )

    def test_who_can_change_a_visit(self):
        now = timezone.now()
        self.assertTrue(owner_can_change(self.visit, now))
        pending = self._visit(days=8, status=Appointment.PENDING)
        self.assertTrue(owner_can_change(pending, now))
        started = self._visit(days=0, hour=9)  # 9:00 began at 9:00, it is 9:30
        self.assertFalse(owner_can_change(started, now))
        for status in (
            Appointment.CANCELLED,
            Appointment.COMPLETED,
            Appointment.NO_SHOW,
        ):
            with self.subTest(status=status):
                visit = self._visit(days=8, status=status)
                self.assertFalse(owner_can_change(visit, now))

    def test_queries_are_flat_for_3_and_30_visits(self):
        def counts():
            found = {}
            visit = self._loaded(self.visit)
            for name, params in (("no day", {}), ("a day", {"date": "2026-11-12"})):
                with CaptureQueriesContext(connection) as queries:
                    owner_reschedule_context(
                        self.owner, visit, params, self.today, timezone.now()
                    )
                found[name] = len(queries)
            return found

        for i in range(2):
            self._visit(days=12 + i, hour=10)
        few = counts()
        for i in range(27):
            self._visit(days=12 + i % 15, hour=8 + i % 9)
        self.assertEqual(Appointment.objects.count(), 30)
        self.assertEqual(counts(), few)
        # settings + the calendar's two + (with a day) the day's two
        self.assertEqual(few, {"no day": 3, "a day": 5})


class OwnerReschedulePageTests(_OwnerRescheduleBase):
    """The Change day or time page (GET) on layouts/owner.html."""

    def _page(self, visit=None, **params):
        visit = visit or self.visit
        return self._html("owner_appointment_reschedule", [visit.pk], **params)

    def test_flow_frame_back_to_the_visit_and_no_bottom_tabs(self):
        html = self._page()
        back = rf'<a href="{self.detail}"[^>]*>(?s:.*?)Back to Visit</a>'
        self.assertRegex(html, back)
        self.assertNotIn('<nav aria-label="Main"', html)
        self.assertIn('data-bar="one"', html)

    def test_exactly_one_violet_button(self):
        for params in ({}, {"date": CHANGE_DAY.isoformat()}):
            with self.subTest(params=params):
                violet = self._violet(self._page(**params))
                self.assertEqual(len(violet), 1)
                self.assertIn('form="reschedule-form"', violet[0])

    def test_title_and_the_pet_line(self):
        html = self._page()
        title = '<h1 class="text-2xl font-bold text-gray-900">Change day or time</h1>'
        self.assertIn(title, html)
        self.assertIn("Bantay · Consultation", html)
        visit = self._visit(days=8, hour=13, service=None)
        self.assertIn("Bantay · Not sure yet", self._page(visit))

    def test_the_late_note_only_within_24_hours(self):
        soon = self._visit(days=0, hour=15)
        far = self._page()
        self.assertNotIn("late change", far)
        html = self._page(soon)
        line = "Your visit is less than 24 hours away, so the clinic will see this"
        self.assertIn(line + " as a late change.", html)
        self.assertIn('<strong class="font-semibold">call them instead</strong>', html)
        self.assertIn('href="tel:09542889774"', html)

    def test_the_late_note_without_a_phone_ends_after_late_change(self):
        ClinicSettings.objects.update(contact_number="")
        html = self._page(self._visit(days=0, hour=15))
        self.assertIn("as a late change.</p>", html)
        self.assertNotIn("call them instead", html)
        self.assertNotIn("tel:", html)

    def test_now_is_crossed_out(self):
        html = self._page()
        self.assertIn(
            '<p class="text-base font-semibold text-gray-500 line-through">'
            "Tue, Nov 10 at 12:00 PM</p>",
            html,
        )

    def test_the_new_box_states(self):
        new = r'(?s)<p class="text-base text-violet-800">New</p>\s*(<p.*?</p>)'
        found = re.search(new, self._page()).group(1)
        self.assertIn(">Pick a day</p>", found)
        found = re.search(new, self._page(date=CHANGE_DAY.isoformat())).group(1)
        self.assertIn("Thu, Nov 12<br>", found)
        self.assertIn("Pick a time below", found)

    def test_the_current_day_is_dashed_with_a_line_and_a_link(self):
        html = self._page()
        dashed = "outline-2 outline-dashed outline-gray-400 -outline-offset-2"
        self.assertIn(dashed, html)
        # 8b-10b-5: the key's "Your current day" span is now a line under the hint
        self.assertIn("Your current day has a dashed outline.</p>", html)
        url = re.escape(self.url)
        link = rf'<a href="{url}\?date=2026-11-10&month=2026-11" '
        self.assertRegex(html, link + r'aria-label="Nov 10, your current day"')

    def test_day_links_keep_the_month_and_month_links_keep_the_day(self):
        html = self._page(date=CHANGE_DAY.isoformat())
        self.assertIn(f"{self.url}?date=2026-11-11&month=2026-11", html)
        self.assertIn(f"{self.url}?month=2026-12&date=2026-11-12", html)

    def test_the_picked_day_is_the_violet_cell(self):
        html = self._page(date=CHANGE_DAY.isoformat())
        picked = r'<span aria-label="[^"]*, picked" aria-current="date" '
        self.assertRegex(html, picked + r'class="[^"]*">12</span>')
        self.assertEqual(html.count(", picked"), 1)

    def test_your_time_is_text_not_an_input(self):
        html = self._page(date=BOOK_DAY.isoformat())
        self.assertIn(">Your time</span>", html)
        self.assertNotIn('name="time" value="12:00:00"', html)
        self.assertIn('name="time" value="13:00:00"', html)
        other = self._page(date=CHANGE_DAY.isoformat())
        self.assertNotIn("Your time", other)

    def test_a_taken_time_is_gray_text_and_not_an_input(self):
        self._hold(CHANGE_DAY, [10])
        html = self._page(date=CHANGE_DAY.isoformat())
        taken = '<span class="line-through">10:00 AM</span>'
        self.assertIn(taken + '<span class="text-sm">Taken</span>', html)
        self.assertNotIn('name="time" value="10:00:00"', html)

    def test_no_day_and_a_bad_day(self):
        self.assertIn("Pick a day to see the times.", self._page())
        html = self._page(date="nonsense")
        self.assertIn(DAY_NOT_OPEN_MESSAGE, html)
        self.assertIn("Pick a day to see the times.", html)

    def test_the_reason_box_and_the_blue_note(self):
        html = self._page(date=CHANGE_DAY.isoformat())
        self.assertIn(">Why the change?</label>", html)
        self.assertIn(
            '<textarea id="reschedule-reason" name="reschedule_reason" rows="3"', html
        )
        self.assertIn('placeholder="e.g. I have work on that day"', html)
        self.assertIn("Needed.", html)
        self.assertIn(
            "Your Tue, Nov 10 at 12:00 PM time is given up. "
            "The new time waits for the clinic to confirm.",
            html,
        )
        # The textarea comes after the times (pick day, then time, then why)
        times_at = html.index("Times on Thu, Nov 12")
        self.assertLess(times_at, html.index('id="reschedule-reason"'))

    def test_the_form_posts_the_picked_day(self):
        html = self._page(date=CHANGE_DAY.isoformat())
        form = re.search(r'(?s)<form id="reschedule-form".*?</form>', html).group(0)
        self.assertIn('method="post"', form)
        self.assertIn('name="date" value="2026-11-12"', form)
        self.assertIn("csrfmiddlewaretoken", form)

    def test_keep_old_time_is_an_outline_link_to_the_visit(self):
        html = self._page()
        keep = rf'<a href="{self.detail}"[^>]*border-gray-300[^>]*>Keep old time</a>'
        self.assertRegex(html, keep)
        self.assertIn("Send new time</button>", html)

    def test_a_pending_visit_can_be_changed_too(self):
        pending = self._visit(days=8, status=Appointment.PENDING)
        url = reverse("owner_appointment_reschedule", args=[pending.pk])
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_another_owners_visit_is_a_404(self):
        other, theirs = self._other_owner()
        visit = Appointment.objects.create(
            owner=other,
            pet=theirs,
            date=BOOK_DAY,
            time=datetime.time(9, 0),
            status=Appointment.CONFIRMED,
        )
        url = reverse("owner_appointment_reschedule", args=[visit.pk])
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(url, {}).status_code, 404)

    def test_a_visit_that_cannot_be_changed_goes_to_its_page_with_the_flash(self):
        cases = {
            "cancelled": self._visit(days=8, status=Appointment.CANCELLED),
            "completed": self._visit(days=8, status=Appointment.COMPLETED),
            "no show": self._visit(days=8, status=Appointment.NO_SHOW),
            "started": self._visit(days=0, hour=9),
        }
        for name, visit in cases.items():
            url = reverse("owner_appointment_reschedule", args=[visit.pk])
            detail = reverse("owner_appointment_detail", args=[visit.pk])
            for method in (self.client.get, self.client.post):
                with self.subTest(case=name, method=method.__name__):
                    # Show a page first so an earlier flash is used up
                    self.client.get(reverse("owner_appointments"))
                    response = method(url)
                    self.assertRedirects(
                        response, detail, fetch_redirect_response=False
                    )
                    self.assertEqual(self._messages(response), [CANNOT_CHANGE_MESSAGE])

    def test_markup_rules(self):
        response = self.client.get(self.url, {"date": CHANGE_DAY.isoformat()})
        html = response.content.decode()
        self.assertEqual(re.findall(r"<a\b[^>]*$", html, re.M), [])
        self.assertEqual(re.findall(r"<script(?![^>]*\bsrc=)", html), [])
        self.assertEqual(re.findall(r"\son[a-z]+=", html), [])
        self.assertNotIn("hx-confirm", html)
        self.assertNotIn("<!--", html)

    def test_query_total_is_flat_for_3_and_30_visits(self):
        # Whole request, measured: 7 without a day, 9 with one (the context
        # alone is 3 and 5)
        expected = (({}, 7), ({"date": "2026-11-12"}, 9))

        def check():
            for params, count in expected:
                with self.subTest(params=params):
                    with self.assertNumQueries(count):
                        response = self.client.get(self.url, params)
                    self.assertEqual(response.status_code, 200)

        for i in range(2):
            self._visit(days=12 + i, hour=10)
        check()
        for i in range(27):
            self._visit(days=12 + i % 15, hour=8 + i % 9)
        self.assertEqual(Appointment.objects.count(), 30)
        check()


class OwnerReschedulePostTests(_OwnerRescheduleBase):
    """The page's POST: the locked reschedule_by_owner, or the page again."""

    def _send(self, visit=None, **changes):
        data = {
            "date": CHANGE_DAY.isoformat(),
            "time": "09:00:00",
            "reschedule_reason": "I have work that day",
        }
        data.update(changes)
        url = reverse("owner_appointment_reschedule", args=[(visit or self.visit).pk])
        # Show a page first so flashes left by an earlier request are used up
        self.client.get(reverse("owner_appointments"))
        return self.client.post(url, data)

    def _admin_notices(self):
        return Notification.objects.filter(
            recipient=self.admin, title="Appointment Reschedule Request"
        )

    def _assert_nothing_changed(self):
        self.visit.refresh_from_db()
        self.assertEqual(self.visit.status, Appointment.CONFIRMED)
        self.assertEqual(self.visit.cancellation_reason, "")
        self.assertIsNone(self.visit.cancelled_by)
        self.assertEqual(Appointment.objects.count(), 1)
        self.assertFalse(self._admin_notices().exists())

    def test_no_time_is_a_field_error_and_the_day_and_text_are_kept(self):
        response = self._send(time="")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["time_error"], PICK_NEW_TIME_MESSAGE)
        self.assertEqual(response.context["picked_date"], CHANGE_DAY)
        self.assertEqual(response.context["reason_text"], "I have work that day")
        html = unescape(response.content.decode())
        self.assertIn(PICK_NEW_TIME_MESSAGE, html)
        self.assertIn(">I have work that day</textarea>", html)
        self._assert_nothing_changed()

    def test_no_reason_is_a_field_error_and_the_time_is_kept(self):
        for text in ("", "   "):
            with self.subTest(text=text):
                response = self._send(reschedule_reason=text)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["reason_error"], WHY_MISSING_MESSAGE)
                self.assertEqual(response.context["new_time"], "9:00 AM")
                self._assert_nothing_changed()

    def test_a_day_that_is_not_open_shows_the_date_error(self):
        BlockedDate.objects.create(date=CHANGE_DAY, reason="Holiday")
        past = (self.today - datetime.timedelta(days=1)).isoformat()
        for value in (CHANGE_DAY.isoformat(), past, "nonsense", "2026-12-03"):
            with self.subTest(date=value):
                response = self._send(date=value)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["date_error"], DAY_NOT_OPEN_MESSAGE)
                self.assertIsNone(response.context["picked_date"])
                self._assert_nothing_changed()

    def test_no_day_at_all(self):
        response = self._send(date="")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["date_error"], PICK_DAY_FIRST_MESSAGE)
        self._assert_nothing_changed()

    def test_the_visits_own_time_is_not_a_new_time(self):
        response = self._send(date=BOOK_DAY.isoformat(), time="12:00:00")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["time_error"], PICK_NEW_TIME_MESSAGE)
        self._assert_nothing_changed()

    def test_a_time_taken_at_save_shows_the_red_box(self):
        with mock.patch("appointments.utils.is_slot_available", return_value=False):
            response = self._send()
        self.assertEqual(response.status_code, 200)
        html = unescape(response.content.decode())
        self.assertIn("9:00 AM was just taken", html)
        self.assertIn(
            "Someone booked it a moment ago. Please pick another time. "
            "Your note is kept.",
            html,
        )
        self.assertEqual(response.context["picked_date"], CHANGE_DAY)
        self.assertEqual(response.context["new_time"], "")
        self.assertNotIn(" checked", html)
        self.assertIn(">I have work that day</textarea>", html)
        self._assert_nothing_changed()

    def test_a_time_taken_before_the_post_shows_the_red_box_too(self):
        self._hold(CHANGE_DAY, [9])
        response = self._send()
        self.assertEqual(response.context["taken_time"], "9:00 AM")
        self.assertContains(response, "9:00 AM was just taken")
        self.visit.refresh_from_db()
        self.assertEqual(self.visit.status, Appointment.CONFIRMED)
        self.assertFalse(self._admin_notices().exists())

    def test_a_valid_send_cancels_the_original_and_makes_a_pending_visit(self):
        self.visit.reason = "Itchy skin"
        self.visit.save()
        response = self._send()
        new = Appointment.objects.exclude(pk=self.visit.pk).get()
        self.assertRedirects(
            response,
            reverse("owner_appointment_detail", args=[new.pk]),
            fetch_redirect_response=False,
        )
        self.assertEqual(self._messages(response), [RESCHEDULE_SENT_MESSAGE])
        self.visit.refresh_from_db()
        self.assertEqual(self.visit.status, Appointment.CANCELLED)
        self.assertEqual(self.visit.cancellation_reason, Appointment.OTHER)
        self.assertEqual(self.visit.cancellation_detail, "Rescheduled by pet owner.")
        self.assertEqual(self.visit.cancelled_by, self.owner.user)
        self.assertEqual(new.status, Appointment.PENDING)
        self.assertEqual(new.rescheduled_from, self.visit)
        self.assertEqual(new.owner, self.owner)
        self.assertEqual(new.pet, self.pet)
        self.assertEqual(new.service, self.service)
        self.assertEqual((new.date, new.time), (CHANGE_DAY, datetime.time(9, 0)))
        self.assertEqual(new.reason, "Itchy skin")
        self.assertEqual(new.reschedule_reason, "I have work that day")

    def test_the_admin_is_told_as_before(self):
        self._send()
        new = Appointment.objects.exclude(pk=self.visit.pk).get()
        notice = self._admin_notices().get()
        self.assertEqual(notice.related_appointment, new)
        self.assertIn("has requested to reschedule their appointment", notice.message)

    def test_the_send_goes_through_the_lock_once(self):
        with mock.patch("appointments.utils.lock_schedule") as lock:
            self._send()
        lock.assert_called_once_with()
        self.assertEqual(Appointment.objects.count(), 2)

    def test_the_new_visit_page_shows_the_flash_and_the_old_time(self):
        self.client.get(reverse("owner_appointments"))
        response = self.client.post(
            self.url,
            {
                "date": CHANGE_DAY.isoformat(),
                "time": "09:00:00",
                "reschedule_reason": "x",
            },
            follow=True,
        )
        html = unescape(response.content.decode())
        # The flash's bold first sentence, then the rest
        self.assertIn("New time sent.", html)
        self.assertIn("The clinic will confirm it soon.", html)
        self.assertIn("You changed the time. Was Tue, Nov 10 at 12:00 PM.", html)

    def test_a_second_post_changes_nothing_and_goes_to_the_original(self):
        self._send()
        response = self._send()
        self.assertRedirects(response, self.detail, fetch_redirect_response=False)
        self.assertEqual(self._messages(response), [CANNOT_CHANGE_MESSAGE])
        self.assertEqual(Appointment.objects.count(), 2)
        self.assertEqual(self._admin_notices().count(), 1)

    def test_an_original_cancelled_meanwhile_changes_nothing(self):
        def cancelled_by_someone_else():
            Appointment.objects.filter(pk=self.visit.pk).update(
                status=Appointment.CANCELLED
            )

        with mock.patch(
            "appointments.utils.lock_schedule", side_effect=cancelled_by_someone_else
        ):
            response = self._send()
        self.assertRedirects(response, self.detail, fetch_redirect_response=False)
        self.assertEqual(self._messages(response), [CANNOT_CHANGE_MESSAGE])
        self.assertEqual(Appointment.objects.count(), 1)
        self.assertFalse(self._admin_notices().exists())

    def test_not_own_is_a_404_and_unchanged(self):
        other, theirs = self._other_owner()
        visit = Appointment.objects.create(
            owner=other,
            pet=theirs,
            date=BOOK_DAY,
            time=datetime.time(10, 0),
            status=Appointment.CONFIRMED,
        )
        self.assertEqual(self._send(visit).status_code, 404)
        visit.refresh_from_db()
        self.assertEqual(visit.status, Appointment.CONFIRMED)
        self.assertEqual(Appointment.objects.count(), 2)

    def test_the_session_has_no_booking_draft_to_clash_with(self):
        self._send()
        self.assertNotIn("owner_booking", self.client.session)


class ClosedDaysLineTests(SimpleTestCase):
    """Slice 8b-9b: "Closed Saturday, Sunday and holidays" from the schedule."""

    def _line(self, days, holidays=False):
        row = ClinicSettings(working_days=days)
        return closed_days_line(row, holidays)

    def test_the_days_missing_from_the_schedule(self):
        self.assertEqual(
            self._line([0, 1, 2, 3, 4], True), "Closed Saturday, Sunday and holidays"
        )
        self.assertEqual(self._line([0, 1, 2, 3, 4]), "Closed Saturday and Sunday")
        self.assertEqual(
            self._line([0, 1, 2, 3, 4, 5], True), "Closed Sunday and holidays"
        )
        self.assertEqual(self._line([0, 1, 2, 3, 4, 5]), "Closed Sunday")
        self.assertEqual(
            self._line([1, 3], False),
            "Closed Monday, Wednesday, Friday, Saturday and Sunday",
        )

    def test_open_every_day_only_says_holidays_when_there_are_some(self):
        every = [0, 1, 2, 3, 4, 5, 6]
        self.assertEqual(self._line(every, True), "Closed on holidays")
        self.assertEqual(self._line(every, False), "")

    def test_never_open_has_no_closed_line(self):
        self.assertEqual(self._line([], True), "")

    def test_vet_initials_skip_the_title(self):
        self.assertEqual(vet_initials("Dr. Edgar Sadiwa"), "ES")
        self.assertEqual(vet_initials("Edgar Antonio Sadiwa"), "EA")
        self.assertEqual(vet_initials("Aga"), "A")
        self.assertEqual(vet_initials(""), "")


class OwnerClinicInfoPageTests(AppointmentTestBase):
    """Slice 8b-9b: the owner's Clinic info on layouts/owner.html (frame 5)."""

    # session, user, settings, blocked dates, any service Offered (was 4)
    PAGE_QUERIES = 5

    def setUp(self):
        super().setUp()
        self.url = reverse("clinic_info")
        self.row = get_clinic_settings()
        self.row.contact_number = "0917 000 0000"
        self.row.email = "clinic@example.com"
        self.row.working_days = [0, 1, 2, 3, 4]
        self.row.veterinarian_name = "Dr. Edgar Sadiwa"
        self.row.veterinarian_license_number = "PRC-VET-0000"
        self.row.veterinarian_bio = "Cares for pets of Gasan.\n\nAsk about vaccines."
        self.row.save()
        self.client.force_login(self.owner.user)

    def _html(self):
        return self.client.get(self.url).content.decode()

    def _main(self):
        return self._html().split('<main id="main"')[1].split("</main>")[0]

    def _clear(self, **fields):
        for name, value in fields.items():
            setattr(self.row, name, value)
        self.row.save()

    def test_owner_layout_account_tab_and_back_bar(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "layouts/owner.html")
        html = response.content.decode()
        back = reverse("owner_account")
        self.assertRegex(html, rf'(?s)<a href="{back}"[^>]*>.*?Back to Account</a>')
        # The Account tab is lit (the back bar's link has no aria-current)
        # Slice 8b-10b-1: the tab link has data attributes after aria-current
        lit = rf'<a href="{back}" class="[^"]*" aria-current="page"[^>]*>'
        self.assertRegex(html, lit)
        self.assertContains(response, '<h1 class="min-w-0 break-words text-2xl')

    def test_call_is_the_one_violet_button_with_a_tel_link(self):
        main = self._main()
        violet = re.findall(r"<a [^>]*bg-violet-600[^>]*>", main)
        self.assertEqual(len(violet), 1)
        self.assertIn('href="tel:09170000000"', violet[0])
        self.assertRegex(main, r"(?s)<a href=\"tel:09170000000\".*?Call</a>")

    def test_email_is_an_outline_mailto_link(self):
        main = self._main()
        link = re.search(r'<a href="mailto:clinic@example.com"[^>]*>', main).group(0)
        self.assertIn("border border-gray-300", link)
        self.assertNotIn("bg-violet", link)
        self.assertIn("Email</a>", main)

    def test_a_missing_phone_or_email_leaves_its_button_out(self):
        self._clear(contact_number="")
        main = self._main()
        self.assertNotIn("tel:", main)
        self.assertNotIn("Call</a>", main)
        self.assertIn('href="mailto:clinic@example.com"', main)
        self.assertIn("grid-cols-1", main)
        self.assertEqual(len(re.findall(r"<a [^>]*bg-violet-600", main)), 0)
        self._clear(contact_number="0917 000 0000", email="")
        main = self._main()
        self.assertIn('href="tel:09170000000"', main)
        self.assertNotIn("mailto:", main)
        self.assertIn("grid-cols-1", main)

    def test_neither_phone_nor_email_has_no_button_row(self):
        self._clear(contact_number="", email="")
        main = self._main()
        self.assertNotIn("tel:", main)
        self.assertNotIn("mailto:", main)
        self.assertNotIn("grid-cols-", main)

    def test_the_rows_show_hours_closed_days_phone_email_and_address(self):
        main = self._main()
        for label, value in (
            ("Open", "Mon to Fri, 8:00 AM to 5:00 PM"),
            ("Phone", "0917 000 0000"),
            ("Email", "clinic@example.com"),
            ("Address", "Gasan, Marinduque"),
        ):
            with self.subTest(row=label):
                self.assertRegex(
                    main, rf'(?s)text-gray-600">{label}</p><p class="[^"]*">{value}</p>'
                )
        self.assertIn("Closed Saturday and Sunday</p>", main)

    def test_closed_line_adds_holidays_only_for_a_blocked_date_from_today(self):
        past = self.today - datetime.timedelta(days=2)
        BlockedDate.objects.create(date=past, reason="Secret reason")
        self.assertIn("Closed Saturday and Sunday</p>", self._main())
        BlockedDate.objects.create(date=self.today, reason="Staff day")
        main = self._main()
        self.assertIn("Closed Saturday, Sunday and holidays</p>", main)
        self.assertNotIn("Staff day", main)
        self.assertNotIn("Secret reason", main)

    def test_a_missing_value_leaves_its_row_out(self):
        self._clear(contact_number="", email="", address="")
        main = self._main()
        for label in ("Phone", "Email", "Address"):
            with self.subTest(row=label):
                self.assertNotIn(f'text-gray-600">{label}</p>', main)
        self.assertIn('text-gray-600">Open</p>', main)

    def test_your_vet_card_with_licence_and_bio(self):
        main = self._main()
        self.assertIn(">Your vet</h2>", main)
        self.assertIn('aria-hidden="true">ES</span>', main)
        self.assertIn('text-lg font-bold text-gray-900">Dr. Edgar Sadiwa</p>', main)
        self.assertIn("License no. PRC-VET-0000", main)
        self.assertIn("<p>Cares for pets of Gasan.</p>", main)
        self.assertIn("<p>Ask about vaccines.</p>", main)

    def test_your_vet_card_needs_a_vet_name(self):
        self._clear(veterinarian_name="")
        main = self._main()
        self.assertNotIn("Your vet", main)
        self.assertNotIn("PRC-VET-0000", main)

    def test_licence_and_bio_show_only_when_filled_in_and_there_is_no_nickname(self):
        self._clear(veterinarian_license_number="", veterinarian_bio="")
        main = self._main()
        self.assertIn("Dr. Edgar Sadiwa", main)
        self.assertNotIn("License no.", main)
        self.assertNotIn("Cares for pets", main)
        # ClinicSettings has no nickname field, so the card never shows one
        field_names = [f.name for f in ClinicSettings._meta.get_fields()]
        self.assertFalse([n for n in field_names if "nick" in n])

    def test_logo_or_paw_tile(self):
        main = self._main()
        self.assertIn("bg-violet-600 text-white", main.split("<h1")[0])
        self.assertNotIn("<img", main)
        self._clear(logo="clinic/logo.png")
        main = self._main()
        self.assertIn(f'<img src="{self.row.logo.url}"', main)
        self.assertNotIn("bg-violet-600 text-white", main.split("<h1")[0])

    def test_no_clinic_js_and_no_map_for_owners(self):
        html = self._html()
        self.assertNotIn("js/shared/clinic_info", html)
        self.assertNotIn("css/shared/clinic_info", html)
        self.assertNotIn("<iframe", html)
        self.assertNotIn("<script>", html)

    def test_an_admin_still_gets_the_admin_page(self):
        self.client.force_login(self.admin)
        response = self.client.get(self.url)
        self.assertTemplateUsed(response, "admin/clinic_info.html")
        self.assertTemplateUsed(response, "layouts/admin.html")
        self.assertTemplateNotUsed(response, "owner/clinic/info.html")
        self.assertContains(response, "Open in Google Maps")

    def test_no_settings_row_shows_a_plain_message(self):
        ClinicSettings.objects.all().delete()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "The clinic details are not available yet.")

    def test_every_link_is_on_one_line(self):
        from django.template.loader import get_template

        names = (
            "owner/clinic/info.html",
            "owner/clinic_parts/header.html",
            "owner/clinic_parts/buttons.html",
            "owner/clinic_parts/details.html",
            "owner/clinic_parts/vet.html",
        )
        for name in names:
            source = get_template(name).template.source
            for link in re.findall(r"(?s)<a\b.*?</a>", source):
                with self.subTest(file=name, link=link[:50]):
                    self.assertNotIn("\n", link)

    def test_no_inline_script_or_handler_or_html_comment(self):
        main = self._main()
        self.assertNotIn("<script", main)
        self.assertNotIn("onclick", main)
        self.assertNotIn("<!--", main)

    def test_query_total_is_flat_for_3_and_30_blocked_dates(self):
        for count in (3, 30):
            BlockedDate.objects.all().delete()
            for n in range(count):
                day = self.today + datetime.timedelta(days=n + 1)
                BlockedDate.objects.create(date=day, reason=f"Reason {n}")
            with self.subTest(blocked=count):
                with self.assertNumQueries(self.PAGE_QUERIES):
                    self.client.get(self.url)


class RemovedOwnerUrlsTests(AppointmentTestBase):
    """Slice 8b-10a: the owner URLs removed in the cleanup are gone."""

    REMOVED = (
        ("owner_get_slots", [], "/owner/appointments/slots/"),
        ("owner_dashboard_refresh", [], "/owner/dashboard/refresh/"),
        ("notification_mark_read", [1], "/notifications/1/read/"),
    )

    def test_the_names_no_longer_reverse(self):
        for name, args, _path in self.REMOVED:
            with self.subTest(name=name):
                with self.assertRaises(NoReverseMatch):
                    reverse(name, args=args)

    def test_the_old_paths_are_404_for_a_logged_in_owner(self):
        self.client.force_login(self.owner.user)
        for name, _args, path in self.REMOVED:
            with self.subTest(name=name):
                self.assertEqual(self.client.get(path).status_code, 404)


# ── Slice 8b-10b-3: the free-times area (HTMX swap) ──────────────────────────

DAY_AREA = "owner/booking_parts/_day_area.html"
OWN_DAY = datetime.date(2026, 11, 11)  # the owner's visit: Wed, Nov 11 at 2:00 PM
# What a full page has and the swap's answer must not have
NOT_IN_THE_AREA = ("<html", "<main", "<header", 'data-bar="one"', 'id="loading-root"')


class _DayAreaBase(_OwnerBookingBase):
    """Step 2 (the session draft) and the change-time page (the owner's
    Confirmed visit on Wed, Nov 11 at 2:00 PM), each asked with and without
    HX-Request. The draft's day is Tue, Nov 10; the change page moves to Thu,
    Nov 12."""

    def setUp(self):
        super().setUp()
        self.visit = Appointment.objects.create(
            owner=self.owner,
            pet=self.pet,
            service=self.service,
            date=OWN_DAY,
            time=datetime.time(14, 0),
            status=Appointment.CONFIRMED,
        )
        self.change = reverse("owner_appointment_reschedule", args=[self.visit.pk])
        self.detail = reverse("owner_appointment_detail", args=[self.visit.pk])

    def _get(self, url, htmx=True, **params):
        headers = {"HX-Request": "true"} if htmx else {}
        return self.client.get(url, params, headers=headers)


class OwnerDayAreaPartialTests(_DayAreaBase):
    """One URL, two answers: HX-Request gets only owner/booking_parts/_day_area.html,
    everything else the full page as before."""

    def _count(self, url, htmx, **params):
        with CaptureQueriesContext(connection) as queries:
            response = self._get(url, htmx=htmx, **params)
        self.assertEqual(response.status_code, 200)
        return len(queries)

    def _totals(self):
        """(partial, page) query totals of step 2 and of the change page."""
        self._set_draft()
        day = {"date": CHANGE_DAY.isoformat()}
        return {
            "step 2": (
                self._count(self.step2, True),
                self._count(self.step2, False),
            ),
            "change page": (
                self._count(self.change, True, **day),
                self._count(self.change, False, **day),
            ),
        }

    def test_step_2_answers_the_swap_with_only_the_area(self):
        self._set_draft()
        response = self._get(self.step2)
        html = response.content.decode()
        self.assertTemplateUsed(response, DAY_AREA)
        self.assertTemplateNotUsed(response, "layouts/owner.html")
        for gone in NOT_IN_THE_AREA:
            with self.subTest(gone=gone):
                self.assertNotIn(gone, html)
        self.assertNotIn('id="day-area"', html)  # the page keeps the wrapper
        self.assertEqual(re.findall(VIOLET_BUTTON, html), [])  # no bottom bar
        self.assertIn('id="calendar-month"', html)
        self.assertIn("November 2026", html)
        self.assertIn("Times on Tue, Nov 10", html)
        self.assertIn('<form id="time-form"', html)
        self.assertIn('name="date" value="2026-11-10"', html)

    def test_step_2_without_the_header_is_the_full_page_as_before(self):
        self._set_draft()
        restore = {"HX-Request": "true", "HX-History-Restore-Request": "true"}
        for headers in ({}, restore):
            with self.subTest(headers=headers):
                response = self.client.get(self.step2, headers=headers)
                html = response.content.decode()
                self.assertTemplateUsed(response, "layouts/owner.html")
                for marker in NOT_IN_THE_AREA:
                    self.assertIn(marker, html)
                self.assertIn('id="day-area"', html)
                self.assertIn("Times on Tue, Nov 10", html)
                self.assertIn('<form id="time-form"', html)
                self.assertEqual(len(re.findall(VIOLET_BUTTON, html)), 1)

    def test_both_answers_vary_on_hx_request(self):
        self._set_draft()
        day = {"date": CHANGE_DAY.isoformat()}
        for url, params in ((self.step2, {}), (self.change, day)):
            for htmx in (True, False):
                with self.subTest(url=url, htmx=htmx):
                    response = self._get(url, htmx=htmx, **params)
                    self.assertIn("HX-Request", response.headers["Vary"])

    def test_a_picked_day_by_swap_is_saved_like_a_plain_get(self):
        other = datetime.date(2026, 11, 11)
        self._set_draft()
        response = self._get(self.step2, date=other.isoformat())
        self.assertContains(response, "Times on Wed, Nov 11")
        swapped = self._draft()
        self._set_draft()
        self._get(self.step2, htmx=False, date=other.isoformat())
        self.assertEqual(self._draft(), swapped)
        self.assertEqual(swapped["date"], other.isoformat())
        self.assertNotIn("time", swapped)

    def test_a_month_by_swap_keeps_the_picked_day_and_the_draft(self):
        draft = self._set_draft()
        response = self._get(self.step2, month="2026-12")
        self.assertContains(response, "December 2026")
        self.assertContains(response, "Times on Tue, Nov 10")
        self.assertEqual(self._draft(), draft)

    def test_a_closed_or_full_day_gives_the_pages_message_by_swap(self):
        self._hold(datetime.date(2026, 11, 12), range(8, 17))
        BlockedDate.objects.create(date=datetime.date(2026, 11, 13))
        draft = self._set_draft()
        message = "That day can&#x27;t be booked. Please pick a day marked open."
        for value in ("2026-11-01", "2026-11-12", "2026-11-13", "2026-12-03", "junk"):
            with self.subTest(value=value):
                swap = self._get(self.step2, date=value)
                page = self._get(self.step2, htmx=False, date=value)
                self.assertContains(swap, message)
                self.assertContains(page, message)
                self.assertContains(swap, "November 2026")
                self.assertEqual(self._draft(), draft)

    def test_the_change_page_answers_the_swap_with_only_the_area(self):
        response = self._get(self.change, date=CHANGE_DAY.isoformat())
        html = response.content.decode()
        self.assertTemplateUsed(response, DAY_AREA)
        self.assertTemplateNotUsed(response, "layouts/owner.html")
        for gone in NOT_IN_THE_AREA:
            with self.subTest(gone=gone):
                self.assertNotIn(gone, html)
        self.assertEqual(re.findall(VIOLET_BUTTON, html), [])
        self.assertIn('id="calendar-month"', html)
        self.assertIn("Times on Thu, Nov 12", html)
        self.assertIn('<form id="reschedule-form"', html)
        # Now / New follow the picked day; the reason box stays outside the area
        self.assertIn("Pick a new day", html)
        self.assertIn("Thu, Nov 12<br>", html)
        self.assertIn("Pick a time below", html)
        self.assertNotIn('id="reschedule-reason"', html)

    def test_the_change_page_without_the_header_is_the_full_page_as_before(self):
        restore = {"HX-Request": "true", "HX-History-Restore-Request": "true"}
        for headers in ({}, restore):
            with self.subTest(headers=headers):
                response = self.client.get(
                    self.change, {"date": CHANGE_DAY.isoformat()}, headers=headers
                )
                html = response.content.decode()
                self.assertTemplateUsed(response, "layouts/owner.html")
                for marker in NOT_IN_THE_AREA:
                    self.assertIn(marker, html)
                self.assertIn('id="day-area"', html)
                self.assertIn('id="reschedule-reason"', html)
                self.assertIn('form="reschedule-form"', html)
                self.assertIn("Times on Thu, Nov 12", html)

    def test_the_change_swap_still_marks_the_visits_own_slot_your_time(self):
        response = self._get(self.change, date=OWN_DAY.isoformat())
        self.assertContains(response, ">Your time</span>")
        self.assertNotContains(response, 'name="time" value="14:00:00"')
        self.assertContains(response, 'name="time" value="13:00:00"')
        other = self._get(self.change, date=CHANGE_DAY.isoformat())
        self.assertNotContains(other, "Your time")

    def test_another_owners_visit_by_swap_is_a_404(self):
        pet = self._other_owners_pet()
        visit = Appointment.objects.create(
            owner=pet.owner,
            pet=pet,
            date=BOOK_DAY,
            time=datetime.time(9, 0),
            status=Appointment.CONFIRMED,
        )
        url = reverse("owner_appointment_reschedule", args=[visit.pk])
        response = self._get(url, date=BOOK_DAY.isoformat())
        self.assertEqual(response.status_code, 404)

    def test_a_redirect_is_never_swapped_into_the_area(self):
        # A draft that is gone, and a visit that can't be changed any more: the
        # swap gets HX-Redirect (the browser opens the page), a plain GET the
        # same redirect as before
        Appointment.objects.filter(pk=self.visit.pk).update(
            status=Appointment.CANCELLED
        )
        cases = ((self.step2, self.step1_back), (self.change, self.detail))
        for url, target in cases:
            with self.subTest(url=url):
                swap = self._get(url)
                self.assertEqual(swap.status_code, 200)
                self.assertEqual(swap.headers["HX-Redirect"], target)
                page = self._get(url, htmx=False)
                self.assertRedirects(page, target, fetch_redirect_response=False)
                self.assertNotIn("HX-Redirect", page.headers)

    def test_query_totals_are_flat_and_the_partial_costs_no_more_than_the_page(self):
        self._hold(datetime.date(2026, 11, 16), [8, 9, 10])
        few = self._totals()
        for day in (17, 18, 19):
            self._hold(datetime.date(2026, 11, day), range(8, 17))
        self.assertEqual(Appointment.objects.exclude(pk=self.visit.pk).count(), 30)
        many = self._totals()
        self.assertEqual(few, many)
        for name, (partial, page) in many.items():
            with self.subTest(page=name):
                self.assertLessEqual(partial, page)


class OwnerDayAreaMarkupTests(_DayAreaBase):
    """The hooks of the free-times area on step 2 and the change-time page."""

    HX_PARTS = (
        'hx-target="#day-area"',
        'hx-swap="innerHTML"',
        'hx-push-url="true"',
        'hx-indicator="#day-area"',
        "hx-request='{\"timeout\": 10000}'",
        'data-loading="off"',
        'data-day-label="',
    )

    def _page(self, url, **params):
        """The page as text (entities decoded) with a full draft."""
        self._set_draft()
        return unescape(self.client.get(url, params).content.decode())

    def _pages(self):
        return {
            "step 2": self._page(self.step2),
            "change page": self._page(self.change, date=CHANGE_DAY.isoformat()),
        }

    def _tag(self, html, label):
        return re.search(rf'<(?:a|span)\b[^>]*aria-label="{label}[^"]*"[^>]*>', html)

    def test_day_links_and_month_arrows_keep_the_href_and_swap_the_area(self):
        for name, html in self._pages().items():
            tags = re.findall(r"<a\b[^>]*\bhx-get=[^>]*>", html)
            with self.subTest(page=name):
                self.assertGreater(len(tags), 10)
            for tag in tags:
                href = re.search(r'\bhref="([^"]*)"', tag).group(1)
                with self.subTest(page=name, href=href):
                    self.assertIn(f'hx-get="{href}"', tag)
                    for part in self.HX_PARTS:
                        self.assertIn(part, tag)

    def test_the_labels_name_the_day_and_the_month(self):
        html = self._page(self.step2)
        day = self._tag(html, "Wednesday, November 4: open").group(0)
        self.assertIn('data-day-label="Wed, Nov 4"', day)
        self.assertIn(f'hx-get="{self.step2}?date=2026-11-04"', day)
        arrow = self._tag(html, "Next month").group(0)
        self.assertIn(f'hx-get="{self.step2}?month=2026-12"', arrow)
        self.assertIn('data-day-label="December 2026"', arrow)
        december = self._page(self.step2, month="2026-12")
        back = self._tag(december, "Previous month").group(0)
        self.assertIn('data-day-label="November 2026"', back)

    def test_the_change_page_links_keep_the_month_and_the_day(self):
        html = self._page(self.change, date=CHANGE_DAY.isoformat())
        link = self._tag(html, "Friday, November 13: open").group(0)
        self.assertIn(f'hx-get="{self.change}?date=2026-11-13&month=2026-11"', link)
        arrow = self._tag(html, "Next month").group(0)
        self.assertIn(f'hx-get="{self.change}?month=2026-12&date=2026-11-12"', arrow)

    def test_closed_full_and_disabled_cells_have_no_hx_attributes(self):
        ClinicSettings.objects.update(working_days=[0, 1, 2, 3, 4, 5])
        self._hold(datetime.date(2026, 11, 12), range(8, 17))
        html = self._page(self.step2, month="2026-11")
        self.assertEqual(re.findall(r"<span\b[^>]*\bhx-", html), [])
        self.assertEqual(re.findall(r"<span\b[^>]*data-day-label", html), [])
        for label in ("Thursday, November 12: full", "Sunday, November 8: "):
            with self.subTest(label=label):
                tag = self._tag(html, label).group(0)
                self.assertTrue(tag.startswith("<span"))
                self.assertNotIn("hx-", tag)

    def test_the_area_is_a_polite_live_region_that_is_never_saved(self):
        for name, html in self._pages().items():
            with self.subTest(page=name):
                tag = re.search(r'<div id="day-area"[^>]*>', html).group(0)
                self.assertIn('aria-live="polite"', tag)
                self.assertIn('aria-busy="false"', tag)
                self.assertIn('hx-history="false"', tag)
                self.assertIn("group/swap", tag)
                self.assertIn("data-swap-area", tag)

    def test_the_loading_block_has_a_status_line_and_nine_grey_boxes(self):
        for name, html in self._pages().items():
            with self.subTest(page=name):
                line = re.search(r'(?s)<p role="status".*?</p>', html).group(0)
                self.assertIn("Finding free times for ", line)
                self.assertIn("<span data-day-name>this day</span>…", line)
                self.assertIn("<svg", line)
                self.assertIn("motion-safe:animate-spin", line)
                self.assertIn("group-[.htmx-request]/swap:flex", line)
                box = '<span class="sk h-12 rounded-xl bg-gray-200">'
                self.assertEqual(html.count(box), 9)
                grid = re.search(
                    r'<div aria-hidden="true" class="hidden grid-cols-3[^"]*"', html
                ).group(0)
                self.assertIn("group-[.htmx-request]/swap:grid", grid)
                self.assertIn("group-data-[swap-failed]/swap:opacity-60", grid)

    def test_the_old_times_are_hidden_while_loading_or_failed(self):
        for name, html in self._pages().items():
            with self.subTest(page=name):
                form = re.search(r'<form id="(?:time|reschedule)-form"[^>]*>', html)
                form = form.group(0)
                self.assertIn("group-[.htmx-request]/swap:hidden", form)
                self.assertIn("group-data-[swap-failed]/swap:hidden", form)

    def test_the_two_alert_boxes_are_hidden_with_an_outline_try_again(self):
        boxes = (
            (
                "slow",
                "This is taking longer than usual",
                "Your internet may be slow. Your answers are kept.",
            ),
            (
                "failed",
                "Couldn't load the free times",
                "Check your internet and try again. Your answers are kept.",
            ),
        )
        for name, html in self._pages().items():
            for kind, title, words in boxes:
                with self.subTest(page=name, box=kind):
                    pattern = (
                        rf'(?s)<div role="alert" hidden data-swap-error="{kind}"'
                        r".*?</button>"
                    )
                    box = re.search(pattern, html).group(0)
                    self.assertIn(title, box)
                    self.assertIn(words, box)
                    self.assertIn("<svg", box)
                    button = re.search(r"<button\b[^>]*data-swap-retry[^>]*>", box)
                    button = button.group(0)
                    for outline in ("min-h-11", "border-orange-400", "bg-white"):
                        self.assertIn(outline, button)
                    self.assertNotIn("bg-violet-600", button)
                    self.assertIn("Try again", box)

    def test_one_violet_button_and_no_inline_script_or_handler(self):
        for name, html in self._pages().items():
            with self.subTest(page=name):
                self.assertEqual(len(re.findall(VIOLET_BUTTON, html)), 1)
                for tag in re.findall(r"<script\b[^>]*>", html):
                    self.assertIn(" src=", tag)
                self.assertIsNone(re.search(r"\son[a-z]+=", html))
                self.assertIsNone(re.search(r"<a\b[^>]*\n", html))
                self.assertNotIn("hx-confirm", html)
                self.assertNotIn("<!--", html)

    def test_the_owner_layout_loads_the_script_with_defer_and_keeps_history_safe(self):
        for name, html in self._pages().items():
            with self.subTest(page=name):
                script = r'<script src="/static/js/owner/day_area\.[0-9a-f]+\.js" '
                self.assertRegex(html, script + "defer>")
                # Back with no saved copy reloads the page (never a swapped body)
                self.assertIn('"refreshOnHistoryMiss": true', html)

    def test_without_javascript_the_times_are_radio_cards_in_the_post_form(self):
        html = self._page(self.step2)
        form = re.search(r'(?s)<form id="time-form".*?</form>', html).group(0)
        self.assertIn('method="post"', form)
        self.assertIn("csrfmiddlewaretoken", form)
        self.assertIn('name="date" value="2026-11-10"', form)
        self.assertEqual(len(re.findall(r'<input type="radio" name="time"', form)), 9)
        change = self._page(self.change, date=CHANGE_DAY.isoformat())
        form = re.search(r'(?s)<form id="reschedule-form".*?</form>', change).group(0)
        self.assertEqual(len(re.findall(r'<input type="radio" name="time"', form)), 9)
        # The reason box sits outside the swapped area and joins the form
        self.assertIn('rows="3" form="reschedule-form"', change)


class OwnerBookingKeyTests(_DayAreaBase):
    """Slice 8b-10b-5: no bottom key; open days and times are light violet, the
    picked ones solid violet; one hint above the calendar, outside #day-area."""

    HINT = "Tap an open box."
    DASHED_LINE = "Your current day has a dashed outline."
    TINT = ("bg-violet-50", "ring-1 ring-inset ring-violet-300", "text-violet-900")
    SOLID = r"(?<![:-])bg-violet-600"

    def _pages(self):
        """Step 2 and the change page as text, with a day picked."""
        self._set_draft()
        change = self.client.get(self.change, {"date": CHANGE_DAY.isoformat()})
        return {
            "step 2": unescape(self.client.get(self.step2).content.decode()),
            "change page": unescape(change.content.decode()),
        }

    def _tag(self, html, label):
        return re.search(rf'<(?:a|span)\b[^>]*aria-label="{label}[^"]*"[^>]*>', html)

    def test_the_hint_is_once_before_the_calendar_and_outside_the_area(self):
        for name, html in self._pages().items():
            with self.subTest(page=name):
                self.assertEqual(html.count(self.HINT), 1)
                self.assertLess(html.index(self.HINT), html.index('id="day-area"'))
                calendar = html.index('id="calendar-month"')
                self.assertLess(html.index(self.HINT), calendar)

    def test_the_hint_has_no_colour_word(self):
        for name, html in self._pages().items():
            with self.subTest(page=name):
                hint = re.search(rf"<p [^>]*>({self.HINT})</p>", html).group(1)
                for word in ("violet", "purple", "blue", "green", "red", "gray"):
                    self.assertNotIn(word, hint.lower())

    def test_the_old_bottom_key_is_gone(self):
        for name, html in self._pages().items():
            with self.subTest(page=name):
                self.assertNotIn("border-t border-gray-100 pt-3 text-base", html)
                for word in (">Open<", ">Full<", ">Closed<", "Your current day</span>"):
                    self.assertNotIn(word, html)

    def test_open_days_are_tinted_and_lose_the_white_ring(self):
        for name, html in self._pages().items():
            day = self._tag(html, "Wednesday, November 4: open").group(0)
            with self.subTest(page=name):
                for part in self.TINT:
                    self.assertIn(part, day)
                for old in ("bg-white", "ring-gray-200", "text-gray-900"):
                    self.assertNotIn(old, day)
                self.assertNotRegex(day, self.SOLID)

    def test_full_closed_and_today_closed_days_are_unchanged(self):
        ClinicSettings.objects.update(working_days=[0, 1, 2, 3, 4, 5])
        self._hold(datetime.date(2026, 11, 12), range(8, 17))
        self.freeze(_manila(15, 30))
        self._set_draft(date=None, time=None)
        html = self.client.get(self.step2).content.decode()
        full = self._tag(html, "Thursday, November 12: full").group(0)
        self.assertIn("bg-gray-100 text-lg text-gray-500 line-through", full)
        closed = self._tag(html, "Sunday, November 8: ").group(0)
        self.assertIn("text-lg text-gray-400", closed)
        self.assertNotIn("bg-violet-50", closed)
        today = self._tag(html, "Monday, November 2: ").group(0)
        self.assertIn("text-gray-400 ring-2 ring-gray-300", today)
        self.assertNotIn("bg-violet-50", today)

    def test_the_picked_day_is_the_only_solid_violet_day(self):
        for name, html in self._pages().items():
            with self.subTest(page=name):
                picked = self._tag(html, r"[^\"]*, picked").group(0)
                self.assertRegex(picked, self.SOLID)
                self.assertEqual(html.count('aria-current="date"'), 1)
                for tag in re.findall(r'<a\b[^>]*aria-label="[^"]*open"[^>]*>', html):
                    self.assertNotRegex(tag, self.SOLID)

    def test_open_times_are_tinted_and_the_picked_time_turns_solid_violet(self):
        radio = (
            r'<input type="radio" name="time" value="{}" class="peer sr-only"'
            r'( checked)?>\s*<span class="([^"]*)">'
        )
        html = self._pages()["step 2"]
        for value, is_checked in (("09:00:00", False), ("12:00:00", True)):
            with self.subTest(value=value):
                found = re.search(radio.format(value), html)
                self.assertEqual(bool(found.group(1)), is_checked)
                classes = found.group(2)
                for part in self.TINT:
                    self.assertIn(part, classes)
                solid = "peer-checked:bg-violet-600 peer-checked:text-white"
                self.assertIn(solid, classes)
                self.assertNotRegex(classes, self.SOLID)

    def test_a_taken_time_keeps_the_word_taken_and_the_line_through(self):
        self._hold(BOOK_DAY, [10])
        html = self._pages()["step 2"]
        taken = (
            r'<span class="line-through">10:00 AM</span>'
            r'<span class="text-sm">Taken</span>'
        )
        self.assertIn(taken, html)
        self.assertRegex(html, r'<span class="[^"]*bg-gray-100[^"]*">' + taken)

    def test_the_dashed_line_is_on_the_change_page_only(self):
        pages = self._pages()
        change = pages["change page"]
        self.assertEqual(change.count(self.DASHED_LINE), 1)
        self.assertNotIn(self.DASHED_LINE, pages["step 2"])
        self.assertIn("outline-dashed", change)
        self.assertLess(change.index(self.HINT), change.index(self.DASHED_LINE))

    def test_the_swap_answer_has_no_hint_but_keeps_the_hooks(self):
        self._set_draft()
        day = {"date": CHANGE_DAY.isoformat()}
        for url, params in ((self.step2, {}), (self.change, day)):
            with self.subTest(url=url):
                html = self._get(url, **params).content.decode()
                self.assertNotIn(self.HINT, html)
                self.assertNotIn(self.DASHED_LINE, html)
                self.assertIn("Times on ", html)
                box = '<span class="sk h-12 rounded-xl bg-gray-200">'
                self.assertEqual(html.count(box), 9)
                self.assertIn('data-swap-error="failed"', html)


class AutoBillOnCompleteTests(AppointmentTestBase):
    """Fix auto-bill on complete: marking a visit Completed starts its bill
    (the only path that sets COMPLETED is admin_appointment_complete), by
    the same rules as New receipt from a visit."""

    STARTED = (
        "Marked Bantay's appointment on today at 9:00 AM as completed. "
        "A bill was started for it."
    )
    OLD_FLASH = "Marked Bantay's appointment on today at 9:00 AM as completed."
    DIALOG_LINE = "A bill will be started for this visit."

    def _visit(self, service=None, walk_in=False, days=0):
        visit = self._appointment(Appointment.CONFIRMED, days_from_today=days)
        visit.service = service
        visit.is_walk_in = walk_in
        visit.save()
        return visit

    def _service(self, name="Consultation", status=Service.ACTIVE, **fields):
        fields.setdefault("base_price", Decimal("350.00"))
        return Service.objects.create(service_name=name, status=status, **fields)

    def _complete(self, visit):
        # Unread flashes pile up in the cookie; each POST starts clean
        self.client.cookies.pop("messages", None)
        return self._post("admin_appointment_complete", visit)

    def _empty_bill(self, visit):
        bill = BillingReceipt.objects.get(appointment=visit)
        self.assertEqual(bill.payment_status, BillingReceipt.PENDING)
        self.assertEqual(bill.billing_date, self.today)
        self.assertEqual(bill.payment_method, "Cash")
        self.assertEqual(bill.owner, self.owner)
        self.assertEqual(bill.pet, self.pet)
        self.assertFalse(bill.billing_items.exists())
        return bill

    def test_a_fixed_price_offered_service_is_the_first_item(self):
        service = self._service()
        visit = self._visit(service)
        response = self._complete(visit)
        self._assert_status(visit, Appointment.COMPLETED)
        self.assertEqual(BillingReceipt.objects.count(), 1)
        bill = BillingReceipt.objects.get(appointment=visit)
        self.assertEqual(bill.payment_status, BillingReceipt.PENDING)
        self.assertEqual(bill.billing_date, self.today)
        self.assertEqual(bill.payment_method, "Cash")
        item = bill.billing_items.get()
        self.assertEqual(item.service, service)
        self.assertEqual(item.unit_price, Decimal("350.00"))
        self.assertEqual(bill.total_amount, Decimal("350.00"))
        self.assertEqual(self._messages(response), [self.STARTED])
        self.assertRedirects(
            response,
            reverse("admin_appointment_detail", args=[visit.pk]),
            fetch_redirect_response=False,
        )

    def test_clinic_use_only_fixed_price_service_is_added_too(self):
        visit = self._visit(self._service(status=Service.UNLISTED))
        self._complete(visit)
        self.assertEqual(BillingReceipt.objects.get().billing_items.count(), 1)

    def test_range_price_no_service_and_closed_or_draft_start_empty(self):
        range_service = self._service(
            "Surgery",
            pricing_type=Service.RANGE,
            min_price=Decimal("500.00"),
            max_price=Decimal("900.00"),
        )
        cases = {
            "range": range_service,
            "none": None,
            "draft": self._service("Old A", status=Service.DRAFT),
            "closed": self._service("Old B", status=Service.CLOSED),
        }
        for name, service in cases.items():
            with self.subTest(case=name):
                visit = self._visit(service)
                response = self._complete(visit)
                self._assert_status(visit, Appointment.COMPLETED)
                self._empty_bill(visit)
                self.assertEqual(self._messages(response), [self.STARTED])

    def test_a_walk_in_visit_gets_a_bill_the_same_way(self):
        visit = self._visit(self._service(), walk_in=True)
        self._complete(visit)
        bill = BillingReceipt.objects.get(appointment=visit)
        self.assertEqual(bill.total_amount, Decimal("350.00"))

    def test_a_visit_with_a_bill_in_any_status_gets_no_second_one(self):
        for number, status in enumerate(
            (BillingReceipt.PENDING, BillingReceipt.PAID, BillingReceipt.CANCELLED),
            start=1,
        ):
            with self.subTest(status=status):
                visit = self._visit(self._service(f"S{number}"))
                bill = BillingReceipt.objects.create(
                    appointment=visit,
                    owner=self.owner,
                    pet=self.pet,
                    receipt_number=f"HV-2026-010{number}",
                    payment_status=status,
                    total_amount=Decimal("10.00"),
                )
                response = self._complete(visit)
                self._assert_status(visit, Appointment.COMPLETED)
                self.assertEqual(
                    BillingReceipt.objects.filter(appointment=visit).count(), 1
                )
                before = bill.total_amount
                bill.refresh_from_db()
                self.assertEqual(bill.payment_status, status)
                self.assertEqual(bill.total_amount, before)
                self.assertFalse(bill.billing_items.exists())
                self.assertEqual(self._messages(response), [self.OLD_FLASH])

    def test_an_archived_owner_or_pet_completes_the_visit_with_no_bill(self):
        cases = (
            ("owner", PetOwner, self.owner, "Ana Cruz"),
            ("pet", Pet, self.pet, "Bantay"),
        )
        for label, model, obj, name in cases:
            with self.subTest(archived=label):
                model.objects.filter(pk=obj.pk).update(is_archived=True)
                visit = self._visit(self._service())
                response = self._complete(visit)
                self._assert_status(visit, Appointment.COMPLETED)
                self.assertFalse(BillingReceipt.objects.exists())
                self.assertEqual(
                    self._messages(response),
                    [
                        "Marked Bantay's appointment on today at 9:00 AM as "
                        "completed. "
                        f"No bill was started because {name} is archived."
                    ],
                )
                model.objects.filter(pk=obj.pk).update(is_archived=False)
                visit.delete()

    def test_an_archived_owner_is_named_before_an_archived_pet(self):
        PetOwner.objects.filter(pk=self.owner.pk).update(is_archived=True)
        Pet.objects.filter(pk=self.pet.pk).update(is_archived=True)
        response = self._complete(self._visit())
        self.assertIn("because Ana Cruz is archived.", self._messages(response)[0])

    def test_completing_twice_never_makes_a_second_bill_or_a_500(self):
        visit = self._visit(self._service())
        self._complete(visit)
        response = self._complete(visit)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(BillingReceipt.objects.count(), 1)

    def test_a_bill_made_meanwhile_is_kept_and_nothing_is_a_500(self):
        """Two requests at once: the second finds no bill at first, then the
        OneToOne refuses its save; the first bill stays and the visit is
        completed."""
        visit = self._visit(self._service())
        first = BillingReceipt.objects.create(
            appointment=visit,
            owner=self.owner,
            pet=self.pet,
            receipt_number="HV-2026-0200",
        )
        with mock.patch("billing.utils.visit_receipt", return_value=None):
            response = self._complete(visit)
        self.assertEqual(response.status_code, 302)
        self._assert_status(visit, Appointment.COMPLETED)
        self.assertEqual(list(BillingReceipt.objects.all()), [first])
        self.assertEqual(self._messages(response), [self.OLD_FLASH])

    def test_no_notification_is_made_for_the_owner(self):
        self._complete(self._visit(self._service()))
        self.assertFalse(
            Notification.objects.filter(recipient=self.owner.user).exists()
        )

    def test_a_refused_complete_makes_no_bill(self):
        future = self._visit(days=2)
        pending = self._appointment(Appointment.PENDING, days_from_today=0)
        for visit in (future, pending):
            self._complete(visit)
        self.assertFalse(BillingReceipt.objects.exists())

    def test_no_show_makes_no_bill(self):
        visit = self._visit()
        self._post("admin_appointment_no_show", visit)
        self.assertFalse(BillingReceipt.objects.exists())

    def test_the_only_code_that_sets_completed_starts_the_bill(self):
        """Step 0: admin_appointment_complete is the one path. Anything else
        that writes COMPLETED would need start_visit_bill too."""
        base = Path(settings.BASE_DIR)
        writers = []
        for path in base.glob("*/*.py"):
            if path.name.startswith("test") or "migrations" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            if re.search(r"\.status\s*=\s*Appointment\.COMPLETED", text):
                writers.append(path.name)
        self.assertEqual(writers, ["views.py"])
        views = (base / "appointments" / "views.py").read_text(encoding="utf-8")
        self.assertIn("start_visit_bill(appointment)", views)

    def _dialog_page(self, visit):
        return self.client.get(reverse("admin_appointment_detail", args=[visit.pk]))

    def test_the_dialog_says_a_bill_will_be_started(self):
        response = self._dialog_page(self._visit())
        self.assertContains(response, self.DIALOG_LINE, count=1)
        self.assertContains(
            response, f'<p class="mt-2 text-base text-gray-700">{self.DIALOG_LINE}</p>'
        )

    def test_the_dialog_line_is_left_out_when_no_bill_will_be_made(self):
        visit = self._visit()
        BillingReceipt.objects.create(
            appointment=visit,
            owner=self.owner,
            pet=self.pet,
            receipt_number="HV-2026-0300",
            payment_status=BillingReceipt.CANCELLED,
        )
        self.assertNotContains(self._dialog_page(visit), self.DIALOG_LINE)
        BillingReceipt.objects.all().delete()
        for model, obj in ((PetOwner, self.owner), (Pet, self.pet)):
            with self.subTest(archived=model.__name__):
                model.objects.filter(pk=obj.pk).update(is_archived=True)
                self.assertNotContains(self._dialog_page(visit), self.DIALOG_LINE)
                model.objects.filter(pk=obj.pk).update(is_archived=False)
        self.assertContains(self._dialog_page(visit), self.DIALOG_LINE)


class AutoBillAtomicTests(AppointmentTestBase):
    """Complete saves the status and starts the bill in one atomic block. The
    IntegrityError catches in billing.utils run after their own savepoint is
    rolled back, so a lost race never breaks that outer transaction."""

    def _visit(self):
        return self._appointment(Appointment.CONFIRMED, days_from_today=0)

    def _bill(self, visit=None, number="HV-2026-0400"):
        return BillingReceipt.objects.create(
            appointment=visit,
            owner=self.owner,
            pet=self.pet,
            receipt_number=number,
        )

    def test_a_bill_the_view_did_not_see_is_kept(self):
        """The "does this visit have a bill" lookup misses once (the other
        request saved its bill a moment later than the check); the OneToOne
        then refuses the second save."""
        visit = self._visit()
        existing = self._bill(visit)
        with mock.patch("billing.utils.visit_receipt", side_effect=[None]):
            response = self._post("admin_appointment_complete", visit)
        self.assertEqual(response.status_code, 302)
        self._assert_status(visit, Appointment.COMPLETED)
        self.assertEqual(BillingReceipt.objects.filter(appointment=visit).count(), 1)
        self.assertEqual(BillingReceipt.objects.get(appointment=visit).pk, existing.pk)
        self.assertEqual(
            self._messages(response),
            ["Marked Bantay's appointment on today at 9:00 AM as completed."],
        )

    def test_a_taken_receipt_number_is_retried_inside_the_block(self):
        """The first number is taken by another receipt: save_new_receipt
        retries once with the next one, and the visit is still completed."""
        visit = self._visit()
        self._bill(number="HV-2026-0400")
        with mock.patch.object(
            BillingReceipt,
            "generate_receipt_number",
            side_effect=["HV-2026-0400", "HV-2026-0401"],
        ):
            response = self._post("admin_appointment_complete", visit)
        self.assertEqual(response.status_code, 302)
        self._assert_status(visit, Appointment.COMPLETED)
        bill = BillingReceipt.objects.get(appointment=visit)
        self.assertEqual(bill.receipt_number, "HV-2026-0401")
        self.assertEqual(
            self._messages(response),
            [
                "Marked Bantay's appointment on today at 9:00 AM as completed. "
                "A bill was started for it."
            ],
        )


class BillWordingTests(AppointmentTestBase):
    """Fix bill wording: the visit page and the Complete flash say "bill"."""

    def _visible(self, html):
        body = re.sub(r"(?s)<(script|style)\b.*?</\1>", " ", html)
        attrs = re.findall(r'\b(?:aria-label|placeholder|alt|title)="([^"]*)"', body)
        text = re.sub(r"<[^>]+>", " ", body)
        return " ".join(unescape(text).split()) + " " + " ".join(attrs)

    def _completed(self, bill_status=None, number="HV-2026-0001"):
        visit = self._appointment(Appointment.COMPLETED, days_from_today=0)
        if bill_status:
            BillingReceipt.objects.create(
                appointment=visit,
                owner=self.owner,
                pet=self.pet,
                receipt_number=number,
                payment_status=bill_status,
            )
        return visit

    def _visit_text(self, visit):
        response = self.client.get(reverse("admin_appointment_detail", args=[visit.pk]))
        self.assertEqual(response.status_code, 200)
        text = self._visible(response.content.decode())
        self.assertNotIn("receipt", text.lower())
        return text

    def test_the_banner_for_an_unpaid_bill(self):
        text = self._visit_text(self._completed(BillingReceipt.PENDING))
        self.assertIn("Bill not paid yet.", text)
        self.assertIn("Open bill HV-2026-0001", text)

    def test_the_banner_for_a_paid_and_a_cancelled_bill(self):
        paid = self._visit_text(self._completed(BillingReceipt.PAID))
        self.assertIn("The bill is paid. One thing left: add the medical record.", paid)
        cancelled = self._visit_text(
            self._completed(BillingReceipt.CANCELLED, number="HV-2026-0002")
        )
        self.assertIn("The bill for this visit was cancelled.", cancelled)

    def test_the_banner_and_buttons_with_no_bill(self):
        text = self._visit_text(self._completed())
        self.assertIn("Next, add the medical record and create the bill.", text)
        self.assertIn("Create bill", text)

    def test_the_complete_flash_when_a_bill_is_started(self):
        visit = self._appointment(Appointment.CONFIRMED, days_from_today=0)
        response = self._post("admin_appointment_complete", visit)
        self.assertEqual(
            self._messages(response),
            [
                "Marked Bantay's appointment on today at 9:00 AM as completed. "
                "A bill was started for it."
            ],
        )

    def test_the_complete_flash_when_the_pet_is_archived(self):
        Pet.objects.filter(pk=self.pet.pk).update(is_archived=True)
        visit = self._appointment(Appointment.CONFIRMED, days_from_today=0)
        response = self._post("admin_appointment_complete", visit)
        self.assertEqual(
            self._messages(response),
            [
                "Marked Bantay's appointment on today at 9:00 AM as completed. "
                "No bill was started because Bantay is archived."
            ],
        )

    def test_the_complete_flash_when_a_bill_already_exists(self):
        visit = self._appointment(Appointment.CONFIRMED, days_from_today=0)
        BillingReceipt.objects.create(
            appointment=visit,
            owner=self.owner,
            pet=self.pet,
            receipt_number="HV-2026-0001",
        )
        response = self._post("admin_appointment_complete", visit)
        self.assertEqual(
            self._messages(response),
            ["Marked Bantay's appointment on today at 9:00 AM as completed."],
        )


class OwnerClinicInfoServicesCardTests(AppointmentTestBase):
    """The Services and prices card on the owner's Clinic info page."""

    def setUp(self):
        super().setUp()
        self.url = reverse("clinic_info")
        self.client.force_login(self.owner.user)

    def _main(self):
        html = self.client.get(self.url).content.decode()
        return html.split('<main id="main"')[1].split("</main>")[0]

    def _service(self, status, name="Consultation"):
        return Service.objects.create(service_name=name, status=status)

    def _card(self, main):
        return re.findall(r'<a href="/owner/services/[^"]*"[^>]*>', main)

    def test_the_card_shows_with_an_offered_service(self):
        self._service(Service.ACTIVE)
        main = self._main()
        found = self._card(main)
        self.assertEqual(len(found), 1)
        self.assertIn(f'href="{reverse("owner_services")}?from=clinic"', found[0])
        self.assertIn(
            'text-lg font-bold text-gray-900">Services and prices</span>', main
        )
        self.assertIn("What the clinic offers and what each one costs", main)

    def test_the_card_sits_between_the_buttons_and_the_details(self):
        self._service(Service.ACTIVE)
        row = get_clinic_settings()
        row.contact_number = "0917 000 0000"
        row.save()
        main = self._main()
        card = main.index("/owner/services/")
        self.assertLess(main.index('href="tel:09170000000"'), card)
        self.assertLess(card, main.index('aria-label="Clinic details"'))

    def test_the_card_is_hidden_without_an_offered_service(self):
        for status in (Service.DRAFT, Service.UNLISTED, Service.CLOSED):
            self._service(status, name=f"Hidden {status}")
        main = self._main()
        self.assertEqual(self._card(main), [])
        self.assertNotIn("Services and prices", main)

    def test_call_stays_the_only_violet_button(self):
        self._service(Service.ACTIVE)
        row = get_clinic_settings()
        row.contact_number = "0917 000 0000"
        row.save()
        violet = re.findall(r"<a [^>]*bg-violet-600[^>]*>", self._main())
        self.assertEqual(len(violet), 1)
        self.assertIn('href="tel:09170000000"', violet[0])

    def test_the_admin_clinic_info_page_has_no_card(self):
        self._service(Service.ACTIVE)
        self.client.force_login(self.admin)
        response = self.client.get(self.url)
        self.assertTemplateUsed(response, "admin/clinic_info.html")
        self.assertNotContains(response, "/owner/services/")
        self.assertNotContains(response, "what each one costs")
