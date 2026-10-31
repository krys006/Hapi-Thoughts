import datetime
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.db import DatabaseError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from appointments.models import Appointment, ClinicSettings
from dashboard.templatetags.friendly_dates import friendly_date
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
            f"Confirmed Bantay for {friendly_date(appointment.date)} at 9:00 AM. "
            "The owner has been notified.",
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
            f"Declined Bantay's request for {friendly_date(appointment.date)} "
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
                    "You can mark this after the appointment date (Tomorrow).",
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
                    f"Marked Bantay's appointment on Today at 9:00 AM {ending}",
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
                    f"Rescheduled Bantay to {friendly_date(self.target)} "
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

    def test_original_kept_if_creating_the_new_one_fails(self):
        original = self._appointment(Appointment.CONFIRMED)
        with mock.patch.object(
            Appointment.objects, "create", side_effect=DatabaseError("boom")
        ):
            with self.assertRaises(DatabaseError):
                self._post("admin_appointment_reschedule", original, self._data())
        self._assert_status(original, Appointment.CONFIRMED)
        self.assertEqual(Appointment.objects.count(), 1)


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
