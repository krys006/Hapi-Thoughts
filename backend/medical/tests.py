import datetime
from decimal import Decimal
from unittest import mock
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.test import SimpleTestCase, TestCase
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from pets.models import Pet, PetOwner

from .forms import VaccinationEditForm, VaccinationForm
from .models import MedicalRecord, Vaccination
from .utils import save_vaccination_edit

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

    def test_archived_pet(self):
        Pet.objects.filter(pk=self.pet.pk).update(is_archived=True)
        self._check_refused("Restore Bantay first.")

    def test_archived_owner(self):
        self.owner.archive()
        self._check_refused("Restore Ana Cruz first.")

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
