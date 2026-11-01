import uuid

from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from pets.models import ContactLink, Pet, PetOwner

User = get_user_model()


def send_claim_email(request, user):
    """
    Sends the 'claim your account' email to a walk-in owner, with a one-time
    password-set link. Reuses default_token_generator (the password reset
    mechanism) with its own template and subject.
    Raises if sending fails; callers decide what to tell the admin.
    """
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)

    # Points to Django's built-in password reset confirm view
    claim_link = request.build_absolute_uri(f"/reset-password/{uid}/{token}/")

    subject = render_to_string("owner/emails/claim_account_subject.txt", {}).strip()

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


def generate_walkin_username(first_name, last_name, email=None):
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


def create_walkin_owner(owner_data, pet_form):
    """
    Creates the User, PetOwner, optional ContactLink and optional Pet for a
    walk-in. Call inside transaction.atomic(). Sends no email.
    Returns (pet_owner, pet); pet is None when the pet section was empty.
    """
    email = owner_data.get("email") or ""
    first_name = owner_data["first_name"]
    last_name = owner_data["last_name"]

    user = User(
        username=generate_walkin_username(first_name, last_name, email or None),
        email=email,
        role=User.PET_OWNER,
        # Walk-in accounts with no email are inactive until
        # email is provided and claimed.
        is_active=bool(email),
        is_onboarded=True,  # Admin-created — skip onboarding
    )
    # Unusable password — the owner sets their own via the claim link
    user.set_unusable_password()
    user.save()

    pet_owner = PetOwner.objects.create(
        user=user,
        first_name=first_name,
        last_name=last_name,
        contact_number=owner_data.get("contact_number", ""),
    )

    contact_link_value = owner_data.get("contact_link", "").strip()
    if contact_link_value:
        ContactLink.objects.create(
            owner=pet_owner,
            platform="Facebook",
            url_or_handle=contact_link_value,
        )

    pet = None
    if pet_form.has_pet_data():
        pet_data = pet_form.cleaned_data
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

    return pet_owner, pet
