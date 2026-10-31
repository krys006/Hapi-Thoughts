from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.shortcuts import redirect

User = get_user_model()

ADMIN_MESSAGE = "Admin accounts sign in with email and password."
INACTIVE_MESSAGE = (
    "This account can't sign in right now. If you just registered, check "
    "your email for the verification link; otherwise contact the clinic."
)
MULTIPLE_MATCHES_MESSAGE = (
    "We couldn't link your Google account. Please contact the clinic."
)
UNVERIFIED_MESSAGE = (
    "Your Google email address isn't verified. Please sign in or register "
    "with your email address instead."
)


class CustomSocialAccountAdapter(DefaultSocialAccountAdapter):

    def pre_social_login(self, request, sociallogin):
        """
        Runs after Google auth, before allauth logs the user in or signs
        them up. Every refusal redirects to owner_login with a message.
        """
        # Google account already linked: allauth has found the user.
        if sociallogin.is_existing:
            _refuse_if_blocked(request, sociallogin.user)
            return

        # Check verified before looking up any user, with one message for
        # every case, so an unverified Google account learns nothing about
        # which local accounts exist (none, several, admin, inactive).
        if not _google_email_verified(sociallogin.account.extra_data):
            _refuse(request, UNVERIFIED_MESSAGE)

        user = _find_matching_user(request, sociallogin.account.extra_data)

        # No local account: let allauth auto-signup a new pet owner.
        if user is None:
            return

        _refuse_if_blocked(request, user)
        # Only link once every check above has passed
        sociallogin.connect(request, user)


def _google_email_verified(extra_data):
    """
    True only if Google itself says the email is verified.

    We read extra_data instead of sociallogin.email_addresses because
    allauth can mark those addresses verified on its own (the provider
    VERIFIED_EMAIL setting). Same keys as allauth's google/provider.py,
    but stricter: only True or "true" count, missing or anything else
    means not verified.
    """
    value = extra_data.get("email_verified", extra_data.get("verified_email"))
    if isinstance(value, str):
        return value.lower() == "true"
    return value is True


def _find_matching_user(request, extra_data):
    """
    Return the one user whose email matches Google's (ignoring case),
    or None. Refuses if more than one matches, since User.email is only
    unique case-sensitively and we must never guess which to link.
    """
    email = (extra_data.get("email") or "").strip()
    if not email:
        return None
    users = list(User.objects.filter(email__iexact=email)[:2])
    if len(users) > 1:
        _refuse(request, MULTIPLE_MATCHES_MESSAGE)
    return users[0] if users else None


def _refuse_if_blocked(request, user):
    """Admins never use Google sign-in (until FEATURES 1.3); inactive
    users (archived or not yet verified) can't sign in at all."""
    if user.role == User.ADMIN:
        _refuse(request, ADMIN_MESSAGE)
    if not user.is_active:
        _refuse(request, INACTIVE_MESSAGE)


def _refuse(request, message):
    """Stop the allauth flow and send the user back to the owner login."""
    messages.error(request, message)
    raise ImmediateHttpResponse(redirect("owner_login"))
