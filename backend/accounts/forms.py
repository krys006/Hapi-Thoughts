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
            raise forms.ValidationError("Passwords do not match.")

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