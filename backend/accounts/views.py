import uuid

from django.conf import settings as django_settings
from django.contrib import messages
from django.contrib.auth import (
    authenticate,
    get_user_model,
    login,
    logout,
)
from django.contrib.auth.decorators import login_required
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import send_mail
from django.db import transaction
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.utils.encoding import force_bytes, force_str
from django.utils.http import (
    urlsafe_base64_decode,
    urlsafe_base64_encode,
)

from accounts.forms import WalkInOwnerForm, WalkInPetForm
from pets.models import ContactLink, Pet, PetOwner

from .forms import PetOwnerRegistrationForm

User = get_user_model()

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
            return render(request, "admin/login.html", {"email": email})

        if user.role != "admin":
            messages.error(request, "You do not have admin access.")
            return render(request, "admin/login.html", {"email": email})

        if not user.is_active:
            messages.error(request, "This account has been deactivated.")
            return render(request, "admin/login.html", {"email": email})

        login(request, user)
        return redirect("admin_dashboard")

    return render(request, "admin/login.html")


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

    # Build the full verification URL
    verification_url = request.build_absolute_uri(f"/verify-email/{uid}/{token}/")

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
    """Logs out any user and redirects to pet owner login."""
    logout(request)
    messages.success(request, "You have been logged out.")
    return redirect("owner_login")


# ─── Placeholder Dashboards ───────────────────────────────────────────────────


@login_required
def admin_dashboard(request):
    """Placeholder admin dashboard."""
    if request.user.role != "admin":
        return redirect("owner_dashboard")
    return render(request, "admin/dashboard/index.html")


@login_required
def owner_dashboard(request):
    """Placeholder pet owner dashboard."""
    if request.user.role != "pet_owner":
        return redirect("admin_dashboard")
    if not request.user.is_onboarded:
        return redirect("owner_onboarding")
    return render(request, "owner/dashboard/index.html")


@login_required
def owner_onboarding(request):
    """
    Keeps the owner_onboarding URL name intact for login redirect logic.
    Forwards immediately to the real onboarding Step 1 in the pets app.
    """
    if request.user.role != "pet_owner":
        return redirect("admin_dashboard")
    return redirect("owner_onboarding_step1")


# ---------------------------------------------------------------------------
# Walk-in client registration
# ---------------------------------------------------------------------------


def _generate_username(first_name, last_name, email=None):
    """
    Generates a unique username for a walk-in account.
    Uses email prefix if available, otherwise firstname_lastname.
    Appends a short hex suffix if the base username is already taken.
    """
    if email:
        base = email.split("@")[0].lower()
    else:
        base = f"{first_name.lower()}_{last_name.lower()}"

    # Strip spaces just in case
    base = base.replace(" ", "_")
    username = base

    while User.objects.filter(username=username).exists():
        username = f"{base}_{uuid.uuid4().hex[:4]}"

    return username


def admin_walkin_create(request):
    """
    Walk-in client registration — Admin only.

    Creates a User + PetOwner record (and optionally a Pet) in a single
    atomic transaction. If an email is provided, sends a claim account
    email with a one-time password-set link.

    URL: /admin/walkin/create/
    """
    # Role guard
    if not request.user.is_authenticated or request.user.role != "admin":
        return redirect("owner_dashboard")

    owner_form = WalkInOwnerForm(request.POST or None)
    pet_form = WalkInPetForm(request.POST or None)

    if request.method == "POST":
        owner_valid = owner_form.is_valid()
        pet_valid = pet_form.is_valid()

        if owner_valid and pet_valid:
            try:
                with transaction.atomic():
                    owner_data = owner_form.cleaned_data
                    pet_data = pet_form.cleaned_data

                    email = owner_data.get("email") or ""
                    first_name = owner_data["first_name"]
                    last_name = owner_data["last_name"]

                    # --- Create User ---
                    username = _generate_username(first_name, last_name, email or None)
                    user = User(
                        username=username,
                        email=email,
                        role="pet_owner",
                        # Walk-in accounts with no email are inactive until
                        # email is provided and claimed.
                        is_active=bool(email),
                        is_onboarded=True,  # Admin-created — skip onboarding
                    )
                    # Set an unusable password — owner sets their own via claim link
                    user.set_unusable_password()
                    user.save()

                    # --- Create PetOwner ---
                    pet_owner = PetOwner.objects.create(
                        user=user,
                        first_name=first_name,
                        last_name=last_name,
                        contact_number=owner_data.get("contact_number", ""),
                    )

                    # --- Create ContactLink if provided ---
                    contact_link_value = owner_data.get("contact_link", "").strip()
                    if contact_link_value:
                        ContactLink.objects.create(
                            owner=pet_owner,
                            platform="Facebook",
                            url_or_handle=contact_link_value,
                        )

                    # --- Create Pet if pet section was filled in ---
                    pet = None
                    if pet_form.has_pet_data():
                        pet = Pet.objects.create(
                            owner=pet_owner,
                            name=pet_data["pet_name"].strip(),
                            species=pet_data["species"],
                            breed=pet_data.get("breed", ""),
                            color=pet_data.get("color", ""),
                            gender=pet_data.get("gender", "unknown") or "unknown",
                            date_of_birth=pet_data.get("date_of_birth") or None,
                            weight=pet_data.get("weight") or None,
                        )

                    # --- Send claim account email if email was provided ---
                    if email:
                        _send_claim_account_email(request, user)

                # Build success message
                pet_created_msg = f" Pet '{pet.name}' was also added." if pet else ""
                email_msg = (
                    " A claim account email has been sent."
                    if email
                    else " No email provided — account saved without claim link."
                )
                messages.success(
                    request,
                    f"Walk-in account created for {first_name} {last_name}."
                    f"{pet_created_msg}{email_msg}",
                )
                return redirect("admin_owner_detail", pk=pet_owner.pk)

            except Exception as e:
                # Catch-all — the atomic block already rolled back any DB writes
                messages.error(
                    request,
                    f"Something went wrong while creating the account. "
                    f"Please try again. (Error: {e})",
                )

    return render(
        request,
        "admin/walkin_create.html",
        {
            "owner_form": owner_form,
            "pet_form": pet_form,
        },
    )


def _send_claim_account_email(request, user):
    """
    Sends a 'claim your account' email to a newly registered walk-in user.
    Reuses Django's default_token_generator (same mechanism as password reset)
    but with a different template and subject line.
    """
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)

    # Build the claim link — points to Django's built-in password reset confirm view
    # You can swap this for a custom view later if needed
    claim_link = request.build_absolute_uri(f"/reset-password/{uid}/{token}/")

    subject = render_to_string(
        "owner/emails/claim_account_subject.txt",
        {},
    ).strip()

    body = render_to_string(
        "owner/emails/claim_account_body.txt",
        {
            "user": user,
            "claim_link": claim_link,
            "clinic_name": "Hapi Tutz Vet. Supplies",
        },
    )

    send_mail(
        subject=subject,
        message=body,
        from_email=None,  # Uses DEFAULT_FROM_EMAIL from settings
        recipient_list=[user.email],
        fail_silently=False,
    )
