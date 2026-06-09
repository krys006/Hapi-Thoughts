from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """
    Custom user model for Hapi Vet.
    Extends AbstractUser to add a role field.
    All authentication flows use this model.
    """

    ADMIN = "admin"
    PET_OWNER = "pet_owner"
    ROLE_CHOICES = [
        (ADMIN, "Admin"),
        (PET_OWNER, "Pet Owner"),
    ]

    role = models.CharField(
        max_length=20,
        choices=ROLE_CHOICES,
        default=PET_OWNER,
    )
    is_onboarded = models.BooleanField(
        default=False,
        help_text="True once pet owner completes onboarding. Always True for Admin.",
    )

    # Use email as the primary login identifier
    email = models.EmailField(unique=True)

    def __str__(self):
        return f"{self.email} ({self.get_role_display()})"

    @property
    def is_admin(self):
        return self.role == self.ADMIN

    @property
    def is_pet_owner(self):
        return self.role == self.PET_OWNER
