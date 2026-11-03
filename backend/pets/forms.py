# pets/forms.py

from django import forms
from django.core.exceptions import NON_FIELD_ERRORS
from django.forms import formset_factory
from django.urls import reverse

from .models import ContactLink, Pet, PetDeletionRequest, PetOwner
from .utils import barangay_choices, marinduque_towns, match_barangay, match_town


class PetOwnerProfileForm(forms.ModelForm):
    class Meta:
        model = PetOwner
        fields = [
            "first_name",
            "last_name",
            "contact_number",
            "address_line",
            "barangay",
            "city_municipality",
            "province",
            "profile_photo",
        ]
        widgets = {
            "first_name": forms.TextInput(attrs={"placeholder": "First name"}),
            "last_name": forms.TextInput(attrs={"placeholder": "Last name"}),
            "contact_number": forms.TextInput(
                attrs={"placeholder": "e.g. 09171234567"}
            ),
            "address_line": forms.TextInput(
                attrs={"placeholder": "e.g. Purok IV, Alihugon (optional)"}
            ),
            "barangay": forms.TextInput(attrs={"placeholder": "e.g. Amoingon"}),
            "city_municipality": forms.TextInput(attrs={"placeholder": "e.g. Baoc"}),
            "province": forms.TextInput(attrs={"placeholder": "e.g. Marinduque"}),
        }

    def clean_first_name(self):
        value = self.cleaned_data.get("first_name", "").strip()
        if not value:
            raise forms.ValidationError("First name is required.")
        return value.title()

    def clean_last_name(self):
        value = self.cleaned_data.get("last_name", "").strip()
        if not value:
            raise forms.ValidationError("Last name is required.")
        return value.title()

    def clean_address_line(self):
        value = self.cleaned_data.get("address_line", "").strip()
        # Preserve user casing but collapse extra whitespace
        return " ".join(value.split())

    def clean_barangay(self):
        value = self.cleaned_data.get("barangay", "").strip()
        return value.title()

    def clean_city_municipality(self):
        value = self.cleaned_data.get("city_municipality", "").strip()
        return value.title()

    def clean_province(self):
        value = self.cleaned_data.get("province", "").strip()
        return value.title()


class ContactLinkForm(forms.ModelForm):
    class Meta:
        model = ContactLink
        fields = ["platform", "url_or_handle"]
        widgets = {
            "platform": forms.TextInput(
                attrs={"placeholder": "Platform (e.g. Facebook)"}
            ),
            "url_or_handle": forms.TextInput(attrs={"placeholder": "URL or username"}),
        }

    def clean(self):
        cleaned_data = super().clean()
        platform = cleaned_data.get("platform", "").strip()
        url_or_handle = cleaned_data.get("url_or_handle", "").strip()

        # Both fields must be filled together, or both left blank.
        # A row with only one field filled is invalid.
        if bool(platform) != bool(url_or_handle):
            raise forms.ValidationError(
                "Please fill in both the platform name and the URL or handle."
            )

        return cleaned_data

    def is_empty(self):
        """Returns True if both fields are blank — used to skip saving."""
        platform = self.cleaned_data.get("platform", "").strip()
        url_or_handle = self.cleaned_data.get("url_or_handle", "").strip()
        return not platform and not url_or_handle


# Two blank rows shown by default — extras left blank are skipped
ContactLinkFormSet = formset_factory(
    ContactLinkForm,
    extra=2,
    max_num=10,
    validate_max=True,
)


class PetForm(forms.ModelForm):
    class Meta:
        model = Pet
        fields = [
            "name",
            "species",
            "breed",
            "color",
            "gender",
            "date_of_birth",
            "weight",
            "registration_number",
            "photo",
        ]
        widgets = {
            "name": forms.TextInput(attrs={"placeholder": "Pet's name"}),
            # species and breed are plain text inputs; a cascading
            # dropdown is planned for later.
            "species": forms.TextInput(attrs={"placeholder": "e.g. Dog, Cat, Bird"}),
            "breed": forms.TextInput(
                attrs={"placeholder": "e.g. Aspin, Persian (optional)"}
            ),
            "color": forms.TextInput(
                attrs={"placeholder": "e.g. Brown and white (optional)"}
            ),
            "gender": forms.Select(),
            "date_of_birth": forms.DateInput(
                attrs={"type": "date"},
                format="%Y-%m-%d",
            ),
            "weight": forms.NumberInput(
                attrs={"placeholder": "Weight in kg (optional)", "step": "0.01"}
            ),
            "registration_number": forms.TextInput(
                attrs={"placeholder": "Government-issued number (optional)"}
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # date_of_birth is optional in the model — reflect that in the form
        self.fields["date_of_birth"].required = False
        # Explicitly mark optional fields so the template can label them clearly
        self.fields["breed"].required = False
        self.fields["color"].required = False
        self.fields["weight"].required = False
        self.fields["registration_number"].required = False
        self.fields["photo"].required = False

    def clean_name(self):
        value = self.cleaned_data.get("name", "").strip()
        if not value:
            raise forms.ValidationError("Pet name is required.")
        return value

    def clean_species(self):
        value = self.cleaned_data.get("species", "").strip()
        if not value:
            raise forms.ValidationError("Species is required.")
        return value

    def clean_weight(self):
        weight = self.cleaned_data.get("weight")
        if weight is not None and weight <= 0:
            raise forms.ValidationError("Weight must be greater than 0.")
        return weight

    def clean_date_of_birth(self):
        from django.utils import timezone

        dob = self.cleaned_data.get("date_of_birth")
        if dob and dob > timezone.localdate():
            raise forms.ValidationError("Date of birth cannot be in the future.")
        return dob


class PetDeletionRequestForm(forms.ModelForm):
    class Meta:
        model = PetDeletionRequest
        fields = ["reason", "reason_detail"]
        widgets = {
            "reason": forms.Select(),
            "reason_detail": forms.Textarea(
                attrs={
                    "rows": 3,
                    "placeholder": (
                        "Please describe the reason (required if Other is selected)"
                    ),
                }
            ),
        }

    def clean(self):
        cleaned_data = super().clean()
        reason = cleaned_data.get("reason")
        reason_detail = cleaned_data.get("reason_detail", "").strip()

        # If reason is OTHER, detail is required
        if reason == PetDeletionRequest.OTHER and not reason_detail:
            raise forms.ValidationError(
                "Please provide details when selecting Other as the reason."
            )

        return cleaned_data


class AdminPetArchiveForm(forms.Form):
    """
    The pet page's direct Archive (no owner request): the reason is saved
    on an already-approved PetDeletionRequest made by the clinic.
    """

    reason = forms.ChoiceField(
        label="Reason",
        choices=[("", "Choose one")] + PetDeletionRequest.REASON_CHOICES,
        error_messages={"required": "Choose a reason."},
    )
    reason_detail = forms.CharField(
        label="Details",
        required=False,
        strip=True,
        help_text="Needed when the reason is Other.",
        widget=forms.Textarea(
            attrs={
                "rows": 3,
                "placeholder": "For example: the owner said he passed away last week.",
            }
        ),
    )

    def clean(self):
        cleaned_data = super().clean()
        reason = cleaned_data.get("reason")
        detail = cleaned_data.get("reason_detail")
        if reason == PetDeletionRequest.OTHER and not detail:
            self.add_error("reason_detail", "Please write a short reason.")
        return cleaned_data


PROVINCE_MARINDUQUE = "Marinduque"
PROVINCE_OTHER = "other"


class _ListChoiceField(forms.ChoiceField):
    """A select whose options only guide the pick: any value passes here,
    and the form's clean() checks it against the chosen town with a plain
    message (instead of "Select a valid choice")."""

    def valid_value(self, value):
        return True


class AdminPetOwnerForm(forms.ModelForm):
    """
    Used by Admin to add an owner or edit their details.
    Does not include password — email is handled as an extra field
    since it lives on the User model, not PetOwner.

    Address: in Marinduque mode the town and barangay are picked from the
    PSA list (address_town / address_barangay) and saved into the model's
    text fields; in "other" mode the text fields are typed. No model change.
    """

    # Extra field — not part of PetOwner model, saved to owner.user.email
    email = forms.EmailField(
        label="Email",
        required=False,
        widget=forms.EmailInput(attrs={"placeholder": "Leave empty if they have none"}),
    )

    # Form-only address fields. The province always has a value (the select
    # has no empty option); clean() refuses a missing one without a red *.
    address_province = forms.ChoiceField(
        label="Province",
        required=False,
        choices=[
            (PROVINCE_MARINDUQUE, "Marinduque"),
            (PROVINCE_OTHER, "Another province"),
        ],
        widget=forms.Select(attrs={"data-address-province": ""}),
    )
    address_town = forms.ChoiceField(
        label="Town",
        required=False,
        error_messages={"invalid_choice": "Choose a town from the list."},
    )
    address_barangay = _ListChoiceField(
        label="Barangay",
        required=False,
        help_text="The list fills in after you choose the town.",
    )

    class Meta:
        model = PetOwner
        fields = [
            "first_name",
            "last_name",
            "contact_number",
            "address_line",
            "barangay",
            "city_municipality",
            "province",
            "profile_photo",
        ]
        labels = {
            "first_name": "First name",
            "last_name": "Last name",
            "contact_number": "Phone number",
            "address_line": "House no., street or purok",
            "barangay": "Barangay",
            "city_municipality": "Town or city",
            "province": "Province name",
        }
        widgets = {
            "contact_number": forms.TextInput(
                attrs={"type": "tel", "placeholder": "For example: 0917 123 4567"}
            ),
            "address_line": forms.TextInput(
                attrs={"placeholder": "For example: Purok 3"}
            ),
            "barangay": forms.TextInput(attrs={"placeholder": "For example: Sabang"}),
            "city_municipality": forms.TextInput(
                attrs={"placeholder": "For example: Lipa City"}
            ),
            "province": forms.TextInput(attrs={"placeholder": "For example: Batangas"}),
        }

    def clean_first_name(self):
        value = self.cleaned_data.get("first_name", "").strip()
        if not value:
            raise forms.ValidationError("First name is required.")
        return value.title()

    def clean_last_name(self):
        value = self.cleaned_data.get("last_name", "").strip()
        if not value:
            raise forms.ValidationError("Last name is required.")
        return value.title()

    def clean_address_line(self):
        value = self.cleaned_data.get("address_line", "").strip()
        return " ".join(value.split())

    def clean_barangay(self):
        value = self.cleaned_data.get("barangay", "").strip()
        return value.title()

    def clean_city_municipality(self):
        value = self.cleaned_data.get("city_municipality", "").strip()
        return value.title()

    def clean_province(self):
        value = self.cleaned_data.get("province", "").strip()
        return value.title()

    def __init__(self, *args, **kwargs):
        # Accept the current user so we can exclude them from uniqueness check
        self.current_user = kwargs.pop("current_user", None)
        super().__init__(*args, **kwargs)
        # Pre-populate email from the linked User account
        if self.current_user:
            self.fields["email"].initial = self.current_user.email
        self.fields["email"].help_text = self._email_help()

        # Read the saved address before validation can change the instance
        picked, hand_typed = self._saved_address_mode()
        for name, value in picked.items():
            self.fields[name].initial = value
        if self.is_bound:
            # A hand-typed address keeps its note while "other" stays chosen
            self.address_hand_typed = hand_typed and self.address_mode == "other"
        else:
            self.address_hand_typed = hand_typed
        self._set_address_choices()

    # ── Email ────────────────────────────────────────────────────────────────

    @property
    def email_optional(self):
        """True without an email: the label then ends with "(optional)"."""
        return not (self.current_user and self.current_user.email)

    def _email_help(self):
        if not self.instance.pk:
            return (
                "We'll email them a link to set a password, so they can book "
                "online and see their pets' records."
            )
        name = self.instance.full_name
        if not self.email_optional:
            return (
                f"This is the email {name} logs in with. "
                "Change it only if they asked you to."
            )
        return (
            f"Adding an email lets {name} log in. "
            "We'll send them a link to set a password."
        )

    # ── Address ──────────────────────────────────────────────────────────────

    def _saved_address_mode(self):
        """
        Initial picks for the edit page, and whether the saved address was
        typed by hand (shown in "other" mode with a note):
        - province Marinduque (any case) + a known town + an empty or known
          barangay → Marinduque mode with them picked (official spelling);
          province Marinduque with no town and no barangay → Marinduque
          mode, nothing picked (that's how Marinduque + street only saves);
        - else any address field filled → "other" mode, saved text as is;
          the note only when the province is Marinduque but the town or
          barangay isn't on the list (another province gets no note);
        - else → Marinduque mode, nothing picked.
        """
        owner = self.instance
        marinduque = {"address_province": PROVINCE_MARINDUQUE}
        if not owner.pk:
            return marinduque, False
        town = match_town(owner.city_municipality)
        barangay = match_barangay(town, owner.barangay) if town else None
        is_marinduque = (owner.province or "").strip().casefold() == "marinduque"
        if is_marinduque and town and (barangay or not owner.barangay.strip()):
            picked = {"address_town": town, "address_barangay": barangay or ""}
            return {**marinduque, **picked}, False
        no_town = not owner.city_municipality.strip() and not owner.barangay.strip()
        if is_marinduque and no_town:
            return marinduque, False
        saved = (
            owner.province,
            owner.city_municipality,
            owner.barangay,
            owner.address_line,
        )
        if any(value.strip() for value in saved):
            return {"address_province": PROVINCE_OTHER}, is_marinduque
        return marinduque, False

    @property
    def address_mode(self):
        """The address mode: "marinduque" (dropdowns) or "other" (typed)."""
        if self.is_bound:
            province = self.data.get(self.add_prefix("address_province"))
        else:
            province = self.fields["address_province"].initial
        return "other" if province == PROVINCE_OTHER else "marinduque"

    def _set_address_choices(self):
        """Town options, and the barangay options of the chosen town (the
        posted one on a POST, so a re-render keeps the list)."""
        town_field = self.fields["address_town"]
        town_field.choices = [("", "Choose a town")] + [
            (town, town) for town in marinduque_towns()
        ]
        town_field.widget.attrs.update(
            {
                "data-address-town": "",
                "hx-get": reverse("admin_address_barangays"),
                "hx-trigger": "change",
                "hx-target": f"#{self['address_barangay'].auto_id}",
                "hx-swap": "innerHTML",
                "hx-include": "this",
            }
        )

        if self.is_bound:
            town = self.data.get(self.add_prefix("address_town"))
        else:
            town = town_field.initial
        barangay_field = self.fields["address_barangay"]
        barangay_field.choices = barangay_choices(town)
        barangay_field.widget.attrs["data-address-barangay"] = ""
        if not match_town(town):
            barangay_field.widget.attrs["disabled"] = True

    def clean(self):
        cleaned = super().clean()
        if self.errors.get("address_province") or self.errors.get("address_town"):
            return cleaned
        province = cleaned.get("address_province")
        if not province:
            self.add_error("address_province", "Choose a province.")
            return cleaned
        if province == PROVINCE_OTHER:
            # Typed fields, cleaned by clean_barangay / clean_province etc.
            return cleaned

        town = match_town(cleaned.get("address_town"))
        typed_barangay = cleaned.get("address_barangay") or ""
        barangay = ""
        if typed_barangay and not town:
            self.add_error("address_town", "Choose the town first.")
            return cleaned
        if typed_barangay:
            barangay = match_barangay(town, typed_barangay)
            if not barangay:
                self.add_error("address_barangay", f"Choose a barangay in {town}.")
                return cleaned

        # Empty rule: nothing picked and no street → no "Marinduque" on its own
        has_address = town or cleaned.get("address_line")
        cleaned["province"] = PROVINCE_MARINDUQUE if has_address else ""
        cleaned["city_municipality"] = town or ""
        cleaned["barangay"] = barangay
        return cleaned

    # ── Page helpers ─────────────────────────────────────────────────────────

    @property
    def error_count(self):
        """Fields with errors, plus 1 when there are form-wide errors."""
        fields = [name for name in self.errors if name != NON_FIELD_ERRORS]
        return len(fields) + (1 if self.non_field_errors() else 0)

    def clean_email(self):
        # Saved lowercase; the check ignores case so "Ana@x.com" matches "ana@x.com"
        email = self.cleaned_data.get("email", "").strip().lower()
        if not email:
            # Once added, an email can be changed but not cleared
            if self.current_user and self.current_user.email:
                raise forms.ValidationError(
                    "An email can't be removed once added. "
                    "Type a new email to change it."
                )
            return email
        from django.contrib.auth import get_user_model

        User = get_user_model()
        qs = User.objects.filter(email__iexact=email)
        if self.current_user:
            qs = qs.exclude(pk=self.current_user.pk)
        if qs.exists():
            raise forms.ValidationError(
                "This email is already used by another account."
            )
        return email


class AdminPetForm(forms.ModelForm):
    """
    Used by Admin to manually add or edit a pet.
    Includes all pet fields — Admin has full access unlike pet owners
    who can only edit minor details.
    """

    class Meta:
        model = Pet
        fields = [
            "name",
            "species",
            "breed",
            "color",
            "gender",
            "date_of_birth",
            "weight",
            "registration_number",
            "photo",
        ]
        widgets = {
            "name": forms.TextInput(attrs={"placeholder": "Pet's name"}),
            "species": forms.TextInput(attrs={"placeholder": "e.g. Dog, Cat, Bird"}),
            "breed": forms.TextInput(
                attrs={"placeholder": "e.g. Aspin, Persian (optional)"}
            ),
            "color": forms.TextInput(
                attrs={"placeholder": "e.g. Brown and white (optional)"}
            ),
            "gender": forms.Select(),
            "date_of_birth": forms.DateInput(
                attrs={"type": "date"},
                format="%Y-%m-%d",
            ),
            "weight": forms.NumberInput(
                attrs={
                    "placeholder": "Weight in kg (optional)",
                    "step": "0.01",
                }
            ),
            "registration_number": forms.TextInput(
                attrs={"placeholder": "Government-issued number (optional)"}
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["date_of_birth"].required = False
        self.fields["breed"].required = False
        self.fields["color"].required = False
        self.fields["weight"].required = False
        self.fields["registration_number"].required = False
        self.fields["photo"].required = False

    def clean_name(self):
        value = self.cleaned_data.get("name", "").strip()
        if not value:
            raise forms.ValidationError("Pet name is required.")
        return value.title()

    def clean_species(self):
        value = self.cleaned_data.get("species", "").strip()
        if not value:
            raise forms.ValidationError("Species is required.")
        return value.title()

    def clean_weight(self):
        weight = self.cleaned_data.get("weight")
        if weight is not None and weight <= 0:
            raise forms.ValidationError("Weight must be greater than 0.")
        return weight

    def clean_date_of_birth(self):
        from django.utils import timezone

        dob = self.cleaned_data.get("date_of_birth")
        if dob and dob > timezone.localdate():
            raise forms.ValidationError("Date of birth cannot be in the future.")
        return dob


class AdminOwnerEmailForm(forms.Form):
    """
    Adds or updates the email address on a walk-in owner's User account.
    Only shown when owner.user.email is blank.
    """

    email = forms.EmailField(
        widget=forms.EmailInput(attrs={"placeholder": "owner@email.com"}),
    )

    def __init__(self, *args, current_user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.current_user = current_user

    def clean_email(self):
        # Saved lowercase; the check ignores case so "Ana@x.com" matches "ana@x.com"
        email = self.cleaned_data.get("email", "").strip().lower()
        from django.contrib.auth import get_user_model

        User = get_user_model()
        # Check uniqueness but exclude the current user
        qs = User.objects.filter(email__iexact=email)
        if self.current_user:
            qs = qs.exclude(pk=self.current_user.pk)
        if qs.exists():
            raise forms.ValidationError(
                "This email is already used by another account."
            )
        return email
