import math
import os
from datetime import date
from decimal import Decimal

from django import forms
from django.utils import timezone
from django.utils.dateformat import format as date_format

from .models import MedicalRecord, PrescriptionItem, TestResultFile, Vaccination


class MedicalRecordForm(forms.ModelForm):
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
        widgets = {
            "record_date": forms.DateInput(attrs={"type": "date"}),
            "diagnosis": forms.Textarea(attrs={"rows": 3}),
            "symptoms": forms.Textarea(attrs={"rows": 3}),
            "treatment_given": forms.Textarea(attrs={"rows": 3}),
            "public_notes": forms.Textarea(attrs={"rows": 3}),
            "private_notes": forms.Textarea(attrs={"rows": 3}),
            "follow_up_date": forms.DateInput(attrs={"type": "date"}),
        }

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
    Admin form — add a prescription item to a medical record.
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
        widgets = {
            "notes": forms.Textarea(attrs={"rows": 2}),
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
        widgets = {
            "file": forms.FileInput(attrs={"accept": ",".join(TEST_FILE_TYPES)}),
            "description": forms.TextInput(
                attrs={"placeholder": "e.g. X-ray, Blood test results"}
            ),
        }

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


class VaccinationRulesMixin:
    """
    Rules shared by the add and edit vaccination forms:
    - a listed vaccine clears any typed name; Other needs a typed name
    - the date given can't be in the future (Manila date)
    - the next dose, when set, must be after the date given
    """

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
        fields = [
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
        widgets = {
            "date_administered": forms.DateInput(attrs={"type": "date"}),
            "next_due_date": forms.DateInput(attrs={"type": "date"}),
        }


# Clinical fields: changing any of these on a saved vaccination is a
# correction and needs a reason. The vaccine and its typed name count once.
CLINICAL_CHANGES = [
    (("vaccine_name", "custom_vaccine_name"), "the vaccine"),
    (("date_administered",), "the date given"),
    (("next_due_date",), "the next dose date"),
    (("weight_at_vaccination",), "the weight"),
    (("site_of_injection",), "where it was given"),
]


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
        fields = [
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
        widgets = {
            "date_administered": forms.DateInput(attrs={"type": "date"}),
            "next_due_date": forms.DateInput(attrs={"type": "date"}),
        }

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
