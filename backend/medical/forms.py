import math
import os
from datetime import date
from decimal import Decimal

from django import forms
from django.core.exceptions import NON_FIELD_ERRORS
from django.utils import timezone
from django.utils.dateformat import format as date_format

from .models import MedicalRecord, PrescriptionItem, TestResultFile, Vaccination
from .utils import FOLLOW_UP_REMINDER_DAYS


class ErrorCountMixin:
    """error_count for the error summary box
    (admin/pets/owner_form_parts/error_summary.html)."""

    @property
    def error_count(self):
        """Fields with errors, plus 1 when there are form-wide errors."""
        fields = [name for name in self.errors if name != NON_FIELD_ERRORS]
        return len(fields) + (1 if self.non_field_errors() else 0)


class MedicalRecordForm(ErrorCountMixin, forms.ModelForm):
    """
    Admin form — create or edit a medical record.
    Pet and appointment are set in the view, not in this form.
    """

    class Meta:
        model = MedicalRecord
        fields = [
            "record_date",
            "diagnosis",
            "symptoms",
            "treatment_given",
            "public_notes",
            "private_notes",
            "follow_up_required",
            "follow_up_date",
        ]
        labels = {
            "record_date": "Visit date",
            "symptoms": "Symptoms",
            "diagnosis": "Diagnosis",
            "treatment_given": "Treatment given",
            "public_notes": "Note for the owner",
            "private_notes": "Private notes",
            "follow_up_required": "Needs a follow-up visit",
            "follow_up_date": "Follow-up date",
        }
        help_texts = {
            "diagnosis": (
                "The lists show the start of this, so begin with the main finding."
            ),
            "follow_up_date": (
                "Must be after the visit date. A reminder goes to the owner "
                f"{FOLLOW_UP_REMINDER_DAYS} days before."
            ),
        }
        widgets = {
            "record_date": forms.DateInput(attrs={"type": "date"}),
            "symptoms": forms.Textarea(
                attrs={
                    "rows": 3,
                    "placeholder": "What the owner noticed and what you saw",
                }
            ),
            "diagnosis": forms.Textarea(
                attrs={"rows": 2, "placeholder": "For example: Ear infection, left ear"}
            ),
            "treatment_given": forms.Textarea(
                attrs={"rows": 3, "placeholder": "What you did at the clinic today"}
            ),
            "public_notes": forms.Textarea(
                attrs={
                    "rows": 3,
                    "placeholder": (
                        "Care at home, what to watch for, when to come back"
                    ),
                }
            ),
            "private_notes": forms.Textarea(
                attrs={"rows": 3, "placeholder": "Anything for the clinic only"}
            ),
            "follow_up_date": forms.DateInput(attrs={"type": "date"}),
        }
        error_messages = {"record_date": {"required": "Pick the visit date."}}

    # Shown under the box while it is unticked (the date block is hidden)
    follow_up_off_help = (
        "Tick it to pick a date. The owner gets a reminder "
        f"{FOLLOW_UP_REMINDER_DAYS} days before."
    )

    @property
    def follow_up_ticked(self):
        """The box as posted (re-render) or as saved / initial."""
        return bool(self["follow_up_required"].value())

    def clean(self):
        """
        Date rules (Manila date):
        - the visit date can't be in the future (today is fine)
        - a follow-up needs a date, after the visit date
        - with the follow-up box unticked the date is cleared
        """
        cleaned_data = super().clean()
        record_date = cleaned_data.get("record_date")
        follow_up_date = cleaned_data.get("follow_up_date")

        if record_date and record_date > timezone.localdate():
            self.add_error("record_date", "The visit date can't be in the future.")

        if not cleaned_data.get("follow_up_required"):
            cleaned_data["follow_up_date"] = None
        elif not follow_up_date:
            # A date that didn't parse already has its own error
            if "follow_up_date" not in self.errors:
                self.add_error(
                    "follow_up_date",
                    "Pick the follow-up date, or untick “Needs a follow-up visit”.",
                )
        elif record_date and follow_up_date <= record_date:
            self.add_error(
                "follow_up_date", "The follow-up date must be after the visit date."
            )

        return cleaned_data


class PrescriptionItemForm(forms.ModelForm):
    """
    Admin form — add a prescription item to a medical record (the record
    page's Add medicine dialog). Only the name is required.
    """

    class Meta:
        model = PrescriptionItem
        fields = [
            "medicine_name",
            "dosage",
            "frequency",
            "duration",
            "notes",
        ]
        labels = {
            "medicine_name": "Medicine name",
            "dosage": "Dose",
            "frequency": "How often",
            "duration": "How long",
            "notes": "Instructions",
        }
        widgets = {
            "medicine_name": forms.TextInput(
                attrs={"placeholder": "For example: Amoxicillin 250 mg"}
            ),
            "dosage": forms.TextInput(attrs={"placeholder": "1 tablet"}),
            "frequency": forms.TextInput(attrs={"placeholder": "Twice a day"}),
            "duration": forms.TextInput(attrs={"placeholder": "7 days"}),
            "notes": forms.Textarea(
                attrs={"rows": 2, "placeholder": "For example: give with food"}
            ),
        }
        error_messages = {
            "medicine_name": {"required": "Type the medicine name."},
        }


TEST_FILE_TYPES = (".pdf", ".jpg", ".jpeg", ".png")
TEST_FILE_MAX_MB = 10
MB = 1024 * 1024


class TestResultFileForm(forms.ModelForm):
    """
    Admin form — upload a test result file to a medical record.
    Only PDF, JPG or PNG (any letter case), up to 10 MB.
    """

    class Meta:
        model = TestResultFile
        fields = ["file", "description"]
        labels = {"file": "File", "description": "What is it?"}
        widgets = {
            "file": forms.FileInput(attrs={"accept": ",".join(TEST_FILE_TYPES)}),
            "description": forms.TextInput(
                attrs={"placeholder": "For example: Blood test, X-ray"}
            ),
        }
        error_messages = {"file": {"required": "Choose a file to add."}}

    def clean_file(self):
        upload = self.cleaned_data.get("file")
        if not upload:
            return upload
        if os.path.splitext(upload.name)[1].lower() not in TEST_FILE_TYPES:
            raise forms.ValidationError("Only PDF, JPG or PNG files can be added.")
        if upload.size > TEST_FILE_MAX_MB * MB:
            # Rounded up, so a file just over the limit never reads "10.0 MB"
            size = math.ceil(upload.size * 10 / MB) / 10
            raise forms.ValidationError(
                f"This file is {size:.1f} MB. "
                f"Please choose one up to {TEST_FILE_MAX_MB} MB."
            )
        return upload


VACCINATION_FIELDS = [
    "vaccine_name",
    "custom_vaccine_name",
    "date_administered",
    "weight_at_vaccination",
    "next_due_date",
    "batch_number",
    "manufacturer",
    "administered_by",
    "site_of_injection",
]
VACCINATION_LABELS = {
    "vaccine_name": "Vaccine",
    "custom_vaccine_name": "Vaccine name",
    "date_administered": "Date given",
    "next_due_date": "Next dose due",
    "weight_at_vaccination": "Weight (kg)",
    "site_of_injection": "Where it was given",
    "batch_number": "Batch / lot no.",
    "manufacturer": "Maker",
    "administered_by": "Given by",
}
VACCINATION_WIDGETS = {
    # data-vaccine-choice: vaccination_form.js shows "Vaccine name" for Other
    "vaccine_name": forms.Select(attrs={"data-vaccine-choice": True}),
    "custom_vaccine_name": forms.TextInput(attrs={"placeholder": "For example: FVRCP"}),
    "date_administered": forms.DateInput(attrs={"type": "date"}),
    "next_due_date": forms.DateInput(attrs={"type": "date"}),
    "weight_at_vaccination": forms.NumberInput(
        attrs={"placeholder": "For example: 12.5"}
    ),
    "site_of_injection": forms.TextInput(
        attrs={"placeholder": "For example: Right hind leg"}
    ),
    "batch_number": forms.TextInput(attrs={"placeholder": "For example: A2311-07"}),
    "manufacturer": forms.TextInput(attrs={"placeholder": "For example: Zoetis"}),
}
# Vaccination reminders follow each owner's own setting, so no fixed number
# of days is named here (VACCINATION_REMINDER_DEFAULT_DAYS in utils.py)
NEXT_DOSE_HELP = "Leave empty if no next dose."
VACCINATION_HELP_TEXTS = {"next_due_date": NEXT_DOSE_HELP}
VACCINATION_ERRORS = {
    "vaccine_name": {"required": "Choose the vaccine."},
    "date_administered": {"required": "Pick the date given."},
}


class VaccinationRulesMixin(ErrorCountMixin):
    """
    Rules shared by the add and edit vaccination forms:
    - a listed vaccine clears any typed name; Other needs a typed name
    - the date given can't be in the future (Manila date)
    - the next dose, when set, must be after the date given
    The Vaccine select reads "Choose one", the model's vaccines in their
    order, then "Other (type it)" (the pet form's species pattern).
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["vaccine_name"].choices = [("", "Choose one")] + [
            (value, "Other (type it)" if value == Vaccination.OTHER else label)
            for value, label in Vaccination.VACCINE_CHOICES
        ]

    @property
    def shows_vaccine_name(self):
        """Other is chosen (as posted on a re-render, else as saved), so the
        "Vaccine name" box starts open."""
        return self["vaccine_name"].value() == Vaccination.OTHER

    def clean(self):
        cleaned_data = super().clean()
        vaccine_name = cleaned_data.get("vaccine_name")

        if vaccine_name and vaccine_name != Vaccination.OTHER:
            cleaned_data["custom_vaccine_name"] = ""
        elif vaccine_name == Vaccination.OTHER and not cleaned_data.get(
            "custom_vaccine_name"
        ):
            self.add_error("custom_vaccine_name", "Type the vaccine name.")

        given = cleaned_data.get("date_administered")
        next_due = cleaned_data.get("next_due_date")
        if given and given > timezone.localdate():
            self.add_error(
                "date_administered", "The date given can't be in the future."
            )
        if given and next_due and next_due <= given:
            self.add_error(
                "next_due_date", "The next dose must be after the date given."
            )

        return cleaned_data


class VaccinationForm(VaccinationRulesMixin, forms.ModelForm):
    """
    Admin form — create a vaccination record.
    Can be linked to a medical record or standalone.
    """

    class Meta:
        model = Vaccination
        fields = VACCINATION_FIELDS
        labels = VACCINATION_LABELS
        widgets = VACCINATION_WIDGETS
        help_texts = {
            **VACCINATION_HELP_TEXTS,
            # The views start Given by with the vet's name (new_vaccination_initial)
            "administered_by": "Filled in with the clinic vet from Clinic settings.",
        }
        error_messages = VACCINATION_ERRORS


# Clinical fields: changing any of these on a saved vaccination is a
# correction and needs a reason. The vaccine and its typed name count once.
CLINICAL_CHANGES = [
    (("vaccine_name", "custom_vaccine_name"), "the vaccine"),
    (("date_administered",), "the date given"),
    (("next_due_date",), "the next dose date"),
    (("weight_at_vaccination",), "the weight"),
    (("site_of_injection",), "where it was given"),
]
# The field whose label names each item on the page ("Date given")
CLINICAL_FIELD = {label: fields[0] for fields, label in CLINICAL_CHANGES}


def _normalised(value):
    """None and "" are equal, strings are stripped, numbers are Decimals."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    return value


def _display_old(value, today):
    """An old clinical value for the error line: 'Tue, Oct 6', '12.5', 'empty'."""
    if value in (None, ""):
        return "empty"
    if isinstance(value, date):
        if value.year == today.year:
            return date_format(value, "D, M j")
        return date_format(value, "D, M j, Y")
    if isinstance(value, Decimal):
        return f"{value.normalize():f}"
    return str(value)


def _join_changes(parts):
    """'a', 'a and b', 'a, b and c'."""
    if len(parts) == 1:
        return parts[0]
    return f"{', '.join(parts[:-1])} and {parts[-1]}"


class VaccinationEditForm(VaccinationRulesMixin, forms.ModelForm):
    """
    Admin form — edit a vaccination record.
    Label fields (batch, manufacturer, given by) change freely.
    A clinical change needs correction_reason; the save adds it to
    correction_note as a dated line (medical/utils.save_vaccination_edit).
    After clean(), changed_clinical lists (label, old value for display).
    """

    correction_reason = forms.CharField(
        label="Reason for a correction",
        required=False,
        widget=forms.Textarea(
            attrs={
                "rows": 2,
                "placeholder": "For example: wrong date typed, it was given the day before",
            }
        ),
    )

    class Meta:
        model = Vaccination
        fields = VACCINATION_FIELDS
        labels = VACCINATION_LABELS
        widgets = VACCINATION_WIDGETS
        help_texts = VACCINATION_HELP_TEXTS
        error_messages = VACCINATION_ERRORS

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Snapshot the saved values: the ModelForm writes the posted
        # values onto self.instance after clean().
        self.before = {
            field: getattr(self.instance, field)
            for fields, _label in CLINICAL_CHANGES
            for field in fields
        }
        # A listed vaccine never keeps a typed name, so compare it as ""
        if self.before["vaccine_name"] != Vaccination.OTHER:
            self.before["custom_vaccine_name"] = ""
        self.before_vaccine = self.instance.display_vaccine_name
        self.changed_clinical = []

    def clean(self):
        cleaned_data = super().clean()
        if self.instance.pk:
            self.changed_clinical = self._clinical_changes(cleaned_data)
        reason = cleaned_data.get("correction_reason", "").strip()
        if self.changed_clinical and not reason:
            parts = [f"{label} (was {old})" for label, old in self.changed_clinical]
            self.add_error(
                "correction_reason",
                f"You changed {_join_changes(parts)}. "
                "Write why, so the change is on record.",
            )
        return cleaned_data

    @property
    def changed_fields(self):
        """changed_clinical with each item's field label instead
        ("Date given", "Vaccine"), for the page's "You changed:" line."""
        return [
            (self.fields[CLINICAL_FIELD[label]].label, old)
            for label, old in self.changed_clinical
        ]

    def _clinical_changes(self, cleaned_data):
        """(label, old value) for each clinical item whose value changed.
        Fields with their own error are skipped."""
        today = timezone.localdate()
        changes = []
        for fields, label in CLINICAL_CHANGES:
            changed = any(
                field in cleaned_data
                and _normalised(cleaned_data[field]) != _normalised(self.before[field])
                for field in fields
            )
            if not changed:
                continue
            if fields[0] == "vaccine_name":
                old = self.before_vaccine
            else:
                old = _display_old(self.before[fields[0]], today)
            changes.append((label, old))
        return changes
