import getpass

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import validate_email

User = get_user_model()

ADMIN_USERNAME = "admin"


class Command(BaseCommand):
    help = "Creates the Hapi Vet admin account. Asks for the email and password."

    def handle(self, *args, **kwargs):
        email = input("Admin email: ").strip().lower()
        self._check_email(email)

        password = self._ask_password()
        user = User(username=ADMIN_USERNAME, email=email)
        try:
            validate_password(password, user)
        except ValidationError as error:
            raise CommandError(
                "That password is not allowed:\n  - " + "\n  - ".join(error.messages)
            )

        user = User.objects.create_user(
            username=ADMIN_USERNAME,
            email=email,
            password=password,
            role=User.ADMIN,
            is_onboarded=True,
            is_staff=True,  # allows future Django shell access if needed
        )

        self.stdout.write(
            self.style.SUCCESS(
                f"Admin account created successfully.\n"
                f"  Email: {email}\n"
                f"  Role:  {user.get_role_display()}"
            )
        )

    def _check_email(self, email):
        """Stop with a plain message if the email is bad or already used."""
        try:
            validate_email(email)
        except ValidationError:
            raise CommandError("That is not a valid email address.")
        if User.objects.filter(email__iexact=email).exists():
            raise CommandError(
                f"A user with the email {email} already exists. Nothing was changed."
            )
        if User.objects.filter(username=ADMIN_USERNAME).exists():
            raise CommandError(
                f'The username "{ADMIN_USERNAME}" is already taken. '
                "Nothing was changed."
            )

    def _ask_password(self):
        """Ask twice, hidden. Empty or different answers stop the command."""
        password = getpass.getpass("Password: ")
        if not password:
            raise CommandError("The password cannot be empty.")
        if getpass.getpass("Password (again): ") != password:
            raise CommandError("The two passwords do not match.")
        return password
