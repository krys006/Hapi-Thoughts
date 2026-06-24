from django import forms
from django.utils import timezone

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
        cleaned_data = super().clean()
        follow_up_required = cleaned_data.get("follow_up_required")
        follow_up_date = cleaned_data.get("follow_up_date")

        # If follow-up is required, a date must be provided
        if follow_up_required and not follow_up_date:
            raise forms.ValidationError(
                "Please provide a follow-up date when follow-up is required."
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


class TestResultFileForm(forms.ModelForm):
    """
    Admin form — upload a test result file to a medical record.
    """

    class Meta:
        model = TestResultFile
        fields = ["file", "description"]
        widgets = {
            "description": forms.TextInput(
                attrs={"placeholder": "e.g. X-ray, Blood test results"}
            ),
        }


class VaccinationForm(forms.ModelForm):
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

    def clean(self):
        cleaned_data = super().clean()
        vaccine_name = cleaned_data.get("vaccine_name")
        custom_name = cleaned_data.get("custom_vaccine_name")

        # Custom name required when Other is selected
        if vaccine_name == Vaccination.OTHER and not custom_name:
            raise forms.ValidationError(
                "Please specify the vaccine name when selecting Other."
            )

        return cleaned_data


class VaccinationCorrectionForm(forms.ModelForm):
    """
    Admin form — mark a vaccination record as corrected.
    Does not edit the original data — only adds a correction note.
    """

    class Meta:
        model = Vaccination
        fields = ["correction_note"]
        widgets = {
            "correction_note": forms.Textarea(
                attrs={
                    "rows": 3,
                    "placeholder": "Explain what was incorrect and what the correct information is.",
                }
            ),
        }

    def clean_correction_note(self):
        note = self.cleaned_data.get("correction_note", "").strip()
        if not note:
            raise forms.ValidationError("A correction note is required.")
        return note


# Clinical fields — correction note required if any of these change
CLINICAL_FIELDS = [
    "vaccine_name",
    "custom_vaccine_name",
    "date_administered",
    "next_due_date",
    "weight_at_vaccination",
    "site_of_injection",
]


class VaccinationEditForm(forms.ModelForm):
    """
    Admin form — edit a vaccination record (Option C).
    All fields are editable.
    Correction note is required only if a clinical field changed.
    is_corrected is set automatically in the view if clinical fields changed.
    """

    correction_note = forms.CharField(
        widget=forms.Textarea(
            attrs={
                "rows": 2,
                "placeholder": "Required if you changed vaccine, date, due date, weight, or site of injection.",
            }
        ),
        required=False,
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
            "correction_note",
        ]
        widgets = {
            "date_administered": forms.DateInput(attrs={"type": "date"}),
            "next_due_date": forms.DateInput(attrs={"type": "date"}),
        }

    def __init__(self, *args, **kwargs):
        # Store the original instance values for comparison in clean()
        self.original = kwargs.get("instance")
        super().__init__(*args, **kwargs)

    def clean(self):
        cleaned_data = super().clean()
        vaccine_name = cleaned_data.get("vaccine_name")
        custom_name = cleaned_data.get("custom_vaccine_name")

        if vaccine_name == Vaccination.OTHER and not custom_name:
            raise forms.ValidationError(
                "Please specify the vaccine name when selecting Other."
            )

        # Check if any clinical field has changed
        if self.original:
            clinical_changed = any(
                str(cleaned_data.get(field, ""))
                != str(getattr(self.original, field) or "")
                for field in CLINICAL_FIELDS
            )
            if clinical_changed and not cleaned_data.get("correction_note", "").strip():
                raise forms.ValidationError(
                    "A correction note is required when changing clinical fields "
                    "(vaccine, date, due date, weight, or site of injection)."
                )

            # Store whether clinical fields changed so the view can use it
            self.clinical_changed = clinical_changed
        else:
            self.clinical_changed = False

        return cleaned_data
