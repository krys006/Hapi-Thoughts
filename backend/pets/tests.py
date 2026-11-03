import re
from datetime import datetime, time, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from allauth.socialaccount.models import SocialAccount
from django.conf import settings
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
from dashboard.templatetags.friendly_dates import friendly_date
from medical.models import MedicalRecord, Vaccination
from notifications.models import Notification
from pets.forms import AdminOwnerEmailForm, AdminPetForm, AdminPetOwnerForm
from pets.models import Pet, PetDeletionRequest, PetOwner
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
                self.assertContains(response, "js/admin/owner_form")

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
        self.assertEqual(answer.title, "Bantay was removed")
        self.assertEqual(
            answer.message,
            "The clinic removed Bantay from your account, as you asked.",
        )

    def test_keep_notification_links_the_pet(self):
        self._post(approve=False)
        answer = self._answers().get()
        self.assertEqual(answer.recipient, self.owner.user)
        self.assertEqual(answer.related_pet, self.pet)
        self.assertEqual(answer.title, "Bantay stays on your account")
        self.assertEqual(
            answer.message,
            "The clinic kept Bantay on your account. If you still want it "
            "removed, please contact the clinic.",
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
        self.assertEqual(answer.title, "Bantay was removed")
        self.assertEqual(
            answer.message,
            "The clinic removed Bantay from your account (reason: duplicate entry).",
        )

    def test_email_when_the_owner_has_one(self):
        self._post()
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["ana@example.com"])
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
        form_tag = re.search(r"<form\b[^>]*>", response.content.decode()).group()
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
        edit = reverse("admin_vaccination_edit", args=[other.pk])
        self.assertIn(f'<a href="{edit}" ', card)
        history = reverse("admin_pet_vaccination_history", args=[self.pet.pk])
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
                self.assertIn("js/admin/photo_input", html)
                self.assertIn("js/admin/pet_form", html)
                self.assertRegex(html, r"<form [^>]*novalidate")
                self.assertRegex(html, r'<form [^>]*enctype="multipart/form-data"')
                self.assertIn("csrfmiddlewaretoken", html)

    def test_owner_form_loads_the_shared_photo_script(self):
        html = self._html(reverse("admin_owner_edit", args=[self.owner.pk]))
        self.assertIn("js/admin/photo_input", html)
        self.assertIn("js/admin/owner_form", html)

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
