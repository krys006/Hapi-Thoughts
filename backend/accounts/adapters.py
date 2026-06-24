from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from django.shortcuts import redirect


class CustomSocialAccountAdapter(DefaultSocialAccountAdapter):

    def get_connect_redirect_url(self, request, socialaccount):
        return "/owner/dashboard/"

    def pre_social_login(self, request, sociallogin):
        """
        Called after successful Google auth but before login is finalized.
        If user already exists with this email, connect the accounts.
        """
        if sociallogin.is_existing:
            return

        # Check if email already exists as a regular account
        try:
            from django.contrib.auth import get_user_model

            User = get_user_model()
            email = sociallogin.account.extra_data.get("email", "")
            if email:
                existing_user = User.objects.get(email=email)
                sociallogin.connect(request, existing_user)
        except User.DoesNotExist:
            pass

    def save_user(self, request, sociallogin, form=None):
        """Assign PET_OWNER role when creating account via Google."""
        user = super().save_user(request, sociallogin, form)
        if not user.role:
            user.role = "pet_owner"
            user.save()
        return user
