# accounts/signals.py
# Role assignment now handled in adapters.py CustomSocialAccountAdapter


from allauth.socialaccount.signals import social_account_added
from django.dispatch import receiver


@receiver(social_account_added)
def set_role_on_google_signup(request, sociallogin, **kwargs):

    user = sociallogin.user
    if not user.role:
        user.role = "pet_owner"
        user.save()


# Automatically assign PET_OWNER role when signing up with Google.
