import logging

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
    urlsafe_base64_decode,
    urlsafe_base64_encode,
)

from accounts.forms import WalkInOwnerForm, WalkInPetForm
from accounts.utils import create_walkin_owner, send_claim_email
from appointments.models import ClinicSettings

from .forms import PetOwnerRegistrationForm

User = get_user_model()
logger = logging.getLogger(__name__)

# ─── Role-based redirect helper ───────────────────────────────────────────────


def redirect_by_role(user):
    """Redirect user to the correct dashboard based on their role."""
    if user.role == "admin":
        return redirect("admin_dashboard")
    return redirect("owner_dashboard")


# ─── Pet Owner Login ──────────────────────────────────────────────────────────


def owner_login(request):
    """Login page for pet owners."""
    if request.user.is_authenticated:
        return redirect_by_role(request.user)

    if request.method == "POST":
        email = request.POST.get("email", "").strip().lower()
        password = request.POST.get("password", "")

        from django.contrib.auth import get_user_model

        User = get_user_model()

        try:
            user_obj = User.objects.get(email=email)
            user = authenticate(request, username=user_obj.username, password=password)
        except User.DoesNotExist:
            user = None

        if user is None:
            messages.error(request, "Invalid email or password.")
            return render(request, "owner/login.html", {"email": email})

        if user.role != "pet_owner":
            messages.error(request, "Please use the admin login page.")
            return render(request, "owner/login.html", {"email": email})

        if not user.is_active:
            messages.error(
                request,
                "Your account is not verified yet. Please check your email for the verification link.",
            )
            return render(request, "owner/login.html", {"email": email})

        login(request, user)

        # Remember Me
        if request.POST.get("remember_me"):
            # Stay logged in for 30 days
            request.session.set_expiry(60 * 60 * 24 * 30)
        else:
            # Session expires when browser closes
            request.session.set_expiry(0)

        if not user.is_onboarded:
            return redirect("owner_onboarding")

        return redirect("owner_dashboard")

    return render(request, "owner/login.html")


# ─── Admin Login ──────────────────────────────────────────────────────────────


def admin_login(request):
    """Separate login page for admin only."""
    if request.user.is_authenticated:
        return redirect_by_role(request.user)

    clinic = ClinicSettings.objects.first()
    email = ""
    login_failed = False

    if request.method == "POST":
        email = request.POST.get("email", "").strip().lower()
        password = request.POST.get("password", "")

        user = None
        if email:
            try:
                user_obj = User.objects.get(email=email)
            except (User.DoesNotExist, User.MultipleObjectsReturned):
                # Defensive: email is unique today, but treat a duplicate
                # as a failure too.
                user_obj = None
            if user_obj is not None:
                user = authenticate(
                    request, username=user_obj.username, password=password
                )

        if user is not None and user.is_admin:
            login(request, user)
            return redirect("admin_dashboard")

        # One message for every failure (unknown email, wrong password,
        # owner account, inactive account) on purpose, so the page never
        # reveals which part was wrong or whether the email has an account.
        messages.error(request, "That email or password isn't right. Please try again.")
        login_failed = True

    return render(
        request,
        "admin/login.html",
        {"clinic": clinic, "email": email, "login_failed": login_failed},
    )


# ─── Pet Owner Registration ───────────────────────────────────────────────────


def owner_register(request):
    """
    Pet owner registration with email verification.
    Account is inactive until email is confirmed.
    """
    if request.user.is_authenticated:
        return redirect_by_role(request.user)

    if request.method == "POST":
        form = PetOwnerRegistrationForm(request.POST)
        if form.is_valid():
            user = form.save()
            _send_verification_email(request, user)
            return redirect("owner_register_pending")
        # Form invalid — re-render with errors
        return render(request, "owner/register.html", {"form": form})

    form = PetOwnerRegistrationForm()
    return render(request, "owner/register.html", {"form": form})


def owner_register_pending(request):
    """
    Shown after registration — tells user to check their email.
    """
    return render(request, "owner/register_pending.html")


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

    if user is not None and default_token_generator.check_token(user, token):
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


@login_required
def user_logout(request):
    """Logs out any user; admins go to admin login, others to owner login."""
    # Read the role first: logout() replaces request.user with AnonymousUser
    was_admin = request.user.is_admin
    logout(request)
    messages.success(request, "You have been logged out.")
    return redirect("admin_login" if was_admin else "owner_login")


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

# Second owner saved without an email: User.email is unique, so a second
# email="" row fails (nullable email is planned, see HANDOFF backlog).
WALKIN_NO_EMAIL_TAKEN = (
    "Only one owner without an email can be saved for now. "
    "Please add an email address for this owner."
)
WALKIN_SAVE_FAILED = (
    "Something went wrong, so this owner was not saved. Please try again."
)


def _claim_email_failed_message(email):
    """Warning when the claim email can't be sent after saving."""
    return (
        f"The claim email to {email} could not be sent. The owner and pet are "
        "saved, but the owner can't set a password until they get this email. "
        "There is no resend button yet."
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
