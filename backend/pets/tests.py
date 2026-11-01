from django.template.loader import render_to_string
from django.test import SimpleTestCase

from pets.models import Pet, PetOwner
from pets.templatetags.pet_display import species_icon


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
