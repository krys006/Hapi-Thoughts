import datetime
from django import forms
from django.utils import timezone

from .models import ClinicSettings, BlockedDate, Appointment
from .utils import is_slot_available, get_available_slots

# ── Weekday and slot duration choices ────────────────────────────────────────
WEEKDAY_CHOICES = [
    (0, "Monday"),
    (1, "Tuesday"),
    (2, "Wednesday"),
    (3, "Thursday"),
    (4, "Friday"),
    (5, "Saturday"),
    (6, "Sunday"),
]

SLOT_DURATION_CHOICES = [
    (30, "30 minutes"),
    (60, "60 minutes"),
]


class ClinicSettingsForm(forms.ModelForm):
    """
    Form for Admin to configure clinic schedule and contact details.
    """

    working_days = forms.MultipleChoiceField(
        choices=WEEKDAY_CHOICES,
        widget=forms.CheckboxSelectMultiple,
        required=True,
        help_text="Select the days the clinic is open.",
    )

    slot_duration_minutes = forms.ChoiceField(
        choices=SLOT_DURATION_CHOICES,
        help_text="Duration of each appointment slot.",
    )

    class Meta:
        model = ClinicSettings
        fields = [
            "clinic_name",
            "address",
            "contact_number",
            "email",
            "logo",
            "opening_time",
            "closing_time",
            "slot_duration_minutes",
            "working_days",
            "same_day_cutoff_time",
            "booking_limit_days",
            "notification_email",
        ]
        widgets = {
            "opening_time": forms.TimeInput(attrs={"type": "time"}),
            "closing_time": forms.TimeInput(attrs={"type": "time"}),
            "same_day_cutoff_time": forms.TimeInput(attrs={"type": "time"}),
            "address": forms.Textarea(attrs={"rows": 3}),
        }

    def clean(self):
        cleaned_data = super().clean()
        opening = cleaned_data.get("opening_time")
        closing = cleaned_data.get("closing_time")
        cutoff = cleaned_data.get("same_day_cutoff_time")

        if opening and closing and closing <= opening:
            raise forms.ValidationError(
                "Closing time must be after opening time."
            )

        if opening and closing and cutoff:
            if not (opening <= cutoff <= closing):
                raise forms.ValidationError(
                    "Same-day cutoff time must be between opening and closing time."
                )

        return cleaned_data

    def clean_working_days(self):
        days = self.cleaned_data.get("working_days", [])
        return [int(d) for d in days]

    def clean_slot_duration_minutes(self):
        return int(self.cleaned_data["slot_duration_minutes"])


class BlockedDateForm(forms.ModelForm):
    """
    Form for Admin to block a specific date.
    """

    class Meta:
        model = BlockedDate
        fields = ["date", "reason"]
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}),
            "reason": forms.TextInput(
                attrs={"placeholder": "e.g. Public holiday, Rest day"}
            ),
        }

    def clean_date(self):
        date = self.cleaned_data.get("date")
        if date and date < timezone.now().date():
            raise forms.ValidationError("Cannot block a date in the past.")
        return date


class AppointmentBookingForm(forms.Form):
    """
    Pet Owner form to book an appointment.
    Uses a plain Form (not ModelForm) so we can control
    the pet queryset and slot choices dynamically per request.
    """

    pet = forms.ModelChoiceField(
        queryset=None,  # Set in __init__ to scope to the logged-in owner
        empty_label="Select a pet",
        help_text="Which pet is this appointment for?",
    )
    service = forms.ModelChoiceField(
        queryset=None,  # Set in __init__ to active services only
        empty_label="Select a service",
        required=False,
        help_text="Select the primary service for this visit.",
    )
    date = forms.DateField(
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text="Select an available date.",
    )
    time = forms.ChoiceField(
        choices=[("", "Select a date first")],
        help_text="Select an available time slot.",
    )
    reason = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 3}),
        required=False,
        help_text="Briefly describe the reason for the visit.",
    )

    def __init__(self, owner, *args, **kwargs):
        super().__init__(*args, **kwargs)

        from pets.models import Pet
        from billing.models import Service

        # Scope pet choices to this owner's non-archived pets only
        self.fields["pet"].queryset = Pet.objects.filter(
            owner=owner,
            is_archived=False,
        )

        # Only show active services to pet owners
        self.fields["service"].queryset = Service.objects.filter(
            status=Service.ACTIVE,
        )

        # If a date was submitted, populate time slot choices for that date
        submitted_date = None
        if args and args[0]:  # args[0] is the POST data dict
            date_str = args[0].get("date")
            if date_str:
                try:
                    submitted_date = datetime.date.fromisoformat(date_str)
                except ValueError:
                    pass

        if submitted_date:
            slots = get_available_slots(submitted_date)
            if slots:
                self.fields["time"].choices = [("", "Select a time")] + [
                    (s.strftime("%H:%M:%S"), s.strftime("%I:%M %p"))
                    for s in slots
                ]
            else:
                self.fields["time"].choices = [
                    ("", "No slots available for this date")
                ]

    def clean(self):
        cleaned_data = super().clean()
        date = cleaned_data.get("date")
        time_str = cleaned_data.get("time")

        if not date or not time_str:
            return cleaned_data

        # Parse the time string back to a time object
        try:
            time = datetime.time.fromisoformat(time_str)
        except ValueError:
            raise forms.ValidationError("Invalid time slot selected.")

        # Final slot availability check at submission time
        # (slot may have been taken since the form was loaded)
        if not is_slot_available(date, time):
            raise forms.ValidationError(
                "This slot is no longer available. Please select another."
            )

        # Store parsed time object for the view to use
        cleaned_data["time"] = time
        return cleaned_data

    def clean_date(self):
        date = self.cleaned_data.get("date")
        if not date:
            return date

        clinic = ClinicSettings.objects.first()
        today = timezone.now().date()

        if date < today:
            raise forms.ValidationError("Cannot book an appointment in the past.")

        if clinic and date > today + datetime.timedelta(days=clinic.booking_limit_days):
            raise forms.ValidationError(
                f"Bookings are only allowed up to "
                f"{clinic.booking_limit_days} days in advance."
            )

        return date


class AppointmentRescheduleForm(forms.Form):
    """
    Form for Pet Owner to request a reschedule.
    Requires a reason and a new date + time slot.
    """

    date = forms.DateField(
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text="Select a new date.",
    )
    time = forms.ChoiceField(
        choices=[("", "Select a date first")],
        help_text="Select a new time slot.",
    )
    reschedule_reason = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Why do you need to reschedule?",
    )

    def __init__(self, appointment, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Store appointment so clean() can exclude it from conflict check
        self.appointment = appointment

        submitted_date = None
        if args and args[0]:
            date_str = args[0].get("date")
            if date_str:
                try:
                    submitted_date = datetime.date.fromisoformat(date_str)
                except ValueError:
                    pass

        if submitted_date:
            slots = get_available_slots(submitted_date)
            if slots:
                self.fields["time"].choices = [("", "Select a time")] + [
                    (s.strftime("%H:%M:%S"), s.strftime("%I:%M %p"))
                    for s in slots
                ]
            else:
                self.fields["time"].choices = [
                    ("", "No slots available for this date")
                ]

    def clean(self):
        cleaned_data = super().clean()
        date = cleaned_data.get("date")
        time_str = cleaned_data.get("time")

        if not date or not time_str:
            return cleaned_data

        try:
            time = datetime.time.fromisoformat(time_str)
        except ValueError:
            raise forms.ValidationError("Invalid time slot selected.")

        # Exclude the current appointment from conflict check
        if not is_slot_available(date, time, exclude_appointment_pk=self.appointment.pk):
            raise forms.ValidationError(
                "This slot is no longer available. Please select another."
            )

        cleaned_data["time"] = time
        return cleaned_data

    def clean_date(self):
        date = self.cleaned_data.get("date")
        if not date:
            return date

        clinic = ClinicSettings.objects.first()
        today = timezone.now().date()

        if date < today:
            raise forms.ValidationError("Cannot reschedule to a past date.")

        if clinic and date > today + datetime.timedelta(days=clinic.booking_limit_days):
            raise forms.ValidationError(
                f"Bookings are only allowed up to "
                f"{clinic.booking_limit_days} days in advance."
            )

        return date


class AppointmentCancellationForm(forms.Form):
    """
    Form for Pet Owner to cancel an appointment.
    Requires a predefined reason.
    """

    cancellation_reason = forms.ChoiceField(
        choices=Appointment.CANCEL_CHOICES,
        help_text="Why are you cancelling?",
    )
    cancellation_detail = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 3}),
        required=False,
        help_text="Additional detail (required if reason is Other).",
    )

    def clean(self):
        cleaned_data = super().clean()
        reason = cleaned_data.get("cancellation_reason")
        detail = cleaned_data.get("cancellation_detail")

        if reason == Appointment.OTHER and not detail:
            raise forms.ValidationError(
                "Please provide details when selecting Other."
            )

        return cleaned_data
    

class AdminAppointmentNotesForm(forms.Form):
    """
    Admin form — add or edit notes on an appointment.
    """
    notes = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 3}),
        required=False,
        help_text="Internal notes about this appointment.",
    )


class AdminAppointmentCancelForm(forms.Form):
    """
    Admin form — cancel an appointment on behalf of the pet owner.
    """
    cancellation_reason = forms.ChoiceField(
        choices=Appointment.CANCEL_CHOICES,
    )
    cancellation_detail = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 3}),
        required=False,
        help_text="Additional detail (required if reason is Other).",
    )

    def clean(self):
        cleaned_data = super().clean()
        reason = cleaned_data.get("cancellation_reason")
        detail = cleaned_data.get("cancellation_detail")
        if reason == Appointment.OTHER and not detail:
            raise forms.ValidationError(
                "Please provide details when selecting Other."
            )
        return cleaned_data


class AdminAppointmentRescheduleForm(forms.Form):
    """
    Admin form — reschedule an appointment on behalf of the pet owner.
    """
    date = forms.DateField(
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    time = forms.ChoiceField(
        choices=[("", "Select a date first")],
    )
    reschedule_reason = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Reason for rescheduling.",
    )

    def __init__(self, appointment, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.appointment = appointment

        submitted_date = None
        if args and args[0]:
            date_str = args[0].get("date")
            if date_str:
                try:
                    submitted_date = datetime.date.fromisoformat(date_str)
                except ValueError:
                    pass

        if submitted_date:
            slots = get_available_slots(submitted_date)
            if slots:
                self.fields["time"].choices = [("", "Select a time")] + [
                    (s.strftime("%H:%M:%S"), s.strftime("%I:%M %p"))
                    for s in slots
                ]
            else:
                self.fields["time"].choices = [
                    ("", "No slots available for this date")
                ]

    def clean(self):
        cleaned_data = super().clean()
        date = cleaned_data.get("date")
        time_str = cleaned_data.get("time")

        if not date or not time_str:
            return cleaned_data

        try:
            time = datetime.time.fromisoformat(time_str)
        except ValueError:
            raise forms.ValidationError("Invalid time slot selected.")

        if not is_slot_available(
            date, time, exclude_appointment_pk=self.appointment.pk
        ):
            raise forms.ValidationError(
                "This slot is no longer available. Please select another."
            )

        cleaned_data["time"] = time
        return cleaned_data

    def clean_date(self):
        date = self.cleaned_data.get("date")
        if not date:
            return date
        today = timezone.now().date()
        if date < today:
            raise forms.ValidationError("Cannot reschedule to a past date.")
        return date


class AdminWalkInAppointmentForm(forms.Form):
    """
    Admin form — create a walk-in appointment directly as CONFIRMED.
    Owner and pet are selected from existing records.
    """
    owner = forms.ModelChoiceField(
        queryset=None,  # Set in __init__
        empty_label="Select a pet owner",
    )
    pet = forms.ModelChoiceField(
        queryset=None,  # Set in __init__, filtered by owner via HTMX
        empty_label="Select a pet",
    )
    service = forms.ModelChoiceField(
        queryset=None,  # Set in __init__
        empty_label="Select a service (optional)",
        required=False,
    )
    date = forms.DateField(
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    time = forms.ChoiceField(
        choices=[("", "Select a date first")],
    )
    reason = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 3}),
        required=False,
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        from pets.models import PetOwner, Pet
        from billing.models import Service

        self.fields["owner"].queryset = PetOwner.objects.filter(
            is_archived=False
        ).order_by("last_name", "first_name")

        # Start with all non-archived pets — filtered by owner via HTMX
        self.fields["pet"].queryset = Pet.objects.filter(
            is_archived=False
        ).order_by("name")

        self.fields["service"].queryset = Service.objects.filter(
            status__in=[Service.ACTIVE, Service.UNLISTED]
        )

        submitted_date = None
        if args and args[0]:
            date_str = args[0].get("date")
            if date_str:
                try:
                    submitted_date = datetime.date.fromisoformat(date_str)
                except ValueError:
                    pass

        if submitted_date:
            slots = get_available_slots(submitted_date)
            if slots:
                self.fields["time"].choices = [("", "Select a time")] + [
                    (s.strftime("%H:%M:%S"), s.strftime("%I:%M %p"))
                    for s in slots
                ]
            else:
                self.fields["time"].choices = [
                    ("", "No slots available for this date")
                ]

    def clean(self):
        cleaned_data = super().clean()
        date = cleaned_data.get("date")
        time_str = cleaned_data.get("time")
        owner = cleaned_data.get("owner")
        pet = cleaned_data.get("pet")

        # Verify the selected pet belongs to the selected owner
        if owner and pet and pet.owner != owner:
            raise forms.ValidationError(
                "The selected pet does not belong to this owner."
            )

        if not date or not time_str:
            return cleaned_data

        try:
            time = datetime.time.fromisoformat(time_str)
        except ValueError:
            raise forms.ValidationError("Invalid time slot selected.")

        # Walk-ins bypass same-day cutoff but still check slot conflicts
        from .models import Appointment
        conflict = Appointment.objects.filter(
            date=date,
            time=time,
        ).exclude(status=Appointment.CANCELLED).exists()

        if conflict:
            raise forms.ValidationError(
                "This slot is already taken. Please select another time."
            )

        cleaned_data["time"] = time
        return cleaned_data

    def clean_date(self):
        date = self.cleaned_data.get("date")
        if not date:
            return date
        today = timezone.now().date()
        if date < today:
            raise forms.ValidationError("Cannot create an appointment in the past.")
        return date