from django.contrib.auth import get_user_model
from django.core import mail
from django.template.loader import render_to_string
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from pets.forms import AdminOwnerEmailForm
from pets.models import Pet, PetOwner
from pets.templatetags.pet_display import species_icon

User = get_user_model()


class SpeciesIconTests(SimpleTestCase):

    def test_each_rule(self):
        cases = {
            "Dog": "dog",
            "puppy": "dog",
            "Cat": "cat",
            "kitten": "cat",
            "Rabbit": "rabbit",
            "bunny": "rabbit",
            "Bird": "bird",
            "parrot": "bird",
            "chicken": "bird",
            "Lovebird": "bird",
        }
        for species, icon in cases.items():
            with self.subTest(species=species):
                self.assertEqual(species_icon(species), icon)

    def test_plurals(self):
        cases = {
            "dogs": "dog",
            "Puppies": "dog",
            "cats": "cat",
            "kittens": "cat",
            "rabbits": "rabbit",
            "Bunnies": "rabbit",
            "birds": "bird",
            "parrots": "bird",
            "chickens": "bird",
            "Lovebirds": "bird",
        }
        for species, icon in cases.items():
            with self.subTest(species=species):
                self.assertEqual(species_icon(species), icon)

    def test_mixed_case_and_extra_words(self):
        self.assertEqual(species_icon("Dog (Aspin)"), "dog")
        self.assertEqual(species_icon("PERSIAN CAT"), "cat")

    def test_empty_and_unknown_get_the_paw(self):
        for species in ("", None, "Hamster", "turtle"):
            with self.subTest(species=species):
                self.assertEqual(species_icon(species), "paw-print")

    def test_whole_words_only(self):
        cases = {
            "Persian cat": "cat",
            "Cat (Puspin)": "cat",
            "Mixed breed dog": "dog",
            "catfish": "paw-print",
            "Bulldogge": "paw-print",
            "Scatter": "paw-print",
            "Hotdog": "paw-print",
        }
        for species, icon in cases.items():
            with self.subTest(species=species):
                self.assertEqual(species_icon(species), icon)


class PetAvatarTests(SimpleTestCase):
    """components/pet_avatar.html: photo, or a species icon tile."""

    def _avatar(self, pet, size):
        return render_to_string(
            "components/pet_avatar.html", {"pet": pet, "size": size}
        )

    def test_photo(self):
        pet = Pet(name="Brownie", species="Dog", photo="pets/brownie.jpg")
        html = self._avatar(pet, "lg")
        self.assertRegex(html, r'src="[^"]*pets/brownie\.jpg"')
        self.assertIn('alt="Photo of Brownie"', html)
        self.assertIn('loading="lazy"', html)
        self.assertIn("object-cover", html)
        self.assertNotIn("No photo", html)
        self.assertNotIn("<svg", html)

    def test_placeholder_has_label_and_thin_icon(self):
        html = self._avatar(Pet(name="Kitty", species="Persian cat"), "lg")
        self.assertNotIn("<img", html)
        self.assertIn('role="img" aria-label="No photo of Kitty yet"', html)
        self.assertIn("bg-violet-50 text-violet-600 ring-1 ring-violet-200", html)
        self.assertIn('stroke-width="1.5"', html)

    def test_icon_follows_species(self):
        cases = {
            "Dog": '<path d="M11.25 16.25h1.5L12 17z" />',
            "Cat": '<path d="M11.25 16.25h1.5L12 17l-.75-.75Z" />',
            "Rabbit": '<path d="M13 16a3 3 0 0 1 2.24 5" />',
            "Parrot": '<path d="M16 7h.01" />',
            "Hamster": '<circle cx="11" cy="4" r="2" />',
        }
        for species, shape in cases.items():
            with self.subTest(species=species):
                html = self._avatar(Pet(name="Max", species=species), "sm")
                self.assertIn(shape, html)

    def test_sizes(self):
        cases = {
            "lg": ("size-16 rounded-2xl", 'class="size-9 shrink-0"'),
            "sm": ("size-10 rounded-xl", 'class="size-6 shrink-0"'),
        }
        for size, (box, icon) in cases.items():
            with self.subTest(size=size):
                tile = self._avatar(Pet(name="Max", species="Dog"), size)
                self.assertIn(box, tile)
                self.assertIn(icon, tile)
                photo = self._avatar(
                    Pet(name="Max", species="Dog", photo="pets/max.jpg"), size
                )
                self.assertIn(box, photo)


class OwnerAvatarTests(SimpleTestCase):
    """components/owner_avatar.html: profile photo, or initials in a circle."""

    def _avatar(self, owner, size="sm"):
        return render_to_string(
            "components/owner_avatar.html", {"owner": owner, "size": size}
        )

    def test_photo(self):
        owner = PetOwner(
            first_name="Ana", last_name="Cruz", profile_photo="owners/ana.jpg"
        )
        html = self._avatar(owner)
        self.assertRegex(html, r'src="[^"]*owners/ana\.jpg"')
        self.assertIn('alt="Ana Cruz"', html)
        self.assertIn("rounded-full object-cover", html)
        self.assertNotIn(">AC<", html)

    def test_initials(self):
        html = self._avatar(PetOwner(first_name="ana", last_name="cruz"))
        self.assertNotIn("<img", html)
        self.assertIn(">AC</span>", html)
        self.assertIn('aria-label="ana cruz"', html)
        self.assertIn("bg-violet-50 font-semibold", html)
        self.assertIn("text-violet-700 ring-1 ring-violet-200", html)

    def test_one_letter_when_last_name_is_blank(self):
        html = self._avatar(PetOwner(first_name="Lito", last_name=""))
        self.assertIn(">L</span>", html)
        self.assertIn('aria-label="Lito"', html)

    def test_sizes(self):
        owner = PetOwner(first_name="Ana", last_name="Cruz")
        self.assertIn("size-10 text-base", self._avatar(owner, "sm"))
        self.assertIn("size-8 text-sm", self._avatar(owner, "xs"))


class AdminOwnerEmailFormTests(TestCase):
    """AdminOwnerEmailForm (add an email to a walk-in owner) and its view."""

    def setUp(self):
        self.walk_in = User.objects.create_user(
            username="walkin", email="", role=User.PET_OWNER, is_active=False
        )
        self.owner = PetOwner.objects.create(
            user=self.walk_in, first_name="Ana", last_name="Cruz"
        )

    def _form(self, email):
        return AdminOwnerEmailForm({"email": email}, current_user=self.walk_in)

    def test_mixed_case_email_cleaned_lowercase(self):
        form = self._form(" Ana.Cruz@Example.COM ")
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data["email"], "ana.cruz@example.com")

    def test_duplicate_email_in_another_case_refused(self):
        User.objects.create_user(
            username="ana", email="ana@example.com", role=User.PET_OWNER
        )
        form = self._form("ANA@example.com")
        self.assertFalse(form.is_valid())
        self.assertEqual(
            form.errors["email"], ["This email is already used by another account."]
        )

    def test_view_saves_lowercase_and_sends_claim_email(self):
        admin = User.objects.create_user(
            username="admin1", email="admin@example.com", role=User.ADMIN
        )
        self.client.force_login(admin)
        self.client.post(
            reverse("admin_owner_add_email", args=[self.owner.pk]),
            {"email": "Ana@Example.com"},
        )
        self.walk_in.refresh_from_db()
        self.assertEqual(self.walk_in.email, "ana@example.com")
        self.assertTrue(self.walk_in.is_active)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["ana@example.com"])


class AdminPetOwnerFormEmailTests(TestCase):
    """AdminPetOwnerForm email (owner edit page)."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="ana", email="ana@example.com", role=User.PET_OWNER
        )
        self.owner = PetOwner.objects.create(
            user=self.user, first_name="Ana", last_name="Cruz"
        )
        admin = User.objects.create_user(
            username="admin1", email="admin@example.com", role=User.ADMIN
        )
        self.client.force_login(admin)

    def _post(self, email):
        return self.client.post(
            reverse("admin_owner_edit", args=[self.owner.pk]),
            {
                "first_name": "Ana",
                "last_name": "Cruz",
                "contact_number": "09171234567",
                "barangay": "Amoingon",
                "city_municipality": "Boac",
                "province": "Marinduque",
                "email": email,
            },
        )

    def test_mixed_case_email_saved_lowercase(self):
        response = self._post(" Ana.Cruz@Example.COM ")
        self.assertRedirects(
            response,
            reverse("admin_owner_detail", args=[self.owner.pk]),
            fetch_redirect_response=False,
        )
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "ana.cruz@example.com")

    def test_own_email_in_another_case_is_not_a_duplicate(self):
        self._post("ANA@example.com")
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "ana@example.com")

    def test_duplicate_email_in_another_case_refused(self):
        User.objects.create_user(
            username="ben", email="ben@example.com", role=User.PET_OWNER
        )
        response = self._post("BEN@Example.com")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context["form"].errors["email"],
            ["This email is already used by another account."],
        )
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "ana@example.com")
