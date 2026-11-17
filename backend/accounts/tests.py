import datetime
import re
from unittest import mock

from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.models import SocialAccount, SocialLogin
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.contrib.messages import get_messages
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.middleware import SessionMiddleware
from django.core import mail
from django.core.cache import cache
from django.db import connection
from django.test import (
    Client,
    RequestFactory,
    TestCase,
    TransactionTestCase,
    override_settings,
)
from django.urls import resolve, reverse
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from accounts.adapters import (
    ADMIN_MESSAGE,
    INACTIVE_MESSAGE,
    MULTIPLE_MATCHES_MESSAGE,
    UNVERIFIED_MESSAGE,
    CustomSocialAccountAdapter,
)
from accounts.utils import verification_pending
from appointments.models import ClinicSettings
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
                self.assertEqual(self.client.get(url).status_code, 200)


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
                self.assertEqual(found[-1], "You have been logged out.")

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
        """The admin sidebar (every admin page) and the owner sidebar
        (base_dashboard.html, every owner page)."""
        logout_url = reverse("logout")
        form = re.compile(
            rf'<form method="post" action="{logout_url}">\s*'
            r'<input type="hidden" name="csrfmiddlewaretoken" value="[^"]+">\s*'
            r'<button type="submit"[^>]*>'
        )
        pages = {
            self.admin: ["admin_dashboard", "admin_clinic_settings", "admin_pet_list"],
            self.owner_user: [
                "owner_dashboard",
                "owner_pet_list",
                "owner_profile",
                "owner_appointments",
                "clinic_info",
            ],
        }
        for user, names in pages.items():
            self.client.force_login(user)
            for name in names:
                with self.subTest(page=name):
                    html = self.client.get(reverse(name)).content.decode()
                    self.assertRegex(html, form)
                    self.assertNotIn(f'href="{logout_url}"', html)

    def test_the_log_out_button_keeps_its_text_and_classes(self):
        self.client.force_login(self.admin)
        html = self.client.get(reverse("admin_dashboard")).content.decode()
        self.assertRegex(
            html,
            r'(?s)<button type="submit" class="flex min-h-11 w-full items-center gap-3 '
            r"rounded-lg px-3 text-left text-base text-gray-800 "
            r'hover:bg-violet-200/70">.*?Log out</button>',
        )
        self.client.force_login(self.owner_user)
        html = self.client.get(reverse("owner_dashboard")).content.decode()
        self.assertIn(
            '<button type="submit" class="owner-sidebar-logout">Logout</button>', html
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
        self.assertContains(response, "<h1>Account</h1>", html=True)
        self.assertContains(response, "Ana Cruz")
        self.assertContains(response, "ana@example.com")
        for name, text in (
            ("owner_profile_edit", "My details"),
            ("owner_notification_preferences", "Email notifications"),
            ("clinic_info", "Clinic info"),
            ("owner_health_article_list", "Health library"),
        ):
            with self.subTest(link=name):
                self.assertContains(
                    response, f'<a href="{reverse(name)}">{text}</a>', html=True
                )

    def test_every_link_is_on_one_line(self):
        self.client.force_login(self.owner_user)
        html = self.client.get(self.url).content.decode()
        # Only this page's own markup (the shell has older multi-line tags)
        page = html.split("<h1>Account</h1>")[1].split("</section>")[0]
        for line in page.splitlines():
            if "<a " in line or "<a>" in line:
                with self.subTest(line=line.strip()):
                    self.assertIn("</a>", line)

    def test_log_out_is_a_post_form_with_a_csrf_token(self):
        self.client.force_login(self.owner_user)
        html = self.client.get(self.url).content.decode()
        form = (
            rf'<form method="post" action="{reverse("logout")}">\s*'
            r'<input type="hidden" name="csrfmiddlewaretoken" value="[^"]+">\s*'
            r'<button type="submit">Log out</button>'
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
GENERIC_LOGIN_ERROR = "Invalid email or password."
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
        self.assertContains(response, RESEND_REPLY_TEXT)

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
        self.assertContains(response, CLOSED_TEXT)
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
