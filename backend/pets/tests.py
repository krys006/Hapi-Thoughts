import re
from datetime import datetime, time, timedelta
from datetime import timezone as dt_timezone
from unittest.mock import patch

from allauth.socialaccount.models import SocialAccount
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
from billing.models import Service
from pets.forms import AdminOwnerEmailForm
from pets.models import Pet, PetDeletionRequest, PetOwner
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


def _google_owner(first="Gina", last="Ramos"):
    """Signed up with Google: a SocialAccount row and no usable password."""
    owner = _owner(first=first, last=last, email=f"{first.lower()}@gmail.com")
    SocialAccount.objects.create(user=owner.user, provider="google", uid=first)
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

    def test_muted_is_a_gray_tile_and_leaves_a_photo_alone(self):
        def muted(pet):
            return render_to_string(
                "components/pet_avatar.html",
                {"pet": pet, "size": "sm", "muted": True},
            )

        tile = muted(Pet(name="Max", species="Dog"))
        self.assertIn("bg-gray-100 text-gray-600 ring-1 ring-gray-300", tile)
        self.assertIn('aria-label="No photo of Max yet"', tile)
        self.assertNotIn("violet", tile)
        photo_pet = Pet(name="Max", species="Dog", photo="pets/max.jpg")
        self.assertEqual(muted(photo_pet), self._avatar(photo_pet, "sm"))


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

    def test_muted_is_a_gray_tile(self):
        html = render_to_string(
            "components/owner_avatar.html",
            {"owner": PetOwner(first_name="Ana", last_name="Cruz"), "muted": True},
        )
        self.assertIn(">AC</span>", html)
        self.assertIn("bg-gray-100 font-semibold", html)
        self.assertIn("text-gray-700 ring-1 ring-gray-300", html)
        self.assertNotIn("violet", html)

    def test_lg_size(self):
        tile = self._avatar(PetOwner(first_name="Ana", last_name="Cruz"), "lg")
        self.assertIn("size-16 text-xl", tile)
        self.assertIn(">AC</span>", tile)
        photo = self._avatar(
            PetOwner(first_name="Ana", last_name="Cruz", profile_photo="o/a.jpg"),
            "lg",
        )
        self.assertIn('class="size-16 shrink-0 rounded-full object-cover"', photo)


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


class AdminOwnerListAddOwnerButtonTests(TestCase):
    """The owners list's only primary button is Add owner (Slice 4a-2).
    "+ Register Walk-in Client" is gone; walk-ins start on Appointments."""

    def setUp(self):
        admin = User.objects.create_user(
            username="admin1", email="admin@example.com", role=User.ADMIN
        )
        user = User.objects.create_user(
            username="ana", email="ana@example.com", role=User.PET_OWNER
        )
        PetOwner.objects.create(user=user, first_name="Ana", last_name="Cruz")
        self.client.force_login(admin)

    def test_add_owner_is_the_only_primary_button(self):
        response = self.client.get(reverse("admin_owner_list"))
        html = response.content.decode()
        self.assertEqual(html.count("btn-primary"), 1)
        self.assertRegex(
            html,
            rf'(?s)<a href="{reverse("admin_owner_create")}" class="btn btn-primary">'
            r"\s*<svg[^>]*>.*?</svg>\s*Add owner</a>",
        )
        self.assertNotContains(response, "Register Walk-in Client")


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

    def _pet_link(self):
        return (
            f'<a href="{self.visit_url}?owner={self.owner.pk}&amp;pet={self.pet.pk}" '
            'class="btn-secondary">Book walk-in</a>'
        )

    def test_owner_page_links_to_the_visit_step(self):
        # The redesigned owner page (4a-3): the primary button in the actions card
        self.assertRegex(
            self._owner_page().content.decode(),
            rf'<a href="{self.visit_url}\?owner={self.owner.pk}" '
            r'class="btn btn-primary [^"]*">\s*<svg[^>]*>(?s:.*?)</svg>\s*'
            r"Book walk-in</a>",
        )

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

    def test_refused_for_a_google_user(self):
        google = _google_owner()
        response = self.client.post(
            reverse("admin_owner_resend_link", args=[google.pk])
        )
        self.assertEqual(_messages(response), ["This owner doesn't need a new link."])
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
        pets; OwnerListPageTests checks the whole page."""

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


class OwnerListPageTests(TestCase):
    """admin/pets/owners_list.html on the new layout (Slice 4a-2)."""

    def setUp(self):
        _admin(self)
        self.url = reverse("admin_owner_list")

    def _get(self, **params):
        return self.client.get(self.url, params)

    def _html(self, **params):
        return self._get(**params).content.decode()

    def _many(self, count, start=0):
        for i in range(start, start + count):
            owner = _owner(
                first=f"Owner{i:02d}", last="Test", email=f"o{i}@example.com"
            )
            Pet.objects.create(owner=owner, name=f"Pet{i:02d}", species="Dog")

    def test_new_layout_without_old_files(self):
        _owner()
        response = self._get()
        self.assertTemplateUsed(response, "layouts/admin.html")
        for old in ("owners_list.css", "owner_detail.css", "owners_list.js"):
            with self.subTest(file=old):
                self.assertNotContains(response, old)
        self.assertNotContains(response, "Find a pet or owner")
        self.assertContains(response, "<title>Pet owners — Hapi Vet Admin</title>")
        self.assertContains(response, "Everyone who brings a pet to the clinic.")

    def test_search_form_in_top_bar_and_page_body(self):
        html = self._html(q="cruz")
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
                    f'<label for="{prefix}-owner-search" class="sr-only">'
                    "Search pet owners</label>",
                    html,
                )
                self.assertIn(
                    f'<input id="{prefix}-owner-search" type="search" name="q" '
                    'value="cruz" placeholder="Search pet owners: name, phone or '
                    'email" class="input pl-10">',
                    html,
                )
        self.assertNotIn('name="status"', html)

    def test_search_form_keeps_the_archived_tab(self):
        html = self._html(status="archived")
        self.assertEqual(
            html.count('<input type="hidden" name="status" value="archived">'), 2
        )

    def test_tabs_counts_and_current(self):
        _owner()
        _owner(first="Ben", last="Santos", email="ben@example.com")
        _archive(_owner(first="Cora", last="Lim", email="cora@example.com"))
        html = self._html()
        self.assertRegex(
            html,
            rf'<a href="{self.url}\?status=active" [^>]*aria-current="page">'
            r"Active<span [^>]*>2</span></a>",
        )
        self.assertRegex(
            html,
            rf'<a href="{self.url}\?status=archived" class="[^"]*">'
            r"Archived<span [^>]*>1</span></a>",
        )
        archived = self._html(status="archived")
        self.assertRegex(
            archived,
            rf'<a href="{self.url}\?status=archived" [^>]*aria-current="page">',
        )
        # One current tab (the sidebar's Pet owners link has its own)
        tabs = re.search(
            r'(?s)<nav [^>]*aria-label="Filter by status">.*?</nav>', archived
        ).group()
        self.assertEqual(tabs.count('aria-current="page"'), 1)

    def test_tab_links_keep_q_and_drop_page(self):
        self._many(30)
        html = self._html(q="owner test", page=2)
        self.assertIn(f'href="{self.url}?status=active&amp;q=owner%20test"', html)
        self.assertIn(f'href="{self.url}?status=archived&amp;q=owner%20test"', html)
        tab_hrefs = re.findall(rf'href="{self.url}\?status=[^"]*"', html)
        self.assertTrue(tab_hrefs)
        for href in tab_hrefs:
            self.assertNotIn("page=", href)

    def test_list_head_on_each_tab(self):
        _owner()
        _archive(_owner(first="Ben", last="Santos", email="ben@example.com"))
        active = self._html()
        self.assertIn(
            'Active owners <span class="font-normal text-gray-600">(1)</span></h2>',
            active,
        )
        self.assertIn("A to Z by first name", active)
        archived = self._html(status="archived")
        self.assertIn(
            'Archived owners <span class="font-normal text-gray-600">(1)</span></h2>',
            archived,
        )
        self.assertIn("Newest first", archived)
        self.assertNotIn("A to Z by first name", archived)

    def test_results_line_and_clear_search(self):
        _owner()
        _owner(first="Ana", last="Reyes", email="reyes@example.com")
        _archive(_owner(first="Ben", last="Cruz", email="ben@example.com"))
        cases = [
            ({"q": "ana"}, "2 owners match", "active", "active"),
            ({"q": "reyes"}, "1 owner matches", "active", "active"),
            ({"q": "zzz"}, "No owner matches", "active", "active"),
            (
                {"q": "ben", "status": "archived"},
                "1 owner matches",
                "archived",
                "archived",
            ),
        ]
        for params, start, where, status in cases:
            with self.subTest(params=params):
                html = self._html(**params)
                self.assertIn(
                    f'{start} <strong class="font-semibold">“{params["q"]}”</strong> '
                    f"in {where} owners</h2>",
                    html,
                )
                self.assertRegex(
                    html,
                    rf'<a href="{self.url}\?status={status}" [^>]*>'
                    r"\s*<svg[^>]*>(?s:.*?)</svg>\s*Clear search</a>",
                )
                self.assertNotIn("A to Z by first name", html)
        self.assertNotIn("Clear search", self._html())

    def test_row(self):
        _owner()
        owner = _owner(
            first="Ben",
            last="Santos",
            email="ben@example.com",
            contact_number="0917 123 4567",
        )
        Pet.objects.create(owner=owner, name="Muning", species="Cat")
        Pet.objects.create(owner=owner, name="Bantay", species="Dog")
        Pet.objects.create(owner=owner, name="Gone", species="Dog", is_archived=True)
        html = self._html()
        # Ana Cruz: no phone, no email, no pets
        self.assertIn(">AC</span>", html)
        self.assertIn('<span class="text-gray-600">No phone number</span>', html)
        self.assertIn('<span class="text-gray-600">No email</span>', html)
        self.assertIn("No pets yet", html)
        # Ben Santos: contact and active pets with species icons
        self.assertIn("0917 123 4567", html)
        self.assertIn("ben@example.com", html)
        self.assertRegex(
            html,
            r'<svg class="size-5 shrink-0 text-violet-600"[^>]*' r'stroke-width="1.5"',
        )
        self.assertIn("Bantay</li>", html)
        self.assertIn("Muning</li>", html)
        self.assertNotIn("Gone", html)
        self.assertRegex(
            html,
            rf'<a href="{reverse("admin_owner_detail", args=[owner.pk])}" '
            r'class="[^"]*border-violet-300 bg-violet-50[^"]*">Open'
            r'<span class="sr-only"> Ben Santos</span>',
        )
        self.assertNotIn("Archived Today", html)
        self.assertNotIn("ring-gray-300", html)

    def test_archived_rows_have_pill_and_muted_avatar(self):
        _archive(_owner())
        older = _archive(_owner(first="Ben", last="Santos", email="ben@example.com"))
        # Aug 16 2025, noon in Manila
        PetOwner.objects.filter(pk=older.pk).update(
            archived_at=datetime(2025, 8, 16, 4, tzinfo=dt_timezone.utc)
        )
        html = self._html(status="archived")
        self.assertIn("Archived today</span>", html)
        self.assertIn("Archived Sat, Aug 16, 2025</span>", html)
        self.assertEqual(html.count("text-gray-700 ring-1 ring-gray-300"), 2)
        self.assertNotIn("text-violet-700 ring-1 ring-violet-200", html)

    def test_archived_note_only_on_the_archived_tab(self):
        _owner()
        _archive(_owner(first="Ben", last="Santos", email="ben@example.com"))
        note = "Archived owners can't log in, and you can't book visits for them."
        self.assertNotIn(note, self._html())
        self.assertIn(note, self._html(status="archived"))

    def test_empty_states(self):
        def title_and_text(html):
            match = re.search(
                r'<p class="text-lg font-semibold text-gray-900">(.*?)</p>\s*'
                r'<p class="mt-1 max-w-sm text-base text-gray-600">(.*?)</p>',
                html,
            )
            return match.groups() if match else None

        self.assertEqual(
            title_and_text(self._html()),
            (
                "No pet owners yet",
                "Owners appear here when they sign up online, or when you add "
                "them with Add owner or a walk-in.",
            ),
        )
        _owner()
        self.assertEqual(
            title_and_text(self._html(status="archived")),
            ("No archived owners", "Owners you archive appear here."),
        )
        self.assertEqual(
            title_and_text(self._html(q="zzz")),
            (
                "No owner matches “zzz”",
                "Check the spelling, or look in the Archived tab. To add someone "
                "new, use Add owner at the top.",
            ),
        )
        self.assertEqual(
            title_and_text(self._html(q="zzz", status="archived")),
            (
                "No owner matches “zzz”",
                "Check the spelling, or look in the Active tab.",
            ),
        )
        PetOwner.objects.get().archive()
        self.assertEqual(
            title_and_text(self._html()),
            (
                "No active owners",
                "Restore one from the Archived tab, or use Add owner.",
            ),
        )

    def test_pager_on_30_owners(self):
        self._many(30)
        html = self._html()
        self.assertEqual(html.count('<li class="flex flex-col gap-3'), 25)
        self.assertIn("Showing 1–25 of 30", html)
        self.assertIn(
            '<a href="?status=active&amp;page=2" class="btn btn-outline">', html
        )
        second = self._html(page=2)
        self.assertEqual(second.count('<li class="flex flex-col gap-3'), 5)
        self.assertIn("Showing 26–30 of 30", second)

    def test_query_count_is_flat(self):
        """The full page costs the same with 3 owners as with 30."""
        self._many(3)
        _archive(_owner(first="Old", last="One", email="old@example.com"))
        self._get()  # warm-up: one-time lookups don't count
        with CaptureQueriesContext(connection) as small:
            self._get()
        self._many(27, 3)
        with self.assertNumQueries(len(small)):
            self._get()

    def test_every_link_is_on_one_line(self):
        self._many(30)
        _archive(_owner())
        for params in ({}, {"q": "owner"}, {"status": "archived"}, {"q": "zzz"}):
            with self.subTest(params=params):
                for tag in re.findall(r"<a\b[^>]*>", self._html(**params)):
                    self.assertNotIn("\n", tag)


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

    def test_google_user_is_active(self):
        google = _google_owner()
        self.assertFalse(google.user.has_usable_password())
        context = self._context(google)
        self.assertEqual(context["account_state"], "active")
        self.assertFalse(context["can_resend"])

    def test_unusable_password_after_a_login_is_active(self):
        self.owner.user.last_login = timezone.now()
        self.owner.user.save()
        context = self._context()
        self.assertEqual(context["account_state"], "active")
        self.assertFalse(context["can_resend"])

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


class OwnerDetailPageTests(TestCase):
    """admin/pets/owner_detail.html on the new layout (Slice 4a-3)."""

    def setUp(self):
        _admin(self)
        # Claimed account with an email
        self.owner = _owner(email="ana@example.com", password="pw-12345!")
        self.today = timezone.localdate()

    def _get(self, owner=None):
        url = reverse("admin_owner_detail", args=[(owner or self.owner).pk])
        return self.client.get(url)

    def _html(self, owner=None):
        return self._get(owner).content.decode()

    def _page(self, owner=None):
        """The page without its <dialog>s: the cards and their buttons."""
        return re.sub(r"(?s)<dialog\b.*?</dialog>", "", self._html(owner))

    def _dialogs(self, owner=None):
        """{dialog id: its markup}."""
        return dict(
            re.findall(r'(?s)<dialog id="([a-z-]+)"(.*?)</dialog>', self._html(owner))
        )

    def _pet(self, name="Bantay", species="Dog", owner=None, **fields):
        return Pet.objects.create(
            owner=owner or self.owner, name=name, species=species, **fields
        )

    def _visit(self, pet, days=0, status=Appointment.CONFIRMED, hour=9, **fields):
        return Appointment.objects.create(
            owner=pet.owner,
            pet=pet,
            date=self.today + timedelta(days=days),
            time=time(hour, 0),
            status=status,
            **fields,
        )

    # ─── Layout and header ────────────────────────────────────────────────────

    def test_new_layout_and_the_page_parses(self):
        response = self._get()
        self.assertTemplateUsed(response, "layouts/admin.html")
        self.assertNotContains(response, "owner_detail.css")
        self.assertNotContains(response, "layer(legacy)")
        self.assertContains(response, "<title>Ana Cruz — Hapi Vet Admin</title>")
        # html=True parses the whole page (the old one had a stray </div>)
        self.assertContains(
            response,
            '<h1 class="text-2xl font-bold text-gray-900">Ana Cruz</h1>',
            html=True,
        )

    def test_header(self):
        PetOwner.objects.filter(pk=self.owner.pk).update(
            date_registered=datetime(2025, 3, 12).date()
        )
        html = self._html()
        self.assertRegex(
            html,
            rf'<a href="{reverse("admin_owner_list")}" class="[^"]*">'
            r"\s*<svg[^>]*>(?s:.*?)</svg>\s*Back to pet owners</a>",
        )
        self.assertRegex(html, r'<span class="grid size-16 text-xl [^"]*bg-violet-50')
        self.assertIn(">AC</span>", html)
        self.assertIn("Pet owner since Wed, Mar 12, 2025</p>", html)
        self.assertNotIn("Archived</span>", html)
        self.assertNotIn("Added by the clinic", html)

    def test_archived_header_has_the_pill_and_a_gray_avatar(self):
        html = self._html(_archive(self.owner))
        self.assertRegex(html, r'<span class="grid size-16 text-xl [^"]*bg-gray-100')
        self.assertRegex(
            html, r"(?s)<svg[^>]*>(?:(?!</svg>).)*</svg>\s*Archived</span>"
        )

    # ─── Banners ──────────────────────────────────────────────────────────────

    def test_deletion_banner_per_pending_request(self):
        choco = self._pet("Choco")
        mingming = self._pet("Mingming", "Cat")
        for pet, reason in (
            (choco, PetDeletionRequest.REHOMED),
            (mingming, PetDeletionRequest.PASSED_AWAY),
        ):
            PetDeletionRequest.objects.create(
                pet=pet, requested_by=self.owner.user, reason=reason
            )
        PetDeletionRequest.objects.create(
            pet=self._pet("Old"),
            requested_by=self.owner.user,
            reason=PetDeletionRequest.DUPLICATE,
            status=PetDeletionRequest.REJECTED,
        )
        html = self._html()
        self.assertEqual(html.count("Deletion request waiting"), 2)
        self.assertIn(
            "Ana asked to remove <b>Choco</b> on today. Reason: rehomed. "
            "Open Choco to approve or reject it.",
            html,
        )
        self.assertIn("Reason: passed away.", html)
        self.assertNotIn("Review Old", html)
        self.assertRegex(
            html,
            rf'<a href="{reverse("admin_pet_detail", args=[choco.pk])}" '
            r'class="btn border border-yellow-400 [^"]*">Review Choco\s*<svg',
        )

    def test_archived_banner(self):
        self.assertNotIn("This owner is archived", self._html())
        html = self._html(_archive(self.owner))
        self.assertIn("This owner is archived", html)
        self.assertIn(
            "Archived on today. Ana Cruz can't log in, and you can't book visits "
            "for them. Their pets, visits and records are kept.",
            html,
        )

    # ─── Contact details ──────────────────────────────────────────────────────

    def test_contact_card_empty_texts(self):
        html = self._html(_owner(first="Lito", last="Reyes"))
        for text in ("No phone number", "No email", "No address", "None"):
            with self.subTest(text=text):
                self.assertIn(f'<span class="text-gray-600">{text}</span>', html)

    def test_contact_card_values(self):
        PetOwner.objects.filter(pk=self.owner.pk).update(
            contact_number="0917 123 4567", barangay="Bognuyan", province="Marinduque"
        )
        self.owner.contact_links.create(
            platform="Facebook", url_or_handle="facebook.com/ana"
        )
        html = self._html()
        self.assertIn("0917 123 4567</dd>", html)
        self.assertIn("ana@example.com</dd>", html)
        self.assertIn("Bognuyan, Marinduque</dd>", html)
        self.assertIn("<li>Facebook: facebook.com/ana</li>", html)
        self.assertNotIn('<span class="text-gray-600">None</span>', html)

    # ─── Pets ─────────────────────────────────────────────────────────────────

    def test_pet_tiles(self):
        brownie = self._pet(
            "Brownie",
            breed="Aspin",
            date_of_birth=self.today - timedelta(days=3 * 365 + 10),
        )
        self._pet("Mingming", "Cat")
        html = self._html()
        self.assertIn(
            'Pets <span class="font-normal text-gray-600">(2)</span></h2>', html
        )
        self.assertIn("Dog · Aspin · 3 yrs</p>", html)
        self.assertIn('<p class="text-sm text-gray-700">Cat</p>', html)
        self.assertRegex(
            html,
            rf'<a href="{reverse("admin_pet_detail", args=[brownie.pk])}" '
            r'class="[^"]*border-violet-300 bg-violet-50[^"]*">Open'
            r'<span class="sr-only"> Brownie</span>',
        )
        self.assertNotIn("<details", html)
        self.assertNotIn("No pets yet.", html)

    def test_no_pets_yet(self):
        self.assertIn("No pets yet.", self._html())

    def test_archived_pets_are_folded_away(self):
        self._pet("Bantay")
        old = self._pet("Old", breed="Aspin", is_archived=True)
        Pet.objects.filter(pk=old.pk).update(archived_at=timezone.now())
        html = self._html()
        self.assertIn(
            'Pets <span class="font-normal text-gray-600">(1)</span></h2>', html
        )
        self.assertRegex(
            html,
            r"(?s)<details [^>]*>\s*<summary [^>]*min-h-11[^>]*>.*?"
            r"Archived pets \(1\)</summary>",
        )
        self.assertIn("Dog · Aspin · Archived today</p>", html)
        self.assertIn(f'href="{reverse("admin_pet_detail", args=[old.pk])}"', html)
        details = re.search(r"(?s)<details.*?</details>", html).group()
        self.assertIn("bg-gray-100 text-gray-600 ring-1 ring-gray-300", details)

    def test_archived_owner_mutes_every_pet_tile(self):
        self._pet("Bantay")
        self._pet("Mingming", "Cat")
        html = self._html(_archive(self.owner))
        self.assertEqual(
            html.count("bg-gray-100 text-gray-600 ring-1 ring-gray-300"), 2
        )
        self.assertNotIn("bg-violet-50 text-violet-600", html)

    # ─── Recent visits ────────────────────────────────────────────────────────

    def test_recent_visits(self):
        pet = self._pet("Bantay")
        checkup = Service.objects.create(service_name="Check-up", status=Service.ACTIVE)
        for days in range(-6, -1):
            self._visit(pet, days, Appointment.COMPLETED)
        latest = self._visit(pet, 0, hour=14, service=checkup)
        html = self._html()
        self.assertEqual(html.count('<span class="sr-only"> visit for Bantay on '), 5)
        self.assertIn("Bantay · Check-up</span>", html)
        self.assertIn("Bantay · Appointment</span>", html)
        self.assertIn("2:00 PM</span>", html)
        self.assertIn(
            '<span class="badge bg-blue-100 text-blue-800">Confirmed</span>', html
        )
        self.assertIn(
            '<span class="badge bg-green-100 text-green-800">Completed</span>', html
        )
        self.assertIn(
            f'<a href="{reverse("admin_appointment_detail", args=[latest.pk])}" '
            'class="inline-flex min-h-11 items-center px-2 text-base font-medium '
            'text-blue-600 underline underline-offset-2">Open'
            '<span class="sr-only"> visit for Bantay on today</span></a>',
            html,
        )
        self.assertRegex(
            html,
            rf'<a href="{reverse("admin_appointments")}\?status=all&amp;q=Ana\+Cruz" '
            r'class="[^"]*">See all visits for Ana Cruz\s*<svg',
        )
        self.assertNotIn("No visits yet.", html)

    def test_no_visits_yet(self):
        html = self._html()
        self.assertIn("No visits yet.", html)
        self.assertNotIn("See all visits", html)

    # ─── Actions ──────────────────────────────────────────────────────────────

    def test_actions_when_not_archived(self):
        page = self._page()
        # Dialog "Yes, …" buttons aside, Book walk-in is the one primary button
        self.assertEqual(page.count("btn-primary"), 1)
        self.assertRegex(
            page,
            rf'<a href="{reverse("admin_walkin_appointment_visit")}'
            rf'\?owner={self.owner.pk}" class="btn btn-primary [^"]*">'
            r"\s*<svg[^>]*>(?s:.*?)</svg>\s*Book walk-in</a>",
        )
        for url, label in (
            (reverse("admin_pet_add", args=[self.owner.pk]), "Add pet"),
            (reverse("admin_owner_edit", args=[self.owner.pk]), "Edit details"),
        ):
            with self.subTest(label=label):
                self.assertRegex(
                    page,
                    rf'<a href="{url}" class="btn btn-outline [^"]*">'
                    rf"\s*<svg[^>]*>(?s:.*?)</svg>\s*{label}</a>",
                )
        self.assertRegex(
            page,
            r'(?s)<button type="button" class="btn [^"]*text-red-700[^"]*" '
            r'data-dialog-open="archive-dialog"[^>]*>.*?Archive owner\s*</button>',
        )
        self.assertNotIn("Restore account", page)

    def test_actions_when_archived(self):
        owner = _archive(_owner(first="Lito", last="Reyes"))
        page = self._page(owner)
        self.assertEqual(page.count("btn-primary"), 1)
        self.assertRegex(
            page,
            r'(?s)<button type="button" class="btn btn-primary [^"]*" '
            r'data-dialog-open="restore-dialog"[^>]*>.*?Restore account\s*</button>',
        )
        self.assertIn(
            "Restore the account to book visits, add pets or change details.", page
        )
        for gone in ("Book walk-in", "Add pet", "Edit details", "Add email"):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, page)
        for url_name in ("admin_pet_add", "admin_owner_edit", "admin_owner_add_email"):
            with self.subTest(url=url_name):
                self.assertNotIn(reverse(url_name, args=[owner.pk]), page)
        self.assertNotIn('data-dialog-open="archive-dialog"', page)

    # ─── Account card ─────────────────────────────────────────────────────────

    def test_account_card_in_each_state(self):
        unclaimed = _owner(first="Ben", last="Santos", email="ben@example.com")
        no_email = _owner(first="Lito", last="Reyes")
        archived = _archive(_owner(first="Cora", last="Lim", email="cora@example.com"))
        google = _google_owner()
        cases = [
            (self.owner, "Ana Cruz can log in and book online."),
            (google, "Gina Ramos can log in and book online."),
            (
                no_email,
                "Lito Reyes can't log in yet. Add an email and we'll send a link "
                "to set a password.",
            ),
            (
                unclaimed,
                "Ben Santos hasn't set a password yet. The link was sent to "
                "<b>ben@example.com</b>.",
            ),
            (archived, "Archived accounts can't log in."),
        ]
        for owner, text in cases:
            with self.subTest(owner=owner.full_name):
                page = self._page(owner)
                self.assertIn(text, page)
                self.assertEqual("Send the link again" in page, owner == unclaimed)
                add_email = reverse("admin_owner_add_email", args=[owner.pk])
                self.assertEqual(f'href="{add_email}"' in page, owner == no_email)
        self.assertRegex(
            self._page(unclaimed),
            r'(?s)<button type="button" class="btn btn-outline [^"]*" '
            r'data-dialog-open="resend-dialog"[^>]*>.*?Send the link again\s*</button>',
        )

    # ─── Dialogs ──────────────────────────────────────────────────────────────

    def test_dialogs_only_with_their_buttons_and_post_to_the_right_url(self):
        unclaimed = _owner(first="Ben", last="Santos", email="ben@example.com")
        archived = _archive(_owner(first="Cora", last="Lim", email="cora@example.com"))
        cases = [
            (self.owner, {"archive-dialog": "admin_owner_archive"}),
            (_google_owner(), {"archive-dialog": "admin_owner_archive"}),
            (
                unclaimed,
                {
                    "archive-dialog": "admin_owner_archive",
                    "resend-dialog": "admin_owner_resend_link",
                },
            ),
            (archived, {"restore-dialog": "admin_owner_restore"}),
        ]
        for owner, expected in cases:
            with self.subTest(owner=owner.full_name):
                dialogs = self._dialogs(owner)
                self.assertEqual(set(dialogs), set(expected))
                page = self._page(owner)
                for dialog_id, url_name in expected.items():
                    self.assertIn(f'data-dialog-open="{dialog_id}"', page)
                    markup = dialogs[dialog_id]
                    action = reverse(url_name, args=[owner.pk])
                    self.assertIn(f'<form method="post" action="{action}"', markup)
                    self.assertIn('name="csrfmiddlewaretoken"', markup)
                    # Focus starts on Go back
                    self.assertIn(
                        "data-dialog-close autofocus>Go back</button>", markup
                    )

    def test_archive_dialog_text_and_red_confirm(self):
        dialog = self._dialogs()["archive-dialog"]
        self.assertIn("Archive Ana Cruz?</h2>", dialog)
        self.assertIn(
            "Ana Cruz won't be able to log in, and you won't be able to book visits "
            "for them. Their pets, visits, medical records and receipts are all "
            "kept. You can restore the account at any time.",
            dialog,
        )
        self.assertRegex(
            dialog,
            r'(?s)<button type="submit" class="btn btn-danger [^"]*">'
            r".*?Yes, archive owner",
        )
        self.assertNotIn("Archiving doesn't cancel", dialog)

    def test_archive_warning_for_1_2_and_3_upcoming_visits(self):
        bantay = self._pet("Bantay")
        self._visit(bantay, -1)  # past: not counted
        self._visit(bantay, 3, Appointment.CANCELLED)  # cancelled: not counted
        self._visit(bantay, 1)
        first = "<b>Bantay has a visit tomorrow at 9:00 AM.</b>"
        cases = [
            (
                1,
                f"{first} Archiving doesn't cancel it. Cancel it on the "
                "appointment page if it won't happen.",
            ),
            (
                2,
                f"{first} There is 1 more. Archiving doesn't cancel them. Cancel "
                "them on the appointment page if they won't happen.",
            ),
            (
                3,
                f"{first} There are 2 more. Archiving doesn't cancel them. Cancel "
                "them on the appointment page if they won't happen.",
            ),
        ]
        for count, text in cases:
            if count > 1:
                self._visit(bantay, count + 1, Appointment.PENDING)
            with self.subTest(count=count):
                self.assertIn(text, self._dialogs()["archive-dialog"])

    def test_restore_dialog_with_and_without_an_email(self):
        cases = [
            (
                _archive(self.owner),
                "Ana Cruz will be able to log in again, and you can book visits "
                "for them. Their pets come back on the Pets list.",
            ),
            (
                _archive(_owner(first="Lito", last="Reyes")),
                "You can book visits for Lito Reyes again. They still can't log in "
                "until they have an email and set a password.",
            ),
        ]
        for owner, text in cases:
            with self.subTest(owner=owner.full_name):
                dialog = self._dialogs(owner)["restore-dialog"]
                self.assertIn(f"Restore {owner.full_name}?</h2>", dialog)
                self.assertIn(text, dialog)
                self.assertRegex(
                    dialog,
                    r'(?s)<button type="submit" class="btn btn-primary [^"]*">'
                    r".*?Yes, restore account",
                )

    def test_resend_dialog_text(self):
        owner = _owner(first="Ben", last="Santos", email="ben@example.com")
        dialog = self._dialogs(owner)["resend-dialog"]
        self.assertIn("Send the link again?</h2>", dialog)
        self.assertIn(
            "Ben Santos will get a new email at <b>ben@example.com</b> with a link "
            "to set a password. Links sent before still work until they expire.",
            dialog,
        )
        self.assertRegex(
            dialog,
            r'(?s)<button type="submit" class="btn btn-primary [^"]*">'
            r".*?Yes, send it",
        )

    # ─── Whole page ───────────────────────────────────────────────────────────

    def test_every_link_is_on_one_line(self):
        pet = self._pet("Bantay")
        self._visit(pet, 1)
        PetDeletionRequest.objects.create(
            pet=pet, requested_by=self.owner.user, reason=PetDeletionRequest.OTHER
        )
        self._pet("Old", is_archived=True)
        owners = [
            self.owner,
            _owner(first="Lito", last="Reyes"),
            _owner(first="Ben", last="Santos", email="ben@example.com"),
            _archive(_owner(first="Cora", last="Lim", email="cora@example.com")),
        ]
        for owner in owners:
            with self.subTest(owner=owner.full_name):
                for tag in re.findall(r"<a\b[^>]*>", self._html(owner)):
                    self.assertNotIn("\n", tag)

    def test_query_count_is_flat(self):
        """The page costs the same with 1 pet and visit as with 5 of each."""

        def add(start, count):
            for i in range(start, start + count):
                self._visit(self._pet(f"Pet{i}"), -i - 1)

        add(0, 1)
        self._pet("Old", is_archived=True)
        self._get()  # warm-up: one-time lookups don't count
        with CaptureQueriesContext(connection) as small:
            self._get()
        add(1, 4)
        with self.assertNumQueries(len(small)):
            self._get()
