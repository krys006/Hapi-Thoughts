from django.shortcuts import render, redirect
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.contrib.auth.tokens import default_token_generator
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode
from django.utils.encoding import force_bytes, force_str
from django.core.mail import send_mail
from django.conf import settings as django_settings

from .forms import PetOwnerRegistrationForm


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
            "Your email has been verified. You can now log in.",
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
    verification_url = request.build_absolute_uri(
        f"/verify-email/{uid}/{token}/"
    )

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
    """Placeholder onboarding view — built properly in Phase 2."""
    if request.user.role != "pet_owner":
        return redirect("admin_dashboard")
    return render(request, "owner/onboarding.html")