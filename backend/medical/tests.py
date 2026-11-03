from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from pets.models import Pet, PetOwner

from .models import MedicalRecord, Vaccination

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
