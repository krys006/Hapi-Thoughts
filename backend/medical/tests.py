import datetime
import re
from decimal import Decimal
from unittest import mock
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.http import QueryDict
from django.template import Context, Template
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from appointments.models import Appointment, ClinicSettings
from dashboard.templatetags.friendly_dates import friendly_date_on
from notifications.models import Notification, NotificationPreference
from pets.models import Pet, PetOwner

from .forms import (
    MedicalRecordForm,
    TestResultFileForm,
    VaccinationEditForm,
    VaccinationForm,
)
from .models import MedicalRecord, PrescriptionItem, TestResultFile, Vaccination
from .utils import (
    can_remove_medicines,
    followups_due,
    is_superseded,
    last_reminder,
    last_reminders,
    medical_history_context,
    record_list_context,
    save_vaccination_edit,
    vaccination_history_context,
    vaccination_list_context,
    vaccinations_due,
    vaccinations_overdue,
)

User = get_user_model()

# Pages that load a new static file (5b-3: js/admin/record_form.js, 5b-4:
# js/admin/vaccination_form.js) render with plain static storage: the
# manifest has no entry for it until collectstatic runs.
PLAIN_STATIC = override_settings(
    STORAGES={
        **settings.STORAGES,
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
        },
    }
)


@PLAIN_STATIC
class ArchivedPetGuardTests(TestCase):
    """New records and vaccinations (standalone or on a record) are refused
    for an archived pet or a pet whose owner is archived (Slice 4b-1)."""

    def setUp(self):
        admin = User.objects.create_user(
            username="admin1", email="admin@example.com", role=User.ADMIN
        )
        self.client.force_login(admin)
        user = User.objects.create_user(
            username="ana_cruz", email="ana@example.com", role=User.PET_OWNER
        )
        self.owner = PetOwner.objects.create(
            user=user, first_name="Ana", last_name="Cruz"
        )
        self.pet = Pet.objects.create(owner=self.owner, name="Bantay", species="Dog")
        self.pet_url = reverse("admin_pet_detail", args=[self.pet.pk])
        today = timezone.localdate()
        self.record = MedicalRecord.objects.create(pet=self.pet, record_date=today)
        shot = {
            "vaccine_name": Vaccination.ANTI_RABIES,
            "date_administered": today.isoformat(),
        }
        self.pages = [
            (
                reverse("admin_medical_record_create", args=[self.pet.pk]),
                {"record_date": today.isoformat(), "diagnosis": "Checkup"},
                MedicalRecord,
            ),
            (
                reverse("admin_vaccination_standalone_create", args=[self.pet.pk]),
                shot,
                Vaccination,
            ),
            # 5b-2: a vaccination on a record goes through the create page
            # with the record (was admin_vaccination_add, removed)
            (
                reverse("admin_vaccination_standalone_create", args=[self.pet.pk]),
                {**shot, "record": self.record.pk},
                Vaccination,
            ),
        ]

    def _check_refused(self, message):
        for url, post, model in self.pages:
            for method in ("get", "post"):
                with self.subTest(url=url, method=method):
                    before = model.objects.count()
                    # Unread messages ride along in the cookie; start fresh
                    self.client.cookies.pop("messages", None)
                    response = getattr(self.client, method)(url, post)
                    self.assertRedirects(
                        response, self.pet_url, fetch_redirect_response=False
                    )
                    messages = [str(m) for m in get_messages(response.wsgi_request)]
                    self.assertEqual(messages, [message])
                    self.assertEqual(model.objects.count(), before)

    def _actions(self):
        """Batch 5a-2: the record actions guarded the same way, each with a
        count that must not change when refused."""
        MedicalRecord.objects.filter(pk=self.record.pk).update(
            follow_up_required=True,
            follow_up_date=timezone.localdate() + datetime.timedelta(days=7),
        )
        item = PrescriptionItem.objects.create(record=self.record, medicine_name="Amox")
        test_file = TestResultFile.objects.create(
            record=self.record, file="test_results/xray.pdf"
        )
        shot = Vaccination.objects.create(
            pet=self.pet,
            vaccine_name=Vaccination.ANTI_RABIES,
            date_administered=timezone.localdate(),
            next_due_date=timezone.localdate() + datetime.timedelta(days=365),
        )
        items = PrescriptionItem.objects.count
        active_files = TestResultFile.objects.filter(is_archived=False).count
        sent = Notification.objects.count
        upload = SimpleUploadedFile("scan.pdf", b"%PDF-1.4", "application/pdf")
        return [
            (
                reverse("admin_prescription_item_add", args=[self.record.pk]),
                {"medicine_name": "Doxy"},
                items,
            ),
            (reverse("admin_prescription_item_delete", args=[item.pk]), {}, items),
            (
                reverse("admin_test_result_file_upload", args=[self.record.pk]),
                {"file": upload},
                TestResultFile.objects.count,
            ),
            (
                reverse("admin_test_result_file_archive", args=[test_file.pk]),
                {},
                active_files,
            ),
            (reverse("admin_trigger_vaccination_reminder", args=[shot.pk]), {}, sent),
            (
                reverse("admin_trigger_followup_reminder", args=[self.record.pk]),
                {},
                sent,
            ),
        ]

    def _check_actions_refused(self, message):
        for url, post, count in self._actions():
            for method in ("get", "post"):
                with self.subTest(url=url, method=method):
                    before = count()
                    self.client.cookies.pop("messages", None)
                    response = getattr(self.client, method)(url, post)
                    self.assertRedirects(
                        response, self.pet_url, fetch_redirect_response=False
                    )
                    messages = [str(m) for m in get_messages(response.wsgi_request)]
                    self.assertEqual(messages, [message])
                    self.assertEqual(count(), before)

    def test_archived_pet(self):
        Pet.objects.filter(pk=self.pet.pk).update(is_archived=True)
        self._check_refused("Restore Bantay first.")
        self._check_actions_refused("Restore Bantay first.")

    def test_archived_owner(self):
        self.owner.archive()
        self._check_refused("Restore Ana Cruz first.")
        self._check_actions_refused("Restore Ana Cruz first.")

    def test_active_pet_opens_both_create_pages(self):
        for url, _post, _model in self.pages[:2]:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_active_pet_can_add_a_vaccination_to_a_record(self):
        url, post, _model = self.pages[2]
        response = self.client.post(url, post)
        self.assertRedirects(
            response,
            reverse("admin_medical_record_detail", args=[self.record.pk]),
            fetch_redirect_response=False,
        )
        self.assertTrue(Vaccination.objects.filter(medical_record=self.record).exists())


MANILA = ZoneInfo("Asia/Manila")
# Tue, Nov 3, 2026, 00:30 Manila: still Mon, Nov 2 in UTC
JUST_AFTER_MIDNIGHT = datetime.datetime(2026, 11, 3, 0, 30, tzinfo=MANILA)
TODAY = datetime.date(2026, 11, 3)
OLD_NOTE = "Wed, Oct 7, 2026: Next dose date was typed as Oct 5, 2027."


class FrozenVaccinationTestBase(TestCase):
    """An admin, a pet and one 8-in-1 given Tue, Oct 6, 2026, with time
    frozen just after midnight Manila on Tue, Nov 3, 2026."""

    def setUp(self):
        patcher = mock.patch(
            "django.utils.timezone.now", return_value=JUST_AFTER_MIDNIGHT
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        admin = User.objects.create_user(
            username="admin1", email="admin@example.com", role=User.ADMIN
        )
        self.client.force_login(admin)
        user = User.objects.create_user(
            username="ana_cruz", email="ana@example.com", role=User.PET_OWNER
        )
        owner = PetOwner.objects.create(user=user, first_name="Ana", last_name="Cruz")
        self.pet = Pet.objects.create(owner=owner, name="Bantay", species="Dog")
        self.shot = Vaccination.objects.create(
            pet=self.pet,
            vaccine_name=Vaccination.EIGHT_IN_ONE,
            date_administered=datetime.date(2026, 10, 6),
            batch_number="A1",
        )
        self.edit_url = reverse("admin_vaccination_edit", args=[self.shot.pk])

    def _data(self, **changes):
        """The saved values as posted, with changes applied."""
        data = {
            "vaccine_name": Vaccination.EIGHT_IN_ONE,
            "custom_vaccine_name": "",
            "date_administered": "2026-10-06",
            "next_due_date": "",
            "weight_at_vaccination": "",
            "site_of_injection": "",
            "batch_number": "A1",
            "manufacturer": "",
            "administered_by": "",
            "correction_reason": "",
        }
        data.update(changes)
        return data

    def _edit_form(self, **changes):
        instance = Vaccination.objects.get(pk=self.shot.pk)
        return VaccinationEditForm(self._data(**changes), instance=instance)

    def _post(self, **changes):
        return self.client.post(self.edit_url, self._data(**changes))


@PLAIN_STATIC
class VaccinationEditFormTests(FrozenVaccinationTestBase):
    """Batch 5a-1: a clinical change needs a reason and is kept as a dated
    line in correction_note; blanks and label fields are not corrections."""

    def test_blank_weight_and_next_date_saved_again_is_no_change(self):
        form = self._edit_form()
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.changed_clinical, [])
        save_vaccination_edit(form, TODAY)
        self.shot.refresh_from_db()
        self.assertFalse(self.shot.is_corrected)
        self.assertEqual(self.shot.correction_note, "")

    def test_same_weight_written_differently_is_no_change(self):
        Vaccination.objects.filter(pk=self.shot.pk).update(
            weight_at_vaccination=Decimal("12.50"), site_of_injection="Right leg"
        )
        form = self._edit_form(
            weight_at_vaccination="12.5", site_of_injection="  Right leg "
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.changed_clinical, [])

    def test_label_fields_change_without_a_reason(self):
        response = self._post(
            batch_number="B2", manufacturer="Zoetis", administered_by="Dr. Sadiwa"
        )
        self.assertEqual(response.status_code, 302)
        self.shot.refresh_from_db()
        self.assertEqual(self.shot.batch_number, "B2")
        self.assertEqual(self.shot.manufacturer, "Zoetis")
        self.assertFalse(self.shot.is_corrected)
        self.assertEqual(self.shot.correction_note, "")

    def test_clinical_change_without_a_reason_is_refused(self):
        response = self._post(date_administered="2026-10-05")
        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        self.assertEqual(
            form.errors["correction_reason"],
            [
                "You changed the date given (was Tue, Oct 6). "
                "Write why, so the change is on record."
            ],
        )
        self.assertEqual(form.changed_clinical, [("the date given", "Tue, Oct 6")])
        # 5b-4: the page's line reads "You changed: Date given (was …)."
        # (was "Changed: the date given (was …)" on the old edit page)
        self.assertContains(response, "You changed:")
        self.shot.refresh_from_db()
        self.assertEqual(self.shot.date_administered, datetime.date(2026, 10, 6))
        self.assertFalse(self.shot.is_corrected)

    def test_several_changes_are_joined_in_the_error(self):
        Vaccination.objects.filter(pk=self.shot.pk).update(
            weight_at_vaccination=Decimal("12.50")
        )
        form = self._edit_form(
            date_administered="2026-10-05", weight_at_vaccination="13"
        )
        self.assertFalse(form.is_valid())
        self.assertEqual(
            form.errors["correction_reason"],
            [
                "You changed the date given (was Tue, Oct 6) and the weight "
                "(was 12.5). Write why, so the change is on record."
            ],
        )

    def test_labels_and_old_values(self):
        Vaccination.objects.filter(pk=self.shot.pk).update(
            date_administered=datetime.date(2025, 12, 30)
        )
        form = self._edit_form(
            vaccine_name=Vaccination.SIX_IN_ONE,
            date_administered="2025-12-29",
            next_due_date="2026-12-29",
            weight_at_vaccination="4",
            site_of_injection="Neck",
        )
        self.assertFalse(form.is_valid())
        self.assertEqual(
            form.changed_clinical,
            [
                ("the vaccine", "8-in-1"),
                ("the date given", "Tue, Dec 30, 2025"),
                ("the next dose date", "empty"),
                ("the weight", "empty"),
                ("where it was given", "empty"),
            ],
        )
        self.assertIn(
            "You changed the vaccine (was 8-in-1), the date given "
            "(was Tue, Dec 30, 2025), the next dose date (was empty), "
            "the weight (was empty) and where it was given (was empty).",
            form.errors["correction_reason"][0],
        )

    def test_clinical_change_with_a_reason_adds_a_dated_line(self):
        response = self._post(
            date_administered="2026-10-05", correction_reason=" wrong date typed "
        )
        self.assertEqual(response.status_code, 302)
        self.shot.refresh_from_db()
        self.assertEqual(self.shot.date_administered, datetime.date(2026, 10, 5))
        self.assertTrue(self.shot.is_corrected)
        self.assertEqual(
            self.shot.correction_note, "Tue, Nov 3, 2026: wrong date typed"
        )

    def test_old_correction_text_is_kept(self):
        Vaccination.objects.filter(pk=self.shot.pk).update(
            is_corrected=True, correction_note=OLD_NOTE
        )
        self._post(next_due_date="2027-10-06", correction_reason="due date missed")
        self.shot.refresh_from_db()
        self.assertTrue(self.shot.is_corrected)
        self.assertEqual(
            self.shot.correction_note,
            f"{OLD_NOTE}\nTue, Nov 3, 2026: due date missed",
        )

    def test_a_second_correction_adds_a_second_line(self):
        self._post(date_administered="2026-10-05", correction_reason="first")
        self._post(
            date_administered="2026-10-05",
            site_of_injection="Neck",
            correction_reason="second",
        )
        self.shot.refresh_from_db()
        self.assertEqual(
            self.shot.correction_note,
            "Tue, Nov 3, 2026: first\nTue, Nov 3, 2026: second",
        )

    def test_reason_without_a_clinical_change_is_ignored(self):
        Vaccination.objects.filter(pk=self.shot.pk).update(correction_note=OLD_NOTE)
        response = self._post(batch_number="B2", correction_reason="just because")
        self.assertEqual(response.status_code, 302)
        self.shot.refresh_from_db()
        self.assertEqual(self.shot.batch_number, "B2")
        self.assertEqual(self.shot.correction_note, OLD_NOTE)
        self.assertFalse(self.shot.is_corrected)

    def test_reason_is_never_prefilled_and_old_lines_show(self):
        Vaccination.objects.filter(pk=self.shot.pk).update(
            is_corrected=True, correction_note=f"{OLD_NOTE}\nThu, Oct 8, 2026: site"
        )
        response = self.client.get(self.edit_url)
        self.assertIsNone(response.context["form"]["correction_reason"].value())
        self.assertNotContains(response, 'name="correction_note"')
        self.assertContains(response, "Reason for a correction")
        self.assertContains(response, "Corrected before")
        self.assertContains(response, OLD_NOTE)
        self.assertContains(response, "Thu, Oct 8, 2026: site")

    def test_no_corrected_before_without_old_lines(self):
        response = self.client.get(self.edit_url)
        self.assertNotContains(response, "Corrected before")

    def test_listed_vaccine_clears_a_typed_name(self):
        Vaccination.objects.filter(pk=self.shot.pk).update(
            vaccine_name=Vaccination.OTHER, custom_vaccine_name="Lepto"
        )
        self._post(custom_vaccine_name="Lepto", correction_reason="wrong vaccine")
        self.shot.refresh_from_db()
        self.assertEqual(self.shot.vaccine_name, Vaccination.EIGHT_IN_ONE)
        self.assertEqual(self.shot.custom_vaccine_name, "")
        self.assertTrue(self.shot.is_corrected)

    def test_stale_typed_name_on_a_listed_vaccine_is_no_change(self):
        Vaccination.objects.filter(pk=self.shot.pk).update(custom_vaccine_name="Old")
        form = self._edit_form(custom_vaccine_name="Old")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.changed_clinical, [])
        self.assertEqual(form.cleaned_data["custom_vaccine_name"], "")

    def test_typed_name_change_counts_as_the_vaccine(self):
        Vaccination.objects.filter(pk=self.shot.pk).update(
            vaccine_name=Vaccination.OTHER, custom_vaccine_name="Lepto"
        )
        form = self._edit_form(
            vaccine_name=Vaccination.OTHER, custom_vaccine_name="FVRCP"
        )
        self.assertFalse(form.is_valid())
        self.assertEqual(form.changed_clinical, [("the vaccine", "Lepto")])


@PLAIN_STATIC
class VaccinationFormDateTests(FrozenVaccinationTestBase):
    """Batch 5a-1: vaccine name and date rules on both vaccination forms."""

    def _forms(self, **data):
        """The add form and the edit form, both bound to the same data."""
        bound = self._data(**data)
        return [
            VaccinationForm(bound),
            VaccinationEditForm(
                bound, instance=Vaccination.objects.get(pk=self.shot.pk)
            ),
        ]

    def _check(self, field, message, **data):
        for form in self._forms(correction_reason="fix", **data):
            with self.subTest(form=type(form).__name__):
                self.assertFalse(form.is_valid())
                self.assertEqual(form.errors[field], [message])

    def test_other_needs_a_typed_name(self):
        self._check(
            "custom_vaccine_name",
            "Type the vaccine name.",
            vaccine_name=Vaccination.OTHER,
            custom_vaccine_name="  ",
        )

    def test_listed_vaccine_clears_a_typed_name_on_add(self):
        form = VaccinationForm(self._data(custom_vaccine_name="Lepto"))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save(commit=False).custom_vaccine_name, "")

    def test_date_given_in_the_future_fails(self):
        self._check(
            "date_administered",
            "The date given can't be in the future.",
            date_administered="2026-11-04",
        )

    def test_today_manila_is_allowed(self):
        # Still Nov 2 in UTC: a UTC date would call Nov 3 the future
        for form in self._forms(
            date_administered="2026-11-03", correction_reason="fix"
        ):
            with self.subTest(form=type(form).__name__):
                self.assertTrue(form.is_valid(), form.errors)

    def test_next_dose_on_or_before_the_date_given_fails(self):
        for next_due in ("2026-10-06", "2026-10-01"):
            with self.subTest(next_due=next_due):
                self._check(
                    "next_due_date",
                    "The next dose must be after the date given.",
                    next_due_date=next_due,
                )

    def test_next_dose_after_the_date_given_passes(self):
        for form in self._forms(next_due_date="2026-10-07", correction_reason="fix"):
            with self.subTest(form=type(form).__name__):
                self.assertTrue(form.is_valid(), form.errors)

    def test_next_dose_error_shows_on_the_add_page(self):
        url = reverse("admin_vaccination_standalone_create", args=[self.pet.pk])
        response = self.client.post(url, self._data(next_due_date="2026-10-01"))
        self.assertContains(response, "The next dose must be after the date given.")


class CorrectViewRemovedTests(SimpleTestCase):
    """Batch 5a-1: the separate Correct path is gone; edits handle it."""

    def test_correct_url_name_no_longer_resolves(self):
        with self.assertRaises(NoReverseMatch):
            reverse("admin_vaccination_correct", args=[1])


# ── Batch 5a-2: record, medicine and file rules ──────────────────────────────

MB = 1024 * 1024
IN_MEMORY_FILES = {
    **settings.STORAGES,
    "default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
}


class FrozenMedicalTestBase(TestCase):
    """An admin (logged in), Ana Cruz and her dog Bantay, with time frozen
    at self.now (default: 00:30 Manila on Tue, Nov 3, 2026, still Nov 2 in
    UTC). Change self.now to move the clock."""

    def setUp(self):
        self.now = JUST_AFTER_MIDNIGHT
        patcher = mock.patch("django.utils.timezone.now", side_effect=lambda: self.now)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.admin = User.objects.create_user(
            username="admin1", email="admin@example.com", role=User.ADMIN
        )
        self.client.force_login(self.admin)
        self.owner_user = User.objects.create_user(
            username="ana_cruz", email="ana@example.com", role=User.PET_OWNER
        )
        self.owner = PetOwner.objects.create(
            user=self.owner_user, first_name="Ana", last_name="Cruz"
        )
        self.pet = Pet.objects.create(owner=self.owner, name="Bantay", species="Dog")

    def _messages(self, response):
        return [str(m) for m in get_messages(response.wsgi_request)]

    def _record(self, **fields):
        fields.setdefault("record_date", TODAY)
        return MedicalRecord.objects.create(pet=self.pet, **fields)


@PLAIN_STATIC
class RecordFormDateTests(FrozenMedicalTestBase):
    """Visit date and follow-up rules on MedicalRecordForm (Manila date)."""

    def _form(self, **changes):
        data = {
            "record_date": "2026-11-03",
            "diagnosis": "Checkup",
            "follow_up_date": "",
        }
        data.update(changes)
        return MedicalRecordForm(data)

    def test_visit_date_in_the_future_fails(self):
        form = self._form(record_date="2026-11-04")
        self.assertFalse(form.is_valid())
        self.assertEqual(
            form.errors["record_date"], ["The visit date can't be in the future."]
        )

    def test_today_manila_is_allowed(self):
        # Still Nov 2 in UTC: a UTC date would call Nov 3 the future
        form = self._form(record_date="2026-11-03")
        self.assertTrue(form.is_valid(), form.errors)

    def test_follow_up_without_a_date_fails_on_the_date(self):
        form = self._form(follow_up_required="on")
        self.assertFalse(form.is_valid())
        self.assertEqual(
            form.errors["follow_up_date"],
            ["Pick the follow-up date, or untick “Needs a follow-up visit”."],
        )
        self.assertEqual(form.non_field_errors(), [])

    def test_follow_up_on_or_before_the_visit_date_fails(self):
        for follow_up in ("2026-11-03", "2026-11-01"):
            with self.subTest(follow_up=follow_up):
                form = self._form(follow_up_required="on", follow_up_date=follow_up)
                self.assertFalse(form.is_valid())
                self.assertEqual(
                    form.errors["follow_up_date"],
                    ["The follow-up date must be after the visit date."],
                )

    def test_follow_up_after_the_visit_date_passes(self):
        form = self._form(follow_up_required="on", follow_up_date="2026-11-10")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(
            form.save(commit=False).follow_up_date, datetime.date(2026, 11, 10)
        )

    def test_unticked_follow_up_clears_the_date(self):
        # Even a date that would fail the rule is simply dropped
        for follow_up in ("2026-11-10", "2026-11-01"):
            with self.subTest(follow_up=follow_up):
                form = self._form(follow_up_date=follow_up)
                self.assertTrue(form.is_valid(), form.errors)
                self.assertIsNone(form.save(commit=False).follow_up_date)

    def test_edit_unticked_saves_the_date_as_none(self):
        record = self._record(
            follow_up_required=True, follow_up_date=datetime.date(2026, 11, 10)
        )
        url = reverse("admin_medical_record_edit", args=[record.pk])
        self.client.post(
            url, {"record_date": "2026-11-03", "follow_up_date": "2026-11-10"}
        )
        record.refresh_from_db()
        self.assertFalse(record.follow_up_required)
        self.assertIsNone(record.follow_up_date)

    def test_errors_show_on_the_record_page(self):
        url = reverse("admin_medical_record_create", args=[self.pet.pk])
        response = self.client.post(
            url, {"record_date": "2026-11-04", "follow_up_required": "on"}
        )
        self.assertContains(response, "The visit date can&#x27;t be in the future.")
        self.assertContains(
            response, "Pick the follow-up date, or untick “Needs a follow-up visit”."
        )
        self.assertFalse(MedicalRecord.objects.exists())


@PLAIN_STATIC
class RecordFromVisitTests(FrozenMedicalTestBase):
    """A record started from a visit (?appointment= or the POST field)."""

    def setUp(self):
        super().setUp()
        self.url = reverse("admin_medical_record_create", args=[self.pet.pk])
        self.visit = self._visit(Appointment.CONFIRMED)

    def _visit(self, status, pet=None, hour=9):
        pet = pet or self.pet
        return Appointment.objects.create(
            owner=pet.owner,
            pet=pet,
            date=TODAY,
            time=datetime.time(hour, 0),
            status=status,
        )

    def _post(self, appointment_pk, **data):
        post = {"record_date": "2026-11-03", "diagnosis": "Checkup"}
        post.update(data)
        return self.client.post(
            f"{self.url}?appointment={appointment_pk}",
            {**post, "appointment": appointment_pk},
        )

    def _check_refused(self, visit, message):
        for method in ("get", "post"):
            with self.subTest(method=method):
                self.client.cookies.pop("messages", None)
                if method == "get":
                    response = self.client.get(f"{self.url}?appointment={visit.pk}")
                else:
                    response = self._post(visit.pk)
                self.assertRedirects(
                    response,
                    reverse("admin_appointment_detail", args=[visit.pk]),
                    fetch_redirect_response=False,
                )
                self.assertEqual(self._messages(response), [message])
                self.assertFalse(MedicalRecord.objects.exists())

    def test_confirmed_and_completed_visits_get_a_record(self):
        completed = self._visit(Appointment.COMPLETED, hour=10)
        for visit in (self.visit, completed):
            with self.subTest(status=visit.status):
                get = self.client.get(f"{self.url}?appointment={visit.pk}")
                self.assertEqual(get.status_code, 200)
                response = self._post(visit.pk)
                record = MedicalRecord.objects.get(appointment=visit)
                self.assertRedirects(
                    response,
                    reverse("admin_medical_record_detail", args=[record.pk]),
                    fetch_redirect_response=False,
                )
                self.assertEqual(record.pet, self.pet)

    def test_another_pets_visit_is_refused(self):
        other = Pet.objects.create(owner=self.owner, name="Muning", species="Cat")
        visit = self._visit(Appointment.CONFIRMED, pet=other, hour=11)
        self._check_refused(visit, "That visit belongs to another pet.")

    def test_pending_cancelled_and_no_show_visits_are_refused(self):
        for hour, status in enumerate(
            (Appointment.PENDING, Appointment.CANCELLED, Appointment.NO_SHOW), start=12
        ):
            with self.subTest(status=status):
                self._check_refused(
                    self._visit(status, hour=hour),
                    "A record can only be added to a confirmed or completed visit.",
                )

    def test_an_id_that_isnt_a_whole_number_is_a_404(self):
        for value in ("abc", "1.5", "1e3", "%20"):
            with self.subTest(value=value):
                response = self.client.get(f"{self.url}?appointment={value}")
                self.assertEqual(response.status_code, 404)
        response = self.client.post(
            self.url, {"record_date": "2026-11-03", "appointment": "abc"}
        )
        self.assertEqual(response.status_code, 404)
        self.assertFalse(MedicalRecord.objects.exists())

    def test_an_unknown_visit_is_a_404(self):
        response = self.client.get(f"{self.url}?appointment=999999")
        self.assertEqual(response.status_code, 404)

    def test_a_visit_with_a_record_goes_to_that_record(self):
        record = self._record(appointment=self.visit)
        for method in ("get", "post"):
            with self.subTest(method=method):
                self.client.cookies.pop("messages", None)
                if method == "get":
                    response = self.client.get(
                        f"{self.url}?appointment={self.visit.pk}"
                    )
                else:
                    response = self._post(self.visit.pk)
                self.assertRedirects(
                    response,
                    reverse("admin_medical_record_detail", args=[record.pk]),
                    fetch_redirect_response=False,
                )
                self.assertEqual(
                    self._messages(response),
                    ["A medical record already exists for this appointment."],
                )
                self.assertEqual(MedicalRecord.objects.count(), 1)

    def test_two_saves_at_once_go_to_the_existing_record(self):
        # The other save lands after this request's check: the OneToOne
        # raises IntegrityError, which must become a redirect, not a 500
        record = self._record(appointment=self.visit)
        with mock.patch("medical.utils.existing_visit_record", return_value=None):
            response = self._post(self.visit.pk)
        self.assertRedirects(
            response,
            reverse("admin_medical_record_detail", args=[record.pk]),
            fetch_redirect_response=False,
        )
        self.assertEqual(
            self._messages(response),
            ["A medical record already exists for this appointment."],
        )
        self.assertEqual(MedicalRecord.objects.count(), 1)
        self.assertFalse(Notification.objects.exists())

    def _visit_on(self, day, hour):
        return Appointment.objects.create(
            owner=self.owner,
            pet=self.pet,
            date=day,
            time=datetime.time(hour, 0),
            status=Appointment.CONFIRMED,
        )

    def test_a_future_visit_is_refused(self):
        cases = [
            (datetime.date(2026, 11, 9), 15, "Mon, Nov 9"),
            (datetime.date(2027, 1, 5), 16, "Tue, Jan 5, 2027"),
        ]
        for day, hour, shown in cases:
            with self.subTest(day=day):
                self._check_refused(
                    self._visit_on(day, hour),
                    "This visit hasn't happened yet. "
                    f"Write the record on or after {shown}.",
                )

    def test_todays_visit_opens_with_todays_date(self):
        # 00:30 Manila on Nov 3 is still Nov 2 in UTC: a UTC date would
        # call today's visit the future
        response = self.client.get(f"{self.url}?appointment={self.visit.pk}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"].initial["record_date"], TODAY)

    def test_a_past_visit_prefills_its_date(self):
        visit = self._visit_on(datetime.date(2026, 11, 1), 17)
        response = self.client.get(f"{self.url}?appointment={visit.pk}")
        self.assertEqual(
            response.context["form"].initial["record_date"], datetime.date(2026, 11, 1)
        )


class MedicineRemoveTests(FrozenMedicalTestBase):
    """Medicines can be removed only on the Manila day the record was written."""

    def setUp(self):
        super().setUp()
        # Written Mon, Nov 2 at 22:30 Manila (14:30 UTC)
        self.now = datetime.datetime(2026, 11, 2, 22, 30, tzinfo=MANILA)
        self.record = self._record(record_date=datetime.date(2026, 11, 2))
        self.item = PrescriptionItem.objects.create(
            record=self.record, medicine_name="Amoxicillin"
        )
        self.url = reverse("admin_prescription_item_delete", args=[self.item.pk])
        self.record_url = reverse("admin_medical_record_detail", args=[self.record.pk])

    def test_same_day_removal_works(self):
        self.now = datetime.datetime(2026, 11, 2, 23, 50, tzinfo=MANILA)
        response = self.client.post(self.url)
        self.assertRedirects(response, self.record_url, fetch_redirect_response=False)
        self.assertEqual(self._messages(response), ["Prescription item removed."])
        self.assertFalse(PrescriptionItem.objects.exists())

    def test_next_morning_is_refused(self):
        # 07:00 Manila on Nov 3 is still Nov 2 in UTC: a UTC date would allow it
        self.now = datetime.datetime(2026, 11, 3, 7, 0, tzinfo=MANILA)
        response = self.client.post(self.url)
        self.assertRedirects(response, self.record_url, fetch_redirect_response=False)
        self.assertEqual(
            self._messages(response),
            ["Medicines can only be removed on the day the record was written."],
        )
        self.assertTrue(PrescriptionItem.objects.filter(pk=self.item.pk).exists())

    def test_get_removes_nothing(self):
        response = self.client.get(self.url)
        self.assertRedirects(response, self.record_url, fetch_redirect_response=False)
        self.assertTrue(PrescriptionItem.objects.filter(pk=self.item.pk).exists())

    def test_can_remove_medicines_helper(self):
        self.assertTrue(can_remove_medicines(self.record, datetime.date(2026, 11, 2)))
        self.assertFalse(can_remove_medicines(self.record, datetime.date(2026, 11, 3)))


class TestFileUploadTests(FrozenMedicalTestBase):
    """Test files: PDF, JPG or PNG (any case), up to 10 MB."""

    def _form(self, name, size=10, content_type="application/pdf"):
        upload = SimpleUploadedFile(name, b"x" * size, content_type)
        return TestResultFileForm({"description": ""}, {"file": upload})

    def test_allowed_types_in_any_case(self):
        for name in ("scan.pdf", "SCAN.PDF", "xray.jpg", "xray.JPEG", "blood.Png"):
            with self.subTest(name=name):
                form = self._form(name)
                self.assertTrue(form.is_valid(), form.errors)

    def test_other_types_are_refused(self):
        for name in ("notes.docx", "photo.gif", "virus.exe", "pdf", "scan.pdf.exe"):
            with self.subTest(name=name):
                form = self._form(name)
                self.assertFalse(form.is_valid())
                self.assertEqual(
                    form.errors["file"], ["Only PDF, JPG or PNG files can be added."]
                )

    def test_exactly_10_mb_is_allowed(self):
        form = self._form("scan.pdf", size=10 * MB)
        self.assertTrue(form.is_valid(), form.errors)

    def test_just_over_10_mb_is_refused(self):
        form = self._form("scan.pdf", size=10 * MB + 1)
        self.assertFalse(form.is_valid())
        self.assertEqual(
            form.errors["file"],
            ["This file is 10.1 MB. Please choose one up to 10 MB."],
        )

    def test_size_is_rounded_up(self):
        form = self._form("scan.pdf", size=int(12.31 * MB))
        self.assertFalse(form.is_valid())
        self.assertEqual(
            form.errors["file"],
            ["This file is 12.4 MB. Please choose one up to 10 MB."],
        )

    def test_size_is_rounded_to_one_decimal(self):
        form = self._form("scan.pdf", size=int(12.36 * MB))
        self.assertFalse(form.is_valid())
        self.assertEqual(
            form.errors["file"],
            ["This file is 12.4 MB. Please choose one up to 10 MB."],
        )

    def test_widget_accepts_only_the_allowed_types(self):
        widget = TestResultFileForm().fields["file"].widget
        self.assertEqual(widget.attrs["accept"], ".pdf,.jpg,.jpeg,.png")

    @override_settings(STORAGES=IN_MEMORY_FILES)
    def test_upload_view_saves_a_good_file(self):
        record = self._record()
        url = reverse("admin_test_result_file_upload", args=[record.pk])
        upload = SimpleUploadedFile("xray.png", b"\x89PNG", "image/png")
        response = self.client.post(url, {"file": upload, "description": "X-ray"})
        self.assertEqual(self._messages(response), ["File uploaded."])
        self.assertEqual(record.test_result_files.count(), 1)

    def test_upload_view_shows_the_real_error(self):
        # 5a-5: the record page is re-rendered with the error (was a redirect
        # with the error flashed)
        record = self._record()
        url = reverse("admin_test_result_file_upload", args=[record.pk])
        upload = SimpleUploadedFile("notes.docx", b"x", "application/msword")
        response = self.client.post(url, {"file": upload})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["open_dialog"], "add-file")
        self.assertContains(response, "Only PDF, JPG or PNG files can be added.")
        self.assertFalse(TestResultFile.objects.exists())


class TestFileArchiveTests(FrozenMedicalTestBase):
    """Remove = archive, once; the second time changes nothing."""

    def setUp(self):
        super().setUp()
        self.record = self._record()
        self.file = TestResultFile.objects.create(
            record=self.record, file="test_results/xray.pdf"
        )
        self.url = reverse("admin_test_result_file_archive", args=[self.file.pk])

    def test_archive_hides_the_file(self):
        response = self.client.post(self.url)
        self.assertRedirects(
            response,
            reverse("admin_medical_record_detail", args=[self.record.pk]),
            fetch_redirect_response=False,
        )
        self.assertEqual(
            self._messages(response), ["File removed. It's hidden from Ana Cruz."]
        )
        self.file.refresh_from_db()
        self.assertTrue(self.file.is_archived)
        self.assertEqual(self.file.archived_at, JUST_AFTER_MIDNIGHT)

    def test_archive_twice_keeps_archived_at(self):
        self.client.post(self.url)
        self.now = JUST_AFTER_MIDNIGHT + datetime.timedelta(hours=5)
        self.client.cookies.pop("messages", None)
        response = self.client.post(self.url)
        self.assertEqual(self._messages(response), ["That file was already removed."])
        self.file.refresh_from_db()
        self.assertEqual(self.file.archived_at, JUST_AFTER_MIDNIGHT)

    def test_get_archives_nothing(self):
        self.client.get(self.url)
        self.file.refresh_from_db()
        self.assertFalse(self.file.is_archived)


class OwnerMedicalHistoryTests(FrozenMedicalTestBase):
    """The owner's read-only history page."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner_user)
        self.url = reverse("owner_pet_medical_history", args=[self.pet.pk])

    def test_archived_files_never_show(self):
        record = self._record()
        TestResultFile.objects.create(
            record=record, file="test_results/wrong.pdf", is_archived=True
        )
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Test Results:")
        self.assertEqual(response.context["records"][0].active_test_files, [])

    def test_active_files_still_show(self):
        record = self._record()
        TestResultFile.objects.create(record=record, file="test_results/xray.pdf")
        response = self.client.get(self.url)
        self.assertContains(response, "Test Results:")

    def test_follow_up_date_only_when_required(self):
        self._record(
            follow_up_required=False, follow_up_date=datetime.date(2026, 12, 1)
        )
        response = self.client.get(self.url)
        self.assertNotContains(response, "December 1, 2026")
        self.assertNotContains(response, "Follow-up Date:")

        self._record(follow_up_required=True, follow_up_date=datetime.date(2026, 12, 9))
        response = self.client.get(self.url)
        self.assertContains(response, "December 9, 2026")
        self.assertNotContains(response, "December 1, 2026")

    def test_private_notes_never_show(self):
        self._record(
            public_notes="Give food slowly", private_notes="Owner seemed upset"
        )
        response = self.client.get(self.url)
        self.assertContains(response, "Give food slowly")
        self.assertNotContains(response, "Owner seemed upset")
        # 5b-2: not even the admin page's card title
        self.assertNotContains(response, "Private notes")


def _days(n):
    return TODAY + datetime.timedelta(days=n)


class DueRuleTests(FrozenMedicalTestBase):
    """Batch 5a-3: the shared due rule in medical/utils.py. today is
    timezone.localdate() at 00:30 Manila on Nov 3 (a UTC date would be
    Nov 2 and move every edge by a day)."""

    def setUp(self):
        super().setUp()
        self.today = timezone.localdate()

    def _dose(self, due_days, given_days=-300, pet=None, **fields):
        fields.setdefault("vaccine_name", Vaccination.ANTI_RABIES)
        return Vaccination.objects.create(
            pet=pet or self.pet,
            date_administered=_days(given_days),
            next_due_date=None if due_days is None else _days(due_days),
            **fields,
        )

    def _followup(self, days, pet=None, required=True):
        return MedicalRecord.objects.create(
            pet=pet or self.pet,
            record_date=_days(-10),
            follow_up_required=required,
            follow_up_date=_days(days),
        )

    def _due(self):
        return list(vaccinations_due(self.today))

    def _overdue(self):
        return list(vaccinations_overdue(self.today))

    def _followups(self):
        return list(followups_due(self.today))

    def test_today_is_the_manila_date(self):
        self.assertEqual(self.today, TODAY)

    def test_due_window_edges(self):
        # (days until next due, in due, in overdue)
        cases = (
            (0, True, False),
            (7, True, False),
            (8, False, False),
            (-1, False, True),
        )
        for due_days, due, overdue in cases:
            with self.subTest(due_days=due_days):
                Vaccination.objects.all().delete()
                dose = self._dose(due_days)
                self.assertEqual(self._due(), [dose] if due else [])
                self.assertEqual(self._overdue(), [dose] if overdue else [])

    def test_no_next_date_is_never_due(self):
        self._dose(None)
        self.assertEqual(self._due(), [])
        self.assertEqual(self._overdue(), [])

    def test_superseded_dose_is_left_out(self):
        due = self._dose(3)
        overdue = self._dose(-3, given_days=-400)
        later = self._dose(360, given_days=-5)
        self.assertEqual(self._due(), [])
        self.assertEqual(self._overdue(), [])
        self.assertTrue(is_superseded(due))
        self.assertTrue(is_superseded(overdue))
        self.assertFalse(is_superseded(later))

    def test_a_later_dose_of_another_vaccine_does_not_supersede(self):
        due = self._dose(3)
        self._dose(360, given_days=-5, vaccine_name=Vaccination.DEWORMING)
        self.assertEqual(self._due(), [due])
        self.assertFalse(is_superseded(due))

    def test_a_later_dose_with_another_typed_name_does_not_supersede(self):
        due = self._dose(3, vaccine_name=Vaccination.OTHER, custom_vaccine_name="Lepto")
        self._dose(
            360,
            given_days=-5,
            vaccine_name=Vaccination.OTHER,
            custom_vaccine_name="Giardia",
        )
        self.assertEqual(self._due(), [due])
        self.assertFalse(is_superseded(due))
        # The same typed name does supersede it
        self._dose(
            360,
            given_days=-4,
            vaccine_name=Vaccination.OTHER,
            custom_vaccine_name="Lepto",
        )
        self.assertEqual(self._due(), [])
        self.assertTrue(is_superseded(due))

    def test_another_pets_later_dose_does_not_supersede(self):
        other = Pet.objects.create(owner=self.owner, name="Muning", species="Cat")
        due = self._dose(3)
        self._dose(360, given_days=-5, pet=other)
        self.assertEqual(self._due(), [due])

    def test_corrected_dose_counts(self):
        due = self._dose(3, is_corrected=True, correction_note="Fixed batch")
        overdue = self._dose(
            -3,
            vaccine_name=Vaccination.DEWORMING,
            is_corrected=True,
            correction_note="Fixed batch",
        )
        self.assertEqual(self._due(), [due])
        self.assertEqual(self._overdue(), [overdue])

    def test_followup_window_edges(self):
        for days, due in ((0, True), (7, True), (8, False), (-1, False)):
            with self.subTest(days=days):
                MedicalRecord.objects.all().delete()
                record = self._followup(days)
                self.assertEqual(self._followups(), [record] if due else [])

    def test_followup_not_required_is_left_out(self):
        self._followup(3, required=False)
        self.assertEqual(self._followups(), [])

    def test_archived_pet_is_left_out(self):
        self._dose(3)
        self._dose(-3, vaccine_name=Vaccination.DEWORMING)
        self._followup(3)
        Pet.objects.filter(pk=self.pet.pk).update(is_archived=True)
        self.assertEqual(self._due(), [])
        self.assertEqual(self._overdue(), [])
        self.assertEqual(self._followups(), [])

    def test_archived_owner_is_left_out(self):
        self._dose(3)
        self._dose(-3, vaccine_name=Vaccination.DEWORMING)
        self._followup(3)
        self.owner.archive()
        self.assertEqual(self._due(), [])
        self.assertEqual(self._overdue(), [])
        self.assertEqual(self._followups(), [])


class VaccinationReminderTests(FrozenMedicalTestBase):
    """Batch 5a-3: the Send reminder button refuses a dose with no next
    dose date and a superseded dose."""

    def _dose(self, due_days=3, given_days=-300, **fields):
        fields.setdefault("vaccine_name", Vaccination.ANTI_RABIES)
        return Vaccination.objects.create(
            pet=self.pet,
            date_administered=_days(given_days),
            next_due_date=None if due_days is None else _days(due_days),
            **fields,
        )

    def _send(self, dose):
        url = reverse("admin_trigger_vaccination_reminder", args=[dose.pk])
        response = self.client.post(url)
        self.assertRedirects(
            response,
            reverse("admin_pet_vaccination_history", args=[self.pet.pk]),
            fetch_redirect_response=False,
        )
        return self._messages(response)

    def _sent(self):
        return Notification.objects.filter(
            notification_type=Notification.VACCINATION_REMINDER
        ).count()

    def test_due_dose_is_sent(self):
        dose = self._dose()
        self.assertEqual(
            self._send(dose), ["Reminder sent to Ana Cruz (app and email)."]
        )
        self.assertEqual(self._sent(), 1)

    def test_no_next_date_is_refused(self):
        dose = self._dose(None)
        self.assertEqual(
            self._send(dose),
            [
                "This vaccination has no next dose date, so there is nothing "
                "to remind about."
            ],
        )
        self.assertEqual(self._sent(), 0)

    def test_superseded_dose_is_refused(self):
        dose = self._dose()
        self._dose(360, given_days=-5)
        self.assertEqual(
            self._send(dose),
            [
                "A later dose of Anti-rabies was already given, so no reminder "
                "is needed."
            ],
        )
        self.assertEqual(self._sent(), 0)

    def test_superseded_typed_vaccine_uses_the_typed_name(self):
        named = {"vaccine_name": Vaccination.OTHER, "custom_vaccine_name": "Lepto"}
        dose = self._dose(**named)
        self._dose(360, given_days=-5, **named)
        self.assertEqual(
            self._send(dose),
            ["A later dose of Lepto was already given, so no reminder is needed."],
        )
        self.assertEqual(self._sent(), 0)


class MedicalListTestBase(FrozenMedicalTestBase):
    """Two owners: Ana Cruz with Bantay (dog), Ben Santos with Muning (cat)."""

    def setUp(self):
        super().setUp()
        self.today = timezone.localdate()
        ben_user = User.objects.create_user(
            username="ben_santos", email="ben@example.com", role=User.PET_OWNER
        )
        self.ben = PetOwner.objects.create(
            user=ben_user, first_name="Ben", last_name="Santos"
        )
        self.muning = Pet.objects.create(owner=self.ben, name="Muning", species="Cat")

    def _context(self, **params):
        response = self.client.get(self.url, params)
        self.assertEqual(response.status_code, 200)
        return response.context

    def _rows(self, **params):
        return list(self._context(**params)[self.rows_key])

    def _queries(self, tab):
        """Queries run by the list helper for one tab (rows evaluated)."""
        params = QueryDict(f"tab={tab}")
        with CaptureQueriesContext(connection) as queries:
            context = self.list_context(params, self.today)
            list(context[self.rows_key])
        return len(queries)


class VaccinationListViewTests(MedicalListTestBase):
    """Batch 5a-4: vaccination_list_context (tabs, search, paging, rows)."""

    rows_key = "vaccinations"

    def setUp(self):
        super().setUp()
        self.url = reverse("admin_vaccination_list")

    def list_context(self, params, today):
        return vaccination_list_context(params, today)

    def _dose(self, due_days, given_days=-300, pet=None, **fields):
        fields.setdefault("vaccine_name", Vaccination.ANTI_RABIES)
        return Vaccination.objects.create(
            pet=pet or self.pet,
            date_administered=_days(given_days),
            next_due_date=None if due_days is None else _days(due_days),
            **fields,
        )

    def test_default_and_unknown_tab_are_due(self):
        for params in ({}, {"tab": "bogus"}, {"tab": ""}):
            with self.subTest(params=params):
                self.assertEqual(self._context(**params)["active_tab"], "due")

    def test_due_tab_soonest_first(self):
        in_5 = self._dose(5)
        today = self._dose(0, vaccine_name=Vaccination.DEWORMING)
        in_7 = self._dose(7, vaccine_name=Vaccination.BORDETELLA)
        in_7_too = self._dose(7, pet=self.muning)
        self._dose(8, vaccine_name=Vaccination.SIX_IN_ONE)
        self._dose(-1, vaccine_name=Vaccination.IVERMECTIN)
        self.assertEqual(self._rows(), [today, in_5, in_7, in_7_too])

    def test_overdue_tab_most_recently_due_first(self):
        long_ago = self._dose(-30)
        yesterday = self._dose(-1, vaccine_name=Vaccination.DEWORMING)
        yesterday_too = self._dose(-1, pet=self.muning)
        self._dose(3, vaccine_name=Vaccination.BORDETELLA)
        self.assertEqual(
            self._rows(tab="overdue"), [yesterday_too, yesterday, long_ago]
        )

    def test_all_tab_newest_given_first_with_archived_pets(self):
        oldest = self._dose(None, given_days=-10)
        older = self._dose(None, given_days=-5, vaccine_name=Vaccination.DEWORMING)
        older_too = self._dose(None, given_days=-5, pet=self.muning)
        archived = Pet.objects.create(
            owner=self.owner, name="Kulot", species="Dog", is_archived=True
        )
        newest = self._dose(None, given_days=-1, pet=archived)
        self.assertEqual(self._rows(tab="all"), [newest, older_too, older, oldest])

    def test_due_and_overdue_follow_the_due_rule(self):
        superseded = self._dose(3)
        self._dose(360, given_days=-2)
        archived = Pet.objects.create(
            owner=self.owner, name="Kulot", species="Dog", is_archived=True
        )
        self._dose(3, pet=archived)
        self._dose(-3, pet=archived)
        rows = self._rows()
        self.assertNotIn(superseded, rows)
        self.assertEqual(rows, [])
        self.assertEqual(self._rows(tab="overdue"), [])

    def test_search_each_field(self):
        bantay = self._dose(None, given_days=-3)
        muning = self._dose(None, given_days=-2, pet=self.muning)
        typed = self._dose(
            None,
            given_days=-1,
            vaccine_name=Vaccination.OTHER,
            custom_vaccine_name="Leptospirosis",
        )
        deworm = self._dose(None, given_days=-4, vaccine_name=Vaccination.DEWORMING)
        cases = {
            "bantay": [typed, bantay, deworm],
            "MUNING": [muning],
            "ben": [muning],
            "cruz": [typed, bantay, deworm],
            "lepto": [typed],
            # The vaccine's label: "rabies" finds Anti-rabies
            "rabies": [muning, bantay],
            "Anti-rabies": [muning, bantay],
            "worm": [deworm],
            "nothing": [],
        }
        for q, expected in cases.items():
            with self.subTest(q=q):
                self.assertEqual(self._rows(tab="all", q=q), expected)

    def test_other_label_is_not_searched(self):
        self._dose(
            None, vaccine_name=Vaccination.OTHER, custom_vaccine_name="Leptospirosis"
        )
        self.assertEqual(self._rows(tab="all", q="specify"), [])

    def test_every_word_must_match(self):
        bantay_rabies = self._dose(None, given_days=-3)
        self._dose(None, given_days=-2, vaccine_name=Vaccination.DEWORMING)
        self._dose(None, given_days=-1, pet=self.muning)
        self.assertEqual(self._rows(tab="all", q="bantay rabies"), [bantay_rabies])
        self.assertEqual(self._rows(tab="all", q="  ana   anti  "), [bantay_rabies])

    def test_search_stays_inside_the_tab(self):
        due = self._dose(3, pet=self.muning)
        self._dose(-3, pet=self.muning, vaccine_name=Vaccination.DEWORMING)
        self._dose(3)
        context = self._context(q="muning")
        self.assertEqual(list(context["vaccinations"]), [due])
        self.assertEqual(context["active_tab"], "due")
        self.assertEqual(context["q"], "muning")
        self.assertTrue(context["is_searching"])

    def test_old_search_param_still_works(self):
        muning = self._dose(None, pet=self.muning)
        bantay = self._dose(None)
        context = self._context(tab="all", search=" Muning ")
        self.assertEqual(list(context["vaccinations"]), [muning])
        self.assertEqual(context["q"], "Muning")
        # q wins when both are sent
        self.assertEqual(self._rows(tab="all", q="bantay", search="muning"), [bantay])

    def test_counts_ignore_the_search(self):
        self._dose(3)
        self._dose(-3, vaccine_name=Vaccination.DEWORMING)
        self._dose(5, pet=self.muning)
        self._dose(None, given_days=-1000, vaccine_name=Vaccination.BORDETELLA)
        for q in ("", "muning", "nothing"):
            with self.subTest(q=q):
                context = self._context(q=q)
                self.assertEqual(context["due_count"], 2)
                self.assertEqual(context["overdue_count"], 1)
                self.assertEqual(context["all_count"], 4)

    def test_paging_keeps_tab_and_q(self):
        for days in range(26):
            self._dose(None, given_days=-1 - days)
        context = self._context(tab="all", q="Bantay")
        self.assertEqual(len(context["vaccinations"]), 25)
        self.assertEqual(context["page_obj"].paginator.count, 26)
        self.assertEqual(context["prev_page_query"], "")
        self.assertEqual(context["next_page_query"], "?tab=all&q=Bantay&page=2")
        page_2 = self._context(tab="all", q="Bantay", page="2")
        self.assertEqual(len(page_2["vaccinations"]), 1)
        self.assertEqual(page_2["prev_page_query"], "?tab=all&q=Bantay&page=1")
        self.assertEqual(page_2["next_page_query"], "")
        # Without a search the pager keeps only the tab
        self.assertEqual(self._context(tab="all")["next_page_query"], "?tab=all&page=2")

    def test_due_state_and_days(self):
        in_3 = self._dose(3)
        today = self._dose(0, vaccine_name=Vaccination.DEWORMING)
        late = self._dose(-4, vaccine_name=Vaccination.BORDETELLA)
        later = self._dose(20, vaccine_name=Vaccination.SIX_IN_ONE)
        none = self._dose(None, vaccine_name=Vaccination.IVERMECTIN)
        archived = Pet.objects.create(
            owner=self.owner, name="Kulot", species="Dog", is_archived=True
        )
        archived_due = self._dose(3, pet=archived)
        expected = {
            in_3: ("due", 3),
            today: ("due", 0),
            late: ("overdue", 4),
            later: ("", None),
            none: ("", None),
            archived_due: ("", None),
        }
        rows = {row: row for row in self._rows(tab="all")}
        for dose, state in expected.items():
            with self.subTest(dose=dose):
                self.assertEqual((rows[dose].due_state, rows[dose].days), state)
        due_rows = self._rows()
        self.assertEqual(
            [(row.due_state, row.days) for row in due_rows], [("due", 0), ("due", 3)]
        )
        late_row = self._rows(tab="overdue")[0]
        self.assertEqual((late_row.due_state, late_row.days), ("overdue", 4))

    def test_is_superseded_on_all(self):
        old = self._dose(-3, given_days=-400)
        new = self._dose(360, given_days=-5)
        rows = {row.pk: row for row in self._rows(tab="all")}
        self.assertTrue(rows[old.pk].is_superseded)
        self.assertFalse(rows[new.pk].is_superseded)
        # A later dose given: never due, never overdue
        self.assertEqual(rows[old.pk].due_state, "")

    def test_rows_have_pet_and_owner_selected(self):
        self._dose(3)
        row = self._rows()[0]
        with self.assertNumQueries(0):
            self.assertEqual(row.pet.owner.user.username, "ana_cruz")

    def test_context_keys(self):
        self._dose(3)
        context = self._context()
        for key in (
            "page_obj",
            "vaccinations",
            "q",
            "is_searching",
            "active_tab",
            "due_count",
            "overdue_count",
            "all_count",
            "prev_page_query",
            "next_page_query",
        ):
            self.assertIn(key, context)
        self.assertEqual(context["today"], TODAY)
        # Old template's keys
        self.assertEqual(context["search_query"], "")
        self.assertEqual(context["total_count"], 1)
        self.assertFalse(context["is_searching"])

    def test_old_search_param_renders_the_page(self):
        self._dose(3, vaccine_name=Vaccination.DEWORMING)
        response = self.client.get(self.url, {"search": "bantay"})
        self.assertContains(response, "Bantay")
        self.assertContains(response, "Deworming")
        # 5b-1 template: the results line (was the old page's "1 record")
        self.assertContains(response, "1 vaccination matches")

    def test_query_count_is_flat(self):
        def make(n, due_days):
            Vaccination.objects.all().delete()
            for i in range(n):
                pet = Pet.objects.create(owner=self.ben, name=f"Pet {i}", species="Cat")
                self._dose(due_days, pet=pet)

        for tab, due_days in (("due", 3), ("overdue", -3), ("all", 3)):
            with self.subTest(tab=tab):
                make(3, due_days)
                few = self._queries(tab)
                make(30, due_days)
                many = self._queries(tab)
                self.assertEqual(len(self._rows(tab=tab)), 25)
                self.assertEqual(few, many)


class RecordListViewTests(MedicalListTestBase):
    """Batch 5a-4: record_list_context (tabs, search, paging, rows)."""

    rows_key = "records"

    def setUp(self):
        super().setUp()
        self.url = reverse("admin_medical_record_list")

    def list_context(self, params, today):
        return record_list_context(params, today)

    def _rec(self, record_days=-10, follow_days=None, pet=None, **fields):
        if follow_days is not None:
            fields.setdefault("follow_up_required", True)
            fields["follow_up_date"] = _days(follow_days)
        return MedicalRecord.objects.create(
            pet=pet or self.pet, record_date=_days(record_days), **fields
        )

    def test_default_and_unknown_tab_are_all(self):
        for params in ({}, {"tab": "bogus"}, {"tab": "due"}):
            with self.subTest(params=params):
                self.assertEqual(self._context(**params)["active_tab"], "all")

    def test_followups_tab_soonest_first(self):
        in_5 = self._rec(follow_days=5)
        today = self._rec(follow_days=0)
        in_7 = self._rec(follow_days=7)
        in_7_too = self._rec(follow_days=7, pet=self.muning)
        self._rec(follow_days=8)
        self._rec(follow_days=-1)
        self._rec(follow_days=3, follow_up_required=False)
        archived = Pet.objects.create(
            owner=self.owner, name="Kulot", species="Dog", is_archived=True
        )
        self._rec(follow_days=3, pet=archived)
        self.assertEqual(self._rows(tab="followups"), [today, in_5, in_7, in_7_too])

    def test_all_tab_newest_first(self):
        oldest = self._rec(-20)
        same_day = self._rec(-5)
        same_day_later = self._rec(-5)
        # Written later in the day, so it comes first though its pk is lower
        MedicalRecord.objects.filter(pk=same_day.pk).update(
            created_at=JUST_AFTER_MIDNIGHT + datetime.timedelta(hours=1)
        )
        archived = Pet.objects.create(
            owner=self.owner, name="Kulot", species="Dog", is_archived=True
        )
        newest = self._rec(-1, pet=archived)
        self.assertEqual(self._rows(), [newest, same_day, same_day_later, oldest])

    def test_search_each_field(self):
        bantay = self._rec(-3, diagnosis="Ear infection, left ear")
        muning = self._rec(-2, pet=self.muning, diagnosis="Upper respiratory")
        cases = {
            "bantay": [bantay],
            "muning": [muning],
            "ANA": [bantay],
            "santos": [muning],
            "ear": [bantay],
            "respiratory": [muning],
            "nothing": [],
        }
        for q, expected in cases.items():
            with self.subTest(q=q):
                self.assertEqual(self._rows(q=q), expected)

    def test_private_notes_are_never_searched(self):
        self._rec(private_notes="Owner seemed upset about parvo cost")
        self.assertEqual(self._rows(q="parvo"), [])
        self.assertEqual(self._rows(q="upset"), [])

    def test_every_word_must_match(self):
        ear = self._rec(-3, diagnosis="Ear infection")
        self._rec(-2, diagnosis="Skin rash")
        self._rec(-1, pet=self.muning, diagnosis="Ear mites")
        self.assertEqual(self._rows(q="bantay ear"), [ear])
        self.assertEqual(self._rows(q="  cruz   infection "), [ear])

    def test_search_stays_inside_the_tab(self):
        due = self._rec(follow_days=3, pet=self.muning)
        self._rec(pet=self.muning)
        self._rec(follow_days=3)
        context = self._context(tab="followups", q="muning")
        self.assertEqual(list(context["records"]), [due])
        self.assertEqual(context["active_tab"], "followups")
        self.assertTrue(context["is_searching"])

    def test_old_search_param_still_works(self):
        muning = self._rec(pet=self.muning)
        self._rec()
        context = self._context(search=" Muning ")
        self.assertEqual(list(context["records"]), [muning])
        self.assertEqual(context["q"], "Muning")

    def test_counts_ignore_the_search(self):
        self._rec(follow_days=3)
        self._rec(follow_days=5, pet=self.muning)
        self._rec(follow_days=20)
        self._rec()
        for q in ("", "muning", "nothing"):
            with self.subTest(q=q):
                context = self._context(q=q)
                self.assertEqual(context["followups_count"], 2)
                self.assertEqual(context["all_count"], 4)

    def test_paging_keeps_tab_and_q(self):
        for days in range(26):
            self._rec(-1 - days, follow_days=3)
        context = self._context(tab="followups", q="Bantay")
        self.assertEqual(len(context["records"]), 25)
        self.assertEqual(context["next_page_query"], "?tab=followups&q=Bantay&page=2")
        page_2 = self._context(tab="followups", q="Bantay", page="2")
        self.assertEqual(len(page_2["records"]), 1)
        self.assertEqual(page_2["prev_page_query"], "?tab=followups&q=Bantay&page=1")
        self.assertEqual(self._context()["next_page_query"], "?tab=all&page=2")

    def test_follow_up_days(self):
        in_3 = self._rec(-3, follow_days=3)
        past = self._rec(-2, follow_days=-2)
        not_required = self._rec(-1, follow_days=4, follow_up_required=False)
        none = self._rec(-4)
        rows = {row: row for row in self._rows()}
        self.assertEqual(rows[in_3].follow_up_days, 3)
        self.assertEqual(rows[past].follow_up_days, -2)
        self.assertIsNone(rows[not_required].follow_up_days)
        self.assertIsNone(rows[none].follow_up_days)

    def test_rows_have_pet_and_owner_selected(self):
        self._rec()
        row = self._rows()[0]
        with self.assertNumQueries(0):
            self.assertEqual(row.pet.owner.user.username, "ana_cruz")

    def test_context_keys(self):
        self._rec()
        context = self._context()
        for key in (
            "page_obj",
            "records",
            "q",
            "is_searching",
            "active_tab",
            "followups_count",
            "all_count",
            "prev_page_query",
            "next_page_query",
        ):
            self.assertIn(key, context)
        self.assertEqual(context["today"], TODAY)
        # Old template's keys
        self.assertEqual(context["search_query"], "")
        self.assertEqual(context["total_count"], 1)

    def test_old_template_still_renders(self):
        self._rec(diagnosis="Ear infection", private_notes="Secret note")
        response = self.client.get(self.url, {"search": "bantay"})
        self.assertContains(response, "Bantay")
        self.assertContains(response, "Ear infection")
        self.assertContains(response, "1 record")
        self.assertNotContains(response, "Secret note")

    def test_query_count_is_flat(self):
        def make(n):
            MedicalRecord.objects.all().delete()
            for i in range(n):
                pet = Pet.objects.create(owner=self.ben, name=f"Pet {i}", species="Cat")
                self._rec(follow_days=3, pet=pet)

        for tab in ("followups", "all"):
            with self.subTest(tab=tab):
                make(3)
                few = self._queries(tab)
                make(30)
                many = self._queries(tab)
                self.assertEqual(len(self._rows(tab=tab)), 25)
                self.assertEqual(few, many)


# ── Batch 5a-5: detail, form and reminder backend ────────────────────────────


class RecordDetailContextTests(FrozenMedicalTestBase):
    """record_detail_context: the record page's keys for 5b-2."""

    def setUp(self):
        super().setUp()
        self.record = self._record(
            record_date=_days(-2),
            follow_up_required=True,
            follow_up_date=_days(3),
        )
        self.url = reverse("admin_medical_record_detail", args=[self.record.pk])

    def _context(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        return response.context

    def test_keys_for_an_active_pet(self):
        PrescriptionItem.objects.create(record=self.record, medicine_name="Amox")
        context = self._context()
        self.assertEqual(context["pet_state"], "active")
        self.assertTrue(context["can_add"])
        # Written today (00:30 Manila Nov 3), so medicines can still go
        self.assertTrue(context["can_remove_medicines"])
        self.assertEqual([i.medicine_name for i in context["medicines"]], ["Amox"])
        self.assertEqual(context["follow_up"], {"date": _days(3), "days": 3})
        self.assertIsNone(context["last_followup_reminder"])
        self.assertTrue(context["owner_has_email"])
        self.assertEqual(context["owner_phone"], "")
        self.assertIsNone(context["open_dialog"])

    def test_old_keys_are_kept(self):
        context = self._context()
        for key in (
            "record",
            "prescription_items",
            "test_files",
            "vaccinations",
            "prescription_form",
            "file_form",
            "last_followup_reminder",
        ):
            with self.subTest(key=key):
                self.assertIn(key, context)
        # 5b-2: the old inline vaccination form is gone
        self.assertNotIn("vaccination_form", context)

    def test_archived_pet_and_owner(self):
        self.pet.is_archived = True
        self.pet.save()
        context = self._context()
        self.assertEqual(context["pet_state"], "archived")
        self.assertFalse(context["can_add"])
        self.pet.is_archived = False
        self.pet.save()
        self.owner.is_archived = True
        self.owner.save()
        context = self._context()
        self.assertEqual(context["pet_state"], "owner_archived")
        self.assertFalse(context["can_add"])

    def test_files_split_into_active_and_removed(self):
        keep = TestResultFile.objects.create(
            record=self.record, file="test_results/a.pdf"
        )
        older = TestResultFile.objects.create(
            record=self.record,
            file="test_results/b.pdf",
            is_archived=True,
            archived_at=JUST_AFTER_MIDNIGHT - datetime.timedelta(days=1),
        )
        newer = TestResultFile.objects.create(
            record=self.record,
            file="test_results/c.pdf",
            is_archived=True,
            archived_at=JUST_AFTER_MIDNIGHT,
        )
        context = self._context()
        self.assertEqual(list(context["active_files"]), [keep])
        self.assertEqual(list(context["test_files"]), [keep])
        self.assertEqual(list(context["removed_files"]), [newer, older])

    def test_vaccinations_at_this_record_only(self):
        here = Vaccination.objects.create(
            pet=self.pet,
            medical_record=self.record,
            vaccine_name=Vaccination.ANTI_RABIES,
            date_administered=_days(-2),
        )
        Vaccination.objects.create(
            pet=self.pet,
            vaccine_name=Vaccination.DEWORMING,
            date_administered=_days(-2),
        )
        self.assertEqual(list(self._context()["vaccinations"]), [here])

    def test_follow_up_none_when_not_required(self):
        MedicalRecord.objects.filter(pk=self.record.pk).update(
            follow_up_required=False, follow_up_date=None
        )
        self.assertIsNone(self._context()["follow_up"])

    def test_owner_without_email_with_phone(self):
        User.objects.filter(pk=self.owner_user.pk).update(email="")
        PetOwner.objects.filter(pk=self.owner.pk).update(contact_number="0917 123 4567")
        context = self._context()
        self.assertFalse(context["owner_has_email"])
        self.assertEqual(context["owner_phone"], "0917 123 4567")

    def test_medicines_not_removable_on_a_later_day(self):
        self.now = JUST_AFTER_MIDNIGHT + datetime.timedelta(days=1)
        self.assertFalse(self._context()["can_remove_medicines"])


class DialogErrorRenderTests(FrozenMedicalTestBase):
    """An invalid Add medicine / Add file POST re-renders the record page
    with the bound form and the dialog to open; nothing is saved."""

    def setUp(self):
        super().setUp()
        self.record = self._record()

    def test_invalid_medicine(self):
        url = reverse("admin_prescription_item_add", args=[self.record.pk])
        response = self.client.post(url, {"medicine_name": "", "dosage": "5 ml"})
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "admin/medical/record_detail.html")
        self.assertEqual(response.context["open_dialog"], "add-medicine")
        form = response.context["prescription_form"]
        self.assertTrue(form.is_bound)
        self.assertIn("medicine_name", form.errors)
        self.assertContains(response, form.errors["medicine_name"][0])
        # The typed value is kept
        self.assertContains(response, 'value="5 ml"')
        self.assertFalse(PrescriptionItem.objects.exists())
        self.assertEqual(self._messages(response), [])

    def test_invalid_file(self):
        url = reverse("admin_test_result_file_upload", args=[self.record.pk])
        upload = SimpleUploadedFile("notes.docx", b"x", "application/msword")
        response = self.client.post(url, {"file": upload, "description": "Blood"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["open_dialog"], "add-file")
        self.assertTrue(response.context["file_form"].is_bound)
        self.assertFalse(response.context["prescription_form"].is_bound)
        self.assertContains(response, "Only PDF, JPG or PNG files can be added.")
        self.assertFalse(TestResultFile.objects.exists())

    def test_valid_medicine_still_redirects(self):
        url = reverse("admin_prescription_item_add", args=[self.record.pk])
        response = self.client.post(url, {"medicine_name": "Amox"})
        self.assertRedirects(
            response,
            reverse("admin_medical_record_detail", args=[self.record.pk]),
            fetch_redirect_response=False,
        )
        self.assertEqual(self._messages(response), ["Prescription item added."])
        self.assertEqual(PrescriptionItem.objects.count(), 1)


@PLAIN_STATIC
class VaccinationFromRecordTests(FrozenMedicalTestBase):
    """admin_vaccination_standalone_create?record=<pk>."""

    def setUp(self):
        super().setUp()
        self.visit = Appointment.objects.create(
            pet=self.pet,
            owner=self.owner,
            date=_days(-5),
            time=datetime.time(9, 0),
            status=Appointment.COMPLETED,
        )
        self.record = self._record(record_date=_days(-5), appointment=self.visit)
        self.url = reverse("admin_vaccination_standalone_create", args=[self.pet.pk])

    def _post_data(self, **changes):
        data = {
            "vaccine_name": Vaccination.ANTI_RABIES,
            "custom_vaccine_name": "",
            "date_administered": _days(-5).isoformat(),
            "next_due_date": "",
        }
        data.update(changes)
        return data

    def test_get_starts_on_the_record_date(self):
        response = self.client.get(self.url, {"record": self.record.pk})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["record"], self.record)
        form = response.context["form"]
        self.assertEqual(form.initial["date_administered"], _days(-5))

    def test_get_without_a_record_starts_today(self):
        response = self.client.get(self.url)
        self.assertIsNone(response.context["record"])
        self.assertEqual(response.context["form"].initial["date_administered"], TODAY)

    def test_post_links_record_and_visit_and_goes_back_to_the_record(self):
        response = self.client.post(
            f"{self.url}?record={self.record.pk}", self._post_data()
        )
        self.assertRedirects(
            response,
            reverse("admin_medical_record_detail", args=[self.record.pk]),
            fetch_redirect_response=False,
        )
        shot = Vaccination.objects.get()
        self.assertEqual(shot.medical_record, self.record)
        self.assertEqual(shot.appointment, self.visit)
        self.assertEqual(shot.pet, self.pet)

    def test_post_field_works_too(self):
        self.client.post(self.url, self._post_data(record=self.record.pk))
        self.assertEqual(Vaccination.objects.get().medical_record, self.record)

    def test_record_of_another_pet_is_a_404(self):
        other = Pet.objects.create(owner=self.owner, name="Muning", species="Cat")
        other_record = MedicalRecord.objects.create(pet=other, record_date=TODAY)
        url = f"{self.url}?record={other_record.pk}"
        # A GET's data replaces the query string, so it carries the record
        for response in (
            self.client.get(self.url, {"record": other_record.pk}),
            self.client.post(url, self._post_data()),
            self.client.post(self.url, self._post_data(record=other_record.pk)),
        ):
            self.assertEqual(response.status_code, 404)
        self.assertFalse(Vaccination.objects.exists())

    def test_junk_record_is_a_404(self):
        for value in ("abc", "999999"):
            with self.subTest(value=value):
                response = self.client.get(self.url, {"record": value})
                self.assertEqual(response.status_code, 404)

    def test_archived_pet_is_refused_first(self):
        self.pet.is_archived = True
        self.pet.save()
        response = self.client.get(self.url, {"record": self.record.pk})
        self.assertRedirects(
            response,
            reverse("admin_pet_detail", args=[self.pet.pk]),
            fetch_redirect_response=False,
        )


@PLAIN_STATIC
class GivenByPrefillTests(FrozenMedicalTestBase):
    """administered_by starts with ClinicSettings.veterinarian_name on a new
    vaccination; edit never touches it."""

    def _clinic(self, vet):
        ClinicSettings.objects.create(
            clinic_name="Hapi Vet",
            address="Gasan",
            veterinarian_name=vet,
            opening_time=datetime.time(8, 0),
            closing_time=datetime.time(17, 0),
            same_day_cutoff_time=datetime.time(15, 0),
        )

    def _initial(self, **params):
        url = reverse("admin_vaccination_standalone_create", args=[self.pet.pk])
        return self.client.get(url, params).context["form"].initial

    def test_set(self):
        self._clinic("Dr. Edgar Sadiwa")
        self.assertEqual(self._initial()["administered_by"], "Dr. Edgar Sadiwa")
        record = self._record()
        self.assertEqual(
            self._initial(record=record.pk)["administered_by"], "Dr. Edgar Sadiwa"
        )

    def test_no_settings(self):
        self.assertEqual(self._initial()["administered_by"], "")

    def test_settings_without_a_vet_name(self):
        self._clinic("")
        self.assertEqual(self._initial()["administered_by"], "")

    def test_edit_is_untouched(self):
        self._clinic("Dr. Edgar Sadiwa")
        shot = Vaccination.objects.create(
            pet=self.pet,
            vaccine_name=Vaccination.ANTI_RABIES,
            date_administered=_days(-3),
            administered_by="Dr. Reyes",
        )
        response = self.client.get(reverse("admin_vaccination_edit", args=[shot.pk]))
        self.assertEqual(
            response.context["form"]["administered_by"].value(), "Dr. Reyes"
        )
        blank = Vaccination.objects.create(
            pet=self.pet,
            vaccine_name=Vaccination.DEWORMING,
            date_administered=_days(-3),
        )
        response = self.client.get(reverse("admin_vaccination_edit", args=[blank.pk]))
        self.assertEqual(response.context["form"]["administered_by"].value(), "")


class HighlightTests(FrozenMedicalTestBase):
    """?highlight=<pk> on the pet's vaccinations, and the redirects to it."""

    def setUp(self):
        super().setUp()
        self.shot = Vaccination.objects.create(
            pet=self.pet,
            vaccine_name=Vaccination.ANTI_RABIES,
            date_administered=_days(-3),
        )
        self.url = reverse("admin_pet_vaccination_history", args=[self.pet.pk])

    def _highlight(self, value):
        response = self.client.get(self.url, {"highlight": value})
        self.assertEqual(response.status_code, 200)
        return response.context["highlight_id"]

    def test_own_dose(self):
        self.assertEqual(self._highlight(self.shot.pk), self.shot.pk)

    def test_other_pets_dose_is_ignored(self):
        other = Pet.objects.create(owner=self.owner, name="Muning", species="Cat")
        theirs = Vaccination.objects.create(
            pet=other, vaccine_name=Vaccination.DEWORMING, date_administered=_days(-3)
        )
        self.assertIsNone(self._highlight(theirs.pk))

    def test_junk_is_ignored(self):
        for value in ("abc", "", "-1", "1.5", "999999"):
            with self.subTest(value=value):
                self.assertIsNone(self._highlight(value))

    def test_no_param(self):
        self.assertIsNone(self.client.get(self.url).context["highlight_id"])

    def test_redirect_after_a_new_dose(self):
        url = reverse("admin_vaccination_standalone_create", args=[self.pet.pk])
        response = self.client.post(
            url,
            {
                "vaccine_name": Vaccination.DEWORMING,
                "date_administered": TODAY.isoformat(),
            },
        )
        new = Vaccination.objects.latest("pk")
        self.assertRedirects(
            response,
            f"{self.url}?highlight={new.pk}#vaccination-{new.pk}",
            fetch_redirect_response=False,
        )

    def test_redirect_after_an_edit(self):
        edit = reverse("admin_vaccination_edit", args=[self.shot.pk])
        response = self.client.post(
            edit,
            {
                "vaccine_name": Vaccination.ANTI_RABIES,
                "date_administered": _days(-3).isoformat(),
                "batch_number": "B2",
            },
        )
        self.assertRedirects(
            response,
            f"{self.url}?highlight={self.shot.pk}#vaccination-{self.shot.pk}",
            fetch_redirect_response=False,
        )

    def test_edit_ignores_next_and_goes_to_the_history(self):
        # 5b-5: ?next=record is gone (no page sent it any more); was
        # test_edit_with_next_record_still_goes_to_the_record
        record = self._record()
        Vaccination.objects.filter(pk=self.shot.pk).update(medical_record=record)
        edit = reverse("admin_vaccination_edit", args=[self.shot.pk])
        for query in ("?next=record", "?next=history", ""):
            with self.subTest(query=query):
                response = self.client.post(
                    f"{edit}{query}",
                    {
                        "vaccine_name": Vaccination.ANTI_RABIES,
                        "date_administered": _days(-3).isoformat(),
                        "next": "record",
                    },
                )
                self.assertRedirects(
                    response,
                    f"{self.url}?highlight={self.shot.pk}#vaccination-{self.shot.pk}",
                    fetch_redirect_response=False,
                )


class ReminderFlashTests(FrozenMedicalTestBase):
    """The flash says how the reminder reached the owner (notify()'s
    email_outcome); messages use a friendly date with the year."""

    def setUp(self):
        super().setUp()
        self.dose = Vaccination.objects.create(
            pet=self.pet,
            vaccine_name=Vaccination.ANTI_RABIES,
            date_administered=_days(-300),
            next_due_date=_days(2),
        )
        self.record = self._record(
            record_date=_days(-1), follow_up_required=True, follow_up_date=_days(2)
        )
        self.urls = [
            reverse("admin_trigger_vaccination_reminder", args=[self.dose.pk]),
            reverse("admin_trigger_followup_reminder", args=[self.record.pk]),
        ]

    def _flashes(self):
        result = []
        for url in self.urls:
            self.client.cookies.pop("messages", None)
            response = self.client.post(url)
            result.append(
                [(m.level_tag, str(m)) for m in get_messages(response.wsgi_request)]
            )
        return result

    def _check(self, level, text):
        for flash in self._flashes():
            self.assertEqual(flash, [(level, text)])

    def test_app_and_email(self):
        self._check("success", "Reminder sent to Ana Cruz (app and email).")
        self.assertEqual(len(mail.outbox), 2)

    def test_no_email_on_file(self):
        User.objects.filter(pk=self.owner_user.pk).update(email="")
        self._check(
            "success", "Reminder sent to Ana Cruz in the app only (no email on file)."
        )
        self.assertEqual(len(mail.outbox), 0)

    def test_email_reminders_off(self):
        NotificationPreference.objects.filter(user=self.owner_user).update(
            email_vaccination=False, email_followup=False
        )
        self._check(
            "success",
            "Reminder sent to Ana Cruz in the app only "
            "(their email reminders are off).",
        )
        self.assertEqual(len(mail.outbox), 0)

    def test_email_failed(self):
        with mock.patch(
            "notifications.utils.send_mail", side_effect=Exception("Brevo down")
        ):
            self._check(
                "warning",
                "Reminder sent to Ana Cruz in the app. The email couldn't be sent.",
            )
        self.assertEqual(Notification.objects.filter(email_failed=True).count(), 2)

    def test_friendly_date_in_the_messages(self):
        self._flashes()
        vaccination = Notification.objects.get(
            notification_type=Notification.VACCINATION_REMINDER
        )
        followup = Notification.objects.get(
            notification_type=Notification.FOLLOWUP_REMINDER
        )
        self.assertIn("is due on Thursday, November 5, 2026.", vaccination.message)
        self.assertIn("scheduled for Thursday, November 5, 2026.", followup.message)
        self.assertNotIn("November 05", vaccination.message + followup.message)

    def test_record_created_message_uses_a_friendly_date(self):
        url = reverse("admin_medical_record_create", args=[self.pet.pk])
        self.client.post(url, {"record_date": _days(-1).isoformat()})
        created = Notification.objects.get(title="Medical Record Created")
        self.assertIn("on Monday, November 2, 2026.", created.message)


class LastReminderTests(FrozenMedicalTestBase):
    """The newest reminder of a kind for a pet (per pet and kind)."""

    def _notice(self, pet, kind, when):
        notice = Notification.objects.create(
            recipient=pet.owner.user,
            notification_type=kind,
            title="Reminder",
            message="Reminder",
            related_pet=pet,
        )
        # created_at is auto_now_add; set it afterwards
        Notification.objects.filter(pk=notice.pk).update(created_at=when)
        return when

    def test_newest_of_the_kind_for_the_pet(self):
        other = Pet.objects.create(owner=self.owner, name="Muning", species="Cat")
        vacc = Notification.VACCINATION_REMINDER
        old = JUST_AFTER_MIDNIGHT - datetime.timedelta(days=5)
        new = JUST_AFTER_MIDNIGHT - datetime.timedelta(days=1)
        self._notice(self.pet, vacc, old)
        self._notice(self.pet, vacc, new)
        # Another kind and another pet don't count
        self._notice(self.pet, Notification.FOLLOWUP_REMINDER, JUST_AFTER_MIDNIGHT)
        self._notice(other, vacc, JUST_AFTER_MIDNIGHT)
        self.assertEqual(last_reminder(self.pet, vacc), new)
        self.assertEqual(
            last_reminders(vacc, [self.pet.pk, other.pk]),
            {self.pet.pk: new, other.pk: JUST_AFTER_MIDNIGHT},
        )

    def test_none_when_never_sent(self):
        self.assertIsNone(last_reminder(self.pet, Notification.FOLLOWUP_REMINDER))

    def test_pages_show_it(self):
        when = self._notice(
            self.pet, Notification.FOLLOWUP_REMINDER, JUST_AFTER_MIDNIGHT
        )
        record = self._record(follow_up_required=True, follow_up_date=_days(3))
        for url in (
            reverse("admin_medical_record_detail", args=[record.pk]),
            reverse("admin_pet_medical_history", args=[self.pet.pk]),
        ):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.context["last_followup_reminder"], when)
        vacc_when = self._notice(
            self.pet, Notification.VACCINATION_REMINDER, JUST_AFTER_MIDNIGHT
        )
        response = self.client.get(
            reverse("admin_pet_vaccination_history", args=[self.pet.pk])
        )
        self.assertEqual(response.context["last_vaccination_reminder"], vacc_when)
        # 5b-5: the page shows it in the reminder dialog of a remindable dose
        # (was the old template's "November 3, 2026, 12:30 AM" line)
        Vaccination.objects.create(
            pet=self.pet,
            vaccine_name=Vaccination.ANTI_RABIES,
            date_administered=_days(-300),
            next_due_date=_days(2),
        )
        response = self.client.get(
            reverse("admin_pet_vaccination_history", args=[self.pet.pk])
        )
        self.assertContains(
            response, "Last vaccination reminder for Bantay: Today.", count=1
        )

    def test_list_rows(self):
        vacc_when = self._notice(
            self.pet,
            Notification.VACCINATION_REMINDER,
            JUST_AFTER_MIDNIGHT - datetime.timedelta(hours=3),
        )
        follow_when = self._notice(
            self.pet, Notification.FOLLOWUP_REMINDER, JUST_AFTER_MIDNIGHT
        )
        Vaccination.objects.create(
            pet=self.pet,
            vaccine_name=Vaccination.ANTI_RABIES,
            date_administered=_days(-300),
            next_due_date=_days(2),
        )
        self._record(follow_up_required=True, follow_up_date=_days(2))
        today = timezone.localdate()
        rows = vaccination_list_context(QueryDict("tab=due"), today)["vaccinations"]
        self.assertEqual([v.last_reminder for v in rows], [vacc_when])
        rows = record_list_context(QueryDict("tab=followups"), today)["records"]
        self.assertEqual([r.last_reminder for r in rows], [follow_when])


class HistoryContextTests(FrozenMedicalTestBase):
    """The pet's medical history and vaccinations pages (5b-5 context)."""

    def _get(self, name):
        response = self.client.get(reverse(name, args=[self.pet.pk]))
        self.assertEqual(response.status_code, 200)
        return response.context

    def _dose(self, given, due=None, **fields):
        fields.setdefault("vaccine_name", Vaccination.ANTI_RABIES)
        return Vaccination.objects.create(
            pet=self.pet,
            date_administered=_days(given),
            next_due_date=None if due is None else _days(due),
            **fields,
        )

    def test_medical_history(self):
        busy = self._record(
            record_date=_days(-1), follow_up_required=True, follow_up_date=_days(5)
        )
        PrescriptionItem.objects.create(record=busy, medicine_name="Amox")
        PrescriptionItem.objects.create(record=busy, medicine_name="Doxy")
        TestResultFile.objects.create(record=busy, file="test_results/a.pdf")
        TestResultFile.objects.create(
            record=busy, file="test_results/b.pdf", is_archived=True
        )
        self._dose(-1, medical_record=busy)
        soon = self._record(
            record_date=_days(-3), follow_up_required=True, follow_up_date=_days(2)
        )
        context = self._get("admin_pet_medical_history")
        self.assertEqual(context["pet_state"], "active")
        self.assertTrue(context["can_add"])
        self.assertEqual(context["records_count"], 2)
        self.assertEqual(context["vaccinations_count"], 1)
        rows = list(context["records"])
        self.assertEqual(rows, [busy, soon])
        self.assertEqual(
            [
                (r.medicine_count, r.active_file_count, r.vaccination_count)
                for r in rows
            ],
            [(2, 1, 1), (0, 0, 0)],
        )
        self.assertEqual(context["next_follow_up"], soon)
        self.assertIsNone(context["last_followup_reminder"])

    def test_next_follow_up_beyond_the_week(self):
        # 5b-5: the banner takes any follow-up dated today or later (was
        # test_no_next_follow_up_outside_the_week: none 20 days out, when it
        # was limited to the 7-day window); past ones still don't count
        self._record(follow_up_required=True, follow_up_date=_days(-1))
        far = self._record(follow_up_required=True, follow_up_date=_days(20))
        self.assertEqual(self._get("admin_pet_medical_history")["next_follow_up"], far)

    def test_archived_pet(self):
        self.pet.is_archived = True
        self.pet.save()
        self._record(
            record_date=_days(-1), follow_up_required=True, follow_up_date=_days(2)
        )
        for name in ("admin_pet_medical_history", "admin_pet_vaccination_history"):
            with self.subTest(name=name):
                context = self._get(name)
                self.assertEqual(context["pet_state"], "archived")
                self.assertFalse(context["can_add"])
        self.assertIsNone(self._get("admin_pet_medical_history")["next_follow_up"])

    def test_vaccination_history(self):
        superseded = self._dose(-400, due=-35)
        overdue = self._dose(-35, due=-3, vaccine_name=Vaccination.DEWORMING)
        due = self._dose(-30, due=3)
        later = self._dose(-20, due=60, vaccine_name=Vaccination.SIX_IN_ONE)
        no_date = self._dose(-10, vaccine_name=Vaccination.BORDETELLA)
        PetOwner.objects.filter(pk=self.owner.pk).update(contact_number="0917 123 4567")
        context = self._get("admin_pet_vaccination_history")
        self.assertEqual(context["records_count"], 0)
        self.assertEqual(context["vaccinations_count"], 5)
        self.assertEqual(
            [v.pk for v in context["vaccinations"]],
            [no_date.pk, later.pk, due.pk, overdue.pk, superseded.pk],
        )
        rows = {v.pk: v for v in context["vaccinations"]}
        expected = {
            superseded.pk: (True, "", None, False),
            overdue.pk: (False, "overdue", 3, True),
            due.pk: (False, "due", 3, True),
            later.pk: (False, "", None, False),
            no_date.pk: (False, "", None, False),
        }
        for pk, values in expected.items():
            row = rows[pk]
            with self.subTest(pk=pk):
                self.assertEqual(
                    (row.is_superseded, row.due_state, row.days, row.can_remind),
                    values,
                )
        self.assertIsNone(context["highlight_id"])
        self.assertIsNone(context["last_vaccination_reminder"])
        self.assertTrue(context["owner_has_email"])
        self.assertEqual(context["owner_phone"], "0917 123 4567")

    def test_no_reminders_for_an_archived_owner(self):
        self._dose(-30, due=3)
        self.owner.is_archived = True
        self.owner.save()
        context = self._get("admin_pet_vaccination_history")
        self.assertEqual([v.can_remind for v in context["vaccinations"]], [False])

    def _make(self, n):
        MedicalRecord.objects.all().delete()
        Vaccination.objects.all().delete()
        for i in range(n):
            record = self._record(record_date=_days(-i - 1))
            PrescriptionItem.objects.create(record=record, medicine_name="Amox")
            TestResultFile.objects.create(record=record, file="test_results/a.pdf")
            self._dose(-i - 1, due=3, medical_record=record)

    def _helper_queries(self, helper, rows_key):
        """Queries run by the context helper (rows evaluated)."""
        pet = Pet.objects.select_related("owner__user").get(pk=self.pet.pk)
        with CaptureQueriesContext(connection) as queries:
            list(helper(pet, timezone.localdate())[rows_key])
        return len(queries)

    def test_helper_query_counts_are_flat(self):
        for helper, rows_key, expected in (
            (medical_history_context, "records", 4),
            (vaccination_history_context, "vaccinations", 3),
        ):
            with self.subTest(helper=helper.__name__):
                self._make(3)
                few = self._helper_queries(helper, rows_key)
                self._make(30)
                many = self._helper_queries(helper, rows_key)
                self.assertEqual(few, many)
                self.assertEqual(many, expected)

    def test_page_query_counts_are_flat(self):
        for name in ("admin_pet_medical_history", "admin_pet_vaccination_history"):
            with self.subTest(name=name):
                url = reverse(name, args=[self.pet.pk])
                self._make(3)
                with CaptureQueriesContext(connection) as few:
                    self.client.get(url)
                self._make(30)
                with CaptureQueriesContext(connection) as many:
                    self.client.get(url)
                self.assertEqual(len(few), len(many))


# ── Batch 5b-1: Vaccinations and Medical records lists ───────────────────────


class FriendlyDateOnTagTests(SimpleTestCase):
    """{% friendly_date_on %} / {% long_date_on %}: the date filters with
    the view's today passed in, never the clock."""

    def _render(self, source, **context):
        return Template("{% load friendly_dates %}" + source).render(Context(context))

    def test_follows_the_given_today(self):
        day = datetime.date(2026, 11, 4)
        for today, expected in (
            (datetime.date(2026, 11, 3), "Tomorrow"),
            (datetime.date(2026, 11, 4), "Today"),
            (datetime.date(2025, 1, 1), "Wed, Nov 4, 2026"),
        ):
            with self.subTest(today=today):
                rendered = self._render(
                    "{% friendly_date_on day today %}", day=day, today=today
                )
                self.assertEqual(rendered, expected)
        self.assertEqual(
            self._render('{% long_date_on day today "mid" %}', day=day, today=day),
            "today, Wednesday, November 4",
        )

    def test_a_datetime_uses_its_manila_date(self):
        # 23:30 UTC on Nov 2 is 07:30 Manila on Nov 3
        when = datetime.datetime(2026, 11, 2, 23, 30, tzinfo=datetime.timezone.utc)
        self.assertEqual(
            self._render("{% friendly_date_on when today %}", when=when, today=TODAY),
            "Today",
        )

    def test_no_value_is_empty(self):
        self.assertEqual(
            self._render("{% friendly_date_on none today %}", none=None, today=TODAY),
            "",
        )
        self.assertEqual(
            self._render("{% long_date_on none today %}", none=None, today=TODAY), ""
        )


class ReminderReturnToTests(FrozenMedicalTestBase):
    """return_to on both Send reminder actions: a fixed choice, never a URL
    from the request; "list" keeps the (re-validated) tab and the q."""

    def setUp(self):
        super().setUp()
        self.dose = Vaccination.objects.create(
            pet=self.pet,
            vaccine_name=Vaccination.ANTI_RABIES,
            date_administered=_days(-300),
            next_due_date=_days(2),
        )
        self.record = self._record(
            record_date=_days(-1), follow_up_required=True, follow_up_date=_days(2)
        )
        pet_pk, record_pk = self.pet.pk, self.record.pk
        self.vacc_url = reverse(
            "admin_trigger_vaccination_reminder", args=[self.dose.pk]
        )
        self.follow_url = reverse("admin_trigger_followup_reminder", args=[record_pk])
        self.vacc_history = reverse("admin_pet_vaccination_history", args=[pet_pk])
        self.med_history = reverse("admin_pet_medical_history", args=[pet_pk])
        self.record_page = reverse("admin_medical_record_detail", args=[record_pk])

    def _goes_to(self, url, data, expected):
        self.client.cookies.pop("messages", None)
        response = self.client.post(url, data)
        self.assertRedirects(response, expected, fetch_redirect_response=False)
        return response

    def _list(self, name, **query):
        return f"{reverse(name)}?{urlencode(query)}"

    def test_vaccination_history_is_the_default(self):
        for data in ({}, {"return_to": "history"}):
            with self.subTest(data=data):
                self._goes_to(self.vacc_url, data, self.vacc_history)

    def test_vaccination_list_keeps_tab_and_q(self):
        self._goes_to(
            self.vacc_url,
            {"return_to": "list", "tab": "overdue", "q": "  ana cruz "},
            self._list("admin_vaccination_list", tab="overdue", q="ana cruz"),
        )
        self._goes_to(
            self.vacc_url,
            {"return_to": "list", "tab": "all"},
            self._list("admin_vaccination_list", tab="all"),
        )

    def test_vaccination_list_tab_is_revalidated(self):
        for tab in ("followups", "bogus", "", None):
            with self.subTest(tab=tab):
                data = {"return_to": "list", "q": "ana"}
                if tab is not None:
                    data["tab"] = tab
                self._goes_to(
                    self.vacc_url,
                    data,
                    self._list("admin_vaccination_list", tab="due", q="ana"),
                )

    def test_q_is_urlencoded_never_pasted(self):
        q = "a&b=c <x>#frag"
        response = self._goes_to(
            self.vacc_url,
            {"return_to": "list", "tab": "due", "q": q},
            self._list("admin_vaccination_list", tab="due", q=q),
        )
        self.assertIn("q=a%26b%3Dc+%3Cx%3E%23frag", response["Location"])

    def test_vaccination_junk_return_to_goes_to_history(self):
        for junk in ("record", "LIST", "https://example.com/", "/admin/", ""):
            with self.subTest(junk=junk):
                self._goes_to(
                    self.vacc_url, {"return_to": junk, "tab": "all"}, self.vacc_history
                )

    def test_vaccination_refusal_goes_back_to_the_list(self):
        Vaccination.objects.filter(pk=self.dose.pk).update(next_due_date=None)
        response = self._goes_to(
            self.vacc_url,
            {"return_to": "list", "tab": "due"},
            self._list("admin_vaccination_list", tab="due"),
        )
        self.assertEqual(
            self._messages(response),
            [
                "This vaccination has no next dose date, so there is nothing "
                "to remind about."
            ],
        )

    def test_followup_history_is_the_default(self):
        for data in ({}, {"return_to": "history"}):
            with self.subTest(data=data):
                self._goes_to(self.follow_url, data, self.med_history)

    def test_followup_record(self):
        self._goes_to(self.follow_url, {"return_to": "record"}, self.record_page)

    def test_followup_list_keeps_tab_and_q(self):
        self._goes_to(
            self.follow_url,
            {"return_to": "list", "tab": "followups", "q": " bantay "},
            self._list("admin_medical_record_list", tab="followups", q="bantay"),
        )
        for tab in ("due", "overdue", "bogus"):
            with self.subTest(tab=tab):
                self._goes_to(
                    self.follow_url,
                    {"return_to": "list", "tab": tab},
                    self._list("admin_medical_record_list", tab="all"),
                )

    def test_followup_junk_return_to_goes_to_history(self):
        for junk in ("RECORD", "https://example.com/", "/admin/", "pet", ""):
            with self.subTest(junk=junk):
                self._goes_to(self.follow_url, {"return_to": junk}, self.med_history)

    def test_record_page_posts_return_to_record(self):
        html = self.client.get(self.record_page).content.decode()
        self.assertIn('<input type="hidden" name="return_to" value="record">', html)

    def test_flash_shows_once_on_the_list(self):
        for url, data, expected in (
            (
                self.vacc_url,
                {"return_to": "list", "tab": "due", "q": "bantay"},
                self._list("admin_vaccination_list", tab="due", q="bantay"),
            ),
            (
                self.follow_url,
                {"return_to": "list", "tab": "followups"},
                self._list("admin_medical_record_list", tab="followups"),
            ),
        ):
            with self.subTest(url=url):
                self.client.cookies.pop("messages", None)
                response = self.client.post(url, data, follow=True)
                self.assertEqual(response.redirect_chain, [(expected, 302)])
                html = response.content.decode()
                self.assertEqual(
                    html.count("Reminder sent to Ana Cruz (app and email)."), 1
                )
                self.assertEqual(html.count("<div data-flash"), 1)
                self.assertIn("Bantay", html)


def _outside_dialogs(html):
    """The page without its <dialog>s (their confirm buttons are primary)."""
    return re.sub(r"(?s)<dialog\b.*?</dialog>", "", html)


def _dialogs(html):
    return re.findall(r"(?s)<dialog\b.*?</dialog>", html)


def _empty_state(html):
    """(title, text) of the page's empty state, or None."""
    match = re.search(
        r'<p class="text-lg font-semibold text-gray-900">(.*?)</p>\s*'
        r'<p class="mt-1 max-w-sm text-base text-gray-600">(.*?)</p>',
        html,
    )
    return match.groups() if match else None


def _pills(html):
    """[(background class, text)] of every pill in the rows."""
    return re.findall(
        r"(?s)rounded-full (bg-\w+-100) px-2\.5 py-1 text-sm font-medium "
        r'text-\w+-\d+">\s*<svg.*?</svg>\s*(.*?)</span>',
        html,
    )


ACTIVE_NAV = (
    'class="flex min-h-11 items-center gap-3 rounded-lg px-3 text-base bg-white '
    'font-semibold text-violet-800 shadow-sm ring-1 ring-violet-200" '
    'aria-current="page">'
)
PLAIN_NAV = (
    'class="flex min-h-11 items-center gap-3 rounded-lg px-3 text-base '
    'text-gray-800 hover:bg-violet-200/70">'
)
LIST_ROW = '<li class="flex flex-col gap-3'


class MedicalListPageTestBase(MedicalListTestBase):
    """Shared page checks for both lists (self.url, self.search_id,
    self.label, self.placeholder, self.tabs_label set by the subclass)."""

    def _html(self, **params):
        response = self.client.get(self.url, params)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def _tabs(self, html):
        return re.search(
            rf'(?s)<nav [^>]*aria-label="{self.tabs_label}">.*?</nav>', html
        ).group()

    def _check_search_forms(self, tab, q="cruz"):
        html = self._html(tab=tab, q=q)
        self.assertEqual(html.count('role="search"'), 2)
        for prefix, form_class in (
            ("topbar", "hidden min-w-0 flex-1 items-center gap-3 md:flex"),
            ("page", "mb-4 flex items-center gap-3 md:hidden"),
        ):
            with self.subTest(prefix=prefix):
                self.assertIn(
                    f'<form method="get" action="{self.url}" class="{form_class}" '
                    'role="search">',
                    html,
                )
                self.assertIn(
                    f'<label for="{prefix}-{self.search_id}" class="sr-only">'
                    f"{self.label}</label>",
                    html,
                )
                self.assertIn(
                    f'<input id="{prefix}-{self.search_id}" type="search" name="q" '
                    f'value="{q}" placeholder="{self.placeholder}" '
                    'class="input pl-10">',
                    html,
                )
        self.assertEqual(
            html.count(f'<input type="hidden" name="tab" value="{tab}">'), 2
        )
        self.assertNotIn("Find a pet or owner", html)

    def _check_one_line_links(self, params_list):
        for params in params_list:
            with self.subTest(params=params):
                for tag in re.findall(r"<a\b[^>]*>", self._html(**params)):
                    self.assertNotIn("\n", tag)

    def _page_queries(self, **params):
        """Queries for the full page (after a warm-up request)."""
        self.client.get(self.url, params)
        with CaptureQueriesContext(connection) as queries:
            self.client.get(self.url, params)
        return len(queries)


class VaccinationListPageTests(MedicalListPageTestBase):
    """admin/medical/vaccination_list.html on the new layout (Slice 5b-1)."""

    tabs_label = "Vaccinations sections"
    search_id = "vaccination-search"
    label = "Search vaccinations"
    placeholder = "Search vaccinations: pet, owner or vaccine"

    def setUp(self):
        super().setUp()
        self.url = reverse("admin_vaccination_list")

    def _dose(self, due_days, given_days=-300, pet=None, **fields):
        fields.setdefault("vaccine_name", Vaccination.ANTI_RABIES)
        return Vaccination.objects.create(
            pet=pet or self.pet,
            date_administered=_days(given_days),
            next_due_date=None if due_days is None else _days(due_days),
            **fields,
        )

    def _many(self, count, due_days):
        for i in range(count):
            pet = Pet.objects.create(owner=self.ben, name=f"Pet{i:02d}", species="Cat")
            self._dose(due_days, pet=pet)

    # ─── Layout, header, sidebar ──────────────────────────────────────────────

    def test_new_layout_without_old_files(self):
        self._dose(3)
        response = self.client.get(self.url)
        self.assertTemplateUsed(response, "layouts/admin.html")
        for old in (
            "vaccination_list.css",
            "vaccination_list.js",
            "vaccination_history.css",
            "layer(legacy)",
            "admin-page",
            "admin-table",
        ):
            with self.subTest(old=old):
                self.assertNotContains(response, old)
        self.assertContains(response, "<title>Vaccinations — Hapi Vet Admin</title>")
        self.assertContains(
            response, '<h1 class="text-2xl font-bold text-gray-900">Vaccinations</h1>'
        )
        self.assertContains(response, "Every vaccination given at the clinic.")

    def test_sidebar_highlights_vaccinations(self):
        html = self._html()
        self.assertIn(f'<a href="{self.url}" {ACTIVE_NAV}', html)
        records_url = reverse("admin_medical_record_list")
        self.assertIn(f'<a href="{records_url}" {PLAIN_NAV}', html)

    def test_no_primary_button_outside_dialogs(self):
        self._dose(3)
        self._dose(-3, vaccine_name=Vaccination.DEWORMING)
        for params in ({}, {"tab": "overdue"}, {"tab": "all"}, {"q": "zzz"}):
            with self.subTest(params=params):
                html = self._html(**params)
                self.assertEqual(_outside_dialogs(html).count("btn-primary"), 0)

    def test_search_forms_with_the_hidden_tab(self):
        for tab in ("due", "overdue", "all"):
            with self.subTest(tab=tab):
                self._check_search_forms(tab)

    # ─── Tabs ─────────────────────────────────────────────────────────────────

    def test_tabs_counts_and_current(self):
        self._dose(3)
        self._dose(-3, vaccine_name=Vaccination.DEWORMING)
        self._dose(None, vaccine_name=Vaccination.BORDETELLA)
        for tab in ("due", "overdue", "all"):
            with self.subTest(tab=tab):
                tabs = self._tabs(self._html(tab=tab))
                self.assertEqual(tabs.count('aria-current="page"'), 1)
                self.assertRegex(
                    tabs,
                    rf'<a href="{self.url}\?tab={tab}" class="[^"]*" '
                    r'aria-current="page">',
                )
        tabs = self._tabs(self._html())
        self.assertIn(
            'aria-current="page">Due in 7 days<span class="rounded-full '
            'bg-yellow-200 text-yellow-900 px-2 text-sm font-semibold">1</span></a>',
            tabs,
        )
        self.assertIn(
            '">Overdue<span class="rounded-full bg-red-100 text-red-800 px-2 text-sm '
            'font-semibold">1</span></a>',
            tabs,
        )
        self.assertIn(
            '">All<span class="rounded-full bg-gray-100 text-gray-800 px-2 text-sm '
            'font-semibold">3</span></a>',
            tabs,
        )

    def test_yellow_and_red_counts_only_above_zero(self):
        self._dose(None)
        tabs = self._tabs(self._html())
        self.assertNotIn("bg-yellow-200", tabs)
        self.assertNotIn("bg-red-100", tabs)
        self.assertIn(
            'aria-current="page">Due in 7 days<span class="rounded-full '
            'bg-violet-100 text-violet-800 px-2 text-sm font-semibold">0</span>',
            tabs,
        )
        self.assertIn(
            '">Overdue<span class="rounded-full bg-gray-100 text-gray-800 px-2 '
            'text-sm font-semibold">0</span>',
            tabs,
        )

    def test_tab_links_keep_q_and_drop_page(self):
        self._many(30, None)
        tabs = self._tabs(self._html(tab="all", q="pet one", page=2))
        for tab in ("due", "overdue", "all"):
            with self.subTest(tab=tab):
                self.assertIn(f'href="{self.url}?tab={tab}&amp;q=pet%20one"', tabs)
        self.assertNotIn("page=", tabs)

    # ─── Heads, note, results line ────────────────────────────────────────────

    def test_list_heads(self):
        self._dose(3)
        self._dose(-3, vaccine_name=Vaccination.DEWORMING)
        due = self._html()
        self.assertIn(
            'Due in the next 7 days <span class="font-normal text-gray-600">(1)'
            "</span></h2>",
            due,
        )
        self.assertIn(
            '<p class="text-sm text-gray-600">Soonest first · Today to Tue, Nov 10</p>',
            due,
        )
        overdue = self._html(tab="overdue")
        self.assertIn(
            'Overdue <span class="font-normal text-gray-600">(1)</span></h2>', overdue
        )
        self.assertIn("Most recently due first</p>", overdue)
        all_tab = self._html(tab="all")
        self.assertIn(
            'All vaccinations <span class="font-normal text-gray-600">(2)</span></h2>',
            all_tab,
        )
        self.assertIn("Newest first</p>", all_tab)
        self.assertNotIn("Clear search", all_tab)

    def test_overdue_note_only_on_overdue(self):
        note = (
            "Next dose date has passed and no later dose of the same vaccine was "
            "given. A pet drops off this list as soon as the next dose is recorded."
        )
        self._dose(3)
        self.assertNotIn(note, self._html(tab="overdue"))  # nothing overdue
        self._dose(-3, vaccine_name=Vaccination.DEWORMING)
        self.assertIn(note, self._html(tab="overdue"))
        self.assertNotIn(note, self._html())
        self.assertNotIn(note, self._html(tab="all"))

    def test_results_line_and_clear_search(self):
        self._dose(None, given_days=-3)
        self._dose(None, given_days=-2, vaccine_name=Vaccination.DEWORMING)
        self._dose(None, given_days=-1, pet=self.muning)
        many = self._html(tab="all", q="cruz")
        self.assertIn(
            '2 vaccinations match <strong class="font-semibold">“cruz”</strong> in '
            "all vaccinations</h2>",
            many,
        )
        self.assertNotIn("All vaccinations <span", many)
        self.assertIn(
            f'<a href="{self.url}?tab=all" class="inline-flex min-h-11 items-center '
            'gap-2 text-base font-medium text-blue-600 underline underline-offset-2">',
            many,
        )
        self.assertIn("Clear search</a>", many)
        self.assertIn(
            '1 vaccination matches <strong class="font-semibold">“muning”</strong> '
            "in all vaccinations</h2>",
            self._html(tab="all", q="muning"),
        )
        self.assertIn(
            'No vaccination matches <strong class="font-semibold">“zzz”</strong> in '
            "overdue vaccinations</h2>",
            self._html(tab="overdue", q="zzz"),
        )
        due = self._html(q="zzz")
        self.assertIn("in vaccinations due in the next 7 days</h2>", due)
        self.assertIn(f'<a href="{self.url}?tab=due" class="inline-flex', due)

    # ─── Rows and pills ───────────────────────────────────────────────────────

    def test_row_halves(self):
        self._dose(2, given_days=-300)
        self._dose(
            None,
            given_days=-1,
            pet=self.muning,
            vaccine_name=Vaccination.OTHER,
            custom_vaccine_name="Leptospirosis",
        )
        PetOwner.objects.filter(pk=self.owner.pk).update(contact_number="0917 123 4567")
        html = self._html(tab="all")
        self.assertEqual(html.count(LIST_ROW), 2)
        self.assertIn(
            '<li class="flex flex-col gap-3 border-t border-gray-100 p-4 '
            "first:border-t-0 hover:bg-gray-50 md:grid "
            "md:grid-cols-[minmax(0,1.25fr)_1px_minmax(0,0.9fr)_auto] md:items-center "
            'md:gap-5 md:px-5">',
            html,
        )
        self.assertRegex(
            html,
            r'(?s)Bantay <span class="font-normal text-gray-500">·</span> <span '
            r'class="inline-flex items-center gap-1.5">\s*<svg[^>]*>.*?</svg>\s*'
            r"Anti-rabies</span></p>",
        )
        self.assertIn("Leptospirosis</span></p>", html)
        self.assertIn(
            '<p class="text-base text-gray-700">Given Wed, Jan 7 · Next due Thu, Nov 5'
            "</p>",
            html,
        )
        self.assertIn(
            '<p class="text-base text-gray-700">Given Yesterday · <span '
            'class="text-gray-600">No next date</span></p>',
            html,
        )
        self.assertIn('<p class="text-sm text-gray-600">Owner</p>', html)
        for owner in (self.owner, self.ben):
            with self.subTest(owner=owner.first_name):
                self.assertIn(
                    f'<a href="{reverse("admin_owner_detail", args=[owner.pk])}" '
                    'class="font-medium text-blue-600 underline underline-offset-2">'
                    f"{owner.full_name}</a>",
                    html,
                )
        self.assertIn('<span class="sr-only">Phone: </span>0917 123 4567</p>', html)
        self.assertIn(
            '<span class="sr-only">Phone: </span><span class="text-gray-600">'
            "No phone number</span></p>",
            html,
        )
        self.assertIn(
            "rounded-lg bg-gray-50 px-3 py-2 md:rounded-none md:bg-transparent md:p-0",
            html,
        )

    def test_due_pill_texts(self):
        self._dose(0)
        self._dose(1, vaccine_name=Vaccination.DEWORMING)
        self._dose(6, vaccine_name=Vaccination.BORDETELLA, is_corrected=True)
        self.assertEqual(
            _pills(self._html()),
            [
                ("bg-yellow-100", "Due today"),
                ("bg-yellow-100", "Due tomorrow"),
                ("bg-yellow-100", "In 6 days"),
                ("bg-gray-100", "Corrected"),
            ],
        )

    def test_overdue_pill_texts(self):
        self._dose(-1)
        self._dose(-3, vaccine_name=Vaccination.DEWORMING)
        self.assertEqual(
            _pills(self._html(tab="overdue")),
            [("bg-red-100", "1 day overdue"), ("bg-red-100", "3 days overdue")],
        )

    def test_frozen_just_after_midnight_manila(self):
        # 00:30 Manila on Tue, Nov 3 is still Nov 2 in UTC: the page follows
        # the view's today (Manila), not the UTC date
        self._dose(0)
        html = self._html()
        self.assertEqual(_pills(html), [("bg-yellow-100", "Due today")])
        self.assertIn("Next due Today</p>", html)
        self.assertIn("Soonest first · Today to Tue, Nov 10</p>", html)

    def test_later_dose_given_only_on_all(self):
        self._dose(-3, given_days=-400)
        self._dose(2, given_days=-5)
        self.assertEqual(
            _pills(self._html(tab="all")),
            [("bg-yellow-100", "In 2 days"), ("bg-gray-100", "Later dose given")],
        )
        for tab in ("due", "overdue"):
            with self.subTest(tab=tab):
                self.assertNotIn("Later dose given", self._html(tab=tab))

    # ─── Send reminder and the dialogs ────────────────────────────────────────

    def test_send_reminder_only_on_due_and_overdue(self):
        due = self._dose(3)
        late = self._dose(-3, vaccine_name=Vaccination.DEWORMING)
        self._dose(None, vaccine_name=Vaccination.BORDETELLA)
        for tab, dose in (("due", due), ("overdue", late)):
            with self.subTest(tab=tab):
                html = self._html(tab=tab)
                self.assertEqual(html.count("data-dialog-open="), 1)
                self.assertIn(
                    '<button type="button" class="btn btn-outline flex-1 font-semibold '
                    f'md:flex-none" data-dialog-open="reminder-dialog-{dose.pk}">',
                    html,
                )
                self.assertIn(
                    '<span class="md:hidden">Remind</span><span class="hidden '
                    'md:inline">Send reminder</span><span class="sr-only"> to Ana '
                    "Cruz about Bantay</span></button>",
                    html,
                )
                dialogs = _dialogs(html)
                self.assertEqual(len(dialogs), 1)
                self.assertIn(f'<dialog id="reminder-dialog-{dose.pk}" ', dialogs[0])
        html = self._html(tab="all")
        self.assertNotIn("data-dialog-open=", html)
        self.assertNotIn("Send reminder", html)
        self.assertEqual(_dialogs(html), [])

    def test_dialog_posts_return_to_list_with_tab_and_q(self):
        dose = self._dose(2)
        dialog = _dialogs(self._html(q="bantay"))[0]
        url = reverse("admin_trigger_vaccination_reminder", args=[dose.pk])
        self.assertIn(f'<form method="post" action="{url}" class="p-6 sm:p-7">', dialog)
        self.assertIn('name="csrfmiddlewaretoken"', dialog)
        self.assertIn('<input type="hidden" name="return_to" value="list">', dialog)
        self.assertIn('<input type="hidden" name="tab" value="due">', dialog)
        self.assertIn('<input type="hidden" name="q" value="bantay">', dialog)
        self.assertRegex(
            dialog,
            r'(?s)<button type="submit" class="btn btn-primary [^"]*">\s*<svg[^>]*>'
            r".*?</svg>\s*Yes, send reminder\s*</button>",
        )
        self.assertIn("data-dialog-close autofocus>Go back</button>", dialog)
        # Without a search there is no q field
        self.assertNotIn('name="q"', _dialogs(self._html())[0])

    def test_dialog_text_due(self):
        dose = self._dose(2)
        dialog = _dialogs(self._html())[0]
        self.assertIn(
            f'id="reminder-dialog-{dose.pk}-title" class="text-xl font-bold">Send a '
            "reminder to Ana Cruz?</h2>",
            dialog,
        )
        self.assertIn(
            "Bantay's <b>Anti-rabies</b> is due on <b>Thursday, November 5</b>. The "
            "reminder asks Ana Cruz to book a visit.",
            dialog,
        )
        self.assertIn("No vaccination reminder sent for Bantay yet.</span>", dialog)
        self.assertIn(
            "Ana Cruz gets a notification in the app, and an email if they have one "
            "and vaccination emails are on.",
            dialog,
        )
        self.assertNotIn("bg-yellow-50", dialog)

    def test_dialog_text_due_today_and_overdue(self):
        self._dose(0)
        self.assertIn(
            "is due on <b>today, Tuesday, November 3</b>.", _dialogs(self._html())[0]
        )
        self._dose(-2, vaccine_name=Vaccination.DEWORMING)
        self._dose(-1, vaccine_name=Vaccination.BORDETELLA)
        dialogs = _dialogs(self._html(tab="overdue"))
        self.assertIn(
            "Bantay's <b>Bordetella</b> was due on <b>Monday, November 2</b> (1 day "
            "ago).",
            dialogs[0],
        )
        self.assertIn(
            "Bantay's <b>Deworming</b> was due on <b>Sunday, November 1</b> (2 days "
            "ago).",
            dialogs[1],
        )

    def test_dialog_last_reminder_line(self):
        self._dose(2)
        notice = Notification.objects.create(
            recipient=self.owner_user,
            notification_type=Notification.VACCINATION_REMINDER,
            title="Reminder",
            message="Reminder",
            related_pet=self.pet,
        )
        Notification.objects.filter(pk=notice.pk).update(
            created_at=JUST_AFTER_MIDNIGHT - datetime.timedelta(days=1)
        )
        # A follow-up reminder doesn't count
        Notification.objects.create(
            recipient=self.owner_user,
            notification_type=Notification.FOLLOWUP_REMINDER,
            title="Reminder",
            message="Reminder",
            related_pet=self.pet,
        )
        dialog = _dialogs(self._html())[0]
        self.assertIn("Last vaccination reminder for Bantay: Yesterday.</span>", dialog)

    def test_no_email_box(self):
        self._dose(2)
        User.objects.filter(pk=self.owner_user.pk).update(email="")
        dialog = _dialogs(self._html())[0]
        self.assertIn(
            "<p><b>Ana Cruz has no email and no phone number on file.</b> The "
            "reminder only shows in the app, the next time they log in.</p>",
            dialog,
        )
        self.assertNotIn("gets a notification in the app", dialog)
        PetOwner.objects.filter(pk=self.owner.pk).update(contact_number="0917 123 4567")
        dialog = _dialogs(self._html())[0]
        self.assertIn(
            "<p><b>Ana Cruz has no email on file.</b> The reminder only shows in the "
            "app, the next time they log in. To reach them sooner, call "
            "0917 123 4567.</p>",
            dialog,
        )

    def test_open_link_highlights_the_dose(self):
        dose = self._dose(2)
        history = reverse("admin_pet_vaccination_history", args=[self.pet.pk])
        self.assertRegex(
            self._html(),
            rf'<a href="{history}\?highlight={dose.pk}#vaccination-{dose.pk}" '
            r'class="inline-flex min-h-11 flex-1 [^"]*border-violet-300 bg-violet-50'
            r'[^"]*md:flex-none">Open<span class="sr-only"> Bantay\'s Anti-rabies'
            r"</span>\s*<svg",
        )

    # ─── Empty states, pager, queries, links ──────────────────────────────────

    def test_empty_states(self):
        for tab in ("due", "overdue", "all"):
            with self.subTest(tab=tab):
                self.assertEqual(
                    _empty_state(self._html(tab=tab)),
                    (
                        "No vaccinations yet",
                        "Vaccinations appear here when you add them from a pet's "
                        "page or a medical record.",
                    ),
                )
        self._dose(None)
        due = self._html()
        self.assertEqual(
            _empty_state(due),
            (
                "Nothing due in the next 7 days",
                "No pet needs a vaccination from today to Tue, Nov 10.",
            ),
        )
        self.assertIn(f'<a href="{self.url}?tab=all" class="inline-flex', due)
        self.assertIn("See all vaccinations", due)
        self.assertEqual(
            _empty_state(self._html(tab="overdue")),
            ("Nothing overdue", "No pet has a next dose date that has passed."),
        )
        self.assertEqual(
            _empty_state(self._html(tab="all", q="zzz")),
            (
                "No vaccination matches “zzz”",
                "Check the spelling, or look in another tab.",
            ),
        )
        self.assertIsNone(_empty_state(self._html(tab="all")))

    def test_pager_on_30_rows(self):
        self._many(30, None)
        html = self._html(tab="all")
        self.assertEqual(html.count(LIST_ROW), 25)
        self.assertIn("Showing 1–25 of 30", html)
        self.assertIn('<a href="?tab=all&amp;page=2" class="btn btn-outline">', html)
        second = self._html(tab="all", page=2)
        self.assertEqual(second.count(LIST_ROW), 5)
        self.assertIn("Showing 26–30 of 30", second)

    def test_query_count_is_flat(self):
        """The full page, reminder dialogs included, costs the same with 3
        rows as with 30, on every tab."""
        for tab, due_days in (("due", 3), ("overdue", -3), ("all", 3)):
            with self.subTest(tab=tab):
                Vaccination.objects.all().delete()
                self._many(3, due_days)
                few = self._page_queries(tab=tab)
                Vaccination.objects.all().delete()
                self._many(30, due_days)
                many = self._page_queries(tab=tab)
                self.assertEqual(few, many)
                self.assertEqual(many, 10)

    def test_every_link_is_on_one_line(self):
        self._many(30, 3)
        self._dose(-3)
        self._check_one_line_links(
            [{}, {"tab": "overdue"}, {"tab": "all"}, {"q": "pet"}, {"q": "zzz"}]
        )


class RecordListPageTests(MedicalListPageTestBase):
    """admin/medical/record_list.html on the new layout (Slice 5b-1)."""

    tabs_label = "Medical records sections"
    search_id = "record-search"
    label = "Search medical records"
    placeholder = "Search records: pet, owner or diagnosis"

    def setUp(self):
        super().setUp()
        self.url = reverse("admin_medical_record_list")

    def _rec(self, record_days=-10, follow_days=None, pet=None, **fields):
        if follow_days is not None:
            fields.setdefault("follow_up_required", True)
            fields["follow_up_date"] = _days(follow_days)
        return MedicalRecord.objects.create(
            pet=pet or self.pet, record_date=_days(record_days), **fields
        )

    def _many(self, count, follow_days=3):
        for i in range(count):
            pet = Pet.objects.create(owner=self.ben, name=f"Pet{i:02d}", species="Cat")
            self._rec(follow_days=follow_days, pet=pet, diagnosis="Ear infection")

    # ─── Layout, header, sidebar ──────────────────────────────────────────────

    def test_new_layout_without_old_files(self):
        self._rec()
        response = self.client.get(self.url)
        self.assertTemplateUsed(response, "layouts/admin.html")
        for old in (
            "record_list.css",
            "record_list.js",
            "record_detail.css",
            "layer(legacy)",
            "admin-page",
            "admin-table",
        ):
            with self.subTest(old=old):
                self.assertNotContains(response, old)
        self.assertContains(response, "<title>Medical records — Hapi Vet Admin</title>")
        self.assertContains(
            response,
            '<h1 class="text-2xl font-bold text-gray-900">Medical records</h1>',
        )
        self.assertContains(response, "Every visit record written at the clinic.")

    def test_sidebar_highlights_medical_records(self):
        html = self._html()
        self.assertIn(f'<a href="{self.url}" {ACTIVE_NAV}', html)
        vaccinations_url = reverse("admin_vaccination_list")
        self.assertIn(f'<a href="{vaccinations_url}" {PLAIN_NAV}', html)

    def test_no_primary_button_outside_dialogs(self):
        self._rec(follow_days=3)
        for params in ({}, {"tab": "followups"}, {"q": "zzz"}):
            with self.subTest(params=params):
                html = self._html(**params)
                self.assertEqual(_outside_dialogs(html).count("btn-primary"), 0)

    def test_search_forms_with_the_hidden_tab(self):
        for tab in ("followups", "all"):
            with self.subTest(tab=tab):
                self._check_search_forms(tab)

    # ─── Tabs ─────────────────────────────────────────────────────────────────

    def test_tabs_counts_and_current(self):
        self._rec(follow_days=3)
        self._rec()
        for tab in ("followups", "all"):
            with self.subTest(tab=tab):
                tabs = self._tabs(self._html(tab=tab))
                self.assertEqual(tabs.count('aria-current="page"'), 1)
                self.assertRegex(
                    tabs,
                    rf'<a href="{self.url}\?tab={tab}" class="[^"]*" '
                    r'aria-current="page">',
                )
        tabs = self._tabs(self._html())
        self.assertIn(
            '">Follow-ups in 7 days<span class="rounded-full bg-yellow-200 '
            'text-yellow-900 px-2 text-sm font-semibold">1</span></a>',
            tabs,
        )
        self.assertIn(
            'aria-current="page">All<span class="rounded-full bg-violet-100 '
            'text-violet-800 px-2 text-sm font-semibold">2</span></a>',
            tabs,
        )

    def test_yellow_count_only_above_zero(self):
        self._rec()
        tabs = self._tabs(self._html())
        self.assertNotIn("bg-yellow-200", tabs)
        self.assertIn(
            '">Follow-ups in 7 days<span class="rounded-full bg-gray-100 '
            'text-gray-800 px-2 text-sm font-semibold">0</span>',
            tabs,
        )

    def test_tab_links_keep_q_and_drop_page(self):
        self._many(30)
        tabs = self._tabs(self._html(q="pet one", page=2))
        for tab in ("followups", "all"):
            with self.subTest(tab=tab):
                self.assertIn(f'href="{self.url}?tab={tab}&amp;q=pet%20one"', tabs)
        self.assertNotIn("page=", tabs)

    # ─── Heads, results line ──────────────────────────────────────────────────

    def test_list_heads(self):
        self._rec(follow_days=3)
        self._rec()
        follow = self._html(tab="followups")
        self.assertIn(
            'Follow-ups in the next 7 days <span class="font-normal text-gray-600">'
            "(1)</span></h2>",
            follow,
        )
        self.assertIn(
            '<p class="text-sm text-gray-600">Soonest first · Today to Tue, Nov 10</p>',
            follow,
        )
        all_tab = self._html()
        self.assertIn(
            'All medical records <span class="font-normal text-gray-600">(2)</span>'
            "</h2>",
            all_tab,
        )
        self.assertIn("Newest visit first</p>", all_tab)
        self.assertNotIn("Clear search", all_tab)

    def test_results_line_and_clear_search(self):
        self._rec(-3, diagnosis="Ear infection")
        self._rec(-2, diagnosis="Skin rash")
        self._rec(-1, diagnosis="Ear mites")
        self._rec(-1, pet=self.muning, diagnosis="Cough")
        html = self._html(q="cruz")
        self.assertIn(
            '3 records match <strong class="font-semibold">“cruz”</strong> in all '
            "medical records</h2>",
            html,
        )
        self.assertNotIn("All medical records <span", html)
        self.assertIn(
            f'<a href="{self.url}?tab=all" class="inline-flex min-h-11 items-center '
            'gap-2 text-base font-medium text-blue-600 underline underline-offset-2">',
            html,
        )
        self.assertIn(
            '1 record matches <strong class="font-semibold">“cough”</strong>',
            self._html(q="cough"),
        )
        follow = self._html(tab="followups", q="zzz")
        self.assertIn(
            'No record matches <strong class="font-semibold">“zzz”</strong> in '
            "follow-ups in the next 7 days</h2>",
            follow,
        )
        self.assertIn(f'<a href="{self.url}?tab=followups" class="inline-flex', follow)

    # ─── Rows and pills ───────────────────────────────────────────────────────

    def test_row(self):
        long = "Upper respiratory infection. Sneezing for 3 days, eating less " * 3
        self._rec(-1, diagnosis=long.strip())
        self._rec(-8, pet=self.muning)
        html = self._html()
        self.assertIn(
            '<li class="flex flex-col gap-3 border-t border-gray-100 p-4 '
            "first:border-t-0 hover:bg-gray-50 md:grid "
            "md:grid-cols-[7.5rem_minmax(0,1.25fr)_1px_minmax(0,0.9fr)_auto] "
            'md:items-center md:gap-5 md:px-5">',
            html,
        )
        self.assertRegex(
            html,
            r'<p class="text-sm text-gray-600">Visit date</p>\s*'
            r'<p class="text-base font-semibold text-gray-900">Yesterday</p>',
        )
        self.assertIn(
            '<p class="text-base font-semibold text-gray-900">Mon, Oct 26</p>', html
        )
        self.assertIn('<p class="text-lg font-semibold text-gray-900">Bantay</p>', html)
        self.assertIn(
            f'<p class="truncate text-base text-gray-800">{long.strip()}</p>', html
        )
        self.assertIn(
            '<p class="text-base text-gray-600">No diagnosis written</p>', html
        )
        self.assertIn(
            f'<a href="{reverse("admin_owner_detail", args=[self.ben.pk])}" '
            'class="font-medium text-blue-600 underline underline-offset-2">'
            "Ben Santos</a>",
            html,
        )
        self.assertEqual(_pills(html), [])

    def test_pills_on_all(self):
        self._rec(-1, follow_days=2)
        self._rec(-2, follow_days=0)
        self._rec(-3, follow_days=20, follow_up_required=False)
        self.assertEqual(
            _pills(self._html()),
            [
                ("bg-blue-100", "Follow-up Thu, Nov 5"),
                ("bg-blue-100", "Follow-up today"),
            ],
        )

    def test_pills_on_followups(self):
        self._rec(-1, follow_days=0)
        self._rec(-2, follow_days=1)
        self._rec(-3, follow_days=6)
        self.assertEqual(
            _pills(self._html(tab="followups")),
            [
                ("bg-yellow-100", "Follow-up today"),
                ("bg-yellow-100", "Follow-up tomorrow"),
                ("bg-yellow-100", "Follow-up in 6 days · Mon, Nov 9"),
            ],
        )

    def test_private_notes_never_on_the_page(self):
        self._rec(follow_days=3, private_notes="Owner upset about the parvo cost")
        for tab in ("followups", "all"):
            with self.subTest(tab=tab):
                self.assertNotIn("parvo", self._html(tab=tab))

    # ─── Send reminder and the dialogs ────────────────────────────────────────

    def test_send_reminder_only_on_followups(self):
        record = self._rec(-1, follow_days=2)
        self._rec(-2, follow_days=20)
        html = self._html(tab="followups")
        self.assertEqual(html.count("data-dialog-open="), 1)
        self.assertIn(
            '<button type="button" class="btn btn-outline flex-1 font-semibold '
            f'md:flex-none" data-dialog-open="reminder-dialog-{record.pk}">',
            html,
        )
        self.assertIn(
            '<span class="sr-only"> to Ana Cruz about Bantay\'s follow-up</span>'
            "</button>",
            html,
        )
        dialogs = _dialogs(html)
        self.assertEqual(len(dialogs), 1)
        self.assertIn(f'<dialog id="reminder-dialog-{record.pk}" ', dialogs[0])
        all_tab = self._html()
        self.assertNotIn("data-dialog-open=", all_tab)
        self.assertNotIn("Send reminder", all_tab)
        self.assertEqual(_dialogs(all_tab), [])

    def test_dialog(self):
        record = self._rec(-1, follow_days=2)
        dialog = _dialogs(self._html(tab="followups", q="ana"))[0]
        url = reverse("admin_trigger_followup_reminder", args=[record.pk])
        self.assertIn(f'<form method="post" action="{url}" class="p-6 sm:p-7">', dialog)
        self.assertIn('name="csrfmiddlewaretoken"', dialog)
        self.assertIn('<input type="hidden" name="return_to" value="list">', dialog)
        self.assertIn('<input type="hidden" name="tab" value="followups">', dialog)
        self.assertIn('<input type="hidden" name="q" value="ana">', dialog)
        self.assertIn('font-bold">Send a follow-up reminder to Ana Cruz?</h2>', dialog)
        self.assertIn(
            "Bantay's follow-up for the visit on yesterday is on <b>Thursday, "
            "November 5</b>. The reminder asks Ana Cruz to book it.",
            dialog,
        )
        self.assertIn("No follow-up reminder sent for Bantay yet.</span>", dialog)
        self.assertIn("and follow-up emails are on.", dialog)
        self.assertRegex(
            dialog,
            r'(?s)<button type="submit" class="btn btn-primary [^"]*">\s*<svg[^>]*>'
            r".*?</svg>\s*Yes, send reminder\s*</button>",
        )

    def test_dialog_last_reminder_line(self):
        self._rec(-1, follow_days=2)
        notice = Notification.objects.create(
            recipient=self.owner_user,
            notification_type=Notification.FOLLOWUP_REMINDER,
            title="Reminder",
            message="Reminder",
            related_pet=self.pet,
        )
        Notification.objects.filter(pk=notice.pk).update(
            created_at=JUST_AFTER_MIDNIGHT - datetime.timedelta(days=3)
        )
        dialog = _dialogs(self._html(tab="followups"))[0]
        self.assertIn("Last follow-up reminder for Bantay: Sat, Oct 31.</span>", dialog)

    def test_no_email_box(self):
        self._rec(-1, follow_days=2)
        User.objects.filter(pk=self.owner_user.pk).update(email="")
        dialog = _dialogs(self._html(tab="followups"))[0]
        self.assertIn(
            "<b>Ana Cruz has no email and no phone number on file.</b>", dialog
        )
        PetOwner.objects.filter(pk=self.owner.pk).update(contact_number="0917 123 4567")
        dialog = _dialogs(self._html(tab="followups"))[0]
        self.assertIn("<b>Ana Cruz has no email on file.</b>", dialog)
        self.assertIn("To reach them sooner, call 0917 123 4567.", dialog)

    def test_open_link(self):
        record = self._rec(-1)
        self.assertRegex(
            self._html(),
            rf'<a href="{reverse("admin_medical_record_detail", args=[record.pk])}" '
            r'class="inline-flex min-h-11 flex-1 [^"]*border-violet-300 bg-violet-50'
            r'[^"]*md:flex-none">Open<span class="sr-only"> Bantay\'s record from '
            r"yesterday</span>\s*<svg",
        )

    # ─── Empty states, pager, queries, links ──────────────────────────────────

    def test_empty_states(self):
        for tab in ("followups", "all"):
            with self.subTest(tab=tab):
                self.assertEqual(
                    _empty_state(self._html(tab=tab)),
                    (
                        "No medical records yet",
                        "Records appear here when you write one from a visit or a "
                        "pet's page.",
                    ),
                )
        self._rec()
        follow = self._html(tab="followups")
        self.assertEqual(
            _empty_state(follow),
            (
                "No follow-ups in the next 7 days",
                "No pet has a follow-up visit from today to Tue, Nov 10.",
            ),
        )
        self.assertIn(f'<a href="{self.url}?tab=all" class="inline-flex', follow)
        self.assertIn("See all medical records", follow)
        self.assertEqual(
            _empty_state(self._html(q="zzz")),
            (
                "No record matches “zzz”",
                "Check the spelling, or look in the other tab.",
            ),
        )
        self.assertIsNone(_empty_state(self._html()))

    def test_pager_on_30_rows(self):
        self._many(30)
        html = self._html(tab="followups")
        self.assertEqual(html.count(LIST_ROW), 25)
        self.assertIn("Showing 1–25 of 30", html)
        self.assertIn(
            '<a href="?tab=followups&amp;page=2" class="btn btn-outline">', html
        )
        second = self._html(tab="followups", page=2)
        self.assertEqual(second.count(LIST_ROW), 5)
        self.assertIn("Showing 26–30 of 30", second)

    def test_query_count_is_flat(self):
        """The full page, reminder dialogs included, costs the same with 3
        rows as with 30, on both tabs."""
        for tab in ("followups", "all"):
            with self.subTest(tab=tab):
                MedicalRecord.objects.all().delete()
                self._many(3)
                few = self._page_queries(tab=tab)
                MedicalRecord.objects.all().delete()
                self._many(30)
                many = self._page_queries(tab=tab)
                self.assertEqual(few, many)
                self.assertEqual(many, 10)

    def test_every_link_is_on_one_line(self):
        self._many(30)
        self._rec()
        self._check_one_line_links(
            [{}, {"tab": "followups"}, {"q": "pet"}, {"q": "zzz"}]
        )


# ── Batch 5b-2: Medical record detail page ───────────────────────────────────


def _dialog(html, dialog_id):
    """The <dialog> with this id, or None."""
    for dialog in _dialogs(html):
        if dialog.startswith(f'<dialog id="{dialog_id}" '):
            return dialog
    return None


class RecordDetailPageTests(FrozenMedicalTestBase):
    """admin/medical/record_detail.html on the new layout (Slice 5b-2).
    Frozen at 00:30 Manila on Tue, Nov 3, 2026; the record is written then,
    so medicines can be removed until the day ends."""

    def setUp(self):
        super().setUp()
        self.record = self._record()
        self.url = reverse("admin_medical_record_detail", args=[self.record.pk])

    def _html(self, url=None):
        response = self.client.get(url or self.url)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def _medicine(self, name="Amoxicillin", **fields):
        return PrescriptionItem.objects.create(
            record=self.record, medicine_name=name, **fields
        )

    def _file(self, name="test_results/scan.pdf", **fields):
        return TestResultFile.objects.create(record=self.record, file=name, **fields)

    def _follow(self, days):
        """Follow-up in `days` days (negative: past), on a visit 10 days ago."""
        MedicalRecord.objects.filter(pk=self.record.pk).update(
            record_date=_days(-10),
            follow_up_required=True,
            follow_up_date=_days(days),
        )

    def _dose(self, **fields):
        fields.setdefault("vaccine_name", Vaccination.ANTI_RABIES)
        fields.setdefault("date_administered", TODAY)
        return Vaccination.objects.create(
            pet=self.pet, medical_record=self.record, **fields
        )

    def _everything(self):
        """A medicine, a file and a follow-up: every button the page can show."""
        item = self._medicine()
        test_file = self._file(description="Blood test")
        self._follow(3)
        return item, test_file

    # ─── Layout ───────────────────────────────────────────────────────────────

    def test_new_layout_without_old_files(self):
        response = self.client.get(self.url)
        self.assertTemplateUsed(response, "layouts/admin.html")
        self.assertTemplateUsed(
            response, "admin/medical/record_detail_parts/header.html"
        )
        for old in (
            "record_detail.css",
            "layer(legacy)",
            "admin-page",
            "detail-layout",
            "data-table",
            "Add Vaccination Record",
            "Send Follow-up Reminder Now",
        ):
            with self.subTest(old=old):
                self.assertNotContains(response, old)
        self.assertContains(
            response, "<title>Bantay's visit record — Hapi Vet Admin</title>"
        )
        self.assertContains(response, "js/admin/photo_input")

    def test_one_violet_button_outside_dialogs(self):
        self._everything()
        edit = reverse("admin_medical_record_edit", args=[self.record.pk])
        for archived in (False, True):
            with self.subTest(archived=archived):
                Pet.objects.filter(pk=self.pet.pk).update(is_archived=archived)
                page = _outside_dialogs(self._html())
                self.assertEqual(page.count("btn-primary"), 1)
                self.assertIn(
                    f'<a href="{edit}" class="btn btn-primary min-h-12 w-full '
                    'font-semibold">',
                    page,
                )

    def test_phone_order_without_copies(self):
        self._follow(3)
        html = self._html()
        self.assertIn(
            '<div class="contents lg:sticky lg:top-24 lg:col-start-2 lg:row-start-1 '
            'lg:block lg:space-y-6">',
            html,
        )
        for marker in (
            '<section class="card order-1" aria-labelledby="actions-title">',
            '<section class="order-2 rounded-xl border-2 border-yellow-300',
            '<div class="order-3 min-w-0 space-y-6 lg:order-none',
            '<section class="card order-4" aria-labelledby="owner-title">',
            '<section class="order-5 rounded-xl',
        ):
            with self.subTest(marker=marker):
                self.assertEqual(html.count(marker), 1)
        self.assertEqual(html.count('id="notes-title"'), 1)

    # ─── Header ───────────────────────────────────────────────────────────────

    def test_header_links(self):
        html = self._html()
        history = reverse("admin_pet_medical_history", args=[self.pet.pk])
        self.assertRegex(
            html,
            rf'(?s)<a href="{history}" class="[^"]*">\s*<svg[^>]*>.*?</svg>\s*'
            r"Back to Bantay's medical history</a>",
        )
        self.assertIn("<span>Visit record · Today, Tuesday, November 3</span>", html)
        owner = reverse("admin_owner_detail", args=[self.owner.pk])
        self.assertIn(
            '<b class="font-semibold">Bantay</b> · Dog · Owner: '
            f'<a href="{owner}" class="font-medium text-blue-600 underline '
            'underline-offset-2">Ana Cruz</a>',
            html,
        )
        self.assertIn(
            '<p class="mt-1 text-base text-gray-600">Not linked to a visit</p>', html
        )
        self.assertNotIn("Pet archived", html)

    def test_header_visit_link(self):
        visit = Appointment.objects.create(
            pet=self.pet,
            owner=self.owner,
            date=_days(-1),
            time=datetime.time(10, 0),
            status=Appointment.COMPLETED,
        )
        MedicalRecord.objects.filter(pk=self.record.pk).update(
            appointment=visit, record_date=_days(-1)
        )
        html = self._html()
        visit_url = reverse("admin_appointment_detail", args=[visit.pk])
        self.assertIn(
            f'From the visit <a href="{visit_url}" '
            'class="inline-flex min-h-11 items-center font-medium text-blue-600 '
            'underline underline-offset-2">Yesterday at 10:00 AM</a>',
            html,
        )
        self.assertNotIn("Not linked to a visit", html)
        self.assertIn("Visit record · Monday, November 2", html)

    # ─── Notes ────────────────────────────────────────────────────────────────

    def test_owner_note_box_and_private_notes_card(self):
        MedicalRecord.objects.filter(pk=self.record.pk).update(
            symptoms="Shaking head",
            public_notes="Clean the ear gently.",
            private_notes="Possibly yeast too.",
        )
        html = self._html()
        self.assertIn("Note for the owner · Ana can see this</p>", html)
        self.assertIn(
            '<p class="mt-1 whitespace-pre-line text-base text-gray-900">'
            "Clean the ear gently.</p>",
            html,
        )
        self.assertIn(
            '<section class="rounded-xl border-2 border-amber-300 bg-amber-50 p-6 '
            'shadow-sm" aria-labelledby="private-title">',
            html,
        )
        self.assertIn("Only the clinic sees this. It is never shown to Ana Cruz.", html)
        self.assertIn(
            '<p class="mt-3 whitespace-pre-line text-base text-gray-900">'
            "Possibly yeast too.</p>",
            html,
        )
        self.assertIn(
            '<dd class="mt-1 whitespace-pre-line text-base text-gray-900">'
            "Shaking head</dd>",
            html,
        )

    def test_every_empty_state_line(self):
        User.objects.filter(pk=self.owner_user.pk).update(email="")
        html = self._html()
        self.assertEqual(
            html.count('<dd class="mt-1 text-base text-gray-600">Not written</dd>'), 3
        )
        for line in (
            '<p class="mt-1 text-base text-gray-600">No note for the owner.</p>',
            '<p class="mt-3 text-base text-gray-600">No private notes.</p>',
            '<p class="text-base text-gray-600">No medicines on this record.</p>',
            '<p class="text-base text-gray-600">No test results added.</p>',
            '<p class="text-base text-gray-600">No vaccinations recorded at this '
            "visit.</p>",
            '<p class="mt-2 text-base text-gray-700">No follow-up needed.</p>',
            '<p class="mt-1 text-sm text-gray-700">To set one, use Edit record.</p>',
            "No phone number</p>",
            "No email</p>",
            "Not linked to a visit",
        ):
            with self.subTest(line=line):
                self.assertEqual(html.count(line), 1)
        for absent in ("Removed files", "can only be removed", "Last changed"):
            with self.subTest(absent=absent):
                self.assertNotIn(absent, html)
        self.assertIn("Medicines (0)", html)
        self.assertIn("Test results (0)", html)
        self.assertIn("Vaccinations at this visit (0)", html)

    # ─── Medicines ────────────────────────────────────────────────────────────

    def test_medicine_row(self):
        item = self._medicine(dosage="1 tablet", notes="Give with food")
        html = self._html()
        self.assertIn("Medicines (1)", html)
        self.assertIn(
            '<p class="text-lg font-semibold text-gray-900">Amoxicillin</p>', html
        )
        self.assertIn(
            '<dt class="text-sm text-gray-600">Dose</dt><dd>1 tablet</dd>', html
        )
        self.assertEqual(html.count('<span class="text-gray-600">Not given</span>'), 2)
        self.assertIn("Give with food</p>", html)
        self.assertIn(f'data-dialog-open="remove-medicine-{item.pk}"', html)

    def test_remove_only_on_the_same_day(self):
        item = self._medicine()
        html = self._html()
        self.assertRegex(
            html,
            r'(?s)<button type="button" class="btn border border-red-300 bg-white '
            r'font-semibold text-red-700 hover:bg-red-50" '
            rf'data-dialog-open="remove-medicine-{item.pk}" aria-haspopup="dialog">'
            r'\s*<svg.*?</svg>\s*Remove<span class="sr-only"> Amoxicillin</span>'
            r"</button>",
        )
        self.assertNotIn("can only be removed on the day", html)
        self.assertIsNotNone(_dialog(html, f"remove-medicine-{item.pk}"))

        # The next morning: no Remove, the gray line instead
        self.now = JUST_AFTER_MIDNIGHT + datetime.timedelta(days=1)
        html = self._html()
        self.assertNotIn("remove-medicine-", html)
        self.assertIn(
            "<span>Medicines can only be removed on the day the record was written. "
            "To change one now, add the right medicine and explain it in Treatment "
            "given.</span>",
            html,
        )

    # ─── Test results ─────────────────────────────────────────────────────────

    def test_active_file_rows(self):
        blood = self._file(description="Blood test")
        self._file(name="test_results/xray.jpeg")
        html = self._html()
        self.assertIn("Test results (2)", html)
        self.assertIn(
            "PDF, JPG or PNG, up to 10 MB. The owner only sees that a result is on "
            "file.</p>",
            html,
        )
        self.assertIn('font-semibold text-gray-900">Blood test</p>', html)
        self.assertIn(
            '<p class="text-sm text-gray-700">PDF · Added today, 12:30 AM</p>', html
        )
        self.assertIn('font-semibold text-gray-900">xray.jpeg</p>', html)
        self.assertIn(
            '<p class="text-sm text-gray-700">JPG · Added today, 12:30 AM</p>', html
        )
        self.assertRegex(
            html,
            rf'<a href="{re.escape(blood.file.url)}" target="_blank" rel="noopener" '
            r'class="[^"]*">Open file<span class="sr-only"> Blood test \(opens in a '
            r"new tab\)</span>",
        )
        self.assertIn(f'data-dialog-open="remove-file-{blood.pk}"', html)
        self.assertIn('Remove<span class="sr-only"> Blood test</span>', html)

    def test_removed_files_only_when_there_are_some(self):
        self._file(description="Blood test")
        # (The remove dialog names "Removed files" too, so check the list)
        html = self._html()
        self.assertNotIn("Removed files (", html)
        self.assertNotIn("<details", html)

        self._file(
            description="Wrong file",
            is_archived=True,
            archived_at=JUST_AFTER_MIDNIGHT - datetime.timedelta(days=1),
        )
        self._file(name="test_results/old.png", is_archived=True)
        html = self._html()
        self.assertIn("Test results (1)", html)
        # Closed by default
        self.assertIn('<details class="group mt-4 rounded-lg border', html)
        self.assertNotRegex(html, r"<details[^>]* open")
        self.assertIn("Removed files (2)</summary>", html)
        self.assertIn("Wrong file · PDF</span>", html)
        self.assertIn('<span class="text-sm">Removed yesterday</span>', html)
        self.assertIn("old.png · PNG</span>", html)
        self.assertIn('<span class="text-sm">Removed, date not recorded</span>', html)
        self.assertIn(
            "Removed files are hidden from Ana Cruz. They stay here for the clinic.",
            html,
        )
        details = re.search(r"(?s)<details.*?</details>", html).group()
        self.assertNotIn("Remove<span", details)
        self.assertEqual(details.count("Open file<span"), 2)

    # ─── Vaccinations ─────────────────────────────────────────────────────────

    def test_vaccination_rows_open_the_highlighted_dose(self):
        dose = self._dose(next_due_date=datetime.date(2027, 11, 3))
        self._dose(vaccine_name=Vaccination.DEWORMING)
        html = self._html()
        self.assertIn("Vaccinations at this visit (2)", html)
        history = reverse("admin_pet_vaccination_history", args=[self.pet.pk])
        self.assertRegex(
            html,
            rf'<a href="{history}\?highlight={dose.pk}#vaccination-{dose.pk}" '
            r'class="inline-flex min-h-11 [^"]*border-violet-300 bg-violet-50[^"]*">'
            r"Open<span class=\"sr-only\"> Bantay's Anti-rabies</span>",
        )
        self.assertIn("Given today · Next due Wed, Nov 3, 2027</p>", html)
        self.assertIn("Given today · No next date</p>", html)
        create = reverse("admin_vaccination_standalone_create", args=[self.pet.pk])
        self.assertEqual(html.count(f'href="{create}?record={self.record.pk}"'), 2)

    # ─── Dialogs ──────────────────────────────────────────────────────────────

    def test_each_dialog_posts_to_its_url_with_csrf(self):
        item, test_file = self._everything()
        html = self._html()
        expected = {
            "add-medicine": reverse(
                "admin_prescription_item_add", args=[self.record.pk]
            ),
            "add-file": reverse("admin_test_result_file_upload", args=[self.record.pk]),
            f"remove-medicine-{item.pk}": reverse(
                "admin_prescription_item_delete", args=[item.pk]
            ),
            f"remove-file-{test_file.pk}": reverse(
                "admin_test_result_file_archive", args=[test_file.pk]
            ),
            f"reminder-dialog-{self.record.pk}": reverse(
                "admin_trigger_followup_reminder", args=[self.record.pk]
            ),
        }
        self.assertEqual(len(_dialogs(html)), len(expected))
        for dialog_id, action in expected.items():
            with self.subTest(dialog=dialog_id):
                dialog = _dialog(html, dialog_id)
                self.assertIsNotNone(dialog)
                self.assertIn(f'<form method="post" action="{action}"', dialog)
                self.assertIn('name="csrfmiddlewaretoken"', dialog)
                # Every dialog has an opener on the page
                self.assertIn(f'data-dialog-open="{dialog_id}"', html)
                if dialog_id.startswith("remove-"):
                    self.assertIn("btn btn-danger", dialog)
                    self.assertIn("Yes, remove it", dialog)
                else:
                    self.assertIn("btn btn-primary", dialog)
        self.assertIn('enctype="multipart/form-data"', _dialog(html, "add-file"))
        self.assertNotIn("data-open-on-load", html)

    def test_dialog_texts(self):
        item, test_file = self._everything()
        html = self._html()
        remove_med = _dialog(html, f"remove-medicine-{item.pk}")
        self.assertIn('font-bold">Remove Amoxicillin?</h2>', remove_med)
        self.assertIn(
            "It disappears from this record and from Bantay's page for Ana Cruz.",
            remove_med,
        )
        self.assertIn(
            "You can remove medicines only today, the day this record was written. "
            "From tomorrow the list is kept as it is.",
            remove_med,
        )
        remove_file = _dialog(html, f"remove-file-{test_file.pk}")
        self.assertIn("Remove “Blood test”?</h2>", remove_file)
        self.assertIn(
            "It is hidden from Ana Cruz and moves to <b>Removed files</b>", remove_file
        )
        add_med = _dialog(html, "add-medicine")
        self.assertIn("Add a medicine for Bantay</h2>", add_med)
        self.assertIn(
            "It shows on this record and on Bantay's page for Ana Cruz.", add_med
        )
        self.assertEqual(add_med.count("(optional)"), 4)
        for placeholder in (
            "For example: Amoxicillin 250 mg",
            "1 tablet",
            "Twice a day",
            "7 days",
            "For example: give with food",
        ):
            with self.subTest(placeholder=placeholder):
                self.assertIn(f'placeholder="{placeholder}"', add_med)
        add_file = _dialog(html, "add-file")
        self.assertIn("Add a test result for Bantay</h2>", add_file)
        self.assertIn('placeholder="For example: Blood test, X-ray"', add_file)
        self.assertIn('accept=".pdf,.jpg,.jpeg,.png"', add_file)
        self.assertIn(
            'class="sr-only" aria-describedby="add-file-help" data-photo-input>',
            add_file,
        )
        self.assertIn("data-photo-name", add_file)
        self.assertIn("PDF, JPG or PNG, up to 10 MB.</p>", add_file)
        self.assertIn(
            "Ana Cruz sees that a test result is on file, but can't open it.", add_file
        )

    def test_add_medicine_error_reopens_with_values(self):
        url = reverse("admin_prescription_item_add", args=[self.record.pk])
        response = self.client.post(
            url, {"medicine_name": "", "dosage": "5 ml", "notes": "With food"}
        )
        html = response.content.decode()
        self.assertEqual(html.count("data-open-on-load"), 1)
        dialog = _dialog(html, "add-medicine")
        self.assertIn("data-open-on-load", dialog)
        self.assertIn('value="5 ml"', dialog)
        self.assertIn("With food</textarea>", dialog)
        self.assertIn("Type the medicine name.", dialog)
        self.assertFalse(PrescriptionItem.objects.exists())

    def test_add_file_error_reopens_with_the_title(self):
        url = reverse("admin_test_result_file_upload", args=[self.record.pk])
        for data, error in (
            (
                {
                    "file": SimpleUploadedFile(
                        "notes.docx", b"x", "application/msword"
                    ),
                    "description": "Blood",
                },
                "Only PDF, JPG or PNG files can be added.",
            ),
            ({"description": "Blood"}, "Choose a file to add."),
        ):
            with self.subTest(error=error):
                html = self.client.post(url, data).content.decode()
                self.assertEqual(html.count("data-open-on-load"), 1)
                dialog = _dialog(html, "add-file")
                self.assertIn("data-open-on-load", dialog)
                self.assertIn('value="Blood"', dialog)
                self.assertIn(error, dialog)
                self.assertIn("Choose the file again.", dialog)
        self.assertFalse(TestResultFile.objects.exists())

    # ─── Follow-up ────────────────────────────────────────────────────────────

    def test_follow_up_card_in_each_state(self):
        html = self._html()
        self.assertIn("No follow-up needed.", html)
        self.assertNotIn("Send reminder", html)
        for days, date_line, when in (
            (3, "Friday, November 6", "in 3 days"),
            (1, "Tomorrow, Wednesday, November 4", "tomorrow"),
            (0, "Today, Tuesday, November 3", "today"),
            (-1, "Monday, November 2", "1 day ago"),
            (-4, "Friday, October 30", "4 days ago"),
        ):
            with self.subTest(days=days):
                self._follow(days)
                html = self._html()
                self.assertIn(
                    '<section class="order-2 rounded-xl border-2 border-yellow-300',
                    html,
                )
                self.assertIn(
                    '<p class="mt-2 text-lg font-semibold text-gray-900">'
                    f"{date_line}</p>",
                    html,
                )
                self.assertIn(f'<p class="text-base text-gray-800">{when}</p>', html)
                self.assertIn(
                    f'data-dialog-open="reminder-dialog-{self.record.pk}"', html
                )
                self.assertNotIn("No follow-up needed.", html)

    def test_send_reminder_posts_return_to_record(self):
        self._follow(3)
        dialog = _dialog(self._html(), f"reminder-dialog-{self.record.pk}")
        self.assertIn('<input type="hidden" name="return_to" value="record">', dialog)
        self.assertNotIn('name="tab"', dialog)
        self.assertIn("Send a follow-up reminder to Ana Cruz?</h2>", dialog)
        self.assertIn(
            "Bantay's follow-up for the visit on Sat, Oct 24 is on <b>Friday, "
            "November 6</b>.",
            dialog,
        )
        self.assertIn("and follow-up emails are on.", dialog)

    def test_last_reminder_line(self):
        self._follow(3)
        html = self._html()
        self.assertIn("<span>No follow-up reminder sent for Bantay yet.</span>", html)
        notice = Notification.objects.create(
            recipient=self.owner_user,
            notification_type=Notification.FOLLOWUP_REMINDER,
            title="Reminder",
            message="Reminder",
            related_pet=self.pet,
        )
        Notification.objects.filter(pk=notice.pk).update(
            created_at=JUST_AFTER_MIDNIGHT - datetime.timedelta(days=3)
        )
        html = self._html()
        self.assertIn(
            "<span>Last follow-up reminder for Bantay: Sat, Oct 31</span>", html
        )
        self.assertIn("Last follow-up reminder for Bantay: Sat, Oct 31.</span>", html)

    # ─── Owner, written ───────────────────────────────────────────────────────

    def test_owner_and_written_cards(self):
        PetOwner.objects.filter(pk=self.owner.pk).update(contact_number="0917 123 4567")
        html = self._html()
        owner = reverse("admin_owner_detail", args=[self.owner.pk])
        self.assertIn(
            f'<a href="{owner}" class="inline-flex min-h-11 items-center font-medium '
            'text-blue-600 underline underline-offset-2">Ana Cruz</a>',
            html,
        )
        self.assertIn("0917 123 4567</p>", html)
        self.assertIn('<span class="break-all">ana@example.com</span>', html)
        self.assertIn("<p>Written today, 12:30 AM</p>", html)
        self.assertNotIn("Last changed", html)
        # Edited later: written yesterday, changed now
        MedicalRecord.objects.filter(pk=self.record.pk).update(
            created_at=JUST_AFTER_MIDNIGHT - datetime.timedelta(days=1)
        )
        html = self._html()
        self.assertIn("<p>Written yesterday, 12:30 AM</p>", html)
        self.assertIn('<p class="mt-1">Last changed today, 12:30 AM</p>', html)

    # ─── Archived ─────────────────────────────────────────────────────────────

    def _check_archived(self, banner, explanation):
        self._everything()
        self._dose()
        html = self._html()
        self.assertIn(banner, html)
        self.assertIn(explanation, html)
        self.assertEqual(_dialogs(html), [])
        self.assertNotIn("data-dialog-open", html)
        for absent in (
            "Add medicine",
            "Add file",
            "Add vaccination",
            "Send reminder",
            "Remove<span",
            "can only be removed",
            "To set one, use Edit record.",
        ):
            with self.subTest(absent=absent):
                self.assertNotIn(absent, html)
        actions = re.search(r"(?s)<section class=\"card order-1\".*?</section>", html)
        self.assertEqual(actions.group().count("<a "), 1)
        self.assertIn("Edit record</a>", actions.group())
        self.assertNotIn("<button", actions.group())
        return html

    def test_archived_pet(self):
        Pet.objects.filter(pk=self.pet.pk).update(is_archived=True)
        html = self._check_archived(
            "<p><b>Bantay is archived.</b> This record is kept and can still be "
            "corrected. Nothing new can be added to it.</p>",
            "Bantay is archived. You can still correct what is written, but you "
            "can't add medicines, files or vaccinations, or send reminders. Restore "
            "Bantay on the pet page to do that.",
        )
        self.assertIn("Pet archived</span>", html)
        self.assertIn("bg-gray-100 text-gray-600 ring-1 ring-gray-300", html)

    def test_archived_owner(self):
        self.owner.archive()
        html = self._check_archived(
            "<p><b>Bantay's owner, Ana Cruz, is archived.</b> This record is kept and "
            "can still be corrected. Nothing new can be added to it.</p>",
            "Ana Cruz is archived. You can still correct what is written, but you "
            "can't add medicines, files or vaccinations, or send reminders. Restore "
            "Ana Cruz on their page to do that.",
        )
        self.assertNotIn("Pet archived", html)
        self.assertNotIn("Bantay is archived.", html)

    # ─── Removed view, queries, links ─────────────────────────────────────────

    def test_removed_vaccination_add_url_no_longer_resolves(self):
        with self.assertRaises(NoReverseMatch):
            reverse("admin_vaccination_add", args=[self.record.pk])
        old = f"/admin/medical/{self.record.pk}/vaccination/add/"
        shot = {
            "vaccine_name": Vaccination.ANTI_RABIES,
            "date_administered": "2026-11-03",
        }
        self.assertEqual(self.client.post(old, shot).status_code, 404)
        self.assertFalse(Vaccination.objects.exists())

    def _fill(self, count):
        for i in range(count):
            self._medicine(name=f"Medicine {i}")
            self._file(name=f"test_results/scan{i}.pdf")
            self._file(
                name=f"test_results/old{i}.png",
                is_archived=True,
                archived_at=JUST_AFTER_MIDNIGHT,
            )
            self._dose(vaccine_name=Vaccination.DEWORMING)

    def _page_queries(self):
        self.client.get(self.url)
        with CaptureQueriesContext(connection) as queries:
            self.client.get(self.url)
        return [query["sql"] for query in queries]

    def test_query_count_is_flat(self):
        """The full page, dialogs included, costs the same with nothing as
        with 5 medicines, files, removed files and vaccinations."""
        self._follow(3)
        few = self._page_queries()
        self._fill(5)
        many = self._page_queries()
        self.assertEqual(len(few), len(many))
        # Session, user, record, active files, removed files, last reminder,
        # clinic settings, 3 sidebar counts, medicines, vaccinations
        self.assertEqual(len(many), 12, "\n".join(many))

    def test_every_link_is_on_one_line(self):
        self._everything()
        self._dose()
        self._file(is_archived=True, archived_at=JUST_AFTER_MIDNIGHT)
        for archived in (False, True):
            with self.subTest(archived=archived):
                Pet.objects.filter(pk=self.pet.pk).update(is_archived=archived)
                for tag in re.findall(r"<a\b[^>]*>", self._html()):
                    self.assertNotIn("\n", tag)


# ── Batch 5b-3: Medical record form (new + edit) ─────────────────────────────

FOLLOW_UP_HIDDEN = (
    '<div class="mt-3 border-l-4 border-violet-200 pl-4 hidden" data-follow-up-on>'
)
FOLLOW_UP_SHOWN = (
    '<div class="mt-3 border-l-4 border-violet-200 pl-4" data-follow-up-on>'
)
OFF_HINT = "Tick it to pick a date. The owner gets a reminder 3 days before."
EMPTY_DATE_HELP = (
    "Must be after the visit date. A reminder goes to the owner 3 days before."
)


@PLAIN_STATIC
class RecordFormPageTests(FrozenMedicalTestBase):
    """admin/medical/record_form.html on the new layout (Slice 5b-3).
    Frozen at 00:30 Manila on Tue, Nov 3, 2026."""

    def setUp(self):
        super().setUp()
        self.new_url = reverse("admin_medical_record_create", args=[self.pet.pk])

    def _visit(self, day=TODAY, hour=9):
        return Appointment.objects.create(
            owner=self.owner,
            pet=self.pet,
            date=day,
            time=datetime.time(hour, 0),
            status=Appointment.CONFIRMED,
        )

    def _edit_url(self, record):
        return reverse("admin_medical_record_edit", args=[record.pk])

    def _html(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def _post(self, url, **data):
        post = {"record_date": "2026-11-03"}
        post.update(data)
        response = self.client.post(url, post)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def _three_pages(self):
        """(case, url, back url, back label): new from a visit, new without
        one, edit (a record from a visit still goes back to the record)."""
        visit = self._visit()
        record = self._record(record_date=_days(-1), appointment=self._visit(_days(-1)))
        return [
            (
                "visit",
                f"{self.new_url}?appointment={visit.pk}",
                reverse("admin_appointment_detail", args=[visit.pk]),
                "Back to the visit",
            ),
            (
                "no visit",
                self.new_url,
                reverse("admin_pet_detail", args=[self.pet.pk]),
                "Back to Bantay",
            ),
            (
                "edit",
                self._edit_url(record),
                reverse("admin_medical_record_detail", args=[record.pk]),
                "Back to the record",
            ),
        ]

    # ─── Layout ───────────────────────────────────────────────────────────────

    def test_new_layout_without_old_files(self):
        record = self._record()
        for url, title in (
            (self.new_url, "New visit record for Bantay"),
            (self._edit_url(record), "Edit Bantay's visit record"),
        ):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertTemplateUsed(response, "layouts/admin.html")
                self.assertTemplateUsed(
                    response, "admin/medical/record_form_parts/header.html"
                )
                for old in (
                    "record_form.css",
                    "layer(legacy)",
                    "admin-page",
                    "medical-form",
                    "form-group",
                    "Create Record",
                    "Record Date",
                    "Public Notes",
                ):
                    with self.subTest(old=old):
                        self.assertNotContains(response, old)
                self.assertContains(
                    response, f"<title>{title} — Hapi Vet Admin</title>"
                )
                self.assertContains(response, "js/admin/record_form.js")
                self.assertContains(response, '<div class="mx-auto max-w-3xl">')

    def test_exactly_one_violet_button(self):
        for case, url, _back, _label in self._three_pages():
            with self.subTest(case=case):
                html = self._html(url)
                self.assertEqual(html.count("btn-primary"), 1)
                label = "Save changes" if case == "edit" else "Save record"
                self.assertRegex(
                    html,
                    r'(?s)<button type="submit" class="btn btn-primary min-h-12 '
                    r'w-full font-semibold sm:w-auto">\s*<svg[^>]*>.*?</svg>\s*'
                    rf"{label}</button>",
                )

    def test_sidebar_highlights_medical_records(self):
        record = self._record()
        records = reverse("admin_medical_record_list")
        for url in (self.new_url, self._edit_url(record)):
            with self.subTest(url=url):
                self.assertRegex(
                    self._html(url), rf'<a href="{records}"[^>]*aria-current="page">'
                )

    # ─── Header, back and Cancel ──────────────────────────────────────────────

    def test_titles_and_subtitles(self):
        html = self._html(self.new_url)
        self.assertIn(
            '<h1 class="text-2xl font-bold text-gray-900">New visit record for '
            "Bantay</h1>",
            html,
        )
        self.assertIn(
            "Write what you found and what you did. Only the visit date is "
            "required.</p>",
            html,
        )
        self.assertNotIn("Changes replace what was written.", html)
        html = self._html(self._edit_url(self._record()))
        self.assertIn(
            '<h1 class="text-2xl font-bold text-gray-900">Edit visit record · '
            "Bantay</h1>",
            html,
        )
        self.assertIn(
            "Changes replace what was written. The record shows when it was last "
            "changed.</p>",
            html,
        )
        self.assertNotIn("Only the visit date is required.", html)

    def test_back_and_cancel_go_to_the_same_place(self):
        for case, url, back, label in self._three_pages():
            with self.subTest(case=case):
                html = self._html(url)
                self.assertRegex(
                    html,
                    rf'(?s)<a href="{re.escape(back)}" class="mb-2 [^"]*">'
                    rf"\s*<svg[^>]*>.*?</svg>\s*{label}</a>",
                )
                self.assertIn(
                    f'<a href="{back}" class="btn btn-outline min-h-12 w-full '
                    'font-semibold sm:w-auto">Cancel</a>',
                    html,
                )

    def test_new_note_by_the_buttons(self):
        note = (
            "<span>After saving you'll see the record, where you can add medicines, "
            "files and vaccinations.</span>"
        )
        for case, url, _back, _label in self._three_pages():
            with self.subTest(case=case):
                self.assertEqual(note in self._html(url), case != "edit")

    # ─── Pet card ─────────────────────────────────────────────────────────────

    def test_pet_line(self):
        html = self._html(self.new_url)
        self.assertIn('<p class="text-sm text-gray-600">Pet</p>', html)
        self.assertIn(
            '<p class="text-lg font-semibold text-gray-900">Bantay <span '
            'class="text-base font-normal text-gray-700">· Owner: Ana Cruz</span></p>',
            html,
        )
        self.assertIn('aria-label="No photo of Bantay yet"', html)
        self.assertIn("size-10 rounded-xl", html)

    def test_visit_line_both_ways(self):
        visit = self._visit()
        html = self._html(f"{self.new_url}?appointment={visit.pk}")
        self.assertIn(
            "bg-violet-50 px-3 py-2 text-base text-gray-900",
            html,
        )
        self.assertIn(
            '<span>From the visit <b class="font-semibold">Today at 9:00 AM</b></span>',
            html,
        )
        self.assertNotIn("Not linked to a visit", html)
        self.assertIn(
            f'<input type="hidden" name="appointment" value="{visit.pk}">', html
        )

        html = self._html(self.new_url)
        self.assertIn("bg-gray-50 px-3 py-2 text-base text-gray-700", html)
        self.assertIn("<span>Not linked to a visit</span>", html)
        self.assertNotIn("From the visit", html)
        self.assertNotIn('name="appointment"', html)

        # Edit: the record's visit, but no hidden field (the view ignores it)
        record = self._record(
            record_date=_days(-1), appointment=self._visit(_days(-1), 10)
        )
        html = self._html(self._edit_url(record))
        self.assertIn(
            '<span>From the visit <b class="font-semibold">Yesterday at 10:00 AM'
            "</b></span>",
            html,
        )
        self.assertNotIn('name="appointment"', html)

    def test_visit_date_field_and_each_help(self):
        from_visit = (
            "Taken from the visit. Change it only if the record is for another day."
        )
        today = (
            "Today. You can pick an earlier day, for example when writing up an old "
            "paper record."
        )
        expected = {"visit": from_visit, "no visit": today, "edit": None}
        for case, url, _back, _label in self._three_pages():
            with self.subTest(case=case):
                html = self._html(url)
                self.assertRegex(
                    html,
                    r'<label for="id_record_date" class="[^"]*">Visit date '
                    r'<span class="text-red-700" aria-hidden="true">\*</span></label>',
                )
                self.assertRegex(
                    html, r'<div class="max-w-xs"><input type="date" name="record_date"'
                )
                for line in (from_visit, today):
                    self.assertEqual(
                        f'<p class="text-sm text-gray-700">{line}</p>' in html,
                        line == expected[case],
                    )

    # ─── Notes ────────────────────────────────────────────────────────────────

    def test_every_placeholder_and_help_text(self):
        html = self._html(self.new_url)
        for name, placeholder in (
            ("symptoms", "What the owner noticed and what you saw"),
            ("diagnosis", "For example: Ear infection, left ear"),
            ("treatment_given", "What you did at the clinic today"),
            ("public_notes", "Care at home, what to watch for, when to come back"),
            ("private_notes", "Anything for the clinic only"),
        ):
            with self.subTest(name=name):
                self.assertRegex(
                    html,
                    rf'<textarea name="{name}" cols="40" rows="\d" '
                    rf'placeholder="{placeholder}"',
                )
        for label in ("Symptoms", "Diagnosis", "Treatment given"):
            with self.subTest(label=label):
                self.assertRegex(
                    html,
                    rf"(?s)<label [^>]*>\s*{label} <span "
                    r'class="font-normal text-gray-600">\(optional\)</span>',
                )
        for line in (
            '<p class="text-sm text-gray-600">The lists show the start of this, so '
            "begin with the main finding.</p>",
            '<p class="text-sm text-gray-700">Medicines, test results and '
            "vaccinations are added on the record page after saving.</p>",
            "Visit notes</h2>",
        ):
            with self.subTest(line=line):
                self.assertEqual(html.count(line), 1)

    def test_owner_note_and_private_cards(self):
        html = self._html(self.new_url)
        self.assertIn(
            '<section class="form-field rounded-xl border border-blue-200 bg-blue-50 '
            'p-6 shadow-sm">',
            html,
        )
        self.assertRegex(
            html,
            r'(?s)<label for="id_public_notes" class="[^"]*">\s*<svg[^>]*>.*?</svg>'
            r'\s*Note for the owner <span class="text-base font-normal text-gray-600">'
            r"\(optional\)</span></label>",
        )
        self.assertIn(
            '<p class="mt-1 text-sm font-medium text-blue-900">Ana Cruz sees this on '
            "Bantay's page, with the diagnosis and medicines.</p>",
            html,
        )
        self.assertIn(
            '<section class="form-field rounded-xl border-2 border-amber-300 '
            'bg-amber-50 p-6 shadow-sm">',
            html,
        )
        self.assertRegex(
            html,
            r'(?s)<label for="id_private_notes" class="[^"]*">\s*<svg[^>]*>.*?</svg>'
            r'\s*Private notes <span class="text-base font-normal text-gray-600">'
            r"\(optional\)</span></label>",
        )
        self.assertIn(
            '<p class="mt-1 text-sm font-medium text-amber-900">Only the clinic sees '
            "this. It is never shown to Ana Cruz.</p>",
            html,
        )

    # ─── Follow-up ────────────────────────────────────────────────────────────

    def test_follow_up_unticked(self):
        html = self._html(self.new_url)
        self.assertIn(
            '<input type="checkbox" name="follow_up_required" '
            'id="id_follow_up_required" class="size-6 shrink-0 accent-violet-600" '
            "data-follow-up-box>",
            html,
        )
        self.assertRegex(
            html,
            r'(?s)<label for="id_follow_up_required" class="mt-3 flex min-h-11 '
            r'[^"]*">.*?Needs a follow-up visit\s*</label>',
        )
        self.assertIn(
            f'<p class="mt-1 text-sm text-gray-700" data-follow-up-off>{OFF_HINT}</p>',
            html,
        )
        self.assertIn(FOLLOW_UP_HIDDEN, html)
        self.assertNotIn(FOLLOW_UP_SHOWN, html)
        # The date is still in the page, for the script to show
        self.assertIn('<label for="id_follow_up_date"', html)
        self.assertIn("<noscript><style>[data-follow-up-on] { display: block; }", html)

    def test_follow_up_ticked(self):
        record = self._record(record_date=_days(-1))
        MedicalRecord.objects.filter(pk=record.pk).update(follow_up_required=True)
        html = self._html(self._edit_url(record))
        self.assertIn('accent-violet-600" checked data-follow-up-box>', html)
        self.assertIn(FOLLOW_UP_SHOWN, html)
        self.assertNotIn(FOLLOW_UP_HIDDEN, html)
        self.assertIn(
            '<p class="mt-1 text-sm text-gray-700 hidden" data-follow-up-off>', html
        )
        # Ticked with no saved date (older data): the plain help
        self.assertIn(f'<p class="text-sm text-gray-700">{EMPTY_DATE_HELP}</p>', html)

    def test_follow_up_on_a_re_render_with_errors(self):
        html = self._post(
            self.new_url, record_date="2026-11-04", follow_up_required="on"
        )
        self.assertIn(FOLLOW_UP_SHOWN, html)
        self.assertIn('accent-violet-600" checked data-follow-up-box>', html)
        self.assertIn(f'<p class="text-sm text-gray-700">{EMPTY_DATE_HELP}</p>', html)

        html = self._post(self.new_url, record_date="2026-11-04")
        self.assertIn(FOLLOW_UP_HIDDEN, html)
        self.assertIn('accent-violet-600" data-follow-up-box>', html)

    def test_edit_help_with_a_saved_date(self):
        cases = [
            (_days(2), "Thursday, November 5 · in 2 days"),
            (_days(1), "Wednesday, November 4 · tomorrow"),
            (TODAY, "Tuesday, November 3 · today"),
            (_days(-1), "Monday, November 2 · 1 day ago"),
            (_days(-4), "Friday, October 30 · 4 days ago"),
            # 5b-4: the shared wording says months after 31 days (was "in 63 days")
            (
                datetime.date(2027, 1, 5),
                "Tuesday, January 5, 2027 · in about 2 months",
            ),
        ]
        record = self._record(record_date=_days(-20))
        for day, line in cases:
            with self.subTest(day=day):
                MedicalRecord.objects.filter(pk=record.pk).update(
                    follow_up_required=True, follow_up_date=day
                )
                html = self._html(self._edit_url(record))
                self.assertIn(f'<p class="text-sm text-gray-700">{line}</p>', html)
                self.assertNotIn(EMPTY_DATE_HELP, html)
                self.assertIn(f'value="{day.isoformat()}"', html)

    def test_edit_help_keeps_the_saved_date_after_a_failed_save(self):
        # A failed POST writes the posted values onto the record; the help
        # still names the saved date
        record = self._record(
            record_date=_days(-20), follow_up_required=True, follow_up_date=_days(2)
        )
        html = self._post(
            self._edit_url(record),
            record_date="2026-11-04",
            follow_up_required="on",
            follow_up_date="2026-11-20",
        )
        self.assertIn("Thursday, November 5 · in 2 days", html)
        self.assertIn('value="2026-11-20"', html)

    # ─── Errors ───────────────────────────────────────────────────────────────

    def test_error_summary_singular_and_plural(self):
        html = self._post(self.new_url, record_date="2026-11-04")
        self.assertIn(
            '<div class="flex items-start gap-3 rounded-xl border border-red-200 '
            'bg-red-50 px-5 py-4 text-red-900" role="alert">',
            html,
        )
        self.assertIn(
            '<p class="text-lg font-semibold">Please fix the 1 item below.</p>', html
        )
        self.assertIn("Nothing was saved yet.", html)
        # The summary replaces the old flash
        self.assertNotIn("Please correct the errors below.", html)

        html = self._post(
            self.new_url, record_date="2026-11-04", follow_up_required="on"
        )
        self.assertIn(
            '<p class="text-lg font-semibold">Please fix the 2 items below.</p>', html
        )
        self.assertFalse(MedicalRecord.objects.exists())

        # No summary before a save
        self.assertNotIn('role="alert"', self._html(self.new_url))

    def test_every_error_message(self):
        cases = [
            ({"record_date": ""}, "Pick the visit date."),
            (
                {"record_date": "2026-11-04"},
                "The visit date can&#x27;t be in the future.",
            ),
            (
                {"follow_up_required": "on"},
                "Pick the follow-up date, or untick “Needs a follow-up visit”.",
            ),
            (
                {"follow_up_required": "on", "follow_up_date": "2026-11-03"},
                "The follow-up date must be after the visit date.",
            ),
        ]
        for data, message in cases:
            with self.subTest(message=message):
                html = self._post(self.new_url, **data)
                # Under its field, after the triangle icon
                self.assertRegex(
                    html,
                    r'(?s)<p class="flex items-start gap-1 text-sm font-medium '
                    rf'text-red-700">\s*<svg[^>]*>.*?</svg>\s*{re.escape(message)}</p>',
                )
                self.assertIn("Please fix the 1 item below.", html)
        self.assertFalse(MedicalRecord.objects.exists())

    def test_typed_values_are_kept_after_an_error(self):
        html = self._post(
            self.new_url,
            record_date="2026-11-13",
            symptoms="Scratching both front paws",
            diagnosis="Skin allergy (mild)",
            treatment_given="Cleaned both paws",
            public_notes="Keep the paws dry",
            private_notes="Owner asked about cost",
            follow_up_required="on",
            follow_up_date="2026-11-20",
        )
        self.assertIn('value="2026-11-13"', html)
        self.assertIn('value="2026-11-20"', html)
        for text in (
            "Scratching both front paws",
            "Skin allergy (mild)",
            "Cleaned both paws",
            "Keep the paws dry",
            "Owner asked about cost",
        ):
            with self.subTest(text=text):
                self.assertRegex(
                    html, rf"<textarea [^>]*>\s*{re.escape(text)}</textarea>"
                )

    def test_unticked_follow_up_date_is_cleared(self):
        # Existing rule (5a-2), seen on the record page after the save
        record = self._record(
            record_date=_days(-1), follow_up_required=True, follow_up_date=_days(5)
        )
        response = self.client.post(
            self._edit_url(record),
            {"record_date": "2026-11-02", "follow_up_date": "2026-11-08"},
            follow=True,
        )
        self.assertContains(
            response, '<p class="mt-2 text-base text-gray-700">No follow-up needed.</p>'
        )

    # ─── Saves, owners, archived ──────────────────────────────────────────────

    def test_private_notes_save_but_never_reach_the_owner(self):
        self.client.post(
            self.new_url,
            {
                "record_date": "2026-11-03",
                "diagnosis": "Ear infection",
                "private_notes": "Possibly yeast too",
            },
        )
        record = MedicalRecord.objects.get()
        self.assertEqual(record.private_notes, "Possibly yeast too")
        self.client.force_login(self.owner_user)
        response = self.client.get(
            reverse("owner_pet_medical_history", args=[self.pet.pk])
        )
        self.assertContains(response, "Ear infection")
        self.assertNotContains(response, "Possibly yeast too")

    def test_edit_is_allowed_for_an_archived_pet_or_owner(self):
        record = self._record()
        url = self._edit_url(record)
        Pet.objects.filter(pk=self.pet.pk).update(is_archived=True)
        self.assertEqual(self.client.get(url).status_code, 200)
        Pet.objects.filter(pk=self.pet.pk).update(is_archived=False)
        self.owner.archive()
        self.assertEqual(self.client.get(url).status_code, 200)
        response = self.client.post(
            url, {"record_date": "2026-11-03", "diagnosis": "Fixed"}
        )
        self.assertEqual(response.status_code, 302)
        record.refresh_from_db()
        self.assertEqual(record.diagnosis, "Fixed")

    # ─── Queries, links ───────────────────────────────────────────────────────

    def _page_queries(self, url):
        self.client.get(url)
        with CaptureQueriesContext(connection) as queries:
            self.client.get(url)
        return [query["sql"] for query in queries]

    def test_query_count_is_flat(self):
        """New and edit cost the same with and without a visit, and with
        more records for the pet."""
        bare = self._record(record_date=_days(-2))
        from_visit = self._record(
            record_date=_days(-1), appointment=self._visit(_days(-1), 10)
        )
        visit = self._visit()
        pages = {
            "new": self.new_url,
            "new from a visit": f"{self.new_url}?appointment={visit.pk}",
            "edit": self._edit_url(bare),
            "edit from a visit": self._edit_url(from_visit),
        }
        counts = {case: len(self._page_queries(url)) for case, url in pages.items()}
        for i in range(5):
            self._record(record_date=_days(-30 - i))
        for case, url in pages.items():
            with self.subTest(case=case):
                queries = self._page_queries(url)
                self.assertEqual(len(queries), counts[case])
        # Session, user, pet + owner, 3 sidebar counts, clinic settings
        self.assertEqual(counts["new"], 7, counts)
        self.assertEqual(counts["edit"], 7, counts)
        # + the visit; + its record check on new
        self.assertEqual(counts["new from a visit"], 9, counts)
        self.assertEqual(counts["edit from a visit"], 7, counts)

    def test_every_link_is_on_one_line(self):
        for case, url, _back, _label in self._three_pages():
            with self.subTest(case=case):
                for tag in re.findall(r"<a\b[^>]*>", self._html(url)):
                    self.assertNotIn("\n", tag)
        html = self._post(self.new_url, record_date="2026-11-04")
        for tag in re.findall(r"<a\b[^>]*>", html):
            self.assertNotIn("\n", tag)


class DaysFromTodayTests(SimpleTestCase):
    """medical/utils.days_from_today, the edit form's follow-up wording."""

    def test_wording(self):
        from .utils import days_from_today

        for days, text in (
            (0, "today"),
            (1, "tomorrow"),
            (2, "in 2 days"),
            (-1, "1 day ago"),
            (-4, "4 days ago"),
        ):
            with self.subTest(days=days):
                self.assertEqual(days_from_today(_days(days), TODAY), text)

    def test_months_and_years_ahead(self):
        """Batch 5b-4: days up to 31, then about N months (days // 30), from
        360 days about N years (days // 360). The past stays in days."""
        from .utils import days_from_today

        for days, text in (
            (10, "in 10 days"),
            (31, "in 31 days"),
            (32, "in about 1 month"),
            (59, "in about 1 month"),
            (60, "in about 2 months"),
            (92, "in about 3 months"),
            (359, "in about 11 months"),
            (360, "in about 1 year"),
            (365, "in about 1 year"),
            (730, "in about 2 years"),
            (-45, "45 days ago"),
            (-400, "400 days ago"),
        ):
            with self.subTest(days=days):
                self.assertEqual(days_from_today(_days(days), TODAY), text)


# ── Batch 5b-4: Vaccination form (new + edit) ────────────────────────────────

VACCINE_NAME_HIDDEN = (
    '<div class="border-l-4 border-violet-200 pl-4 hidden" data-vaccine-other>'
)
VACCINE_NAME_SHOWN = (
    '<div class="border-l-4 border-violet-200 pl-4" data-vaccine-other>'
)
NEXT_DOSE_EMPTY = '<p class="text-sm text-gray-700">Leave empty if no next dose.</p>'
GIVEN_BY_HELP = (
    '<p class="text-sm text-gray-600">Filled in with the clinic vet from Clinic '
    "settings.</p>"
)
LABEL_CARD_EDIT_LINE = (
    '<p class="text-sm text-gray-700">These can be fixed any time without a '
    "reason.</p>"
)


@PLAIN_STATIC
class VaccinationFormPageTests(FrozenMedicalTestBase):
    """admin/medical/vaccination_form.html on the new layout (Slice 5b-4),
    for new from the pet page, new on a record and edit. Frozen at 00:30
    Manila on Tue, Nov 3, 2026."""

    def setUp(self):
        super().setUp()
        pk = self.pet.pk
        self.new_url = reverse("admin_vaccination_standalone_create", args=[pk])
        self.history_url = reverse("admin_pet_vaccination_history", args=[pk])

    def _dose(self, **fields):
        """An 8-in-1 given Tue, Oct 6, 2026 (the mockup's edit state)."""
        fields.setdefault("vaccine_name", Vaccination.EIGHT_IN_ONE)
        fields.setdefault("date_administered", datetime.date(2026, 10, 6))
        return Vaccination.objects.create(pet=self.pet, **fields)

    def _edit_url(self, dose):
        return reverse("admin_vaccination_edit", args=[dose.pk])

    def _on_record_url(self, record):
        return f"{self.new_url}?record={record.pk}"

    def _html(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def _post(self, url, **data):
        post = {
            "vaccine_name": Vaccination.ANTI_RABIES,
            "date_administered": "2026-11-03",
        }
        post.update(data)
        response = self.client.post(url, post)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def _edit_data(self, **changes):
        """The mockup dose's saved values as posted, with changes applied."""
        data = {
            "vaccine_name": Vaccination.EIGHT_IN_ONE,
            "custom_vaccine_name": "",
            "date_administered": "2026-10-06",
            "next_due_date": "",
            "weight_at_vaccination": "12.50",
            "site_of_injection": "",
            "batch_number": "",
            "manufacturer": "",
            "administered_by": "",
            "correction_reason": "",
        }
        data.update(changes)
        return data

    def _clinic(self, vet="Dr. Edgar Sadiwa"):
        ClinicSettings.objects.create(
            clinic_name="Hapi Vet",
            address="Gasan",
            veterinarian_name=vet,
            opening_time=datetime.time(8, 0),
            closing_time=datetime.time(17, 0),
            same_day_cutoff_time=datetime.time(15, 0),
        )

    def _three_pages(self):
        """(case, url, back url, back label as escaped on the page)."""
        record = self._record(record_date=_days(-8))
        dose = self._dose()
        return [
            (
                "pet page",
                self.new_url,
                reverse("admin_pet_detail", args=[self.pet.pk]),
                "Back to Bantay",
            ),
            (
                "record",
                self._on_record_url(record),
                reverse("admin_medical_record_detail", args=[record.pk]),
                "Back to the record",
            ),
            (
                "edit",
                self._edit_url(dose),
                self.history_url,
                "Back to Bantay&#x27;s vaccinations",
            ),
        ]

    # ─── Layout ───────────────────────────────────────────────────────────────

    def test_new_layout_without_old_files(self):
        for case, url, _back, _label in self._three_pages():
            with self.subTest(case=case):
                response = self.client.get(url)
                self.assertTemplateUsed(response, "layouts/admin.html")
                self.assertTemplateUsed(response, "admin/medical/vaccination_form.html")
                self.assertTemplateNotUsed(
                    response, "admin/medical/vaccination_edit.html"
                )
                for old in (
                    "vaccination_form.css",
                    "layer(legacy)",
                    "admin-page",
                    "medical-form",
                    "form-group",
                    "Custom Vaccine Name",
                    "Date Administered",
                    "Administrative Fields",
                    "Please correct the errors below.",
                ):
                    with self.subTest(old=old):
                        self.assertNotContains(response, old)
                title = (
                    "Edit Bantay's vaccination"
                    if case == "edit"
                    else "New vaccination for Bantay"
                )
                self.assertContains(
                    response, f"<title>{title} — Hapi Vet Admin</title>"
                )
                self.assertContains(response, "js/admin/vaccination_form.js")
                self.assertContains(response, '<div class="mx-auto max-w-3xl">')
                self.assertContains(
                    response,
                    "<noscript><style>[data-vaccine-other] { display: block; }",
                )

    def test_exactly_one_violet_button(self):
        for case, url, _back, _label in self._three_pages():
            with self.subTest(case=case):
                html = self._html(url)
                self.assertEqual(html.count("btn-primary"), 1)
                label = "Save changes" if case == "edit" else "Save vaccination"
                self.assertRegex(
                    html,
                    r'(?s)<button type="submit" class="btn btn-primary min-h-12 '
                    r'w-full font-semibold sm:w-auto">\s*<svg[^>]*>.*?</svg>\s*'
                    rf"{label}</button>",
                )

    def test_sidebar_highlights_vaccinations(self):
        vaccinations = reverse("admin_vaccination_list")
        records = reverse("admin_medical_record_list")
        for case, url, _back, _label in self._three_pages():
            with self.subTest(case=case):
                html = self._html(url)
                self.assertRegex(
                    html, rf'<a href="{vaccinations}"[^>]*aria-current="page">'
                )
                self.assertNotRegex(
                    html, rf'<a href="{records}"[^>]*aria-current="page">'
                )

    # ─── Header, back and Cancel, record line ─────────────────────────────────

    def test_titles_and_subtitles(self):
        html = self._html(self.new_url)
        self.assertIn(
            '<h1 class="text-2xl font-bold text-gray-900">New vaccination for '
            "Bantay</h1>",
            html,
        )
        self.assertIn(
            '<p class="mt-1 text-base text-gray-600">Required: the vaccine and the '
            "date given.</p>",
            html,
        )
        self.assertNotIn("Fix the label details freely.", html)
        html = self._html(self._edit_url(self._dose()))
        self.assertIn(
            '<h1 class="text-2xl font-bold text-gray-900">Edit vaccination · '
            "Bantay</h1>",
            html,
        )
        self.assertIn(
            '<p class="mt-1 text-base text-gray-600">Fix the label details freely. '
            "A change to the vaccine, dates, weight or site is a correction and "
            "needs a reason.</p>",
            html,
        )
        self.assertNotIn("Required: the vaccine and the date given.", html)

    def test_back_and_cancel_go_to_the_same_place(self):
        for case, url, back, label in self._three_pages():
            with self.subTest(case=case):
                html = self._html(url)
                self.assertRegex(
                    html,
                    rf'(?s)<a href="{re.escape(back)}" class="mb-2 [^"]*">'
                    rf"\s*<svg[^>]*>.*?</svg>\s*{label}</a>",
                )
                self.assertIn(
                    f'<a href="{back}" class="btn btn-outline min-h-12 w-full '
                    'font-semibold sm:w-auto">Cancel</a>',
                    html,
                )

    def test_pet_line(self):
        html = self._html(self.new_url)
        self.assertIn('<p class="text-sm text-gray-600">Pet</p>', html)
        self.assertIn(
            '<p class="text-lg font-semibold text-gray-900">Bantay <span '
            'class="text-base font-normal text-gray-700">· Owner: Ana Cruz</span></p>',
            html,
        )
        self.assertIn('aria-label="No photo of Bantay yet"', html)
        self.assertIn("size-10 rounded-xl", html)

    def test_record_line_both_ways(self):
        part_of = (
            '<span>Part of the visit record from <b class="font-semibold">'
            "Mon, Oct 26</b></span>"
        )
        not_part = "<span>Not part of a visit record</span>"
        record = self._record(record_date=_days(-8))
        on_record = self._dose(medical_record=record)
        cases = [
            ("pet page", self.new_url, False),
            ("record", self._on_record_url(record), True),
            ("edit on a record", self._edit_url(on_record), True),
            ("edit", self._edit_url(self._dose()), False),
        ]
        for case, url, linked in cases:
            with self.subTest(case=case):
                html = self._html(url)
                self.assertEqual(part_of in html, linked)
                self.assertEqual(not_part in html, not linked)
                self.assertEqual(
                    "bg-violet-50 px-3 py-2 text-base text-gray-900" in html, linked
                )
                # The record rides along in the POST only on new
                self.assertEqual(
                    f'<input type="hidden" name="record" value="{record.pk}">' in html,
                    case == "record",
                )

    # ─── The vaccination card ─────────────────────────────────────────────────

    def test_select_order_with_other_last(self):
        for case, url, _back, _label in self._three_pages():
            with self.subTest(case=case):
                select = re.search(
                    r'(?s)<select name="vaccine_name"([^>]*)>(.*?)</select>',
                    self._html(url),
                )
                self.assertIn("data-vaccine-choice", select.group(1))
                options = re.findall(
                    r'<option value="([^"]*)"[^>]*>([^<]*)</option>', select.group(2)
                )
                self.assertEqual(
                    options,
                    [
                        ("", "Choose one"),
                        ("Deworming", "Deworming"),
                        ("Ivermectin", "Ivermectin"),
                        ("Anti-rabies", "Anti-rabies"),
                        ("6-in-1", "6-in-1"),
                        ("7-in-1", "7-in-1"),
                        ("8-in-1", "8-in-1"),
                        ("Bordetella", "Bordetella"),
                        ("Other", "Other (type it)"),
                    ],
                )

    def test_vaccine_name_box(self):
        html = self._html(self.new_url)
        self.assertIn(VACCINE_NAME_HIDDEN, html)
        self.assertNotIn(VACCINE_NAME_SHOWN, html)
        self.assertRegex(
            html,
            r'(?s)<label for="id_custom_vaccine_name" [^>]*>\s*Vaccine name '
            r'<span class="text-red-700" aria-hidden="true">\*</span>',
        )
        self.assertIn('placeholder="For example: FVRCP"', html)
        # A re-render with Other chosen opens the box, with its error
        html = self._post(self.new_url, vaccine_name=Vaccination.OTHER)
        self.assertIn(VACCINE_NAME_SHOWN, html)
        self.assertIn('<option value="Other" selected>Other (type it)</option>', html)
        self.assertIn("Type the vaccine name.", html)
        # A listed vaccine keeps it closed after an error elsewhere
        html = self._post(self.new_url, date_administered="2026-11-04")
        self.assertIn(VACCINE_NAME_HIDDEN, html)
        # Edit of a typed vaccine opens it with the saved name
        dose = self._dose(vaccine_name=Vaccination.OTHER, custom_vaccine_name="FVRCP")
        html = self._html(self._edit_url(dose))
        self.assertIn(VACCINE_NAME_SHOWN, html)
        self.assertRegex(
            html, r'<input type="text" name="custom_vaccine_name" value="FVRCP"'
        )

    def test_every_label_and_placeholder(self):
        html = self._html(self.new_url)
        star = r' <span class="text-red-700" aria-hidden="true">\*</span>'
        optional = r' <span class="font-normal text-gray-600">\(optional\)</span>'
        for name, label, mark in (
            ("vaccine_name", "Vaccine", star),
            ("date_administered", "Date given", star),
            ("next_due_date", "Next dose due", optional),
            ("weight_at_vaccination", r"Weight \(kg\)", optional),
            ("site_of_injection", "Where it was given", optional),
            ("batch_number", "Batch / lot no.", optional),
            ("manufacturer", "Maker", optional),
            ("administered_by", "Given by", optional),
        ):
            with self.subTest(name=name):
                self.assertRegex(
                    html, rf'(?s)<label for="id_{name}" [^>]*>\s*{label}{mark}'
                )
        for name, placeholder in (
            ("weight_at_vaccination", "For example: 12.5"),
            ("site_of_injection", "For example: Right hind leg"),
            ("batch_number", "For example: A2311-07"),
            ("manufacturer", "For example: Zoetis"),
        ):
            with self.subTest(name=name):
                self.assertRegex(
                    html, rf'name="{name}"[^>]*placeholder="{placeholder}"'
                )
        for name in ("date_administered", "next_due_date"):
            with self.subTest(name=name):
                self.assertRegex(
                    html, rf'<div class="max-w-xs"><input type="date" name="{name}"'
                )
        for heading in ("The vaccination</h2>", "From the vial label</h2>"):
            with self.subTest(heading=heading):
                self.assertEqual(html.count(heading), 1)

    def test_correction_pill_only_on_edit(self):
        pill = "Changing these is a correction</span>"
        for case, url, _back, _label in self._three_pages():
            with self.subTest(case=case):
                self.assertEqual(pill in self._html(url), case == "edit")

    # ─── Help lines ───────────────────────────────────────────────────────────

    def test_each_date_given_help(self):
        lines = {"pet page": "Today.", "record": "Taken from the record."}
        for case, url, _back, _label in self._three_pages():
            with self.subTest(case=case):
                html = self._html(url)
                for line in lines.values():
                    self.assertEqual(
                        f'<p class="text-sm text-gray-700">{line}</p>' in html,
                        lines.get(case) == line,
                    )

    def test_next_dose_help_when_empty(self):
        for case, url, _back, _label in self._three_pages():
            with self.subTest(case=case):
                self.assertIn(NEXT_DOSE_EMPTY, self._html(url))

    def test_next_dose_help_with_a_value(self):
        dose = self._dose()
        for day, line in (
            (_days(10), "Friday, November 13 · in 10 days"),
            (
                datetime.date(2027, 2, 3),
                "Wednesday, February 3, 2027 · in about 3 months",
            ),
            (
                datetime.date(2027, 11, 3),
                "Wednesday, November 3, 2027 · in about 1 year",
            ),
        ):
            with self.subTest(day=day):
                Vaccination.objects.filter(pk=dose.pk).update(next_due_date=day)
                html = self._html(self._edit_url(dose))
                self.assertIn(f'<p class="text-sm text-gray-700">{line}</p>', html)
                self.assertNotIn(NEXT_DOSE_EMPTY, html)

    def test_next_dose_help_on_a_re_render(self):
        # The typed date, worked out on the server
        html = self._post(
            self.new_url, date_administered="2026-11-04", next_due_date="2026-11-13"
        )
        self.assertIn(
            '<p class="text-sm text-gray-700">Friday, November 13 · in 10 days</p>',
            html,
        )
        self.assertNotIn(NEXT_DOSE_EMPTY, html)

    def test_given_by_prefilled_on_new_not_overwritten_on_edit(self):
        self._clinic()
        record = self._record(record_date=_days(-8))
        for url in (self.new_url, self._on_record_url(record)):
            with self.subTest(url=url):
                html = self._html(url)
                self.assertRegex(
                    html, r'name="administered_by" value="Dr. Edgar Sadiwa"'
                )
                self.assertIn(GIVEN_BY_HELP, html)
                self.assertNotIn(LABEL_CARD_EDIT_LINE, html)
        html = self._html(self._edit_url(self._dose(administered_by="Dr. Reyes")))
        self.assertRegex(html, r'name="administered_by" value="Dr. Reyes"')
        self.assertNotIn("Dr. Edgar Sadiwa", html)
        self.assertNotIn(GIVEN_BY_HELP, html)
        self.assertIn(LABEL_CARD_EDIT_LINE, html)

    # ─── Errors ───────────────────────────────────────────────────────────────

    def test_error_summary_singular_and_plural(self):
        html = self._post(self.new_url, date_administered="2026-11-04")
        self.assertIn(
            '<div class="flex items-start gap-3 rounded-xl border border-red-200 '
            'bg-red-50 px-5 py-4 text-red-900" role="alert">',
            html,
        )
        self.assertIn(
            '<p class="text-lg font-semibold">Please fix the 1 item below.</p>', html
        )
        self.assertIn("Nothing was saved yet.", html)
        self.assertNotIn("Please correct the errors below.", html)

        # The mockup's state 4: Other with no name, a future date given, a
        # next dose before it
        html = self._post(
            self.new_url,
            vaccine_name=Vaccination.OTHER,
            date_administered="2026-11-30",
            next_due_date="2026-11-10",
        )
        self.assertIn(
            '<p class="text-lg font-semibold">Please fix the 3 items below.</p>', html
        )
        self.assertFalse(Vaccination.objects.exists())
        # No summary before a save
        self.assertNotIn('role="alert"', self._html(self.new_url))

    def test_every_error_message(self):
        cases = [
            ({"vaccine_name": ""}, "Choose the vaccine."),
            ({"date_administered": ""}, "Pick the date given."),
            ({"vaccine_name": Vaccination.OTHER}, "Type the vaccine name."),
            (
                {"date_administered": "2026-11-04"},
                "The date given can&#x27;t be in the future.",
            ),
            (
                {"next_due_date": "2026-11-03"},
                "The next dose must be after the date given.",
            ),
        ]
        for data, message in cases:
            with self.subTest(message=message):
                html = self._post(self.new_url, **data)
                self.assertRegex(
                    html,
                    r'(?s)<p class="flex items-start gap-1 text-sm font-medium '
                    rf'text-red-700">\s*<svg[^>]*>.*?</svg>\s*{re.escape(message)}</p>',
                )
                self.assertIn("Please fix the 1 item below.", html)
        self.assertFalse(Vaccination.objects.exists())

    def test_typed_values_are_kept_after_an_error(self):
        html = self._post(
            self.new_url,
            vaccine_name=Vaccination.OTHER,
            custom_vaccine_name="FVRCP",
            date_administered="2026-11-04",
            next_due_date="2027-11-04",
            weight_at_vaccination="3.8",
            site_of_injection="Left shoulder",
            batch_number="A2311-07",
            manufacturer="Zoetis",
            administered_by="Dr. Reyes",
        )
        self.assertIn('<option value="Other" selected>', html)
        for name, value in (
            ("custom_vaccine_name", "FVRCP"),
            ("date_administered", "2026-11-04"),
            ("next_due_date", "2027-11-04"),
            ("weight_at_vaccination", "3.8"),
            ("site_of_injection", "Left shoulder"),
            ("batch_number", "A2311-07"),
            ("manufacturer", "Zoetis"),
            ("administered_by", "Dr. Reyes"),
        ):
            with self.subTest(name=name):
                self.assertRegex(html, rf'name="{name}" value="{re.escape(value)}"')

    # ─── Reason for a correction ──────────────────────────────────────────────

    def test_correction_card_only_on_edit(self):
        for case, url, _back, _label in self._three_pages():
            with self.subTest(case=case):
                html = self._html(url)
                is_edit = case == "edit"
                heading = "Reason for a correction</label></h2>"
                self.assertEqual(heading in html, is_edit)
                self.assertEqual('name="correction_reason"' in html, is_edit)
        html = self._html(self._edit_url(self._dose()))
        self.assertIn(
            '<section class="rounded-xl border-2 border-gray-300 bg-white p-6 '
            'shadow-sm" aria-labelledby="correction-title">',
            html,
        )
        self.assertIn(
            '<p class="mt-1 text-base text-gray-700">Needed only if you change '
            "something in “The vaccination”. The record then shows a "
            '<b class="font-semibold">Corrected</b> tag with this reason and '
            "today's date.</p>",
            html,
        )
        self.assertNotIn("You changed:", html)

    def test_reason_textarea_is_never_prefilled(self):
        dose = self._dose(is_corrected=True, correction_note=OLD_NOTE)
        html = self._html(self._edit_url(dose))
        self.assertRegex(
            html,
            r'<textarea name="correction_reason" cols="40" rows="2" placeholder="For '
            r'example: wrong date typed, it was given the day before" '
            r'id="id_correction_reason">\s*</textarea>',
        )

    def test_red_state_names_one_changed_field(self):
        dose = self._dose(weight_at_vaccination=Decimal("12.50"))
        response = self.client.post(
            self._edit_url(dose), self._edit_data(date_administered="2026-10-05")
        )
        html = response.content.decode()
        self.assertIn(
            '<section class="rounded-xl border-2 border-red-300 bg-white p-6 '
            'shadow-sm" aria-labelledby="correction-title">',
            html,
        )
        self.assertIn(
            '<span>You changed: <b class="font-semibold">Date given</b> (was Tue, '
            "Oct 6).</span>",
            html,
        )
        self.assertIn("bg-yellow-50", html)
        # The 5a-1 error, under the reason
        self.assertRegex(
            html,
            r'(?s)<textarea name="correction_reason"[^>]*>\s*</textarea>\s*'
            r'<p class="flex items-start gap-1 text-sm font-medium text-red-700">'
            r"\s*<svg[^>]*>.*?</svg>\s*You changed the date given "
            r"\(was Tue, Oct 6\)\. Write why, so the change is on record\.</p>",
        )
        self.assertIn("Please fix the 1 item below.", html)
        dose.refresh_from_db()
        self.assertEqual(dose.date_administered, datetime.date(2026, 10, 6))

    def test_red_state_names_two_and_three_changed_fields(self):
        dose = self._dose(weight_at_vaccination=Decimal("12.50"))
        html = self.client.post(
            self._edit_url(dose),
            self._edit_data(date_administered="2026-10-05", weight_at_vaccination="13"),
        ).content.decode()
        self.assertIn(
            '<span>You changed: <b class="font-semibold">Date given</b> (was Tue, '
            'Oct 6) and <b class="font-semibold">Weight (kg)</b> (was 12.5).</span>',
            html,
        )
        html = self.client.post(
            self._edit_url(dose),
            self._edit_data(
                date_administered="2026-10-05",
                weight_at_vaccination="13",
                site_of_injection="Neck",
            ),
        ).content.decode()
        self.assertIn(
            '<span>You changed: <b class="font-semibold">Date given</b> (was Tue, '
            'Oct 6), <b class="font-semibold">Weight (kg)</b> (was 12.5) and '
            '<b class="font-semibold">Where it was given</b> (was empty).</span>',
            html,
        )

    def test_label_fields_alone_are_no_correction(self):
        dose = self._dose(weight_at_vaccination=Decimal("12.50"))
        response = self.client.post(
            self._edit_url(dose), self._edit_data(batch_number="B2")
        )
        self.assertEqual(response.status_code, 302)

    def test_corrected_before_lists_each_dated_line(self):
        dose = self._dose(
            is_corrected=True,
            correction_note=f"{OLD_NOTE}\nThu, Oct 8, 2026: site",
        )
        html = self._html(self._edit_url(dose))
        self.assertIn(
            '<p class="text-sm font-semibold text-gray-800">Corrected before</p>', html
        )
        self.assertIn(f"<li>{OLD_NOTE}</li><li>Thu, Oct 8, 2026: site</li>", html)
        self.assertIn(
            '<p class="mt-2 text-sm text-gray-700">Earlier reasons are kept. A new '
            "one is added below them.</p>",
            html,
        )
        self.assertNotIn("Corrected before", self._html(self._edit_url(self._dose())))

    # ─── After saving ─────────────────────────────────────────────────────────

    def test_after_save_line_matches_the_redirect(self):
        record = self._record(record_date=_days(-8))
        cases = [
            (
                self.new_url,
                "After saving you&#x27;ll see Bantay&#x27;s vaccinations with the "
                "new one highlighted.",
                self.history_url,
            ),
            (
                self._on_record_url(record),
                "After saving you&#x27;ll go back to the record.",
                reverse("admin_medical_record_detail", args=[record.pk]),
            ),
        ]
        for url, line, target in cases:
            with self.subTest(url=url):
                self.assertIn(f"<span>{line}</span>", self._html(url))
                response = self.client.post(
                    url,
                    {
                        "vaccine_name": Vaccination.ANTI_RABIES,
                        "date_administered": "2026-11-03",
                    },
                )
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response["Location"].startswith(target))
        # Edit has no after-save line
        self.assertNotIn("After saving", self._html(self._edit_url(self._dose())))

    # ─── Archived ─────────────────────────────────────────────────────────────

    def test_archived_pet_or_owner_new_refused_edit_renders(self):
        record = self._record(record_date=_days(-8))
        dose = self._dose()
        pet_url = reverse("admin_pet_detail", args=[self.pet.pk])
        for who in ("pet", "owner"):
            with self.subTest(who=who):
                if who == "pet":
                    Pet.objects.filter(pk=self.pet.pk).update(is_archived=True)
                    message = "Restore Bantay first."
                else:
                    Pet.objects.filter(pk=self.pet.pk).update(is_archived=False)
                    self.owner.archive()
                    message = "Restore Ana Cruz first."
                for url in (self.new_url, self._on_record_url(record)):
                    for method in ("get", "post"):
                        self.client.cookies.pop("messages", None)
                        response = getattr(self.client, method)(
                            url,
                            {
                                "vaccine_name": Vaccination.ANTI_RABIES,
                                "date_administered": "2026-11-03",
                            },
                        )
                        self.assertRedirects(
                            response, pet_url, fetch_redirect_response=False
                        )
                        self.assertEqual(self._messages(response), [message])
                self.assertEqual(Vaccination.objects.count(), 1)
                edit = self.client.get(self._edit_url(dose))
                self.assertEqual(edit.status_code, 200)

    # ─── Queries, links ───────────────────────────────────────────────────────

    def _page_queries(self, url):
        self.client.get(url)
        with CaptureQueriesContext(connection) as queries:
            self.client.get(url)
        return [query["sql"] for query in queries]

    def test_query_count_is_flat(self):
        """Each case costs the same with more doses and records for the pet."""
        self._clinic()
        record = self._record(record_date=_days(-8))
        pages = {
            "new from the pet page": self.new_url,
            "new on a record": self._on_record_url(record),
            "edit": self._edit_url(self._dose()),
            "edit on a record": self._edit_url(self._dose(medical_record=record)),
        }
        counts = {case: len(self._page_queries(url)) for case, url in pages.items()}
        for i in range(5):
            self._dose(date_administered=_days(-40 - i), correction_note=OLD_NOTE)
            self._record(record_date=_days(-40 - i))
        for case, url in pages.items():
            with self.subTest(case=case):
                self.assertEqual(len(self._page_queries(url)), counts[case])
        # Session, user, pet + owner, the vet's name, 3 sidebar counts,
        # clinic settings; + the record on a record
        self.assertEqual(counts["new from the pet page"], 8, counts)
        self.assertEqual(counts["new on a record"], 9, counts)
        # Session, user, dose + pet + owner + record, 3 sidebar counts,
        # clinic settings
        self.assertEqual(counts["edit"], 7, counts)
        self.assertEqual(counts["edit on a record"], 7, counts)

    def test_every_link_is_on_one_line(self):
        for case, url, _back, _label in self._three_pages():
            with self.subTest(case=case):
                for tag in re.findall(r"<a\b[^>]*>", self._html(url)):
                    self.assertNotIn("\n", tag)
        html = self._post(self.new_url, date_administered="2026-11-04")
        for tag in re.findall(r"<a\b[^>]*>", html):
            self.assertNotIn("\n", tag)


@PLAIN_STATIC
class GivenByHelpTests(FrozenMedicalTestBase):
    """5b-5: the Given by help on a new vaccination says it was filled in
    only when Clinic settings has the vet's name; otherwise it says where
    to add it."""

    NO_VET_HELP = (
        '<p class="text-sm text-gray-600">Add the vet&#x27;s name in Clinic '
        "settings to fill this in.</p>"
    )

    def setUp(self):
        super().setUp()
        self.url = reverse("admin_vaccination_standalone_create", args=[self.pet.pk])

    def _clinic(self, vet):
        ClinicSettings.objects.create(
            clinic_name="Hapi Vet",
            address="Gasan",
            veterinarian_name=vet,
            opening_time=datetime.time(8, 0),
            closing_time=datetime.time(17, 0),
            same_day_cutoff_time=datetime.time(15, 0),
        )

    def _html(self, method="get", **data):
        response = getattr(self.client, method)(self.url, data)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_prefilled_with_the_vet(self):
        self._clinic("Dr. Edgar Sadiwa")
        record = self._record(record_date=_days(-8))
        for params in ({}, {"record": record.pk}):
            with self.subTest(params=params):
                html = self._html(**params)
                self.assertIn(GIVEN_BY_HELP, html)
                self.assertNotIn(self.NO_VET_HELP, html)

    def test_no_vet_name_says_where_to_add_it(self):
        for vet in (None, ""):
            with self.subTest(vet=vet):
                ClinicSettings.objects.all().delete()
                if vet is not None:
                    self._clinic(vet)
                html = self._html()
                self.assertRegex(html, r'name="administered_by"(?! value=)')
                self.assertIn(self.NO_VET_HELP, html)
                self.assertNotIn(GIVEN_BY_HELP, html)

    def test_re_render_after_errors_keeps_the_rule(self):
        self._clinic("Dr. Edgar Sadiwa")
        html = self._html("post", date_administered="2026-11-03")
        self.assertIn(GIVEN_BY_HELP, html)
        ClinicSettings.objects.all().delete()
        html = self._html("post", date_administered="2026-11-03")
        self.assertIn(self.NO_VET_HELP, html)


# ── Batch 5b-5: the pet's medical history and vaccinations ───────────────────

HEALTH_TAB_ON = (
    "inline-flex min-h-12 items-center gap-2 rounded-lg bg-violet-50 px-4 "
    "text-base font-semibold text-violet-800 ring-2 ring-violet-600"
)
HEALTH_TAB_OFF = (
    "inline-flex min-h-12 items-center gap-2 rounded-lg border border-gray-300 "
    "bg-white px-4 text-base font-medium text-gray-800 hover:bg-gray-50"
)
PET_ARCHIVED_BANNER = (
    "<p><b>Bantay is archived.</b> Everything here is kept and can still be "
    "corrected. Nothing new can be added and no reminders are sent. Restore "
    "Bantay on the pet page to change that.</p>"
)
OWNER_ARCHIVED_BANNER = (
    "<p><b>Bantay's owner, Ana Cruz, is archived.</b> Everything here is "
    "kept and can still be corrected. Nothing new can be added and no "
    "reminders are sent. Restore Ana Cruz on their page to change that.</p>"
)
ARCHIVED_PILL = re.compile(
    r'<span class="inline-flex items-center gap-1\.5 rounded-full bg-gray-100 '
    r'px-2\.5 py-1 text-sm font-medium text-gray-800">\s*<svg(?s:.*?)</svg>\s*'
    r"Archived</span>"
)


class PetHealthPageChecks:
    """Shared checks for the pet's two health pages, mixed into each page's
    test class before FrozenMedicalTestBase (frozen at 00:30 Manila on Tue,
    Nov 3, 2026). The page class sets url_name, title, tab_label, add_label,
    add_url_name, nav_url_name, _make(n) and _make_remindable()."""

    url_name = ""
    title = ""
    tab_label = ""
    add_label = ""
    add_url_name = ""
    nav_url_name = ""

    def setUp(self):
        super().setUp()
        pk = self.pet.pk
        self.url = reverse(self.url_name, args=[pk])
        self.records_url = reverse("admin_pet_medical_history", args=[pk])
        self.vaccinations_url = reverse("admin_pet_vaccination_history", args=[pk])

    def _html(self, url=None, **params):
        response = self.client.get(url or self.url, params)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def _archive(self, who):
        if who == "pet":
            Pet.objects.filter(pk=self.pet.pk).update(is_archived=True)
        else:
            self.owner.archive()

    def _shot(self, given, due=None, **fields):
        fields.setdefault("vaccine_name", Vaccination.ANTI_RABIES)
        return Vaccination.objects.create(
            pet=self.pet,
            date_administered=_days(given),
            next_due_date=None if due is None else _days(due),
            **fields,
        )

    def _tabs(self, html):
        return re.search(
            r'(?s)<nav class="mb-6 flex flex-wrap gap-2" aria-label="Bantay\'s '
            r'health records">.*?</nav>',
            html,
        ).group()

    # ─── Shared tests (run by each page class) ────────────────────────────────

    def test_header_back_link_title_pet_line_and_owner_link(self):
        html = self._html()
        pet_url = reverse("admin_pet_detail", args=[self.pet.pk])
        self.assertRegex(
            html,
            rf'<a href="{pet_url}" class="mb-2 inline-flex min-h-11 items-center '
            r"gap-2 text-base font-medium text-blue-600 underline "
            r'underline-offset-2">\s*<svg(?s:.*?)</svg>\s*Back to Bantay</a>',
        )
        self.assertRegex(
            html,
            r'<h1 class="flex flex-wrap items-center gap-3 text-2xl font-bold '
            rf"text-gray-900\">\s*<span>Bantay's {self.title}</span>\s*</h1>",
        )
        self.assertIn(f"<title>Bantay's {self.title} — Hapi Vet Admin</title>", html)
        self.assertNotRegex(html, ARCHIVED_PILL)
        owner_url = reverse("admin_owner_detail", args=[self.owner.pk])
        self.assertIn(
            '<p class="mt-1 text-base text-gray-700">Dog · Owner: '
            f'<a href="{owner_url}" class="font-medium text-blue-600 underline '
            'underline-offset-2">Ana Cruz</a></p>',
            html,
        )
        # The pet picture is violet (not muted) while active
        self.assertIn("bg-violet-50 text-violet-600 ring-1 ring-violet-200", html)

    def test_breed_and_age_on_the_pet_line(self):
        Pet.objects.filter(pk=self.pet.pk).update(
            breed="Aspin", date_of_birth=datetime.date(2023, 10, 1)
        )
        self.assertIn(
            '<p class="mt-1 text-base text-gray-700">Dog · Aspin · 3 yrs · Owner: ',
            self._html(),
        )

    def test_exactly_one_violet_button(self):
        html = self._html()
        self.assertEqual(_outside_dialogs(html).count("btn-primary"), 1)
        add_url = reverse(self.add_url_name, args=[self.pet.pk])
        self.assertRegex(
            html,
            rf'<a href="{add_url}" class="btn btn-primary w-full font-semibold '
            rf'sm:w-auto">\s*<svg(?s:.*?)</svg>\s*{self.add_label}</a>',
        )

    def test_no_violet_button_when_archived(self):
        self._make_remindable()
        for who in ("pet", "owner"):
            with self.subTest(who=who):
                self._archive(who)
                html = self._html()
                self.assertEqual(html.count("btn-primary"), 0)
                self.assertNotIn(reverse(self.add_url_name, args=[self.pet.pk]), html)
                self.assertNotIn("<dialog", html)
                self.assertNotIn("Send reminder", html)

    def test_tabs_with_counts_and_aria_current(self):
        self._record()
        self._record(record_date=_days(-2))
        self._shot(-3)
        tabs = self._tabs(self._html())
        self.assertEqual(tabs.count("<a "), 2)
        self.assertEqual(tabs.count('aria-current="page"'), 1)
        for url, label, count in (
            (self.records_url, "Medical records", 2),
            (self.vaccinations_url, "Vaccinations", 1),
        ):
            with self.subTest(label=label):
                current = label == self.tab_label
                classes = HEALTH_TAB_ON if current else HEALTH_TAB_OFF
                pill = (
                    "bg-violet-100 text-violet-800"
                    if current
                    else ("bg-gray-100 text-gray-800")
                )
                aria = ' aria-current="page"' if current else ""
                self.assertRegex(
                    tabs,
                    rf'<a href="{url}" class="{classes}"{aria}>\s*<svg(?s:.*?)'
                    rf'</svg>\s*{label}<span class="rounded-full {pill} px-2 '
                    rf'text-sm font-semibold">{count}</span></a>',
                )

    def test_sidebar_highlights_its_section_only(self):
        html = self._html()
        nav_url = reverse(self.nav_url_name)
        self.assertIn(f'<a href="{nav_url}" {ACTIVE_NAV}', html)
        self.assertEqual(html.count(ACTIVE_NAV), 1)
        # The URL names start with admin_pet, but Pets is not highlighted
        self.assertIn(f'<a href="{reverse("admin_pet_list")}" {PLAIN_NAV}', html)

    def test_archived_banners(self):
        html = self._html()
        self.assertNotIn(PET_ARCHIVED_BANNER, html)
        self.assertNotIn(OWNER_ARCHIVED_BANNER, html)
        self._archive("pet")
        html = self._html()
        self.assertIn(PET_ARCHIVED_BANNER, html)
        self.assertNotIn(OWNER_ARCHIVED_BANNER, html)
        self.assertRegex(html, ARCHIVED_PILL)
        self.assertIn("bg-gray-100 text-gray-600 ring-1 ring-gray-300", html)
        Pet.objects.filter(pk=self.pet.pk).update(is_archived=False)
        self._archive("owner")
        html = self._html()
        self.assertIn(OWNER_ARCHIVED_BANNER, html)
        self.assertNotIn(PET_ARCHIVED_BANNER, html)
        # The tag is the pet's; the picture is gray for either
        self.assertNotRegex(html, ARCHIVED_PILL)
        self.assertIn("bg-gray-100 text-gray-600 ring-1 ring-gray-300", html)
        Pet.objects.filter(pk=self.pet.pk).update(is_archived=True)
        html = self._html()
        self.assertIn(PET_ARCHIVED_BANNER, html)
        self.assertIn(OWNER_ARCHIVED_BANNER, html)

    def test_every_link_is_on_one_line(self):
        self._make(3)
        for tag in re.findall(r"<a\b[^>]*>", self._html()):
            self.assertNotIn("\n", tag)

    def _page_queries(self):
        self.client.get(self.url)
        with CaptureQueriesContext(connection) as queries:
            self.client.get(self.url)
        return len(queries)


class PetMedicalHistoryPageTests(PetHealthPageChecks, FrozenMedicalTestBase):
    """admin/medical/pet_history.html on the new layout (Slice 5b-5),
    mockup states 1 and 5."""

    url_name = "admin_pet_medical_history"
    title = "medical history"
    tab_label = "Medical records"
    add_label = "Add medical record"
    add_url_name = "admin_medical_record_create"
    nav_url_name = "admin_medical_record_list"

    def _make_remindable(self):
        self._record(
            record_date=_days(-1), follow_up_required=True, follow_up_date=_days(2)
        )

    def _make(self, n):
        MedicalRecord.objects.all().delete()
        for i in range(n):
            record = self._record(
                record_date=_days(-i - 1),
                diagnosis="Checkup",
                follow_up_required=True,
                follow_up_date=_days(i + 1),
            )
            PrescriptionItem.objects.create(record=record, medicine_name="Amox")
            TestResultFile.objects.create(record=record, file="test_results/a.pdf")
            TestResultFile.objects.create(
                record=record, file="test_results/b.pdf", is_archived=True
            )
            self._shot(-i - 1, due=3, medical_record=record)

    def _visit(self, day, hour):
        return Appointment.objects.create(
            owner=self.owner,
            pet=self.pet,
            date=day,
            time=datetime.time(hour, 0),
            status=Appointment.COMPLETED,
        )

    def _cards(self, html):
        return re.findall(
            r'(?s)<li class="flex flex-col gap-4 rounded-xl border border-gray-200 '
            r'bg-white p-5 shadow-sm sm:flex-row sm:gap-5">.*?</li>',
            html,
        )

    def _banner(self, html):
        match = re.search(
            r'(?s)<div class="mb-6 flex flex-wrap items-center gap-4 rounded-xl '
            r'border-2 border-yellow-300 bg-yellow-50 px-5 py-4">.*?</div>',
            html,
        )
        return match.group() if match else None

    # ─── Layout ───────────────────────────────────────────────────────────────

    def test_new_layout_without_old_files(self):
        self._make(2)
        response = self.client.get(self.url)
        self.assertTemplateUsed(response, "layouts/admin.html")
        self.assertTemplateUsed(response, "admin/medical/pet_history.html")
        for old in (
            "pet_history.js",
            "pet_history.css",
            "record_detail.css",
            "layer(legacy)",
            "admin-page",
            "record-card",
            "followup-badge",
            "+ New Record",
            "Back to Pet",
        ):
            with self.subTest(old=old):
                self.assertNotContains(response, old)

    # ─── Follow-up banner ─────────────────────────────────────────────────────

    def test_banner_shows_the_soonest_follow_up_today_or_later(self):
        self._record(
            record_date=_days(-30),
            diagnosis="Old wound",
            follow_up_required=True,
            follow_up_date=_days(-2),
        )
        self._record(
            record_date=_days(-20),
            diagnosis="Skin rash",
            follow_up_required=True,
            follow_up_date=_days(9),
        )
        soonest = self._record(
            record_date=_days(-6),
            diagnosis="Ear infection, left ear",
            follow_up_required=True,
            follow_up_date=_days(4),
        )
        # Not required: its date doesn't count
        self._record(record_date=_days(-3), follow_up_date=_days(1))
        html = self._html()
        banner = self._banner(html)
        self.assertIn(
            '<p class="min-w-0 flex-1 text-base text-gray-900"><b>Follow-up on '
            "Saturday, November 7</b> (in 4 days), for the ear infection seen on "
            "Wed, Oct 28.</p>",
            banner,
        )
        self.assertRegex(
            banner,
            r'<button type="button" class="btn btn-outline w-full font-semibold '
            rf'sm:w-auto" data-dialog-open="reminder-dialog-{soonest.pk}">\s*<svg'
            r"(?s:.*?)</svg>\s*Send reminder<span class=\"sr-only\"> to Ana Cruz "
            r"about Bantay's follow-up</span></button>",
        )
        self.assertEqual(self.client.get(self.url).context["next_follow_up"], soonest)

    def test_banner_today_with_no_diagnosis_says_visit(self):
        self._record(
            record_date=_days(-1), follow_up_required=True, follow_up_date=TODAY
        )
        self.assertIn(
            "<b>Follow-up on Tuesday, November 3</b> (today), for the visit seen on "
            "Mon, Nov 2.</p>",
            self._banner(self._html()),
        )

    def test_banner_diagnosis_start(self):
        cases = [
            ("UTI; started antibiotics", "UTI"),
            ("Diarrhea for 2 days. Mild dehydration", "diarrhea for 2 days"),
            ("Vomiting\nand fever", "vomiting"),
            (
                "Limping on the right hind leg after a fall from the stairs",
                "limping on the right hind leg after a…",
            ),
        ]
        for diagnosis, about in cases:
            with self.subTest(diagnosis=diagnosis):
                MedicalRecord.objects.all().delete()
                self._record(
                    record_date=_days(-1),
                    diagnosis=diagnosis,
                    follow_up_required=True,
                    follow_up_date=_days(1),
                )
                self.assertIn(f"for the {about} seen on", self._banner(self._html()))

    def test_no_banner_when_only_past_or_none(self):
        self.assertIsNone(self._banner(self._html()))
        self._record(follow_up_required=True, follow_up_date=None)
        self._record(
            record_date=_days(-9), follow_up_required=True, follow_up_date=_days(-1)
        )
        html = self._html()
        self.assertIsNone(self._banner(html))
        self.assertNotIn("<dialog", html)

    def test_no_banner_when_archived(self):
        self._make_remindable()
        for who in ("pet", "owner"):
            with self.subTest(who=who):
                self._archive(who)
                html = self._html()
                self.assertIsNone(self._banner(html))
                self.assertNotIn("Follow-up on", html)

    def test_banner_dialog_posts_return_to_history(self):
        record = self._record(
            record_date=_days(-1), follow_up_required=True, follow_up_date=_days(2)
        )
        Notification.objects.create(
            recipient=self.owner_user,
            notification_type=Notification.FOLLOWUP_REMINDER,
            title="Reminder",
            message="Reminder",
            related_pet=self.pet,
        )
        dialogs = _dialogs(self._html())
        self.assertEqual(len(dialogs), 1)
        dialog = dialogs[0]
        self.assertIn(f'<dialog id="reminder-dialog-{record.pk}"', dialog)
        url = reverse("admin_trigger_followup_reminder", args=[record.pk])
        self.assertIn(f'<form method="post" action="{url}" class="p-6 sm:p-7">', dialog)
        self.assertIn("csrfmiddlewaretoken", dialog)
        self.assertIn('<input type="hidden" name="return_to" value="history">', dialog)
        self.assertNotIn('name="tab"', dialog)
        self.assertIn("Send a follow-up reminder to Ana Cruz?", dialog)
        self.assertIn("Last follow-up reminder for Bantay: Today.", dialog)
        self.assertEqual(dialog.count("btn-primary"), 1)

    # ─── Record cards ─────────────────────────────────────────────────────────

    def test_list_head(self):
        self._record()
        self._record(record_date=_days(-2))
        html = self._html()
        self.assertIn(
            '<h2 class="text-lg font-semibold text-gray-900">All records <span '
            'class="font-normal text-gray-600">(2)</span></h2>',
            html,
        )
        self.assertIn(
            '<p class="text-sm text-gray-600">Newest first · private notes are only '
            "on each record</p>",
            html,
        )

    def test_card_with_everything(self):
        record = self._record(
            record_date=_days(-1),
            appointment=self._visit(_days(-1), 10),
            diagnosis="Ear infection, left ear",
            treatment_given="Cleaned the left ear.",
            follow_up_required=True,
            follow_up_date=_days(2),
        )
        PrescriptionItem.objects.create(record=record, medicine_name="Amox")
        PrescriptionItem.objects.create(record=record, medicine_name="Drops")
        TestResultFile.objects.create(record=record, file="test_results/a.pdf")
        # A removed file is never counted
        TestResultFile.objects.create(
            record=record, file="test_results/b.pdf", is_archived=True
        )
        self._shot(-1, medical_record=record)
        [card] = self._cards(self._html())
        self.assertRegex(
            card,
            r'<span class="bg-gray-700 py-0\.5 text-sm font-semibold text-white">Mon'
            r'</span>\s*<span class="py-1 text-2xl font-bold text-gray-900">2</span>',
        )
        self.assertIn(
            '<h2 class="text-lg font-semibold text-gray-900">Monday, November 2</h2>',
            card,
        )
        self.assertIn(
            '<p class="mt-1 line-clamp-2 text-base text-gray-900"><span '
            'class="text-gray-600">Diagnosis:</span> Ear infection, left ear</p>',
            card,
        )
        self.assertIn(
            '<p class="line-clamp-2 text-base text-gray-800"><span '
            'class="text-gray-600">Treatment:</span> Cleaned the left ear.</p>',
            card,
        )
        self.assertIn(
            "<span>From the visit yesterday at 10:00 AM · 2 medicines · 1 test "
            "result · 1 vaccination</span>",
            card,
        )
        self.assertEqual(_pills(card), [("bg-blue-100", "Follow-up Thu, Nov 5")])
        detail = reverse("admin_medical_record_detail", args=[record.pk])
        self.assertRegex(
            card,
            rf'<a href="{detail}" class="inline-flex min-h-11 w-full items-center '
            r"justify-center gap-2 whitespace-nowrap rounded-lg border "
            r"border-violet-300 bg-violet-50 px-5 text-base font-semibold "
            r"text-violet-800 hover:border-violet-400 hover:bg-violet-100 "
            r'sm:w-auto">Open<span class="sr-only"> record from Monday, November 2'
            r"</span>",
        )

    def test_card_with_nothing_written(self):
        record = self._record(
            record_date=datetime.date(2026, 6, 12),
            follow_up_required=True,
            follow_up_date=datetime.date(2026, 6, 15),
        )
        PrescriptionItem.objects.create(record=record, medicine_name="Amox")
        bare = self._record(record_date=datetime.date(2026, 8, 15))
        first, second = self._cards(self._html())
        self.assertIn(">Saturday, August 15</h2>", first)
        self.assertIn("<span>Not linked to a visit</span>", first)
        self.assertEqual(_pills(first), [])
        self.assertIn(reverse("admin_medical_record_detail", args=[bare.pk]), first)
        self.assertIn(">Friday, June 12</h2>", second)
        self.assertIn(
            '<p class="mt-1 text-base text-gray-600"><span class="text-gray-600">'
            "Diagnosis:</span> No diagnosis written</p>",
            second,
        )
        self.assertIn(
            '<p class="text-base text-gray-600"><span class="text-gray-600">'
            "Treatment:</span> Not written</p>",
            second,
        )
        self.assertIn("<span>Not linked to a visit · 1 medicine</span>", second)
        self.assertEqual(_pills(second), [("bg-blue-100", "Follow-up was Mon, Jun 15")])

    def test_follow_up_pill_wording(self):
        cases = [
            (0, "Follow-up today"),
            (1, "Follow-up tomorrow"),
            (30, "Follow-up Thu, Dec 3"),
            (-1, "Follow-up was yesterday"),
            (None, "Follow-up"),
        ]
        for days, text in cases:
            with self.subTest(days=days):
                MedicalRecord.objects.all().delete()
                self._record(
                    record_date=_days(-5),
                    follow_up_required=True,
                    follow_up_date=None if days is None else _days(days),
                )
                [card] = self._cards(self._html())
                self.assertEqual(_pills(card), [("bg-blue-100", text)])

    def test_newest_first(self):
        old = self._record(record_date=_days(-30))
        new = self._record(record_date=_days(-1))
        cards = self._cards(self._html())
        self.assertIn(reverse("admin_medical_record_detail", args=[new.pk]), cards[0])
        self.assertIn(reverse("admin_medical_record_detail", args=[old.pk]), cards[1])

    def test_private_notes_and_owner_note_never_show(self):
        self._record(
            diagnosis="Checkup",
            private_notes="Owner seemed upset about the bill",
            public_notes="Give the drops twice a day",
            follow_up_required=True,
            follow_up_date=_days(3),
        )
        html = self._html()
        self.assertNotIn("Owner seemed upset", html)
        self.assertNotIn("Give the drops", html)
        self.assertNotIn("Private notes", html)
        self.assertNotIn("Note for the owner", html)

    # ─── Empty states ─────────────────────────────────────────────────────────

    def test_empty_state(self):
        html = self._html()
        self.assertEqual(
            _empty_state(html),
            (
                "No medical records yet",
                "A record is usually added from a visit. Use “Add medical record” "
                "for a check-up written without one.",
            ),
        )
        self.assertNotIn("All records", html)
        for who in ("pet", "owner"):
            with self.subTest(who=who):
                self._archive(who)
                html = self._html()
                self.assertIsNone(_empty_state(html))
                self.assertIn(
                    '<p class="text-lg font-semibold text-gray-900">No medical '
                    "records yet</p>",
                    html,
                )
                self.assertNotIn("Add medical record", html)

    # ─── Reminder landing, queries ────────────────────────────────────────────

    def test_reminder_lands_here_with_the_flash(self):
        record = self._record(
            record_date=_days(-1), follow_up_required=True, follow_up_date=_days(2)
        )
        url = reverse("admin_trigger_followup_reminder", args=[record.pk])
        response = self.client.post(url, {"return_to": "history"}, follow=True)
        self.assertEqual(response.redirect_chain, [(self.url, 302)])
        self.assertTemplateUsed(response, "admin/medical/pet_history.html")
        html = response.content.decode()
        self.assertEqual(html.count("Reminder sent to Ana Cruz (app and email)."), 1)
        self.assertEqual(html.count("<div data-flash"), 1)

    def test_query_count_is_flat(self):
        self._make(3)
        few = self._page_queries()
        self._make(30)
        many = self._page_queries()
        self.assertEqual(few, many)
        # Session, user, pet + owner + user, the two tab counts, the banner's
        # follow-up, the records (counts annotated), the last reminder,
        # 3 sidebar counts, clinic settings
        self.assertEqual(many, 11)


class PetVaccinationsPageTests(PetHealthPageChecks, FrozenMedicalTestBase):
    """admin/medical/vaccination_history.html on the new layout (Slice
    5b-5), mockup states 2–4 and 6."""

    url_name = "admin_pet_vaccination_history"
    title = "vaccinations"
    tab_label = "Vaccinations"
    add_label = "Add vaccination"
    add_url_name = "admin_vaccination_standalone_create"
    nav_url_name = "admin_vaccination_list"

    def _make_remindable(self):
        return self._shot(-300, due=2)

    def _make(self, n):
        Vaccination.objects.all().delete()
        MedicalRecord.objects.all().delete()
        for i in range(n):
            record = self._record(record_date=_days(-i - 1))
            self._shot(
                -i - 1,
                due=i - 5,
                vaccine_name=Vaccination.OTHER,
                custom_vaccine_name=f"Vaccine {i}",
                medical_record=record,
                is_corrected=True,
                correction_note=OLD_NOTE,
            )

    def _named(self, name, given, due=None, **fields):
        return self._shot(
            given,
            due,
            vaccine_name=Vaccination.OTHER,
            custom_vaccine_name=name,
            **fields,
        )

    def _card(self, html, dose):
        return re.search(
            rf'(?s)<li id="vaccination-{dose.pk}" class="[^"]*">.*?</li>', html
        ).group()

    # ─── Layout ───────────────────────────────────────────────────────────────

    def test_new_layout_without_old_files(self):
        self._make(2)
        response = self.client.get(self.url)
        self.assertTemplateUsed(response, "layouts/admin.html")
        self.assertTemplateUsed(response, "admin/medical/vaccination_history.html")
        for old in (
            "vaccination_history.css",
            "layer(legacy)",
            "admin-page",
            "data-table",
            "corrected-badge",
            "?next=history",
            "+ Add Vaccination",
            "Back to Pet",
            # The old hover-only tooltip with the correction note
            f'title="{OLD_NOTE}',
        ):
            with self.subTest(old=old):
                self.assertNotContains(response, old)

    def test_list_head_and_order(self):
        old = self._shot(-40)
        new = self._shot(-2, vaccine_name=Vaccination.DEWORMING)
        html = self._html()
        self.assertIn(
            '<h2 class="text-lg font-semibold text-gray-900">All vaccinations <span '
            'class="font-normal text-gray-600">(2)</span></h2>',
            html,
        )
        self.assertIn('<p class="text-sm text-gray-600">Newest first</p>', html)
        self.assertLess(
            html.index(f'id="vaccination-{new.pk}"'),
            html.index(f'id="vaccination-{old.pk}"'),
        )

    # ─── Pills ────────────────────────────────────────────────────────────────

    def test_every_pill(self):
        later = self._shot(-30, due=335)
        cases = [
            (self._named("Alpha", -300, 0), [("bg-yellow-100", "Due today")]),
            (self._named("Bravo", -300, 1), [("bg-yellow-100", "Due tomorrow")]),
            (self._named("Charlie", -300, 7), [("bg-yellow-100", "Due in 7 days")]),
            # Outside the 7-day window: no pill
            (self._named("Delta", -300, 8), []),
            (self._named("Echo", -300, -1), [("bg-red-100", "1 day overdue")]),
            (self._named("Foxtrot", -300, -52), [("bg-red-100", "52 days overdue")]),
            (
                self._named("Golf", -300, 3, is_corrected=True),
                [("bg-yellow-100", "Due in 3 days"), ("bg-gray-100", "Corrected")],
            ),
            (self._named("Hotel", -300), []),
            # Superseded: overdue by its date, but only "Later dose given"
            (self._shot(-400, due=-35), [("bg-gray-100", "Later dose given")]),
            (later, []),
        ]
        html = self._html()
        for dose, pills in cases:
            with self.subTest(dose=dose.display_vaccine_name, pk=dose.pk):
                self.assertEqual(_pills(self._card(html, dose)), pills)

    def test_no_due_pills_when_archived(self):
        dose = self._named("Alpha", -300, -3, is_corrected=True)
        for who in ("pet", "owner"):
            with self.subTest(who=who):
                self._archive(who)
                card = self._card(self._html(), dose)
                self.assertEqual(_pills(card), [("bg-gray-100", "Corrected")])

    # ─── Details ──────────────────────────────────────────────────────────────

    def test_details_filled_in(self):
        record = self._record(record_date=datetime.date(2026, 10, 6))
        dose = Vaccination.objects.create(
            pet=self.pet,
            vaccine_name=Vaccination.EIGHT_IN_ONE,
            date_administered=datetime.date(2026, 10, 6),
            next_due_date=_days(2),
            weight_at_vaccination=Decimal("12.50"),
            site_of_injection="Right hind leg",
            batch_number="A2311-07",
            medical_record=record,
        )
        card = self._card(self._html(), dose)
        self.assertIn(
            '<h2 class="text-lg font-semibold text-gray-900">8-in-1</h2>', card
        )
        self.assertIn(
            '<p class="mt-1 text-base text-gray-800 sm:hidden">Given Tue, Oct 6 · '
            "Next due Thu, Nov 5</p>",
            card,
        )
        self.assertIn(
            '<dl class="mt-2 grid grid-cols-2 gap-x-4 gap-y-2 lg:grid-cols-5">', card
        )
        dl = re.findall(
            r'<dt class="text-sm text-gray-600">(.*?)</dt>\s*'
            r'<dd class="text-base text-gray-900">(.*?)</dd>',
            card,
        )
        self.assertEqual(
            dl,
            [
                ("Given", "Tue, Oct 6"),
                ("Next due", "Thu, Nov 5"),
                ("Weight", "12.5 kg"),
                ("Where", "Right hind leg"),
                ("Batch / lot", "A2311-07"),
            ],
        )
        detail = reverse("admin_medical_record_detail", args=[record.pk])
        self.assertRegex(
            card,
            r"</svg>\s*Part of the record from "
            rf'<a href="{detail}" class="inline-flex min-h-11 items-center '
            r'font-medium text-blue-600 underline underline-offset-2">Tue, Oct 6</a>',
        )
        self.assertNotIn("Corrected", card)

    def test_details_left_empty(self):
        dose = self._shot(-3)
        card = self._card(self._html(), dose)
        self.assertEqual(
            card.count('<dd class="text-base text-gray-600">Not given</dd>'), 3
        )
        self.assertIn('<dd class="text-base text-gray-600">No next date</dd>', card)
        self.assertIn(
            '<p class="mt-1 text-base text-gray-800 sm:hidden">Given Sat, Oct 31 · '
            '<span class="text-gray-600">No next date</span></p>',
            card,
        )
        self.assertNotIn("Part of the record", card)

    def test_corrected_box_with_two_dated_lines(self):
        dose = self._shot(
            -28,
            is_corrected=True,
            correction_note=f"{OLD_NOTE}\n\nThu, Oct 8, 2026: site",
        )
        card = self._card(self._html(), dose)
        self.assertRegex(
            card,
            r'(?s)<div class="mt-3 rounded-lg border border-gray-200 bg-gray-50 '
            r'px-3 py-2">\s*<p class="flex items-center gap-1\.5 text-sm '
            r'font-semibold text-gray-800">\s*<svg.*?</svg>\s*Corrected</p>\s*'
            rf'<p class="text-base text-gray-800">{re.escape(OLD_NOTE)}</p>\s*'
            r'<p class="text-base text-gray-800">Thu, Oct 8, 2026: site</p>\s*</div>',
        )
        self.assertNotIn("title=", card)

    def test_corrected_with_no_reason_on_record(self):
        dose = self._shot(-28, is_corrected=True)
        self.assertIn(
            '<p class="text-base text-gray-600">No reason was written down.</p>',
            self._card(self._html(), dose),
        )

    # ─── Buttons and dialogs ──────────────────────────────────────────────────

    def test_send_reminder_only_on_remindable_doses(self):
        later = self._shot(-30, due=335)
        # Newest given first, so the due dose's dialog comes first
        due = self._named("Alpha", -300, 2)
        overdue = self._named("Bravo", -301, -4)
        quiet = [
            self._named("Charlie", -300, 30),
            self._named("Delta", -300),
            self._shot(-400, due=-35),
            later,
        ]
        html = self._html()
        for dose in (due, overdue):
            with self.subTest(dose=dose.display_vaccine_name):
                self.assertRegex(
                    self._card(html, dose),
                    r'<button type="button" class="btn btn-outline font-semibold" '
                    rf'data-dialog-open="reminder-dialog-{dose.pk}">\s*<svg(?s:.*?)'
                    r'</svg>\s*<span class="sm:hidden">Remind</span><span '
                    r'class="hidden sm:inline">Send reminder</span><span '
                    rf'class="sr-only"> to Ana Cruz about {dose.display_vaccine_name}'
                    r"</span></button>",
                )
        for dose in quiet:
            with self.subTest(dose=dose.display_vaccine_name, pk=dose.pk):
                self.assertNotIn("data-dialog-open", self._card(html, dose))
        dialogs = _dialogs(html)
        self.assertEqual(
            [re.search(r'<dialog id="([^"]+)"', d).group(1) for d in dialogs],
            [f"reminder-dialog-{due.pk}", f"reminder-dialog-{overdue.pk}"],
        )

    def test_dialog_posts_return_to_history(self):
        dose = self._make_remindable()
        Notification.objects.create(
            recipient=self.owner_user,
            notification_type=Notification.VACCINATION_REMINDER,
            title="Reminder",
            message="Reminder",
            related_pet=self.pet,
        )
        [dialog] = _dialogs(self._html())
        url = reverse("admin_trigger_vaccination_reminder", args=[dose.pk])
        self.assertIn(f'<form method="post" action="{url}" class="p-6 sm:p-7">', dialog)
        self.assertIn("csrfmiddlewaretoken", dialog)
        self.assertIn('<input type="hidden" name="return_to" value="history">', dialog)
        self.assertNotIn('name="tab"', dialog)
        self.assertIn("Send a reminder to Ana Cruz?", dialog)
        self.assertIn(
            "Bantay's <b>Anti-rabies</b> is due on <b>Thursday, November 5</b>.",
            dialog,
        )
        self.assertIn("Last vaccination reminder for Bantay: Today.", dialog)
        self.assertEqual(dialog.count("btn-primary"), 1)

    def test_edit_on_every_dose_archived_included(self):
        doses = [self._make_remindable(), self._named("Alpha", -5)]
        for who in (None, "pet", "owner"):
            with self.subTest(who=who):
                if who:
                    self._archive(who)
                html = self._html()
                for dose in doses:
                    edit = reverse("admin_vaccination_edit", args=[dose.pk])
                    self.assertRegex(
                        self._card(html, dose),
                        rf'<a href="{edit}" class="btn btn-outline[^"]*">\s*<svg'
                        r"(?s:.*?)</svg>\s*Edit<span class=\"sr-only\"> "
                        rf"{dose.display_vaccine_name} given "
                        rf"{friendly_date_on(dose.date_administered, TODAY)}</span></a>",
                    )
                if who:
                    self.assertNotIn("data-dialog-open", html)
                    self.assertEqual(_dialogs(html), [])

    # ─── Highlight ────────────────────────────────────────────────────────────

    def test_highlight_on_the_opened_dose(self):
        opened = self._shot(-30)
        other = self._shot(-10, vaccine_name=Vaccination.DEWORMING)
        html = self._html(highlight=opened.pk)
        card = self._card(html, opened)
        self.assertIn(
            f'<li id="vaccination-{opened.pk}" class="scroll-mt-24 rounded-xl border '
            "border-violet-400 bg-violet-50/60 p-4 shadow-sm ring-2 ring-violet-300 "
            'sm:p-5">',
            card,
        )
        self.assertRegex(
            card,
            r'<p class="mb-2 inline-flex items-center gap-1\.5 rounded-full '
            r'bg-violet-600 px-2\.5 py-1 text-sm font-semibold text-white">\s*<svg'
            r"(?s:.*?)</svg>\s*The one you opened</p>",
        )
        self.assertEqual(html.count("The one you opened"), 1)
        self.assertNotIn("ring-violet-300", self._card(html, other))

    def test_junk_or_another_pets_highlight_is_ignored(self):
        self._shot(-30)
        muning = Pet.objects.create(owner=self.owner, name="Muning", species="Cat")
        theirs = Vaccination.objects.create(
            pet=muning, vaccine_name=Vaccination.DEWORMING, date_administered=_days(-3)
        )
        for value in ("abc", "", "-1", "999999", str(theirs.pk)):
            with self.subTest(value=value):
                html = self._html(highlight=value)
                self.assertNotIn("The one you opened", html)
                self.assertNotIn("ring-violet-300", html)

    def test_every_card_has_its_anchor(self):
        self._make(4)
        html = self._html()
        ids = re.findall(r'<li id="vaccination-(\d+)" class="([^"]*)">', html)
        self.assertEqual(
            sorted(int(pk) for pk, _ in ids),
            sorted(Vaccination.objects.values_list("pk", flat=True)),
        )
        for _pk, classes in ids:
            self.assertTrue(classes.startswith("scroll-mt-24 "), classes)

    # ─── Empty states ─────────────────────────────────────────────────────────

    def test_empty_state(self):
        html = self._html()
        self.assertEqual(
            _empty_state(html),
            (
                "No vaccinations yet",
                "A vaccination is usually added from a visit record. Use “Add "
                "vaccination” for one given without a record.",
            ),
        )
        self.assertNotIn("All vaccinations", html)
        for who in ("pet", "owner"):
            with self.subTest(who=who):
                self._archive(who)
                html = self._html()
                self.assertIsNone(_empty_state(html))
                self.assertIn(
                    '<p class="text-lg font-semibold text-gray-900">No vaccinations '
                    "yet</p>",
                    html,
                )

    # ─── Reminder landing, queries ────────────────────────────────────────────

    def test_reminder_lands_here_with_the_flash(self):
        dose = self._make_remindable()
        url = reverse("admin_trigger_vaccination_reminder", args=[dose.pk])
        response = self.client.post(url, {"return_to": "history"}, follow=True)
        self.assertEqual(response.redirect_chain, [(self.url, 302)])
        self.assertTemplateUsed(response, "admin/medical/vaccination_history.html")
        html = response.content.decode()
        self.assertEqual(html.count("Reminder sent to Ana Cruz (app and email)."), 1)
        self.assertEqual(html.count("<div data-flash"), 1)

    def test_query_count_is_flat(self):
        self._make(3)
        few = self._page_queries()
        self._make(30)
        many = self._page_queries()
        self.assertEqual(few, many)
        # Session, user, pet + owner + user, the two tab counts, the doses
        # (later dose annotated, record selected), the last reminder,
        # 3 sidebar counts, clinic settings
        self.assertEqual(many, 10)
