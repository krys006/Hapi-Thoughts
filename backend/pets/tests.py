import re
from datetime import date, datetime, time, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from allauth.socialaccount.models import SocialAccount
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.core import mail
from django.db import connection, reset_queries
from django.http import Http404, QueryDict
from django.template import engines
from django.template.loader import render_to_string
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from appointments.models import Appointment, ClinicSettings
from appointments.utils import CLINIC_SETTINGS_DEFAULTS
from billing.models import Service
from dashboard.templatetags.friendly_dates import friendly_date
from medical.models import MedicalRecord, TestResultFile, Vaccination
from notifications.models import Notification
from pets.forms import (
    AdminOwnerEmailForm,
    AdminPetForm,
    AdminPetOwnerForm,
    OwnerOnboardingForm,
    OwnerPetEditForm,
    OwnerPetForm,
)
from pets.models import ContactLink, Pet, PetDeletionRequest, PetOwner
from pets.templatetags.pet_display import species_icon, weight_kg
from pets.utils import (
    PET_BREEDS_BY_SPECIES,
    answer_deletion_request,
    archive_pet_directly,
    barangay_choices,
    barangays_for,
    breed_choices,
    breeds_for,
    marinduque_towns,
    match_barangay,
    match_breed,
    match_species,
    match_town,
    owner_list_context,
    owner_pet_profile_context,
    owner_pets_context,
    pet_list_context,
    psgc_marinduque,
    species_options,
)

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


# Slice 4a-4: the address is picked (Province / Town / Barangay selects);
# the typed barangay / city_municipality / province are for other provinces
OWNER_POST = {
    "first_name": "Ana",
    "last_name": "Cruz",
    "contact_number": "09171234567",
    "address_province": "Marinduque",
    "address_town": "Boac",
    "address_barangay": "Amoingon",
}

# Slice 8b-3: the address an owner must give on onboarding step 1
OWNER_ADDRESS = {
    "address_province": "Marinduque",
    "address_town": "Boac",
    "address_barangay": "Amoingon",
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


class WeightKgTests(SimpleTestCase):
    """weight_kg drops trailing zeros, never scientific notation."""

    def test_trailing_zero_is_dropped(self):
        self.assertEqual(weight_kg(Decimal("12.50")), "12.5")

    def test_whole_number_has_no_point(self):
        self.assertEqual(weight_kg(Decimal("10.00")), "10")

    def test_two_decimals_are_kept(self):
        self.assertEqual(weight_kg(Decimal("8.25")), "8.25")

    def test_none_is_empty(self):
        self.assertEqual(weight_kg(None), "")


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
            {**OWNER_POST, "email": email},
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

    def test_owner_page_links_to_the_visit_step(self):
        # The redesigned owner page (4a-3): the primary button in the actions card
        self.assertRegex(
            self._owner_page().content.decode(),
            rf'<a href="{self.visit_url}\?owner={self.owner.pk}" '
            r'class="btn btn-primary [^"]*">\s*<svg[^>]*>(?s:.*?)</svg>\s*'
            r"Book walk-in</a>",
        )

    def test_pet_page_links_to_the_visit_step_with_the_pet(self):
        # The redesigned pet page (4b-3): the primary button in the actions card
        self.assertRegex(
            self._pet_page().content.decode(),
            rf'<a href="{self.visit_url}\?owner={self.owner.pk}&amp;pet={self.pet.pk}" '
            r'class="btn btn-primary [^"]*">\s*<svg[^>]*>(?s:.*?)</svg>\s*'
            r"Book walk-in</a>",
        )

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


# ─── Slice 4a-4: owner forms and Marinduque address dropdowns ────────────────

TOWNS = ["Boac", "Buenavista", "Gasan", "Mogpog", "Santa Cruz", "Torrijos"]


def _options(html):
    """(value, label) pairs of every <option> in html."""
    return re.findall(r'<option value="([^"]*)"[^>]*>([^<]*)</option>', html)


class PsgcDataTests(SimpleTestCase):
    """backend/pets/data/psgc_marinduque.json (PSA PSGC list)."""

    def test_six_towns_each_with_a_sorted_list(self):
        data = psgc_marinduque()
        self.assertEqual(data["province"], "Marinduque")
        self.assertTrue(data["source"].strip())
        self.assertEqual(list(data["towns"]), TOWNS)
        for town, barangays in data["towns"].items():
            with self.subTest(town=town):
                self.assertTrue(barangays)
                self.assertEqual(barangays, sorted(barangays, key=str.casefold))
                self.assertEqual(len(barangays), len(set(barangays)))

    def test_marinduque_towns_is_the_list_a_to_z(self):
        self.assertEqual(marinduque_towns(), TOWNS)


class AddressHelperTests(SimpleTestCase):
    """pets/utils.py: match_town, barangays_for, match_barangay."""

    def test_town_match_ignores_case_and_spaces(self):
        self.assertEqual(match_town("  santa   CRUZ "), "Santa Cruz")
        self.assertEqual(match_town("gasan"), "Gasan")

    def test_unknown_town_is_none(self):
        for value in ("Gassan", "", None, "Batangas"):
            with self.subTest(value=value):
                self.assertIsNone(match_town(value))
        self.assertEqual(barangays_for("Gassan"), [])

    def test_barangay_match_returns_the_official_spelling(self):
        official = barangays_for("Boac")[0]
        self.assertEqual(barangays_for("boac"), barangays_for("Boac"))
        self.assertEqual(match_barangay("BOAC", f"  {official.upper()} "), official)
        self.assertIsNone(match_barangay("Boac", "Nowhere"))
        self.assertIsNone(match_barangay("Gassan", official))

    def test_barangay_choices(self):
        self.assertEqual(barangay_choices(""), [("", "Choose the town first")])
        choices = barangay_choices("Mogpog")
        self.assertEqual(choices[0], ("", "Choose a barangay"))
        self.assertEqual([v for v, _ in choices[1:]], barangays_for("Mogpog"))


class BarangayEndpointTests(TestCase):
    """admin_address_barangays: the Barangay <option> list over HTMX."""

    def setUp(self):
        self.url = reverse("admin_address_barangays")

    def test_admin_only(self):
        response = self.client.get(self.url, {"town": "Boac"})
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("<option", response.content.decode())
        owner = _owner(email="ana@example.com", password="pw-12345!")
        self.client.force_login(owner.user)
        response = self.client.get(self.url, {"town": "Boac"})
        self.assertRedirects(
            response, reverse("owner_dashboard"), fetch_redirect_response=False
        )

    def test_known_town_lists_its_barangays_placeholder_first(self):
        _admin(self)
        for params in ({"town": "boac"}, {"address_town": "Boac"}):
            with self.subTest(params=params):
                response = self.client.get(
                    self.url, params, headers={"HX-Request": "true"}
                )
                options = _options(response.content.decode())
                self.assertEqual(options[0], ("", "Choose a barangay"))
                self.assertEqual([v for v, _ in options[1:]], barangays_for("Boac"))
                self.assertEqual([t for _, t in options[1:]], barangays_for("Boac"))

    def test_unknown_or_empty_town(self):
        _admin(self)
        for town in ("", "Nowhere"):
            with self.subTest(town=town):
                response = self.client.get(self.url, {"town": town})
                self.assertEqual(
                    _options(response.content.decode()),
                    [("", "Choose the town first")],
                )


class OwnerFormAddressTests(TestCase):
    """AdminPetOwnerForm: address modes, errors and the empty rule."""

    BASE = {
        "first_name": "Ana",
        "last_name": "Cruz",
        "address_province": "Marinduque",
    }

    def _form(self, **data):
        return AdminPetOwnerForm({**self.BASE, **data})

    def _address(self, form):
        self.assertTrue(form.is_valid(), form.errors)
        data = form.cleaned_data
        return (
            data["province"],
            data["city_municipality"],
            data["barangay"],
            data["address_line"],
        )

    def test_marinduque_pick_saves_the_official_spellings(self):
        official = barangays_for("Gasan")[0]
        form = self._form(address_town="Gasan", address_barangay=official.lower())
        self.assertEqual(self._address(form), ("Marinduque", "Gasan", official, ""))

    def test_marinduque_ignores_the_hidden_typed_fields(self):
        official = barangays_for("Boac")[0]
        form = self._form(
            address_town="Boac",
            address_barangay=official,
            province="Batangas",
            city_municipality="Lipa City",
            barangay="Sabang",
        )
        self.assertEqual(self._address(form), ("Marinduque", "Boac", official, ""))

    def test_barangay_of_another_town_is_refused(self):
        boac = barangays_for("Boac")
        other = next(b for b in barangays_for("Torrijos") if b not in boac)
        form = self._form(address_town="Boac", address_barangay=other)
        self.assertFalse(form.is_valid())
        self.assertEqual(
            form.errors["address_barangay"], ["Choose a barangay in Boac."]
        )

    def test_barangay_without_a_town_is_refused(self):
        form = self._form(address_barangay=barangays_for("Boac")[0])
        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors["address_town"], ["Choose the town first."])

    def test_unknown_town_is_refused(self):
        form = self._form(address_town="Gassan")
        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors["address_town"], ["Choose a town from the list."])

    def test_missing_province_is_refused(self):
        form = AdminPetOwnerForm({"first_name": "Ana", "last_name": "Cruz"})
        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors["address_province"], ["Choose a province."])

    def test_other_mode_saves_the_typed_text(self):
        form = self._form(
            address_province="other",
            address_town="Boac",
            province=" batangas ",
            city_municipality="lipa city",
            barangay="sabang",
            address_line="  Purok   3 ",
        )
        self.assertEqual(
            self._address(form), ("Batangas", "Lipa City", "Sabang", "Purok 3")
        )

    def test_empty_rule_saves_four_empty_strings(self):
        self.assertEqual(self._address(self._form()), ("", "", "", ""))
        form = self._form(address_province="other")
        self.assertEqual(self._address(form), ("", "", "", ""))

    def test_street_only_keeps_marinduque(self):
        form = self._form(address_line="Purok 3")
        self.assertEqual(self._address(form), ("Marinduque", "", "", "Purok 3"))

    def test_barangay_list_follows_the_posted_town(self):
        form = self._form(address_town="Boac", first_name="")
        field = form.fields["address_barangay"]
        self.assertEqual([v for v, _ in field.choices[1:]], barangays_for("Boac"))
        self.assertNotIn("disabled", field.widget.attrs)

    def test_barangay_is_disabled_without_a_town(self):
        field = AdminPetOwnerForm().fields["address_barangay"]
        self.assertEqual(field.choices, [("", "Choose the town first")])
        self.assertTrue(field.widget.attrs["disabled"])

    def test_saved_through_the_add_page(self):
        _admin(self)
        official = barangays_for("Santa Cruz")[-1]
        self.client.post(
            reverse("admin_owner_create"),
            {**OWNER_POST, "address_town": "Santa Cruz", "address_barangay": official},
        )
        owner = PetOwner.objects.get()
        self.assertEqual(
            (owner.province, owner.city_municipality, owner.barangay),
            ("Marinduque", "Santa Cruz", official),
        )


class OwnerFormInitialTests(TestCase):
    """AdminPetOwnerForm on the edit page: which address mode opens."""

    def _form(self, data=None, **address):
        owner = _owner(**address)
        return AdminPetOwnerForm(data, instance=owner, current_user=owner.user)

    def test_matching_saved_values_open_marinduque_with_them_picked(self):
        official = barangays_for("Gasan")[0]
        form = self._form(
            province="marinduque", city_municipality="GASAN", barangay=official.lower()
        )
        self.assertEqual(form.address_mode, "marinduque")
        self.assertFalse(form.address_hand_typed)
        self.assertEqual(form["address_town"].value(), "Gasan")
        self.assertEqual(form["address_barangay"].value(), official)

    def test_town_without_barangay_opens_marinduque(self):
        form = self._form(province="Marinduque", city_municipality="Boac")
        self.assertEqual(form.address_mode, "marinduque")
        self.assertEqual(form["address_town"].value(), "Boac")
        self.assertFalse(form.address_hand_typed)

    def test_typo_opens_other_mode_hand_typed(self):
        official = barangays_for("Gasan")[0]
        form = self._form(
            province="marinduque", city_municipality="gassan", barangay=official
        )
        self.assertEqual(form.address_mode, "other")
        self.assertTrue(form.address_hand_typed)
        self.assertEqual(form["city_municipality"].value(), "gassan")
        self.assertEqual(form["province"].value(), "marinduque")

    def test_another_province_opens_other_mode_without_the_note(self):
        form = self._form(
            province="Batangas", city_municipality="Lipa City", barangay="Sabang"
        )
        self.assertEqual(form.address_mode, "other")
        self.assertFalse(form.address_hand_typed)

    def test_no_province_with_a_town_has_no_note(self):
        form = self._form(city_municipality="Gassan")
        self.assertEqual(form.address_mode, "other")
        self.assertFalse(form.address_hand_typed)

    def test_all_empty_opens_marinduque_with_nothing_picked(self):
        form = self._form()
        self.assertEqual(form.address_mode, "marinduque")
        self.assertFalse(form.address_hand_typed)
        self.assertFalse(form["address_town"].value())
        self.assertFalse(form["address_barangay"].value())

    def test_marinduque_with_street_only_is_not_hand_typed(self):
        form = self._form(province="Marinduque", address_line="Purok 3")
        self.assertEqual(form.address_mode, "marinduque")
        self.assertFalse(form.address_hand_typed)

    def test_note_goes_once_marinduque_is_posted(self):
        data = {
            "first_name": "",
            "last_name": "Cruz",
            "address_province": "Marinduque",
        }
        form = self._form(data, province="marinduque", city_municipality="Gassan")
        self.assertFalse(form.is_valid())
        self.assertEqual(form.address_mode, "marinduque")
        self.assertFalse(form.address_hand_typed)


class OwnerFormPageTests(TestCase):
    """admin/pets/owner_form.html: Add owner and Edit details."""

    def setUp(self):
        _admin(self)
        self.add = reverse("admin_owner_create")
        self.liza = _owner(first="Liza", email="liza@example.com")
        self.ana = _owner()
        self.edit = reverse("admin_owner_edit", args=[self.liza.pk])
        self.edit_ana = reverse("admin_owner_edit", args=[self.ana.pk])

    def _html(self, url, data=None):
        if data is None:
            return self.client.get(url).content.decode()
        return self.client.post(url, data).content.decode()

    def test_new_layout(self):
        for url in (self.add, self.edit):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertTemplateUsed(response, "layouts/admin.html")
                self.assertNotContains(response, "owner_form.css")
                self.assertNotContains(response, "admin-page")
                self.assertContains(response, '<div class="mx-auto max-w-3xl">')
                # Slice 8b-3: the address script moved to js/shared/
                self.assertContains(response, "js/shared/address_form")

    def test_add_header(self):
        html = self._html(self.add)
        self.assertRegex(
            html,
            rf'(?s)<a href="{reverse("admin_owner_list")}" [^>]*>\s*<svg.*?</svg>'
            r"\s*Back to pet owners</a>",
        )
        self.assertRegex(html, r"<h1[^>]*>Add owner</h1>")
        self.assertIn(
            "Add an owner without booking a visit. "
            "You can add their pets on the next page.",
            html,
        )

    def test_edit_header(self):
        html = self._html(self.edit)
        detail = reverse("admin_owner_detail", args=[self.liza.pk])
        self.assertRegex(
            html,
            rf'(?s)<a href="{detail}" [^>]*>\s*<svg.*?</svg>\s*Back to Liza Cruz</a>',
        )
        self.assertRegex(html, r"<h1[^>]*>Edit details</h1>")
        self.assertIn('<p class="mt-1 text-base text-gray-600">Liza Cruz</p>', html)

    def test_failed_save_keeps_the_saved_name_in_the_header(self):
        data = {
            **OWNER_POST,
            "first_name": "",
            "last_name": "Reyes",
            "email": "liza@example.com",
        }
        html = self._html(self.edit, data)
        self.assertIn("Back to Liza Cruz", html)
        self.assertIn('value="Reyes"', html)

    def test_required_note_without_errors(self):
        html = self._html(self.add)
        self.assertIn("are required. Everything else can be added later.", html)
        self.assertNotIn('role="alert"', html)

    def test_error_summary_counts_the_fields(self):
        User.objects.create_user(
            username="ben", email="ben@example.com", role=User.PET_OWNER
        )
        cases = (
            ({"first_name": ""}, "Please fix the item below."),
            (
                {"first_name": "", "email": "BEN@example.com"},
                "Please fix the 2 items below.",
            ),
        )
        for fields, title in cases:
            with self.subTest(fields=fields):
                html = self._html(self.add, {**OWNER_POST, **fields})
                self.assertIn('role="alert"', html)
                self.assertIn(title, html)
                self.assertIn(
                    "Nothing was saved yet. Everything you typed is still here.", html
                )
                self.assertNotIn("Everything else can be added later.", html)

    def test_email_help_and_optional(self):
        optional = (
            r'(?s)for="id_email"[^>]*>\s*Email '
            r'<span class="font-normal text-gray-600">\(optional\)</span>'
        )
        add = self._html(self.add)
        self.assertIn("a link to set a password, so they can book online", add)
        self.assertRegex(add, optional)

        with_email = self._html(self.edit)
        self.assertIn(
            "This is the email Liza Cruz logs in with. "
            "Change it only if they asked you to.",
            with_email,
        )
        self.assertNotRegex(with_email, optional)

        without = self._html(self.edit_ana)
        self.assertIn("Adding an email lets Ana Cruz log in.", without)
        self.assertRegex(without, optional)

    def test_one_primary_button(self):
        for url, label in ((self.add, "Add owner"), (self.edit, "Save changes")):
            with self.subTest(label=label):
                html = self._html(url)
                self.assertEqual(html.count("btn-primary"), 1)
                self.assertRegex(
                    html,
                    r'(?s)<button type="submit" class="btn btn-primary[^"]*">'
                    rf"\s*<svg.*?</svg>\s*{label}</button>",
                )

    def test_add_note_only_on_add(self):
        note = "where you can add pets or book a walk-in."
        self.assertIn(note, self._html(self.add))
        self.assertNotIn(note, self._html(self.edit))

    def test_cancel_targets(self):
        cancel = (
            '<a href="{}" class="btn btn-outline w-full font-semibold sm:w-auto">'
            "Cancel</a>"
        )
        self.assertIn(cancel.format(reverse("admin_owner_list")), self._html(self.add))
        detail = reverse("admin_owner_detail", args=[self.liza.pk])
        self.assertIn(cancel.format(detail), self._html(self.edit))

    def test_photo_card_without_a_photo(self):
        html = self._html(self.add)
        self.assertRegex(
            html,
            r'(?s)<label for="id_profile_photo" class="btn btn-outline[^"]*">'
            r"\s*<svg.*?</svg>\s*Choose a photo",
        )
        self.assertRegex(html, r'<input type="file" name="profile_photo"[^>]*sr-only')
        self.assertIn("Without a photo, the initials are shown.", html)
        self.assertNotIn("Remove the photo", html)
        self.assertNotIn("Choose the photo again.", html)

    def test_photo_card_with_a_photo(self):
        self.liza.profile_photo.name = "owners/liza.jpg"
        self.liza.save()
        html = self._html(self.edit)
        self.assertIn("Choose a new photo", html)
        self.assertIn("Leave it as it is to keep the current photo.", html)
        self.assertIn('name="profile_photo-clear"', html)
        self.assertIn("Remove the photo", html)
        self.assertNotIn("Remove the photo", self._html(self.edit_ana))

    def test_remove_the_photo_clears_it(self):
        self.liza.profile_photo.name = "owners/liza.jpg"
        self.liza.save()
        self.client.post(
            self.edit,
            {**OWNER_POST, "email": "liza@example.com", "profile_photo-clear": "on"},
        )
        self.liza.refresh_from_db()
        self.assertFalse(self.liza.profile_photo)

    def test_repick_line_only_after_errors(self):
        line = "Choose the photo again. Browsers can't keep it after a problem."
        self.assertNotIn(line, self._html(self.add))
        self.assertIn(line, self._html(self.add, {**OWNER_POST, "first_name": ""}))

    def test_address_card_marinduque_mode(self):
        official = barangays_for("Gasan")[0]
        self.liza.province = "Marinduque"
        self.liza.city_municipality = "Gasan"
        self.liza.barangay = official
        self.liza.save()
        html = self._html(self.edit)
        self.assertIn('class="contents" data-address-pick', html)
        self.assertIn('class="hidden" data-address-typed', html)
        self.assertIn('<option value="Gasan" selected>Gasan</option>', html)
        self.assertIn(f'<option value="{official}" selected>{official}</option>', html)
        self.assertNotRegex(html, r'<select name="address_barangay"[^>]*disabled')
        self.assertNotIn("typed by hand", html)

    def test_address_card_hand_typed(self):
        self.liza.province = "marinduque"
        self.liza.city_municipality = "Gassan"
        self.liza.save()
        html = self._html(self.edit)
        self.assertIn('class="hidden" data-address-pick', html)
        self.assertIn('class="contents" data-address-typed', html)
        self.assertIn("This address was typed by hand", html)
        self.assertIn('value="Gassan"', html)

    def test_address_card_another_province_has_no_note(self):
        self.liza.province = "Batangas"
        self.liza.city_municipality = "Lipa City"
        self.liza.barangay = "Sabang"
        self.liza.save()
        html = self._html(self.edit)
        self.assertIn('<option value="other" selected>Another province</option>', html)
        self.assertIn('class="hidden" data-address-pick', html)
        self.assertIn('class="contents" data-address-typed', html)
        self.assertIn('value="Lipa City"', html)
        self.assertNotIn("typed by hand", html)

    def test_add_page_opens_on_marinduque_with_nothing_picked(self):
        html = self._html(self.add)
        self.assertIn('<option value="Marinduque" selected>Marinduque</option>', html)
        self.assertIn('<option value="other">Another province</option>', html)
        self.assertRegex(html, r'<select name="address_barangay"[^>]*disabled')
        self.assertIn("The list fills in after you choose the town.", html)
        self.assertIn("Choose the town first", html)

    def test_town_select_loads_the_barangays_over_htmx(self):
        html = self._html(self.add)
        select = re.search(r'<select name="address_town"[^>]*>', html).group(0)
        self.assertIn(f'hx-get="{reverse("admin_address_barangays")}"', select)
        self.assertIn('hx-trigger="change"', select)
        self.assertIn('hx-target="#id_address_barangay"', select)
        self.assertIn('hx-swap="innerHTML"', select)
        self.assertIn('hx-include="this"', select)

    def test_rerender_keeps_the_mode_and_the_posted_values(self):
        official = barangays_for("Boac")[1]
        html = self._html(
            self.add,
            {**OWNER_POST, "first_name": "", "address_barangay": official},
        )
        self.assertIn('class="contents" data-address-pick', html)
        self.assertIn('<option value="Boac" selected>Boac</option>', html)
        self.assertIn(f'<option value="{official}" selected>{official}</option>', html)
        barangay_select = re.search(
            r'(?s)<select name="address_barangay".*?</select>', html
        ).group(0)
        self.assertEqual(
            [v for v, _ in _options(barangay_select)[1:]], barangays_for("Boac")
        )

        data = {
            **OWNER_POST,
            "first_name": "",
            "address_province": "other",
            "province": "Batangas",
        }
        html = self._html(self.add, data)
        self.assertIn('class="contents" data-address-typed', html)
        self.assertIn('value="Batangas"', html)
        self.assertNotIn("typed by hand", html)

    def test_every_link_is_on_one_line(self):
        for url in (self.add, self.edit, self.edit_ana):
            with self.subTest(url=url):
                for tag in re.findall(r"<a\b[^>]*>", self._html(url)):
                    self.assertNotIn("\n", tag)


class AddEmailPageTests(TestCase):
    """admin/pets/owner_add_email.html."""

    def setUp(self):
        _admin(self)
        self.owner = _owner()
        self.url = reverse("admin_owner_add_email", args=[self.owner.pk])

    def test_layout_and_texts(self):
        response = self.client.get(self.url)
        html = response.content.decode()
        self.assertTemplateUsed(response, "layouts/admin.html")
        self.assertNotIn("admin-page", html)
        self.assertIn('<div class="mx-auto max-w-xl">', html)
        self.assertIn("Back to Ana Cruz", html)
        self.assertRegex(html, r"<h1[^>]*>Add email</h1>")
        self.assertIn("a link to set a password. After that they can log in", html)
        self.assertEqual(html.count("btn-primary"), 1)
        self.assertRegex(
            html,
            r'(?s)<button type="submit" class="btn btn-primary[^"]*">'
            r"\s*<svg.*?</svg>\s*Add email and send link</button>",
        )
        detail = reverse("admin_owner_detail", args=[self.owner.pk])
        self.assertIn(f'<a href="{detail}" class="btn btn-outline', html)
        for tag in re.findall(r"<a\b[^>]*>", html):
            self.assertNotIn("\n", tag)

    def test_email_already_used(self):
        User.objects.create_user(
            username="ben", email="ben@example.com", role=User.PET_OWNER
        )
        response = self.client.post(self.url, {"email": "Ben@example.com"})
        self.assertContains(response, "This email is already used by another account.")
        # The typed email is kept as typed, so it can be corrected
        self.assertContains(response, 'value="Ben@example.com"')


# ── Slice 4b-1: pets backend ─────────────────────────────────────────────────


def _pet(owner, name="Bantay", species="Dog", breed="", **fields):
    return Pet.objects.create(
        owner=owner, name=name, species=species, breed=breed, **fields
    )


def _request(pet, status=PetDeletionRequest.PENDING, created=None, reviewed=None):
    """A deletion request; created / reviewed are datetimes to set."""
    deletion_request = PetDeletionRequest.objects.create(
        pet=pet,
        requested_by=pet.owner.user,
        reason=PetDeletionRequest.PASSED_AWAY,
        status=status,
        reviewed_at=reviewed,
    )
    if created:
        PetDeletionRequest.objects.filter(pk=deletion_request.pk).update(
            created_at=created
        )
    return deletion_request


class PetListViewTests(TestCase):
    """admin_pet_list: tabs, search, old params, owner filter, order, paging."""

    def setUp(self):
        _admin(self)
        self.url = reverse("admin_pet_list")
        self.ana = _owner(email="ana@example.com")
        self.ben = _owner(first="Ben", last="Santos", email="ben@example.com")

    def _get(self, **params):
        return self.client.get(self.url, params)

    def _names(self, response):
        return [pet.name for pet in response.context["pets"]]

    def test_tabs(self):
        _pet(self.ana, "Bantay")
        _pet(self.ana, "Old", is_archived=True, archived_at=timezone.now())
        cora = _archive(_owner(first="Cora", last="Lim", email="cora@example.com"))
        _pet(cora, "Choco")
        self.assertEqual(self._names(self._get()), ["Bantay"])
        self.assertEqual(self._names(self._get(status="active")), ["Bantay"])
        self.assertEqual(self._names(self._get(status="bogus")), ["Bantay"])
        self.assertEqual(self._get(status="bogus").context["active_tab"], "active")
        archived = self._get(status="archived")
        self.assertEqual(self._names(archived), ["Choco", "Old"])
        self.assertEqual(archived.context["active_tab"], "archived")

    def test_counts_ignore_the_search(self):
        _pet(self.ana, "Bantay")
        _pet(self.ben, "Mingming", species="Cat")
        _pet(self.ana, "Old", is_archived=True)
        response = self._get(q="mingming")
        self.assertEqual(self._names(response), ["Mingming"])
        self.assertEqual(response.context["active_count"], 2)
        self.assertEqual(response.context["archived_count"], 1)
        self.assertEqual(response.context["total_count"], 1)
        self.assertTrue(response.context["is_searching"])

    def test_search_every_word_over_all_four_fields(self):
        _pet(self.ana, "Bantay", species="Dog", breed="Aspin")
        _pet(self.ben, "Mingming", species="Cat", breed="Persian")
        cases = [
            ("bantay", ["Bantay"]),
            ("cat", ["Mingming"]),
            ("aspin", ["Bantay"]),
            ("ANA", ["Bantay"]),
            ("santos", ["Mingming"]),
            ("dog ana", ["Bantay"]),
            ("persian ben", ["Mingming"]),
            ("cat cruz", []),
            ("  aspin   cruz ", ["Bantay"]),
        ]
        for q, names in cases:
            with self.subTest(q=q):
                self.assertEqual(self._names(self._get(q=q)), names)

    def test_old_search_param_and_q_wins(self):
        _pet(self.ana, "Bantay")
        _pet(self.ben, "Mingming", species="Cat")
        self.assertEqual(self._names(self._get(search="ming")), ["Mingming"])
        self.assertEqual(self._get(search="ming").context["search_query"], "ming")
        self.assertEqual(self._names(self._get(search="ming", q="bantay")), ["Bantay"])

    def test_old_species_param(self):
        _pet(self.ana, "Bantay", species="Dog")
        _pet(self.ben, "Mingming", species="Cat")
        response = self._get(species="ca")
        self.assertEqual(self._names(response), ["Mingming"])
        self.assertEqual(response.context["species_filter"], "ca")
        self.assertEqual(list(response.context["species_list"]), ["Cat", "Dog"])

    def test_owner_filter(self):
        _pet(self.ana, "Bantay")
        _pet(self.ben, "Mingming", species="Cat")
        response = self._get(owner=self.ben.pk)
        self.assertEqual(self._names(response), ["Mingming"])
        self.assertEqual(response.context["owner_filter"], self.ben)

    def test_bad_or_unknown_owner_is_ignored(self):
        _pet(self.ana, "Bantay")
        _pet(self.ben, "Mingming", species="Cat")
        for value in ("abc", "99999", "", "1.5", "-"):
            with self.subTest(owner=value):
                response = self._get(owner=value)
                self.assertEqual(response.status_code, 200)
                self.assertIsNone(response.context["owner_filter"])
                self.assertEqual(self._names(response), ["Bantay", "Mingming"])

    def test_order_by_name_then_owner_first_name(self):
        _pet(self.ben, "Bantay")
        _pet(self.ana, "Bantay")
        _pet(self.ben, "Arf")
        pets = list(self._get().context["pets"])
        self.assertEqual(
            [(pet.name, pet.owner.first_name) for pet in pets],
            [("Arf", "Ben"), ("Bantay", "Ana"), ("Bantay", "Ben")],
        )

    def test_25_per_page_and_the_pager_keeps_params(self):
        for i in range(30):
            _pet(self.ana, f"Pet{i:02d}")
        first = self._get(status="active", q="pet", owner=self.ana.pk)
        self.assertEqual(len(first.context["pets"]), 25)
        self.assertEqual(first.context["prev_page_query"], "")
        self.assertEqual(
            first.context["next_page_query"],
            f"?status=active&q=pet&owner={self.ana.pk}&page=2",
        )
        second = self._get(status="active", q="pet", owner=self.ana.pk, page=2)
        self.assertEqual(self._names(second), [f"Pet{i}" for i in range(25, 30)])
        self.assertEqual(
            second.context["prev_page_query"],
            f"?status=active&q=pet&owner={self.ana.pk}&page=1",
        )
        self.assertEqual(second.context["next_page_query"], "")
        self.assertEqual(second.context["total_count"], 30)

    def test_has_pending_deletion(self):
        waiting = _pet(self.ana, "Bantay")
        answered = _pet(self.ana, "Choco")
        _pet(self.ana, "Dash")
        _request(waiting)
        _request(answered, status=PetDeletionRequest.REJECTED)
        flags = {
            pet.name: pet.has_pending_deletion for pet in self._get().context["pets"]
        }
        self.assertEqual(flags, {"Bantay": True, "Choco": False, "Dash": False})

    def test_pending_requests_count(self):
        _request(_pet(self.ana, "Bantay"))
        _request(_pet(self.ben, "Mingming"))
        _request(_pet(self.ben, "Old"), status=PetDeletionRequest.APPROVED)
        self.assertEqual(self._get(q="zzz").context["pending_requests_count"], 2)

    def test_query_count_is_flat(self):
        """The list's own queries don't grow with the number of pets.
        Measured on pet_list_context plus each row's owner, owner email and
        flag."""

        def run():
            context = pet_list_context(QueryDict(f"owner={self.ana.pk}"))
            list(context["species_list"])
            for pet in context["pets"]:
                (pet.owner.full_name, pet.owner.user.email, pet.has_pending_deletion)

        for i in range(3):
            _request(_pet(self.ana, f"Pet{i:02d}"))
        with CaptureQueriesContext(connection) as small:
            run()
        for i in range(3, 30):
            _request(_pet(self.ana, f"Pet{i:02d}"))
        with self.assertNumQueries(len(small)):
            run()


class DeletionRequestsViewTests(TestCase):
    """admin_deletion_requests: waiting oldest first, history newest first."""

    def setUp(self):
        _admin(self)
        self.url = reverse("admin_deletion_requests")
        self.owner = _owner(email="ana@example.com")
        self.now = timezone.now()

    def test_pending_oldest_first(self):
        newer = _request(_pet(self.owner, "A"), created=self.now - timedelta(days=1))
        older = _request(_pet(self.owner, "B"), created=self.now - timedelta(days=3))
        middle = _request(_pet(self.owner, "C"), created=self.now - timedelta(days=2))
        context = self.client.get(self.url).context
        self.assertEqual(list(context["pending"]), [older, middle, newer])
        self.assertEqual(context["pending_count"], 3)

    def test_history_newest_first_by_reviewed_at(self):
        old = _request(
            _pet(self.owner, "A"),
            status=PetDeletionRequest.APPROVED,
            reviewed=self.now - timedelta(days=5),
        )
        new = _request(
            _pet(self.owner, "B"),
            status=PetDeletionRequest.REJECTED,
            reviewed=self.now - timedelta(days=1),
        )
        _request(_pet(self.owner, "C"))
        context = self.client.get(self.url).context
        self.assertEqual(list(context["resolved"]), [new, old])

    def test_history_25_per_page(self):
        for i in range(30):
            _request(
                _pet(self.owner, f"Pet{i:02d}"),
                status=PetDeletionRequest.APPROVED,
                reviewed=self.now - timedelta(days=i),
            )
        first = self.client.get(self.url).context
        self.assertEqual(len(first["resolved"]), 25)
        self.assertEqual(first["resolved"][0].pet.name, "Pet00")
        self.assertEqual(first["next_page_query"], "?page=2")
        second = self.client.get(self.url, {"page": 2}).context
        self.assertEqual(
            [r.pet.name for r in second["resolved"]],
            [f"Pet{i}" for i in range(25, 30)],
        )
        self.assertEqual(second["prev_page_query"], "?page=1")
        self.assertEqual(second["next_page_query"], "")


class AnswerDeletionTests(TestCase):
    """Archive (admin_approve_deletion) and Keep (admin_reject_deletion)."""

    def setUp(self):
        self.admin = _admin(self)
        self.owner = _owner(email="ana@example.com")
        self.pet = _pet(self.owner)
        self.request = _request(self.pet)
        self.requests_url = reverse("admin_deletion_requests")
        self.pet_url = reverse("admin_pet_detail", args=[self.pet.pk])

    def _post(self, approve=True, deletion_request=None, **data):
        name = "admin_approve_deletion" if approve else "admin_reject_deletion"
        url = reverse(name, args=[(deletion_request or self.request).pk])
        return self.client.post(url, data)

    def _refresh(self):
        self.pet.refresh_from_db()
        self.request.refresh_from_db()

    def _answers(self):
        return Notification.objects.filter(
            notification_type=Notification.PET_DELETION_ANSWERED
        )

    def test_get_does_nothing(self):
        for name in ("admin_approve_deletion", "admin_reject_deletion"):
            with self.subTest(name=name):
                response = self.client.get(reverse(name, args=[self.request.pk]))
                self.assertRedirects(
                    response, self.requests_url, fetch_redirect_response=False
                )
        self._refresh()
        self.assertEqual(self.request.status, PetDeletionRequest.PENDING)
        self.assertFalse(self.pet.is_archived)
        self.assertFalse(self._answers().exists())
        self.assertEqual(mail.outbox, [])

    def test_archive(self):
        response = self._post()
        self.assertRedirects(response, self.requests_url, fetch_redirect_response=False)
        self.assertEqual(
            _messages(response), ["Archived Bantay. Ana Cruz was notified."]
        )
        self._refresh()
        self.assertTrue(self.pet.is_archived)
        self.assertIsNotNone(self.pet.archived_at)
        self.assertEqual(self.request.status, PetDeletionRequest.APPROVED)
        self.assertEqual(self.request.reviewed_by, self.admin)
        self.assertIsNotNone(self.request.reviewed_at)

    def test_keep(self):
        response = self._post(approve=False)
        self.assertRedirects(response, self.requests_url, fetch_redirect_response=False)
        self.assertEqual(_messages(response), ["Kept Bantay. Ana Cruz was notified."])
        self._refresh()
        self.assertFalse(self.pet.is_archived)
        self.assertIsNone(self.pet.archived_at)
        self.assertEqual(self.request.status, PetDeletionRequest.REJECTED)
        self.assertEqual(self.request.reviewed_by, self.admin)
        self.assertIsNotNone(self.request.reviewed_at)

    def test_archive_notification_has_no_related_pet(self):
        self._post()
        answer = self._answers().get()
        self.assertEqual(answer.recipient, self.owner.user)
        self.assertIsNone(answer.related_pet)
        # 8a-2b: was "Bantay was removed" and the full sentence (now the email)
        self.assertEqual(answer.title, "Pet removed")
        self.assertEqual(
            answer.message, "Bantay · Removed from your account, as you asked."
        )
        self.assertEqual(
            mail.outbox[0].body,
            "The clinic removed Bantay from your account, as you asked.",
        )
        # 8a-2c (added check): the in-app title stays, the subject names the pet
        self.assertEqual(mail.outbox[0].subject, "Bantay was removed — Hapi Vet")

    def test_keep_notification_links_the_pet(self):
        self._post(approve=False)
        answer = self._answers().get()
        self.assertEqual(answer.recipient, self.owner.user)
        self.assertEqual(answer.related_pet, self.pet)
        # 8a-2b: was "Bantay stays on your account" and the full sentence
        self.assertEqual(answer.title, "Pet stays on your account")
        self.assertEqual(
            answer.message,
            "Bantay · The clinic kept your pet. Call us if you still want it removed.",
        )
        self.assertEqual(
            mail.outbox[0].body,
            "The clinic kept Bantay on your account. If you still want it "
            "removed, please contact the clinic.",
        )
        # 8a-2c (added check): the subject names the pet, the title stays short
        self.assertEqual(
            mail.outbox[0].subject, "Bantay stays on your account — Hapi Vet"
        )

    def test_email_sent_when_the_owner_has_one_whatever_the_preferences(self):
        self.owner.user.notification_preference.__class__.objects.filter(
            user=self.owner.user
        ).update(
            email_appointment_reminders=False,
            email_appointment_status=False,
            email_billing=False,
            email_vaccination=False,
            email_followup=False,
        )
        self._post()
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["ana@example.com"])
        # 8a-2c: the subject names the pet again (8a-2b: "Pet removed — Hapi Vet")
        self.assertEqual(mail.outbox[0].subject, "Bantay was removed — Hapi Vet")
        self.assertTrue(self._answers().get().email_sent)

    def test_no_email_still_notifies_in_app(self):
        owner = _owner(first="Lito", last="Reyes")
        pet = _pet(owner, "Choco")
        response = self._post(approve=False, deletion_request=_request(pet))
        self.assertEqual(_messages(response), ["Kept Choco. Lito Reyes was notified."])
        self.assertEqual(mail.outbox, [])
        answer = self._answers().get()
        self.assertEqual(answer.recipient, owner.user)
        self.assertFalse(answer.email_sent)

    def test_send_failure_keeps_the_save(self):
        with patch("notifications.utils.send_mail", side_effect=OSError("down")):
            response = self._post()
        self.assertEqual(
            _messages(response), ["Archived Bantay. Ana Cruz was notified."]
        )
        self._refresh()
        self.assertTrue(self.pet.is_archived)
        self.assertEqual(self.request.status, PetDeletionRequest.APPROVED)
        answer = self._answers().get()
        self.assertTrue(answer.email_failed)
        self.assertEqual(answer.email_error, "down")

    def test_notify_error_is_logged_and_keeps_the_save(self):
        with patch("pets.utils.notify", side_effect=RuntimeError("boom")):
            with self.assertLogs("pets.utils", level="ERROR"):
                self._post(approve=False)
        self._refresh()
        self.assertEqual(self.request.status, PetDeletionRequest.REJECTED)

    def test_archive_saves_both_or_neither(self):
        with patch.object(
            PetDeletionRequest, "save", side_effect=RuntimeError("db down")
        ):
            with self.assertRaises(RuntimeError):
                answer_deletion_request(self.request, self.admin, approve=True)
        self._refresh()
        self.assertFalse(self.pet.is_archived)
        self.assertIsNone(self.pet.archived_at)
        self.assertEqual(self.request.status, PetDeletionRequest.PENDING)
        self.assertFalse(self._answers().exists())

    def test_answered_request_is_refused(self):
        for status in (PetDeletionRequest.APPROVED, PetDeletionRequest.REJECTED):
            answered = _request(_pet(self.owner, f"Pet {status}"), status=status)
            for approve in (True, False):
                with self.subTest(status=status, approve=approve):
                    # Unread messages ride along in the cookie; start fresh
                    self.client.cookies.pop("messages", None)
                    response = self._post(approve=approve, deletion_request=answered)
                    self.assertRedirects(
                        response, self.requests_url, fetch_redirect_response=False
                    )
                    self.assertEqual(
                        _messages(response), ["This request was already answered."]
                    )
                    answered.refresh_from_db()
                    self.assertEqual(answered.status, status)
        self.assertFalse(self._answers().exists())

    def test_archive_refused_for_an_archived_pet(self):
        Pet.objects.filter(pk=self.pet.pk).update(is_archived=True)
        response = self._post()
        self.assertEqual(_messages(response), ["Bantay is already archived."])
        self._refresh()
        self.assertEqual(self.request.status, PetDeletionRequest.PENDING)
        self.assertFalse(self._answers().exists())

    def test_archive_refused_for_an_archived_owner(self):
        _archive(self.owner)
        response = self._post()
        self.assertEqual(_messages(response), ["Restore Ana Cruz first."])
        self._refresh()
        self.assertFalse(self.pet.is_archived)
        self.assertEqual(self.request.status, PetDeletionRequest.PENDING)
        self.assertFalse(self._answers().exists())

    def test_return_to_pet(self):
        for approve in (True, False):
            with self.subTest(approve=approve):
                deletion_request = _request(_pet(self.owner, f"Pet {approve}"))
                response = self._post(
                    approve=approve, deletion_request=deletion_request, return_to="pet"
                )
                self.assertRedirects(
                    response,
                    reverse("admin_pet_detail", args=[deletion_request.pet.pk]),
                    fetch_redirect_response=False,
                )

    def test_return_to_anything_else_is_the_requests_page(self):
        for value in ("", "list", "https://example.com/", "/admin/pets/"):
            with self.subTest(return_to=value):
                deletion_request = _request(_pet(self.owner, f"Pet {value}"))
                response = self._post(
                    deletion_request=deletion_request, return_to=value
                )
                self.assertRedirects(
                    response, self.requests_url, fetch_redirect_response=False
                )

    def test_refusal_also_honours_return_to(self):
        _archive(self.owner)
        response = self._post(return_to="pet")
        self.assertRedirects(response, self.pet_url, fetch_redirect_response=False)


class RestorePetTests(TestCase):
    """admin_pet_restore: only a pet archived on its own."""

    def setUp(self):
        _admin(self)
        self.owner = _owner(email="ana@example.com")
        self.pet = _pet(self.owner, is_archived=True, archived_at=timezone.now())
        self.url = reverse("admin_pet_restore", args=[self.pet.pk])
        self.pet_url = reverse("admin_pet_detail", args=[self.pet.pk])

    def test_url(self):
        self.assertEqual(self.url, f"/admin/pets/{self.pet.pk}/restore/")

    def test_restore(self):
        reviewed = timezone.now() - timedelta(days=2)
        approved = _request(
            self.pet, status=PetDeletionRequest.APPROVED, reviewed=reviewed
        )
        response = self.client.post(self.url)
        self.assertRedirects(response, self.pet_url, fetch_redirect_response=False)
        self.assertEqual(_messages(response), ["Restored Bantay."])
        self.pet.refresh_from_db()
        self.assertFalse(self.pet.is_archived)
        self.assertIsNone(self.pet.archived_at)
        # The request stays in the history as it was
        approved.refresh_from_db()
        self.assertEqual(approved.status, PetDeletionRequest.APPROVED)
        self.assertEqual(approved.reviewed_at, reviewed)
        self.assertEqual(PetDeletionRequest.objects.count(), 1)

    def test_get_does_nothing(self):
        response = self.client.get(self.url)
        self.assertRedirects(response, self.pet_url, fetch_redirect_response=False)
        self.pet.refresh_from_db()
        self.assertTrue(self.pet.is_archived)

    def test_refused_when_the_owner_is_archived(self):
        _archive(self.owner)
        response = self.client.post(self.url)
        self.assertRedirects(response, self.pet_url, fetch_redirect_response=False)
        self.assertEqual(
            _messages(response),
            ["Restore Ana Cruz first. Their pets come back with them."],
        )
        self.pet.refresh_from_db()
        self.assertTrue(self.pet.is_archived)

    def test_refused_when_the_pet_isnt_archived(self):
        active = _pet(self.owner, "Choco")
        url = reverse("admin_pet_restore", args=[active.pk])
        response = self.client.post(url)
        self.assertRedirects(
            response,
            reverse("admin_pet_detail", args=[active.pk]),
            fetch_redirect_response=False,
        )
        self.assertEqual(_messages(response), ["This pet isn't archived."])


class DirectArchiveTests(TestCase):
    """admin_pet_archive: the clinic archives a pet with no owner request
    (Slice 4b-3), saved as an APPROVED PetDeletionRequest by the admin."""

    VALID = {"reason": PetDeletionRequest.PASSED_AWAY, "reason_detail": ""}

    def setUp(self):
        self.admin = _admin(self)
        self.owner = _owner(email="ana@example.com")
        self.pet = _pet(self.owner)
        self.url = reverse("admin_pet_archive", args=[self.pet.pk])
        self.pet_url = reverse("admin_pet_detail", args=[self.pet.pk])

    def _post(self, **data):
        return self.client.post(self.url, {**self.VALID, **data})

    def _answers(self):
        return Notification.objects.filter(
            notification_type=Notification.PET_DELETION_ANSWERED
        )

    def _assert_nothing_saved(self):
        self.pet.refresh_from_db()
        self.assertFalse(self.pet.is_archived)
        self.assertIsNone(self.pet.archived_at)
        self.assertFalse(
            PetDeletionRequest.objects.exclude(
                status=PetDeletionRequest.PENDING
            ).exists()
        )
        self.assertFalse(self._answers().exists())
        self.assertEqual(mail.outbox, [])

    def test_url(self):
        self.assertEqual(self.url, f"/admin/pets/{self.pet.pk}/archive/")

    def test_get_does_nothing(self):
        response = self.client.get(self.url, self.VALID)
        self.assertRedirects(response, self.pet_url, fetch_redirect_response=False)
        self._assert_nothing_saved()

    def test_refused_for_an_archived_pet(self):
        Pet.objects.filter(pk=self.pet.pk).update(is_archived=True)
        response = self._post()
        self.assertRedirects(response, self.pet_url, fetch_redirect_response=False)
        self.assertEqual(_messages(response), ["This pet is already archived."])
        self.assertFalse(PetDeletionRequest.objects.exists())
        self.assertFalse(self._answers().exists())

    def test_refused_for_an_archived_owner(self):
        _archive(self.owner)
        response = self._post()
        self.assertRedirects(response, self.pet_url, fetch_redirect_response=False)
        self.assertEqual(_messages(response), ["Restore Ana Cruz first."])
        self._assert_nothing_saved()

    def test_refused_while_a_request_is_waiting(self):
        waiting = _request(self.pet)
        response = self._post()
        self.assertRedirects(response, self.pet_url, fetch_redirect_response=False)
        self.assertEqual(_messages(response), ["Answer the waiting request instead."])
        self._assert_nothing_saved()
        waiting.refresh_from_db()
        self.assertEqual(waiting.status, PetDeletionRequest.PENDING)

    def test_missing_reason_reopens_the_dialog_with_the_values(self):
        response = self._post(reason="", reason_detail="Told us at the counter.")
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "admin/pets/pet_detail.html")
        html = response.content.decode()
        dialog = re.search(
            r'(?s)<dialog id="archive-pet-dialog"[^>]*>.*?</dialog>', html
        ).group()
        self.assertRegex(
            dialog, r'<dialog id="archive-pet-dialog"[^>]* data-open-on-load>'
        )
        self.assertIn("Choose a reason.</p>", dialog)
        self.assertIn("Told us at the counter.</textarea>", dialog)
        self.assertEqual(response.context["archive_form"].data["reason"], "")
        self._assert_nothing_saved()

    def test_other_without_detail_reopens_the_dialog(self):
        response = self._post(reason=PetDeletionRequest.OTHER, reason_detail="   ")
        self.assertEqual(response.status_code, 200)
        dialog = re.search(
            r'(?s)<dialog id="archive-pet-dialog"[^>]*>.*?</dialog>',
            response.content.decode(),
        ).group()
        self.assertIn("data-open-on-load", dialog)
        self.assertIn("Please write a short reason.</p>", dialog)
        self.assertIn('<option value="other" selected>Other</option>', dialog)
        self.assertEqual(
            response.context["archive_form"].errors,
            {"reason_detail": ["Please write a short reason."]},
        )
        self._assert_nothing_saved()

    def test_archive(self):
        before = timezone.now()
        response = self._post(
            reason=PetDeletionRequest.OTHER, reason_detail="  Moved to Manila.  "
        )
        self.assertRedirects(response, self.pet_url, fetch_redirect_response=False)
        self.assertEqual(
            _messages(response), ["Archived Bantay. Ana Cruz was notified."]
        )
        self.pet.refresh_from_db()
        self.assertTrue(self.pet.is_archived)
        self.assertGreaterEqual(self.pet.archived_at, before)
        deletion_request = PetDeletionRequest.objects.get()
        self.assertEqual(deletion_request.pet, self.pet)
        self.assertEqual(deletion_request.requested_by, self.admin)
        self.assertEqual(deletion_request.reason, PetDeletionRequest.OTHER)
        self.assertEqual(deletion_request.reason_detail, "Moved to Manila.")
        self.assertEqual(deletion_request.status, PetDeletionRequest.APPROVED)
        self.assertEqual(deletion_request.reviewed_by, self.admin)
        self.assertEqual(deletion_request.reviewed_at, self.pet.archived_at)

    def test_saves_both_or_neither(self):
        with patch.object(Pet, "save", side_effect=RuntimeError("db down")):
            with self.assertRaises(RuntimeError):
                archive_pet_directly(
                    self.pet, self.admin, PetDeletionRequest.PASSED_AWAY, ""
                )
        self.assertFalse(PetDeletionRequest.objects.exists())
        self._assert_nothing_saved()

    def test_one_notification_without_related_pet(self):
        self._post(reason=PetDeletionRequest.DUPLICATE)
        answer = self._answers().get()
        self.assertEqual(Notification.objects.count(), 1)
        self.assertEqual(answer.recipient, self.owner.user)
        self.assertIsNone(answer.related_pet)
        # 8a-2b: was "Bantay was removed" and the full sentence (now the email)
        self.assertEqual(answer.title, "Pet removed")
        self.assertEqual(
            answer.message, "Bantay · Removed by the clinic (duplicate entry)."
        )
        self.assertEqual(
            mail.outbox[0].body,
            "The clinic removed Bantay from your account (reason: duplicate entry).",
        )

    def test_email_when_the_owner_has_one(self):
        self._post()
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["ana@example.com"])
        # 8a-2c: the subject names the pet again (8a-2b: "Pet removed — Hapi Vet")
        self.assertEqual(mail.outbox[0].subject, "Bantay was removed — Hapi Vet")

    def test_no_email_still_notifies_in_app(self):
        owner = _owner(first="Lito", last="Reyes")
        pet = _pet(owner, "Choco")
        response = self.client.post(
            reverse("admin_pet_archive", args=[pet.pk]), self.VALID
        )
        self.assertEqual(
            _messages(response), ["Archived Choco. Lito Reyes was notified."]
        )
        self.assertEqual(mail.outbox, [])
        self.assertEqual(self._answers().get().recipient, owner.user)

    def test_notify_error_is_logged_and_keeps_the_save(self):
        with patch("pets.utils.notify", side_effect=RuntimeError("boom")):
            with self.assertLogs("pets.utils", level="ERROR"):
                response = self._post()
        self.assertRedirects(response, self.pet_url, fetch_redirect_response=False)
        self.pet.refresh_from_db()
        self.assertTrue(self.pet.is_archived)
        self.assertEqual(
            PetDeletionRequest.objects.get().status, PetDeletionRequest.APPROVED
        )

    def test_history_says_by_the_clinic(self):
        self._post()
        by_owner = _request(
            _pet(self.owner, "Choco"),
            status=PetDeletionRequest.REJECTED,
            reviewed=timezone.now() - timedelta(days=1),
        )
        html = self.client.get(reverse("admin_deletion_requests")).content.decode()
        self.assertIn(
            '<b class="font-semibold">Bantay</b> · Ana Cruz · Passed Away '
            '<span class="text-sm text-gray-600">· By the clinic</span></span>',
            html,
        )
        self.assertIn(
            f'<b class="font-semibold">{by_owner.pet.name}</b> · Ana Cruz · '
            "Passed Away</span>",
            html,
        )
        self.assertEqual(html.count("By the clinic"), 1)

    def test_restore_works_afterwards(self):
        self._post()
        response = self.client.post(reverse("admin_pet_restore", args=[self.pet.pk]))
        self.assertEqual(_messages(response)[-1], "Restored Bantay.")
        self.pet.refresh_from_db()
        self.assertFalse(self.pet.is_archived)
        self.assertIsNone(self.pet.archived_at)
        # The clinic's request stays in the history
        self.assertEqual(
            PetDeletionRequest.objects.get().status, PetDeletionRequest.APPROVED
        )


class PetDetailContextTests(TestCase):
    """admin_pet_detail: context for the 4b-3 redesign (old keys kept)."""

    def setUp(self):
        _admin(self)
        self.owner = _owner(email="ana@example.com")
        self.pet = _pet(self.owner)
        self.today = timezone.localdate()

    def _context(self, pet=None):
        url = reverse("admin_pet_detail", args=[(pet or self.pet).pk])
        return self.client.get(url).context

    def _visit(self, days, status=Appointment.CONFIRMED, hour=9):
        return Appointment.objects.create(
            owner=self.owner,
            pet=self.pet,
            date=self.today + timedelta(days=days),
            time=time(hour, 0),
            status=status,
        )

    def _shot(self, given_days, due_days=None, name=Vaccination.ANTI_RABIES, **kw):
        return Vaccination.objects.create(
            pet=self.pet,
            vaccine_name=name,
            date_administered=self.today + timedelta(days=given_days),
            next_due_date=(
                None if due_days is None else self.today + timedelta(days=due_days)
            ),
            **kw,
        )

    def test_recent_visits_are_the_3_newest(self):
        visits = [self._visit(-days) for days in range(1, 6)]
        later_same_day = self._visit(-1, hour=15)
        context = self._context()
        self.assertEqual(
            list(context["recent_visits"]), [later_same_day, visits[0], visits[1]]
        )
        self.assertEqual(context["visits_query"], "status=all&q=Bantay")

    def test_upcoming_visits(self):
        self._visit(-1)
        self._visit(2, status=Appointment.CANCELLED)
        later = self._visit(5, status=Appointment.PENDING)
        today = self._visit(0)
        context = self._context()
        self.assertEqual(context["upcoming_count"], 2)
        self.assertEqual(context["next_upcoming_visit"], today)
        self.assertNotEqual(context["next_upcoming_visit"], later)

    def test_no_upcoming_visits(self):
        context = self._context()
        self.assertEqual(context["upcoming_count"], 0)
        self.assertIsNone(context["next_upcoming_visit"])

    def test_recent_records_and_count(self):
        records = [
            MedicalRecord.objects.create(
                pet=self.pet, record_date=self.today - timedelta(days=days)
            )
            for days in range(1, 6)
        ]
        same_day = MedicalRecord.objects.create(
            pet=self.pet, record_date=self.today - timedelta(days=1)
        )
        context = self._context()
        self.assertEqual(
            list(context["recent_records"]), [same_day, records[0], records[1]]
        )
        self.assertEqual(context["records_count"], 6)
        # Old key, every record
        self.assertEqual(len(context["medical_records"]), 6)

    def test_recent_vaccinations_and_count(self):
        shots = [self._shot(-days) for days in range(1, 5)]
        same_day = self._shot(-1, name=Vaccination.DEWORMING)
        context = self._context()
        self.assertEqual(
            list(context["recent_vaccinations"]), [same_day, shots[0], shots[1]]
        )
        self.assertEqual(context["vaccinations_count"], 5)
        self.assertEqual(len(context["vaccinations"]), 5)

    def test_vaccination_due_soon_30_day_edge(self):
        for due_days, due in ((0, True), (30, True), (31, False), (-1, False)):
            with self.subTest(due_days=due_days):
                Vaccination.objects.all().delete()
                shot = self._shot(-300, due_days)
                expected = shot if due else None
                self.assertEqual(self._context()["vaccination_due_soon"], expected)

    def test_vaccination_due_soon_is_the_earliest(self):
        self._shot(-300, 20)
        earliest = self._shot(-300, 5, name=Vaccination.DEWORMING)
        self._shot(-300, None, name=Vaccination.BORDETELLA)
        self.assertEqual(self._context()["vaccination_due_soon"], earliest)

    def test_vaccination_due_soon_leaves_out_superseded(self):
        self._shot(-300, 10)
        # A later Anti-rabies dose replaces the one due in 10 days
        self._shot(-2, 363)
        self.assertIsNone(self._context()["vaccination_due_soon"])
        # Another vaccine is not affected by that dose
        other = self._shot(-300, 12, name=Vaccination.DEWORMING)
        self.assertEqual(self._context()["vaccination_due_soon"], other)

    def test_a_corrected_due_vaccination_still_counts(self):
        due = self._shot(-300, 5, is_corrected=True, correction_note="Fixed batch")
        self._shot(-300, 10, name=Vaccination.DEWORMING)
        self.assertEqual(self._context()["vaccination_due_soon"], due)

    def test_a_corrected_later_dose_supersedes(self):
        self._shot(-300, 10)
        self._shot(-2, 363, is_corrected=True, correction_note="Fixed batch")
        self.assertIsNone(self._context()["vaccination_due_soon"])

    def test_other_vaccines_compare_the_typed_name(self):
        due = self._shot(-300, 10, name=Vaccination.OTHER, custom_vaccine_name="Lepto")
        self._shot(-2, 363, name=Vaccination.OTHER, custom_vaccine_name="Giardia")
        self.assertEqual(self._context()["vaccination_due_soon"], due)

    def test_vaccination_due_soon_is_none_for_an_archived_pet(self):
        # 5a-4: reminders are refused there, so the line isn't shown
        self._shot(-300, 5)
        Pet.objects.filter(pk=self.pet.pk).update(is_archived=True)
        context = self._context()
        self.assertIsNone(context["vaccination_due_soon"])
        self.assertIsNone(context["due_soon_days"])

    def test_vaccination_due_soon_is_none_for_an_archived_owner(self):
        self._shot(-300, 5)
        _archive(self.owner)
        context = self._context()
        self.assertIsNone(context["vaccination_due_soon"])
        self.assertIsNone(context["due_soon_days"])

    def test_pet_state_and_flags(self):
        context = self._context()
        self.assertEqual(context["pet_state"], "active")
        self.assertFalse(context["owner_archived"])
        self.assertIsNone(context["pending_deletion"])

        waiting = _request(self.pet)
        self.assertEqual(self._context()["pending_deletion"], waiting)

        _archive(self.owner)
        context = self._context()
        self.assertEqual(context["pet_state"], "owner_archived")
        self.assertTrue(context["owner_archived"])

        Pet.objects.filter(pk=self.pet.pk).update(is_archived=True)
        context = self._context()
        self.assertEqual(context["pet_state"], "archived")
        self.assertTrue(context["owner_archived"])

    def test_4b3_keys(self):
        context = self._context()
        self.assertTrue(context["show_archive_button"])
        self.assertIsNone(context["archive_request"])
        self.assertIsNone(context["due_soon_days"])
        self.assertFalse(context["archive_form"].is_bound)

        self._shot(-300, 13)
        self.assertEqual(self._context()["due_soon_days"], 13)

        waiting = _request(self.pet)
        self.assertFalse(self._context()["show_archive_button"])
        PetDeletionRequest.objects.filter(pk=waiting.pk).update(
            status=PetDeletionRequest.APPROVED, reviewed_at=timezone.now()
        )
        Pet.objects.filter(pk=self.pet.pk).update(is_archived=True)
        context = self._context()
        self.assertFalse(context["show_archive_button"])
        self.assertEqual(context["archive_request"], waiting)


class ArchivedPetGuardTests(TestCase):
    """admin_pet_edit refuses archived pets and pets of archived owners."""

    def setUp(self):
        _admin(self)
        self.owner = _owner(email="ana@example.com")
        self.pet = _pet(self.owner)
        self.url = reverse("admin_pet_edit", args=[self.pet.pk])
        self.pet_url = reverse("admin_pet_detail", args=[self.pet.pk])

    def _check_refused(self, message):
        post = {"name": "Changed", "species": "Dog", "gender": "unknown"}
        for method in ("get", "post"):
            with self.subTest(method=method):
                # Unread messages ride along in the cookie; start fresh
                self.client.cookies.pop("messages", None)
                response = getattr(self.client, method)(self.url, post)
                self.assertRedirects(
                    response, self.pet_url, fetch_redirect_response=False
                )
                self.assertEqual(_messages(response), [message])
        self.pet.refresh_from_db()
        self.assertEqual(self.pet.name, "Bantay")

    def test_archived_pet(self):
        Pet.objects.filter(pk=self.pet.pk).update(is_archived=True)
        self._check_refused("Restore Bantay first.")

    def test_archived_owner(self):
        _archive(self.owner)
        self._check_refused("Restore Ana Cruz first.")

    def test_both_archived_names_the_owner(self):
        Pet.objects.filter(pk=self.pet.pk).update(is_archived=True)
        _archive(self.owner)
        self._check_refused("Restore Ana Cruz first.")

    def test_active_pet_can_be_edited(self):
        self.assertEqual(self.client.get(self.url).status_code, 200)


class AddPetReturnToWalkInTests(TestCase):
    """admin_pet_add ?return=walkin goes back to walk-in step 2."""

    # Slice 4b-4: the species is picked from the list (species_choice)
    POST = {"name": "Bantay", "species_choice": "Dog", "gender": "unknown"}

    def setUp(self):
        _admin(self)
        self.owner = _owner(email="ana@example.com")
        self.url = reverse("admin_pet_add", args=[self.owner.pk])
        self.visit_url = reverse("admin_walkin_appointment_visit")

    def test_return_walkin_goes_to_step_2_with_the_new_pet(self):
        response = self.client.post(f"{self.url}?return=walkin", self.POST)
        pet = Pet.objects.get(owner=self.owner)
        self.assertRedirects(
            response,
            f"{self.visit_url}?owner={self.owner.pk}&pet={pet.pk}",
            fetch_redirect_response=False,
        )
        self.assertEqual(_messages(response), ["Bantay added."])

    def test_return_field_also_counts(self):
        response = self.client.post(self.url, {**self.POST, "return": "walkin"})
        pet = Pet.objects.get(owner=self.owner)
        self.assertRedirects(
            response,
            f"{self.visit_url}?owner={self.owner.pk}&pet={pet.pk}",
            fetch_redirect_response=False,
        )

    def test_kept_through_a_failed_post(self):
        response = self.client.post(f"{self.url}?return=walkin", {"species": "Dog"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["return_to"], "walkin")
        self.assertFalse(Pet.objects.exists())
        # The form has no action, so the next POST goes to the same URL
        # (?return=walkin included)
        # (the first form on the page is now the sidebar's POST-only Log out
        # form, which has an action; the pet form is the one with the species)
        forms = re.findall(r"<form\b[^>]*>", response.content.decode())
        logout_action = f'action="{reverse("logout")}"'
        form_tag = next(tag for tag in forms if logout_action not in tag)
        self.assertNotIn("action=", form_tag)
        response = self.client.post(f"{self.url}?return=walkin", self.POST)
        pet = Pet.objects.get(owner=self.owner)
        self.assertRedirects(
            response,
            f"{self.visit_url}?owner={self.owner.pk}&pet={pet.pk}",
            fetch_redirect_response=False,
        )

    def test_without_it_the_owner_page(self):
        for url in (self.url, f"{self.url}?return=https://example.com/"):
            with self.subTest(url=url):
                Pet.objects.all().delete()
                self.client.cookies.pop("messages", None)
                response = self.client.post(url, self.POST)
                self.assertRedirects(
                    response,
                    reverse("admin_owner_detail", args=[self.owner.pk]),
                    fetch_redirect_response=False,
                )
                self.assertEqual(
                    _messages(response),
                    ["Bantay has been added to Ana Cruz's account."],
                )
        self.assertEqual(self.client.get(self.url).context["return_to"], "")

    def test_archived_owner_still_refused(self):
        _archive(self.owner)
        response = self.client.post(f"{self.url}?return=walkin", self.POST)
        self.assertRedirects(
            response,
            reverse("admin_owner_detail", args=[self.owner.pk]),
            fetch_redirect_response=False,
        )
        self.assertFalse(Pet.objects.exists())

    def test_step_2_links_have_return_walkin(self):
        link = f'href="{self.url}?return=walkin"'
        empty = self.client.get(self.visit_url, {"owner": self.owner.pk})
        self.assertContains(empty, link)
        self.assertContains(empty, "Add a pet for Ana Cruz")
        _pet(self.owner)
        with_pets = self.client.get(self.visit_url, {"owner": self.owner.pk})
        self.assertContains(with_pets, link)
        self.assertContains(with_pets, "Add another pet")
        for tag in re.findall(r"<a\b[^>]*>", with_pets.content.decode()):
            self.assertNotIn("\n", tag)


def _outside_dialogs(html):
    """The page without its <dialog>s (their confirm buttons are primary)."""
    return re.sub(r"(?s)<dialog\b.*?</dialog>", "", html)


def _empty_state(html):
    """(title, text) of the page's empty state, or None."""
    match = re.search(
        r'<p class="text-lg font-semibold text-gray-900">(.*?)</p>\s*'
        r'<p class="mt-1 max-w-sm text-base text-gray-600">(.*?)</p>',
        html,
    )
    return match.groups() if match else None


class PetsListPageTests(TestCase):
    """admin/pets/pets_list.html on the new layout (Slice 4b-2)."""

    def setUp(self):
        _admin(self)
        self.url = reverse("admin_pet_list")
        self.ana = _owner(email="ana@example.com", contact_number="0917 123 4567")
        self.ben = _owner(first="Ben", last="Santos", email="ben@example.com")

    def _get(self, **params):
        return self.client.get(self.url, params)

    def _html(self, **params):
        return self._get(**params).content.decode()

    def _tabs(self, html):
        return re.search(
            r'(?s)<nav [^>]*aria-label="Pets sections">.*?</nav>', html
        ).group()

    def _many(self, count, start=0):
        for i in range(start, start + count):
            owner = _owner(
                first=f"Owner{i:02d}", last="Test", email=f"o{i}@example.com"
            )
            _request(_pet(owner, f"Pet{i:02d}", breed="Aspin"))

    # ─── Layout and header ────────────────────────────────────────────────────

    def test_new_layout_without_old_files(self):
        _pet(self.ana)
        response = self._get()
        self.assertTemplateUsed(response, "layouts/admin.html")
        for old in (
            "pets_list.css",
            "pets_list.js",
            "pet_detail.css",
            "deletion_requests.css",
            "deletion_requests.js",
            "layer(legacy)",
            "admin-page",
        ):
            with self.subTest(old=old):
                self.assertNotContains(response, old)
        self.assertNotContains(response, "Find a pet or owner")
        self.assertContains(response, "<title>Pets — Hapi Vet Admin</title>")
        self.assertContains(
            response, '<h1 class="text-2xl font-bold text-gray-900">Pets</h1>'
        )
        self.assertContains(response, "Every pet registered at the clinic.")

    def test_no_primary_button_outside_dialogs(self):
        _pet(self.ana)
        _pet(self.ana, "Old", is_archived=True, archived_at=timezone.now())
        for params in ({}, {"status": "archived"}, {"q": "zzz"}):
            with self.subTest(params=params):
                html = _outside_dialogs(self._html(**params))
                self.assertEqual(html.count("btn-primary"), 0)

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
                    f'<label for="{prefix}-pet-search" class="sr-only">'
                    "Search pets</label>",
                    html,
                )
                self.assertIn(
                    f'<input id="{prefix}-pet-search" type="search" name="q" '
                    'value="cruz" placeholder="Search pets: pet name, owner, '
                    'species or breed" class="input pl-10">',
                    html,
                )
        self.assertNotIn('name="status"', html)
        self.assertNotIn('name="owner"', html)

    def test_search_form_keeps_the_archived_tab_and_the_owner(self):
        html = self._html(status="archived", owner=self.ana.pk)
        self.assertEqual(
            html.count('<input type="hidden" name="status" value="archived">'), 2
        )
        self.assertEqual(
            html.count(f'<input type="hidden" name="owner" value="{self.ana.pk}">'),
            2,
        )

    # ─── Tabs ─────────────────────────────────────────────────────────────────

    def test_tabs_counts_and_current(self):
        _pet(self.ana)
        _pet(self.ben, "Mingming", species="Cat")
        _pet(self.ana, "Old", is_archived=True, archived_at=timezone.now())
        requests_url = reverse("admin_deletion_requests")
        tabs = self._tabs(self._html())
        self.assertRegex(
            tabs,
            rf'<a href="{self.url}\?status=active" class="[^"]*" '
            r'aria-current="page">Active<span [^>]*>2</span></a>',
        )
        self.assertRegex(
            tabs,
            rf'<a href="{self.url}\?status=archived" class="[^"]*">'
            r"Archived<span [^>]*>1</span></a>",
        )
        self.assertRegex(
            tabs,
            rf'<a href="{requests_url}" class="[^"]*">Deletion requests'
            r'<span class="rounded-full bg-gray-100 text-gray-800 [^"]*">0</span></a>',
        )
        self.assertEqual(tabs.count('aria-current="page"'), 1)
        archived = self._tabs(self._html(status="archived"))
        self.assertRegex(
            archived,
            rf'<a href="{self.url}\?status=archived" class="[^"]*" '
            r'aria-current="page">',
        )
        self.assertEqual(archived.count('aria-current="page"'), 1)

    def test_requests_count_is_yellow_only_while_waiting(self):
        pet = _pet(self.ana)
        self.assertNotIn("bg-yellow-200", self._tabs(self._html()))
        _request(pet)
        _request(_pet(self.ben, "Mingming"))
        tabs = self._tabs(self._html())
        self.assertIn(
            '<span class="rounded-full bg-yellow-200 text-yellow-900 px-2 text-sm '
            'font-semibold">2<span class="sr-only"> waiting</span></span>',
            tabs,
        )
        self.assertEqual(tabs.count("bg-yellow-200"), 1)

    def test_tab_links_keep_q_and_owner_and_drop_page(self):
        for i in range(30):
            _pet(self.ana, f"Pet{i:02d}")
        html = self._tabs(self._html(q="pet one", owner=self.ana.pk, page=2))
        for status in ("active", "archived"):
            with self.subTest(status=status):
                self.assertIn(
                    f'href="{self.url}?status={status}&amp;q=pet%20one'
                    f'&amp;owner={self.ana.pk}"',
                    html,
                )
        self.assertNotIn("page=", html)
        self.assertIn(f'href="{reverse("admin_deletion_requests")}"', html)

    # ─── Heading, results line, owner filter ──────────────────────────────────

    def test_list_head_when_not_searching(self):
        _pet(self.ana)
        _pet(self.ana, "Old", is_archived=True, archived_at=timezone.now())
        active = self._html()
        self.assertIn(
            'Active pets <span class="font-normal text-gray-600">(1)</span></h2>',
            active,
        )
        self.assertIn("A to Z by pet name", active)
        self.assertNotIn("Clear search", active)
        archived = self._html(status="archived")
        self.assertIn(
            'Archived pets <span class="font-normal text-gray-600">(1)</span></h2>',
            archived,
        )
        self.assertIn("A to Z by pet name", archived)

    def test_results_line_while_searching(self):
        _pet(self.ana, "Bantay")
        _pet(self.ana, "Bruno")
        _pet(self.ben, "Mingming", species="Cat")
        html = self._html(q="cruz")
        self.assertIn(
            '2 pets match <strong class="font-semibold">“cruz”</strong> in active '
            "pets</h2>",
            html,
        )
        self.assertNotIn("Active pets <span", html)
        self.assertIn(
            f'<a href="{self.url}?status=active" class="inline-flex min-h-11 '
            "items-center gap-2 text-base font-medium text-blue-600 underline "
            'underline-offset-2">',
            html,
        )
        self.assertIn("Clear search</a>", html)
        self.assertIn(
            '1 pet matches <strong class="font-semibold">“ming”</strong>',
            self._html(q="ming"),
        )
        self.assertIn(
            'No pet matches <strong class="font-semibold">“zzz”</strong> in '
            "archived pets</h2>",
            self._html(q="zzz", status="archived"),
        )
        # Clear search keeps the owner filter
        self.assertIn(
            f'<a href="{self.url}?status=active&amp;owner={self.ana.pk}" ',
            self._html(q="b", owner=self.ana.pk),
        )

    def test_owner_filter_line_and_show_all_pets(self):
        _pet(self.ana, "Bantay")
        _pet(self.ana, "Choco")
        _pet(self.ben, "Mingming")
        html = self._html(owner=self.ana.pk)
        self.assertIn(
            '<span>Showing the pets of <b class="font-semibold">Ana Cruz</b> (2)'
            "</span>",
            html,
        )
        self.assertRegex(html, r'<span class="grid size-8 [^"]*"[^>]*>AC</span>')
        self.assertIn(
            f'<a href="{self.url}?status=active" class="inline-flex min-h-11 '
            "items-center gap-2 text-base font-medium text-blue-600 underline "
            'underline-offset-2">',
            html,
        )
        self.assertIn("Show all pets</a>", html)
        self.assertNotIn("Active pets <span", html)
        self.assertNotIn("Mingming", html)
        # Show all pets keeps the tab and the search
        self.assertIn(
            f'<a href="{self.url}?status=archived&amp;q=b" ',
            self._html(owner=self.ana.pk, status="archived", q="b"),
        )
        self.assertNotIn("Showing the pets of", self._html())

    def test_archived_note_only_on_the_archived_tab(self):
        _pet(self.ana)
        _pet(self.ana, "Old", is_archived=True, archived_at=timezone.now())
        note = (
            "Archived pets, and pets of archived owners. Their medical records "
            "and vaccinations are kept. They don't appear in walk-ins or online "
            "booking. <b>Restore</b> brings an archived pet back; a pet whose "
            "owner is archived comes back when you restore the owner."
        )
        self.assertNotIn(note, self._html())
        self.assertIn(note, self._html(status="archived"))

    # ─── Rows ─────────────────────────────────────────────────────────────────

    def test_row_pet_half(self):
        _pet(
            self.ana,
            "Bantay",
            breed="Aspin",
            date_of_birth=timezone.localdate() - timedelta(days=800),
        )
        _pet(self.ben, "Mingming", species="Cat")
        html = self._html()
        self.assertIn(
            '<li class="flex flex-col gap-3 border-t border-gray-100 p-4 '
            "first:border-t-0 hover:bg-gray-50 md:grid "
            "md:grid-cols-[minmax(0,1fr)_1px_minmax(0,1fr)_auto] md:items-center "
            'md:gap-5 md:px-5">',
            html,
        )
        self.assertIn('aria-label="No photo of Bantay yet"', html)
        self.assertIn(
            '<p class="text-base text-gray-700">Dog · Aspin · 2 yrs</p>', html
        )
        # No breed and no date of birth: only the species
        self.assertIn('<p class="text-base text-gray-700">Cat</p>', html)
        self.assertIn(
            '<span class="hidden h-10 w-px bg-gray-200 md:block" '
            'aria-hidden="true"></span>',
            html,
        )
        self.assertNotIn("Deletion requested", html)
        self.assertNotIn("Owner archived", html)

    def test_row_owner_half(self):
        _pet(self.ana, "Bantay")
        _pet(self.ben, "Mingming")
        html = self._html()
        self.assertIn('<p class="text-sm text-gray-600">Owner</p>', html)
        for owner in (self.ana, self.ben):
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
        # The owner box is gray below md only
        self.assertIn(
            "rounded-lg bg-gray-50 px-3 py-2 md:rounded-none md:bg-transparent md:p-0",
            html,
        )

    def test_open_button(self):
        pet = _pet(self.ana, "Bantay")
        self.assertRegex(
            self._html(),
            rf'<a href="{reverse("admin_pet_detail", args=[pet.pk])}" '
            r'class="inline-flex min-h-11 w-full [^"]*border-violet-300 bg-violet-50'
            r'[^"]*sm:w-auto">Open<span class="sr-only"> Bantay</span>\s*<svg',
        )

    def test_tags(self):
        waiting = _pet(self.ana, "Bantay")
        _request(waiting)
        _pet(self.ana, "Old", is_archived=True, archived_at=timezone.now())
        cora = _archive(_owner(first="Cora", last="Lim", email="cora@example.com"))
        _pet(cora, "Choco")
        active = self._html()
        self.assertEqual(active.count("Deletion requested</span>"), 1)
        self.assertIn(
            "bg-yellow-100 px-2.5 py-1 text-sm font-medium text-yellow-900", active
        )
        archived = self._html(status="archived")
        self.assertIn("Archived today</span>", archived)
        self.assertEqual(archived.count("Owner archived</span>"), 1)

    def test_archived_pet_of_an_archived_owner_shows_only_its_own_tag(self):
        cora = _owner(first="Cora", last="Lim", email="cora@example.com")
        _pet(cora, "Choco", is_archived=True, archived_at=timezone.now())
        _archive(cora)
        html = self._html(status="archived")
        self.assertIn("Archived today</span>", html)
        self.assertNotIn("Owner archived</span>", html)

    def test_muted_avatars(self):
        _pet(self.ana, "Bantay")
        self.assertNotIn("text-gray-600 ring-1 ring-gray-300", self._html())
        _pet(self.ana, "Old", is_archived=True, archived_at=timezone.now())
        cora = _archive(_owner(first="Cora", last="Lim", email="cora@example.com"))
        _pet(cora, "Choco")
        html = self._html(status="archived")
        # Both pet tiles, and only Cora's initials
        self.assertEqual(
            html.count("bg-gray-100 text-gray-600 ring-1 ring-gray-300"), 2
        )
        self.assertEqual(html.count("text-gray-700 ring-1 ring-gray-300"), 1)

    # ─── Restore ──────────────────────────────────────────────────────────────

    def test_restore_button_and_dialog_only_for_a_pet_archived_on_its_own(self):
        old = _pet(self.ana, "Old", is_archived=True, archived_at=timezone.now())
        cora = _archive(_owner(first="Cora", last="Lim", email="cora@example.com"))
        choco = _pet(cora, "Choco")
        html = self._html(status="archived")
        self.assertIn(
            '<button type="button" class="btn btn-outline w-full font-semibold '
            f'sm:w-auto" data-dialog-open="restore-dialog-{old.pk}">',
            html,
        )
        self.assertIn('Restore<span class="sr-only"> Old</span></button>', html)
        self.assertEqual(html.count("data-dialog-open="), 1)
        self.assertEqual(html.count("<dialog "), 1)
        self.assertIn(f'<dialog id="restore-dialog-{old.pk}" ', html)
        self.assertNotIn(f"restore-dialog-{choco.pk}", html)
        # Active tab: nothing to restore
        _pet(self.ana, "Bantay")
        self.assertNotIn("<dialog ", self._html())

    def test_restore_dialog(self):
        old = _pet(self.ana, "Old", is_archived=True, archived_at=timezone.now())
        html = self._html(status="archived")
        dialog = re.search(r"(?s)<dialog\b.*?</dialog>", html).group()
        restore_url = reverse("admin_pet_restore", args=[old.pk])
        self.assertIn(
            f'<form method="post" action="{restore_url}" class="p-6 sm:p-7">',
            dialog,
        )
        self.assertIn('name="csrfmiddlewaretoken"', dialog)
        self.assertIn(f'aria-labelledby="restore-dialog-{old.pk}-title"', dialog)
        self.assertIn(">Restore Old?</h2>", dialog)
        self.assertIn(
            "Old comes back on Ana Cruz's account and on the Pets list. You can "
            "book visits for Old again. Any old deletion request stays in the "
            "history.",
            dialog,
        )
        self.assertRegex(
            dialog,
            r'(?s)<button type="submit" class="btn btn-primary [^"]*">\s*<svg[^>]*>'
            r".*?</svg>\s*Yes, restore Old\s*</button>",
        )
        self.assertIn('<path d="M3 3v5h5"', dialog)  # rotate-ccw

    # ─── Empty states and pager ───────────────────────────────────────────────

    def test_empty_states(self):
        self.assertEqual(
            _empty_state(self._html()),
            (
                "No pets yet",
                "Pets appear here when owners add them online, or when you add "
                "them from an owner's page.",
            ),
        )
        self.assertEqual(
            _empty_state(self._html(owner=self.ben.pk)),
            ("Ben Santos has no pets yet.", "Add one from their owner page."),
        )
        pet = _pet(self.ana, "Bantay")
        self.assertEqual(
            _empty_state(self._html(status="archived")),
            ("No archived pets", "Pets you archive appear here."),
        )
        self.assertEqual(
            _empty_state(self._html(q="rex")),
            (
                "No pet matches “rex”",
                "Check the spelling, or look in the Archived tab. To add a pet, "
                "open its owner first.",
            ),
        )
        self.assertEqual(
            _empty_state(self._html(q="rex", status="archived")),
            ("No pet matches “rex”", "Check the spelling, or look in the Active tab."),
        )
        pet.is_archived = True
        pet.save()
        self.assertEqual(
            _empty_state(self._html()),
            (
                "No active pets",
                "Restore one from the Archived tab, or add one from an owner's page.",
            ),
        )

    def test_pager_on_30_pets(self):
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
        """The full page costs the same with 3 pets per tab as with 30."""

        def add(start, count):
            self._many(count, start)
            for i in range(start, start + count):
                _pet(self.ana, f"Old{i:02d}", is_archived=True, archived_at=self.now)

        self.now = timezone.now()
        add(0, 3)
        small = {}
        for status in ("active", "archived"):
            self._get(status=status)  # warm-up: one-time lookups don't count
            with CaptureQueriesContext(connection) as queries:
                self._get(status=status)
            small[status] = len(queries)
        add(3, 27)
        for status in ("active", "archived"):
            with self.subTest(status=status):
                with self.assertNumQueries(small[status]):
                    self._get(status=status)

    def test_every_link_is_on_one_line(self):
        self._many(30)
        _pet(self.ana, "Old", is_archived=True, archived_at=timezone.now())
        for params in (
            {},
            {"q": "pet"},
            {"status": "archived"},
            {"q": "zzz"},
            {"owner": self.ana.pk},
        ):
            with self.subTest(params=params):
                for tag in re.findall(r"<a\b[^>]*>", self._html(**params)):
                    self.assertNotIn("\n", tag)


class DeletionRequestsPageTests(TestCase):
    """admin/pets/deletion_requests.html on the new layout (Slice 4b-2)."""

    def setUp(self):
        _admin(self)
        self.url = reverse("admin_deletion_requests")
        self.owner = _owner(email="ana@example.com")
        self.now = timezone.now()

    def _get(self, **params):
        return self.client.get(self.url, params)

    def _html(self, **params):
        return self._get(**params).content.decode()

    def _dialog(self, html, dialog_id):
        return re.search(rf'(?s)<dialog id="{dialog_id}" .*?</dialog>', html).group()

    def _waiting(self, name="Choco", detail=""):
        deletion_request = _request(_pet(self.owner, name, breed="Shih Tzu"))
        if detail:
            deletion_request.reason = PetDeletionRequest.OTHER
            deletion_request.reason_detail = detail
            deletion_request.save()
        return deletion_request

    def _answered(self, name, status, days=1):
        return _request(
            _pet(self.owner, name),
            status=status,
            reviewed=self.now - timedelta(days=days),
        )

    def test_new_layout_header_search_and_tabs(self):
        self._waiting()
        response = self._get()
        self.assertTemplateUsed(response, "layouts/admin.html")
        for old in (
            "deletion_requests.css",
            "deletion_requests.js",
            "pet_detail.css",
            "pets_list.css",
            "layer(legacy)",
            "admin-page",
        ):
            with self.subTest(old=old):
                self.assertNotContains(response, old)
        self.assertContains(
            response, "<title>Deletion requests — Hapi Vet Admin</title>"
        )
        self.assertContains(
            response, '<h1 class="text-2xl font-bold text-gray-900">Pets</h1>'
        )
        self.assertContains(response, "Every pet registered at the clinic.")
        html = response.content.decode()
        self.assertEqual(html.count('role="search"'), 2)
        pet_list = reverse("admin_pet_list")
        for prefix in ("topbar", "page"):
            with self.subTest(prefix=prefix):
                self.assertIn(f'<input id="{prefix}-pet-search" type="search"', html)
        self.assertEqual(html.count(f'<form method="get" action="{pet_list}" '), 2)
        self.assertNotIn('name="status"', html)
        tabs = re.search(
            r'(?s)<nav [^>]*aria-label="Pets sections">.*?</nav>', html
        ).group()
        self.assertRegex(
            tabs,
            rf'<a href="{self.url}" class="[^"]*" aria-current="page">'
            r'Deletion requests<span class="rounded-full bg-yellow-200 '
            r'text-yellow-900 [^"]*">1<span class="sr-only"> waiting</span>',
        )
        self.assertEqual(tabs.count('aria-current="page"'), 1)
        self.assertRegex(
            tabs,
            rf'<a href="{pet_list}\?status=active" class="[^"]*">Active'
            r"<span [^>]*>1</span>",
        )
        self.assertRegex(
            tabs,
            rf'<a href="{pet_list}\?status=archived" class="[^"]*">Archived'
            r"<span [^>]*>0</span>",
        )

    def test_tab_counts_in_the_context(self):
        self._waiting()
        _pet(self.owner, "Old", is_archived=True)
        context = self._get().context
        self.assertEqual(context["active_count"], 1)
        self.assertEqual(context["archived_count"], 1)
        self.assertEqual(context["pending_requests_count"], 1)
        self.assertEqual(context["active_tab"], "requests")
        self.assertEqual(context["q"], "")

    def test_no_primary_button_outside_dialogs(self):
        self._waiting()
        self._answered("Spot", PetDeletionRequest.APPROVED)
        html = self._html()
        self.assertIn("btn-primary", html)  # the Keep dialog's confirm
        self.assertEqual(_outside_dialogs(html).count("btn-primary"), 0)

    def test_intro(self):
        self.assertIn(
            "Owners ask here to remove a pet from their account. <b>Archive</b> "
            "hides the pet but keeps its records. <b>Keep</b> closes the request "
            "and the pet stays.",
            self._html(),
        )

    def test_request_cards(self):
        choco = self._waiting()
        muning = self._waiting(
            "Muning", detail="We registered her twice. Please remove this one."
        )
        html = self._html()
        self.assertIn(
            'Waiting for you <span class="font-normal text-gray-600">(2)</span></h2>',
            html,
        )
        self.assertIn("Oldest first", html)
        self.assertIn('aria-label="No photo of Choco yet"', html)
        self.assertIn(
            'Choco <span class="text-base font-normal text-gray-700">· Dog · '
            "Shih Tzu</span></p>",
            html,
        )
        owner_url = reverse("admin_owner_detail", args=[self.owner.pk])
        self.assertEqual(
            html.count(
                f'Asked by <a href="{owner_url}" class="font-medium text-blue-600 '
                'underline underline-offset-2">Ana Cruz</a> · Today, '
            ),
            2,
        )
        self.assertIn(
            '<span class="text-gray-600">Reason:</span> <b class="font-semibold">'
            "Passed Away</b>",
            html,
        )
        self.assertIn('<b class="font-semibold">Other</b>', html)
        self.assertIn(
            '<p class="mt-1 rounded-lg bg-gray-50 px-3 py-2 text-base text-gray-800">'
            "“We registered her twice. Please remove this one.”</p>",
            html,
        )
        self.assertEqual(html.count("rounded-lg bg-gray-50 px-3 py-2"), 1)
        self.assertIn(
            f'<a href="{reverse("admin_pet_detail", args=[choco.pet.pk])}" ', html
        )
        self.assertIn("Open Choco's page</a>", html)
        for deletion_request in (choco, muning):
            name = deletion_request.pet.name
            with self.subTest(pet=name):
                self.assertIn(
                    '<button type="button" class="btn btn-outline min-h-12 '
                    'font-semibold" data-dialog-open="keep-dialog-'
                    f'{deletion_request.pk}">',
                    html,
                )
                self.assertIn(
                    '<button type="button" class="btn min-h-12 border '
                    "border-red-300 bg-white font-semibold text-red-700 "
                    'hover:bg-red-50" data-dialog-open="archive-dialog-'
                    f'{deletion_request.pk}">',
                    html,
                )
                self.assertRegex(html, rf"</svg>\s*Keep {name}</button>")
                self.assertRegex(html, rf"</svg>\s*Archive {name}</button>")

    def test_dialogs_post_to_the_right_urls_with_return_to(self):
        choco = self._waiting()
        html = self._html()
        self.assertEqual(html.count("<dialog "), 2)
        for kind, url_name in (
            ("archive", "admin_approve_deletion"),
            ("keep", "admin_reject_deletion"),
        ):
            with self.subTest(kind=kind):
                dialog = self._dialog(html, f"{kind}-dialog-{choco.pk}")
                self.assertIn(
                    '<form method="post" action="'
                    f'{reverse(url_name, args=[choco.pk])}" class="p-6 sm:p-7">',
                    dialog,
                )
                self.assertIn('name="csrfmiddlewaretoken"', dialog)
                self.assertIn(
                    '<input type="hidden" name="return_to" value="requests">', dialog
                )
                self.assertIn(
                    "Ana Cruz gets a notification, and an email if they have one.",
                    dialog,
                )

    def test_archived_owner_has_no_archive_button_or_dialog(self):
        choco = self._waiting()
        _archive(self.owner)
        html = self._html()
        self.assertNotIn(f'data-dialog-open="archive-dialog-{choco.pk}"', html)
        self.assertNotIn(f'<dialog id="archive-dialog-{choco.pk}"', html)
        self.assertNotRegex(html, r"</svg>\s*Archive Choco</button>")
        owner_url = reverse("admin_owner_detail", args=[self.owner.pk])
        self.assertIn(
            f'<span><a href="{owner_url}" class="font-medium text-blue-600 '
            'underline underline-offset-2">Ana Cruz</a> is archived. Restore '
            "Ana Cruz first to archive Choco.</span></p>",
            html,
        )
        self.assertIn('<path d="M12 16v-4"', html)  # info icon
        # Keep still works
        self.assertIn(f'data-dialog-open="keep-dialog-{choco.pk}"', html)
        self.assertRegex(html, r"</svg>\s*Keep Choco</button>")
        self.assertIn(f'<dialog id="keep-dialog-{choco.pk}"', html)

    def test_archive_dialog_text(self):
        choco = self._waiting()
        dialog = self._dialog(self._html(), f"archive-dialog-{choco.pk}")
        self.assertIn(">Archive Choco?</h2>", dialog)
        self.assertIn(
            "Ana Cruz asked to remove Choco (reason: passed away). Choco will be "
            "hidden from their account and from walk-ins. Medical records and "
            "vaccinations are kept, and the pet stays on the Archived tab.",
            dialog,
        )
        self.assertRegex(
            dialog,
            r'(?s)<button type="submit" class="btn btn-danger [^"]*">\s*<svg[^>]*>'
            r".*?</svg>\s*Yes, archive Choco\s*</button>",
        )

    def test_keep_dialog_text(self):
        choco = self._waiting()
        dialog = self._dialog(self._html(), f"keep-dialog-{choco.pk}")
        self.assertIn(">Keep Choco?</h2>", dialog)
        self.assertIn(
            "The request is closed and Choco stays on Ana Cruz's account. "
            "Ana Cruz can ask again later.",
            dialog,
        )
        self.assertRegex(
            dialog,
            r'(?s)<button type="submit" class="btn btn-primary [^"]*">\s*<svg[^>]*>'
            r".*?</svg>\s*Yes, keep Choco\s*</button>",
        )

    def test_nothing_waiting(self):
        self._answered("Spot", PetDeletionRequest.APPROVED)
        html = self._html()
        self.assertEqual(
            _empty_state(html),
            (
                "No requests waiting",
                "When an owner asks to remove a pet, it shows up here.",
            ),
        )
        self.assertNotIn("Waiting for you", html)
        self.assertNotIn("<dialog ", html)
        # The history stays below
        self.assertIn("Already answered <span", html)

    def test_history(self):
        self._answered("Spot", PetDeletionRequest.APPROVED, days=5)
        self._answered("Kitty", PetDeletionRequest.REJECTED, days=1)
        html = self._html()
        self.assertIn(
            'Already answered <span class="font-normal text-gray-600">(2)</span></h2>',
            html,
        )
        self.assertIn("Newest first", html)
        self.assertIn(
            '<b class="font-semibold">Spot</b> · Ana Cruz · Passed Away</span>', html
        )
        self.assertRegex(html, r"</svg>\s*Archived</span>")
        self.assertIn(
            "bg-green-100 px-2.5 py-1 text-sm font-medium text-green-800", html
        )
        self.assertRegex(html, r"</svg>\s*Kept</span>")
        self.assertLess(html.index(">Kitty</b>"), html.index(">Spot</b>"))
        # Only the archived pet's tile is gray
        self.assertEqual(
            html.count("bg-gray-100 text-gray-600 ring-1 ring-gray-300"), 1
        )
        self.assertIn("Showing all 2", html)
        self.assertNotIn("Nothing answered yet.", html)

    def test_nothing_answered_yet(self):
        self._waiting()
        html = self._html()
        self.assertIn(
            '<p class="text-base text-gray-600">Nothing answered yet.</p>', html
        )
        self.assertNotIn("Newest first", html)

    def test_history_pager(self):
        for i in range(30):
            self._answered(f"Pet{i:02d}", PetDeletionRequest.APPROVED, days=i)
        html = self._html()
        self.assertIn("Showing 1–25 of 30", html)
        self.assertIn('<a href="?page=2" class="btn btn-outline">', html)

    def test_query_count_is_flat(self):
        """The full page costs the same with 3 requests of each kind as with 30."""

        def add(start, count):
            for i in range(start, start + count):
                self._waiting(f"Wait{i:02d}")
                self._answered(f"Done{i:02d}", PetDeletionRequest.REJECTED, days=i)

        add(0, 3)
        self._get()  # warm-up: one-time lookups don't count
        with CaptureQueriesContext(connection) as small:
            self._get()
        add(3, 27)
        with self.assertNumQueries(len(small)):
            self._get()

    def test_every_link_is_on_one_line(self):
        self._waiting(detail="Twice.")
        self._answered("Spot", PetDeletionRequest.APPROVED)
        for tag in re.findall(r"<a\b[^>]*>", self._html()):
            self.assertNotIn("\n", tag)


class PetDetailPageTests(TestCase):
    """admin/pets/pet_detail.html on the new layout (Slice 4b-3)."""

    def setUp(self):
        self.admin = _admin(self)
        self.owner = _owner(
            email="ana@example.com",
            password="pw-12345!",
            contact_number="0917 123 4567",
        )
        self.pet = _pet(self.owner, breed="Aspin")
        self.today = timezone.localdate()

    def _get(self, pet=None):
        url = reverse("admin_pet_detail", args=[(pet or self.pet).pk])
        return self.client.get(url)

    def _html(self, pet=None):
        return self._get(pet).content.decode()

    def _page(self, pet=None):
        """The page without its <dialog>s: the cards and their buttons."""
        return _outside_dialogs(self._html(pet))

    def _dialogs(self, pet=None):
        """{dialog id: its markup}."""
        return dict(
            re.findall(r'(?s)<dialog id="([a-z0-9-]+)"(.*?)</dialog>', self._html(pet))
        )

    def _archive_pet(self, pet=None):
        pet = pet or self.pet
        Pet.objects.filter(pk=pet.pk).update(
            is_archived=True, archived_at=timezone.now()
        )
        pet.refresh_from_db()
        return pet

    def _visit(self, days=0, status=Appointment.CONFIRMED, hour=9, **fields):
        return Appointment.objects.create(
            owner=self.owner,
            pet=self.pet,
            date=self.today + timedelta(days=days),
            time=time(hour, 0),
            status=status,
            **fields,
        )

    def _record(self, days=-1, **fields):
        return MedicalRecord.objects.create(
            pet=self.pet, record_date=self.today + timedelta(days=days), **fields
        )

    def _shot(self, given_days=-1, due_days=None, name=Vaccination.ANTI_RABIES, **kw):
        return Vaccination.objects.create(
            pet=self.pet,
            vaccine_name=name,
            date_administered=self.today + timedelta(days=given_days),
            next_due_date=(
                None if due_days is None else self.today + timedelta(days=due_days)
            ),
            **kw,
        )

    def _card(self, html, title_id):
        return re.search(
            rf'(?s)<section class="card" aria-labelledby="{title_id}">.*?</section>',
            html,
        ).group()

    # ─── Layout and header ────────────────────────────────────────────────────

    def test_new_layout_and_the_page_parses(self):
        response = self._get()
        self.assertTemplateUsed(response, "layouts/admin.html")
        for old in ("pet_detail.css", "layer(legacy)", "admin-page", "back-link"):
            with self.subTest(old=old):
                self.assertNotContains(response, old)
        self.assertContains(response, "<title>Bantay — Hapi Vet Admin</title>")
        # html=True parses the whole page (the old one was missing a </div>)
        self.assertContains(
            response,
            '<h1 class="text-2xl font-bold text-gray-900">Bantay</h1>',
            html=True,
        )

    def test_header(self):
        Pet.objects.filter(pk=self.pet.pk).update(
            date_of_birth=self.today - timedelta(days=3 * 365 + 10)
        )
        html = self._html()
        self.assertRegex(
            html,
            rf'<a href="{reverse("admin_pet_list")}" class="[^"]*">'
            r"\s*<svg[^>]*>(?s:.*?)</svg>\s*Back to pets</a>",
        )
        self.assertRegex(html, r'<span class="grid size-16 rounded-2xl [^"]*bg-violet')
        owner_url = reverse("admin_owner_detail", args=[self.owner.pk])
        self.assertIn(
            f'Dog · Aspin · 3 yrs · Owner: <a href="{owner_url}" class="font-medium '
            'text-blue-600 underline underline-offset-2">Ana Cruz</a></p>',
            html,
        )
        self.assertNotRegex(html, r"</svg>\s*Archived</span>")
        self.assertNotIn("Deletion requested</span>", html)

    def test_header_leaves_out_empty_parts(self):
        pet = _pet(self.owner, "Mingming", "Cat")
        self.assertIn(
            '<p class="mt-1 text-base text-gray-600">Cat · Owner: <a ',
            self._html(pet),
        )

    def test_tags_and_gray_picture(self):
        _request(self.pet)
        html = self._html()
        self.assertRegex(html, r"</svg>\s*Deletion requested</span>")
        self.assertRegex(html, r'<span class="grid size-16 rounded-2xl [^"]*bg-violet')
        self._archive_pet()
        PetDeletionRequest.objects.all().delete()
        html = self._html()
        self.assertRegex(html, r"</svg>\s*Archived</span>")
        self.assertNotIn("Deletion requested</span>", html)
        self.assertRegex(html, r'<span class="grid size-16 rounded-2xl [^"]*bg-gray')

    def test_owner_archived_mutes_the_picture_without_the_tag(self):
        _archive(self.owner)
        html = self._html()
        self.assertRegex(html, r'<span class="grid size-16 rounded-2xl [^"]*bg-gray')
        self.assertNotRegex(html, r"</svg>\s*Archived</span>")

    # ─── Banners ──────────────────────────────────────────────────────────────

    def test_deletion_banner(self):
        waiting = _request(self.pet)
        waiting.reason = PetDeletionRequest.OTHER
        waiting.reason_detail = "We moved abroad."
        waiting.save()
        page = self._page()
        self.assertIn(
            '<p class="text-lg font-semibold">Ana Cruz asked to remove Bantay</p>',
            page,
        )
        self.assertIn("Asked today. Reason: <b>other</b>.</p>", page)
        self.assertIn("“We moved abroad.”</p>", page)
        self.assertRegex(
            page,
            r'<button type="button" class="btn btn-outline [^"]*" '
            rf'data-dialog-open="keep-dialog-{waiting.pk}"[^>]*>\s*<svg[^>]*>'
            r"(?s:.*?)</svg>\s*Keep Bantay</button>",
        )
        self.assertRegex(
            page,
            r'<button type="button" class="btn [^"]*text-red-700[^"]*" '
            rf'data-dialog-open="archive-dialog-{waiting.pk}"[^>]*>\s*<svg[^>]*>'
            r"(?s:.*?)</svg>\s*Archive Bantay</button>",
        )

    def test_deletion_banner_without_archive_while_the_owner_is_archived(self):
        waiting = _request(self.pet)
        _archive(self.owner)
        page = self._page()
        self.assertIn("Ana Cruz asked to remove Bantay", page)
        self.assertIn(f'data-dialog-open="keep-dialog-{waiting.pk}"', page)
        self.assertNotIn(f'data-dialog-open="archive-dialog-{waiting.pk}"', page)

    def test_no_banners_for_an_active_pet(self):
        html = self._html()
        for text in ("asked to remove", "is archived</p>", "owner is archived"):
            with self.subTest(text=text):
                self.assertNotIn(text, html)

    def test_archived_banner_after_the_owner_asked(self):
        _request(
            self.pet,
            status=PetDeletionRequest.APPROVED,
            reviewed=timezone.now() - timedelta(days=1),
        )
        self._archive_pet()
        html = self._html()
        self.assertIn('<p class="text-lg font-semibold">Bantay is archived</p>', html)
        self.assertIn(
            "Archived on today after Ana Cruz asked to remove the pet (reason: "
            "passed away). Records and vaccinations are kept.</p>",
            html,
        )

    def test_archived_banner_by_the_clinic(self):
        # An older owner request was kept; the clinic archived the pet later
        _request(
            self.pet,
            status=PetDeletionRequest.REJECTED,
            reviewed=timezone.now() - timedelta(days=3),
        )
        archive_pet_directly(self.pet, self.admin, PetDeletionRequest.REHOMED, "")
        self.assertIn(
            "Archived on today by the clinic (reason: rehomed). Records and "
            "vaccinations are kept.</p>",
            self._html(),
        )

    def test_archived_banner_without_a_request(self):
        self._archive_pet()
        self.assertIn(
            "Archived on today. Records and vaccinations are kept.</p>", self._html()
        )

    def test_owner_archived_banner(self):
        _archive(self.owner)
        html = self._html()
        self.assertIn(
            '<p class="text-lg font-semibold">Bantay\'s owner is archived</p>', html
        )
        self.assertIn(
            "Ana Cruz was archived on today, so Bantay is hidden from walk-ins and "
            "online booking. Records and vaccinations are kept.</p>",
            html,
        )

    # ─── Pet details ──────────────────────────────────────────────────────────

    def test_info_card_values(self):
        born = self.today - timedelta(days=3 * 365 + 10)
        Pet.objects.filter(pk=self.pet.pk).update(
            gender=Pet.MALE,
            date_of_birth=born,
            color="Brown and white",
            weight="12.50",
            registration_number="MRQ-2023-0412",
        )
        card = self._card(self._html(), "info-title")
        self.assertIn(">Pet details</h2>", card)
        self.assertIn('<dl class="grid grid-cols-1 gap-x-8 sm:grid-cols-2">', card)
        for label, value in (
            ("Species", "Dog"),
            ("Breed", "Aspin"),
            ("Sex", "Male"),
            ("Born", f"{born.strftime('%b')} {born.day}, {born.year} · 3 yrs"),
            ("Color", "Brown and white"),
            ("Weight", "12.5 kg"),
            ("Registration no.", "MRQ-2023-0412"),
        ):
            with self.subTest(label=label):
                self.assertRegex(
                    card,
                    rf'<dt class="text-sm text-gray-600">{re.escape(label)}</dt>\s*'
                    rf'<dd class="[^"]*">{re.escape(value)}</dd>',
                )
        self.assertNotIn("Not given", card)

    def test_info_card_empty_texts(self):
        pet = _pet(self.owner, "Mingming", "Cat")
        card = self._card(self._html(pet), "info-title")
        self.assertEqual(card.count('<span class="text-gray-600">Not given</span>'), 4)
        self.assertIn('<span class="text-gray-600">Unknown</span>', card)
        self.assertIn('<span class="text-gray-600">None</span>', card)

    # ─── Recent visits ────────────────────────────────────────────────────────

    def test_recent_visits(self):
        grooming = Service.objects.create(
            service_name="Grooming", status=Service.ACTIVE
        )
        for days in range(-5, -1):
            self._visit(days, Appointment.COMPLETED)
        latest = self._visit(0, hour=14, service=grooming)
        card = self._card(self._html(), "visits-title")
        self.assertEqual(card.count("<li "), 3)
        self.assertIn(
            '<span class="w-32 text-base font-medium text-gray-900">Today</span>', card
        )
        self.assertIn("2:00 PM</span>", card)
        self.assertIn("Grooming</span>", card)
        self.assertIn("Appointment</span>", card)
        self.assertIn(
            '<span class="badge bg-blue-100 text-blue-800">Confirmed</span>', card
        )
        self.assertIn(
            f'<a href="{reverse("admin_appointment_detail", args=[latest.pk])}" '
            'class="inline-flex min-h-11 items-center px-2 text-base font-medium '
            'text-blue-600 underline underline-offset-2">Open'
            '<span class="sr-only"> visit on today</span></a>',
            card,
        )
        self.assertRegex(
            card,
            rf'<a href="{reverse("admin_appointments")}\?status=all&amp;q=Bantay" '
            r'class="[^"]*">See all visits for Bantay\s*<svg',
        )
        self.assertNotIn("No visits yet.", card)

    def test_no_visits_yet(self):
        card = self._card(self._html(), "visits-title")
        self.assertIn('<p class="text-base text-gray-600">No visits yet.</p>', card)
        self.assertNotIn("See all visits", card)

    # ─── Medical records ──────────────────────────────────────────────────────

    def test_records(self):
        self._record(-30, diagnosis="Old")
        followup = self._record(
            -10,
            diagnosis="Mild skin allergy",
            follow_up_required=True,
            follow_up_date=self.today + timedelta(days=1),
            private_notes="SECRET-NOTE",
        )
        self._record(-5, diagnosis="x" * 100)
        self._record(-2)
        html = self._html()
        card = self._card(html, "records-title")
        self.assertEqual(card.count("<li "), 3)
        self.assertNotIn(">Old</span>", card)
        self.assertIn(
            '<span class="w-32 text-base font-medium text-gray-900">'
            f"{friendly_date(followup.record_date)}</span>",
            card,
        )
        self.assertIn("Mild skin allergy</span>", card)
        self.assertIn(f"{'x' * 79}…</span>", card)
        self.assertIn('<span class="text-gray-600">No diagnosis written</span>', card)
        self.assertRegex(
            card,
            r'<span class="[^"]*bg-blue-100[^"]*text-blue-800">\s*<svg[^>]*>'
            r"(?s:.*?)</svg>\s*Follow-up tomorrow</span>",
        )
        self.assertEqual(card.count("Follow-up "), 1)
        detail = reverse("admin_medical_record_detail", args=[followup.pk])
        self.assertIn(f'<a href="{detail}" ', card)
        history = reverse("admin_pet_medical_history", args=[self.pet.pk])
        self.assertRegex(
            card,
            rf'<a href="{history}" class="[^"]*">'
            r"See full medical history \(4 records\)\s*<svg",
        )
        self.assertNotIn("SECRET-NOTE", html)

    def test_follow_up_without_a_date_has_no_pill(self):
        self._record(-1, follow_up_required=True)
        self.assertNotIn("Follow-up ", self._card(self._html(), "records-title"))

    def test_no_records_yet(self):
        card = self._card(self._html(), "records-title")
        self.assertIn(
            '<p class="text-base text-gray-600">No medical records yet.</p>', card
        )
        self.assertNotIn("See full medical history", card)

    def test_add_buttons_only_while_active(self):
        add_record = reverse("admin_medical_record_create", args=[self.pet.pk])
        add_shot = reverse("admin_vaccination_standalone_create", args=[self.pet.pk])
        html = self._html()
        self.assertRegex(
            self._card(html, "records-title"),
            rf'<a href="{add_record}" class="btn btn-outline font-semibold">\s*<svg'
            r"(?s:.*?)</svg>\s*Add record</a>",
        )
        self.assertRegex(
            self._card(html, "vaccinations-title"),
            rf'<a href="{add_shot}" class="btn btn-outline font-semibold">\s*<svg'
            r"(?s:.*?)</svg>\s*Add vaccination</a>",
        )
        archived = self._archive_pet(_pet(self.owner, "Old"))
        lito = _owner("Lito", "Reyes")
        spot = _pet(lito, "Spot")
        _archive(lito)
        for pet in (archived, spot):
            with self.subTest(pet=pet.name):
                html = self._html(pet)
                for url_name in (
                    "admin_medical_record_create",
                    "admin_vaccination_standalone_create",
                ):
                    self.assertNotIn(reverse(url_name, args=[pet.pk]), html)

    # ─── Vaccinations ─────────────────────────────────────────────────────────

    def test_vaccinations(self):
        self._shot(-400)
        other = self._shot(
            -20,
            name=Vaccination.OTHER,
            custom_vaccine_name="Lepto",
            is_corrected=True,
            correction_note="Fixed the batch",
        )
        self._shot(-10, 365, name=Vaccination.SIX_IN_ONE)
        self._shot(-1, name=Vaccination.DEWORMING)
        card = self._card(self._html(), "vaccinations-title")
        self.assertEqual(card.count("<li "), 3)
        given = friendly_date(other.date_administered, "mid")
        self.assertIn(
            f'<b class="font-semibold">Lepto</b> · given {given}</span>', card
        )
        self.assertIn(
            '<b class="font-semibold">Deworming</b> · given yesterday</span>', card
        )
        self.assertRegex(card, r"</svg>\s*Corrected</span>")
        self.assertEqual(card.count("Corrected</span>"), 1)
        due = friendly_date(self.today + timedelta(days=365), "mid")
        self.assertIn(f"Next due {due}</span>", card)
        self.assertEqual(card.count("No next date</span>"), 2)
        # 5b-5: Open goes to the pet's vaccinations with that dose highlighted
        # (was the edit form)
        history = reverse("admin_pet_vaccination_history", args=[self.pet.pk])
        self.assertIn(
            f'<a href="{history}?highlight={other.pk}#vaccination-{other.pk}" ', card
        )
        self.assertNotIn(reverse("admin_vaccination_edit", args=[other.pk]), card)
        self.assertRegex(
            card,
            rf'<a href="{history}" class="[^"]*">See all vaccinations \(4\)\s*<svg',
        )

    def test_no_vaccinations_yet(self):
        card = self._card(self._html(), "vaccinations-title")
        self.assertIn(
            '<p class="text-base text-gray-600">No vaccinations yet.</p>', card
        )
        self.assertNotIn("See all vaccinations", card)
        self.assertNotIn(" is due ", card)

    def test_due_soon_line(self):
        in_13 = friendly_date(self.today + timedelta(days=13), "mid")
        cases = [
            (13, f"<b>Anti-rabies is due {in_13}</b> (in 13 days).</p>"),
            (1, "<b>Anti-rabies is due tomorrow</b>.</p>"),
            (0, "<b>Anti-rabies is due today</b>.</p>"),
        ]
        for due_days, text in cases:
            with self.subTest(due_days=due_days):
                Vaccination.objects.all().delete()
                self._shot(-300, due_days)
                card = self._card(self._html(), "vaccinations-title")
                self.assertIn(text, card)
                self.assertIn("border-yellow-300 bg-yellow-50", card)
        Vaccination.objects.all().delete()
        self._shot(-300, 31)
        self.assertNotIn(" is due ", self._card(self._html(), "vaccinations-title"))

    # ─── Actions ──────────────────────────────────────────────────────────────

    def test_actions_when_active(self):
        page = self._page()
        self.assertEqual(page.count("btn-primary"), 1)
        self.assertRegex(
            page,
            rf'<a href="{reverse("admin_walkin_appointment_visit")}'
            rf'\?owner={self.owner.pk}&amp;pet={self.pet.pk}" class="btn btn-primary '
            r'[^"]*">\s*<svg[^>]*>(?s:.*?)</svg>\s*Book walk-in</a>',
        )
        pk = self.pet.pk
        for url, label in (
            (reverse("admin_medical_record_create", args=[pk]), "Add medical record"),
            (
                reverse("admin_vaccination_standalone_create", args=[pk]),
                "Add vaccination",
            ),
            (reverse("admin_pet_edit", args=[pk]), "Edit details"),
        ):
            with self.subTest(label=label):
                self.assertRegex(
                    page,
                    rf'<a href="{url}" class="btn btn-outline min-h-12 [^"]*">'
                    rf"\s*<svg[^>]*>(?s:.*?)</svg>\s*{label}</a>",
                )
        self.assertRegex(
            page,
            r'(?s)<hr [^>]*>\s*<button type="button" class="btn [^"]*text-red-700'
            r'[^"]*" data-dialog-open="archive-pet-dialog"[^>]*>.*?'
            r"Archive Bantay\s*</button>",
        )
        self.assertNotIn("Restore Bantay", page)

    def test_actions_when_the_pet_is_archived(self):
        self._archive_pet()
        page = self._page()
        self.assertEqual(page.count("btn-primary"), 1)
        self.assertRegex(
            page,
            r'(?s)<button type="button" class="btn btn-primary [^"]*" '
            r'data-dialog-open="restore-pet-dialog"[^>]*>.*?Restore Bantay\s*</button>',
        )
        self.assertIn(
            "<span>Restore Bantay to book visits or add records.</span>", page
        )
        for gone in (
            "Book walk-in",
            "Add medical record",
            "Edit details",
            "Archive Bantay",
        ):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, page)
        self.assertNotIn(reverse("admin_pet_edit", args=[self.pet.pk]), page)

    def test_actions_when_the_owner_is_archived(self):
        owner_url = reverse("admin_owner_detail", args=[self.owner.pk])
        _archive(self.owner)
        # Both archived: Restore would be refused, so the owner hint wins
        for pet_archived in (False, True):
            if pet_archived:
                self._archive_pet()
            with self.subTest(pet_archived=pet_archived):
                page = self._page()
                self.assertEqual(page.count("btn-primary"), 0)
                self.assertIn(
                    "<span>Bantay's owner is archived. Restore Ana Cruz on their "
                    "page to book visits or add records.</span>",
                    page,
                )
                self.assertRegex(
                    page,
                    rf'<a href="{owner_url}" class="btn btn-outline min-h-12 [^"]*">'
                    r"\s*<svg[^>]*>(?s:.*?)</svg>\s*Open Ana Cruz's page</a>",
                )
                for gone in (
                    "Book walk-in",
                    "Restore Bantay",
                    "Archive Bantay",
                    "Edit details",
                ):
                    self.assertNotIn(gone, page)

    def test_archive_button_hidden_while_a_request_waits(self):
        _request(self.pet)
        page = self._page()
        self.assertNotIn('data-dialog-open="archive-pet-dialog"', page)
        self.assertNotIn("<hr ", page)
        self.assertEqual(page.count("btn-primary"), 1)
        self.assertIn("Book walk-in</a>", page)

    # ─── Dialogs ──────────────────────────────────────────────────────────────

    def test_dialogs_only_with_their_buttons_and_post_to_the_right_url(self):
        waiting = _request(_pet(self.owner, "Choco"))
        archived = self._archive_pet(_pet(self.owner, "Old"))
        lito = _owner("Lito", "Reyes")
        spot = _pet(lito, "Spot")
        lito_waiting = _request(_pet(lito, "Muning"))
        _archive(lito)
        reject, approve = "admin_reject_deletion", "admin_approve_deletion"
        cases = [
            (self.pet, {"archive-pet-dialog": ("admin_pet_archive", self.pet.pk)}),
            (
                waiting.pet,
                {
                    f"keep-dialog-{waiting.pk}": (reject, waiting.pk),
                    f"archive-dialog-{waiting.pk}": (approve, waiting.pk),
                },
            ),
            (archived, {"restore-pet-dialog": ("admin_pet_restore", archived.pk)}),
            (spot, {}),
            (
                lito_waiting.pet,
                {f"keep-dialog-{lito_waiting.pk}": (reject, lito_waiting.pk)},
            ),
        ]
        for pet, expected in cases:
            with self.subTest(pet=pet.name):
                dialogs = self._dialogs(pet)
                self.assertEqual(set(dialogs), set(expected))
                page = self._page(pet)
                for dialog_id, (url_name, pk) in expected.items():
                    self.assertIn(f'data-dialog-open="{dialog_id}"', page)
                    markup = dialogs[dialog_id]
                    action = reverse(url_name, args=[pk])
                    self.assertIn(f'<form method="post" action="{action}"', markup)
                    self.assertIn('name="csrfmiddlewaretoken"', markup)
                    self.assertIn(
                        "data-dialog-close autofocus>Go back</button>", markup
                    )
                    if url_name in (reject, approve):
                        self.assertIn(
                            '<input type="hidden" name="return_to" value="pet">',
                            markup,
                        )

    def test_archive_dialog_text_and_fields(self):
        dialog = self._dialogs()["archive-pet-dialog"]
        self.assertNotIn("data-open-on-load", dialog)
        self.assertIn(">Archive Bantay?</h2>", dialog)
        self.assertIn(
            "Bantay will be hidden from Ana Cruz's account, the Pets list and "
            "walk-ins. Medical records and vaccinations are kept, and you can "
            "restore Bantay later.</p>",
            dialog,
        )
        self.assertRegex(
            dialog,
            r'(?s)<label for="id_reason" [^>]*>\s*Reason <span[^>]*>\*</span>',
        )
        self.assertRegex(
            dialog,
            r'(?s)<select name="reason" [^>]*id="id_reason">\s*'
            r'<option value="" selected>Choose one</option>\s*'
            r'<option value="passed_away">Passed Away</option>',
        )
        self.assertRegex(
            dialog,
            r'(?s)<label for="id_reason_detail" [^>]*>\s*Details <span '
            r'class="font-normal text-gray-600">\(optional\)</span>',
        )
        self.assertIn('<textarea name="reason_detail"', dialog)
        self.assertIn("Needed when the reason is Other.</p>", dialog)
        self.assertIn(
            "Ana Cruz gets a notification, and an email if they have one.", dialog
        )
        self.assertRegex(
            dialog,
            r'(?s)<button type="submit" class="btn btn-danger [^"]*">\s*<svg[^>]*>'
            r".*?</svg>\s*Yes, archive Bantay\s*</button>",
        )
        self.assertNotIn("Archiving doesn't cancel", dialog)

    def test_restore_dialog_text(self):
        dialog = self._dialogs(self._archive_pet())["restore-pet-dialog"]
        self.assertIn(">Restore Bantay?</h2>", dialog)
        self.assertIn(
            "Bantay comes back on Ana Cruz's account and on the Pets list. You can "
            "book visits and add records again. Any old deletion request stays in "
            "the history.</p>",
            dialog,
        )
        self.assertRegex(
            dialog,
            r'(?s)<button type="submit" class="btn btn-primary [^"]*">\s*<svg[^>]*>'
            r".*?</svg>\s*Yes, restore Bantay\s*</button>",
        )

    def test_waiting_request_archive_dialog_text_on_the_pet_page(self):
        waiting = _request(self.pet)
        dialog = self._dialogs()[f"archive-dialog-{waiting.pk}"]
        self.assertIn(
            "Ana Cruz asked to remove Bantay (reason: passed away). Bantay will be "
            "hidden from their account and from walk-ins. Medical records and "
            "vaccinations are kept, and you can restore Bantay later.</p>",
            dialog,
        )

    def test_visit_warning_for_0_1_2_and_3_upcoming_visits(self):
        self._visit(-1)  # past: not counted
        self._visit(3, Appointment.CANCELLED)  # cancelled: not counted
        dialog = self._dialogs()["archive-pet-dialog"]
        self.assertNotIn("Archiving doesn't cancel", dialog)
        self._visit(1)
        first = "<b>Bantay has a visit tomorrow at 9:00 AM.</b>"
        them = (
            "Archiving doesn't cancel them. Cancel them on the appointment page "
            "if they won't happen."
        )
        cases = [
            (
                1,
                f"{first} Archiving doesn't cancel it. Cancel it on the "
                "appointment page if it won't happen.",
            ),
            (2, f"{first} There is 1 more. {them}"),
            (3, f"{first} There are 2 more. {them}"),
        ]
        for count, text in cases:
            if count > 1:
                self._visit(count + 1, Appointment.PENDING)
            with self.subTest(count=count):
                self.assertIn(text, self._dialogs()["archive-pet-dialog"])
        # The waiting request's Archive dialog warns too
        waiting = _request(self.pet)
        self.assertIn(cases[2][1], self._dialogs()[f"archive-dialog-{waiting.pk}"])

    # ─── Owner card ───────────────────────────────────────────────────────────

    def test_owner_card(self):
        card = self._card(self._html(), "owner-title")
        owner_url = reverse("admin_owner_detail", args=[self.owner.pk])
        self.assertIn(
            f'<a href="{owner_url}" class="font-medium text-blue-600 underline '
            'underline-offset-2">Ana Cruz</a>',
            card,
        )
        self.assertIn(">AC</span>", card)
        self.assertIn("bg-violet-50", card)
        self.assertIn('<p class="text-sm text-gray-700">0917 123 4567</p>', card)
        self.assertIn('<span class="break-all">ana@example.com</span>', card)

    def test_owner_card_empty_and_archived(self):
        lito = _archive(_owner("Lito", "Reyes"))
        card = self._card(self._html(_pet(lito, "Spot")), "owner-title")
        self.assertIn('<span class="text-gray-600">No phone number</span>', card)
        self.assertIn('<span class="text-gray-600">No email</span>', card)
        self.assertIn("bg-gray-100", card)
        self.assertNotIn("bg-violet-50", card)

    # ─── Whole page ───────────────────────────────────────────────────────────

    def test_every_link_is_on_one_line(self):
        self._visit(1)
        self._record(-1)
        self._shot(-1, 10)
        waiting = _request(_pet(self.owner, "Choco"))
        archived = self._archive_pet(_pet(self.owner, "Old"))
        lito = _owner("Lito", "Reyes")
        spot = _pet(lito, "Spot")
        _archive(lito)
        for pet in (self.pet, waiting.pet, archived, spot):
            with self.subTest(pet=pet.name):
                for tag in re.findall(r"<a\b[^>]*>", self._html(pet)):
                    self.assertNotIn("\n", tag)

    def test_query_count_is_flat(self):
        """The page costs the same with 1 visit, record and vaccination as
        with 5 of each."""
        service = Service.objects.create(service_name="Check-up", status=Service.ACTIVE)

        def add(start, count):
            for i in range(start, start + count):
                self._visit(-i - 1, Appointment.COMPLETED, service=service)
                self._record(-i - 1, diagnosis=f"Visit {i}")
                self._shot(-i - 1, 10 + i)

        add(0, 1)
        self._get()  # warm-up: one-time lookups don't count
        with CaptureQueriesContext(connection) as small:
            self._get()
        add(1, 4)
        with self.assertNumQueries(len(small)):
            self._get()


# ─── Slice 4b-4: pet form (species / breed dropdowns) ────────────────────────

SHARED_SPECIES_JS = (
    Path(settings.BASE_DIR) / "static" / "js" / "shared" / "pet_species_breed.js"
)


def _js_species_lists():
    """PET_SPECIES_OPTIONS and PET_BREEDS_BY_SPECIES read from the shared JS."""
    text = SHARED_SPECIES_JS.read_text(encoding="utf-8")
    species_block = re.search(r"PET_SPECIES_OPTIONS = \[(.*?)\];", text, re.S)
    species = re.findall(r'"([^"]*)"', species_block.group(1))
    breeds_block = re.search(r"PET_BREEDS_BY_SPECIES = \{(.*?)\n\};", text, re.S)
    breeds = {
        name: re.findall(r'"([^"]*)"', items)
        for name, items in re.findall(r"(\w+): \[(.*?)\]", breeds_block.group(1), re.S)
    }
    return species, breeds


def _select(html, name):
    """The <select name=…> tag and its (value, label) options."""
    match = re.search(rf'(?s)(<select name="{name}"[^>]*>)(.*?)</select>', html)
    return match.group(1), _options(match.group(2))


def _selected(html, name):
    """The value of the selected option of <select name=…>, or None."""
    match = re.search(rf'(?s)<select name="{name}"[^>]*>(.*?)</select>', html)
    picked = re.search(r'<option value="([^"]*)"[^>]*selected', match.group(1))
    return picked.group(1) if picked else None


def _wrapper_hidden(html, attr):
    """True when the <div … data-attr> wrapper has the hidden class."""
    tag = re.search(rf'<div class="([^"]*)" {attr}\b', html)
    return "hidden" in tag.group(1).split()


class SpeciesBreedListTests(SimpleTestCase):
    """pets/utils.py: the species / breed lists and their helpers."""

    def test_lists_match_the_shared_js(self):
        species, breeds = _js_species_lists()
        self.assertEqual(species_options(), species)
        self.assertEqual(list(PET_BREEDS_BY_SPECIES), list(breeds))
        self.assertEqual(PET_BREEDS_BY_SPECIES, breeds)
        self.assertNotIn("Other", species_options())

    def test_species_match_ignores_case_and_spaces(self):
        self.assertEqual(match_species("dog"), "Dog")
        self.assertEqual(match_species("  HAMSTER "), "Hamster")
        for value in ("Turtle", "", None, "Dogs"):
            with self.subTest(value=value):
                self.assertIsNone(match_species(value))

    def test_breeds_for_and_match_breed(self):
        self.assertEqual(breeds_for("dog"), PET_BREEDS_BY_SPECIES["Dog"])
        self.assertEqual(breeds_for("Turtle"), [])
        self.assertEqual(
            match_breed("DOG", "  golden   RETRIEVER "), "Golden Retriever"
        )
        self.assertEqual(match_breed("cat", "puspin (native)"), "Puspin (Native)")
        self.assertIsNone(match_breed("Cat", "Golden Retriever"))
        self.assertIsNone(match_breed("Turtle", "Golden Retriever"))

    def test_breed_choices(self):
        for species in ("", "Turtle", None):
            with self.subTest(species=species):
                self.assertEqual(
                    breed_choices(species), [("", "Choose the species first")]
                )
        choices = breed_choices("rabbit")
        self.assertEqual(choices[0], ("", "Not sure"))
        self.assertEqual(choices[-1], ("__other__", "Other (type it)"))
        self.assertEqual(
            [value for value, _ in choices[1:-1]], PET_BREEDS_BY_SPECIES["Rabbit"]
        )


class BreedOptionsEndpointTests(TestCase):
    """admin_pet_breed_options: the Breed <option> list over HTMX."""

    def setUp(self):
        self.url = reverse("admin_pet_breed_options")

    def test_url(self):
        self.assertEqual(self.url, "/admin/pets/breed-options/")

    def test_admin_only(self):
        response = self.client.get(self.url, {"species": "Dog"})
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("<option", response.content.decode())
        owner = _owner(email="ana@example.com", password="pw-12345!")
        self.client.force_login(owner.user)
        response = self.client.get(self.url, {"species": "Dog"})
        self.assertRedirects(
            response, reverse("owner_dashboard"), fetch_redirect_response=False
        )

    def test_known_species(self):
        _admin(self)
        for params in ({"species": "dog"}, {"species_choice": "Dog"}):
            with self.subTest(params=params):
                response = self.client.get(
                    self.url, params, headers={"HX-Request": "true"}
                )
                options = _options(response.content.decode())
                self.assertEqual(options[0], ("", "Not sure"))
                self.assertEqual(options[-1], ("__other__", "Other (type it)"))
                self.assertEqual(
                    [v for v, _ in options[1:-1]], PET_BREEDS_BY_SPECIES["Dog"]
                )
                self.assertEqual(
                    [t for _, t in options[1:-1]], PET_BREEDS_BY_SPECIES["Dog"]
                )

    def test_unknown_or_empty_species(self):
        _admin(self)
        for params in ({"species": ""}, {"species": "Turtle"}, {}):
            with self.subTest(params=params):
                response = self.client.get(self.url, params)
                self.assertEqual(
                    _options(response.content.decode()),
                    [("", "Choose the species first")],
                )


class PetFormTests(TestCase):
    """AdminPetForm: species / breed picks, labels and errors."""

    def _form(self, **data):
        return AdminPetForm(data={"name": "bantay", "gender": "unknown", **data})

    def _cleaned(self, **data):
        form = self._form(**data)
        self.assertTrue(form.is_valid(), form.errors)
        return form.cleaned_data

    def test_list_species_and_breed_save_the_official_spellings(self):
        cleaned = self._cleaned(
            species_choice="Dog", breed_choice="golden retriever", species="Cat"
        )
        self.assertEqual(cleaned["species"], "Dog")
        self.assertEqual(cleaned["breed"], "Golden Retriever")
        self.assertEqual(cleaned["name"], "Bantay")

    def test_saved_through_the_add_page(self):
        _admin(self)
        owner = _owner()
        self.client.post(
            reverse("admin_pet_add", args=[owner.pk]),
            {
                "name": "Brownie",
                "gender": "male",
                "species_choice": "Cat",
                "breed_choice": "Puspin (Native)",
            },
        )
        pet = Pet.objects.get(owner=owner)
        self.assertEqual((pet.species, pet.breed), ("Cat", "Puspin (Native)"))
        self.assertEqual(pet.gender, Pet.MALE)

    def test_breed_from_another_species(self):
        form = self._form(species_choice="Cat", breed_choice="Golden Retriever")
        self.assertFalse(form.is_valid())
        self.assertEqual(
            form.errors["breed_choice"],
            ["Choose a breed for Cat, or pick Other."],
        )

    def test_other_species_needs_the_typed_text(self):
        form = self._form(species_choice="__other__", species="  ")
        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors["species"], ["Type the species."])

    def test_other_species_with_a_typed_breed(self):
        cleaned = self._cleaned(
            species_choice="__other__",
            species="turtle",
            breed_choice="Beagle",
            breed="Red-eared slider",
        )
        self.assertEqual(cleaned["species"], "Turtle")
        self.assertEqual(cleaned["breed"], "Red-eared slider")

    def test_breed_not_sure_saves_empty(self):
        cleaned = self._cleaned(species_choice="Dog", breed_choice="", breed="Askal")
        self.assertEqual(cleaned["breed"], "")

    def test_breed_other_uses_the_typed_breed(self):
        cleaned = self._cleaned(
            species_choice="Dog", breed_choice="__other__", breed=" Askal mix "
        )
        self.assertEqual(cleaned["breed"], "Askal mix")
        cleaned = self._cleaned(species_choice="Dog", breed_choice="__other__")
        self.assertEqual(cleaned["breed"], "")

    def test_species_is_required(self):
        cases = (
            ("", "Choose the species."),
            ("Turtle", "Choose a species from the list."),
        )
        for value, message in cases:
            with self.subTest(value=value):
                form = self._form(species_choice=value)
                self.assertFalse(form.is_valid())
                self.assertEqual(form.errors["species_choice"], [message])

    def test_species_options(self):
        choices = AdminPetForm().fields["species_choice"].choices
        self.assertEqual(choices[0], ("", "Choose one"))
        self.assertEqual([v for v, _ in choices[1:-1]], species_options())
        self.assertEqual(choices[-1], ("__other__", "Other (type it)"))

    def test_sex_defaults_to_not_sure(self):
        form = AdminPetForm()
        self.assertEqual(form.fields["gender"].label, "Sex")
        self.assertEqual(
            form.fields["gender"].choices,
            [("unknown", "Not sure"), ("male", "Male"), ("female", "Female")],
        )
        self.assertEqual(form["gender"].value(), "unknown")
        cleaned = self._cleaned(species_choice="Dog", gender="")
        self.assertEqual(cleaned["gender"], "unknown")

    def test_labels_help_and_placeholders(self):
        form = AdminPetForm()
        labels = {
            "name": "Name",
            "species_choice": "Species",
            "species": "Type the species",
            "breed_choice": "Breed",
            "breed": "Type the breed",
            "date_of_birth": "Birthday",
            "weight": "Weight (kg)",
            "color": "Color",
            "registration_number": "Registration no.",
        }
        for name, label in labels.items():
            with self.subTest(name=name):
                self.assertEqual(form.fields[name].label, label)
        self.assertEqual(
            form.fields["date_of_birth"].help_text, "A rough date is fine."
        )
        self.assertEqual(
            form.fields["registration_number"].widget.attrs["placeholder"],
            "From the pet's ID card, if any",
        )
        self.assertTrue(form.fields["species_choice"].required)
        self.assertFalse(form.fields["breed_choice"].required)

    def test_name_weight_and_birthday_errors(self):
        tomorrow = timezone.localdate() + timedelta(days=1)
        form = AdminPetForm(
            data={
                "name": "",
                "species_choice": "Dog",
                "weight": "0",
                "date_of_birth": tomorrow.isoformat(),
            }
        )
        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors["name"], ["Enter the pet's name."])
        self.assertEqual(form.errors["weight"], ["Weight must be more than 0."])
        self.assertEqual(
            form.errors["date_of_birth"], ["The birthday can't be in the future."]
        )
        self.assertEqual(form.error_count, 3)


class PetFormInitialTests(TestCase):
    """AdminPetForm on Edit: the saved text picks the dropdowns."""

    def setUp(self):
        self.owner = _owner()

    def test_matching_text_in_another_case_picks_the_choices(self):
        pet = _pet(self.owner, species="dog", breed="GOLDEN retriever")
        form = AdminPetForm(instance=pet)
        self.assertEqual(form["species_choice"].value(), "Dog")
        self.assertEqual(form["breed_choice"].value(), "Golden Retriever")
        self.assertEqual(form.species_mode, "list")
        self.assertFalse(form.show_breed_text)
        self.assertNotIn("disabled", form.fields["breed_choice"].widget.attrs)
        self.assertEqual(
            [v for v, _ in form.fields["breed_choice"].choices[1:-1]],
            PET_BREEDS_BY_SPECIES["Dog"],
        )

    def test_empty_breed_is_not_sure(self):
        form = AdminPetForm(instance=_pet(self.owner, species="Cat", breed=""))
        self.assertEqual(form["breed_choice"].value(), "")
        self.assertFalse(form.show_breed_text)

    def test_species_not_in_the_list_is_other_with_the_text_kept(self):
        pet = _pet(self.owner, name="Shelly", species="Turtle", breed="Box turtle")
        form = AdminPetForm(instance=pet)
        self.assertEqual(form["species_choice"].value(), "__other__")
        self.assertEqual(form["species"].value(), "Turtle")
        self.assertEqual(form["breed"].value(), "Box turtle")
        self.assertEqual(form.species_mode, "other")
        self.assertTrue(form.show_breed_text)
        self.assertEqual(form.fields["breed"].label, "Breed")
        self.assertTrue(form.fields["breed_choice"].widget.attrs["disabled"])

    def test_breed_not_in_the_list_is_other_with_the_text_kept(self):
        # "Golden Retriever" is a Dog breed, so it isn't on the Cat list
        pet = _pet(self.owner, name="Max", species="Cat", breed="Golden Retriever")
        form = AdminPetForm(instance=pet)
        self.assertEqual(form["species_choice"].value(), "Cat")
        self.assertEqual(form["breed_choice"].value(), "__other__")
        self.assertEqual(form["breed"].value(), "Golden Retriever")
        self.assertTrue(form.show_breed_text)
        self.assertEqual(form.fields["breed"].label, "Type the breed")

    def test_saving_the_page_as_it_opened_keeps_the_saved_text(self):
        _admin(self)
        cases = [
            ("Turtle", "Box turtle", "Turtle", "Box turtle"),
            ("Cat", "Golden Retriever", "Cat", "Golden Retriever"),
            ("dog", "aspin (native)", "Dog", "Aspin (Native)"),
        ]
        fields = (
            "name",
            "gender",
            "species_choice",
            "species",
            "breed_choice",
            "breed",
        )
        for species, breed, saved_species, saved_breed in cases:
            with self.subTest(species=species, breed=breed):
                pet = _pet(self.owner, species=species, breed=breed)
                url = reverse("admin_pet_edit", args=[pet.pk])
                form = self.client.get(url).context["form"]
                data = {name: form[name].value() or "" for name in fields}
                if form.species_mode == "other":
                    data.pop("breed_choice")  # a disabled select isn't sent
                self.client.post(url, data)
                pet.refresh_from_db()
                self.assertEqual((pet.species, pet.breed), (saved_species, saved_breed))


class PetFormPageTests(TestCase):
    """admin/pets/pet_form.html: Add pet and Edit pet."""

    def setUp(self):
        _admin(self)
        self.owner = _owner(first="Liza", email="liza@example.com")
        self.pet = _pet(self.owner, name="Brownie", breed="Aspin (Native)")
        self.add = reverse("admin_pet_add", args=[self.owner.pk])
        self.edit = reverse("admin_pet_edit", args=[self.pet.pk])
        self.owner_url = reverse("admin_owner_detail", args=[self.owner.pk])
        self.pet_url = reverse("admin_pet_detail", args=[self.pet.pk])
        self.walkin_url = (
            f"{reverse('admin_walkin_appointment_visit')}?owner={self.owner.pk}"
        )

    def _html(self, url, data=None):
        if data is None:
            return self.client.get(url).content.decode()
        return self.client.post(url, data).content.decode()

    def test_new_layout(self):
        inline = r"<script(?![^>]*\bsrc=)[^>]*>"
        owner_form = self._html(reverse("admin_owner_edit", args=[self.owner.pk]))
        for url in (self.add, self.edit):
            with self.subTest(url=url):
                response = self.client.get(url)
                html = response.content.decode()
                self.assertTemplateUsed(response, "layouts/admin.html")
                self.assertNotIn("pet_form.css", html)
                self.assertNotIn("admin-page", html)
                self.assertNotIn("pet_species_breed.js", html)
                # Only the layout's own inline script, as on the owner form
                self.assertEqual(
                    len(re.findall(inline, html)), len(re.findall(inline, owner_form))
                )
                self.assertIn('<div class="mx-auto max-w-3xl">', html)
                # Slice 8b-3: both scripts moved to js/shared/
                self.assertIn("js/shared/photo_input", html)
                self.assertIn("js/shared/pet_form", html)
                self.assertRegex(html, r"<form [^>]*novalidate")
                self.assertRegex(html, r'<form [^>]*enctype="multipart/form-data"')
                self.assertIn("csrfmiddlewaretoken", html)

    def test_owner_form_loads_the_shared_photo_script(self):
        html = self._html(reverse("admin_owner_edit", args=[self.owner.pk]))
        # Slice 8b-3: both scripts moved to js/shared/
        self.assertIn("js/shared/photo_input", html)
        self.assertIn("js/shared/address_form", html)

    def test_add_header(self):
        html = self._html(self.add)
        self.assertRegex(
            html,
            rf'(?s)<a href="{self.owner_url}" [^>]*>\s*<svg.*?</svg>'
            r"\s*Back to Liza Cruz</a>",
        )
        self.assertRegex(html, r"<h1[^>]*>Add pet</h1>")
        self.assertIn("Add a pet to Liza Cruz's account.", html)

    def test_add_from_the_walkin_header(self):
        html = self._html(f"{self.add}?return=walkin")
        self.assertRegex(
            html,
            rf'(?s)<a href="{re.escape(self.walkin_url)}" [^>]*>\s*<svg.*?</svg>'
            r"\s*Back to the walk-in</a>",
        )
        self.assertRegex(html, r"<h1[^>]*>Add pet</h1>")
        self.assertIn("Add a pet to Liza Cruz's account.", html)

    def test_edit_header(self):
        html = self._html(self.edit)
        self.assertRegex(
            html,
            rf'(?s)<a href="{self.pet_url}" [^>]*>\s*<svg.*?</svg>'
            r"\s*Back to Brownie</a>",
        )
        self.assertRegex(html, r"<h1[^>]*>Edit Brownie</h1>")
        self.assertIn(
            '<p class="mt-1 text-base text-gray-600">Owner: Liza Cruz</p>', html
        )

    def test_failed_edit_keeps_the_saved_name_in_the_header(self):
        html = self._html(self.edit, {"name": "Rex", "species_choice": ""})
        self.assertRegex(html, r"<h1[^>]*>Edit Brownie</h1>")
        self.assertIn("Back to Brownie", html)
        self.assertIn('value="Rex"', html)

    def test_required_note_or_error_summary(self):
        html = self._html(self.add)
        self.assertIn(
            'Fields marked <span class="text-red-700">*</span> are required. '
            "Everything else can be added later.",
            html,
        )
        self.assertNotIn('role="alert"', html)
        html = self._html(self.add, {"name": "", "species_choice": "Dog"})
        self.assertIn('role="alert"', html)
        self.assertIn("Please fix the item below.", html)
        self.assertIn("Enter the pet&#x27;s name.", html)
        self.assertNotIn("Everything else can be added later.", html)
        html = self._html(self.add, {"name": "", "species_choice": ""})
        self.assertIn("Please fix the 2 items below.", html)

    def test_owner_line_only_on_add(self):
        add = self._html(self.add)
        self.assertRegex(
            add,
            r'(?s)data-owner-line>.*?<p class="text-sm text-gray-600">Owner</p>\s*'
            r'<p class="text-base font-semibold text-gray-900">Liza Cruz</p>',
        )
        self.assertNotIn("data-owner-line", self._html(self.edit))

    def test_one_primary_button(self):
        for url, label in ((self.add, "Add pet"), (self.edit, "Save changes")):
            with self.subTest(label=label):
                html = self._html(url)
                self.assertEqual(html.count("btn-primary"), 1)
                self.assertRegex(
                    html,
                    r'(?s)<button type="submit" class="btn btn-primary[^"]*">'
                    rf"\s*<svg.*?</svg>\s*{label}</button>",
                )

    def test_cancel_targets_and_add_notes(self):
        cases = (
            (
                self.add,
                self.owner_url,
                "After saving you'll see Liza Cruz's page with the new pet.",
            ),
            (
                f"{self.add}?return=walkin",
                self.walkin_url,
                "After saving you'll go back to the walk-in "
                "with the new pet picked.",
            ),
            (self.edit, self.pet_url, None),
        )
        for url, cancel, note in cases:
            with self.subTest(url=url):
                html = self._html(url)
                self.assertIn(
                    f'<a href="{cancel}" '
                    'class="btn btn-outline w-full font-semibold sm:w-auto">Cancel</a>',
                    html,
                )
                if note:
                    self.assertIn(note, html)
                else:
                    self.assertNotIn("After saving", html)

    def test_hidden_return_kept_after_a_failed_post(self):
        hidden = '<input type="hidden" name="return" value="walkin">'
        self.assertIn(hidden, self._html(f"{self.add}?return=walkin"))
        self.assertNotIn('name="return"', self._html(self.add))
        # A failed POST to the plain URL keeps the posted field
        html = self._html(self.add, {"return": "walkin", "species_choice": "Dog"})
        self.assertIn(hidden, html)
        self.assertIn("Back to the walk-in", html)
        # Anything else is dropped
        html = self._html(self.add, {"return": "https://example.com/"})
        self.assertNotIn('name="return"', html)

    def test_species_and_breed_selects(self):
        html = self._html(self.add)
        tag, options = _select(html, "species_choice")
        self.assertIn(f'hx-get="{reverse("admin_pet_breed_options")}"', tag)
        self.assertIn('hx-trigger="change"', tag)
        self.assertIn('hx-target="#id_breed_choice"', tag)
        self.assertEqual(options[0], ("", "Choose one"))
        self.assertEqual(options[-1], ("__other__", "Other (type it)"))
        tag, options = _select(html, "breed_choice")
        self.assertIn("disabled", tag)
        self.assertEqual(options, [("", "Choose the species first")])
        self.assertTrue(_wrapper_hidden(html, "data-species-other"))
        self.assertFalse(_wrapper_hidden(html, "data-breed-pick"))
        self.assertTrue(_wrapper_hidden(html, "data-breed-other"))
        _, sex = _select(html, "gender")
        self.assertEqual(
            sex, [("unknown", "Not sure"), ("male", "Male"), ("female", "Female")]
        )
        self.assertEqual(_selected(html, "gender"), "unknown")

    def test_edit_picks_the_saved_species_and_breed(self):
        html = self._html(self.edit)
        self.assertEqual(_selected(html, "species_choice"), "Dog")
        self.assertEqual(_selected(html, "breed_choice"), "Aspin (Native)")
        tag, _ = _select(html, "breed_choice")
        self.assertNotIn("disabled", tag)
        self.assertTrue(_wrapper_hidden(html, "data-species-other"))
        self.assertFalse(_wrapper_hidden(html, "data-breed-pick"))
        self.assertTrue(_wrapper_hidden(html, "data-breed-other"))

    def test_edit_with_species_other(self):
        turtle = _pet(self.owner, name="Shelly", species="Turtle")
        html = self._html(reverse("admin_pet_edit", args=[turtle.pk]))
        self.assertEqual(_selected(html, "species_choice"), "__other__")
        self.assertIn('value="Turtle"', html)
        self.assertFalse(_wrapper_hidden(html, "data-species-other"))
        self.assertTrue(_wrapper_hidden(html, "data-breed-pick"))
        self.assertFalse(_wrapper_hidden(html, "data-breed-other"))
        self.assertRegex(
            html,
            r'(?s)for="id_species"[^>]*>\s*Type the species '
            r'<span class="text-red-700" aria-hidden="true">\*</span>',
        )
        self.assertRegex(html, r'(?s)for="id_breed"[^>]*>\s*Breed <span')

    def test_failed_post_keeps_the_breed_list_of_the_posted_species(self):
        html = self._html(
            self.add, {"name": "", "species_choice": "Cat", "breed_choice": "Persian"}
        )
        _, options = _select(html, "breed_choice")
        self.assertEqual([v for v, _ in options[1:-1]], PET_BREEDS_BY_SPECIES["Cat"])
        self.assertEqual(_selected(html, "breed_choice"), "Persian")

    def test_photo_card_on_add(self):
        html = self._html(self.add)
        self.assertIn("Choose a photo", html)
        self.assertNotIn("Choose a new photo", html)
        self.assertIn("Without a photo, a picture of the species is shown.", html)
        self.assertNotIn("Remove the photo", html)
        self.assertNotIn("Choose the photo again.", html)
        self.assertIn("data-photo-input", html)
        self.assertIn("data-photo-name", html)

    def test_photo_card_on_edit_without_a_photo(self):
        html = self._html(self.edit)
        self.assertIn('aria-label="No photo of Brownie yet"', html)
        self.assertIn("Choose a photo", html)
        self.assertIn("Without a photo, a picture of the species is shown.", html)
        self.assertNotIn("Remove the photo", html)

    def test_photo_card_on_edit_with_a_photo(self):
        Pet.objects.filter(pk=self.pet.pk).update(photo="pets/brownie.jpg")
        html = self._html(self.edit)
        self.assertIn('alt="Photo of Brownie"', html)
        self.assertIn("Choose a new photo", html)
        self.assertIn("Leave it as it is to keep the current photo.", html)
        self.assertRegex(
            html,
            r'(?s)<label for="photo-clear_id"[^>]*>\s*<input type="checkbox" '
            r'name="photo-clear" id="photo-clear_id"[^>]*>\s*Remove the photo',
        )

    def test_photo_repick_line_after_errors(self):
        html = self._html(self.add, {"name": "", "species_choice": "Dog"})
        self.assertIn(
            "Choose the photo again. Browsers can't keep it after a problem.",
            html,
        )

    def test_age_line_on_edit(self):
        born = timezone.localdate() - timedelta(days=3 * 365 + 10)
        Pet.objects.filter(pk=self.pet.pk).update(date_of_birth=born)
        html = self._html(self.edit)
        self.assertIn(
            '<p class="text-sm text-gray-600" data-age-line>'
            '<b class="font-semibold text-gray-800">3 yrs old</b> · '
            "A rough date is fine.</p>",
            html,
        )
        no_birthday = reverse("admin_pet_edit", args=[_pet(self.owner).pk])
        for url in (self.add, no_birthday):
            with self.subTest(url=url):
                self.assertIn(
                    '<p class="text-sm text-gray-600" data-age-line>'
                    "A rough date is fine.</p>",
                    self._html(url),
                )

    def test_every_link_on_one_line(self):
        pages = [
            self._html(self.add),
            self._html(f"{self.add}?return=walkin"),
            self._html(self.edit),
            self._html(self.add, {"name": "", "species_choice": "Dog"}),
        ]
        for html in pages:
            for tag in re.findall(r"<a\b[^>]*>", html):
                self.assertNotIn("\n", tag)


class DeletionRequestCancelledChoiceTests(SimpleTestCase):
    """Slice 8a-0: CANCELLED, "Taken back by the owner", is a status choice."""

    def test_constant_value(self):
        self.assertEqual(PetDeletionRequest.CANCELLED, "cancelled")

    def test_label(self):
        deletion_request = PetDeletionRequest(status=PetDeletionRequest.CANCELLED)
        self.assertEqual(
            deletion_request.get_status_display(), "Taken back by the owner"
        )

    def test_it_is_the_last_choice(self):
        self.assertEqual(
            PetDeletionRequest.STATUS_CHOICES[-1],
            (PetDeletionRequest.CANCELLED, "Taken back by the owner"),
        )


def _login_owner(test, first="Ana", last="Cruz", email="ana@example.com"):
    """An active owner with a PetOwner row, logged in."""
    owner = _owner(first=first, last=last, email=email)
    test.client.force_login(owner.user)
    return owner


def _links_post(*links):
    """The contact-links formset POST data for (platform, url) pairs."""
    data = {
        "links-TOTAL_FORMS": str(len(links) + 1),
        "links-INITIAL_FORMS": "0",
        "links-MIN_NUM_FORMS": "0",
        "links-MAX_NUM_FORMS": "10",
    }
    rows = list(links) + [("", "")]
    for i, (platform, url) in enumerate(rows):
        data[f"links-{i}-platform"] = platform
        data[f"links-{i}-url_or_handle"] = url
    return data


class OnboardingStepOneReentryTests(TestCase):
    """Slice 8a-1a: a second visit to step 1 works on the saved PetOwner."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="newbie",
            email="newbie@example.com",
            password="pass-12345-x",
            role=User.PET_OWNER,
        )
        self.client.force_login(self.user)
        self.url = reverse("owner_onboarding_step1")

    def _post(self, first="Ana", links=()):
        # Slice 8b-3: the address is required on step 1, so it is posted too
        data = {
            "first_name": first,
            "last_name": "Cruz",
            **OWNER_ADDRESS,
            **_links_post(*links),
        }
        return self.client.post(self.url, data)

    def test_second_post_updates_the_same_owner(self):
        step2 = reverse("owner_onboarding_step2")
        self.assertRedirects(self._post("Ana"), step2, fetch_redirect_response=False)
        first_pk = PetOwner.objects.get().pk

        response = self._post("Anna")  # Back, then submit again
        self.assertRedirects(response, step2, fetch_redirect_response=False)
        owner = PetOwner.objects.get()
        self.assertEqual(owner.pk, first_pk)
        self.assertEqual(owner.first_name, "Anna")
        self.assertEqual(self.client.session["onboarding_owner_id"], first_pk)

    def test_get_shows_the_saved_values_and_links(self):
        self._post("Ana", links=[("Facebook", "fb.com/ana")])
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        form = response.context["profile_form"]
        self.assertEqual(form.instance.pk, PetOwner.objects.get().pk)
        self.assertEqual(form["first_name"].value(), "Ana")
        self.assertEqual(
            response.context["link_formset"].initial,
            [{"platform": "Facebook", "url_or_handle": "fb.com/ana"}],
        )

    def test_links_are_replaced_on_a_second_post(self):
        self._post("Ana", links=[("Facebook", "fb.com/ana")])
        self._post("Ana", links=[("Instagram", "@ana")])
        links = PetOwner.objects.get().contact_links.all()
        self.assertEqual([link.platform for link in links], ["Instagram"])

    def test_first_visit_still_works_without_a_row(self):
        self.assertEqual(self.client.get(self.url).status_code, 200)
        self.assertFalse(PetOwner.objects.exists())


class OwnerPetEditFormTests(TestCase):
    """Slice 8a-1a: the owner edits name, breed, color, weight and photo only."""

    def setUp(self):
        self.owner = _login_owner(self)
        self.pet = Pet.objects.create(
            owner=self.owner,
            name="Max",
            species="Dog",
            breed="Aspin",
            color="Brown",
            gender=Pet.MALE,
            date_of_birth=date(2020, 1, 2),
            weight=Decimal("5.00"),
            registration_number="R-1",
        )
        self.url = reverse("owner_pet_edit", args=[self.pet.pk])

    def test_allowed_fields_save(self):
        response = self.client.post(
            self.url,
            {"name": " Rex ", "breed": " Poodle ", "color": "White", "weight": "7.5"},
        )
        self.assertRedirects(
            response,
            reverse("owner_pet_detail", args=[self.pet.pk]),
            fetch_redirect_response=False,
        )
        self.pet.refresh_from_db()
        self.assertEqual(self.pet.name, "Rex")
        self.assertEqual(self.pet.breed, "Poodle")
        self.assertEqual(self.pet.color, "White")
        self.assertEqual(self.pet.weight, Decimal("7.50"))

    def test_locked_fields_are_ignored(self):
        self.client.post(
            self.url,
            {
                "name": "Rex",
                "species": "Cat",
                "gender": "female",
                "date_of_birth": "2019-05-05",
                "registration_number": "HACKED",
            },
        )
        self.pet.refresh_from_db()
        self.assertEqual(self.pet.name, "Rex")
        self.assertEqual(self.pet.species, "Dog")
        self.assertEqual(self.pet.gender, Pet.MALE)
        self.assertEqual(self.pet.date_of_birth, date(2020, 1, 2))
        self.assertEqual(self.pet.registration_number, "R-1")

    def test_form_has_only_the_allowed_fields(self):
        self.assertEqual(
            list(OwnerPetEditForm().fields),
            ["name", "breed", "color", "weight", "photo"],
        )

    def test_weight_zero_is_refused(self):
        response = self.client.post(self.url, {"name": "Max", "weight": "0"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("weight", response.context["form"].errors)
        self.pet.refresh_from_db()
        self.assertEqual(self.pet.weight, Decimal("5.00"))

    def test_name_is_required(self):
        response = self.client.post(self.url, {"name": "   "})
        self.assertEqual(response.status_code, 200)
        self.assertIn("name", response.context["form"].errors)

    def test_another_owners_pet_is_a_404(self):
        other = _owner(first="Ben", last="Reyes", email="ben@example.com")
        pet = Pet.objects.create(owner=other, name="Rex", species="Dog")
        response = self.client.post(
            reverse("owner_pet_edit", args=[pet.pk]), {"name": "Hacked"}
        )
        self.assertEqual(response.status_code, 404)
        pet.refresh_from_db()
        self.assertEqual(pet.name, "Rex")


class ContactLinksAtomicTests(TestCase):
    """Slice 8a-1a: replacing the contact links is all or nothing."""

    def setUp(self):
        self.owner = _login_owner(self)
        self.owner.user.is_onboarded = True
        self.owner.user.save()
        ContactLink.objects.create(
            owner=self.owner, platform="Facebook", url_or_handle="fb.com/ana"
        )
        ContactLink.objects.create(
            owner=self.owner, platform="Viber", url_or_handle="0917"
        )

    def _edit(self, first="Changed"):
        data = {"first_name": first, "last_name": "Cruz"}
        data.update(_links_post(("Instagram", "@ana")))
        return self.client.post(reverse("owner_profile_edit"), data)

    def test_failed_re_add_keeps_the_old_links_and_profile(self):
        with patch.object(ContactLink, "save", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self._edit()
        platforms = sorted(self.owner.contact_links.values_list("platform", flat=True))
        self.assertEqual(platforms, ["Facebook", "Viber"])
        self.owner.refresh_from_db()
        self.assertEqual(self.owner.first_name, "Ana")

    def test_successful_edit_replaces_the_links(self):
        response = self._edit()
        self.assertRedirects(
            response, reverse("owner_profile"), fetch_redirect_response=False
        )
        platforms = list(self.owner.contact_links.values_list("platform", flat=True))
        self.assertEqual(platforms, ["Instagram"])
        self.owner.refresh_from_db()
        self.assertEqual(self.owner.first_name, "Changed")


class TakeBackRemovalTests(TestCase):
    """Slice 8a-1b: the owner takes back a waiting removal request. It becomes
    CANCELLED (the row is kept); the clinic's history doesn't count it."""

    def setUp(self):
        self.owner = _login_owner(self)
        self.owner.user.is_onboarded = True
        self.owner.user.save()
        self.pet = _pet(self.owner, "Max")

    def _url(self, pet=None):
        return reverse("owner_pet_cancel_delete_request", args=[(pet or self.pet).pk])

    def _found(self, response):
        return [(m.level_tag, str(m)) for m in get_messages(response.wsgi_request)]

    def test_post_sets_cancelled_and_keeps_the_row(self):
        waiting = _request(self.pet)
        response = self.client.post(self._url())
        self.assertRedirects(
            response,
            reverse("owner_pet_detail", args=[self.pet.pk]),
            fetch_redirect_response=False,
        )
        waiting.refresh_from_db()
        self.assertEqual(waiting.status, PetDeletionRequest.CANCELLED)
        self.assertIsNone(waiting.reviewed_by)
        self.assertIsNone(waiting.reviewed_at)
        self.assertEqual(PetDeletionRequest.objects.count(), 1)
        self.assertEqual(
            self._found(response),
            [("success", "Removal request taken back. Max stays on your account.")],
        )

    def test_get_changes_nothing(self):
        waiting = _request(self.pet)
        response = self.client.get(self._url())
        self.assertRedirects(
            response,
            reverse("owner_pet_detail", args=[self.pet.pk]),
            fetch_redirect_response=False,
        )
        waiting.refresh_from_db()
        self.assertEqual(waiting.status, PetDeletionRequest.PENDING)
        self.assertEqual(self._found(response), [])

    def test_nothing_pending_gives_an_info_message(self):
        info = [("info", "There's no removal request to take back.")]
        response = self.client.post(self._url())
        self.assertRedirects(
            response,
            reverse("owner_pet_detail", args=[self.pet.pk]),
            fetch_redirect_response=False,
        )
        self.assertEqual(self._found(response), info)

        # An answered request is not waiting either, and stays as it is
        answered = _request(
            self.pet, PetDeletionRequest.REJECTED, reviewed=timezone.now()
        )
        response = self.client.post(self._url())
        # (the first message may still be waiting in the cookie)
        self.assertEqual(self._found(response)[-1:], info)
        answered.refresh_from_db()
        self.assertEqual(answered.status, PetDeletionRequest.REJECTED)

    def test_another_owners_pet_is_a_404(self):
        other = _owner(first="Bo", last="Li", email="bo@example.com")
        theirs = _request(_pet(other, "Rex"))
        response = self.client.post(self._url(theirs.pet))
        self.assertEqual(response.status_code, 404)
        theirs.refresh_from_db()
        self.assertEqual(theirs.status, PetDeletionRequest.PENDING)

    def test_a_new_request_works_afterwards(self):
        _request(self.pet)
        self.client.post(self._url())
        response = self.client.post(
            reverse("owner_pet_delete_request", args=[self.pet.pk]),
            {"reason": PetDeletionRequest.REHOMED},
        )
        self.assertRedirects(
            response,
            reverse("owner_pet_detail", args=[self.pet.pk]),
            fetch_redirect_response=False,
        )
        statuses = list(
            PetDeletionRequest.objects.order_by("pk").values_list("status", flat=True)
        )
        self.assertEqual(
            statuses, [PetDeletionRequest.CANCELLED, PetDeletionRequest.PENDING]
        )

    def test_the_clinic_pages_do_not_count_it_as_answered(self):
        taken_back = _request(self.pet)
        self.client.post(self._url())
        other_pet = _pet(self.owner, "Rex")
        waiting = _request(other_pet)
        archived = _request(
            _pet(self.owner, "Old"),
            PetDeletionRequest.APPROVED,
            reviewed=timezone.now(),
        )
        _admin(self)

        response = self.client.get(reverse("admin_deletion_requests"))
        self.assertEqual([r.pk for r in response.context["pending"]], [waiting.pk])
        self.assertEqual([r.pk for r in response.context["resolved"]], [archived.pk])
        self.assertEqual(response.context["pending_count"], 1)
        self.assertEqual(response.context["pending_requests_count"], 1)
        self.assertEqual(response.context["page_obj"].paginator.count, 1)
        self.assertContains(response, "Already answered <span")
        self.assertEqual(response.context["shell"].pending_deletions, 1)

        pet_page = self.client.get(reverse("admin_pet_detail", args=[self.pet.pk]))
        self.assertIsNone(pet_page.context["pending_deletion"])
        self.assertTrue(pet_page.context["show_archive_button"])
        taken_back.refresh_from_db()
        self.assertEqual(taken_back.status, PetDeletionRequest.CANCELLED)

    def test_the_clinic_cannot_answer_a_request_taken_back(self):
        taken_back = _request(self.pet)
        self.client.post(self._url())
        _admin(self)
        for name in ("admin_approve_deletion", "admin_reject_deletion"):
            with self.subTest(name=name):
                response = self.client.post(reverse(name, args=[taken_back.pk]))
                self.assertIn(
                    ("error", "The owner took this request back."),
                    self._found(response),
                )
        taken_back.refresh_from_db()
        self.assertEqual(taken_back.status, PetDeletionRequest.CANCELLED)
        self.assertFalse(Pet.objects.get(pk=self.pet.pk).is_archived)


class AddPetNextBookTests(TestCase):
    """Slice 8a-2a: owner_pet_add ?next=book goes on to the booking page with
    the new pet picked. "book" is a fixed choice, never a URL."""

    # Slice 8b-3: the species is picked (species_choice); it was typed (species)
    POST = {"name": "Bantay", "species_choice": "Dog", "gender": "unknown"}

    def setUp(self):
        self.owner = _login_owner(self)
        self.owner.user.is_onboarded = True
        self.owner.user.save()
        self.url = reverse("owner_pet_add")

    def _book_url(self, pet):
        return f"{reverse('owner_book_appointment')}?pet={pet.pk}"

    def test_next_book_goes_to_booking_with_the_new_pet(self):
        response = self.client.post(f"{self.url}?next=book", self.POST)
        pet = Pet.objects.get(owner=self.owner)
        self.assertRedirects(
            response, self._book_url(pet), fetch_redirect_response=False
        )
        self.assertEqual(_messages(response), ["Bantay has been added."])

    def test_the_hidden_field_also_counts(self):
        response = self.client.post(self.url, {**self.POST, "next": "book"})
        pet = Pet.objects.get(owner=self.owner)
        self.assertRedirects(
            response, self._book_url(pet), fetch_redirect_response=False
        )

    def test_the_booking_page_starts_with_that_pet(self):
        response = self.client.post(f"{self.url}?next=book", self.POST, follow=True)
        pet = Pet.objects.get(owner=self.owner)
        self.assertEqual(response.context["form"]["pet"].value(), pet.pk)

    def test_without_it_or_with_junk_the_pet_page(self):
        queries = (
            "",
            "?next=",
            "?next=https://example.com/",
            "?next=/dashboard/",
            "?next=BOOK",
        )
        for query in queries:
            with self.subTest(query=query):
                Pet.objects.all().delete()
                response = self.client.post(f"{self.url}{query}", self.POST)
                pet = Pet.objects.get(owner=self.owner)
                self.assertRedirects(
                    response,
                    reverse("owner_pet_detail", args=[pet.pk]),
                    fetch_redirect_response=False,
                )

    def test_a_junk_hidden_field_is_ignored(self):
        response = self.client.post(self.url, {**self.POST, "next": "https://x.test/"})
        pet = Pet.objects.get(owner=self.owner)
        self.assertRedirects(
            response,
            reverse("owner_pet_detail", args=[pet.pk]),
            fetch_redirect_response=False,
        )

    def test_the_form_carries_next_only_when_it_is_set(self):
        with_next = self.client.get(f"{self.url}?next=book")
        self.assertEqual(with_next.context["next_value"], "book")
        self.assertContains(with_next, '<input type="hidden" name="next" value="book">')
        for query in ("", "?next=https://example.com/"):
            with self.subTest(query=query):
                response = self.client.get(f"{self.url}{query}")
                self.assertEqual(response.context["next_value"], "")
                self.assertNotContains(response, 'name="next"')

    def test_kept_through_a_failed_post(self):
        response = self.client.post(f"{self.url}?next=book", {"species": "Dog"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Pet.objects.exists())
        self.assertContains(response, '<input type="hidden" name="next" value="book">')
        # The pet form has no action: the next POST goes to the same URL.
        # The hidden field alone also carries it.
        again = self.client.post(self.url, {**self.POST, "next": "book"})
        pet = Pet.objects.get(owner=self.owner)
        self.assertRedirects(again, self._book_url(pet), fetch_redirect_response=False)


class OwnerProfileEmailReadOnlyTests(TestCase):
    """Slice 8a-2c (decision 3): only the clinic changes an owner's email.
    My details shows it as text; a posted email changes nothing."""

    SENTENCE = "To change your email, contact the clinic."

    def setUp(self):
        self.owner = _login_owner(self)
        self.owner.user.is_onboarded = True
        self.owner.user.save()
        self.url = reverse("owner_profile_edit")

    def _post(self, **changes):
        data = {"first_name": "Ana", "last_name": "Cruz", **changes}
        data.update(_links_post())
        return self.client.post(self.url, data)

    def test_the_form_has_no_email_field(self):
        response = self.client.get(self.url)
        self.assertNotIn("email", response.context["profile_form"].fields)

    def test_email_is_shown_as_text_with_the_sentence(self):
        response = self.client.get(self.url)
        self.assertContains(response, "ana@example.com")
        self.assertContains(response, self.SENTENCE)
        self.assertNotRegex(response.content.decode(), r'name="email"')

    def test_a_posted_email_changes_nothing(self):
        for posted in ("new@example.com", "ANA@EXAMPLE.COM", ""):
            with self.subTest(posted=posted):
                response = self._post(email=posted)
                self.assertRedirects(
                    response, reverse("owner_profile"), fetch_redirect_response=False
                )
                self.owner.user.refresh_from_db()
                self.assertEqual(self.owner.user.email, "ana@example.com")
                self.assertFalse(
                    User.objects.filter(email__iexact="new@example.com").exists()
                )

    def test_the_rest_still_saves_beside_a_posted_email(self):
        response = self._post(
            first_name="Anita",
            contact_number="09171234567",
            email="new@example.com",
        )
        self.assertRedirects(
            response, reverse("owner_profile"), fetch_redirect_response=False
        )
        self.owner.refresh_from_db()
        self.assertEqual(self.owner.first_name, "Anita")
        self.assertEqual(self.owner.contact_number, "09171234567")
        self.assertEqual(self.owner.user.email, "ana@example.com")


# ── Slice 8b-3: owner onboarding and the owner Add pet page ──────────────────

INLINE_SCRIPT = r"<script(?![^>]*\bsrc=)[^>]*>"
VIOLET_BUTTON = r"<(?:a|button)\b[^>]*\bbg-violet-600\b[^>]*>"


class AddressMixinTests(TestCase):
    """AddressFieldsMixin: the admin owner form keeps every part optional, the
    owner's onboarding form requires barangay, town and province."""

    BASE = {"first_name": "Ana", "last_name": "Cruz"}

    def _admin(self, **data):
        return AdminPetOwnerForm({**self.BASE, **data})

    def _owner(self, **data):
        return OwnerOnboardingForm({**self.BASE, **data})

    def _errors(self, form):
        return {name: list(errors) for name, errors in form.errors.items()}

    def test_the_flag_per_form(self):
        self.assertFalse(AdminPetOwnerForm.address_required)
        self.assertTrue(OwnerOnboardingForm.address_required)

    def test_admin_optional_in_both_modes(self):
        for province in ("Marinduque", "other"):
            with self.subTest(province=province):
                form = self._admin(address_province=province)
                self.assertTrue(form.is_valid(), form.errors)

    def test_owner_marinduque_asks_for_the_town_and_barangay(self):
        form = self._owner(address_province="Marinduque")
        self.assertFalse(form.is_valid())
        self.assertEqual(
            self._errors(form),
            {
                "address_town": ["Choose your town."],
                "address_barangay": ["Choose your barangay."],
            },
        )

    def test_owner_marinduque_town_without_barangay(self):
        form = self._owner(address_province="Marinduque", address_town="Boac")
        self.assertFalse(form.is_valid())
        self.assertEqual(
            self._errors(form), {"address_barangay": ["Choose your barangay."]}
        )

    def test_owner_marinduque_barangay_without_town(self):
        form = self._owner(address_province="Marinduque", address_barangay="Amoingon")
        self.assertFalse(form.is_valid())
        self.assertEqual(self._errors(form), {"address_town": ["Choose your town."]})

    def test_owner_other_province_asks_for_all_three(self):
        form = self._owner(address_province="other")
        self.assertFalse(form.is_valid())
        self.assertEqual(
            self._errors(form),
            {
                "province": ["Type your province."],
                "city_municipality": ["Type your town."],
                "barangay": ["Type your barangay."],
            },
        )

    def test_owner_other_province_each_missing_part(self):
        full = {
            "province": "Batangas",
            "city_municipality": "Lipa",
            "barangay": "Sabang",
        }
        messages = {
            "province": "Type your province.",
            "city_municipality": "Type your town.",
            "barangay": "Type your barangay.",
        }
        for name, message in messages.items():
            with self.subTest(missing=name):
                form = self._owner(address_province="other", **{**full, name: " "})
                self.assertFalse(form.is_valid())
                self.assertEqual(self._errors(form), {name: [message]})

    def test_owner_street_stays_optional(self):
        form = self._owner(**OWNER_ADDRESS)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["address_line"], "")

    def test_owner_saves_the_psa_spelling(self):
        # The town select takes the exact name; the barangay any case
        form = self._owner(
            address_province="Marinduque",
            address_town="Boac",
            address_barangay="amoingon",
        )
        self.assertTrue(form.is_valid(), form.errors)
        data = form.cleaned_data
        self.assertEqual(data["province"], "Marinduque")
        self.assertEqual(data["city_municipality"], "Boac")
        self.assertEqual(data["barangay"], "Amoingon")

    def test_owner_other_province_is_saved_as_typed(self):
        form = self._owner(
            address_province="other",
            province="batangas",
            city_municipality="lipa city",
            barangay="sabang",
        )
        self.assertTrue(form.is_valid(), form.errors)
        data = form.cleaned_data
        self.assertEqual(
            (data["province"], data["city_municipality"], data["barangay"]),
            ("Batangas", "Lipa City", "Sabang"),
        )

    def test_the_other_rules_are_the_same(self):
        # A barangay of another town, and a town not on the list
        for form_class in (AdminPetOwnerForm, OwnerOnboardingForm):
            with self.subTest(form=form_class.__name__):
                form = form_class(
                    {
                        **self.BASE,
                        "address_province": "Marinduque",
                        "address_town": "Boac",
                        "address_barangay": "Not A Place",
                    }
                )
                self.assertFalse(form.is_valid())
                self.assertEqual(
                    self._errors(form),
                    {"address_barangay": ["Choose a barangay in Boac."]},
                )
                form = form_class(
                    {**self.BASE, "address_province": "Marinduque", "address_town": "X"}
                )
                self.assertFalse(form.is_valid())
                self.assertEqual(
                    self._errors(form),
                    {"address_town": ["Choose a town from the list."]},
                )

    def test_hand_typed_initial_is_the_same_for_both_forms(self):
        owner = _owner(
            first="Hand",
            email="hand@example.com",
            province="Marinduque",
            city_municipality="Nowhere",
            barangay="Sabang",
        )
        for form_class in (AdminPetOwnerForm, OwnerOnboardingForm):
            with self.subTest(form=form_class.__name__):
                form = form_class(instance=owner)
                self.assertEqual(form.address_mode, "other")
                self.assertTrue(form.address_hand_typed)

    def test_saved_list_address_picks_the_dropdowns(self):
        owner = _owner(
            first="Pick",
            email="pick@example.com",
            province="marinduque",
            city_municipality="boac",
            barangay="amoingon",
        )
        form = OwnerOnboardingForm(instance=owner)
        self.assertEqual(form.address_mode, "marinduque")
        self.assertFalse(form.address_hand_typed)
        self.assertEqual(form.fields["address_town"].initial, "Boac")
        self.assertEqual(form.fields["address_barangay"].initial, "Amoingon")

    def test_each_form_loads_barangays_from_its_own_endpoint(self):
        admin_attrs = AdminPetOwnerForm().fields["address_town"].widget.attrs
        owner_attrs = OwnerOnboardingForm().fields["address_town"].widget.attrs
        self.assertEqual(admin_attrs["hx-get"], reverse("admin_address_barangays"))
        self.assertEqual(owner_attrs["hx-get"], reverse("owner_address_barangays"))

    def test_error_count(self):
        form = self._owner(address_province="Marinduque")
        self.assertFalse(form.is_valid())
        self.assertEqual(form.error_count, 2)


class OwnerAddressEndpointTests(TestCase):
    """owner_address_barangays: the admin partial, for owners (also before a
    PetOwner row exists), refused for the clinic."""

    def setUp(self):
        self.url = reverse("owner_address_barangays")
        self.user = User.objects.create_user(
            username="newbie",
            email="newbie@example.com",
            password="pass-12345-x",
            role=User.PET_OWNER,
        )

    def test_the_url(self):
        self.assertEqual(self.url, "/owner/address/barangays/")

    def test_an_owner_without_a_pet_owner_row_is_allowed(self):
        self.client.force_login(self.user)
        self.assertFalse(PetOwner.objects.exists())
        response = self.client.get(self.url, {"town": "Boac"})
        self.assertEqual(response.status_code, 200)

    def test_the_partial_and_both_params(self):
        self.client.force_login(self.user)
        for params in ({"town": "boac"}, {"address_town": "Boac"}):
            with self.subTest(params=params):
                response = self.client.get(
                    self.url, params, headers={"HX-Request": "true"}
                )
                self.assertTemplateUsed(response, "admin/pets/_barangay_options.html")
                options = _options(response.content.decode())
                self.assertEqual(options[0], ("", "Choose a barangay"))
                self.assertEqual([v for v, _ in options[1:]], barangays_for("Boac"))

    def test_unknown_or_empty_town(self):
        self.client.force_login(self.user)
        for town in ("", "Nowhere"):
            with self.subTest(town=town):
                response = self.client.get(self.url, {"town": town})
                self.assertEqual(
                    _options(response.content.decode()),
                    [("", "Choose the town first")],
                )

    def test_the_clinic_is_refused(self):
        _admin(self)
        response = self.client.get(self.url, {"town": "Boac"})
        self.assertRedirects(
            response, "/admin/dashboard/", fetch_redirect_response=False
        )
        response = self.client.get(
            self.url, {"town": "Boac"}, headers={"HX-Request": "true"}
        )
        self.assertEqual(response.status_code, 403)

    def test_logged_out_goes_to_the_login(self):
        response = self.client.get(self.url, {"town": "Boac"})
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response["Location"])
        self.assertNotIn("<option", response.content.decode())

    def test_the_admin_endpoint_is_unchanged_for_owners(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("admin_address_barangays"))
        self.assertEqual(response.status_code, 302)


class OwnerBreedEndpointTests(TestCase):
    """owner_pet_breed_options: the admin partial, for owners (also before a
    PetOwner row exists), refused for the clinic."""

    def setUp(self):
        self.url = reverse("owner_pet_breed_options")
        self.user = User.objects.create_user(
            username="newbie",
            email="newbie@example.com",
            password="pass-12345-x",
            role=User.PET_OWNER,
        )

    def test_the_url(self):
        self.assertEqual(self.url, "/owner/pets/breed-options/")

    def test_an_owner_without_a_pet_owner_row_is_allowed(self):
        self.client.force_login(self.user)
        self.assertFalse(PetOwner.objects.exists())
        response = self.client.get(self.url, {"species": "Dog"})
        self.assertEqual(response.status_code, 200)

    def test_the_partial_and_both_params(self):
        self.client.force_login(self.user)
        for params in ({"species": "dog"}, {"species_choice": "Dog"}):
            with self.subTest(params=params):
                response = self.client.get(
                    self.url, params, headers={"HX-Request": "true"}
                )
                self.assertTemplateUsed(response, "admin/pets/_breed_options.html")
                options = _options(response.content.decode())
                self.assertEqual(options[0], ("", "Not sure"))
                self.assertEqual(options[-1], ("__other__", "Other (type it)"))
                self.assertEqual([v for v, _ in options[1:-1]], breeds_for("Dog"))

    def test_unknown_or_empty_species(self):
        self.client.force_login(self.user)
        for species in ("", "Dragon"):
            with self.subTest(species=species):
                response = self.client.get(self.url, {"species": species})
                self.assertEqual(
                    _options(response.content.decode()),
                    [("", "Choose the species first")],
                )

    def test_the_clinic_is_refused(self):
        _admin(self)
        response = self.client.get(self.url, {"species": "Dog"})
        self.assertRedirects(
            response, "/admin/dashboard/", fetch_redirect_response=False
        )
        response = self.client.get(
            self.url, {"species": "Dog"}, headers={"HX-Request": "true"}
        )
        self.assertEqual(response.status_code, 403)

    def test_logged_out_goes_to_the_login(self):
        response = self.client.get(self.url, {"species": "Dog"})
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response["Location"])
        self.assertNotIn("<option", response.content.decode())


class OwnerPetFormTests(TestCase):
    """OwnerPetForm: the owner's pet form (onboarding step 2 and Add pet)."""

    def _form(self, **data):
        return OwnerPetForm({"name": "bantay", "gender": "unknown", **data})

    def test_the_fields(self):
        self.assertEqual(
            set(OwnerPetForm().fields),
            {
                "species_choice",
                "breed_choice",
                "name",
                "species",
                "breed",
                "color",
                "gender",
                "date_of_birth",
                "weight",
                "registration_number",
                "photo",
            },
        )

    def test_the_owner_is_not_a_field(self):
        self.assertNotIn("owner", OwnerPetForm().fields)

    def test_sex_starts_on_not_sure(self):
        choices = OwnerPetForm().fields["gender"].choices
        self.assertEqual(
            list(choices),
            [("unknown", "Not sure"), ("male", "Male"), ("female", "Female")],
        )
        form = OwnerPetForm({"name": "Bantay", "species_choice": "Dog"})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["gender"], "unknown")

    def test_name_is_required(self):
        form = self._form(name="  ", species_choice="Dog")
        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors["name"], ["Enter your pet's name."])

    def test_species_is_required(self):
        form = self._form()
        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors["species_choice"], ["Choose the species."])

    def test_a_species_from_the_list_with_a_breed(self):
        form = self._form(species_choice="Dog", breed_choice="aspin (native)")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["species"], "Dog")
        self.assertEqual(form.cleaned_data["breed"], "Aspin (Native)")

    def test_a_breed_of_another_species_is_refused(self):
        form = self._form(species_choice="Dog", breed_choice="Persian")
        self.assertFalse(form.is_valid())
        self.assertEqual(
            form.errors["breed_choice"], ["Choose a breed for Dog, or pick Other."]
        )

    def test_breed_is_optional(self):
        form = self._form(species_choice="Cat")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["breed"], "")

    def test_breed_other_uses_the_typed_box(self):
        form = self._form(species_choice="Dog", breed_choice="__other__", breed="Corgi")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["breed"], "Corgi")

    def test_species_other_needs_the_typed_species(self):
        form = self._form(species_choice="__other__")
        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors["species"], ["Type the species."])
        form = self._form(species_choice="__other__", species="turtle", breed="Red")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["species"], "Turtle")
        self.assertEqual(form.cleaned_data["breed"], "Red")

    def test_birthday_is_optional_and_not_in_the_future(self):
        form = self._form(species_choice="Dog")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertIsNone(form.cleaned_data["date_of_birth"])
        future = timezone.localdate() + timedelta(days=1)
        form = self._form(species_choice="Dog", date_of_birth=future.isoformat())
        self.assertFalse(form.is_valid())
        self.assertEqual(
            form.errors["date_of_birth"], ["The birthday can't be in the future."]
        )
        self.assertEqual(
            OwnerPetForm().fields["date_of_birth"].help_text, "A rough date is fine."
        )

    def test_weight_must_be_more_than_zero(self):
        form = self._form(species_choice="Dog", weight="0")
        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors["weight"], ["Weight must be more than 0."])

    def test_color_weight_and_registration_are_optional(self):
        form = self._form(species_choice="Dog")
        self.assertTrue(form.is_valid(), form.errors)

    def test_it_asks_the_owner_endpoint_for_breeds(self):
        attrs = OwnerPetForm().fields["species_choice"].widget.attrs
        self.assertEqual(attrs["hx-get"], reverse("owner_pet_breed_options"))
        admin_attrs = AdminPetForm().fields["species_choice"].widget.attrs
        self.assertEqual(admin_attrs["hx-get"], reverse("admin_pet_breed_options"))

    def test_posting_another_owners_pk_does_nothing(self):
        mine = _login_owner(self)
        mine.user.is_onboarded = True
        mine.user.save()
        other = _owner(first="Ben", last="Reyes", email="ben@example.com")
        response = self.client.post(
            reverse("owner_pet_add"),
            {
                "name": "Rex",
                "species_choice": "Dog",
                "gender": "male",
                "owner": other.pk,
                "owner_id": other.pk,
            },
        )
        pet = Pet.objects.get(name="Rex")
        self.assertRedirects(
            response,
            reverse("owner_pet_detail", args=[pet.pk]),
            fetch_redirect_response=False,
        )
        self.assertEqual(pet.owner, mine)
        self.assertFalse(Pet.objects.filter(owner=other).exists())


class OnboardingPagesTests(TestCase):
    """Onboarding on layouts/owner.html: step 1 About you, step 2 Your first
    pet (skippable), step 3 Pet added."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="newbie",
            email="newbie@example.com",
            password="pass-12345-x",
            role=User.PET_OWNER,
        )
        self.client.force_login(self.user)
        self.step1 = reverse("owner_onboarding_step1")
        self.step2 = reverse("owner_onboarding_step2")
        self.step3 = reverse("owner_onboarding_step3")

    def _data(self, **changes):
        data = {
            "first_name": "Ana",
            "last_name": "Cruz",
            **OWNER_ADDRESS,
            **_links_post(),
        }
        return {**data, **changes}

    def _to_step2(self):
        response = self.client.post(self.step1, self._data())
        self.assertRedirects(response, self.step2, fetch_redirect_response=False)

    def _pet(self, **changes):
        return {
            "name": "Snow",
            "species_choice": "Cat",
            "gender": "female",
            **changes,
        }

    def _page(self, url):
        return self.client.get(url).content.decode()

    # ── Layout ───────────────────────────────────────────────────────────────

    def test_step_one_and_two_are_on_the_owner_layout(self):
        self.assertTemplateUsed(self.client.get(self.step1), "layouts/owner.html")
        self._to_step2()
        self.assertTemplateUsed(self.client.get(self.step2), "layouts/owner.html")

    def test_step_three_is_on_the_owner_layout(self):
        self._to_step2()
        self.client.post(self.step2, self._pet())
        self.assertTemplateUsed(self.client.get(self.step3), "layouts/owner.html")

    def test_step_two_has_a_base_layout_now(self):
        self._to_step2()
        html = self._page(self.step2)
        self.assertEqual(html.count("<!doctype html>"), 1)
        self.assertRegex(html, r'<link rel="stylesheet" href="/static/css/app\.')
        self.assertRegex(html, r"/static/vendor/htmx-2\.0\.11\.min\.[0-9a-f]+\.js")

    def _all_pages(self):
        """The HTML of step 1, 2 and 3, fetched in the order a user sees them
        (once the pet is saved, steps 1 and 2 redirect)."""
        pages = [self._page(self.step1)]
        self._to_step2()
        pages.append(self._page(self.step2))
        self.client.post(self.step2, self._pet())
        pages.append(self._page(self.step3))
        return pages

    def test_no_bell_and_no_tabs(self):
        for html in self._all_pages():
            self.assertNotEqual(html, "")
            self.assertNotIn(reverse("owner_notifications"), html)
            self.assertNotIn('aria-label="Notifications', html)
            self.assertNotIn('aria-label="Main"', html)
            self.assertIn("Hapi Tutz", html)

    # ── Progress ─────────────────────────────────────────────────────────────

    def test_progress_per_step(self):
        self._to_step2()
        self.assertContains(self.client.get(self.step1), "Step 1 of 2")
        self.assertContains(self.client.get(self.step1), 'aria-valuenow="1"')
        step2 = self.client.get(self.step2)
        self.assertContains(step2, "Step 2 of 2")
        self.assertContains(step2, 'aria-valuenow="2"')
        self.assertContains(step2, 'role="progressbar"')
        self.assertContains(step2, 'aria-valuemax="2"')

    # ── One violet button, flow buttons ──────────────────────────────────────

    def test_one_primary_per_step(self):
        self.assertEqual(len(re.findall(VIOLET_BUTTON, self._page(self.step1))), 1)
        self._to_step2()
        self.assertEqual(len(re.findall(VIOLET_BUTTON, self._page(self.step2))), 1)
        self.client.post(self.step2, self._pet())
        self.assertEqual(len(re.findall(VIOLET_BUTTON, self._page(self.step3))), 1)

    def test_step_one_has_next_and_no_back(self):
        html = self._page(self.step1)
        self.assertRegex(
            html, r'<button type="submit" form="onboarding-form"[^>]*>Next'
        )
        self.assertNotIn(">Back<", html)

    def test_step_two_buttons_are_the_same_size(self):
        self._to_step2()
        html = self._page(self.step2)
        form_button = r'<button type="submit" form="onboarding-form" class="([^"]*)"'
        add = re.search(form_button, html)
        skip = re.search(r'<button[^>]*value="skip"[^>]*class="([^"]*)"', html)
        back = re.search(rf'<a href="{self.step1}" class="([^"]*)"', html)
        for found in (add, skip, back):
            self.assertIsNotNone(found)
            self.assertIn("min-h-14", found.group(1))
            self.assertIn("text-lg", found.group(1))
        self.assertIn("bg-violet-600", add.group(1))
        self.assertNotIn("bg-violet-600", skip.group(1))
        self.assertIn("Skip for now", html)
        self.assertIn("Add pet", html)

    # ── Step 1 ───────────────────────────────────────────────────────────────

    def test_address_is_required_on_step_one(self):
        cases = {
            "Choose your town.": {"address_province": "Marinduque"},
            "Choose your barangay.": {
                "address_province": "Marinduque",
                "address_town": "Boac",
                "address_barangay": "",
            },
            "Type your province.": {"address_province": "other"},
            "Type your town.": {"address_province": "other"},
            "Type your barangay.": {"address_province": "other"},
        }
        for message, address in cases.items():
            with self.subTest(message=message):
                data = self._data(address_barangay="", address_town="")
                data.update(address)
                response = self.client.post(self.step1, data)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, message)
                self.assertContains(response, "Please fix the")
                self.assertFalse(PetOwner.objects.exists())

    def test_a_valid_marinduque_address_saves(self):
        response = self.client.post(
            self.step1,
            self._data(address_town="Boac", address_barangay="amoingon"),
        )
        self.assertRedirects(response, self.step2, fetch_redirect_response=False)
        owner = PetOwner.objects.get()
        self.assertEqual(
            (owner.province, owner.city_municipality, owner.barangay),
            ("Marinduque", "Boac", "Amoingon"),
        )
        self.assertEqual(owner.user, self.user)

    def test_a_valid_other_province_address_saves(self):
        data = self._data(
            address_province="other",
            province="batangas",
            city_municipality="lipa city",
            barangay="sabang",
            address_line="Purok 3",
        )
        response = self.client.post(self.step1, data)
        self.assertRedirects(response, self.step2, fetch_redirect_response=False)
        owner = PetOwner.objects.get()
        self.assertEqual(
            (owner.province, owner.city_municipality, owner.barangay),
            ("Batangas", "Lipa City", "Sabang"),
        )
        self.assertEqual(owner.address_line, "Purok 3")

    def test_the_page_has_the_address_card_and_the_endpoint(self):
        html = self._page(self.step1)
        self.assertIn("Your address", html)
        self.assertIn(f'hx-get="{reverse("owner_address_barangays")}"', html)
        self.assertNotIn(reverse("admin_address_barangays"), html)
        self.assertIn("data-address-pick", html)
        self.assertIn("data-address-typed", html)
        self.assertRegex(html, r"/static/js/shared/address_form\.[0-9a-f]+\.js")
        self.assertRegex(html, r"/static/js/shared/photo_input\.[0-9a-f]+\.js")

    def test_errors_show_in_a_summary_and_under_the_fields(self):
        response = self.client.post(
            self.step1, self._data(first_name="", address_town="")
        )
        self.assertContains(response, 'role="alert"')
        self.assertContains(response, "Nothing was saved yet.")
        # (Django's own required message comes before the form's clean_first_name)
        self.assertContains(response, "This field is required.")
        self.assertContains(response, "Choose your town.")
        self.assertEqual(response.context["error_count"], 2)

    def test_re_entry_still_binds_the_existing_owner(self):
        self._to_step2()
        first_pk = PetOwner.objects.get().pk
        gasan = barangays_for("Gasan")[0]
        response = self.client.post(
            self.step1,
            self._data(first_name="Anna", address_town="Gasan", address_barangay=gasan),
        )
        self.assertRedirects(response, self.step2, fetch_redirect_response=False)
        owner = PetOwner.objects.get()
        self.assertEqual(owner.pk, first_pk)
        self.assertEqual(owner.first_name, "Anna")
        self.assertEqual(owner.city_municipality, "Gasan")

    def test_the_saved_address_shows_again(self):
        self._to_step2()
        form = self.client.get(self.step1).context["profile_form"]
        self.assertEqual(form.address_mode, "marinduque")
        self.assertEqual(form.fields["address_town"].initial, "Boac")

    # ── Step 2 ───────────────────────────────────────────────────────────────

    def test_skip_finishes_onboarding_as_before(self):
        self._to_step2()
        response = self.client.post(self.step2, {"action": "skip"})
        self.assertRedirects(
            response, reverse("owner_dashboard"), fetch_redirect_response=False
        )
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_onboarded)
        self.assertNotIn("onboarding_owner_id", self.client.session)
        self.assertFalse(Pet.objects.exists())

    def test_step_two_adds_the_pet_to_the_new_owner(self):
        self._to_step2()
        response = self.client.post(self.step2, self._pet())
        self.assertRedirects(response, self.step3, fetch_redirect_response=False)
        pet = Pet.objects.get()
        self.assertEqual(pet.owner, PetOwner.objects.get())
        self.assertEqual(pet.name, "Snow")
        self.assertEqual((pet.species, pet.gender), ("Cat", "female"))
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_onboarded)
        self.assertNotIn("onboarding_owner_id", self.client.session)

    def test_step_two_ignores_a_posted_owner(self):
        other = _owner(first="Ben", last="Reyes", email="ben@example.com")
        self._to_step2()
        self.client.post(self.step2, self._pet(owner=other.pk))
        pet = Pet.objects.get()
        self.assertEqual(pet.owner.user, self.user)
        self.assertFalse(Pet.objects.filter(owner=other).exists())

    def test_step_two_errors_keep_the_page(self):
        self._to_step2()
        response = self.client.post(self.step2, self._pet(name="", species_choice=""))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "owner/onboarding/step2.html")
        self.assertContains(response, "Enter your pet&#x27;s name.")
        self.assertContains(response, "Choose the species.")
        self.assertFalse(Pet.objects.exists())
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_onboarded)

    def test_step_two_without_step_one_goes_back(self):
        response = self.client.get(self.step2)
        self.assertRedirects(response, self.step1, fetch_redirect_response=False)

    def test_step_two_uses_the_owner_breed_endpoint(self):
        self._to_step2()
        html = self._page(self.step2)
        self.assertIn(f'hx-get="{reverse("owner_pet_breed_options")}"', html)
        self.assertNotIn(reverse("admin_pet_breed_options"), html)
        self.assertRegex(html, r"/static/js/shared/pet_form\.[0-9a-f]+\.js")
        self.assertNotIn("pet_species_breed.js", html)

    # ── Step 3 ───────────────────────────────────────────────────────────────

    def test_the_step_three_frame(self):
        self._to_step2()
        self.client.post(self.step2, self._pet())
        response = self.client.get(self.step3)
        self.assertContains(response, "<b>Snow was added.</b> Do you have other")
        self.assertContains(response, 'aria-label="No photo of Snow yet"')
        self.assertContains(response, "Cat · Female")
        self.assertContains(response, f'<a href="{reverse("owner_pet_add")}"')
        self.assertContains(response, "Add another pet")
        self.assertContains(
            response,
            f'<a href="{reverse("owner_dashboard")}" class="flex min-h-14',
        )
        self.assertContains(response, "I'm done, go to my dashboard")

    def test_step_three_needs_the_pet_just_added(self):
        self._to_step2()
        self.client.post(self.step2, {"action": "skip"})
        response = self.client.get(self.step3)
        self.assertRedirects(
            response, reverse("owner_dashboard"), fetch_redirect_response=False
        )

    def test_step_three_does_not_show_another_owners_pet(self):
        other = _owner(first="Ben", last="Reyes", email="ben@example.com")
        theirs = _pet(other, "Rex")
        self._to_step2()
        self.client.post(self.step2, {"action": "skip"})
        session = self.client.session
        session["onboarding_pet_id"] = theirs.pk
        session.save()
        response = self.client.get(self.step3)
        self.assertEqual(response.status_code, 302)

    def test_an_admin_is_sent_away(self):
        _admin(self)
        for url in (self.step1, self.step2, self.step3):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 302)

    # ── Markup rules ─────────────────────────────────────────────────────────

    def test_every_link_is_on_one_line(self):
        for html in self._all_pages():
            self.assertNotEqual(html, "")
            for tag in re.findall(r"<a\b[^>]*>", html):
                self.assertNotIn("\n", tag)

    def test_no_inline_script_or_handler(self):
        for html in self._all_pages():
            self.assertNotEqual(html, "")
            self.assertEqual(re.findall(INLINE_SCRIPT, html), [])
            self.assertIsNone(re.search(r"\son[a-z]+=", html))
            self.assertNotIn("hx-confirm", html)


class OwnerAddPetPageTests(TestCase):
    """owner_pet_add on layouts/owner.html, Pets tab lit."""

    def setUp(self):
        self.owner = _login_owner(self)
        self.owner.user.is_onboarded = True
        self.owner.user.save()
        self.url = reverse("owner_pet_add")
        self.book = f"{reverse('owner_book_appointment')}?pet="

    def _page(self, query=""):
        return self.client.get(f"{self.url}{query}").content.decode()

    def test_layout_and_the_pets_tab(self):
        response = self.client.get(self.url)
        self.assertTemplateUsed(response, "layouts/owner.html")
        self.assertTemplateUsed(response, "owner/pets/add.html")
        html = response.content.decode()
        nav = re.search(r'(?s)<nav aria-label="Main"[^>]*>(.*?)</nav>', html).group(1)
        self.assertEqual(nav.count('aria-current="page"'), 1)
        current = re.search(r'<a href="([^"]+)"[^>]*aria-current="page"', nav)
        self.assertEqual(current.group(1), reverse("owner_pet_list"))

    def test_back_bar_to_my_pets(self):
        html = self._page()
        pets = reverse("owner_pet_list")
        self.assertRegex(
            html,
            rf'(?s)<a href="{pets}" [^>]*>\s*<svg.*?</svg>\s*Back to My Pets</a>',
        )

    def test_back_bar_to_booking(self):
        html = self._page("?next=book")
        book = reverse("owner_book_appointment")
        self.assertRegex(
            html,
            rf'(?s)<a href="{book}" [^>]*>\s*<svg.*?</svg>\s*Back to booking</a>',
        )
        self.assertIn("go back to booking with this pet already picked", html)

    def test_junk_next_is_the_my_pets_bar(self):
        html = self._page("?next=https://example.com/")
        self.assertIn("Back to My Pets", html)
        self.assertNotIn("Back to booking", html)

    def test_same_form_and_parts_as_onboarding(self):
        response = self.client.get(self.url)
        self.assertIsInstance(response.context["form"], OwnerPetForm)
        html = response.content.decode()
        self.assertIn(f'hx-get="{reverse("owner_pet_breed_options")}"', html)
        self.assertIn("data-pet-about", html)
        self.assertIn("A rough date is fine.", html)
        self.assertIn("Choose a photo", html)
        self.assertRegex(html, r"/static/js/shared/pet_form\.[0-9a-f]+\.js")
        self.assertRegex(html, r"/static/js/shared/photo_input\.[0-9a-f]+\.js")
        self.assertNotIn("pet_species_breed.js", html)

    def test_next_book_still_returns_with_the_pet_picked(self):
        post = {"name": "Bantay", "species_choice": "Dog", "gender": "unknown"}
        response = self.client.post(f"{self.url}?next=book", post)
        pet = Pet.objects.get(owner=self.owner)
        self.assertRedirects(
            response, f"{self.book}{pet.pk}", fetch_redirect_response=False
        )
        followed = self.client.get(f"{self.book}{pet.pk}")
        self.assertEqual(followed.context["form"]["pet"].value(), pet.pk)

    def test_without_next_it_goes_to_the_pet(self):
        post = {"name": "Bantay", "species_choice": "Dog", "gender": "unknown"}
        response = self.client.post(self.url, post)
        pet = Pet.objects.get(owner=self.owner)
        self.assertRedirects(
            response,
            reverse("owner_pet_detail", args=[pet.pk]),
            fetch_redirect_response=False,
        )

    def test_a_failed_save_keeps_the_page_and_the_back_bar(self):
        response = self.client.post(
            f"{self.url}?next=book", {"name": "", "species_choice": ""}
        )
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("Please fix the 2 items below.", html)
        self.assertIn("Back to booking", html)
        self.assertIn('<input type="hidden" name="next" value="book">', html)

    def test_one_primary_button(self):
        for query in ("", "?next=book"):
            with self.subTest(query=query):
                html = self._page(query)
                found = re.findall(VIOLET_BUTTON, html)
                self.assertEqual(len(found), 1)
                self.assertIn('type="submit"', found[0])
                self.assertEqual(html.count("Add pet</button>"), 1)

    def test_no_inline_script_or_handler_and_one_line_links(self):
        for query in ("", "?next=book"):
            with self.subTest(query=query):
                html = self._page(query)
                self.assertEqual(re.findall(INLINE_SCRIPT, html), [])
                self.assertIsNone(re.search(r"\son[a-z]+=", html))
                for tag in re.findall(r"<a\b[^>]*>", html):
                    self.assertNotIn("\n", tag)


# ── Slice 8b-4: owner My Pets ────────────────────────────────────────────────


class OwnerPetsContextTests(TestCase):
    """owner_pets_context(owner, params): order, search, type, counts."""

    def setUp(self):
        self.owner = _owner()

    def _context(self, query="", owner=None):
        return owner_pets_context(owner or self.owner, QueryDict(query))

    def _names(self, query=""):
        return [pet.name for pet in self._context(query)["pets"]]

    def _mixed(self):
        """Two dogs, a lower-case cat, a rabbit, a bird and a blank species."""
        for name, species in (
            ("Rex", "Dog"),
            ("Bantay", "dog"),
            ("Tom", "cat"),
            ("Kiko", "Rabbit"),
            ("Pip", "Bird"),
            ("Blank", ""),
        ):
            _pet(self.owner, name, species)

    def test_by_name_then_pk(self):
        for name in ("Mingming", "Browny", "Choco"):
            _pet(self.owner, name)
        first = _pet(self.owner, "Max")
        second = _pet(self.owner, "Max")
        self.assertEqual(self._names(), ["Browny", "Choco", "Max", "Max", "Mingming"])
        pets = self._context()["pets"]
        self.assertEqual([p.pk for p in pets if p.name == "Max"], [first.pk, second.pk])
        self.assertEqual(self._names()[2:4], ["Max", "Max"])

    def test_search_matches_the_name_only(self):
        _pet(self.owner, "Browny", "Dog", "Aspin")
        _pet(self.owner, "Mini Mo", "Cat", "Puspin")
        self.assertEqual(self._names("q=brow"), ["Browny"])
        self.assertEqual(self._names("q=NY"), ["Browny"])
        self.assertEqual(self._names("q=Aspin"), [])  # the breed
        self.assertEqual(self._names("q=Cat"), [])  # the species
        self.assertEqual(self._names("q=zzz"), [])

    def test_search_collapses_spaces(self):
        _pet(self.owner, "Mini Mo")
        context = self._context("q=++mini+++mo+")
        self.assertEqual(context["q"], "mini mo")
        self.assertEqual([p.name for p in context["pets"]], ["Mini Mo"])
        self.assertEqual(self._context("q=+++")["q"], "")

    def test_each_type(self):
        self._mixed()
        self.assertEqual(self._names("type=dog"), ["Bantay", "Rex"])
        self.assertEqual(self._names("type=cat"), ["Tom"])
        # Everything that is not a Dog or a Cat, an empty species too
        self.assertEqual(self._names("type=other"), ["Blank", "Kiko", "Pip"])
        self.assertEqual(len(self._names("type=all")), 6)

    def test_default_and_unknown_types_are_all(self):
        self._mixed()
        for query in ("", "type=all", "type=wolf", "type=", "type=DOG"):
            with self.subTest(query=query):
                context = self._context(query)
                self.assertEqual(context["active_type"], "all")
                self.assertEqual(len(context["pets"]), 6)
                self.assertIs(context["is_filtering"], False)

    def test_type_and_search_work_together(self):
        self._mixed()
        self.assertEqual(self._names("type=dog&q=rex"), ["Rex"])
        self.assertEqual(self._names("type=cat&q=rex"), [])

    def test_counts_cover_every_pet_and_ignore_the_search(self):
        self._mixed()
        everything = {"all": 6, "dog": 2, "cat": 1, "other": 3}
        for query in ("", "q=rex", "type=cat", "type=cat&q=zzz"):
            with self.subTest(query=query):
                context = self._context(query)
                self.assertEqual(context["counts"], everything)
                self.assertEqual(context["total_count"], 6)
        chips = self._context("q=rex")["chips"]
        self.assertEqual([c["count"] for c in chips], [6, 2, 1, 3])

    def test_chips_keep_the_search_and_hide_empty_types(self):
        _pet(self.owner, "Rex", "Dog")
        _pet(self.owner, "Tom", "Cat")
        chips = self._context("q=ma")["chips"]
        self.assertEqual([c["key"] for c in chips], ["all", "dog", "cat"])
        self.assertEqual(
            [c["query"] for c in chips], ["q=ma", "type=dog&q=ma", "type=cat&q=ma"]
        )
        self.assertEqual([c["is_active"] for c in chips], [True, False, False])
        # An open type stays, even when it has no pet
        chips = self._context("type=other")["chips"]
        self.assertEqual([c["key"] for c in chips], ["all", "dog", "cat", "other"])
        self.assertEqual([c["is_active"] for c in chips], [False, False, False, True])

    def test_filters_show_from_four_pets(self):
        for number in range(3):
            _pet(self.owner, f"Pet{number}")
        self.assertIs(self._context()["show_filters"], False)
        _pet(self.owner, "Pet3")
        self.assertIs(self._context()["show_filters"], True)

    def test_archived_pets_are_left_out_of_everything(self):
        for number in range(3):
            _pet(self.owner, f"Pet{number}", "Cat")
        _pet(self.owner, "Gone", "Dog", is_archived=True)
        context = self._context("type=dog")
        self.assertEqual(context["pets"], [])
        self.assertEqual(context["counts"]["dog"], 0)
        self.assertEqual(context["total_count"], 3)
        self.assertIs(context["show_filters"], False)  # 3 pets, not 4

    def test_another_owners_pets_are_never_shown_or_counted(self):
        mine = _pet(self.owner, "Mine", "Dog")
        other = _owner(first="Ben", last="Reyes", email="ben@example.com")
        _pet(other, "Theirs", "Dog")
        context = self._context()
        self.assertEqual(context["pets"], [mine])
        self.assertEqual(context["counts"]["all"], 1)
        self.assertEqual(self._names("q=theirs"), [])
        theirs = self._context(owner=other)["pets"]
        self.assertEqual([p.name for p in theirs], ["Theirs"])

    def test_pending_removal_flag(self):
        waiting = _pet(self.owner, "Waiting")
        _request(waiting, PetDeletionRequest.PENDING)
        for name, status in (
            ("Approved", PetDeletionRequest.APPROVED),
            ("Rejected", PetDeletionRequest.REJECTED),
            ("TakenBack", PetDeletionRequest.CANCELLED),
        ):
            _request(_pet(self.owner, name), status)
        _pet(self.owner, "Plain")
        flags = {p.name: p.has_pending_removal for p in self._context()["pets"]}
        self.assertEqual(
            flags,
            {
                "Waiting": True,
                "Approved": False,
                "Rejected": False,
                "TakenBack": False,
                "Plain": False,
            },
        )

    def test_queries_are_flat(self):
        def count():
            with CaptureQueriesContext(connection) as queries:
                context = self._context("type=dog")
                list(context["pets"])
            return len(queries)

        for number in range(3):
            _request(_pet(self.owner, f"Few{number}", "Dog"))
        few = count()
        for number in range(27):
            _request(_pet(self.owner, f"Many{number}", "Dog"))
        self.assertEqual(count(), few)
        self.assertEqual(few, 2)  # the counts and the list


class OwnerPetsPageTests(TestCase):
    """owner_pet_list on layouts/owner.html, Pets tab, white bar."""

    PARTS = (
        "owner/pets/list.html",
        "owner/pets_parts/header.html",
        "owner/pets_parts/filters.html",
        "owner/pets_parts/pet_card.html",
        "owner/pets_parts/no_results.html",
        "owner/pets_parts/no_pets.html",
    )

    def setUp(self):
        self.owner = _login_owner(self)
        self.owner.user.is_onboarded = True
        self.owner.user.save()
        self.url = reverse("owner_pet_list")

    def _html(self, query=""):
        response = self.client.get(f"{self.url}{query}")
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def _pets(self, *names):
        return [_pet(self.owner, name) for name in names]

    def _card(self, html, pet):
        detail = reverse("owner_pet_detail", args=[pet.pk])
        found = re.search(
            rf'(?s)<a href="{detail}" class="flex w-full flex-col.*?</a>', html
        )
        self.assertIsNotNone(found, pet.name)
        return found.group(0)

    def _chips(self, html):
        nav = re.search(
            r'(?s)<nav aria-label="Show pets by type"[^>]*>(.*?)</nav>', html
        )
        self.assertIsNotNone(nav)
        return nav.group(1)

    def test_layout_the_pets_tab_and_the_white_bar(self):
        self._pets("Bantay")
        response = self.client.get(self.url)
        self.assertTemplateUsed(response, "layouts/owner.html")
        self.assertTemplateUsed(response, "owner/pets/list.html")
        self.assertTemplateNotUsed(response, "base_dashboard.html")
        html = response.content.decode()
        nav = re.search(r'(?s)<nav aria-label="Main"[^>]*>(.*?)</nav>', html).group(1)
        self.assertEqual(nav.count('aria-current="page"'), 1)
        current = re.search(r'<a href="([^"]+)"[^>]*aria-current="page"', nav)
        self.assertEqual(current.group(1), self.url)
        # The normal white bar, no violet band
        self.assertRegex(html, r'<header class="sticky top-0 z-20[^"]*bg-white')
        self.assertNotIn("data-dashboard-band", html)
        # (the Add a pet button has hover:bg-violet-700, so match the plain class)
        self.assertNotRegex(html, r'"[^"]*\sbg-violet-700\b')

    def test_add_a_pet_is_the_one_primary_button(self):
        add = reverse("owner_pet_add")
        self._pets("A", "B", "C", "D")
        for query in ("", "?q=zzz", "?type=cat"):
            with self.subTest(query=query):
                found = re.findall(VIOLET_BUTTON, self._html(query))
                self.assertEqual(len(found), 1)
                self.assertIn(f'href="{add}"', found[0])
        self.assertIn("Add a pet</a>", self._html())

    def test_the_count_line(self):
        self._pets("A")
        self.assertIn("1 pet</p>", self._html())
        self._pets("B")
        self.assertIn("2 pets</p>", self._html())

    def test_search_and_chips_only_from_four_pets(self):
        self._pets("A", "B", "C")
        html = self._html()
        self.assertNotIn("Search your pets", html)
        self.assertNotIn('role="search"', html)
        self.assertNotIn("Show pets by type", html)
        self._pets("D")
        html = self._html()
        self.assertIn(f'<form method="get" action="{self.url}" role="search"', html)
        self.assertIn(
            '<label for="pet-search" class="sr-only">Search your pets</label>', html
        )
        self.assertIn('placeholder="Search your pets by name"', html)
        self.assertIn('<input type="hidden" name="type" value="all">', html)
        self.assertIn('type="search" name="q"', html)
        self.assertIn(
            '<button type="submit" class="btn btn-outline min-h-12">Search</button>',
            html,
        )

    def test_the_search_form_keeps_the_type_and_the_text(self):
        for name, species in (("Rex", "Dog"), ("Max", "Dog"), ("Tom", "Cat")):
            _pet(self.owner, name, species)
        _pet(self.owner, "Kiko", "Rabbit")
        html = self._html("?type=dog&q=ma")
        self.assertIn('<input type="hidden" name="type" value="dog">', html)
        self.assertIn('value="ma"', html)
        self.assertIn(">Max<", self._card(html, Pet.objects.get(name="Max")))
        self.assertNotIn("Rex", html)

    def test_chip_links_keep_the_search(self):
        for name, species in (("Rex", "Dog"), ("Max", "Dog"), ("Tom", "Cat")):
            _pet(self.owner, name, species)
        _pet(self.owner, "Kiko", "Rabbit")
        chips = self._chips(self._html("?q=ma"))
        hrefs = re.findall(r'<a href="([^"]+)"', chips)
        self.assertEqual(
            hrefs,
            [
                f"{self.url}?q=ma",
                f"{self.url}?type=dog&amp;q=ma",
                f"{self.url}?type=cat&amp;q=ma",
                f"{self.url}?type=other&amp;q=ma",
                self.url,  # Clear
            ],
        )
        self.assertIn("All (4)", chips)
        self.assertIn("Dogs (2)", chips)
        self.assertIn("Cats (1)", chips)
        self.assertIn("Others (1)", chips)

    def test_the_selected_chip_has_aria_current_and_light_violet(self):
        for name, species in (("Rex", "Dog"), ("Max", "Dog"), ("Tom", "Cat")):
            _pet(self.owner, name, species)
        _pet(self.owner, "Kiko", "Rabbit")
        for query, label in (
            ("", "All"),
            ("?type=cat", "Cats"),
            ("?type=dog", "Dogs"),
        ):
            with self.subTest(query=query):
                chips = self._chips(self._html(query))
                self.assertEqual(chips.count('aria-current="true"'), 1)
                current = re.search(
                    r'<a [^>]*aria-current="true"[^>]*>([^<]*)</a>', chips
                )
                self.assertTrue(current.group(1).startswith(label))
                self.assertIn("bg-violet-100", current.group(0))
                self.assertIn("text-violet-800", current.group(0))
        # Every chip is a 44 px link
        for tag in re.findall(r"<a [^>]*>", self._chips(self._html())):
            self.assertIn("min-h-11", tag)

    def test_clear_only_shows_while_filtering(self):
        self._pets("A", "B", "C", "D")
        self.assertNotIn(">Clear</a>", self._html())
        for query in ("?q=a", "?type=dog"):
            with self.subTest(query=query):
                chips = self._chips(self._html(query))
                clear = re.search(r'<a href="([^"]+)" class="([^"]*)">Clear<', chips)
                self.assertIsNotNone(clear)
                self.assertEqual(clear.group(1), self.url)
                self.assertIn("min-h-11", clear.group(2))

    def test_cards_link_to_the_pet_page_in_two_columns(self):
        pets = self._pets("Bantay", "Choco", "Mingming")
        html = self._html()
        self.assertIn('<ul class="grid grid-cols-2 gap-3">', html)
        for pet in pets:
            with self.subTest(pet=pet.name):
                detail = reverse("owner_pet_detail", args=[pet.pk])
                self.assertEqual(html.count(f'<a href="{detail}"'), 1)
                self.assertIn("text-lg font-semibold", self._card(html, pet))
                self.assertIn(pet.name, self._card(html, pet))
        self.assertLess(html.index("Bantay"), html.index("Choco"))
        self.assertLess(html.index("Choco"), html.index("Mingming"))

    def test_species_and_breed_line_leaves_out_empty_parts(self):
        both = _pet(self.owner, "Both", "Dog", "Aspin")
        species = _pet(self.owner, "Species", "Rabbit")
        breed = _pet(self.owner, "Breed", "", "Aspin")
        html = self._html()
        self.assertIn("Dog · Aspin</span>", self._card(html, both))
        card = self._card(html, species)
        self.assertIn(">Rabbit</span>", card)
        self.assertNotIn("·", card)
        card = self._card(html, breed)
        self.assertIn(">Aspin</span>", card)
        self.assertNotIn("·", card)

    def test_photo_or_the_species_icon_on_a_light_violet_tile(self):
        photo = _pet(self.owner, "Bantay", "Dog", photo="pets/bantay.jpg")
        cat = _pet(self.owner, "Tom", "cat")
        rabbit = _pet(self.owner, "Kiko", "Rabbit")
        blank = _pet(self.owner, "Blank", "")
        html = self._html()
        card = self._card(html, photo)
        self.assertRegex(
            card,
            r'<img src="[^"]*pets/bantay\.jpg" alt="Photo of Bantay" loading="lazy"',
        )
        self.assertNotIn('role="img"', card)
        for pet, shape in (
            (cat, "M12 5c.67 0 1.35.09 2 .26"),
            (rabbit, "M13 16a3 3 0 0 1 2.24 5"),
            (blank, '<circle cx="11" cy="4" r="2"'),
        ):
            with self.subTest(pet=pet.name):
                card = self._card(html, pet)
                self.assertIn("bg-violet-50 text-violet-600", card)
                self.assertIn(f'aria-label="No photo of {pet.name} yet"', card)
                self.assertIn(shape, card)
                self.assertNotIn("<img", card)

    def test_removal_tag_only_while_a_request_is_waiting(self):
        waiting, plain, taken_back = self._pets("Waiting", "Plain", "TakenBack")
        _request(waiting, PetDeletionRequest.PENDING)
        _request(taken_back, PetDeletionRequest.CANCELLED)
        html = self._html()
        tag = (
            '<span class="mt-2 inline-flex rounded-full bg-yellow-100 px-2.5 py-1 '
            'text-sm font-medium text-yellow-800">Removal requested</span>'
        )
        self.assertIn(tag, self._card(html, waiting))
        self.assertNotIn("Removal requested", self._card(html, plain))
        self.assertNotIn("Removal requested", self._card(html, taken_back))
        self.assertEqual(html.count("Removal requested"), 1)
        self.assertNotIn("Pending Deletion", html)

    def test_one_pet_is_still_the_grid(self):
        (bantay,) = self._pets("Bantay")
        html = self._html()
        self.assertIn('<ul class="grid grid-cols-2 gap-3">', html)
        self.assertIn("Bantay", self._card(html, bantay))
        self.assertNotIn('role="search"', html)

    def test_no_results_says_what_was_searched_and_clears(self):
        self._pets("A", "B", "C", "D")
        html = self._html("?q=zzz")
        self.assertIn('No pet matches "zzz"', html)
        self.assertIn('role="status"', html)
        clear = f'<a href="{self.url}" class="btn btn-outline mt-4">Clear</a>'
        self.assertIn(clear, html)
        self.assertNotIn("grid-cols-2", html)
        self.assertIn("4 pets", html)  # the header and its Add a pet stay
        html = self._html("?type=cat")
        self.assertIn("No pet matches this filter", html)
        self.assertIn("Clear the filter to see all your pets.", html)

    def test_no_pets_is_the_empty_state_with_add_a_pet(self):
        html = self._html()
        self.assertIn("added a pet yet", html)
        self.assertIn("Add your pet so you can book visits", html)
        found = re.findall(VIOLET_BUTTON, html)
        self.assertEqual(len(found), 1)
        self.assertIn(f'href="{reverse("owner_pet_add")}"', found[0])
        self.assertIn("Add a pet</a>", html)
        self.assertNotIn('role="search"', html)
        self.assertNotIn("grid-cols-2", html)
        # Filtering words do not turn it into "no results"
        self.assertNotIn("No pet matches", self._html("?q=zzz"))

    def test_only_the_owners_own_active_pets_are_shown(self):
        self._pets("Mine")
        _pet(self.owner, "Gone", is_archived=True)
        _pet(_owner(first="Ben", last="Reyes"), "Theirs")
        html = self._html()
        self.assertIn("Mine", html)
        self.assertNotIn("Gone", html)
        self.assertNotIn("Theirs", html)
        self.assertIn("1 pet</p>", html)

    def test_page_queries_are_flat(self):
        def count():
            with CaptureQueriesContext(connection) as queries:
                self.assertEqual(self.client.get(self.url).status_code, 200)
            return len(queries)

        for number in range(4):
            _request(_pet(self.owner, f"Few{number}", "Dog"))
        few = count()
        for number in range(26):
            _request(_pet(self.owner, f"Many{number}", "Cat"))
        self.assertEqual(count(), few)
        # (printed so the page total can be written in HANDOFF)
        print(f"\nowner My Pets page queries: {few}")

    def test_no_inline_script_handler_or_one_line_break_in_a_link(self):
        _request(_pet(self.owner, "Bantay"))
        self._pets("B", "C", "D")
        for query in ("", "?q=zzz", "?type=dog"):
            with self.subTest(query=query):
                html = self._html(query)
                self.assertEqual(re.findall(INLINE_SCRIPT, html), [])
                self.assertIsNone(re.search(r"\son[a-z]+\s*=", html))
                self.assertNotIn("hx-confirm", html)
                for tag in re.findall(r"<a\b[^>]*>", html):
                    self.assertNotIn("\n", tag)
        for name in self.PARTS:
            source = engines["django"].get_template(name).template.source
            with self.subTest(file=name):
                self.assertNotIn("<!--", source)
                for link in re.findall(r"(?s)<a\b.*?</a>", source):
                    self.assertNotIn("\n", link)


# ── Slice 8b-5: owner pet profile, edit and remove ───────────────────────────

PROFILE_TODAY = date(2026, 11, 3)
# 9:00 AM in Manila on Tue, Nov 3, 2026 (the clinic's default slot is 60 min)
PROFILE_NOW = datetime(2026, 11, 3, 9, 0, tzinfo=timezone.get_fixed_timezone(480))


def _dose(pet, due_days=None, given_days=-300, **fields):
    """A vaccination given given_days from PROFILE_TODAY, next due due_days
    from it (None: no next date)."""
    fields.setdefault("vaccine_name", Vaccination.ANTI_RABIES)
    due = None if due_days is None else PROFILE_TODAY + timedelta(days=due_days)
    return Vaccination.objects.create(
        pet=pet,
        date_administered=PROFILE_TODAY + timedelta(days=given_days),
        next_due_date=due,
        **fields,
    )


def _profile_record(pet, days=-5, **fields):
    return MedicalRecord.objects.create(
        pet=pet, record_date=PROFILE_TODAY + timedelta(days=days), **fields
    )


def _sheet_form(sheet):
    """(action URL, inner HTML) of the POST form inside a bottom sheet."""
    found = re.search(r'(?s)<form method="post" action="([^"]+)"(.*?)</form>', sheet)
    return found.group(1), found.group(2)


def _profile_visit(pet, days=1, hour=10, status=Appointment.CONFIRMED, **fields):
    return Appointment.objects.create(
        owner=pet.owner,
        pet=pet,
        date=PROFILE_TODAY + timedelta(days=days),
        time=time(hour, 0),
        status=status,
        **fields,
    )


class OwnerPetProfileContextTests(TestCase):
    """owner_pet_profile_context: tabs, next visit, due note, records."""

    def setUp(self):
        self.owner = _owner(first="Ana", last="Cruz", email="ana@example.com")
        self.pet = _pet(self.owner, "Bantay", "Dog", "Aspin")

    def _ctx(self, query="", pet=None, owner=None):
        return owner_pet_profile_context(
            owner or self.owner,
            pet or self.pet,
            QueryDict(query),
            PROFILE_TODAY,
            PROFILE_NOW,
        )

    def _count(self, query):
        with CaptureQueriesContext(connection) as queries:
            self._ctx(query)
        return len(queries)

    # ── Tabs ────────────────────────────────────────────────────────────────

    def test_tab_parsing_unknown_and_missing_fall_back_to_about(self):
        cases = (
            ("", "about"),
            ("tab=about", "about"),
            ("tab=vaccines", "vaccines"),
            ("tab=records", "records"),
            ("tab=nonsense", "about"),
            ("tab=", "about"),
            ("tab=VACCINES", "about"),
        )
        for query, expected in cases:
            with self.subTest(query=query):
                self.assertEqual(self._ctx(query)["tab"], expected)

    def test_tab_links_and_only_the_open_panel_is_built(self):
        about = self._ctx()
        base = reverse("owner_pet_detail", args=[self.pet.pk])
        self.assertEqual(
            [(t["key"], t["url"]) for t in about["tabs"]],
            [
                ("about", f"{base}?tab=about"),
                ("vaccines", f"{base}?tab=vaccines"),
                ("records", f"{base}?tab=records"),
            ],
        )
        self.assertIn("details", about)
        self.assertNotIn("vaccines", about)
        self.assertNotIn("records", about)
        vaccines = self._ctx("tab=vaccines")
        self.assertIn("vaccines", vaccines)
        self.assertNotIn("details", vaccines)
        self.assertNotIn("records", vaccines)
        records = self._ctx("tab=records")
        self.assertIn("records", records)
        self.assertNotIn("vaccines", records)

    def test_top_of_the_page(self):
        pet = _pet(
            self.owner,
            "Mingming",
            "Cat",
            "",
            gender=Pet.FEMALE,
            date_of_birth=date(2025, 3, 3),
            weight=Decimal("4.50"),
        )
        context = self._ctx(pet=pet)
        self.assertEqual(context["kind_line"], "Cat")
        self.assertEqual(context["facts"], ["Female", "1 year old", "4.5 kg"])
        bare = self._ctx()
        self.assertEqual(bare["kind_line"], "Dog · Aspin")
        self.assertEqual(bare["facts"], [])

    def test_age_wording(self):
        from pets.utils import pet_age_chip, pet_age_phrase

        cases = (
            (date(2022, 6, 15), "4 years old", "4 years"),
            (date(2025, 11, 3), "1 year old", "1 year"),
            (date(2026, 3, 3), "8 months old", "8 months"),
            (date(2026, 10, 3), "1 month old", "1 month"),
            (date(2026, 10, 4), "Under 1 month old", "under 1 month"),
            (date(2026, 11, 3), "Under 1 month old", "under 1 month"),
            (None, "", ""),
        )
        for born, chip, phrase in cases:
            with self.subTest(born=born):
                self.assertEqual(pet_age_chip(born, PROFILE_TODAY), chip)
                self.assertEqual(pet_age_phrase(born, PROFILE_TODAY), phrase)

    def test_details_rows(self):
        pet = _pet(
            self.owner,
            "Max",
            "Dog",
            "Aspin",
            color="Brown",
            gender=Pet.MALE,
            date_of_birth=date(2022, 6, 15),
            weight=Decimal("12.50"),
        )
        rows = dict(self._ctx(pet=pet)["details"])
        self.assertEqual(rows["Species"], "Dog")
        self.assertEqual(rows["Male or female"], "Male")
        self.assertEqual(rows["Birthday"], "Jun 15, 2022 (4 years)")
        self.assertEqual(rows["Weight"], "12.5 kg")
        self.assertIsNone(rows["Registration no."])

    # ── Next visit ──────────────────────────────────────────────────────────

    def test_next_visit_is_the_soonest_pending_or_confirmed(self):
        _profile_visit(self.pet, days=5)
        soon = _profile_visit(self.pet, days=2, status=Appointment.PENDING)
        _profile_visit(self.pet, days=1, status=Appointment.CANCELLED)
        _profile_visit(self.pet, days=0, hour=8, status=Appointment.COMPLETED)
        self.assertEqual(self._ctx()["next_visit"], soon)

    def test_a_slot_that_just_ended_is_not_next_but_one_going_is(self):
        _profile_visit(self.pet, days=0, hour=8)  # 8:00 to 9:00, ended
        self.assertIsNone(self._ctx()["next_visit"])
        going = _profile_visit(self.pet, days=0, hour=9)
        self.assertEqual(self._ctx()["next_visit"], going)

    def test_another_pets_visit_is_not_this_pets(self):
        other = _pet(self.owner, "Rex")
        _profile_visit(other, days=1)
        self.assertIsNone(self._ctx()["next_visit"])

    # ── Due note ────────────────────────────────────────────────────────────

    def _note(self):
        return self._ctx("tab=vaccines")["due_note"]

    def test_due_note_today_in_n_days_and_overdue(self):
        protect = "Book a visit so Bantay stays protected."
        dose = _dose(self.pet, due_days=0)
        self.assertEqual(
            self._note(),
            {
                "title": "Anti-rabies is due on Nov 3",
                "text": f"That's today. {protect}",
            },
        )
        dose.next_due_date = PROFILE_TODAY + timedelta(days=7)
        dose.save()
        self.assertEqual(
            self._note(),
            {
                "title": "Anti-rabies is due on Nov 10",
                "text": f"That's in 7 days. {protect}",
            },
        )
        dose.next_due_date = PROFILE_TODAY + timedelta(days=1)
        dose.save()
        self.assertEqual(self._note()["text"], f"That's in 1 day. {protect}")
        dose.next_due_date = PROFILE_TODAY - timedelta(days=2)
        dose.save()
        self.assertEqual(
            self._note(),
            {"title": "Anti-rabies was due on Nov 1", "text": protect},
        )

    def test_no_due_note_outside_the_shared_rule(self):
        self.assertIsNone(self._note())
        _dose(self.pet, due_days=None)
        _dose(self.pet, due_days=8, vaccine_name=Vaccination.DEWORMING)
        self.assertIsNone(self._note())

    def test_a_dose_with_a_later_dose_is_not_due(self):
        old = _dose(self.pet, due_days=-3, given_days=-400)
        _dose(self.pet, due_days=300, given_days=-5)
        self.assertIsNone(self._note())
        rows = {row["given"]: row for row in self._ctx("tab=vaccines")["vaccines"]}
        self.assertFalse(rows[old.date_administered]["is_due"])

    def test_another_pets_dose_is_not_this_pets_note(self):
        _dose(_pet(self.owner, "Rex"), due_days=2)
        self.assertIsNone(self._note())

    def test_history_is_newest_given_first_and_flags_corrected_only(self):
        _dose(self.pet, given_days=-200, vaccine_name=Vaccination.DEWORMING)
        _dose(
            self.pet,
            given_days=-30,
            is_corrected=True,
            correction_note="Dose was wrong, fixed by Dr. Aga",
        )
        rows = self._ctx("tab=vaccines")["vaccines"]
        self.assertEqual([row["name"] for row in rows], ["Anti-rabies", "Deworming"])
        self.assertEqual([row["is_corrected"] for row in rows], [True, False])
        for row in rows:
            self.assertNotIn("note", " ".join(row))
            self.assertNotIn("Dr. Aga", str(row))

    # ── Records ─────────────────────────────────────────────────────────────

    def test_records_newest_first_with_public_fields_only(self):
        _profile_record(self.pet, days=-20, diagnosis="Old cough")
        newer = _profile_record(
            self.pet,
            days=-2,
            diagnosis="Skin allergy",
            symptoms="Itchy paws",
            public_notes="Use the cream",
            private_notes="Owner was rude",
            follow_up_required=True,
            follow_up_date=date(2026, 11, 15),
        )
        TestResultFile.objects.create(record=newer, file="test_results/a.pdf")
        TestResultFile.objects.create(
            record=newer, file="test_results/b.pdf", is_archived=True
        )
        rows = self._ctx("tab=records")["records"]
        headings = [row["heading"] for row in rows]
        self.assertEqual(headings, ["Skin allergy", "Old cough"])
        first = rows[0]
        self.assertEqual(first["line"], "Itchy paws")
        self.assertEqual(first["follow_up"], date(2026, 11, 15))
        self.assertEqual(first["files"], 1)
        self.assertEqual(rows[1]["files"], 0)
        self.assertIsNone(rows[1]["follow_up"])
        self.assertNotIn("Owner was rude", str(rows))

    def test_record_heading_kind_and_truncated_line(self):
        checkup = Service.objects.create(service_name="Check-up", status=Service.ACTIVE)
        visit = _profile_visit(self.pet, days=-5, status=Appointment.COMPLETED)
        visit.service = checkup
        visit.save()
        _profile_record(self.pet, appointment=visit, treatment_given="x" * 300)
        _profile_record(self.pet, days=-9, follow_up_date=date(2026, 12, 1))
        rows = self._ctx("tab=records")["records"]
        self.assertEqual(rows[0]["kind"], "Check-up")
        self.assertEqual(rows[0]["heading"], "Visit record")
        self.assertLessEqual(len(rows[0]["line"]), 90)
        self.assertTrue(rows[0]["line"].endswith("…"))
        self.assertEqual(rows[1]["kind"], "Visit")
        # A follow-up date with the flag off is not a follow-up
        self.assertIsNone(rows[1]["follow_up"])

    # ── Removal pending ─────────────────────────────────────────────────────

    def test_pending_flag_and_the_reason_in_plain_words(self):
        self.assertIsNone(self._ctx()["pending"])
        waiting = _request(self.pet)
        cases = (
            (
                PetDeletionRequest.PASSED_AWAY,
                "You said Bantay has passed away. The clinic will review it soon.",
            ),
            (
                PetDeletionRequest.REHOMED,
                "You said Bantay has a new home. The clinic will review it soon.",
            ),
            (
                PetDeletionRequest.DUPLICATE,
                "You said Bantay was added twice. The clinic will review it soon.",
            ),
            (PetDeletionRequest.OTHER, "The clinic will review your request soon."),
        )
        for reason, line in cases:
            with self.subTest(reason=reason):
                waiting.reason = reason
                waiting.save()
                context = self._ctx()
                self.assertEqual(context["pending"], waiting)
                self.assertEqual(context["pending_line"], line)

    def test_a_taken_back_or_answered_request_is_not_pending(self):
        for status in (
            PetDeletionRequest.CANCELLED,
            PetDeletionRequest.REJECTED,
            PetDeletionRequest.APPROVED,
        ):
            with self.subTest(status=status):
                PetDeletionRequest.objects.all().delete()
                _request(self.pet, status)
                self.assertIsNone(self._ctx()["pending"])

    # ── Ownership ───────────────────────────────────────────────────────────

    def test_another_owners_pet_and_an_archived_pet_are_a_404(self):
        other = _owner(first="Ben", last="Reyes", email="ben@example.com")
        theirs = _pet(other, "Rex")
        with self.assertRaises(Http404):
            self._ctx(pet=theirs)
        gone = _pet(self.owner, "Gone", is_archived=True)
        with self.assertRaises(Http404):
            self._ctx(pet=gone)

    # ── Queries ─────────────────────────────────────────────────────────────

    def test_queries_per_tab_are_flat(self):
        _profile_visit(self.pet, days=1)
        for number in range(3):
            _dose(self.pet, given_days=-10 - number, due_days=number)
            record = _profile_record(self.pet, days=-10 - number, diagnosis="D")
            TestResultFile.objects.create(record=record, file="test_results/a.pdf")
        tabs = ("about", "vaccines", "records")
        few = {tab: self._count(f"tab={tab}") for tab in tabs}
        for number in range(27):
            _dose(self.pet, given_days=-50 - number, due_days=number)
            record = _profile_record(self.pet, days=-50 - number, diagnosis="D")
            TestResultFile.objects.create(record=record, file="test_results/a.pdf")
        many = {tab: self._count(f"tab={tab}") for tab in tabs}
        self.assertEqual(few, many)
        # The removal request, plus: About 2 (settings, visit), Vaccines 1, Records 1
        self.assertEqual(many, {"about": 3, "vaccines": 2, "records": 2})


@patch("django.utils.timezone.now", return_value=PROFILE_NOW)
class OwnerPetProfilePageTests(TestCase):
    """owner_pet_detail on layouts/owner.html, Pets tab, back bar, three tabs,
    the remove sheet and the take-back sheet."""

    def setUp(self):
        self.owner = _login_owner(self)
        self.owner.user.is_onboarded = True
        self.owner.user.save()
        self.pet = _pet(self.owner, "Bantay", "Dog", "Aspin")
        self.url = reverse("owner_pet_detail", args=[self.pet.pk])

    def _html(self, query="", pet=None):
        url = reverse("owner_pet_detail", args=[(pet or self.pet).pk])
        response = self.client.get(f"{url}{query}")
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def _main(self, html):
        return re.search(r"(?s)<main\b.*?</main>", html).group(0)

    def _flash(self, response):
        return [(m.level_tag, str(m)) for m in get_messages(response.wsgi_request)]

    def test_layout_pets_tab_and_back_bar(self, _now):
        response = self.client.get(self.url)
        self.assertTemplateUsed(response, "layouts/owner.html")
        self.assertTemplateUsed(response, "owner/pets/detail.html")
        self.assertTemplateNotUsed(response, "base_dashboard.html")
        html = response.content.decode()
        nav = re.search(r'(?s)<nav aria-label="Main"[^>]*>(.*?)</nav>', html).group(1)
        self.assertEqual(nav.count('aria-current="page"'), 1)
        current = re.search(r'<a href="([^"]+)"[^>]*aria-current="page"', nav)
        self.assertEqual(current.group(1), reverse("owner_pet_list"))
        self.assertIn(f'<a href="{reverse("owner_pet_list")}"', html)
        self.assertIn("Back to My Pets", html)
        self.assertIn("Bantay", html)

    def test_one_violet_button_the_book_link_and_none_while_pending(self, _now):
        book = f"{reverse('owner_book_appointment')}?pet={self.pet.pk}"
        for query in ("", "?tab=vaccines", "?tab=records"):
            with self.subTest(query=query):
                found = re.findall(VIOLET_BUTTON, self._html(query))
                self.assertEqual(len(found), 1)
                self.assertIn(f'href="{book}"', found[0])
        self.assertIn("Book a visit for Bantay</a>", self._html())
        _request(self.pet)
        for query in ("", "?tab=vaccines", "?tab=records"):
            with self.subTest(pending=query):
                html = self._html(query)
                self.assertEqual(re.findall(VIOLET_BUTTON, html), [])
                self.assertNotIn("Book a visit for", html)

    def test_each_tab_has_one_aria_current_link(self, _now):
        cases = (
            ("", "about"),
            ("?tab=vaccines", "vaccines"),
            ("?tab=records", "records"),
            ("?tab=zzz", "about"),
        )
        for query, key in cases:
            with self.subTest(query=query):
                html = self._html(query)
                tabs = re.search(
                    r'(?s)<nav aria-label="About this pet"[^>]*>(.*?)</nav>', html
                ).group(1)
                self.assertEqual(tabs.count('aria-current="page"'), 1)
                current = re.search(r'<a href="([^"]+)"[^>]*aria-current="page"', tabs)
                self.assertTrue(current.group(1).endswith(f"?tab={key}"))
                self.assertEqual(len(re.findall(r"<a\b", tabs)), 3)
                for tag in re.findall(r"<a\b[^>]*>", tabs):
                    self.assertIn("min-h-11", tag)

    def test_about_details_edit_and_remove(self, _now):
        html = self._html()
        edit = reverse("owner_pet_edit", args=[self.pet.pk])
        self.assertIn(f'<a href="{edit}"', html)
        self.assertIn('<span class="font-normal text-gray-500">None</span>', html)
        self.assertIn("Registration no.", html)
        self.assertIn('data-dialog-open="remove-sheet"', html)
        self.assertIn("Remove Bantay from my pets", html)
        # Only the open panel: nothing from the other two
        self.assertNotIn("Vaccine history", html)
        self.assertNotIn("No health records yet", html)

    def test_next_visit_card_blue_or_yellow_with_one_link(self, _now):
        self.assertNotIn("Next visit", self._html())
        visit = _profile_visit(self.pet, days=1, hour=14)
        html = self._html()
        link = reverse("owner_appointment_detail", args=[visit.pk])
        self.assertEqual(html.count(f'<a href="{link}"'), 1)
        card = re.search(rf'(?s)<a href="{link}".*?</a>', html).group(0)
        self.assertIn("border-blue-200 bg-blue-50", card)
        self.assertIn("2:00 PM · Confirmed", card)
        self.assertIn(">Nov</span>", card)
        visit.status = Appointment.PENDING
        visit.save()
        card = re.search(rf'(?s)<a href="{link}".*?</a>', self._html()).group(0)
        self.assertIn("border-yellow-300 bg-yellow-50", card)
        self.assertIn("Waiting for approval", card)

    def test_empty_states(self, _now):
        self.assertIn("No vaccines yet", self._html("?tab=vaccines"))
        self.assertIn(
            "The clinic adds them here after each shot.", self._html("?tab=vaccines")
        )
        html = self._html("?tab=records")
        self.assertIn("No health records yet", html)
        self.assertIn("The clinic adds one after each visit.", html)

    def test_vaccines_due_note_history_and_corrected_words_only(self, _now):
        _dose(
            self.pet,
            due_days=7,
            given_days=-358,
            is_corrected=True,
            correction_note="Secret clinic note about the date",
        )
        _dose(self.pet, 200, -20, vaccine_name=Vaccination.DEWORMING)
        html = self._html("?tab=vaccines")
        self.assertIn("Anti-rabies is due on Nov 10", html)
        self.assertIn(
            "That&#x27;s in 7 days. Book a visit so Bantay stays protected.", html
        )
        self.assertIn("border-orange-300 bg-orange-50", html)
        self.assertIn("Vaccine history", html)
        self.assertLess(html.index("Deworming"), html.index("Given Nov 10, 2025"))
        self.assertIn("Next: Nov 10, 2026", html)
        self.assertIn("bg-orange-100 text-orange-800", html)  # due
        self.assertIn("bg-gray-100 text-gray-800", html)  # not due
        self.assertIn("Corrected by the clinic", html)
        self.assertEqual(html.count("Corrected by the clinic"), 1)
        self.assertNotIn("Secret clinic note", html)

    def test_records_cards_follow_up_files_and_private_notes(self, _now):
        record = _profile_record(
            self.pet,
            days=-3,
            diagnosis="Mild skin allergy",
            treatment_given="Cream and a tablet",
            private_notes="Owner seemed upset",
            follow_up_required=True,
            follow_up_date=date(2026, 11, 15),
        )
        TestResultFile.objects.create(record=record, file="test_results/a.pdf")
        TestResultFile.objects.create(record=record, file="test_results/b.pdf")
        plain = _profile_record(self.pet, days=-20, diagnosis="Healthy check")
        html = self._html("?tab=records")
        self.assertIn("What Dr. Aga wrote after each visit.", html)
        link = reverse("owner_medical_record", args=[self.pet.pk, record.pk])
        self.assertEqual(html.count(f'<a href="{link}"'), 1)
        card = re.search(rf'(?s)<a href="{link}".*?</a>', html).group(0)
        for text in (
            "Oct 31, 2026 · Visit",
            "Mild skin allergy",
            "Cream and a tablet",
            "Follow-up around Nov 15",
            "2 test results",
        ):
            self.assertIn(text, card)
        self.assertIn("bg-blue-100", card)
        self.assertIn("text-blue-800", card)
        self.assertNotIn("Owner seemed upset", html)
        plain_link = reverse("owner_medical_record", args=[self.pet.pk, plain.pk])
        other = re.search(rf'(?s)<a href="{plain_link}".*?</a>', html).group(0)
        self.assertNotIn("Follow-up around", other)
        self.assertNotIn("test result", other)
        self.assertLess(html.index("Mild skin allergy"), html.index("Healthy check"))
        # Text only: no file link of any kind
        self.assertNotIn("test_results/", html)

    def test_remove_sheet_has_four_reasons_and_a_post_form(self, _now):
        html = self._html()
        sheet = re.search(r'(?s)<dialog id="remove-sheet".*?</dialog>', html).group(0)
        self.assertIn("Remove Bantay from your pets?", sheet)
        self.assertIn(
            "The clinic will check your request first. "
            "Bantay&#x27;s health records are kept safe either way.",
            sheet,
        )
        self.assertIn("<legend", sheet)
        self.assertIn("What happened?", sheet)
        values = re.findall(r'<input type="radio" name="reason" value="([^"]+)"', sheet)
        self.assertEqual(values, ["passed_away", "rehomed", "duplicate", "other"])
        for words in (
            "My pet has passed away",
            "My pet has a new home",
            "I added this pet twice",
            "Other reason",
        ):
            self.assertIn(words, sheet)
        self.assertEqual(sheet.count("has-[:checked]:bg-violet-50"), 4)
        self.assertEqual(sheet.count("min-h-14"), 4 + 2)  # cards + the two buttons
        self.assertNotIn(" checked", sheet)
        action, inner = _sheet_form(sheet)
        self.assertEqual(
            action, reverse("owner_pet_delete_request", args=[self.pet.pk])
        )
        self.assertIn("csrfmiddlewaretoken", inner)
        self.assertIn("Send request", inner)
        self.assertIn("bg-red-600", inner)
        self.assertIn('name="reason"', inner)
        self.assertIn("Keep Bantay</button>", sheet)
        # Only on the About tab
        self.assertNotIn("remove-sheet", self._html("?tab=records"))

    def test_while_pending_no_remove_and_a_take_back_sheet(self, _now):
        _request(self.pet)
        html = self._html()
        self.assertNotIn("remove-sheet", html)
        self.assertNotIn("Remove Bantay from my pets", html)
        self.assertNotIn(reverse("owner_pet_edit", args=[self.pet.pk]), html)
        self.assertIn("Removal requested", html)
        self.assertIn("You said Bantay has passed away.", html)
        self.assertIn('data-dialog-open="take-back-sheet"', html)
        found = re.search(r'(?s)<dialog id="take-back-sheet".*?</dialog>', html)
        sheet = found.group(0)
        self.assertIn("Take back your request?", sheet)
        self.assertIn("Bantay stays on your account.", sheet)
        action, inner = _sheet_form(sheet)
        self.assertEqual(
            action, reverse("owner_pet_cancel_delete_request", args=[self.pet.pk])
        )
        self.assertIn("csrfmiddlewaretoken", inner)
        self.assertIn("Yes, take it back", inner)
        # Both buttons the same size
        self.assertEqual(sheet.count("min-h-14 w-full"), 2)
        self.assertIn("Close</button>", sheet)
        # The sheet is on every tab while a request is waiting
        self.assertIn("take-back-sheet", self._html("?tab=vaccines"))

    def test_send_without_a_reason_is_an_error_and_creates_nothing(self, _now):
        post = reverse("owner_pet_delete_request", args=[self.pet.pk])
        for data in ({}, {"reason": ""}, {"reason": "bored"}):
            with self.subTest(data=data):
                response = self.client.post(post, data)
                self.assertRedirects(response, self.url, fetch_redirect_response=False)
                # (earlier subtests' messages may still wait in the cookie)
                self.assertEqual(
                    self._flash(response)[-1:],
                    [("error", "Choose what happened, then send the request.")],
                )
                self.assertFalse(PetDeletionRequest.objects.exists())

    def test_send_with_a_reason_makes_a_pending_request(self, _now):
        post = reverse("owner_pet_delete_request", args=[self.pet.pk])
        response = self.client.post(post, {"reason": "rehomed"})
        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        waiting = PetDeletionRequest.objects.get()
        self.assertEqual(waiting.status, PetDeletionRequest.PENDING)
        self.assertEqual(waiting.reason, PetDeletionRequest.REHOMED)
        self.assertEqual(waiting.requested_by, self.owner.user)
        self.assertEqual(waiting.pet, self.pet)
        self.assertEqual(
            self._flash(response),
            [("success", "Request sent. The clinic will review it soon.")],
        )
        page = self.client.get(self.url)
        self.assertContains(page, "Request sent.")
        self.assertContains(page, "You said Bantay has a new home.")

    def test_a_second_request_while_one_waits_makes_no_second_row(self, _now):
        _request(self.pet)
        post = reverse("owner_pet_delete_request", args=[self.pet.pk])
        response = self.client.post(post, {"reason": "other"})
        self.assertEqual(PetDeletionRequest.objects.count(), 1)
        self.assertEqual(self._flash(response)[0][0], "warning")

    def test_a_get_to_the_request_url_changes_nothing(self, _now):
        post = reverse("owner_pet_delete_request", args=[self.pet.pk])
        response = self.client.get(post)
        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        self.assertFalse(PetDeletionRequest.objects.exists())

    def test_another_owners_pet_and_an_archived_pet_are_a_404(self, _now):
        other = _owner(first="Ben", last="Reyes", email="ben@example.com")
        theirs = _pet(other, "Rex")
        gone = _pet(self.owner, "Gone", is_archived=True)
        for pet in (theirs, gone):
            for tab in ("", "?tab=vaccines", "?tab=records"):
                with self.subTest(pet=pet.name, tab=tab):
                    url = reverse("owner_pet_detail", args=[pet.pk])
                    self.assertEqual(self.client.get(f"{url}{tab}").status_code, 404)
            post = reverse("owner_pet_delete_request", args=[pet.pk])
            response = self.client.post(post, {"reason": "other"})
            self.assertEqual(response.status_code, 404)
        self.assertFalse(PetDeletionRequest.objects.exists())

    def test_page_queries_are_flat_per_tab(self, _now):
        def count(query):
            # A request clears the query log, so start from an empty one
            reset_queries()
            with CaptureQueriesContext(connection) as queries:
                response = self.client.get(f"{self.url}{query}")
            total = len(queries)  # read now: the next request clears the log
            self.assertEqual(response.status_code, 200)
            return total

        tabs = ("", "?tab=vaccines", "?tab=records")
        for number in range(3):
            _dose(self.pet, given_days=-10 - number)
            _profile_record(self.pet, days=-10 - number)
        few = [count(query) for query in tabs]
        for number in range(27):
            _dose(self.pet, given_days=-50 - number)
            _profile_record(self.pet, days=-50 - number)
        self.assertEqual([count(query) for query in tabs], few)
        # (printed so the page totals can be written in HANDOFF)
        print(f"\nowner pet profile page queries (about, vaccines, records): {few}")

    def test_links_on_one_line_no_inline_script_or_handler(self, _now):
        _request(self.pet)
        record = _profile_record(self.pet, diagnosis="Cough")
        _dose(self.pet, due_days=3)
        _profile_visit(self.pet, days=1)
        for query in ("", "?tab=vaccines", "?tab=records"):
            with self.subTest(query=query):
                html = self._html(query)
                self.assertEqual(re.findall(INLINE_SCRIPT, html), [])
                self.assertIsNone(re.search(r"\son[a-z]+\s*=", html))
                self.assertNotIn("hx-confirm", html)
                self.assertNotIn("<!--", html)
                # Each opening tag is on one line (the icon's SVG inside an
                # anchor may span lines, as on My Pets)
                for tag in re.findall(r"<a\b[^>]*>", self._main(html)):
                    self.assertNotIn("\n", tag)
        self.assertIn(str(record.pk), self._html("?tab=records"))
        for name in (
            "owner/pets/detail.html",
            "owner/pet_profile_parts/top.html",
            "owner/pet_profile_parts/tabs.html",
            "owner/pet_profile_parts/about.html",
            "owner/pet_profile_parts/vaccines.html",
            "owner/pet_profile_parts/records.html",
            "owner/pet_profile_parts/removal_box.html",
            "owner/pet_profile_parts/remove_reasons.html",
            "owner/pet_edit_parts/photo_row.html",
            "owner/pet_edit_parts/change_card.html",
            "owner/pet_edit_parts/locked_card.html",
            "owner/pets/edit.html",
        ):
            source = engines["django"].get_template(name).template.source
            with self.subTest(file=name):
                self.assertNotIn("<!--", source)
                for link in re.findall(r"(?s)<a\b.*?</a>", source):
                    self.assertNotIn("\n", link)


class OwnerPetEditPageTests(TestCase):
    """owner_pet_edit on layouts/owner.html, Pets tab, back bar to the profile."""

    def setUp(self):
        self.owner = _login_owner(self)
        self.owner.user.is_onboarded = True
        self.owner.user.save()
        self.pet = _pet(
            self.owner,
            "Bantay",
            "Dog",
            "Aspin (Native)",
            color="Brown",
            gender=Pet.MALE,
            date_of_birth=date(2022, 6, 15),
            weight=Decimal("12.50"),
            registration_number="R-1",
        )
        self.url = reverse("owner_pet_edit", args=[self.pet.pk])
        self.profile = reverse("owner_pet_detail", args=[self.pet.pk])

    def _html(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def _flash(self, response):
        return [(m.level_tag, str(m)) for m in get_messages(response.wsgi_request)]

    def _clinic(self, phone):
        return ClinicSettings.objects.create(
            **{**CLINIC_SETTINGS_DEFAULTS, "contact_number": phone}
        )

    def test_layout_pets_tab_and_back_bar_to_the_profile(self):
        response = self.client.get(self.url)
        self.assertTemplateUsed(response, "layouts/owner.html")
        self.assertTemplateNotUsed(response, "base_dashboard.html")
        html = response.content.decode()
        self.assertIn(f'<a href="{self.profile}"', html)
        self.assertIn("Back to Bantay", html)
        nav = re.search(r'(?s)<nav aria-label="Main"[^>]*>(.*?)</nav>', html).group(1)
        current = re.search(r'<a href="([^"]+)"[^>]*aria-current="page"', nav)
        self.assertEqual(current.group(1), reverse("owner_pet_list"))

    def test_one_violet_save_button_for_the_form(self):
        found = re.findall(VIOLET_BUTTON, self._html())
        self.assertEqual(len(found), 1)
        self.assertIn('form="pet-edit-form"', found[0])
        self.assertIn("Save changes", self._html())
        self.assertIn('<form id="pet-edit-form" method="post"', self._html())

    def test_locked_list_with_lock_icons(self):
        html = self._html()
        card = re.search(
            r'(?s)<section[^>]*aria-labelledby="locked-title".*?</section>', html
        ).group(0)
        self.assertIn("Only the clinic can change these", card)
        for label in ("Species", "Male or female", "Birthday", "Registration no."):
            self.assertIn(f">{label}</dt>", card)
        self.assertEqual(card.count("<svg"), 4)
        self.assertIn("Jun 15, 2022", card)
        self.assertIn("R-1", card)
        self.assertNotIn("Weight", card)

    def test_phone_line_with_and_without_a_clinic_phone(self):
        none = "Something wrong here? Tell the clinic at your next visit."
        self.assertIn(none, self._html())  # no ClinicSettings row at all
        clinic = self._clinic("")
        self.assertIn(none, self._html())
        clinic.contact_number = "0954 288 9774"
        clinic.save()
        self.assertIn(
            "Something wrong here? Tell the clinic at your next visit "
            "or call 0954 288 9774.",
            self._html(),
        )

    def test_breed_options_come_from_the_pets_species(self):
        response = self.client.get(self.url)
        form = response.context["form"]
        dog = [value for value, _ in form.fields["breed_choice"].choices]
        self.assertIn("Labrador Retriever", dog)
        self.assertNotIn("Persian", dog)
        self.assertEqual(form["breed_choice"].value(), "Aspin (Native)")
        self.assertNotIn("species", form.fields)
        cat = _pet(self.owner, "Mingming", "Cat", "Persian")
        response = self.client.get(reverse("owner_pet_edit", args=[cat.pk]))
        field = response.context["form"].fields["breed_choice"]
        options = [value for value, _ in field.choices]
        self.assertIn("Persian", options)
        self.assertNotIn("Labrador Retriever", options)

    def test_a_species_outside_the_list_has_only_the_typed_breed(self):
        turtle = _pet(self.owner, "Shelly", "Turtle", "Red-eared")
        response = self.client.get(reverse("owner_pet_edit", args=[turtle.pk]))
        form = response.context["form"]
        self.assertNotIn("breed_choice", form.fields)
        self.assertContains(response, 'value="Red-eared"')
        self.assertContains(response, "Weight in kg")
        self.assertContains(response, "The clinic also weighs Shelly at each visit.")

    def test_optional_labels_and_weight_help(self):
        html = self._html()
        self.assertGreaterEqual(html.count("(optional)"), 3)
        self.assertIn("The clinic also weighs Bantay at each visit.", html)
        self.assertIn("You can change these", html)

    def test_save_goes_to_the_profile_with_the_flash_and_new_values(self):
        response = self.client.post(
            self.url,
            {
                "name": "Rex",
                "breed_choice": "Shih Tzu",
                "color": "White",
                "weight": "7.5",
            },
        )
        self.assertRedirects(response, self.profile, fetch_redirect_response=False)
        self.assertEqual(
            self._flash(response), [("success", "Rex's details were saved.")]
        )
        self.pet.refresh_from_db()
        self.assertEqual(
            (self.pet.name, self.pet.breed, self.pet.color, self.pet.weight),
            ("Rex", "Shih Tzu", "White", Decimal("7.50")),
        )
        page = self.client.get(self.profile)
        self.assertContains(page, "Rex&#x27;s details were saved.")

    def test_breed_other_keeps_the_typed_text_and_not_sure_clears_it(self):
        post = {"name": "Bantay", "breed_choice": "__other__", "breed": " Mix of two "}
        self.client.post(self.url, post)
        self.pet.refresh_from_db()
        self.assertEqual(self.pet.breed, "Mix of two")
        self.client.post(self.url, {"name": "Bantay", "breed_choice": ""})
        self.pet.refresh_from_db()
        self.assertEqual(self.pet.breed, "")

    def test_a_breed_of_another_species_is_refused(self):
        response = self.client.post(
            self.url, {"name": "Bantay", "breed_choice": "Persian"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("breed_choice", response.context["form"].errors)
        self.pet.refresh_from_db()
        self.assertEqual(self.pet.breed, "Aspin (Native)")

    def test_locked_fields_posted_anyway_change_nothing(self):
        self.client.post(
            self.url,
            {
                "name": "Rex",
                "species": "Cat",
                "species_choice": "Cat",
                "gender": "female",
                "date_of_birth": "2019-05-05",
                "registration_number": "HACKED",
            },
        )
        self.pet.refresh_from_db()
        self.assertEqual(self.pet.name, "Rex")
        self.assertEqual(self.pet.species, "Dog")
        self.assertEqual(self.pet.gender, Pet.MALE)
        self.assertEqual(self.pet.date_of_birth, date(2022, 6, 15))
        self.assertEqual(self.pet.registration_number, "R-1")

    def test_form_with_a_pet_adds_only_the_breed_list(self):
        form = OwnerPetEditForm(instance=self.pet)
        self.assertEqual(
            list(form.fields),
            ["name", "breed", "color", "weight", "photo", "breed_choice"],
        )

    def test_remove_photo_only_when_there_is_a_photo_and_it_clears_it(self):
        self.assertNotIn("Remove photo", self._html())
        Pet.objects.filter(pk=self.pet.pk).update(photo="pets/bantay.jpg")
        html = self._html()
        self.assertIn("Remove photo", html)
        self.assertIn('name="photo-clear"', html)
        self.assertIn("Change photo", html)
        self.client.post(self.url, {"name": "Bantay", "photo-clear": "on"})
        self.pet.refresh_from_db()
        self.assertFalse(self.pet.photo)

    def test_errors_use_the_summary_and_keep_what_was_typed(self):
        response = self.client.post(
            self.url, {"name": "  ", "color": "Typed color", "weight": "0"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Please fix the 2 items below.")
        self.assertIn("name", response.context["form"].errors)
        self.assertContains(response, "Weight must be greater than 0.")
        self.assertContains(response, 'value="Typed color"')
        # The back bar and the heading still show the saved name
        self.assertContains(response, "Back to Bantay")
        self.pet.refresh_from_db()
        self.assertEqual(self.pet.color, "Brown")

    def test_editing_while_a_removal_waits_goes_back_to_the_profile(self):
        _request(self.pet)
        response = self.client.get(self.url)
        self.assertRedirects(response, self.profile, fetch_redirect_response=False)
        self.assertEqual(self._flash(response)[0][0], "warning")
        response = self.client.post(self.url, {"name": "Changed"})
        self.assertRedirects(response, self.profile, fetch_redirect_response=False)
        self.pet.refresh_from_db()
        self.assertEqual(self.pet.name, "Bantay")

    def test_another_owners_and_archived_pets_are_a_404(self):
        other = _owner(first="Ben", last="Reyes", email="ben@example.com")
        theirs = _pet(other, "Rex")
        gone = _pet(self.owner, "Gone", is_archived=True)
        for pet in (theirs, gone):
            url = reverse("owner_pet_edit", args=[pet.pk])
            self.assertEqual(self.client.get(url).status_code, 404)

    def test_page_queries_are_flat(self):
        def count():
            # A request clears the query log, so start from an empty one
            reset_queries()
            with CaptureQueriesContext(connection) as queries:
                response = self.client.get(self.url)
            total = len(queries)
            self.assertEqual(response.status_code, 200)
            return total

        few = count()
        for number in range(25):
            _dose(self.pet, given_days=-10 - number)
            _profile_record(self.pet, days=-10 - number)
        self.assertEqual(count(), few)
        # (printed so the page total can be written in HANDOFF)
        print(f"\nowner pet edit page queries: {few}")
