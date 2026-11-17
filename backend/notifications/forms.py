# notifications/forms.py

from django import forms
from .models import NotificationPreference

# Fixed reminder timings (the fields stay IntegerFields on the model)
APPOINTMENT_REMINDER_DAYS = (0, 1, 2, 3)
VACCINATION_REMINDER_DAYS = (3, 7, 14, 30)
CHOOSE_ONE_MESSAGE = "Choose one of the options."


def reminder_days_label(days):
    """ "On the same day", "1 day before", "2 days before"."""
    if days == 0:
        return "On the same day"
    return f"{days} day{'' if days == 1 else 's'} before"


def _choice_field(days_options):
    return forms.TypedChoiceField(
        choices=[(days, reminder_days_label(days)) for days in days_options],
        coerce=int,
        error_messages={
            "required": CHOOSE_ONE_MESSAGE,
            "invalid_choice": CHOOSE_ONE_MESSAGE,
        },
    )


class NotificationPreferenceForm(forms.ModelForm):
    appointment_reminder_days = _choice_field(APPOINTMENT_REMINDER_DAYS)
    vaccination_reminder_days = _choice_field(VACCINATION_REMINDER_DAYS)

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

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # A saved value outside the list stays selectable as one extra
        # choice, so saving the page never changes it silently.
        for name in ("appointment_reminder_days", "vaccination_reminder_days"):
            field = self.fields[name]
            saved = getattr(self.instance, name, None)
            if saved is None or saved in dict(field.choices):
                continue
            label = f"{reminder_days_label(saved)} (your earlier choice)"
            field.choices = [*field.choices, (saved, label)]
