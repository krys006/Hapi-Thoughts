# notifications/forms.py

from django import forms
from .models import NotificationPreference


class NotificationPreferenceForm(forms.ModelForm):
    class Meta:
        model = NotificationPreference
        fields = [
            "email_appointment_reminders",
            "email_appointment_status",
            "email_billing",
            "email_vaccination",
            "email_followup",
            "appointment_reminder_days",
            "vaccination_reminder_days",
        ]
        widgets = {
            "appointment_reminder_days": forms.NumberInput(
                attrs={"min": 1, "max": 7, "step": 1}
            ),
            "vaccination_reminder_days": forms.NumberInput(
                attrs={"min": 1, "max": 30, "step": 1}
            ),
        }

    def clean_appointment_reminder_days(self):
        value = self.cleaned_data.get("appointment_reminder_days")
        if value < 1 or value > 7:
            raise forms.ValidationError("Must be between 1 and 7 days.")
        return value

    def clean_vaccination_reminder_days(self):
        value = self.cleaned_data.get("vaccination_reminder_days")
        if value < 1 or value > 30:
            raise forms.ValidationError("Must be between 1 and 30 days.")
        return value
