from datetime import time, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.core import mail
from django.db import connection
from django.http import QueryDict
from django.template.loader import render_to_string
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from appointments.models import Appointment
from pets.forms import AdminOwnerEmailForm
from pets.models import Pet, PetOwner
from pets.templatetags.pet_display import species_icon
from pets.utils import owner_list_context

User = get_user_model()

SEND_FAILED = (
    "Email saved, but the link couldn't be sent. Use Send the link again later."
)


def _messages(response):
    return [str(m) for m in get_messages(response.wsgi_request)]


def _admin(test):
    admin = User.objects.create_user(
        username="admin1", email="admin@example.com", role=User.ADMIN
    )
    test.client.force_login(admin)
    return admin


def _owner(first="Ana", last="Cruz", email="", password=None, **fields):
    """An owner; password=None leaves it unusable (not claimed yet)."""
    user = User.objects.create_user(
        username=f"{first}_{last}".lower(),
        email=email,
        password=password,
        role=User.PET_OWNER,
        is_active=bool(email),
    )
    return PetOwner.objects.create(
        user=user, first_name=first, last_name=last, **fields
    )


def _archive(owner):
    owner.archive()
    owner.refresh_from_db()
    return owner


OWNER_POST = {
    "first_name": "Ana",
    "last_name": "Cruz",
    "contact_number": "09171234567",
    "barangay": "Amoingon",
    "city_municipality": "Boac",
    "province": "Marinduque",
}


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


class AdminOwnerListWalkInButtonTests(TestCase):
    """The owners list's "+ Register Walk-in Client" is a plain link to the
    walk-in New owner page (the owners_list.js modal was removed in 3c-2)."""

    def setUp(self):
        admin = User.objects.create_user(
            username="admin1", email="admin@example.com", role=User.ADMIN
        )
        user = User.objects.create_user(
            username="ana", email="ana@example.com", role=User.PET_OWNER
        )
        PetOwner.objects.create(user=user, first_name="Ana", last_name="Cruz")
        self.client.force_login(admin)

    def test_button_links_to_the_new_owner_page(self):
        response = self.client.get(reverse("admin_owner_list"))
        self.assertContains(
            response,
            f'<a href="{reverse("admin_walkin_create")}" class="btn-primary">'
            "+ Register Walk-in Client</a>",
            html=True,
        )


class BookWalkInLinkTests(TestCase):
    """Owner and pet detail pages link to walk-in step 2 with the owner
    (and pet) chosen, skipping step 1. Hidden when archived."""

    def setUp(self):
        admin = User.objects.create_user(
            username="admin1", email="admin@example.com", role=User.ADMIN
        )
        user = User.objects.create_user(
            username="ana", email="ana@example.com", role=User.PET_OWNER
        )
        self.owner = PetOwner.objects.create(
            user=user, first_name="Ana", last_name="Cruz"
        )
        self.pet = Pet.objects.create(owner=self.owner, name="Bantay", species="Dog")
        self.client.force_login(admin)
        self.visit_url = reverse("admin_walkin_appointment_visit")

    def _owner_page(self):
        return self.client.get(reverse("admin_owner_detail", args=[self.owner.pk]))

    def _pet_page(self):
        return self.client.get(reverse("admin_pet_detail", args=[self.pet.pk]))

    def _owner_link(self):
        return (
            f'<a href="{self.visit_url}?owner={self.owner.pk}" '
            'class="btn-secondary">Book walk-in</a>'
        )

    def _pet_link(self):
        return (
            f'<a href="{self.visit_url}?owner={self.owner.pk}&amp;pet={self.pet.pk}" '
            'class="btn-secondary">Book walk-in</a>'
        )

    def test_owner_page_links_to_the_visit_step(self):
        # Exact raw match: owner_detail.html has a stray </div> (Slice 4),
        # so the page is not valid HTML for html=True
        self.assertContains(self._owner_page(), self._owner_link())

    def test_pet_page_links_to_the_visit_step_with_the_pet(self):
        self.assertContains(self._pet_page(), self._pet_link(), html=True)

    def test_archived_owner_has_no_link(self):
        self.owner.is_archived = True
        self.owner.save()
        for response in (self._owner_page(), self._pet_page()):
            with self.subTest(url=response.wsgi_request.path):
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, "Book walk-in")

    def test_archived_pet_has_no_link(self):
        self.pet.is_archived = True
        self.pet.save()
        response = self._pet_page()
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Book walk-in")


class AddEmailViewTests(TestCase):
    """admin_owner_add_email: login required, archived refused, saved
    before sending."""

    def setUp(self):
        _admin(self)
        self.owner = _owner()
        self.url = reverse("admin_owner_add_email", args=[self.owner.pk])
        self.detail = reverse("admin_owner_detail", args=[self.owner.pk])

    def test_anonymous_is_redirected(self):
        self.client.logout()
        for response in (self.client.get(self.url), self.client.post(self.url)):
            with self.subTest(method=response.wsgi_request.method):
                self.assertRedirects(
                    response, reverse("admin_login"), fetch_redirect_response=False
                )
        self.owner.user.refresh_from_db()
        self.assertEqual(self.owner.user.email, "")

    def test_archived_owner_is_refused(self):
        _archive(self.owner)
        response = self.client.post(self.url, {"email": "ana@example.com"})
        self.assertRedirects(response, self.detail, fetch_redirect_response=False)
        self.assertEqual(
            _messages(response), ["Restore the account first to add an email."]
        )
        self.owner.user.refresh_from_db()
        self.assertEqual(self.owner.user.email, "")
        self.assertEqual(len(mail.outbox), 0)

    def test_success_saves_and_sends_one_email(self):
        response = self.client.post(self.url, {"email": "ana@example.com"})
        self.assertRedirects(response, self.detail, fetch_redirect_response=False)
        self.owner.user.refresh_from_db()
        self.assertEqual(self.owner.user.email, "ana@example.com")
        self.assertTrue(self.owner.user.is_active)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(
            _messages(response),
            [
                "Email added for Ana Cruz. We sent a link to ana@example.com "
                "so they can set a password."
            ],
        )

    def test_send_failure_keeps_the_email_and_warns(self):
        with patch(
            "pets.views.send_claim_email", side_effect=Exception("smtp down")
        ), self.assertLogs("pets.views", "ERROR"):
            response = self.client.post(self.url, {"email": "ana@example.com"})
        self.assertRedirects(response, self.detail, fetch_redirect_response=False)
        self.owner.user.refresh_from_db()
        self.assertEqual(self.owner.user.email, "ana@example.com")
        self.assertTrue(self.owner.user.is_active)
        self.assertEqual(_messages(response), [SEND_FAILED])


class EditOwnerViewTests(TestCase):
    """admin_owner_edit: archived refused, email saved before sending,
    an email can't be cleared."""

    def setUp(self):
        _admin(self)
        self.owner = _owner()
        self.url = reverse("admin_owner_edit", args=[self.owner.pk])
        self.detail = reverse("admin_owner_detail", args=[self.owner.pk])

    def _post(self, email):
        return self.client.post(self.url, {**OWNER_POST, "email": email})

    def test_archived_is_refused(self):
        _archive(self.owner)
        for send in (lambda: self.client.get(self.url), lambda: self._post("a@x.com")):
            # Drop the previous request's unread flash message
            self.client.cookies.pop("messages", None)
            response = send()
            with self.subTest(method=response.wsgi_request.method):
                self.assertRedirects(
                    response, self.detail, fetch_redirect_response=False
                )
                self.assertEqual(
                    _messages(response),
                    ["Restore the account first to change their details."],
                )
        self.owner.user.refresh_from_db()
        self.assertEqual(self.owner.user.email, "")

    def test_first_email_send_failure_keeps_the_email(self):
        with patch(
            "pets.views.send_claim_email", side_effect=Exception("smtp down")
        ), self.assertLogs("pets.views", "ERROR"):
            response = self._post("ana@example.com")
        self.assertRedirects(response, self.detail, fetch_redirect_response=False)
        self.owner.user.refresh_from_db()
        self.assertEqual(self.owner.user.email, "ana@example.com")
        self.assertTrue(self.owner.user.is_active)
        self.assertIn(SEND_FAILED, _messages(response))

    def test_first_email_sends_the_link(self):
        self._post("ana@example.com")
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["ana@example.com"])

    def test_clearing_an_existing_email_is_refused(self):
        self.owner.user.email = "ana@example.com"
        self.owner.user.save()
        response = self._post("")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context["form"].errors["email"],
            ["An email can't be removed once added. Type a new email to change it."],
        )
        self.owner.user.refresh_from_db()
        self.assertEqual(self.owner.user.email, "ana@example.com")

    def test_changing_the_email_works(self):
        self.owner.user.email = "ana@example.com"
        self.owner.user.save()
        response = self._post("ana.cruz@example.com")
        self.assertRedirects(response, self.detail, fetch_redirect_response=False)
        self.owner.user.refresh_from_db()
        self.assertEqual(self.owner.user.email, "ana.cruz@example.com")
        # Only a first email sends the link
        self.assertEqual(len(mail.outbox), 0)


class ResendLinkViewTests(TestCase):
    """admin_owner_resend_link: POST only, only for unclaimed owners."""

    def setUp(self):
        _admin(self)
        self.owner = _owner(email="ana@example.com")
        self.url = reverse("admin_owner_resend_link", args=[self.owner.pk])
        self.detail = reverse("admin_owner_detail", args=[self.owner.pk])

    def test_post_sends_one_email(self):
        response = self.client.post(self.url)
        self.assertRedirects(response, self.detail, fetch_redirect_response=False)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["ana@example.com"])
        self.assertEqual(
            _messages(response), ["Sent the link again to ana@example.com."]
        )

    def test_get_sends_nothing(self):
        response = self.client.get(self.url)
        self.assertRedirects(response, self.detail, fetch_redirect_response=False)
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(_messages(response), [])

    def test_refused_when_not_needed(self):
        no_email = _owner(first="Lito", last="Reyes")
        claimed = _owner(
            first="Ben", last="Santos", email="ben@example.com", password="pw-12345!"
        )
        archived = _archive(_owner(first="Cora", last="Lim", email="cora@example.com"))
        for owner in (archived, no_email, claimed):
            self.client.cookies.pop("messages", None)
            with self.subTest(owner=owner.full_name):
                response = self.client.post(
                    reverse("admin_owner_resend_link", args=[owner.pk])
                )
                self.assertRedirects(
                    response,
                    reverse("admin_owner_detail", args=[owner.pk]),
                    fetch_redirect_response=False,
                )
                self.assertEqual(
                    _messages(response), ["This owner doesn't need a new link."]
                )
        self.assertEqual(len(mail.outbox), 0)

    def test_send_failure_warns(self):
        with patch(
            "pets.views.send_claim_email", side_effect=Exception("smtp down")
        ), self.assertLogs("pets.views", "ERROR"):
            response = self.client.post(self.url)
        self.assertRedirects(response, self.detail, fetch_redirect_response=False)
        self.assertEqual(
            _messages(response),
            ["The email couldn't be sent. Check the address, or try again later."],
        )


class RestoreArchiveTests(TestCase):
    """PetOwner.archive() / restore() and the restore message."""

    def setUp(self):
        _admin(self)

    def _restore(self, owner):
        return self.client.post(reverse("admin_owner_restore", args=[owner.pk]))

    def test_archive_sets_both_flags(self):
        owner = _archive(_owner(email="ana@example.com"))
        self.assertTrue(owner.is_archived)
        self.assertIsNotNone(owner.archived_at)
        owner.user.refresh_from_db()
        self.assertFalse(owner.user.is_active)

    def test_archive_leaves_pets_alone(self):
        owner = _owner(email="ana@example.com")
        pet = Pet.objects.create(owner=owner, name="Bantay", species="Dog")
        _archive(owner)
        pet.refresh_from_db()
        self.assertFalse(pet.is_archived)

    def test_restore_without_email_stays_inactive(self):
        owner = _archive(_owner())
        response = self._restore(owner)
        owner.refresh_from_db()
        owner.user.refresh_from_db()
        self.assertFalse(owner.is_archived)
        self.assertIsNone(owner.archived_at)
        self.assertFalse(owner.user.is_active)
        self.assertEqual(
            _messages(response),
            [
                "Ana Cruz's account has been restored. They still can't log in "
                "until they have an email and set a password."
            ],
        )

    def test_restore_with_email_becomes_active(self):
        owner = _archive(_owner(email="ana@example.com"))
        response = self._restore(owner)
        owner.user.refresh_from_db()
        self.assertTrue(owner.user.is_active)
        self.assertEqual(_messages(response), ["Ana Cruz's account has been restored."])


class AddPetArchivedOwnerTests(TestCase):

    def test_refused(self):
        _admin(self)
        owner = _archive(_owner())
        response = self.client.post(
            reverse("admin_pet_add", args=[owner.pk]),
            {"name": "Bantay", "species": "Dog", "gender": "unknown"},
        )
        self.assertRedirects(
            response,
            reverse("admin_owner_detail", args=[owner.pk]),
            fetch_redirect_response=False,
        )
        self.assertEqual(
            _messages(response), ["Restore the account first to add a pet."]
        )
        self.assertFalse(Pet.objects.exists())


class AdminOwnerCreateTests(TestCase):
    """Add owner (no booking): User + PetOwner only."""

    def setUp(self):
        _admin(self)
        self.url = reverse("admin_owner_create")

    def _post(self, email="", **fields):
        return self.client.post(self.url, {**OWNER_POST, "email": email, **fields})

    def test_get_renders_the_form(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form_title"], "Add owner")
        self.assertEqual(response.context["submit_label"], "Add owner")

    def test_creates_user_and_owner_only(self):
        response = self._post()
        owner = PetOwner.objects.get()
        self.assertRedirects(
            response,
            reverse("admin_owner_detail", args=[owner.pk]),
            fetch_redirect_response=False,
        )
        self.assertEqual(owner.user.role, User.PET_OWNER)
        self.assertFalse(owner.user.has_usable_password())
        self.assertTrue(owner.user.is_onboarded)
        self.assertEqual(owner.barangay, "Amoingon")
        self.assertFalse(Pet.objects.exists())
        self.assertFalse(Appointment.objects.exists())

    def test_with_email_sends_one_email_and_is_active(self):
        response = self._post("Ana@Example.com")
        owner = PetOwner.objects.get()
        self.assertEqual(owner.user.email, "ana@example.com")
        self.assertTrue(owner.user.is_active)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["ana@example.com"])
        self.assertEqual(
            _messages(response),
            [
                "Ana Cruz added. We sent a link to ana@example.com "
                "so they can set a password."
            ],
        )

    def test_without_email_is_inactive(self):
        response = self._post()
        owner = PetOwner.objects.get()
        self.assertFalse(owner.user.is_active)
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(_messages(response), ["Ana Cruz added."])

    def test_send_failure_keeps_the_owner_and_warns(self):
        with patch(
            "pets.views.send_claim_email", side_effect=Exception("smtp down")
        ), self.assertLogs("pets.views", "ERROR"):
            response = self._post("ana@example.com")
        owner = PetOwner.objects.get()
        self.assertRedirects(
            response,
            reverse("admin_owner_detail", args=[owner.pk]),
            fetch_redirect_response=False,
        )
        self.assertEqual(owner.user.email, "ana@example.com")
        self.assertTrue(owner.user.is_active)
        self.assertEqual(_messages(response), ["Ana Cruz added.", SEND_FAILED])

    def test_second_owner_without_email_gets_the_plain_message(self):
        self._post()
        self.client.cookies.pop("messages", None)
        with self.assertLogs("pets.views", "ERROR"):
            response = self._post(first_name="Ben", last_name="Santos")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(PetOwner.objects.count(), 1)
        self.assertEqual(User.objects.filter(role=User.PET_OWNER).count(), 1)
        self.assertEqual(
            _messages(response),
            [
                "Only one owner without an email can be saved for now. "
                "Please add an email address for this owner."
            ],
        )


class OwnerListViewTests(TestCase):
    """admin_owner_list: tabs, search, order, paging, counts."""

    def setUp(self):
        _admin(self)
        self.url = reverse("admin_owner_list")

    def _get(self, **params):
        return self.client.get(self.url, params)

    def _names(self, response):
        return [owner.full_name for owner in response.context["owners"]]

    def _many(self, count, start=0):
        for i in range(start, start + count):
            _owner(first=f"Owner{i:02d}", last="Test", email=f"o{i}@example.com")

    def test_25_per_page_and_page_2(self):
        self._many(30)
        first = self._get()
        self.assertEqual(len(first.context["owners"]), 25)
        self.assertEqual(first.context["page_obj"].number, 1)
        self.assertEqual(first.context["prev_page_query"], "")
        self.assertEqual(first.context["next_page_query"], "?status=active&page=2")
        second = self._get(page=2)
        self.assertEqual(len(second.context["owners"]), 5)
        self.assertEqual(second.context["owners"][0].first_name, "Owner25")
        self.assertEqual(second.context["prev_page_query"], "?status=active&page=1")
        self.assertEqual(second.context["next_page_query"], "")
        self.assertEqual(second.context["total_count"], 30)

    def test_pager_keeps_status_and_q(self):
        self._many(30)
        for owner in PetOwner.objects.all():
            owner.archive()
        response = self._get(status="archived", q="test")
        self.assertEqual(
            response.context["next_page_query"], "?status=archived&q=test&page=2"
        )

    def test_first_name_order(self):
        for first, last in [("Cora", "Abad"), ("Ana", "Zamora"), ("Ana", "Bautista")]:
            _owner(first=first, last=last, email=f"{first}.{last}@example.com")
        self.assertEqual(
            self._names(self._get()), ["Ana Bautista", "Ana Zamora", "Cora Abad"]
        )

    def test_archived_tab_order_and_counts(self):
        _owner(first="Active", last="One")
        older = _owner(first="Older", last="Two", email="older@example.com")
        newer = _owner(first="Newer", last="Three", email="newer@example.com")
        older.archive()
        newer.archive()
        PetOwner.objects.filter(pk=older.pk).update(
            archived_at=timezone.now() - timedelta(days=3)
        )
        response = self._get(status="archived")
        self.assertEqual(response.context["active_tab"], "archived")
        self.assertEqual(response.context["status_filter"], "archived")
        self.assertEqual(self._names(response), ["Newer Three", "Older Two"])
        self.assertEqual(response.context["active_count"], 1)
        self.assertEqual(response.context["archived_count"], 2)

    def test_unknown_status_is_active(self):
        _owner()
        response = self._get(status="bogus")
        self.assertEqual(response.context["active_tab"], "active")
        self.assertEqual(self._names(response), ["Ana Cruz"])

    def test_search(self):
        _owner(contact_number="0917 123 4567")
        _owner(
            first="Ben",
            last="Santos",
            email="ben.s@example.com",
            contact_number="09981112222",
        )
        _owner(
            first="Ana",
            last="Reyes",
            email="reyes@example.com",
            contact_number="09180000000",
        )
        cases = {
            "09171234567": ["Ana Cruz"],
            "0917 123 4567": ["Ana Cruz"],
            "0998 111": ["Ben Santos"],
            "ben.s@example": ["Ben Santos"],
            "ana reyes": ["Ana Reyes"],
            "ana": ["Ana Cruz", "Ana Reyes"],
        }
        for q, expected in cases.items():
            with self.subTest(q=q):
                response = self._get(q=q)
                self.assertEqual(self._names(response), expected)
                self.assertTrue(response.context["is_searching"])
                self.assertEqual(response.context["q"], q)

    def test_search_stays_inside_the_tab_and_counts_ignore_it(self):
        _owner()
        _owner(first="Ben", last="Santos", email="ben@example.com")
        _archive(_owner(first="Ana", last="Lim", email="ana.lim@example.com"))
        response = self._get(q="ana")
        self.assertEqual(self._names(response), ["Ana Cruz"])
        self.assertEqual(response.context["active_count"], 2)
        self.assertEqual(response.context["archived_count"], 1)
        self.assertEqual(response.context["total_count"], 1)
        self.assertFalse(self._get().context["is_searching"])

    def test_active_pet_count_excludes_archived_pets(self):
        owner = _owner()
        Pet.objects.create(owner=owner, name="Muning", species="Cat")
        Pet.objects.create(owner=owner, name="Bantay", species="Dog")
        Pet.objects.create(owner=owner, name="Old", species="Dog", is_archived=True)
        row = self._get().context["owners"][0]
        self.assertEqual(row.active_pet_count, 2)
        self.assertEqual([pet.name for pet in row.active_pets], ["Bantay", "Muning"])

    def test_old_search_param_still_works(self):
        _owner()
        _owner(first="Ben", last="Santos", email="ben@example.com")
        response = self._get(search="ben")
        self.assertEqual(self._names(response), ["Ben Santos"])
        self.assertEqual(response.context["search_query"], "ben")
        # q wins when both are sent
        self.assertEqual(self._names(self._get(search="ben", q="ana")), ["Ana Cruz"])

    def test_query_count_is_flat(self):
        """The list's own queries don't grow with the number of owners.
        Measured on owner_list_context plus each row's email, pet count and
        pets: the old template's {{ owner.pets.count }} still costs one
        query per row until the 4a-3 redesign."""

        def run():
            context = owner_list_context(QueryDict("q=test"))
            for owner in context["owners"]:
                (owner.user.email, owner.active_pet_count, owner.active_pets)

        def make(count, start):
            self._many(count, start)
            for owner in PetOwner.objects.filter(pets__isnull=True):
                Pet.objects.create(owner=owner, name="Bantay", species="Dog")

        make(3, 0)
        with CaptureQueriesContext(connection) as small:
            run()
        make(27, 3)
        with self.assertNumQueries(len(small)):
            run()


class OwnerDetailContextTests(TestCase):
    """admin_owner_detail: context for the 4a-3 redesign."""

    def setUp(self):
        _admin(self)
        self.owner = _owner(email="ana@example.com")
        self.pet = Pet.objects.create(owner=self.owner, name="Bantay", species="Dog")
        self.today = timezone.localdate()

    def _visit(self, days, status=Appointment.CONFIRMED, hour=9):
        return Appointment.objects.create(
            owner=self.owner,
            pet=self.pet,
            date=self.today + timedelta(days=days),
            time=time(hour, 0),
            status=status,
        )

    def _context(self, owner=None):
        url = reverse("admin_owner_detail", args=[(owner or self.owner).pk])
        return self.client.get(url).context

    def test_recent_visits_are_the_5_newest(self):
        visits = [self._visit(-days) for days in range(1, 8)]
        later_same_day = self._visit(-1, hour=15)
        recent = list(self._context()["recent_visits"])
        self.assertEqual(recent, [later_same_day] + visits[:4])

    def test_account_states_and_can_resend(self):
        claimed = _owner(
            first="Ben", last="Santos", email="ben@example.com", password="pw-12345!"
        )
        no_email = _owner(first="Lito", last="Reyes")
        archived = _archive(_owner(first="Cora", last="Lim", email="cora@example.com"))
        cases = [
            (self.owner, "unclaimed", True),
            (claimed, "active", False),
            (no_email, "no_email", False),
            (archived, "archived", False),
        ]
        for owner, state, can_resend in cases:
            with self.subTest(state=state):
                context = self._context(owner)
                self.assertEqual(context["account_state"], state)
                self.assertEqual(context["can_resend"], can_resend)

    def test_upcoming_visits_leave_out_past_and_cancelled(self):
        self._visit(-1)
        self._visit(2, status=Appointment.CANCELLED)
        self._visit(3, status=Appointment.COMPLETED)
        later = self._visit(5, status=Appointment.PENDING)
        today = self._visit(0)
        context = self._context()
        self.assertEqual(context["upcoming_visits"], [today, later])
        self.assertEqual(context["next_upcoming_visit"], today)
        self.assertEqual(context["upcoming_count"], 2)

    def test_visits_query_and_pet_keys(self):
        Pet.objects.create(
            owner=self.owner, name="Old", species="Dog", is_archived=True
        )
        context = self._context()
        self.assertEqual(context["visits_query"], "status=all&q=Ana+Cruz")
        self.assertEqual(list(context["active_pets"]), [self.pet])
        self.assertEqual(list(context["pets"]), [self.pet])
        self.assertEqual([pet.name for pet in context["archived_pets"]], ["Old"])
