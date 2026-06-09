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

        if password1 and password2:
            if password1 != password2:
                raise forms.ValidationError("Passwords do not match.")

            # Run Django's built-in password validators
            from django.contrib.auth.password_validation import validate_password

            try:
                validate_password(password1)
            except forms.ValidationError as e:
                self.add_error("password1", e)

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
    Owner section of the walk-in registration form.
    Creates a User + PetOwner record.
    Email is optional — walk-in accounts without email cannot log in
    until email is added later.
    """

    first_name = forms.CharField(
        max_length=50,
        widget=forms.TextInput(attrs={"placeholder": "First name"}),
    )
    last_name = forms.CharField(
        max_length=50,
        widget=forms.TextInput(attrs={"placeholder": "Last name"}),
    )
    contact_number = forms.CharField(
        max_length=20,
        required=False,
        widget=forms.TextInput(attrs={"placeholder": "e.g. 09xxxxxxxxx"}),
    )
    email = forms.EmailField(
        required=False,
        widget=forms.EmailInput(attrs={"placeholder": "owner@email.com"}),
    )
    # Single optional contact link (Facebook or other)
    contact_link = forms.CharField(
        max_length=255,
        required=False,
        widget=forms.TextInput(
            attrs={"placeholder": "Facebook profile URL or username (optional)"}
        ),
    )

    def clean_email(self):
        email = self.cleaned_data.get("email")
        if email and User.objects.filter(email=email).exists():
            raise forms.ValidationError("An account with this email already exists.")
        return email


class WalkInPetForm(forms.Form):
    """
    Pet section of the walk-in registration form.
    Entirely optional — if name and species are both blank,
    the view skips pet creation entirely.
    """

    GENDER_CHOICES = [
        ("", "— Select gender —"),
        ("male", "Male"),
        ("female", "Female"),
        ("unknown", "Unknown"),
    ]

    # Common species options — matches the dropdown convention in FEATURES.md
    SPECIES_CHOICES = [
        ("", "— Select species —"),
        ("dog", "Dog"),
        ("cat", "Cat"),
        ("bird", "Bird"),
        ("rabbit", "Rabbit"),
        ("other", "Other"),
    ]

    pet_name = forms.CharField(
        max_length=50,
        required=False,
        widget=forms.TextInput(attrs={"placeholder": "Pet's name"}),
    )
    species = forms.ChoiceField(
        choices=SPECIES_CHOICES,
        required=False,
    )
    breed = forms.CharField(
        max_length=50,
        required=False,
        widget=forms.TextInput(attrs={"placeholder": "Breed (optional)"}),
    )
    color = forms.CharField(
        max_length=50,
        required=False,
        widget=forms.TextInput(attrs={"placeholder": "Color (optional)"}),
    )
    gender = forms.ChoiceField(
        choices=GENDER_CHOICES,
        required=False,
    )
    date_of_birth = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    weight = forms.DecimalField(
        max_digits=6,
        decimal_places=2,
        required=False,
        widget=forms.NumberInput(attrs={"placeholder": "Weight in kg", "step": "0.01"}),
    )

    def clean(self):
        cleaned_data = super().clean()
        pet_name = cleaned_data.get("pet_name", "").strip()
        species = cleaned_data.get("species", "").strip()

        # If either name or species is filled in, both become required together.
        # This prevents a half-completed pet entry from being saved.
        if pet_name and not species:
            self.add_error("species", "Species is required if you enter a pet name.")
        if species and not pet_name:
            self.add_error("pet_name", "Pet name is required if you select a species.")

        return cleaned_data

    def has_pet_data(self):
        """
        Returns True if the form has enough data to create a pet.
        Used by the view to decide whether to create a Pet record.
        """
        pet_name = self.cleaned_data.get("pet_name", "").strip()
        species = self.cleaned_data.get("species", "").strip()
        return bool(pet_name and species)
