from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model

User = get_user_model()


class Command(BaseCommand):
    help = "Creates the Hapi Vet admin account (Dr. Edgar Sadiwa)"

    def handle(self, *args, **kwargs):
        email = "admin@hapivet.com"
        username = "admin"
        password = "hapivet2025"

        if User.objects.filter(email=email).exists():
            self.stdout.write(
                self.style.WARNING(f"Admin account already exists: {email}")
            )
            return

        user = User.objects.create_user(
            username=username,
            email=email,
            password=password,
            role=User.ADMIN,
            is_onboarded=True,
            is_staff=True,  # allows future Django shell access if needed
        )

        self.stdout.write(
            self.style.SUCCESS(
                f"Admin account created successfully.\n"
                f"  Email:    {email}\n"
                f"  Password: {password}\n"
                f"  Role:     {user.get_role_display()}\n"
                f"\nChange this password after first login."
            )
        )
