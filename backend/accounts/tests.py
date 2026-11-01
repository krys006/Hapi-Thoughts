import datetime
import re
from unittest import mock

from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.models import SocialAccount, SocialLogin
from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.middleware import SessionMiddleware
from django.core import mail
from django.db import connection
from django.test import RequestFactory, TestCase, TransactionTestCase
from django.urls import reverse

from accounts.adapters import (
    ADMIN_MESSAGE,
    INACTIVE_MESSAGE,
    MULTIPLE_MATCHES_MESSAGE,
    UNVERIFIED_MESSAGE,
    CustomSocialAccountAdapter,
)
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
                    "until they get this email. There is no resend button yet.",
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
