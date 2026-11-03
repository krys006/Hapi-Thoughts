import datetime
from decimal import Decimal
from unittest import mock
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.http import QueryDict
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from appointments.models import Appointment
from notifications.models import Notification
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
    record_list_context,
    save_vaccination_edit,
    vaccination_list_context,
    vaccinations_due,
    vaccinations_overdue,
)

User = get_user_model()


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
            (
                reverse("admin_vaccination_add", args=[self.record.pk]),
                shot,
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
        self.assertContains(response, "Changed:")
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

    def test_upload_view_flashes_the_real_error(self):
        record = self._record()
        url = reverse("admin_test_result_file_upload", args=[record.pk])
        upload = SimpleUploadedFile("notes.docx", b"x", "application/msword")
        response = self.client.post(url, {"file": upload})
        self.assertRedirects(
            response,
            reverse("admin_medical_record_detail", args=[record.pk]),
            fetch_redirect_response=False,
        )
        self.assertEqual(
            self._messages(response), ["Only PDF, JPG or PNG files can be added."]
        )
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
        self.assertEqual(self._send(dose), ["Reminder sent to Ana Cruz."])
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

    def test_old_template_still_renders(self):
        self._dose(3, vaccine_name=Vaccination.DEWORMING)
        response = self.client.get(self.url, {"search": "bantay"})
        self.assertContains(response, "Bantay")
        self.assertContains(response, "Deworming")
        self.assertContains(response, "1 record")

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
