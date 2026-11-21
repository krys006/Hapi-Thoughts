import datetime
import re
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock

from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.models import SocialAccount, SocialLogin
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.contrib.messages import constants as message_levels
from django.contrib.messages import get_messages
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.middleware import SessionMiddleware
from django.core import mail
from django.core.cache import cache
from django.db import connection
from django.template import Context, Template, engines
from django.template.loader import get_template
from django.test import (
    Client,
    RequestFactory,
    SimpleTestCase,
    TestCase,
    TransactionTestCase,
    override_settings,
)
from django.test.utils import CaptureQueriesContext
from django.urls import get_resolver, resolve, reverse
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from django.views.defaults import permission_denied, server_error

from accounts.adapters import (
    ADMIN_MESSAGE,
    INACTIVE_MESSAGE,
    MULTIPLE_MATCHES_MESSAGE,
    UNVERIFIED_MESSAGE,
    CustomSocialAccountAdapter,
)
from accounts.utils import owner_tab, verification_pending
from appointments.models import Appointment, ClinicSettings
from billing.models import Service
from notifications.models import Notification
from pets.models import Pet, PetOwner

User = get_user_model()


class GoogleSignInAdapterTests(TestCase):
    """CustomSocialAccountAdapter.pre_social_login, called directly."""

    def setUp(self):
        self.factory = RequestFactory()
        self.adapter = CustomSocialAccountAdapter()

    # ─── Helpers ──────────────────────────────────────────────────────────────

    def _request(self):
        """A request with a session and message storage, like a real one."""
        request = self.factory.get("/accounts/google/login/callback/")
        SessionMiddleware(lambda r: None).process_request(request)
        request.session.save()
        request._messages = FallbackStorage(request)
        return request

    def _messages(self, request):
        return [str(m) for m in get_messages(request)]

    def _user(self, username, email, role=User.PET_OWNER, is_active=True):
        return User.objects.create_user(
            username=username,
            email=email,
            password="unused-pass-123",
            role=role,
            is_active=is_active,
        )

    def _new_login(self, extra_data, uid="google-uid-1"):
        """Google account not linked yet: unsaved user, so is_existing is
        False (allauth checks user.pk exists in the DB)."""
        account = SocialAccount(provider="google", uid=uid, extra_data=extra_data)
        return SocialLogin(
            user=User(email=extra_data.get("email", "")), account=account
        )

    def _linked_login(self, user, uid="google-uid-1"):
        """Google account already linked: saved SocialAccount + saved user,
        which is what allauth's lookup() sets up, so is_existing is True."""
        extra_data = {"email": user.email, "email_verified": True}
        account = SocialAccount.objects.create(
            user=user, provider="google", uid=uid, extra_data=extra_data
        )
        return SocialLogin(user=user, account=account)

    def assertRefused(self, request, sociallogin, message):
        """Raises ImmediateHttpResponse to owner_login with the message,
        and creates no SocialAccount row."""
        before = SocialAccount.objects.count()
        with self.assertRaises(ImmediateHttpResponse) as ctx:
            self.adapter.pre_social_login(request, sociallogin)
        response = ctx.exception.response
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("owner_login"))
        self.assertIn(message, self._messages(request))
        self.assertEqual(SocialAccount.objects.count(), before)

    # ─── Not linked yet ───────────────────────────────────────────────────────

    def test_unverified_email_refused_without_leaking_admin(self):
        """False, missing and "false" all count as unverified; an admin
        match still gets only the unverified message."""
        self._user("admin1", "admin@example.com", role=User.ADMIN)
        cases = {
            "False": {"email": "admin@example.com", "email_verified": False},
            "missing": {"email": "admin@example.com"},
            '"false"': {"email": "admin@example.com", "email_verified": "false"},
        }
        for label, extra_data in cases.items():
            with self.subTest(email_verified=label):
                request = self._request()
                self.assertRefused(
                    request, self._new_login(extra_data), UNVERIFIED_MESSAGE
                )
                self.assertNotIn(ADMIN_MESSAGE, self._messages(request))

    def test_true_and_string_true_count_as_verified(self):
        """True and "true" both let a matching owner be linked."""
        owner = self._user("owner1", "owner@example.com")
        for i, value in enumerate([True, "true"]):
            with self.subTest(email_verified=value):
                uid = f"google-uid-{i}"
                extra_data = {"email": "owner@example.com", "email_verified": value}
                self.adapter.pre_social_login(
                    self._request(), self._new_login(extra_data, uid=uid)
                )
                self.assertTrue(
                    SocialAccount.objects.filter(user=owner, uid=uid).exists()
                )

    def test_verified_no_match_allows_signup(self):
        """No matching user: no exception, nothing linked (allauth signs up)."""
        request = self._request()
        extra_data = {"email": "new@example.com", "email_verified": True}
        self.adapter.pre_social_login(request, self._new_login(extra_data))
        self.assertEqual(SocialAccount.objects.count(), 0)
        self.assertEqual(self._messages(request), [])

    def test_verified_match_ignores_case_and_links(self):
        """Email matching is case-insensitive; the active owner gets linked."""
        owner = self._user("owner1", "owner@example.com")
        extra_data = {"email": "Owner@Example.COM", "email_verified": True}
        self.adapter.pre_social_login(self._request(), self._new_login(extra_data))
        self.assertTrue(
            SocialAccount.objects.filter(
                user=owner, provider="google", uid="google-uid-1"
            ).exists()
        )

    def test_verified_admin_match_refused(self):
        """A verified Google email matching an admin is never linked."""
        self._user("admin1", "admin@example.com", role=User.ADMIN)
        extra_data = {"email": "admin@example.com", "email_verified": True}
        self.assertRefused(self._request(), self._new_login(extra_data), ADMIN_MESSAGE)

    def test_verified_inactive_match_refused(self):
        """A verified Google email matching an inactive owner is refused."""
        self._user("owner1", "owner@example.com", is_active=False)
        extra_data = {"email": "owner@example.com", "email_verified": True}
        self.assertRefused(
            self._request(), self._new_login(extra_data), INACTIVE_MESSAGE
        )

    def test_verified_multiple_matches_refused(self):
        """Two users whose emails differ only by case: nothing is linked."""
        self._user("owner1", "dup@example.com")
        self._user("owner2", "DUP@example.com")
        extra_data = {"email": "Dup@Example.com", "email_verified": True}
        self.assertRefused(
            self._request(), self._new_login(extra_data), MULTIPLE_MATCHES_MESSAGE
        )

    # ─── Already linked ───────────────────────────────────────────────────────

    def test_linked_admin_refused(self):
        """An admin who was linked under the old code is still refused."""
        admin = self._user("admin1", "admin@example.com", role=User.ADMIN)
        self.assertRefused(self._request(), self._linked_login(admin), ADMIN_MESSAGE)

    def test_linked_inactive_owner_refused(self):
        """A linked but archived/unverified owner is refused."""
        owner = self._user("owner1", "owner@example.com", is_active=False)
        self.assertRefused(self._request(), self._linked_login(owner), INACTIVE_MESSAGE)

    def test_linked_active_owner_allowed(self):
        """A linked, active owner passes through to the normal login."""
        owner = self._user("owner1", "owner@example.com")
        request = self._request()
        self.adapter.pre_social_login(request, self._linked_login(owner))
        self.assertEqual(self._messages(request), [])


class GoogleSignInViewTests(TestCase):
    """Login-CSRF protection: Google login must start from a POST."""

    def test_get_google_login_does_not_redirect_to_google(self):
        """GET shows allauth's confirmation page (LOGIN_ON_GET is off)."""
        response = self.client.get(reverse("google_login"))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("accounts.google.com", response.get("Location", ""))

    def test_owner_login_has_google_post_form_with_csrf(self):
        """The Google button is a POST form to provider_login_url with a
        CSRF token field."""
        response = self.client.get(reverse("owner_login"))
        content = response.content.decode()
        # provider_login_url 'google' with no params renders this URL
        action = re.escape(reverse("google_login"))
        form = re.search(
            rf'<form method="post" action="{action}"[^>]*>(.*?)</form>',
            content,
            re.S,
        )
        self.assertIsNotNone(form, "Google POST form not found")
        self.assertIn('name="csrfmiddlewaretoken"', form.group(1))


class AdminLoginTests(TestCase):
    """admin_login: redesigned page, and one generic error for every failure."""

    PASSWORD = "admin-pass-123"
    GENERIC_ERROR = "That email or password isn't right. Please try again."

    def setUp(self):
        self.url = reverse("admin_login")

    # ─── Helpers ──────────────────────────────────────────────────────────────

    def _clinic(self):
        return ClinicSettings.objects.create(
            clinic_name="Test Paws Clinic",
            address="1 Test Street",
            opening_time=datetime.time(8, 0),
            closing_time=datetime.time(17, 0),
            same_day_cutoff_time=datetime.time(15, 0),
        )

    def _user(self, username, email, role=User.ADMIN, is_active=True):
        return User.objects.create_user(
            username=username,
            email=email,
            password=self.PASSWORD,
            role=role,
            is_active=is_active,
        )

    def assertGenericFailure(self, response, email):
        """200, the one generic message, typed email kept, nobody logged in."""
        self.assertEqual(response.status_code, 200)
        messages = [str(m) for m in get_messages(response.wsgi_request)]
        self.assertEqual(messages, [self.GENERIC_ERROR])
        self.assertTrue(response.context["login_failed"])
        self.assertEqual(response.context["email"], email)
        self.assertContains(response, f'value="{email}"')
        self.assertNotIn("_auth_user_id", self.client.session)

    # ─── GET ──────────────────────────────────────────────────────────────────

    def test_get_shows_clinic_name_without_debug_text(self):
        """The page uses the auth layout and shows no leftover debug output."""
        self._clinic()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "admin/login.html")
        self.assertTemplateUsed(response, "layouts/auth.html")
        self.assertContains(response, "Test Paws Clinic")
        self.assertNotContains(response, "Logo URL")
        self.assertNotContains(response, "No logo uploaded")
        self.assertNotContains(response, 'src=""')
        self.assertContains(response, 'autocomplete="current-password"')

    def test_get_without_clinic_settings_shows_default_name(self):
        """No ClinicSettings row yet: the page still renders."""
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Hapi Vet")

    def test_logged_in_admin_is_redirected(self):
        self.client.force_login(self._user("admin1", "admin@example.com"))
        response = self.client.get(self.url)
        self.assertRedirects(
            response, reverse("admin_dashboard"), fetch_redirect_response=False
        )

    # ─── POST ─────────────────────────────────────────────────────────────────

    def test_every_failure_shows_one_generic_message(self):
        """Wrong password, unknown or empty email, owner and inactive admin
        all look the same."""
        self._user("admin1", "admin@example.com")
        self._user("owner1", "owner@example.com", role=User.PET_OWNER)
        self._user("admin2", "off@example.com", is_active=False)
        cases = {
            "wrong password": ("admin@example.com", "wrong-pass-123"),
            "unknown email": ("nobody@example.com", self.PASSWORD),
            "empty email": ("", self.PASSWORD),
            "owner account": ("owner@example.com", self.PASSWORD),
            "inactive admin": ("off@example.com", self.PASSWORD),
        }
        for label, (email, password) in cases.items():
            with self.subTest(label):
                response = self.client.post(
                    self.url, {"email": email, "password": password}
                )
                self.assertGenericFailure(response, email)

    def test_empty_email_with_walkin_user_fails_cleanly(self):
        """A walk-in saved with email="" can't be reached by an empty email
        (User.email is unique, so only one such row can exist)."""
        self._user("walkin1", "", role=User.PET_OWNER)
        response = self.client.post(self.url, {"email": "", "password": self.PASSWORD})
        self.assertGenericFailure(response, "")

    def test_admin_logs_in_with_uppercase_email(self):
        admin = self._user("admin1", "admin@example.com")
        response = self.client.post(
            self.url, {"email": "ADMIN@EXAMPLE.COM", "password": self.PASSWORD}
        )
        self.assertRedirects(
            response, reverse("admin_dashboard"), fetch_redirect_response=False
        )
        self.assertEqual(self.client.session["_auth_user_id"], str(admin.pk))


class WalkInOwnerCreateTests(TestCase):
    """admin_walkin_create: walk-in "New owner" (owner + pet), claim email."""

    NO_EMAIL_TAKEN = (
        "Only one owner without an email can be saved for now. "
        "Please add an email address for this owner."
    )

    def setUp(self):
        self.url = reverse("admin_walkin_create")
        self.admin = User.objects.create_user(
            username="admin1",
            email="admin@example.com",
            password="admin-pass-123",
            role=User.ADMIN,
        )
        self.client.force_login(self.admin)

    def _post(self, **overrides):
        data = {
            "first_name": "Ana",
            "last_name": "Cruz",
            "email": "ana@example.com",
            "pet_name": "Max",
            "species": "dog",
        }
        data.update(overrides)
        return self.client.post(self.url, data)

    def _messages(self, response):
        return [(m.level_tag, str(m)) for m in get_messages(response.wsgi_request)]

    def test_page_is_one_centered_column(self):
        """Back link, h1, steps and cards share one mx-auto max-w-4xl column."""
        html = self.client.get(self.url).content.decode()
        match = re.search(r'<div id="page-column" class="([^"]*)">', html)
        self.assertIsNotNone(match)
        classes = match.group(1).split()
        self.assertIn("mx-auto", classes)
        self.assertIn("max-w-4xl", classes)
        self.assertGreater(html.index("<h1"), match.end())
        self.assertEqual(html.count("<h1"), 1)

    def _assert_goes_to_visit_step(self, response):
        owner = PetOwner.objects.get()
        visit_url = reverse("admin_walkin_appointment_visit")
        self.assertRedirects(
            response,
            f"{visit_url}?owner={owner.pk}&pet={Pet.objects.get().pk}",
            fetch_redirect_response=False,
        )

    # ─── Access ───────────────────────────────────────────────────────────────

    def test_anonymous_goes_to_admin_login(self):
        """The login middleware catches /admin/* first; @login_required on
        the view is the second guard."""
        self.client.logout()
        response = self.client.get(self.url)
        self.assertRedirects(
            response, reverse("admin_login"), fetch_redirect_response=False
        )

    def test_owner_role_refused(self):
        owner = User.objects.create_user(
            username="owner1", email="owner@example.com", role=User.PET_OWNER
        )
        self.client.force_login(owner)
        response = self._post(email="new@example.com")
        self.assertRedirects(
            response, reverse("owner_dashboard"), fetch_redirect_response=False
        )
        self.assertFalse(PetOwner.objects.exists())

    # ─── Saving ───────────────────────────────────────────────────────────────

    def test_creates_owner_pet_and_sends_claim_email(self):
        response = self._post()
        owner = PetOwner.objects.get()
        self._assert_goes_to_visit_step(response)
        self.assertEqual(owner.user.email, "ana@example.com")
        self.assertTrue(owner.user.is_active)
        self.assertEqual(Pet.objects.get().owner, owner)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["ana@example.com"])
        self.assertEqual(
            self._messages(response), [("success", "Ana Cruz and Max added.")]
        )

    def test_success_opens_the_visit_step_with_owner_and_pet_chosen(self):
        response = self.client.get(self._post()["Location"])
        self.assertTemplateUsed(response, "admin/appointments/walkin_visit.html")
        self.assertEqual(response.context["owner"], PetOwner.objects.get())
        self.assertEqual(response.context["selected_pet"], str(Pet.objects.get().pk))

    def test_pet_is_required(self):
        for missing in ("pet_name", "species"):
            with self.subTest(missing=missing):
                response = self._post(**{missing: ""})
                self.assertEqual(response.status_code, 200)
                self.assertIn(missing, response.context["pet_form"].errors)
                self.assertFalse(PetOwner.objects.exists())
                self.assertFalse(Pet.objects.exists())
        self.assertEqual(len(mail.outbox), 0)

    def test_page_uses_admin_layout_with_one_primary_and_cancel_to_step_1(self):
        response = self.client.get(self.url)
        self.assertTemplateUsed(response, "layouts/admin.html")
        self.assertContains(response, "Who is it for?")
        self.assertEqual(
            len(re.findall(r"\bbtn-primary\b", response.content.decode())), 1
        )
        self.assertContains(
            response,
            f'<a href="{reverse("admin_walkin_appointment_create")}" '
            'class="btn btn-outline">Cancel</a>',
        )

    def test_mixed_case_email_saved_lowercase(self):
        self._post(email="  Ana.Cruz@Example.COM ")
        self.assertEqual(PetOwner.objects.get().user.email, "ana.cruz@example.com")

    def test_duplicate_email_in_another_case_refused(self):
        User.objects.create_user(
            username="ana", email="ana@example.com", role=User.PET_OWNER
        )
        response = self._post(email="ANA@Example.com")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context["owner_form"].errors["email"],
            ["This email is already used by another account."],
        )
        self.assertFalse(PetOwner.objects.exists())
        self.assertEqual(len(mail.outbox), 0)

    # ─── Claim email fails ────────────────────────────────────────────────────

    def test_send_failure_keeps_owner_and_pet_and_warns(self):
        with (
            mock.patch(
                "accounts.utils.send_mail", side_effect=OSError("smtp secret 535")
            ),
            self.assertLogs("accounts.views", level="ERROR"),
        ):
            response = self._post()
        owner = PetOwner.objects.get()
        self._assert_goes_to_visit_step(response)
        self.assertEqual(Pet.objects.get().owner, owner)
        self.assertEqual(
            self._messages(response),
            [
                ("success", "Ana Cruz and Max added."),
                (
                    "warning",
                    "The claim email to ana@example.com could not be sent. The "
                    "owner and pet are saved, but the owner can't set a password "
                    "until they get this email. Use Send the link again on the "
                    "owner page later.",
                ),
            ],
        )

    # ─── Save errors ──────────────────────────────────────────────────────────

    def test_second_owner_without_email_shows_plain_message(self):
        self._post(email="", first_name="Ben")
        self.assertEqual(PetOwner.objects.count(), 1)

        with self.assertLogs("accounts.views", level="ERROR") as logs:
            response = self._post(email="", first_name="Carla")

        self.assertEqual(response.status_code, 200)
        # [0] is the first owner's success message, not shown until now
        self.assertEqual(self._messages(response)[1:], [("error", self.NO_EMAIL_TAKEN)])
        self.assertContains(response, "Only one owner without an email")
        for raw in ("IntegrityError", "duplicate key", "Error:"):
            self.assertNotContains(response, raw)
        self.assertIn("IntegrityError", "\n".join(logs.output))
        self.assertEqual(PetOwner.objects.count(), 1)
        self.assertEqual(Pet.objects.count(), 1)

    def test_unexpected_error_shows_plain_message(self):
        with (
            mock.patch(
                "accounts.views.create_walkin_owner",
                side_effect=RuntimeError("boom secret detail"),
            ),
            self.assertLogs("accounts.views", level="ERROR"),
        ):
            response = self._post()
        self.assertEqual(
            self._messages(response),
            [
                (
                    "error",
                    "Something went wrong, so this owner was not saved. "
                    "Please try again.",
                )
            ],
        )
        self.assertNotContains(response, "boom secret detail")
        self.assertEqual(len(mail.outbox), 0)


class WalkInClaimEmailAfterCommitTests(TransactionTestCase):
    """The claim email goes out only after the owner is committed.
    (TestCase wraps every test in a transaction, so it can't show this.)"""

    def test_claim_email_sent_after_commit(self):
        admin = User.objects.create_user(
            username="admin1", email="admin@example.com", role=User.ADMIN
        )
        self.client.force_login(admin)
        seen = {}

        def record(request, user):
            seen["in_atomic_block"] = connection.in_atomic_block
            seen["saved"] = User.objects.filter(pk=user.pk).exists()

        with mock.patch("accounts.views.send_claim_email", side_effect=record):
            self.client.post(
                reverse("admin_walkin_create"),
                {
                    "first_name": "Ana",
                    "last_name": "Cruz",
                    "email": "ana@example.com",
                    "pet_name": "Max",
                    "species": "dog",
                },
            )

        self.assertEqual(seen, {"in_atomic_block": False, "saved": True})


class OwnerProfileRequiredTests(TestCase):
    """Slice 8a-1a: a pet owner with no PetOwner row goes to onboarding step 1
    (no 500, no 404); an admin and an owner with a row are unaffected."""

    def setUp(self):
        self.step1 = reverse("owner_onboarding_step1")

    def _user(self, role=User.PET_OWNER, is_onboarded=False):
        user = User.objects.create_user(
            username=f"{role}-{is_onboarded}",
            email=f"{role}-{is_onboarded}@example.com",
            password="pass-12345-x",
            role=role,
            is_onboarded=is_onboarded,
        )
        self.client.force_login(user)
        return user

    def _pages(self):
        """One owner view from each app: appointments, billing, medical
        history, owner search, pets list."""
        return [
            reverse("owner_appointments"),
            reverse("owner_billing_list"),
            reverse("owner_pet_medical_history", args=[1]),
            reverse("owner_global_search") + "?q=max",
            reverse("owner_pet_list"),
        ]

    def test_owner_without_a_row_is_sent_to_step_1_on_every_page(self):
        self._user()
        for url in self._pages():
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertRedirects(
                    response, self.step1, fetch_redirect_response=False
                )

    def test_onboarded_flag_without_a_row_still_reaches_step_1(self):
        """No redirect loop: step 1 shows its form for this user."""
        self._user(is_onboarded=True)
        response = self.client.get(reverse("owner_appointments"), follow=True)
        self.assertEqual(response.redirect_chain[-1][0], self.step1)
        self.assertEqual(response.status_code, 200)

    def test_htmx_request_gets_hx_redirect(self):
        self._user()
        response = self.client.get(
            reverse("owner_global_search") + "?q=max", HTTP_HX_REQUEST="true"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["HX-Redirect"], self.step1)

    def test_admin_is_unaffected(self):
        self._user(role=User.ADMIN)
        response = self.client.get(reverse("owner_appointments"))
        self.assertRedirects(
            response, reverse("admin_dashboard"), fetch_redirect_response=False
        )

    def test_owner_with_a_row_gets_the_pages(self):
        user = self._user(is_onboarded=True)
        owner = PetOwner.objects.create(user=user, first_name="Ana", last_name="Cruz")
        pet = Pet.objects.create(owner=owner, name="Max", species="Dog")
        urls = [
            reverse("owner_appointments"),
            reverse("owner_billing_list"),
            reverse("owner_pet_medical_history", args=[pet.pk]),
            reverse("owner_global_search") + "?q=max",
            reverse("owner_pet_list"),
        ]
        for url in urls:
            with self.subTest(url=url):
                # follow=True: the medical history URL redirects to the pet
                # profile since 8b-5 (it was a 200 page)
                self.assertEqual(self.client.get(url, follow=True).status_code, 200)


class RegisterEmailFailureTests(TestCase):
    """Slice 8a-1a: a failed verification email keeps the account, logs the
    error and still goes to the check-your-email page."""

    WARNING = (
        "Your account was created, but we couldn't send the email just now. "
        "Please contact the clinic so they can help."
    )

    def _post(self, **extra):
        data = {
            "email": "new.owner@example.com",
            "password1": "Str0ng-pass-123",
            "password2": "Str0ng-pass-123",
        }
        with mock.patch(
            "accounts.views.send_mail", side_effect=Exception("boom secret detail")
        ):
            return self.client.post(reverse("owner_register"), data, **extra)

    def test_account_kept_and_redirected_with_warning(self):
        with self.assertLogs("accounts.views", level="ERROR"):
            response = self._post()
        self.assertRedirects(
            response, reverse("owner_register_pending"), fetch_redirect_response=False
        )
        user = User.objects.get(email="new.owner@example.com")
        self.assertFalse(user.is_active)
        found = [(m.level_tag, str(m)) for m in get_messages(response.wsgi_request)]
        self.assertEqual(found, [("warning", self.WARNING)])

    def test_error_is_logged_and_never_shown(self):
        with self.assertLogs("accounts.views", level="ERROR") as logs:
            response = self._post(follow=True)
        self.assertIn("boom secret detail", "\n".join(logs.output))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "boom secret detail")


class AllauthRedirectTests(TestCase):
    """Slice 8a-1a: allauth's login and signup pages go to our own pages."""

    def test_login_redirects_to_owner_login(self):
        response = self.client.get("/accounts/login/")
        self.assertRedirects(
            response, reverse("owner_login"), fetch_redirect_response=False
        )

    def test_signup_redirects_to_owner_register(self):
        response = self.client.get("/accounts/signup/")
        self.assertRedirects(
            response, reverse("owner_register"), fetch_redirect_response=False
        )

    def test_next_is_kept(self):
        for path, name in [
            ("/accounts/login/", "owner_login"),
            ("/accounts/signup/", "owner_register"),
        ]:
            with self.subTest(path=path):
                response = self.client.get(path, {"next": "/owner/pets/"})
                self.assertEqual(
                    response["Location"], f"{reverse(name)}?next=%2Fowner%2Fpets%2F"
                )

    def test_an_outside_next_is_dropped(self):
        response = self.client.get("/accounts/login/", {"next": "https://evil.test/"})
        self.assertEqual(response["Location"], reverse("owner_login"))

    def test_allauth_names_resolve_to_the_redirects(self):
        self.assertEqual(reverse("account_login"), "/accounts/login/")
        self.assertEqual(reverse("account_signup"), "/accounts/signup/")

    def test_google_login_and_callback_still_resolve(self):
        self.assertEqual(reverse("google_login"), "/accounts/google/login/")
        self.assertEqual(reverse("google_callback"), "/accounts/google/login/callback/")
        self.assertEqual(self.client.get(reverse("google_login")).status_code, 200)


class TermsPrivacyLinkTests(TestCase):
    """Slice 8a-1a: the Create account links come from the two settings."""

    def test_register_page_uses_the_settings(self):
        with override_settings(
            TERMS_URL="https://example.test/terms",
            PRIVACY_URL="https://example.test/privacy",
        ):
            response = self.client.get(reverse("owner_register"))
        self.assertContains(response, 'href="https://example.test/terms"')
        self.assertContains(response, 'href="https://example.test/privacy"')
        self.assertEqual(response.context["terms_url"], "https://example.test/terms")
        self.assertEqual(
            response.context["privacy_url"], "https://example.test/privacy"
        )

    def test_default_values_are_todays_external_links(self):
        self.assertEqual(
            settings.PRIVACY_URL, "https://privacy.gov.ph/data-privacy-act/"
        )
        self.assertTrue(settings.TERMS_URL.startswith("https://paws.org.ph/"))
        response = self.client.get(reverse("owner_register"))
        self.assertContains(response, f'href="{settings.TERMS_URL}"')


class _LoggedInBase(TestCase):
    """One admin and one owner (with a PetOwner row), neither logged in yet."""

    def setUp(self):
        self.admin = User.objects.create_user(
            username="clinic-admin",
            email="admin@example.com",
            password="pass-12345-x",
            role=User.ADMIN,
            is_onboarded=True,
        )
        self.owner_user = User.objects.create_user(
            username="ana",
            email="ana@example.com",
            password="pass-12345-x",
            role=User.PET_OWNER,
            is_onboarded=True,
        )
        PetOwner.objects.create(
            user=self.owner_user, first_name="Ana", last_name="Cruz"
        )

    def _is_logged_in(self):
        return "_auth_user_id" in self.client.session


class LogoutPostTests(_LoggedInBase):
    """Slice 8a-1b: logging out is a POST. A GET never logs anyone out."""

    def test_post_logs_out_and_redirects_by_role(self):
        for user, login_page in (
            (self.admin, "admin_login"),
            (self.owner_user, "owner_login"),
        ):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                response = self.client.post(reverse("logout"))
                self.assertRedirects(
                    response, reverse(login_page), fetch_redirect_response=False
                )
                self.assertFalse(self._is_logged_in())
                found = [str(m) for m in get_messages(response.wsgi_request)]
                # (the first subtest's message may still be waiting in the cookie)
                self.assertEqual(found[-1], "You're logged out.")

    def test_get_keeps_the_user_logged_in_and_goes_to_their_dashboard(self):
        for user, dashboard in (
            (self.admin, "admin_dashboard"),
            (self.owner_user, "owner_dashboard"),
        ):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                response = self.client.get(reverse("logout"))
                self.assertRedirects(
                    response, reverse(dashboard), fetch_redirect_response=False
                )
                self.assertTrue(self._is_logged_in())

    def test_logged_out_visitor_goes_to_owner_login(self):
        for send in (self.client.get, self.client.post):
            with self.subTest(method=send.__name__):
                response = send(reverse("logout"))
                self.assertRedirects(
                    response, reverse("owner_login"), fetch_redirect_response=False
                )

    def test_post_needs_the_csrf_token(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner_user)
        self.assertEqual(client.post(reverse("logout")).status_code, 403)
        self.assertIn("_auth_user_id", client.session)

    def test_every_logout_control_is_a_post_form_with_a_csrf_token(self):
        """The admin sidebar (every admin page). Owner pages are on
        layouts/owner.html, where the only Log out is the sheet on the Account
        page (OwnerAccountHubTests)."""
        logout_url = reverse("logout")
        form = re.compile(
            rf'<form method="post" action="{logout_url}">\s*'
            r'<input type="hidden" name="csrfmiddlewaretoken" value="[^"]+">\s*'
            r'<button type="submit"[^>]*>'
        )
        pages = {
            self.admin: ["admin_dashboard", "admin_clinic_settings", "admin_pet_list"],
        }
        for user, names in pages.items():
            self.client.force_login(user)
            for name in names:
                with self.subTest(page=name):
                    html = self.client.get(reverse(name)).content.decode()
                    self.assertRegex(html, form)
                    self.assertNotIn(f'href="{logout_url}"', html)

    def test_owner_dashboard_and_my_pets_have_no_get_link_to_logout(self):
        """Both moved to layouts/owner.html in 8b-4: their only Log out is the
        sheet on the Account page, so they never link to the logout URL."""
        self.client.force_login(self.owner_user)
        # 8b-7a: owner_appointments (Visits) moved to the owner layout too
        for name in ("owner_dashboard", "owner_pet_list", "owner_appointments"):
            with self.subTest(page=name):
                response = self.client.get(reverse(name))
                self.assertNotContains(response, f'href="{reverse("logout")}"')

    def test_the_log_out_button_keeps_its_text_and_classes(self):
        self.client.force_login(self.admin)
        html = self.client.get(reverse("admin_dashboard")).content.decode()
        self.assertRegex(
            html,
            r'(?s)<button type="submit" class="flex min-h-11 w-full items-center gap-3 '
            r"rounded-lg px-3 text-left text-base text-gray-800 "
            r'hover:bg-violet-200/70">.*?Log out</button>',
        )


class AllauthSideDoorTests(_LoggedInBase):
    """Slice 8a-1b: allauth's password reset, confirm-email, inactive and
    logout pages go to ours. Google login and callback stay."""

    def _assert_redirects_to(self, path, name):
        response = self.client.get(path)
        self.assertRedirects(response, reverse(name), fetch_redirect_response=False)

    def test_password_reset_pages_go_to_forgot_password(self):
        for path in (
            "/accounts/password/reset/",
            "/accounts/password/reset/done/",
            "/accounts/password/reset/key/done/",
            "/accounts/password/reset/key/3f-abc123def456/",
        ):
            with self.subTest(path=path):
                self._assert_redirects_to(path, "forgot_password")

    def test_confirm_email_pages_go_to_owner_login(self):
        for path in ("/accounts/confirm-email/", "/accounts/confirm-email/ab:cd-1/"):
            with self.subTest(path=path):
                self._assert_redirects_to(path, "owner_login")

    def test_inactive_goes_to_owner_login_with_the_error(self):
        response = self.client.get("/accounts/inactive/")
        self.assertRedirects(
            response, reverse("owner_login"), fetch_redirect_response=False
        )
        found = [(m.level_tag, str(m)) for m in get_messages(response.wsgi_request)]
        self.assertEqual(
            found,
            [
                (
                    "error",
                    "This account can't log in yet. Check your email for the "
                    "link, or contact the clinic.",
                )
            ],
        )
        shown = self.client.get(reverse("owner_login"))
        self.assertContains(shown, "This account can&#x27;t log in yet.")

    def test_logout_is_our_view_and_a_post_still_logs_out(self):
        self.assertEqual(resolve("/accounts/logout/").func.__name__, "user_logout")
        self.client.force_login(self.owner_user)
        response = self.client.get("/accounts/logout/")
        self.assertRedirects(
            response, reverse("owner_dashboard"), fetch_redirect_response=False
        )
        self.assertTrue(self._is_logged_in())

        response = self.client.post("/accounts/logout/")
        self.assertRedirects(
            response, reverse("owner_login"), fetch_redirect_response=False
        )
        self.assertFalse(self._is_logged_in())

    def test_google_login_and_callback_still_resolve(self):
        self.assertEqual(reverse("google_login"), "/accounts/google/login/")
        self.assertEqual(reverse("google_callback"), "/accounts/google/login/callback/")
        self.assertEqual(self.client.get(reverse("google_login")).status_code, 200)


class OwnerAccountHubTests(_LoggedInBase):
    """Slice 8a-2c: /owner/account/, the account hub (plain markup; the design
    comes with 8b-9)."""

    def setUp(self):
        super().setUp()
        self.url = reverse("owner_account")

    def test_the_url(self):
        self.assertEqual(self.url, "/owner/account/")

    def test_anonymous_is_sent_to_log_in(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertNotEqual(response.url, self.url)

    def test_admin_is_sent_to_the_admin_dashboard(self):
        self.client.force_login(self.admin)
        response = self.client.get(self.url)
        self.assertRedirects(
            response, reverse("admin_dashboard"), fetch_redirect_response=False
        )

    def test_owner_without_a_row_goes_to_onboarding(self):
        PetOwner.objects.filter(user=self.owner_user).delete()
        self.client.force_login(self.owner_user)
        response = self.client.get(self.url)
        self.assertRedirects(
            response, reverse("owner_onboarding_step1"), fetch_redirect_response=False
        )

    def test_owner_sees_name_email_and_the_links(self):
        self.client.force_login(self.owner_user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "owner/account/index.html")
        self.assertEqual(response.context["owner"].user, self.owner_user)
        # 8b-9a: the h1 and the link rows have classes and inner spans now
        self.assertContains(
            response, '<h1 class="text-2xl font-bold text-gray-900">Account</h1>'
        )
        self.assertContains(response, "Ana Cruz")
        self.assertContains(response, "ana@example.com")
        html = response.content.decode()
        for name, text in (
            ("owner_profile_edit", "My details"),
            ("owner_notification_preferences", "Email notifications"),
            ("clinic_info", "Clinic info"),
            ("owner_health_article_list", "Health library"),
        ):
            with self.subTest(link=name):
                link = (
                    rf'(?s)<a href="{reverse(name)}" class="flex min-h-16[^>]*>'
                    rf".*?>{text}</span>"
                )
                self.assertRegex(html, link)

    def test_every_link_is_on_one_line(self):
        # 8b-9a, re-anchored: the rows hold icons (multi-line once rendered),
        # so the same check reads the page template and its row part
        for name in ("owner/account/index.html", "owner/account_parts/link_row.html"):
            for link in re.findall(r"(?s)<a\b.*?</a>", template_source(name)):
                with self.subTest(file=name, link=link[:50]):
                    self.assertNotIn("\n", link)

    def test_log_out_is_a_post_form_with_a_csrf_token(self):
        self.client.force_login(self.owner_user)
        html = self.client.get(self.url).content.decode()
        # Slice 8b-1: the form now lives in the log out sheet (its button
        # reads "Yes, log out"; the page's "Log out" button only opens it)
        form = (
            rf'<form method="post" action="{reverse("logout")}">\s*'
            r'<input type="hidden" name="csrfmiddlewaretoken" value="[^"]+">\s*'
            r'<button type="submit"[^>]*>(?s:.*?)Yes, log out\s*</button>'
        )
        self.assertRegex(html, form)
        self.assertNotIn(f'href="{reverse("logout")}"', html)

    def test_a_get_of_logout_still_never_logs_out(self):
        self.client.force_login(self.owner_user)
        self.client.get(self.url)
        response = self.client.get(reverse("logout"))
        self.assertRedirects(
            response, reverse("owner_dashboard"), fetch_redirect_response=False
        )
        self.assertTrue(self._is_logged_in())


# ── Slice 8a-2d: resend verification, not-confirmed login, verify guard ──────

PENDING_PASSWORD = "Str0ng-pass-123"
RESEND_REPLY_TEXT = (
    "If that email has an account waiting to be confirmed, we sent the link "
    "again. It may take a minute. Check Spam too."
)
GENERIC_LOGIN_ERROR = "That email and password don't match"
CLOSED_TEXT = "This account is closed. Please contact the clinic."


def make_owner(email, **extra):
    """An owner User (pending by default: inactive, usable password, no login
    yet, no PetOwner row)."""
    fields = {"is_active": False, "role": User.PET_OWNER, "password": PENDING_PASSWORD}
    fields.update(extra)
    return User.objects.create_user(username=email.split("@")[0], email=email, **fields)


def archive_owner(user):
    return PetOwner.objects.create(
        user=user, first_name="Old", last_name="Owner", is_archived=True
    )


def verify_path(user):
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    return reverse("owner_verify_email", kwargs={"uidb64": uid, "token": token})


def reply_of(response):
    """What a visitor can see of a reply: status, target, messages."""
    found = [(m.level_tag, str(m)) for m in get_messages(response.wsgi_request)]
    return response.status_code, response.url, found


class VerificationPendingTests(TestCase):
    """Slice 8a-2d: the one rule for who may get the link again."""

    def test_true_for_a_freshly_registered_owner(self):
        data = {
            "email": "fresh@example.com",
            "password1": PENDING_PASSWORD,
            "password2": PENDING_PASSWORD,
        }
        self.client.post(reverse("owner_register"), data)
        user = User.objects.get(email="fresh@example.com")
        self.assertTrue(verification_pending(user))

    def test_true_with_no_pet_owner_row_yet(self):
        user = make_owner("norow@example.com")
        self.assertFalse(PetOwner.objects.filter(user=user).exists())
        self.assertTrue(verification_pending(user))

    def test_true_with_a_pet_owner_row_that_is_not_archived(self):
        user = make_owner("row@example.com")
        PetOwner.objects.create(user=user, first_name="A", last_name="B")
        self.assertTrue(verification_pending(user))

    def test_false_when_active(self):
        user = make_owner("a@example.com", is_active=True)
        self.assertFalse(verification_pending(user))

    def test_false_when_logged_in_before(self):
        user = make_owner("b@example.com", last_login=timezone.now())
        self.assertFalse(verification_pending(user))

    def test_false_without_a_usable_password(self):
        # Walk-in, clinic-made and Google sign-ups have no usable password
        walk_in = make_owner("c@example.com", password=None)
        self.assertFalse(walk_in.has_usable_password())
        self.assertFalse(verification_pending(walk_in))

    def test_false_for_an_admin(self):
        user = make_owner("d@example.com", role=User.ADMIN)
        self.assertFalse(verification_pending(user))

    def test_false_for_an_archived_owner(self):
        user = make_owner("e@example.com")
        archive_owner(user)
        self.assertFalse(verification_pending(user))


class ResendVerificationTests(TestCase):
    """Slice 8a-2d: POST /resend-verification/. Always the same reply."""

    def setUp(self):
        cache.clear()
        self.url = reverse("owner_resend_verification")
        self.user = make_owner("waiting@example.com")

    def _post(self, email="waiting@example.com", client=None):
        return (client or self.client).post(self.url, {"email": email})

    def _link_path(self, message):
        return re.search(r"https?://[^/\s]+(/verify-email/\S+)", message.body).group(1)

    def test_the_url(self):
        self.assertEqual(self.url, "/resend-verification/")

    def test_get_sends_nothing_and_goes_to_login(self):
        response = self.client.get(self.url)
        self.assertRedirects(
            response, reverse("owner_login"), fetch_redirect_response=False
        )
        self.assertEqual(len(mail.outbox), 0)

    def test_post_sends_one_email_with_a_working_link(self):
        response = self._post()
        self.assertRedirects(
            response, reverse("owner_register_pending"), fetch_redirect_response=False
        )
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["waiting@example.com"])
        self.client.get(self._link_path(mail.outbox[0]))
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)

    def test_email_case_and_spaces_are_ignored(self):
        self._post("  WAITING@Example.COM ")
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["waiting@example.com"])

    def test_a_second_post_within_two_minutes_sends_nothing(self):
        self._post()
        self._post()
        self.assertEqual(len(mail.outbox), 1)

    def test_the_throttle_is_one_send_per_120_seconds_per_account(self):
        with mock.patch("accounts.utils.cache.add", return_value=True) as add:
            self._post()
        add.assert_called_once_with(f"resend-verification:{self.user.pk}", 1, 120)

    def test_sends_again_after_the_key_expires(self):
        self._post()
        cache.delete(f"resend-verification:{self.user.pk}")
        self._post()
        self.assertEqual(len(mail.outbox), 2)

    def test_another_account_is_not_throttled_by_this_one(self):
        make_owner("other@example.com")
        self._post()
        self._post("other@example.com")
        self.assertEqual(len(mail.outbox), 2)

    def test_the_reply_is_identical_for_every_case(self):
        make_owner("active@example.com", is_active=True)
        archive_owner(make_owner("closed@example.com"))
        make_owner("google@example.com", password=None)
        make_owner("busy@example.com")
        self._post("busy@example.com")  # the next post is throttled
        cases = {
            "pending": "waiting@example.com",
            "unknown": "nobody@example.com",
            "active": "active@example.com",
            "archived": "closed@example.com",
            "google": "google@example.com",
            "throttled": "busy@example.com",
            "empty": "",
        }
        expected = (
            302,
            reverse("owner_register_pending"),
            [("info", RESEND_REPLY_TEXT)],
        )
        for name, email in cases.items():
            with self.subTest(case=name):
                # A new client each time: unread messages would pile up
                response = self._post(email, client=Client())
                self.assertEqual(reply_of(response), expected)
        # Only the pending owner and the first "busy" post got an email
        self.assertEqual(len(mail.outbox), 2)

    def test_a_send_error_gives_the_same_reply_and_is_logged_quietly(self):
        boom = Exception("boom secret detail waiting@example.com")
        with mock.patch("accounts.views.send_mail", side_effect=boom):
            with self.assertLogs("accounts.views", level="ERROR") as logs:
                response = self._post()
        self.assertEqual(
            reply_of(response),
            (302, reverse("owner_register_pending"), [("info", RESEND_REPLY_TEXT)]),
        )
        text = "\n".join(logs.output)
        for secret in ("boom secret", "waiting@example.com", "verify-email"):
            self.assertNotIn(secret, text)
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_active)

    def test_csrf_is_enforced(self):
        client = Client(enforce_csrf_checks=True)
        response = client.post(self.url, {"email": "waiting@example.com"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(len(mail.outbox), 0)

    def test_works_logged_out_and_the_reply_is_shown_on_the_page(self):
        response = self.client.post(self.url, {"email": "x@example.com"}, follow=True)
        self.assertEqual(response.status_code, 200)
        # The shared flash component shows the first sentence in bold
        lead, rest = RESEND_REPLY_TEXT.split(". ", 1)
        self.assertContains(
            response, f'<strong class="font-semibold">{lead}.</strong> {rest}'
        )

    def test_the_check_your_email_page_has_the_resend_form(self):
        response = self.client.get(reverse("owner_register_pending"))
        self.assertContains(response, f'action="{self.url}"')
        self.assertContains(response, 'name="email"')
        self.assertContains(response, "Email address")
        self.assertContains(response, "Send the link again")
        self.assertContains(response, "csrfmiddlewaretoken")


class LoginNotConfirmedTests(TestCase):
    """Slice 8a-2d: the "Please confirm your email first" box on the login."""

    BOX = "Please confirm your email first"

    def setUp(self):
        cache.clear()
        self.url = reverse("owner_login")

    def _login(self, email, password=PENDING_PASSWORD):
        return self.client.post(self.url, {"email": email, "password": password})

    def _assert_generic(self, response):
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, GENERIC_LOGIN_ERROR)
        self.assertNotContains(response, self.BOX)
        self.assertNotContains(response, "Send the link again")
        self.assertIsNone(response.context.get("unconfirmed_email"))
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_right_password_on_a_pending_owner_shows_the_box(self):
        user = make_owner("waiting@example.com")
        response = self._login("waiting@example.com")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["unconfirmed_email"], "waiting@example.com")
        self.assertContains(response, self.BOX)
        self.assertContains(
            response,
            "We sent a link to waiting@example.com when you signed up. "
            "Open it, then log in here.",
        )
        self.assertContains(response, 'name="email" value="waiting@example.com"')
        resend_url = reverse("owner_resend_verification")
        self.assertContains(response, f'action="{resend_url}"')
        self.assertContains(response, "Send the link again")
        self.assertNotContains(response, GENERIC_LOGIN_ERROR)
        self.assertNotIn("_auth_user_id", self.client.session)
        user.refresh_from_db()
        self.assertFalse(user.is_active)
        self.assertEqual(len(mail.outbox), 0)

    def test_the_box_email_is_typed_case_insensitively(self):
        make_owner("waiting@example.com")
        response = self._login("  Waiting@Example.com ")
        self.assertContains(response, self.BOX)

    def test_the_resend_form_in_the_box_sends_the_link(self):
        make_owner("waiting@example.com")
        self._login("waiting@example.com")
        resend_url = reverse("owner_resend_verification")
        self.client.post(resend_url, {"email": "waiting@example.com"})
        self.assertEqual(len(mail.outbox), 1)

    def test_wrong_password_shows_the_generic_error(self):
        make_owner("waiting@example.com")
        self._assert_generic(self._login("waiting@example.com", "wrong-password-1"))

    def test_unknown_email_shows_the_generic_error(self):
        self._assert_generic(self._login("nobody@example.com"))

    def test_archived_owner_with_the_right_password_shows_the_generic_error(self):
        archive_owner(make_owner("closed@example.com"))
        self._assert_generic(self._login("closed@example.com"))

    def test_google_and_walk_in_users_show_the_generic_error(self):
        make_owner("google@example.com", password=None)
        self._assert_generic(self._login("google@example.com"))
        self._assert_generic(self._login("google@example.com", ""))

    def test_an_inactive_admin_shows_the_generic_error(self):
        make_owner("boss@example.com", role=User.ADMIN)
        self._assert_generic(self._login("boss@example.com"))

    def test_an_active_admin_never_sees_the_box(self):
        make_owner("boss@example.com", role=User.ADMIN, is_active=True)
        response = self._login("boss@example.com")
        self.assertNotContains(response, self.BOX)
        self.assertContains(response, "Please use the admin login page.")

    def test_an_owner_who_logged_in_before_shows_the_generic_error(self):
        make_owner("back@example.com", last_login=timezone.now())
        self._assert_generic(self._login("back@example.com"))

    def test_a_normal_active_owner_still_logs_in(self):
        make_owner("ana@example.com", is_active=True, is_onboarded=True)
        response = self._login("ana@example.com")
        self.assertRedirects(
            response, reverse("owner_dashboard"), fetch_redirect_response=False
        )
        self.assertIn("_auth_user_id", self.client.session)


class VerifyEmailGuardTests(TestCase):
    """Slice 8a-2d: the verification link only turns on a normal owner."""

    def test_a_pending_owners_link_activates_the_account(self):
        user = make_owner("waiting@example.com")
        response = self.client.get(verify_path(user))
        self.assertRedirects(
            response, reverse("owner_login"), fetch_redirect_response=False
        )
        self.assertEqual(
            [(m.level_tag, str(m)) for m in get_messages(response.wsgi_request)],
            [("success", "Email verified successfully. You can now log in.")],
        )
        user.refresh_from_db()
        self.assertTrue(user.is_active)

    def test_an_archived_owners_link_does_not_activate(self):
        user = make_owner("closed@example.com")
        archive_owner(user)
        response = self.client.get(verify_path(user), follow=True)
        self.assertRedirects(response, reverse("owner_login"))
        # The shared flash component shows the first sentence in bold
        lead, rest = CLOSED_TEXT.split(". ", 1)
        self.assertContains(
            response, f'<strong class="font-semibold">{lead}.</strong> {rest}'
        )
        user.refresh_from_db()
        self.assertFalse(user.is_active)

    def test_a_non_owners_link_does_not_activate(self):
        user = make_owner("boss@example.com", role=User.ADMIN)
        response = self.client.get(verify_path(user))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "owner/verify_email_invalid.html")
        user.refresh_from_db()
        self.assertFalse(user.is_active)

    def test_an_already_active_owner_keeps_todays_behaviour(self):
        user = make_owner("ana@example.com", is_active=True)
        response = self.client.get(verify_path(user))
        self.assertRedirects(
            response, reverse("owner_login"), fetch_redirect_response=False
        )
        user.refresh_from_db()
        self.assertTrue(user.is_active)

    def test_a_bad_token_is_still_the_invalid_page(self):
        user = make_owner("waiting@example.com")
        uid = urlsafe_base64_encode(force_bytes(user.pk))
        path = reverse("owner_verify_email", kwargs={"uidb64": uid, "token": "x-y"})
        response = self.client.get(path)
        self.assertTemplateUsed(response, "owner/verify_email_invalid.html")
        user.refresh_from_db()
        self.assertFalse(user.is_active)


# ── Slice 8b-1: owner layout, flash component, bottom sheet, error pages ─────

TEMPLATES_DIR = Path(settings.BASE_DIR) / "templates"
LAYOUT_FILES = [
    "layouts/owner.html",
    "layouts/owner_parts/tabs.html",
    "layouts/owner_parts/tab_link.html",
    "layouts/owner_parts/back_bar.html",
]


def template_source(name):
    return (TEMPLATES_DIR / name).read_text(encoding="utf-8")


class OwnerLayoutTests(_LoggedInBase):
    """The owner layout, seen on the Account page (the pilot page)."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner_user)
        self.url = reverse("owner_account")

    def _html(self):
        return self.client.get(self.url).content.decode()

    def _bell(self, html):
        return re.search(rf'<a href="{reverse("owner_notifications")}"[^>]*>', html)

    def _notify(self, user, count):
        for number in range(count):
            Notification.objects.create(
                recipient=user,
                notification_type=Notification.APPOINTMENT_APPROVED,
                title=f"Title {number}",
                message="Message",
            )

    def test_the_page_uses_the_owner_layout_only(self):
        response = self.client.get(self.url)
        self.assertTemplateUsed(response, "layouts/owner.html")
        self.assertTemplateNotUsed(response, "base_dashboard.html")
        self.assertTemplateNotUsed(response, "shared/modals.html")

    def test_loads_the_compiled_css_htmx_and_the_shared_scripts(self):
        html = self._html()
        for pattern in (
            r'<link rel="stylesheet" href="/static/css/app\.[0-9a-f]+\.css">',
            r'<script src="/static/vendor/htmx-2\.0\.11\.min\.[0-9a-f]+\.js" defer>',
            r'<script src="/static/js/shared/dialogs\.[0-9a-f]+\.js" defer>',
            r'<script src="/static/js/shared/flash\.[0-9a-f]+\.js" defer>',
        ):
            with self.subTest(pattern=pattern):
                self.assertRegex(html, pattern)

    def test_no_cdn_legacy_css_modals_or_font_import(self):
        html = self._html()
        for word in (
            "cdn.tailwindcss.com",
            "unpkg.com",
            "css/owner/",
            "css/shared/",
            "confirm-modal",
            "openConfirmModal",
            "Poppins",
            "fonts.googleapis",
            "@import",
        ):
            with self.subTest(word=word):
                self.assertNotIn(word, html)

    def test_layout_and_parts_have_no_inline_script_or_handlers(self):
        names = LAYOUT_FILES + [
            "components/flash_messages.html",
            "components/bottom_sheet.html",
        ]
        for name in names:
            source = template_source(name)
            with self.subTest(file=name):
                self.assertNotIn("<script>", source)
                self.assertIsNone(re.search(r"<script(?![^>]*\ssrc=)", source))
                self.assertIsNone(re.search(r"\son[a-z]+\s*=", source))
                self.assertNotIn("hx-confirm", source)
                self.assertNotIn("<!--", source)

    def test_every_link_in_the_layout_files_and_the_page_is_on_one_line(self):
        names = LAYOUT_FILES + ["owner/account/index.html"]
        for name in names:
            for link in re.findall(r"(?s)<a\b.*?</a>", template_source(name)):
                with self.subTest(file=name, link=link[:50]):
                    self.assertNotIn("\n", link)

    def test_skip_link_is_first_in_the_body_and_main_has_its_id(self):
        html = self._html()
        self.assertRegex(
            html, r'<body[^>]*>\s*<a href="#main"[^>]*>Skip to content</a>'
        )
        self.assertRegex(html, r'<main id="main" class="[^"]*\bpb-28\b[^"]*">')

    def test_top_bar_logo_links_home_and_has_the_clinic_name(self):
        html = self._html()
        home = reverse("owner_dashboard")
        link = re.search(rf'(?s)<a href="{home}" class="[^"]*">(.*?)</a>', html)
        self.assertIn("Hapi Tutz", link.group(1))
        self.assertIn("<svg", link.group(1))  # the paw tile: no logo uploaded
        self.assertRegex(html, r'<header class="sticky top-0 z-20[^"]*bg-white')
        self.assertIn("h-16", html)

    def test_bell_links_to_the_notifications_page_with_words(self):
        html = self._html()
        bell = self._bell(html)
        self.assertIsNotNone(bell)
        after = html.split(bell.group(0), 1)[1].split("</a>", 1)[0]
        self.assertIn("<svg", after)
        self.assertIn('<span class="sr-only">Notifications</span>', after)

    def test_badge_is_hidden_at_zero_and_counts_at_three_and_caps_at_ten(self):
        html = self._html()
        self.assertIn('aria-label="Notifications"', self._bell(html).group(0))
        self.assertNotIn("bg-red-600", html.split("</header>")[0])
        self._notify(self.owner_user, 3)
        html = self._html()
        self.assertIn('aria-label="Notifications, 3 unread"', self._bell(html).group(0))
        self.assertRegex(html, r'aria-hidden="true">3</span></a>')
        self._notify(self.owner_user, 7)
        html = self._html()
        self.assertIn(
            'aria-label="Notifications, 10 unread"', self._bell(html).group(0)
        )
        self.assertRegex(html, r'aria-hidden="true">9\+</span></a>')

    def test_another_owners_unread_count_is_never_used(self):
        other = User.objects.create_user(
            username="ben",
            email="ben@example.com",
            password="pass-12345-x",
            role=User.PET_OWNER,
            is_onboarded=True,
        )
        self._notify(other, 4)
        self._notify(self.admin, 2)
        html = self._html()
        self.assertIn('aria-label="Notifications"', self._bell(html).group(0))
        self.assertNotIn("unread", html)

    def test_five_tabs_in_order_with_icons_words_and_links(self):
        html = self._html()
        nav = re.search(r'(?s)<nav aria-label="Main"[^>]*>(.*?)</nav>', html)
        self.assertIsNotNone(nav)
        self.assertIn("pb-[env(safe-area-inset-bottom)]", nav.group(0))
        self.assertIn("fixed inset-x-0 bottom-0", nav.group(0))
        tabs = re.findall(r'(?s)<a href="([^"]+)"([^>]*)>(.*?)</a>', nav.group(1))
        expected = [
            ("owner_dashboard", "Home"),
            ("owner_pet_list", "Pets"),
            ("owner_appointments", "Visits"),
            ("owner_billing_list", "Bills"),
            ("owner_account", "Account"),
        ]
        self.assertEqual(len(tabs), 5)
        for (href, attrs, inner), (name, word) in zip(tabs, expected):
            with self.subTest(tab=word):
                self.assertEqual(href, reverse(name))
                self.assertIn("<svg", inner)
                self.assertEqual(re.sub(r"(?s)<svg.*?</svg>|\s", "", inner), word)
                self.assertIn("min-h-14", attrs)

    def test_only_the_account_tab_is_current_and_light_violet(self):
        html = self._html()
        nav = re.search(r'(?s)<nav aria-label="Main"[^>]*>(.*?)</nav>', html).group(1)
        self.assertEqual(nav.count('aria-current="page"'), 1)
        self.assertEqual(nav.count("bg-violet-100 text-violet-800"), 1)
        current = re.search(r'<a href="([^"]+)"[^>]*aria-current="page"', nav)
        self.assertEqual(current.group(1), reverse("owner_account"))

    def test_owner_tab_mapping(self):
        expected = {
            "home": ["owner_dashboard"],
            "pets": [
                "owner_pet_list",
                "owner_pet_add",
                "owner_pet_detail",
                "owner_pet_edit",
                "owner_pet_delete_request",
                "owner_pet_cancel_delete_request",
                "owner_pet_medical_history",
                "owner_medical_record",
                "owner_vaccination_history",
            ],
            "visits": [
                "owner_appointments",
                "owner_book_appointment",
                "owner_appointment_detail",
                "owner_appointment_cancel",
                "owner_appointment_reschedule",
            ],
            "bills": ["owner_billing_list", "owner_receipt_detail"],
            "account": [
                "owner_account",
                "owner_profile",
                "owner_profile_edit",
                "owner_notification_preferences",
                "clinic_info",
                "owner_health_article_list",
                "owner_health_article_detail",
            ],
        }
        for tab, names in expected.items():
            for name in names:
                with self.subTest(name=name):
                    self.assertEqual(owner_tab(name), tab)
        # The bell's page and anything unknown: no tab
        for name in ("owner_notifications", "admin_dashboard", "", None):
            with self.subTest(name=name):
                self.assertEqual(owner_tab(name), "")

    def test_every_mapped_name_is_a_real_url_name(self):
        from accounts.utils import OWNER_TAB_BY_URL_NAME

        known = get_resolver().reverse_dict
        for name in OWNER_TAB_BY_URL_NAME:
            with self.subTest(name=name):
                self.assertTrue(known.getlist(name))

    def test_blocks_are_all_there(self):
        source = template_source("layouts/owner.html")
        for block in (
            "title",
            "extra_head",
            "top_bar",
            "flash",
            "content",
            "bottom_bar",
            "extra_js",
        ):
            with self.subTest(block=block):
                self.assertIn("{% block " + block + " %}", source)

    def test_back_bar_is_44px_with_the_label(self):
        html = (
            engines["django"]
            .from_string(
                '{% include "layouts/owner_parts/back_bar.html" with url="/x/" '
                'label="Pets" %}'
            )
            .render({})
        )
        # Slice 8b-10b-1: the link also carries data-open-label after its class
        self.assertRegex(html, r'<a href="/x/" class="[^"]*\bmin-h-11\b[^"]*"[^>]*>')
        self.assertIn('data-open-label="Pets"', html)
        self.assertIn("Back to Pets</a>", html)
        self.assertIn("<svg", html)

    def test_the_log_out_sheet(self):
        html = self._html()
        self.assertRegex(html, r'<dialog id="logout-sheet"[^>]*>')
        self.assertIn('data-dialog-open="logout-sheet"', html)
        self.assertIn("Log out of Hapi Tutz?", html)
        form = re.search(
            r'(?s)<form method="post" action="/logout/">(.*?)</form>', html
        )
        self.assertIsNotNone(form)
        self.assertRegex(form.group(1), r'name="csrfmiddlewaretoken" value="[^"]+"')
        self.assertIn("Yes, log out", form.group(1))
        self.assertRegex(
            html,
            r'<button type="button"[^>]*data-dialog-close autofocus>Stay logged in',
        )


class BottomSheetTests(TestCase):
    def _render(self):
        return (
            engines["django"]
            .from_string(
                '{% include "components/bottom_sheet.html" with id="s1" '
                'title="Remove Bantay?" text="Plain words." action_url="/go/" '
                'action_label="Yes, remove" action_icon="trash-2" '
                'keep_label="Keep Bantay" %}'
            )
            .render({}, RequestFactory().get("/"))
        )

    def test_native_dialog_with_title_and_text(self):
        html = self._render()
        self.assertRegex(html, r'<dialog id="s1" aria-labelledby="s1-title"')
        self.assertIn(
            '<h2 id="s1-title" class="text-xl font-bold">Remove Bantay?', html
        )
        self.assertIn("Plain words.", html)

    def test_phone_sheet_and_desktop_card_classes(self):
        html = self._render()
        for cls in (
            "mt-auto",
            "w-full",
            "rounded-t-3xl",
            "sm:m-auto",
            "sm:max-w-md",
            "sm:rounded-3xl",
        ):
            with self.subTest(cls=cls):
                self.assertIn(cls, html.split(">", 1)[0])

    def test_drag_handle_is_decorative(self):
        self.assertRegex(
            self._render(), r'<span class="[^"]*h-1\.5 w-12[^"]*" aria-hidden="true">'
        )

    def test_buttons_are_stacked_and_the_same_size(self):
        html = self._render()
        self.assertEqual(html.count("min-h-14 w-full"), 2)
        self.assertRegex(html, r"data-dialog-close autofocus>Keep Bantay</button>")
        action = re.search(r'(?s)<form method="post" action="/go/">(.*?)</form>', html)
        self.assertIn("csrfmiddlewaretoken", action.group(1))
        self.assertIn("Yes, remove", action.group(1))
        self.assertIn("<svg", action.group(1))


class FlashComponentTests(_LoggedInBase):
    def _flash(self, level, text):
        request = RequestFactory().get("/")
        request.session = {}
        request._messages = FallbackStorage(request)
        request._messages.add(level, text)
        return get_template("components/flash_messages.html").render(
            {"messages": request._messages}, request
        )

    def _layout_html(self, layout, user, level, text):
        request = RequestFactory().get("/")
        request.user = user
        request.session = {}
        request._messages = FallbackStorage(request)
        request._messages.add(level, text)
        page = engines["django"].from_string(f'{{% extends "{layout}" %}}')
        return page.render({}, request)

    def test_each_level_has_its_colour_icon_and_role(self):
        levels = message_levels
        cases = (
            (levels.SUCCESS, "bg-green-50 text-green-900", "m16 9-5.5", "status"),
            (levels.INFO, "bg-blue-50 text-blue-900", "M12 16v-4", "status"),
            (levels.WARNING, "bg-yellow-50 text-yellow-900", "m21.73 18", "status"),
            (levels.ERROR, "bg-red-50 text-red-900", 'y1="8" y2="12"', "alert"),
        )
        for level, classes, icon_part, role in cases:
            with self.subTest(level=level):
                html = self._flash(level, "Done")
                self.assertIn(classes, html)
                self.assertIn(icon_part, html)
                self.assertIn(f'role="{role}"', html)
                self.assertNotIn("text-white", html)

    def test_lead_sentence_is_bold_only_with_two_sentences(self):
        two = self._flash(message_levels.SUCCESS, "Visit sent. We will confirm.")
        self.assertIn(
            '<strong class="font-semibold">Visit sent.</strong> We will confirm.',
            two,
        )
        one = self._flash(message_levels.SUCCESS, "Visit sent.")
        self.assertNotIn("<strong", one)
        self.assertIn("Visit sent.", one)
        self.assertNotIn("<strong", self._flash(message_levels.INFO, "Saved"))

    def test_message_text_is_escaped(self):
        html = self._flash(message_levels.INFO, "<b>x</b>. <i>y</i>")
        self.assertNotIn("<i>y</i>", html)
        self.assertIn("&lt;i&gt;y&lt;/i&gt;", html)

    def test_flash_lead_filter(self):
        from accounts.templatetags.ui_helpers import flash_lead

        self.assertEqual(flash_lead("A. B. C"), ("A.", "B. C"))
        self.assertEqual(flash_lead("Only one."), ("Only one.", ""))
        self.assertEqual(flash_lead("No full stop"), ("No full stop", ""))

    def test_close_button_has_an_icon_a_word_and_no_inline_handler(self):
        html = self._flash(message_levels.SUCCESS, "Done")
        button = re.search(
            r"(?s)<button[^>]*data-flash-close[^>]*>(.*?)</button>", html
        )
        self.assertIsNotNone(button)
        self.assertIn("<svg", button.group(1))
        self.assertIn('<span class="sr-only">Close</span>', button.group(1))
        self.assertIn("min-h-11 min-w-11", button.group(0))
        self.assertNotIn("onclick", html)
        self.assertNotIn(
            "setTimeout", template_source("components/flash_messages.html")
        )

    def test_flash_js_closes_without_auto_hide(self):
        source = (Path(settings.BASE_DIR) / "static/js/shared/flash.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("data-flash-close", source)
        self.assertNotIn("setTimeout", source)

    def test_both_layouts_render_it_and_load_flash_js(self):
        for layout, user in (
            ("layouts/admin.html", self.admin),
            ("layouts/owner.html", self.owner_user),
        ):
            with self.subTest(layout=layout):
                html = self._layout_html(
                    layout, user, message_levels.SUCCESS, "Saved. All good."
                )
                self.assertIn("<div data-flash", html)
                self.assertIn("data-flash-close", html)
                self.assertRegex(html, r"/static/js/shared/flash\.[0-9a-f]+\.js")

    def test_owner_flash_sits_between_the_top_bar_and_main(self):
        html = self._layout_html(
            "layouts/owner.html", self.owner_user, message_levels.INFO, "Hello"
        )
        self.assertLess(html.index("</header>"), html.index("<div data-flash"))
        self.assertLess(html.index("<div data-flash"), html.index('<main id="main"'))


class DialogsMovedTests(TestCase):
    def test_old_dialogs_path_is_not_referenced_anywhere(self):
        base = Path(settings.BASE_DIR)
        for folder in ("templates", "static/js"):
            for path in (base / folder).rglob("*"):
                if path.suffix not in (".html", ".js", ".txt"):
                    continue
                with self.subTest(file=str(path.relative_to(base))):
                    self.assertNotIn("admin/dialogs", path.read_text(encoding="utf-8"))

    def test_shared_files_exist(self):
        base = Path(settings.BASE_DIR) / "static/js/shared"
        self.assertTrue((base / "dialogs.js").is_file())
        self.assertTrue((base / "flash.js").is_file())


@override_settings(DEBUG=False)
class ErrorPageTests(_LoggedInBase):
    def _assert_page(self, html, title):
        self.assertIn(title, html)
        self.assertIn("Hapi Tutz Vet", html)
        self.assertIn('<a href="/"', html)
        self.assertIn("Go to the home page", html)
        self.assertRegex(html, r'<link rel="stylesheet" href="/static/css/app\.')
        self.assertIn("<svg", html)

    def test_a_missing_page_is_the_404_page_for_everyone(self):
        # /accounts/ is public, so a logged-out visitor gets the 404 itself
        # (any other missing URL sends them to log in first)
        cases = (
            (None, "/accounts/no-such-page/"),
            (self.owner_user, "/no-such-page/"),
            (self.admin, "/no-such-page/"),
        )
        for user, path in cases:
            with self.subTest(user=getattr(user, "username", "logged out")):
                self.client.logout()
                if user:
                    self.client.force_login(user)
                response = self.client.get(path)
                self.assertEqual(response.status_code, 404)
                self.assertTemplateUsed(response, "404.html")
                self._assert_page(response.content.decode(), "We can't find that page")

    def test_403_page(self):
        request = RequestFactory().get("/")
        response = permission_denied(request, Exception("no"))
        self.assertEqual(response.status_code, 403)
        self._assert_page(response.content.decode(), "You can't open this page")

    def test_500_page(self):
        response = server_error(RequestFactory().get("/"))
        self.assertEqual(response.status_code, 500)
        self._assert_page(response.content.decode(), "Something went wrong on our side")

    def test_500_renders_without_a_request_or_context(self):
        source = template_source("500.html")
        html = Template(source).render(Context())
        self._assert_page(html, "Something went wrong on our side")
        self._assert_page(get_template("500.html").render(), "Error 500")

    def test_error_pages_are_standalone(self):
        for name in ("404.html", "403.html", "500.html"):
            source = template_source(name)
            with self.subTest(name=name):
                self.assertNotIn("{% extends", source)
                self.assertNotIn("{% url", source)
                self.assertNotIn("shell", source)
                self.assertNotIn("request", source)
                self.assertNotIn("<script", source)
                self.assertIsNone(re.search(r"\son[a-z]+\s*=", source))
                self.assertEqual(len(re.findall(r"btn-primary", source)), 1)


# ── Slice 8b-2: owner auth pages ─────────────────────────────────────────────

AUTH_SOURCES = [
    "layouts/owner_auth.html",
    "owner/login.html",
    "owner/register.html",
    "owner/register_pending.html",
    "owner/forgot_password.html",
    "owner/forgot_password_sent.html",
    "owner/reset_password.html",
    "owner/reset_password_complete.html",
    "owner/verify_email_invalid.html",
    "owner/auth_parts/alert_box.html",
    "owner/auth_parts/email_field.html",
    "owner/auth_parts/google_button.html",
    "owner/auth_parts/or_line.html",
    "owner/auth_parts/password_field.html",
    "owner/auth_parts/password_rules.html",
    "socialaccount/login.html",
    "socialaccount/authentication_error.html",
    "socialaccount/login_cancelled.html",
]
BACK_TO_LOGIN = "Back to log in"
FORGOT_HINT = "Check for typos, or tap “Forgot password?” to make a new one."


def reset_urls(user):
    """The emailed reset link and the set-password page it redirects to."""
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    return (
        f"/reset-password/{uid}/{token}/",
        f"/reset-password/{uid}/set-password/",
    )


class AuthPagesBase(TestCase):
    """Every owner auth page, as (name, url, count of violet buttons)."""

    def setUp(self):
        cache.clear()
        self.user = make_owner("ana@example.com", is_active=True)
        link, _ = reset_urls(self.user)
        self.pages = [
            ("login", reverse("owner_login"), 1),
            ("register", reverse("owner_register"), 1),
            ("check email", reverse("owner_register_pending"), 0),
            ("forgot", reverse("forgot_password"), 1),
            ("forgot sent", reverse("password_reset_done"), 0),
            ("new password", link, 1),
            ("bad link", "/reset-password/bad/bad/", 1),
            ("done", reverse("password_reset_complete"), 1),
            ("bad verification link", "/verify-email/bad/bad/", 1),
            ("google confirm", reverse("google_login"), 1),
            ("google error", reverse("socialaccount_login_error"), 1),
            ("google cancelled", reverse("socialaccount_login_cancelled"), 1),
        ]

    def html(self, url):
        return self.client.get(url, follow=True).content.decode()


class OwnerAuthLayoutTests(AuthPagesBase):
    def test_every_page_extends_the_owner_auth_layout(self):
        for name, url, _ in self.pages:
            with self.subTest(page=name):
                response = self.client.get(url, follow=True)
                # allauth answers its error page with 401
                expected = 401 if name == "google error" else 200
                self.assertEqual(response.status_code, expected)
                self.assertTemplateUsed(response, "layouts/owner_auth.html")
                self.assertTemplateNotUsed(response, "base.html")
                self.assertTemplateNotUsed(response, "layouts/auth.html")

    def test_loads_app_css_flash_and_password_toggle_only(self):
        for name, url, _ in self.pages:
            html = self.html(url)
            with self.subTest(page=name):
                for pattern in (
                    r'<link rel="stylesheet" href="/static/css/app\.[0-9a-f]+\.css">',
                    r'<script src="/static/js/shared/flash\.[0-9a-f]+\.js" defer>',
                    r'<script src="/static/js/shared/password_toggle\.[0-9a-f]+\.js" '
                    r"defer>",
                    # Slice 8b-10b-1: the loading states script joined the layout
                    r'<script src="/static/js/owner/loading\.[0-9a-f]+\.js" defer>',
                ):
                    self.assertRegex(html, pattern)
                self.assertEqual(len(re.findall(r"<script", html)), 3)

    def test_no_cdn_font_import_or_legacy_css(self):
        for name, url, _ in self.pages:
            html = self.html(url)
            with self.subTest(page=name):
                for word in (
                    "cdn.tailwindcss.com",
                    "Poppins",
                    "fonts.googleapis",
                    "@import",
                    "css/owner/",
                    "css/shared/",
                    "layer(legacy)",
                    "auth-page",
                ):
                    self.assertNotIn(word, html)

    def test_violet_band_with_the_logo_tile_and_the_card(self):
        for name, url, _ in self.pages:
            html = self.html(url)
            with self.subTest(page=name):
                self.assertIn("bg-violet-700", html)
                self.assertIn("Hapi Tutz Vet", html)
                self.assertRegex(html, r'rounded-2xl bg-white[^"]*">\s*<svg')
                self.assertIn("max-w-md", html)
                self.assertNotIn("<nav", html)  # no bottom tabs

    def test_no_staff_or_admin_link(self):
        for name, url, _ in self.pages:
            html = self.html(url)
            with self.subTest(page=name):
                self.assertNotIn(reverse("admin_login"), html)
                self.assertNotIn("/admin", html)

    def test_at_most_one_violet_button_per_page(self):
        for name, url, count in self.pages:
            with self.subTest(page=name):
                self.assertEqual(self.html(url).count("btn-primary"), count)

    def test_no_inline_script_or_handlers_in_pages_or_sources(self):
        for name, url, _ in self.pages:
            html = self.html(url)
            with self.subTest(page=name):
                self.assertIsNone(re.search(r"<script(?![^>]*\ssrc=)", html))
        for name in AUTH_SOURCES:
            source = template_source(name)
            with self.subTest(file=name):
                self.assertIsNone(re.search(r"\son[a-z]+\s*=", source))
                self.assertNotIn("hx-confirm", source)
                self.assertNotIn("<!--", source)

    def test_every_link_in_the_sources_is_on_one_line(self):
        for name in AUTH_SOURCES:
            for link in re.findall(r"(?s)<a\b.*?</a>", template_source(name)):
                with self.subTest(file=name, link=link[:40]):
                    self.assertNotIn("\n", link)

    def test_the_admin_login_still_uses_its_own_layout(self):
        response = self.client.get(reverse("admin_login"))
        self.assertTemplateUsed(response, "layouts/auth.html")
        self.assertTemplateNotUsed(response, "layouts/owner_auth.html")


class LoginPageStatesTests(TestCase):
    def setUp(self):
        cache.clear()
        self.url = reverse("owner_login")

    def _login(self, email, password=PENDING_PASSWORD):
        return self.client.post(self.url, {"email": email, "password": password})

    def test_logged_out_box_after_a_post_logout_for_owner_and_admin(self):
        owner = make_owner("ana@example.com", is_active=True)
        admin = make_owner("boss@example.com", is_active=True, role=User.ADMIN)
        for user, page in ((owner, "owner_login"), (admin, "admin_login")):
            with self.subTest(user=user.email):
                self.client.force_login(user)
                response = self.client.post(reverse("logout"), follow=True)
                self.assertRedirects(response, reverse(page))
                self.assertContains(response, "You&#x27;re logged out.")
                self.assertContains(response, "border-green-200")

    def test_the_owner_login_shows_the_logged_out_box_as_a_status(self):
        make_owner("ana@example.com", is_active=True)
        self.client.force_login(User.objects.get(email="ana@example.com"))
        response = self.client.post(reverse("logout"), follow=True)
        self.assertRegex(
            response.content.decode(),
            r'(?s)role="status"[^>]*>.*?You&#x27;re logged out\.',
        )
        self.assertNotContains(response, GENERIC_LOGIN_ERROR)

    def test_the_wrong_box_for_every_generic_failure_keeps_the_email(self):
        make_owner("pending@example.com")
        archive_owner(make_owner("closed@example.com"))
        make_owner("google@example.com", password=None)
        make_owner("back@example.com", last_login=timezone.now())
        make_owner("boss@example.com", role=User.ADMIN)
        make_owner("ana@example.com", is_active=True)
        cases = {
            "unknown email": ("nobody@example.com", PENDING_PASSWORD),
            "wrong password": ("ana@example.com", "wrong-password-1"),
            "inactive, wrong password": ("pending@example.com", "wrong-password-1"),
            "archived": ("closed@example.com", PENDING_PASSWORD),
            "walk-in or Google": ("google@example.com", PENDING_PASSWORD),
            "not pending": ("back@example.com", PENDING_PASSWORD),
            "inactive admin": ("boss@example.com", PENDING_PASSWORD),
        }
        for name, (email, password) in cases.items():
            with self.subTest(case=name):
                response = self._login(email, password)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, GENERIC_LOGIN_ERROR)
                self.assertContains(response, FORGOT_HINT)
                self.assertContains(response, 'role="alert"')
                self.assertContains(response, "border-red-200")
                self.assertContains(response, f'value="{email}"')
                self.assertNotContains(response, "Please confirm your email first")
                self.assertNotIn("_auth_user_id", self.client.session)

    def test_the_failure_is_a_box_not_a_flash_message(self):
        response = self._login("nobody@example.com")
        self.assertEqual(list(get_messages(response.wsgi_request)), [])
        self.assertTrue(response.context["login_failed"])

    def test_not_confirmed_box_only_for_pending_owner_and_right_password(self):
        make_owner("pending@example.com")
        archive_owner(make_owner("closed@example.com"))
        right = self._login("pending@example.com")
        self.assertContains(right, "Please confirm your email first")
        self.assertContains(right, "border-blue-200")
        self.assertContains(right, "Send the link again")
        self.assertContains(
            right,
            "We sent a link to pending@example.com when you signed up. "
            "Open it, then log in here.",
        )
        self.assertNotContains(right, GENERIC_LOGIN_ERROR)
        for email, password in (
            ("pending@example.com", "wrong-password-1"),
            ("closed@example.com", PENDING_PASSWORD),
            ("nobody@example.com", PENDING_PASSWORD),
        ):
            with self.subTest(email=email):
                response = self._login(email, password)
                self.assertNotContains(response, "Please confirm your email first")
                self.assertNotContains(response, "Send the link again")

    def test_the_admin_message_is_unchanged(self):
        make_owner("boss@example.com", role=User.ADMIN, is_active=True)
        response = self._login("boss@example.com")
        self.assertContains(response, "Please use the admin login page.")
        self.assertContains(response, 'value="boss@example.com"')
        self.assertNotContains(response, GENERIC_LOGIN_ERROR)

    def test_the_page_has_its_parts_and_no_staff_link(self):
        html = self.client.get(self.url).content.decode()
        for text in (
            "Welcome back",
            "Log in to see your pets, visits and bills.",
            "Email address",
            "Forgot password?",
            "Keep me logged in",
            "Continue with Google",
            "New here?",
            "Create an account",
        ):
            with self.subTest(text=text):
                self.assertIn(text, html)
        self.assertNotIn("Privacy Policy", html)
        self.assertNotIn("Terms", html)

    def test_the_unreachable_not_verified_branch_is_gone(self):
        source = (Path(settings.BASE_DIR) / "accounts" / "views.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("not verified yet", source)
        self.assertNotIn("Invalid email or password", source)
        self.assertNotIn("remember_me", source)


class KeepMeLoggedInTests(TestCase):
    def setUp(self):
        self.user = make_owner("ana@example.com", is_active=True, is_onboarded=True)
        self.url = reverse("owner_login")

    def _login(self, **extra):
        data = {"email": "ana@example.com", "password": PENDING_PASSWORD}
        data.update(extra)
        return self.client.post(self.url, data)

    def test_unticked_ends_the_session_when_the_browser_closes(self):
        response = self._login()
        self.assertRedirects(
            response, reverse("owner_dashboard"), fetch_redirect_response=False
        )
        self.assertTrue(self.client.session.get_expire_at_browser_close())

    def test_ticked_keeps_the_session_for_30_days_as_the_old_remember_me(self):
        self._login(remember="on")
        self.assertIn("_auth_user_id", self.client.session)
        self.assertFalse(self.client.session.get_expire_at_browser_close())
        age = self.client.session.get_expiry_age()
        self.assertAlmostEqual(age, 60 * 60 * 24 * 30, delta=5)

    def test_the_box_starts_unticked(self):
        response = self.client.get(self.url)
        html = response.content.decode()
        self.assertRegex(html, r'<input type="checkbox" name="remember"[^>]*>')
        self.assertNotRegex(html, r'name="remember"[^>]* checked')
        self.assertFalse(response.context["remember"])

    def test_a_failed_login_keeps_the_choice(self):
        data = {"email": "ana@example.com", "password": "wrong-password-1"}
        unticked = self.client.post(self.url, data).content.decode()
        self.assertNotRegex(unticked, r'name="remember"[^>]* checked')
        ticked = self.client.post(self.url, {**data, "remember": "on"})
        self.assertRegex(ticked.content.decode(), r'name="remember"[^>]* checked')


class PasswordFieldTests(AuthPagesBase):
    def _boxes(self, url):
        html = self.html(url)
        return html, re.findall(r'<input type="password" id="([^"]+)"', html)

    def test_every_password_box_has_the_eye_button_inside_it(self):
        pages = {
            "login": (reverse("owner_login"), 1),
            "register": (reverse("owner_register"), 2),
            "new password": (self.pages[5][1], 2),
        }
        for name, (url, count) in pages.items():
            html, ids = self._boxes(url)
            with self.subTest(page=name):
                self.assertEqual(len(ids), count)
                for box in ids:
                    button = re.search(
                        r'(?s)<button type="button" data-password-toggle '
                        rf'aria-controls="{box}" aria-pressed="false" '
                        r'class="([^"]*)">(.*?)</button>',
                        html,
                    )
                    self.assertIsNotNone(button, box)
                    self.assertIn("size-11", button.group(1))
                    self.assertIn("absolute", button.group(1))
                    inner = button.group(2)
                    self.assertEqual(inner.count("<svg"), 2)
                    self.assertIn("data-label>Show password</span>", inner)
                    self.assertIn('class="sr-only"', inner)
                    self.assertIn('data-icon-hide class="hidden"', inner)

    def test_each_box_has_a_label_and_the_lock_icon(self):
        html, ids = self._boxes(reverse("owner_register"))
        for box in ids:
            with self.subTest(box=box):
                self.assertIn(f'<label for="{box}"', html)

    def test_errors_show_under_the_box_with_the_alert_icon(self):
        response = self.client.post(
            reverse("owner_register"),
            {"email": "x@example.com", "password1": "1234", "password2": "12345"},
        )
        html = response.content.decode()
        self.assertRegex(
            html, r'(?s)id="id_password1-errors">\s*<p class="[^"]*text-red-700'
        )
        self.assertIn('aria-invalid="true"', html)
        self.assertIn("Passwords do not match.", html)

    def test_the_script_toggles_only_the_password_boxes(self):
        path = Path(settings.BASE_DIR) / "static/js/shared/password_toggle.js"
        source = path.read_text(encoding="utf-8")
        for text in ("data-password-toggle", "Hide password", "Show password"):
            self.assertIn(text, source)
        self.assertNotIn("onclick", source)
        self.assertNotIn("fetch(", source)
        field = template_source("owner/auth_parts/password_field.html")
        self.assertIn("eye-off", field)


class GoogleButtonTests(AuthPagesBase):
    def _form(self, url):
        action = re.escape(reverse("google_login"))
        html = self.html(url)
        found = re.search(
            rf'<form method="post" action="{action}"[^>]*>(.*?)</form>', html, re.S
        )
        self.assertIsNotNone(found, "Google POST form not found")
        return found.group(1)

    def test_post_form_with_csrf_the_svg_and_the_wording(self):
        cases = (
            (reverse("owner_login"), "Continue with Google", "Sign up with Google"),
            (reverse("owner_register"), "Sign up with Google", "Continue with Google"),
        )
        for url, shown, hidden in cases:
            form = self._form(url)
            with self.subTest(page=url):
                self.assertIn('name="csrfmiddlewaretoken"', form)
                self.assertRegex(
                    form, r'<img src="/static/img/google_g\.[0-9a-f]+\.svg" alt=""'
                )
                self.assertIn(f"<span>{shown}</span>", form)
                self.assertNotIn(hidden, self.html(url))
                self.assertIn("border-[#747775]", form)
                self.assertIn("bg-white", form)

    def test_it_is_never_a_link(self):
        for url in (reverse("owner_login"), reverse("owner_register")):
            self.assertNotIn(f'href="{reverse("google_login")}"', self.html(url))

    def test_the_svg_keeps_only_the_coloured_g(self):
        path = Path(settings.BASE_DIR) / "static/img/google_g.svg"
        source = path.read_text(encoding="utf-8")
        self.assertIn('viewBox="10 10 20 20"', source)
        self.assertNotIn('fill="white"', source)
        self.assertNotIn('stroke="#747775"', source)
        self.assertNotIn("H36C37.933", source)
        self.assertIn('mask id="mask0_1298_12516"', source)
        self.assertIn("conic-gradient", source)

    def test_the_old_google_files_are_not_loaded(self):
        for url in (reverse("owner_login"), reverse("owner_register")):
            self.assertNotIn("gsi-material-button", self.html(url))
            self.assertNotIn("google-button.css", self.html(url))


class RegisterPageTests(TestCase):
    def setUp(self):
        self.url = reverse("owner_register")

    def test_fields_hints_and_the_footer(self):
        html = self.client.get(self.url).content.decode()
        for name in ("email", "password1", "password2"):
            self.assertIn(f'name="{name}"', html)
        for text in (
            "Create an account",
            "For pet owners of Hapi Tutz. It's free.",
            "Email address",
            "Type the password again",
            "At least 8 characters",
            "Not only numbers",
            'Not too common (like "password123")',
            "Already have an account?",
            ">Log in</a>",
            ">Create account</button>",
        ):
            with self.subTest(text=text):
                self.assertIn(text, html)
        self.assertIn('<ul class="mt-2 list-disc', html)
        self.assertEqual(html.count("At least 8 characters"), 1)

    def test_google_comes_first_then_the_or_line_then_the_form(self):
        html = self.client.get(self.url).content.decode()
        google = html.index("Sign up with Google")
        self.assertLess(google, html.index(">or<"))
        self.assertLess(html.index(">or<"), html.index('name="email"'))

    def test_terms_and_privacy_open_in_a_new_tab_with_the_settings_urls(self):
        with override_settings(
            TERMS_URL="https://example.test/terms",
            PRIVACY_URL="https://example.test/privacy",
        ):
            html = self.client.get(self.url).content.decode()
        for href, label in (
            ("https://example.test/terms", "Terms"),
            ("https://example.test/privacy", "Privacy Policy"),
        ):
            with self.subTest(label=label):
                self.assertIn(
                    f'<a href="{href}" target="_blank" rel="noopener" '
                    f'class="font-medium text-blue-600 underline underline-offset-2">'
                    f'{label}<span class="sr-only"> (opens in a new tab)</span></a>',
                    html,
                )

    def test_terms_are_on_this_page_only(self):
        for name in (
            "owner_login",
            "owner_register_pending",
            "forgot_password",
            "password_reset_done",
            "password_reset_complete",
            "socialaccount_login_error",
        ):
            html = self.client.get(reverse(name)).content.decode()
            with self.subTest(page=name):
                self.assertNotIn(settings.TERMS_URL, html)
                self.assertNotIn(settings.PRIVACY_URL, html)
                self.assertNotIn("Privacy Policy", html)

    def test_errors_show_under_each_field_and_the_email_is_kept(self):
        make_owner("taken@example.com")
        response = self.client.post(
            self.url,
            {
                "email": "taken@example.com",
                "password1": "Str0ng-pass-123",
                "password2": "Different-pass-9",
            },
        )
        html = response.content.decode()
        self.assertEqual(response.status_code, 200)
        self.assertIn('value="taken@example.com"', html)
        email_part = html.split('id="id_email-errors"')[1].split("</div>")[0]
        self.assertIn("An account with this email already exists.", email_part)
        part = html.split('id="id_password2-errors"')[1].split("</div>")[0]
        self.assertIn("Passwords do not match.", part)
        self.assertEqual(User.objects.filter(email="taken@example.com").count(), 1)

    def test_a_good_post_still_registers_and_sends_the_email(self):
        data = {
            "email": "New.Owner@Example.com",
            "password1": PENDING_PASSWORD,
            "password2": PENDING_PASSWORD,
        }
        response = self.client.post(self.url, data)
        self.assertRedirects(
            response, reverse("owner_register_pending"), fetch_redirect_response=False
        )
        self.assertEqual(len(mail.outbox), 1)
        self.assertFalse(User.objects.get(email="new.owner@example.com").is_active)


class CheckEmailPageTests(TestCase):
    def setUp(self):
        cache.clear()
        self.url = reverse("owner_register_pending")

    def _register(self, email="new.owner@example.com"):
        return self.client.post(
            reverse("owner_register"),
            {
                "email": email,
                "password1": PENDING_PASSWORD,
                "password2": PENDING_PASSWORD,
            },
        )

    def test_registering_stores_only_the_address_in_the_session(self):
        self._register("New.Owner@Example.com")
        self.assertEqual(self.client.session["pending_email"], "new.owner@example.com")
        self.assertNotIn(PENDING_PASSWORD, str(dict(self.client.session)))

    def test_the_address_is_shown_bold_with_a_hidden_resend_email(self):
        self._register()
        html = self.client.get(self.url).content.decode()
        self.assertIn("We sent a link to</p>", html)
        self.assertIn(
            '<p class="break-all text-lg font-bold text-gray-900">'
            "new.owner@example.com</p>",
            html,
        )
        hidden = '<input type="hidden" name="email" value="new.owner@example.com">'
        self.assertIn(hidden, html)
        self.assertNotIn('type="email"', html)
        self.assertIn("Open the email and tap the link", html)
        self.assertIn("Check Spam too.", html)

    def test_without_the_session_a_visible_email_field_is_shown(self):
        html = self.client.get(self.url).content.decode()
        self.assertIn('type="email" id="resend-email" name="email"', html)
        self.assertIn(">Email address</label>", html)
        self.assertNotIn('type="hidden" name="email"', html)

    def test_the_footer_and_the_resend_button(self):
        html = self.client.get(self.url).content.decode()
        self.assertIn("Wrong email?", html)
        self.assertIn(f'href="{reverse("owner_register")}"', html)
        self.assertIn(">Sign up again</a>", html)
        self.assertIn("Send the link again", html)
        self.assertIn(f'action="{reverse("owner_resend_verification")}"', html)
        self.assertIn("csrfmiddlewaretoken", html)

    def test_the_resend_reply_is_unchanged_with_and_without_the_session(self):
        make_owner("waiting@example.com")
        expected = (302, self.url, [("info", RESEND_REPLY_TEXT)])
        post = {"email": "waiting@example.com"}
        url = reverse("owner_resend_verification")
        self.assertEqual(reply_of(self.client.post(url, post)), expected)
        other = self.client_class()
        reply = other.post(url, {"email": "nobody@example.com"})
        self.assertEqual(reply_of(reply), expected)
        self.assertEqual(len(mail.outbox), 1)

    def test_the_reply_message_shows_in_the_card(self):
        self._register()
        response = self.client.post(
            reverse("owner_resend_verification"),
            {"email": "new.owner@example.com"},
            follow=True,
        )
        self.assertContains(response, 'role="status"')
        self.assertContains(response, "new.owner@example.com")


class ResetPagesTests(TestCase):
    def setUp(self):
        self.user = make_owner("ana@example.com", is_active=True)

    def test_forgot_password_wording_and_back_link(self):
        response = self.client.get(reverse("forgot_password"))
        self.assertTemplateUsed(response, "layouts/owner_auth.html")
        html = response.content.decode()
        for text in (
            "Forgot your password?",
            "Type your email and we'll send you a link to make a new one.",
            "Email address",
            ">Send me a link</button>",
            "Back to log in</a>",
        ):
            with self.subTest(text=text):
                self.assertIn(text, html)
        self.assertRegex(html, rf'<a href="{reverse("owner_login")}"[^>]*>\s*<svg')

    def test_forgot_password_still_sends_and_goes_to_the_sent_page(self):
        response = self.client.post(
            reverse("forgot_password"), {"email": "ana@example.com"}
        )
        self.assertRedirects(
            response, reverse("password_reset_done"), fetch_redirect_response=False
        )
        self.assertEqual(len(mail.outbox), 1)

    def test_a_bad_email_shows_the_error_under_the_box(self):
        response = self.client.post(reverse("forgot_password"), {"email": "nope"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="id_email-errors"')
        self.assertContains(response, 'value="nope"')

    def test_the_sent_page(self):
        response = self.client.get(reverse("password_reset_done"))
        self.assertTemplateUsed(response, "layouts/owner_auth.html")
        for text in ("Check your email", "If that email has an account", BACK_TO_LOGIN):
            self.assertContains(response, text)

    def test_new_password_page_and_the_whole_reset(self):
        link, set_url = reset_urls(self.user)
        response = self.client.get(link, follow=True)
        self.assertRedirects(response, set_url)
        self.assertTemplateUsed(response, "layouts/owner_auth.html")
        html = response.content.decode()
        for text in (
            "Make a new password",
            "For ana@example.com",
            "New password",
            "Type it again",
            "At least 8 characters",
            ">Save new password</button>",
        ):
            with self.subTest(text=text):
                self.assertIn(text, html)
        self.assertEqual(len(re.findall(r'<input type="password"', html)), 2)
        new = "Br4nd-new-pass-77"
        done = self.client.post(set_url, {"new_password1": new, "new_password2": new})
        self.assertRedirects(
            done, reverse("password_reset_complete"), fetch_redirect_response=False
        )
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(new))

    def test_new_password_errors_show_under_the_boxes(self):
        link, set_url = reset_urls(self.user)
        self.client.get(link)
        # Django's form puts both the mismatch and the weak-password errors
        # on the second box
        for first, second in (("1234", "5678"), ("1234", "1234")):
            with self.subTest(first=first, second=second):
                response = self.client.post(
                    set_url, {"new_password1": first, "new_password2": second}
                )
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'id="id_new_password2-errors"')
                self.assertContains(response, 'aria-invalid="true"')

    def test_a_bad_link_shows_no_address(self):
        response = self.client.get("/reset-password/bad/bad/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "This link doesn't work")
        self.assertContains(response, "Ask for a new link")
        self.assertNotContains(response, "ana@example.com")

    def test_the_done_page(self):
        response = self.client.get(reverse("password_reset_complete"))
        self.assertTemplateUsed(response, "layouts/owner_auth.html")
        self.assertContains(response, "Password changed")
        self.assertContains(response, "You can now log in with your new password.")
        self.assertContains(response, ">Go to log in</a>")
        self.assertContains(response, f'href="{reverse("owner_login")}"')


class InvalidVerificationLinkPageTests(TestCase):
    def test_the_page_is_on_the_auth_layout_with_plain_words(self):
        response = self.client.get("/verify-email/bad/bad/")
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "owner/verify_email_invalid.html")
        self.assertTemplateUsed(response, "layouts/owner_auth.html")
        for text in (
            "This link doesn't work",
            "This confirmation link can't be used.",
            "It may have been used already, be too old, or be copied wrongly.",
            "Register again</a>",
            BACK_TO_LOGIN,
        ):
            with self.subTest(text=text):
                self.assertContains(response, text)
        self.assertContains(response, 'role="alert"')
        self.assertContains(response, f'href="{reverse("owner_register")}"')
        self.assertContains(response, f'href="{reverse("owner_login")}"')
        self.assertNotContains(response, "auth-page")


class AuthLogoTests(TestCase):
    """The clinic logo on logged-out pages comes from the context processor."""

    PAW = r'rounded-2xl bg-white[^"]*">\s*<svg'

    def _clinic(self, logo=""):
        from appointments.utils import CLINIC_SETTINGS_DEFAULTS

        clinic = ClinicSettings.objects.create(**CLINIC_SETTINGS_DEFAULTS)
        if logo:
            ClinicSettings.objects.filter(pk=clinic.pk).update(logo=logo)
        return ClinicSettings.objects.get(pk=clinic.pk)

    def test_the_logo_shows_in_the_white_tile_when_there_is_one(self):
        clinic = self._clinic("clinic/logo.png")
        html = self.client.get(reverse("owner_login")).content.decode()
        self.assertIn(
            f'<img src="{clinic.logo.url}" alt="" class="size-12 object-contain">',
            html,
        )
        self.assertNotRegex(html, self.PAW)

    def test_the_paw_tile_when_the_clinic_has_no_logo_or_no_row(self):
        for make_row in (True, False):
            with self.subTest(row=make_row):
                ClinicSettings.objects.all().delete()
                if make_row:
                    self._clinic()
                html = self.client.get(reverse("owner_login")).content.decode()
                self.assertRegex(html, self.PAW)
                self.assertNotIn("size-12 object-contain", html)

    def test_one_query_and_no_row_is_ever_created(self):
        from django.test.utils import CaptureQueriesContext

        table = ClinicSettings._meta.db_table
        with CaptureQueriesContext(connection) as queries:
            self.client.get(reverse("owner_login"))
        asked = [q for q in queries if f'FROM "{table}"' in q["sql"]]
        self.assertEqual(len(asked), 1)
        self.assertEqual(ClinicSettings.objects.count(), 0)

    def test_only_logged_out_pages_get_auth_shell(self):
        response = self.client.get(reverse("owner_login"))
        self.assertIn("auth_shell", response.context)
        self.assertNotIn("shell", response.context)
        user = make_owner("ana@example.com", is_active=True, is_onboarded=True)
        PetOwner.objects.create(user=user, first_name="Ana", last_name="Cruz")
        self.client.force_login(user)
        response = self.client.get(reverse("owner_account"))
        self.assertIn("shell", response.context)
        self.assertNotIn("auth_shell", response.context)

    def test_the_admin_login_and_owner_layout_do_not_change(self):
        clinic = self._clinic("clinic/logo.png")
        response = self.client.get(reverse("admin_login"))
        self.assertTemplateUsed(response, "layouts/auth.html")
        self.assertTemplateNotUsed(response, "layouts/owner_auth.html")
        user = make_owner("ana@example.com", is_active=True, is_onboarded=True)
        PetOwner.objects.create(user=user, first_name="Ana", last_name="Cruz")
        self.client.force_login(user)
        response = self.client.get(reverse("owner_account"))
        self.assertTemplateUsed(response, "layouts/owner.html")
        self.assertContains(response, f'<img src="{clinic.logo.url}" alt=""')


class SocialaccountTemplateTests(TestCase):
    def test_the_confirmation_page(self):
        response = self.client.get(reverse("google_login"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "socialaccount/login.html")
        self.assertTemplateUsed(response, "layouts/owner_auth.html")
        html = response.content.decode()
        self.assertIn("Continue with Google?", html)
        self.assertIn(BACK_TO_LOGIN, html)
        form = re.search(r'(?s)<form method="post">(.*?)</form>', html)
        self.assertIsNotNone(form)
        self.assertIn('name="csrfmiddlewaretoken"', form.group(1))
        self.assertIn(">Continue</button>", form.group(1))
        self.assertNotIn('method="get"', html)

    def test_the_authentication_error_page(self):
        response = self.client.get(reverse("socialaccount_login_error"))
        self.assertTemplateUsed(response, "socialaccount/authentication_error.html")
        self.assertTemplateUsed(response, "layouts/owner_auth.html")
        # allauth itself answers this page with HTTP 401
        found = {"status_code": 401}
        self.assertContains(response, "Google sign-in didn't work.", **found)
        self.assertContains(
            response, "Please try again, or log in with your email.", **found
        )
        self.assertContains(response, 'role="alert"', **found)
        self.assertContains(response, BACK_TO_LOGIN, **found)
        self.assertNotContains(response, "Third-Party", **found)

    def test_the_login_cancelled_page(self):
        response = self.client.get(reverse("socialaccount_login_cancelled"))
        self.assertTemplateUsed(response, "socialaccount/login_cancelled.html")
        self.assertTemplateUsed(response, "layouts/owner_auth.html")
        self.assertContains(response, "You cancelled Google sign-in.")
        self.assertContains(response, 'role="status"')
        self.assertContains(response, "border-blue-200")
        self.assertContains(response, BACK_TO_LOGIN)
        self.assertNotContains(response, "Login Cancelled")


# ── Slice 8b-9a: the Account hub, designed ───────────────────────────────────


class OwnerAccountHubV2Tests(_LoggedInBase):
    """/owner/account/ as designed (owner_account_v1.html, frame 1)."""

    PARTS = ["owner/account/index.html", "owner/account_parts/link_row.html"]
    VIOLET_BUTTON = r"<(?:a|button)\b[^>]*\bbg-violet-600\b[^>]*>"

    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner_user)
        self.url = reverse("owner_account")

    def _html(self):
        return self.client.get(self.url).content.decode()

    def test_the_account_tab_is_the_only_one_lit(self):
        html = self._html()
        nav = re.search(r'(?s)<nav aria-label="Main"[^>]*>(.*?)</nav>', html).group(1)
        self.assertEqual(nav.count('aria-current="page"'), 1)
        current = re.search(r'<a href="([^"]+)"[^>]*aria-current="page"', nav)
        self.assertEqual(current.group(1), reverse("owner_account"))
        heading = '<h1 class="text-2xl font-bold text-gray-900">Account</h1>'
        self.assertIn(heading, html)

    def test_the_profile_card_is_one_link_to_my_details(self):
        html = self._html()
        edit = reverse("owner_profile_edit")
        card = re.search(
            rf'(?s)<a href="{edit}" class="flex items-center gap-4 rounded-2xl '
            rf'border border-gray-200 bg-white p-4 shadow-sm[^"]*">(.*?)</a>',
            html,
        )
        self.assertIsNotNone(card)
        inside = card.group(1)
        self.assertIn("bg-violet-50", inside)  # the initials circle
        self.assertIn(">AC</span>", inside)
        self.assertIn('text-xl font-bold text-gray-900">Ana Cruz</span>', inside)
        email = 'truncate text-base text-gray-600">ana@example.com</span>'
        self.assertIn(email, inside)
        self.assertIn("Edit my details", inside)
        self.assertIn("text-blue-600", inside)
        self.assertIn("<svg", inside)  # the chevron

    def test_the_profile_card_shows_the_photo_when_there_is_one(self):
        PetOwner.objects.filter(user=self.owner_user).update(
            profile_photo="owners/ana.png"
        )
        photo = PetOwner.objects.get(user=self.owner_user).profile_photo
        edit = reverse("owner_profile_edit")
        card = re.search(rf'(?s)<a href="{edit}"[^>]*>(.*?)</a>', self._html())
        self.assertIn(f'<img src="{photo.url}"', card.group(1))
        self.assertNotIn(">AC</span>", card.group(1))

    def test_the_groups_and_their_rows(self):
        html = self._html()
        mine = "owner_profile_edit", "owner_notification_preferences"
        clinic = "clinic_info", "owner_health_article_list"
        groups = (
            ("account-mine", "My account", mine),
            ("account-clinic", "The clinic", clinic),
        )
        rows = {
            "owner_profile_edit": ("My details", "Name, phone, address, photo"),
            "owner_notification_preferences": (
                "Email notifications",
                "Which emails you get, and when",
            ),
            "clinic_info": ("Clinic info", "Hours, phone, and your vet"),
            "owner_health_article_list": (
                "Health library",
                "Common pet illnesses and care",
            ),
        }
        for key, heading, names in groups:
            pattern = rf'(?s)<section aria-labelledby="{key}">(.*?)</section>'
            section = re.search(pattern, html).group(1)
            title = f'class="mb-2 px-1 text-base font-semibold text-gray-600">{heading}'
            self.assertIn(f"{title}</h2>", section)
            self.assertIn("divide-y divide-gray-100", section)
            self.assertEqual(section.count('class="flex min-h-16'), len(names))
            for name in names:
                label, sub = rows[name]
                with self.subTest(row=label):
                    link = rf'(?s)<a href="{reverse(name)}" class="flex min-h-16[^"]*">'
                    row = re.search(link + "(.*?)</a>", section).group(1)
                    self.assertIn(f">{label}</span>", row)
                    self.assertIn(f">{sub}</span>", row)
                    self.assertEqual(row.count("<svg"), 2)  # the tile and a chevron

    def test_no_password_row_while_there_is_no_password_page(self):
        html = self._html()
        self.assertNotIn("Sign-in and password", html)
        self.assertNotIn("Change your password", html)
        self.assertNotIn("/accounts/password/", html)

    def test_the_log_out_row_opens_the_sheet(self):
        html = self._html()
        row = re.search(
            r'(?s)<button type="button" class="([^"]*)" '
            r'data-dialog-open="logout-sheet">(.*?)</button>',
            html,
        )
        self.assertIsNotNone(row)
        self.assertIn("min-h-16", row.group(1))
        self.assertIn("text-red-700", row.group(2))
        self.assertIn(">Log out</span>", row.group(2))
        self.assertEqual(row.group(2).count("<svg"), 1)  # no chevron

    def test_the_log_out_sheet_wording_form_and_buttons(self):
        html = self._html()
        sheet = re.search(r'(?s)<dialog id="logout-sheet".*?</dialog>', html).group(0)
        self.assertIn(">Log out of Hapi Tutz?</h2>", sheet)
        self.assertIn(
            "You'll need your email and password (or Google) to sign in again.", sheet
        )
        action = reverse("logout")
        pattern = rf'(?s)<form method="post" action="{action}">(.*?)</form>'
        form = re.search(pattern, sheet).group(1)
        self.assertIn('name="csrfmiddlewaretoken"', form)
        self.assertIn("bg-red-600", form)
        self.assertIn("Yes, log out", form)
        stay = r'<button type="button" class="([^"]*)" data-dialog-close'
        keep = re.search(stay, sheet)
        yes = re.search(r'<button type="submit" class="([^"]*)"', form)
        self.assertIn(">Stay logged in</button>", sheet)
        # Same size: the same height, width, shape and text size
        for classes in (keep.group(1), yes.group(1)):
            for word in ("min-h-14", "w-full", "rounded-2xl", "text-lg"):
                self.assertIn(word, classes.split())

    def test_logging_out_is_still_a_post_that_needs_the_token(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner_user)
        self.assertEqual(client.post(reverse("logout")).status_code, 403)
        self.assertIn("_auth_user_id", client.session)
        self.assertNotIn(f'href="{reverse("logout")}"', self._html())

    def test_no_violet_button_on_the_hub(self):
        self.assertEqual(re.findall(self.VIOLET_BUTTON, self._html()), [])

    def test_every_a_tag_is_on_one_line(self):
        for name in self.PARTS:
            for link in re.findall(r"(?s)<a\b.*?</a>", template_source(name)):
                with self.subTest(file=name, link=link[:50]):
                    self.assertNotIn("\n", link)

    def test_no_inline_script_or_handlers(self):
        html = self._html()
        self.assertIsNone(re.search(r"<script(?![^>]*\ssrc=)[^>]*>", html))
        self.assertIsNone(re.search(r"\son[a-z]+\s*=", html))
        for name in self.PARTS:
            source = template_source(name)
            for banned in ("<script", "hx-confirm", "<!--"):
                self.assertNotIn(banned, source)

    def test_the_whole_request_is_flat(self):
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        # session, user, owner, clinic (logo), unread count (bell)
        self.assertEqual(len(queries), 5)
        for number in range(20):
            Notification.objects.create(
                recipient=self.owner_user,
                notification_type=Notification.APPOINTMENT_APPROVED,
                title=f"Title {number}",
                message="Message",
            )
        with self.assertNumQueries(len(queries)):
            self.client.get(self.url)


# ── Slice 8b-10b-1: loading states of the owner pages ────────────────────────

LOADING_PART = "layouts/owner_parts/loading.html"
LOADING_JS = Path(settings.BASE_DIR) / "static" / "js" / "owner" / "loading.js"
LOADING_SCRIPT = r'<script src="/static/js/owner/loading\.[0-9a-f]+\.js" defer>'
LOADING_IDS = (
    "loading-status",
    "loading-bar",
    "loading-pill",
    "loading-slow",
    "loading-offline",
    "loading-root",
)


def loading_part_of(html):
    """The rendered loading part: from its live region to its template."""
    start = html.index('<div id="loading-status"')
    end = html.index("</template>", start) + len("</template>")
    return html[start:end]


class OwnerLoadingPartTests(_LoggedInBase):
    """The loading part and its script on both owner layouts (8b-10b-1)."""

    def _pages(self):
        self.client.force_login(self.owner_user)
        owner = self.client.get(reverse("owner_dashboard"))
        login = Client().get(reverse("owner_login"))
        return [("dashboard", owner), ("login", login)]

    def test_both_layouts_render_the_part_and_load_the_script(self):
        for name, response in self._pages():
            html = response.content.decode()
            with self.subTest(page=name):
                self.assertEqual(response.status_code, 200)
                self.assertTemplateUsed(response, LOADING_PART)
                for element in LOADING_IDS:
                    self.assertIn(f'id="{element}"', html)
                self.assertIn('data-slow-after="10000"', html)
                self.assertIn('<template id="loading-spinner">', html)
                self.assertRegex(html, LOADING_SCRIPT)

    def test_everything_but_the_live_region_and_the_template_starts_hidden(self):
        for name, response in self._pages():
            part = loading_part_of(response.content.decode())
            with self.subTest(page=name):
                for element in LOADING_IDS[1:]:
                    tag = re.search(rf'<div id="{element}"[^>]*>', part).group(0)
                    self.assertRegex(tag, r"\shidden[\s>]")
                live = re.search(r'<div id="loading-status"[^>]*>', part).group(0)
                self.assertNotRegex(live, r"\shidden[\s>]")
                self.assertIn('role="status" aria-live="polite"', live)

    def test_try_again_is_an_outline_button(self):
        for name, response in self._pages():
            part = loading_part_of(response.content.decode())
            with self.subTest(page=name):
                button = re.search(r"<button\b[^>]*data-loading-retry[^>]*>", part)
                self.assertIsNotNone(button)
                tag = button.group(0)
                self.assertIn('type="button"', tag)
                classes = re.search(r'class="([^"]*)"', tag).group(1).split()
                self.assertNotIn("bg-violet-600", classes)
                for word in ("bg-white", "border", "min-h-11"):
                    self.assertIn(word, classes)
                self.assertIn("Try again", part)

    def test_no_inline_script_or_handlers_in_the_part(self):
        for name, response in self._pages():
            part = loading_part_of(response.content.decode())
            with self.subTest(page=name):
                self.assertNotIn("<script", part)
                self.assertIsNone(re.search(r"\son[a-z]+\s*=", part))
        source = template_source(LOADING_PART)
        for banned in ("<script", "hx-confirm", "<!--"):
            self.assertNotIn(banned, source)
        self.assertIsNone(re.search(r"\son[a-z]+\s*=", source))
        for link in re.findall(r"(?s)<a\b.*?</a>", source):
            self.assertNotIn("\n", link)

    def test_the_admin_pages_do_not_load_it(self):
        response = self.client.get(reverse("admin_login"))
        self.assertNotContains(response, "js/owner/loading")
        self.assertTemplateNotUsed(response, LOADING_PART)
        self.client.force_login(self.admin)
        response = self.client.get(reverse("admin_dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "layouts/admin.html")
        self.assertNotContains(response, "js/owner/loading")
        self.assertTemplateNotUsed(response, LOADING_PART)


class MarkupParser(HTMLParser):
    """Forms, submit buttons and links of a page, each with its attributes."""

    def __init__(self):
        super().__init__()
        self.forms = []
        self.buttons = []
        self.links = []
        self._open = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self._open = {"attrs": attrs, "csrf": False}
            self.forms.append(self._open)
        elif tag == "input" and attrs.get("name") == "csrfmiddlewaretoken":
            if self._open is not None:
                self._open["csrf"] = True
        elif tag == "button" or (tag == "input" and attrs.get("type") == "submit"):
            if attrs.get("type", "submit") == "submit":
                self.buttons.append({"attrs": attrs, "inside": self._open})
        elif tag == "a":
            self.links.append(attrs)

    def handle_endtag(self, tag):
        if tag == "form":
            self._open = None

    def post_forms(self):
        return [
            form
            for form in self.forms
            if form["attrs"].get("method", "").lower() == "post"
        ]

    def buttons_of(self, form):
        """The submit buttons inside the form or tied to it by form="id"."""
        form_id = form["attrs"].get("id")
        return [
            button
            for button in self.buttons
            if button["inside"] is form
            or (form_id and button["attrs"].get("form") == form_id)
        ]


def parse_markup(html):
    parser = MarkupParser()
    parser.feed(html)
    return parser


class OwnerLoadingHooksTests(_LoggedInBase):
    """Every owner POST form is ready for loading.js and still posts without it."""

    def setUp(self):
        super().setUp()
        self.today = timezone.localdate()
        ClinicSettings.objects.create(
            clinic_name="Test Clinic",
            address="Gasan, Marinduque",
            opening_time=datetime.time(8, 0),
            closing_time=datetime.time(17, 0),
            slot_duration_minutes=60,
            working_days=[0, 1, 2, 3, 4, 5, 6],
            same_day_cutoff_time=datetime.time(15, 0),
            booking_limit_days=30,
        )
        self.owner = PetOwner.objects.get(user=self.owner_user)
        self.pet = Pet.objects.create(owner=self.owner, name="Bantay", species="Dog")
        self.service = Service.objects.create(
            service_name="Consultation", status=Service.ACTIVE
        )
        self.day = self.today + datetime.timedelta(days=8)
        self.visit = Appointment.objects.create(
            owner=self.owner,
            pet=self.pet,
            service=self.service,
            date=self.day,
            time=datetime.time(12, 0),
            status=Appointment.CONFIRMED,
        )
        Notification.objects.create(
            recipient=self.owner_user,
            notification_type=Notification.APPOINTMENT_APPROVED,
            title="Visit confirmed",
            message="See you soon.",
        )

    def _draft(self):
        session = self.client.session
        session["owner_booking"] = {
            "pet": self.pet.pk,
            "service": str(self.service.pk),
            "note": "Limping",
            "date": self.day.isoformat(),
            "time": "14:00:00",  # not 12:00: the fixture visit holds that slot
        }
        session.save()

    def _html(self, client, url):
        response = client.get(url)
        self.assertEqual(response.status_code, 200, url)
        return response.content.decode()

    def _signed_out(self):
        """Pages of a logged-out visitor: (name, html, labels expected)."""
        client = Client()
        session = client.session
        session["pending_email"] = "new.owner@example.com"
        session.save()
        google = "Opening Google…"
        pages = (
            ("login", "owner_login", {"Logging in…", google}),
            ("register", "owner_register", {"Please wait…", google}),
            ("forgot password", "forgot_password", {"Please wait…"}),
            ("check your email", "owner_register_pending", {"Sending…"}),
        )
        return [
            (name, self._html(client, reverse(url)), labels)
            for name, url, labels in pages
        ]

    def _onboarding(self):
        """Steps 1 and 2 for a new owner who has not finished onboarding."""
        user = User.objects.create_user(
            username="newbie",
            email="newbie@example.com",
            password="pass-12345-x",
            role=User.PET_OWNER,
        )
        row = PetOwner.objects.create(user=user, first_name="Nia", last_name="Lim")
        client = Client()
        client.force_login(user)
        session = client.session
        session["onboarding_owner_id"] = row.pk
        session.save()
        step1 = self._html(client, reverse("owner_onboarding_step1"))
        step2 = self._html(client, reverse("owner_onboarding_step2"))
        return [
            ("onboarding step 1", step1, {"Saving…"}),
            ("onboarding step 2", step2, {"Saving…", "Please wait…"}),
        ]

    def _signed_in(self):
        client = self.client
        client.force_login(self.owner_user)
        pet = self.pet.pk
        step1 = self._html(client, reverse("owner_book_appointment"))
        self._draft()
        step2 = self._html(client, reverse("owner_book_time"))
        step3 = self._html(client, reverse("owner_book_check"))
        visit = self.visit.pk
        pages = (
            ("add pet", "owner_pet_add", [], {"Saving…"}),
            ("edit pet", "owner_pet_edit", [pet], {"Saving…"}),
            ("change time", "owner_appointment_reschedule", [visit], {"Sending…"}),
            ("visit detail", "owner_appointment_detail", [visit], {"Sending…"}),
            ("pet profile", "owner_pet_detail", [pet], {"Sending…"}),
            ("my details", "owner_profile_edit", [], {"Saving…"}),
            ("email settings", "owner_notification_preferences", [], {"Saving…"}),
            ("account", "owner_account", [], {"Logging out…"}),
            ("notifications", "owner_notifications", [], {"Please wait…"}),
        )
        booking = [
            ("booking step 1", step1, {"Please wait…"}),
            ("booking step 2", step2, {"Please wait…"}),
            ("booking step 3", step3, {"Sending…"}),
        ]
        rest = [
            (name, self._html(client, reverse(url, args=args)), labels)
            for name, url, args, labels in pages
        ]
        return booking + rest

    def _pages(self):
        return self._signed_out() + self._onboarding() + self._signed_in()

    def test_every_post_form_button_has_a_busy_label_and_is_not_disabled(self):
        for name, html, expected in self._pages():
            parsed = parse_markup(html)
            forms = parsed.post_forms()
            labels = set()
            with self.subTest(page=name):
                self.assertTrue(forms)
                for form in forms:
                    buttons = parsed.buttons_of(form)
                    self.assertTrue(buttons)
                    self.assertNotIn("onsubmit", form["attrs"])
                    for button in buttons:
                        attrs = button["attrs"]
                        self.assertTrue(attrs.get("data-busy-label"))
                        self.assertNotIn("disabled", attrs)
                        self.assertNotIn("onclick", attrs)
                        labels.add(attrs["data-busy-label"])
                self.assertTrue(expected <= labels, f"{expected} vs {labels}")

    def test_the_forms_still_post_without_the_script(self):
        for name, html, _ in self._pages():
            parsed = parse_markup(html)
            with self.subTest(page=name):
                for form in parsed.post_forms():
                    self.assertTrue(form["csrf"])
                    if "action" in form["attrs"]:
                        self.assertTrue(form["attrs"]["action"])

    def test_a_named_submit_button_keeps_its_name_and_value(self):
        for name, html, _ in self._pages():
            parsed = parse_markup(html)
            named = [
                (button["attrs"]["name"], button["attrs"].get("value"))
                for button in parsed.buttons
                if "name" in button["attrs"]
            ]
            expected = [("action", "skip")] if name == "onboarding step 2" else []
            with self.subTest(page=name):
                self.assertEqual(named, expected)

    def test_the_tab_links_carry_the_tab_attribute_and_their_label(self):
        client = self.client
        client.force_login(self.owner_user)
        links = parse_markup(self._html(client, reverse("owner_dashboard"))).links
        tabs = [link for link in links if "data-tab-link" in link]
        labels = [tab.get("data-open-label") for tab in tabs]
        self.assertEqual(labels, ["Home", "Pets", "Visits", "Bills", "Account"])

    def test_the_back_bar_says_where_it_goes(self):
        client = self.client
        client.force_login(self.owner_user)
        pages = (
            (reverse("owner_pet_edit", args=[self.pet.pk]), "Bantay"),
            (reverse("owner_notification_preferences"), "Account"),
            (reverse("owner_appointment_detail", args=[self.visit.pk]), "Visits"),
        )
        for url, label in pages:
            links = parse_markup(self._html(client, url)).links
            with self.subTest(url=url):
                self.assertIn(label, [link.get("data-open-label") for link in links])


class OwnerLoadingScriptTests(SimpleTestCase):
    """static/js/owner/loading.js, read as text (8b-10b-1)."""

    def setUp(self):
        self.source = LOADING_JS.read_text(encoding="utf-8")

    def test_it_never_builds_markup_or_calls_the_network(self):
        banned = ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "fetch(")
        for word in banned:
            with self.subTest(word=word):
                self.assertNotIn(word, self.source)

    def test_no_english_text_from_the_part_is_in_the_file(self):
        for words in ("taking longer", "No internet", "Opening", "Try again"):
            with self.subTest(words=words):
                self.assertNotIn(words, self.source)
        part = template_source(LOADING_PART)
        pattern = r'data-(?:opening-[a-z]+|busy-default|offline-hold|online-message)="'
        for value in re.findall(pattern + r'([^"]*)"', part):
            with self.subTest(value=value):
                self.assertNotIn(value, self.source)

    def test_it_listens_for_the_page_coming_back_and_for_the_connection(self):
        for event in ("pageshow", "offline", "online"):
            with self.subTest(event=event):
                self.assertIn(f'"{event}"', self.source)

    def test_it_never_sets_the_real_disabled_attribute(self):
        self.assertIsNone(re.search(r"\.disabled\s*=", self.source))
        self.assertNotIn('setAttribute("disabled"', self.source)
        self.assertIn('"aria-disabled"', self.source)
