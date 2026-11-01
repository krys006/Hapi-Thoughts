from django import forms
from django.contrib.auth import get_user_model

User = get_user_model()


class PetOwnerRegistrationForm(forms.ModelForm):
    """
    Registration form for pet owners.
    Collects email + password only — profile details collected during onboarding.
    """

    password1 = forms.CharField(
        label="Password",
        widget=forms.PasswordInput(attrs={"placeholder": "Create a password"}),
    )
    password2 = forms.CharField(
        label="Confirm Password",
        widget=forms.PasswordInput(attrs={"placeholder": "Repeat your password"}),
    )

    class Meta:
        model = User
        fields = ["email"]
        widgets = {
            "email": forms.EmailInput(attrs={"placeholder": "you@email.com"}),
        }

    def clean_email(self):
        email = self.cleaned_data.get("email", "").strip().lower()
        if User.objects.filter(email=email).exists():
            raise forms.ValidationError("An account with this email already exists.")
        return email

    def clean(self):
        cleaned_data = super().clean()
        password1 = cleaned_data.get("password1")
        password2 = cleaned_data.get("password2")

        if password1 and password2 and password1 != password2:
            self.add_error("password2", "Passwords do not match.")

        if password1:
            # Run Django's built-in password validators
            from django.contrib.auth.password_validation import validate_password

            try:
                validate_password(password1)
            except forms.ValidationError as error:
                self.add_error("password1", error)

        return cleaned_data

    def save(self, commit=True):
        user = super().save(commit=False)
        email = self.cleaned_data["email"]

        # Use email prefix as username (must be unique)
        base_username = email.split("@")[0]
        username = base_username
        counter = 1
        while User.objects.filter(username=username).exists():
            username = f"{base_username}{counter}"
            counter += 1

        user.username = username
        user.email = email
        user.role = User.PET_OWNER
        user.is_onboarded = False
        user.is_active = False  # inactive until email verified

        user.set_password(self.cleaned_data["password1"])

        if commit:
            user.save()
        return user


class WalkInOwnerForm(forms.Form):
    """
    Owner section of the walk-in "New owner" form.
    Creates a User + PetOwner record.
    Email is optional — walk-in accounts without email cannot log in
    until email is added later.
    """

    first_name = forms.CharField(max_length=50)
    last_name = forms.CharField(max_length=50)
    contact_number = forms.CharField(
        label="Phone number",
        max_length=20,
        required=False,
        widget=forms.TextInput(attrs={"placeholder": "For example: 0917 123 4567"}),
    )
    email = forms.EmailField(
        required=False,
        widget=forms.EmailInput(attrs={"placeholder": "Leave empty if they have none"}),
        help_text=(
            "We will email them a link to set a password and see their "
            "pet’s records."
        ),
    )
    # Single optional contact link (Facebook or other)
    contact_link = forms.CharField(
        label="Facebook or other contact link",
        max_length=255,
        required=False,
    )

    def clean_email(self):
        # Saved lowercase; the check ignores case so "Ana@x.com" matches "ana@x.com"
        email = (self.cleaned_data.get("email") or "").strip().lower()
        if email and User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError(
                "This email is already used by another account."
            )
        return email


class WalkInPetForm(forms.Form):
    """
    Pet section of the walk-in "New owner" form: the pet being seen.
    Name and species are required, since a visit needs a pet.
    """

    GENDER_CHOICES = [
        ("", "Choose one"),
        ("male", "Male"),
        ("female", "Female"),
        ("unknown", "Unknown"),
    ]

    # Common species options — matches the dropdown convention in FEATURES.md
    SPECIES_CHOICES = [
        ("", "Choose one"),
        ("dog", "Dog"),
        ("cat", "Cat"),
        ("bird", "Bird"),
        ("rabbit", "Rabbit"),
        ("other", "Other"),
    ]

    pet_name = forms.CharField(max_length=50)
    species = forms.ChoiceField(choices=SPECIES_CHOICES)
    breed = forms.CharField(
        max_length=50,
        required=False,
        widget=forms.TextInput(attrs={"placeholder": "For example: Aspin"}),
    )
    color = forms.CharField(max_length=50, required=False)
    gender = forms.ChoiceField(label="Sex", choices=GENDER_CHOICES, required=False)
    date_of_birth = forms.DateField(
        label="Birthday",
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text="A rough date is fine.",
    )
    weight = forms.DecimalField(
        label="Weight (kg)",
        max_digits=6,
        decimal_places=2,
        required=False,
        widget=forms.NumberInput(
            attrs={"placeholder": "For example: 8.5", "step": "0.01"}
        ),
    )
