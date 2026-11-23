import logging
from urllib.parse import urlencode

from django.conf import settings as django_settings
from django.contrib import messages
from django.contrib.auth import (
    authenticate,
    get_user_model,
    login,
    logout,
)

from django.contrib.auth import views as auth_views
from django.urls import reverse, reverse_lazy

from django.contrib.auth.decorators import login_required
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import send_mail
from django.db import IntegrityError, transaction
from django.shortcuts import redirect, render
from django.utils.encoding import force_bytes, force_str
from django.utils.http import (
    url_has_allowed_host_and_scheme,
    urlsafe_base64_decode,
    urlsafe_base64_encode,
)

from accounts import throttle
from accounts.forms import WalkInOwnerForm, WalkInPetForm
from accounts.utils import (
    WALKIN_NO_EMAIL_TAKEN,
    WALKIN_SAVE_FAILED,
    create_walkin_owner,
    find_pending_owner,
    has_archived_owner,
    is_unconfirmed_login,
    owner_profile_required,
    resend_allowed,
    send_claim_email,
)
from appointments.utils import get_clinic_settings

from .forms import PetOwnerRegistrationForm

User = get_user_model()
logger = logging.getLogger(__name__)

VERIFICATION_EMAIL_FAILED = (
    "Your account was created, but we couldn't send the email just now. "
    "Please contact the clinic so they can help."
)

RESEND_REPLY = (
    "If that email has an account waiting to be confirmed, we sent the link "
    "again. It may take a minute. Check Spam too."
)
REMEMBER_ME_SECONDS = 60 * 60 * 24 * 30  # 30 days, as before this redesign
ACCOUNT_CLOSED_MESSAGE = "This account is closed. Please contact the clinic."

# ─── Role-based redirect helper ───────────────────────────────────────────────


def redirect_by_role(user):
    """Redirect user to the correct dashboard based on their role."""
    if user.role == "admin":
        return redirect("admin_dashboard")
    return redirect("owner_dashboard")


# ─── Pet Owner Login ──────────────────────────────────────────────────────────


def _count_login_failure(scope, email, password):
    """Count a failed login. Empty fields are a form error, not a failure."""
    if email and password:
        throttle.record_login_failure(scope, email)


def owner_login(request):
    """Login page for pet owners."""
    if request.user.is_authenticated:
        return redirect_by_role(request.user)

    if request.method != "POST":
        # "Keep me logged in" starts unticked
        return render(request, "owner/login.html", {"remember": False})

    email = request.POST.get("email", "").strip().lower()
    password = request.POST.get("password", "")
    remember = bool(request.POST.get("remember"))
    context = {"email": email, "remember": remember}

    # Locked: authenticate is not called, even the right password is refused
    if throttle.login_is_locked("owner", email):
        context["login_locked"] = throttle.LOGIN_LOCKED_MESSAGE
        return render(request, "owner/login.html", context, status=429)

    user_obj = User.objects.filter(email=email).first()
    user = None
    if user_obj is not None:
        user = authenticate(request, username=user_obj.username, password=password)

    if user is None:
        # Right password on an owner who hasn't confirmed the email yet: the
        # only case that gets the "not confirmed" box (never logged in), and
        # not a failure to count. Every other failure gets the one generic box.
        if is_unconfirmed_login(user_obj, password):
            context["unconfirmed_email"] = user_obj.email
        else:
            _count_login_failure("owner", email, password)
            context["login_failed"] = True
        return render(request, "owner/login.html", context)

    if user.role != "pet_owner":
        messages.error(request, "Please use the admin login page.")
        return render(request, "owner/login.html", context)

    throttle.clear_login_failures("owner", email)
    login(request, user)
    # Ticked: 30 days, as before. Unticked: the session
    # ends when the browser closes.
    request.session.set_expiry(REMEMBER_ME_SECONDS if remember else 0)

    if not user.is_onboarded:
        return redirect("owner_onboarding")
    return redirect("owner_dashboard")


# ─── allauth pages → our own pages ───────────────────────────────────────────


def _redirect_keeping_next(request, url_name):
    """Redirect to url_name, keeping a safe ?next= when present."""
    url = reverse(url_name)
    next_url = request.GET.get("next", "")
    if next_url and url_has_allowed_host_and_scheme(
        next_url, allowed_hosts={request.get_host()}
    ):
        url = f"{url}?{urlencode({'next': next_url})}"
    return redirect(url)


def allauth_login_redirect(request):
    """/accounts/login/ → the owner login page."""
    return _redirect_keeping_next(request, "owner_login")


def allauth_signup_redirect(request):
    """/accounts/signup/ → the owner register page."""
    return _redirect_keeping_next(request, "owner_register")


ACCOUNT_INACTIVE_MESSAGE = (
    "This account can't log in yet. Check your email for the link, "
    "or contact the clinic."
)


def allauth_password_redirect(request, key=None):
    """allauth's password reset pages (start, done, key, key done) → our
    forgot-password page."""
    return redirect("forgot_password")


def allauth_confirm_email_redirect(request, key=None):
    """allauth's confirm-email pages → the owner login page (owners verify
    through our own link, see owner_verify_email)."""
    return redirect("owner_login")


def allauth_inactive_redirect(request):
    """/accounts/inactive/ → the owner login page, with the reason."""
    messages.error(request, ACCOUNT_INACTIVE_MESSAGE)
    return redirect("owner_login")


# ─── Admin Login ──────────────────────────────────────────────────────────────


def admin_login(request):
    """Separate login page for admin only."""
    if request.user.is_authenticated:
        return redirect_by_role(request.user)

    clinic = get_clinic_settings()
    email = ""
    login_failed = False

    if request.method == "POST":
        email = request.POST.get("email", "").strip().lower()
        password = request.POST.get("password", "")

        # Locked: authenticate is not called, even the right password is refused
        if throttle.login_is_locked("admin", email):
            messages.error(request, throttle.LOGIN_LOCKED_MESSAGE)
            context = {"clinic": clinic, "email": email, "login_failed": True}
            return render(request, "admin/login.html", context, status=429)

        user = _admin_authenticate(request, email, password)
        if user is not None and user.is_admin:
            throttle.clear_login_failures("admin", email)
            login(request, user)
            return redirect("admin_dashboard")
        if user is None:
            _count_login_failure("admin", email, password)

        # One message for every failure (unknown email, wrong password,
        # owner account, inactive account) on purpose, so the page never
        # reveals which part was wrong or whether the email has an account.
        messages.error(request, "That email or password isn't right. Please try again.")
        login_failed = True

    context = {"clinic": clinic, "email": email, "login_failed": login_failed}
    return render(request, "admin/login.html", context)


def _admin_authenticate(request, email, password):
    """The user for this email and password, or None (also for no email)."""
    if not email:
        return None
    try:
        user_obj = User.objects.get(email=email)
    except (User.DoesNotExist, User.MultipleObjectsReturned):
        # Defensive: email is unique today, but treat a duplicate as a failure.
        return None
    return authenticate(request, username=user_obj.username, password=password)


# ─── Pet Owner Registration ───────────────────────────────────────────────────


def owner_register(request):
    """
    Pet owner registration with email verification.
    Account is inactive until email is confirmed.
    """
    if request.user.is_authenticated:
        return redirect_by_role(request.user)

    is_post = request.method == "POST"
    form = PetOwnerRegistrationForm(request.POST if is_post else None)
    status = 200
    if is_post and form.is_valid() and not throttle.allow_register():
        # Site-wide hourly cap: counted right before the User is created
        form.add_error(None, throttle.REGISTER_BUSY_MESSAGE)
        status = 429
    elif is_post and form.is_valid():
        user = form.save()
        try:
            _send_verification_email(request, user)
        except Exception:
            # The account stays; the error goes to the log, never the page
            logger.exception("Verification email not sent to user %s", user.pk)
            messages.warning(request, VERIFICATION_EMAIL_FAILED)
        # Only the address, so "Check your email" can show it
        request.session["pending_email"] = user.email
        return redirect("owner_register_pending")

    context = {
        "form": form,
        "terms_url": django_settings.TERMS_URL,
        "privacy_url": django_settings.PRIVACY_URL,
    }
    return render(request, "owner/register.html", context, status=status)


def owner_register_pending(request):
    """
    Shown after registration — tells user to check their email.
    """
    context = {"pending_email": request.session.get("pending_email", "")}
    return render(request, "owner/register_pending.html", context)


def owner_resend_verification(request):
    """
    POST: sends the verification link again to an owner who is still waiting
    to confirm. The reply is always the same (one message, one page) whether
    or not an account matched, was throttled, was sent or failed, so the
    page never tells anyone which emails have accounts. A GET sends nothing.
    """
    if request.method != "POST":
        return redirect("owner_login")

    user = find_pending_owner(request.POST.get("email", ""))
    if user is not None and resend_allowed(user):
        try:
            _send_verification_email(request, user)
        except Exception as error:
            # Class name only: the message could hold the address or link
            logger.error(
                "Verification email not resent to user %s (%s)",
                user.pk,
                type(error).__name__,
            )
    messages.info(request, RESEND_REPLY)
    return redirect("owner_register_pending")


def owner_verify_email(request, uidb64, token):
    """
    Handles the email verification link.
    Activates account if token is valid.
    """
    from django.contrib.auth import get_user_model

    User = get_user_model()

    try:
        uid = force_str(urlsafe_base64_decode(uidb64))
        user = User.objects.get(pk=uid)
    except (TypeError, ValueError, OverflowError, User.DoesNotExist):
        user = None

    if user is not None and user.role != User.PET_OWNER:
        user = None  # only pet owners verify here: same as an invalid link

    if user is not None and default_token_generator.check_token(user, token):
        if has_archived_owner(user):
            # Archived on purpose: a valid link must not turn the account on
            messages.error(request, ACCOUNT_CLOSED_MESSAGE)
            return redirect("owner_login")
        user.is_active = True
        user.save()
        messages.success(
            request,
            "Email verified successfully. You can now log in.",
        )
        return redirect("owner_login")

    # Invalid or expired token
    return render(request, "owner/verify_email_invalid.html")


# ─── Email Verification Helper ────────────────────────────────────────────────


def _send_verification_email(request, user):
    """Sends email verification link to newly registered pet owner."""
    token = default_token_generator.make_token(user)
    uid = urlsafe_base64_encode(force_bytes(user.pk))

    # Build the verification path from its named URL
    verification_path = reverse(
        "owner_verify_email",
        kwargs={
            "uidb64": uid,
            "token": token,
        },
    )

    # Add the current domain and protocol
    verification_url = request.build_absolute_uri(verification_path)

    subject = "Verify your Hapi Vet account"
    message = (
        f"Hi {user.email},\n\n"
        f"Thank you for registering with Hapi Vet.\n\n"
        f"Please click the link below to verify your email address:\n"
        f"{verification_url}\n\n"
        f"This link will expire after 3 days.\n\n"
        f"If you did not create this account, please ignore this email.\n\n"
        f"— Hapi Tutz Veterinary Clinic"
    )

    send_mail(
        subject=subject,
        message=message,
        from_email=django_settings.DEFAULT_FROM_EMAIL,
        recipient_list=[user.email],
        fail_silently=False,
    )


# ─── Logout ───────────────────────────────────────────────────────────────────


def user_logout(request):
    """
    POST logs out any user; admins go to admin login, others to owner login.
    A GET never logs out: a logged-in user goes to their dashboard, a
    logged-out visitor to the owner login page (so is a POST with no one
    logged in).
    """
    if not request.user.is_authenticated:
        return redirect("owner_login")
    if request.method != "POST":
        return redirect_by_role(request.user)
    # Read the role first: logout() replaces request.user with AnonymousUser
    was_admin = request.user.is_admin
    logout(request)
    messages.success(request, "You're logged out.")
    return redirect("admin_login" if was_admin else "owner_login")


@login_required
@owner_profile_required
def owner_account(request):
    """
    Pet Owner view — the account hub: the owner's name and email, links to
    My details, Email notifications, Clinic info and the Health library, and
    the Log out form. The template lists the links, so none are passed here.
    (The unread count comes from the shell's context processor, not here.)
    """
    if request.user.role != User.PET_OWNER:
        return redirect("admin_dashboard")
    return render(request, "owner/account/index.html", {"owner": request.owner})


@login_required
def owner_onboarding(request):
    """
    Keeps the owner_onboarding URL name intact for login redirect logic.
    Forwards immediately to the real onboarding Step 1 in the pets app.
    """
    if request.user.role != "pet_owner":
        return redirect("admin_dashboard")
    return redirect("owner_onboarding_step1")


class HapiVetPasswordResetView(auth_views.PasswordResetView):
    template_name = "owner/forgot_password.html"
    email_template_name = "owner/emails/password_reset_email.txt"
    subject_template_name = "owner/emails/password_reset_subject.txt"
    success_url = reverse_lazy("password_reset_done")

    def form_valid(self, form):
        # Over a limit: send nothing, but show the normal "sent" page so the
        # reply never tells anyone which emails have accounts.
        if not throttle.allow_forgot(form.cleaned_data["email"]):
            return redirect(self.get_success_url())
        form.save(
            use_https=self.request.is_secure(),
            token_generator=self.token_generator,
            from_email=self.from_email,
            email_template_name=self.email_template_name,
            subject_template_name=self.subject_template_name,
            request=self.request,
            html_email_template_name=self.html_email_template_name,
            extra_email_context=self.extra_email_context,
            domain_override=self.request.get_host(),
        )

        return redirect(self.get_success_url())


# ---------------------------------------------------------------------------
# Walk-in client registration
# ---------------------------------------------------------------------------


def _claim_email_failed_message(email):
    """Warning when the claim email can't be sent after saving."""
    return (
        f"The claim email to {email} could not be sent. The owner and pet are "
        "saved, but the owner can't set a password until they get this email. "
        "Use Send the link again on the owner page later."
    )


def _save_walkin_owner(request, owner_form, pet_form):
    """
    Saves the walk-in owner and pet in one transaction, then sends the
    claim email (after the commit) when there is an email. Returns the pet,
    or None when nothing was saved (the admin sees a plain error; details
    go to the log, never to the page).
    """
    email = owner_form.cleaned_data.get("email") or ""
    try:
        with transaction.atomic():
            pet_owner, pet = create_walkin_owner(owner_form.cleaned_data, pet_form)
    except IntegrityError:
        # The atomic block rolled back every write
        logger.exception("Walk-in owner not saved (integrity error)")
        messages.error(request, WALKIN_SAVE_FAILED if email else WALKIN_NO_EMAIL_TAKEN)
        return None
    except Exception:
        logger.exception("Walk-in owner not saved")
        messages.error(request, WALKIN_SAVE_FAILED)
        return None

    messages.success(request, f"{pet_owner.full_name} and {pet.name} added.")
    if email:
        try:
            send_claim_email(request, pet_owner.user)
        except Exception:
            logger.exception("Claim email not sent to walk-in owner")
            messages.warning(request, _claim_email_failed_message(email))
    return pet


@login_required
def admin_walkin_create(request):
    """
    Walk-in "New owner" (still step 1 of the walk-in) — Admin only.

    Creates a User + PetOwner record and their Pet in a single atomic
    transaction, then goes to walk-in step 2 with both chosen. If an email
    is provided, the claim account email (one-time password-set link) is
    sent after the transaction commits; if sending fails, the records stay
    saved and the admin sees a warning.

    URL: /admin/walkin/create/
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    owner_form = WalkInOwnerForm(request.POST or None)
    pet_form = WalkInPetForm(request.POST or None)

    if request.method == "POST":
        owner_valid = owner_form.is_valid()
        pet_valid = pet_form.is_valid()
        if owner_valid and pet_valid:
            pet = _save_walkin_owner(request, owner_form, pet_form)
            if pet:
                visit_url = reverse("admin_walkin_appointment_visit")
                return redirect(f"{visit_url}?owner={pet.owner_id}&pet={pet.pk}")

    return render(
        request,
        "admin/walkin_create.html",
        {
            "owner_form": owner_form,
            "pet_form": pet_form,
        },
    )
