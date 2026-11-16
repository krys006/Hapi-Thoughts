import datetime
import math
import os

from django import forms
from django.utils import timezone
from django.utils.dateformat import format as date_format

from .models import ClinicSettings, BlockedDate, Appointment
from .utils import (
    SETTINGS_TAB_FIELDS,
    is_slot_available,
    get_available_slots,
    get_clinic_settings,
)

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


LOGO_TYPES = (".jpg", ".jpeg", ".png", ".webp")
LOGO_MAX_MB = 2
MB = 1024 * 1024  # Same definition as the medical test-file check


def check_logo_upload(upload):
    """
    Clinic logo rule: JPG, PNG or WebP (any letter case), up to 2 MB.
    The type is checked first, then the size. Raises ValidationError.
    """
    if os.path.splitext(upload.name)[1].lower() not in LOGO_TYPES:
        raise forms.ValidationError("Only JPG, PNG or WebP files can be used.")
    if upload.size > LOGO_MAX_MB * MB:
        # Rounded up, so a file just over the limit never reads "2.0 MB"
        size = math.ceil(upload.size * 10 / MB) / 10
        raise forms.ValidationError(
            f"This file is {size:.1f} MB. Please choose one up to {LOGO_MAX_MB} MB."
        )


class LogoImageField(forms.ImageField):
    """
    ImageField that runs the logo rule before Django's own image check, so a
    wrong type (for example an SVG) gets the type message, not "not an image".
    Only a newly uploaded file reaches to_python; a kept or cleared logo doesn't.
    """

    def to_python(self, data):
        if data is not None and hasattr(data, "size"):
            check_logo_upload(data)
        return super().to_python(data)


class ClinicSettingsForm(forms.ModelForm):
    """
    Form for Admin to configure clinic schedule and contact details.
    With tab=details, schedule or vet it keeps only that tab's fields
    (SETTINGS_TAB_FIELDS), so saving never touches the other tabs' values.
    """

    working_days = forms.MultipleChoiceField(
        choices=WEEKDAY_CHOICES,
        widget=forms.CheckboxSelectMultiple,
        required=True,
        help_text="Select the days the clinic is open.",
    )

    # TypedChoiceField (coerce=int) so changed_data compares 60 with 60, not
    # the saved 60 with the posted "60".
    slot_duration_minutes = forms.TypedChoiceField(
        label="Appointment slot length",
        choices=SLOT_DURATION_CHOICES,
        coerce=int,
        help_text="Each booking takes one slot.",
    )

    booking_limit_days = forms.IntegerField(
        label="Booking limit (days ahead)",
        min_value=1,
        max_value=365,
        help_text="Owners can book up to this many days ahead. Between 1 and 365.",
        error_messages={
            "min_value": "The booking limit must be at least 1 day.",
            "max_value": "The booking limit can't be more than 365 days.",
        },
    )

    class Meta:
        model = ClinicSettings
        fields = [
            "clinic_name",
            "address",
            "contact_number",
            "email",
            "logo",
            "veterinarian_name",
            "veterinarian_license_number",
            "veterinarian_bio",
            "opening_time",
            "closing_time",
            "slot_duration_minutes",
            "working_days",
            "same_day_cutoff_time",
            "booking_limit_days",
            "notification_email",
        ]
        field_classes = {"logo": LogoImageField}
        labels = {
            "notification_email": "Admin notification email",
            "veterinarian_license_number": "License number",
            "veterinarian_bio": "About the vet",
            "same_day_cutoff_time": "Same-day booking cutoff",
        }
        help_texts = {
            "notification_email": (
                "New visit requests are sent here. "
                "Blank means the admin account email."
            ),
            "veterinarian_bio": "A few sentences. Shown on the clinic page.",
            "same_day_cutoff_time": "No bookings for today after this time.",
            "logo": (
                "JPG, PNG or WebP, up to 2 MB. It shows on receipts and the "
                "clinic page. Choosing a new file replaces the old one."
            ),
        }
        widgets = {
            "opening_time": forms.TimeInput(attrs={"type": "time"}),
            "closing_time": forms.TimeInput(attrs={"type": "time"}),
            "same_day_cutoff_time": forms.TimeInput(attrs={"type": "time"}),
            "address": forms.Textarea(attrs={"rows": 3}),
            "veterinarian_bio": forms.Textarea(attrs={"rows": 4}),
        }

    def __init__(self, *args, tab=None, **kwargs):
        super().__init__(*args, **kwargs)
        if tab in SETTINGS_TAB_FIELDS:
            for name in list(self.fields):
                if name not in SETTINGS_TAB_FIELDS[tab]:
                    del self.fields[name]

    def clean(self):
        cleaned_data = super().clean()
        opening = cleaned_data.get("opening_time")
        closing = cleaned_data.get("closing_time")
        cutoff = cleaned_data.get("same_day_cutoff_time")

        if opening and closing and closing <= opening:
            raise forms.ValidationError("Closing time must be after opening time.")

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
        if date and date < timezone.localdate():
            raise forms.ValidationError("Cannot block a date in the past.")
        if date and BlockedDate.objects.filter(date=date).exists():
            raise forms.ValidationError(
                f"{date_format(date, 'D, M j')} is already blocked. "
                "Pick another date."
            )
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
                    (s.strftime("%H:%M:%S"), s.strftime("%I:%M %p")) for s in slots
                ]
            else:
                self.fields["time"].choices = [("", "No slots available for this date")]

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

        clinic = get_clinic_settings()
        today = timezone.localdate()

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
                    (s.strftime("%H:%M:%S"), s.strftime("%I:%M %p")) for s in slots
                ]
            else:
                self.fields["time"].choices = [("", "No slots available for this date")]

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

        clinic = get_clinic_settings()
        today = timezone.localdate()

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
            raise forms.ValidationError("Please provide details when selecting Other.")

        return cleaned_data


class AdminAppointmentNotesForm(forms.Form):
    """
    Admin form — add or edit notes on an appointment.
    """

    notes = forms.CharField(
        widget=forms.Textarea(
            attrs={
                "rows": 4,
                "placeholder": "For example: nervous with strangers, use a muzzle.",
                # Helper line on the detail page's notes card
                "aria-describedby": "notes-help",
            }
        ),
        required=False,
        help_text="Internal notes about this appointment.",
    )


class AdminAppointmentCancelForm(forms.Form):
    """
    Admin form — cancel an appointment on behalf of the pet owner.
    """

    cancellation_reason = forms.ChoiceField(
        label="Reason",
        choices=Appointment.CANCEL_CHOICES,
    )
    cancellation_detail = forms.CharField(
        label="Details",
        widget=forms.Textarea(attrs={"rows": 3}),
        required=False,
        help_text="Required when the reason is Other. The owner can see this.",
    )

    def clean(self):
        cleaned_data = super().clean()
        reason = cleaned_data.get("cancellation_reason")
        detail = cleaned_data.get("cancellation_detail")
        if reason == Appointment.OTHER and not detail:
            raise forms.ValidationError("Please provide details when selecting Other.")
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
        label="Reason for the change",
        widget=forms.Textarea(attrs={"rows": 3}),
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
            # Move dialog rules: no same-day cutoff, only slots not started yet
            slots = get_available_slots(submitted_date, move=True)
            if slots:
                self.fields["time"].choices = [("", "Select a time")] + [
                    (s.strftime("%H:%M:%S"), s.strftime("%I:%M %p")) for s in slots
                ]
            else:
                self.fields["time"].choices = [("", "No slots available for this date")]

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
            date, time, exclude_appointment_pk=self.appointment.pk, move=True
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
        today = timezone.localdate()
        if date < today:
            raise forms.ValidationError("Cannot reschedule to a past date.")
        return date


class AdminWalkInAppointmentForm(forms.Form):
    """
    Admin form — step 2 of a walk-in (the visit), saved directly as
    CONFIRMED. The owner is chosen in step 1 and passed in; the pet must be
    one of that owner's non-archived pets.
    """

    pet = forms.ModelChoiceField(
        queryset=None,  # Set in __init__ to the owner's pets
        widget=forms.RadioSelect,
        empty_label=None,
        error_messages={"required": "Pick the pet."},
    )
    service = forms.ModelChoiceField(
        queryset=None,  # Set in __init__
        empty_label="No service",
        required=False,
    )
    date = forms.DateField(
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text="Today is filled in. Pick another day for a phone booking.",
    )
    time = forms.ChoiceField(
        choices=[("", "Select a date first")],
        error_messages={"required": "Pick a time."},
    )
    reason = forms.CharField(
        label="Reason for the visit",
        widget=forms.Textarea(attrs={"rows": 3}),
        required=False,
    )

    def __init__(self, owner, *args, **kwargs):
        super().__init__(*args, **kwargs)

        from pets.models import Pet
        from billing.models import Service

        self.fields["pet"].queryset = Pet.objects.filter(
            owner=owner, is_archived=False
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
            # Walk-in rules: no same-day cutoff, the slot in progress is kept
            slots = get_available_slots(submitted_date, walk_in=True)
            if slots:
                self.fields["time"].choices = [("", "Select a time")] + [
                    (s.strftime("%H:%M:%S"), s.strftime("%I:%M %p")) for s in slots
                ]
            else:
                self.fields["time"].choices = [("", "No slots available for this date")]

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

        # The time choices already leave out taken slots; check again in
        # case the slot was booked after the choices were built.
        if self._slot_is_booked(date, time):
            raise forms.ValidationError(
                "This slot is already taken. Please select another time."
            )

        cleaned_data["time"] = time
        return cleaned_data

    def _slot_is_booked(self, date, time):
        return (
            Appointment.objects.filter(date=date, time=time)
            .exclude(status=Appointment.CANCELLED)
            .exists()
        )

    def slot_was_taken(self):
        """
        True when the form failed because someone booked the chosen time
        after the page was loaded. A taken time is missing from the time
        choices, so it fails as an invalid choice, not in clean(); this
        tells it apart from a time that has simply ended.
        """
        if self.is_valid():
            return False
        date = self.cleaned_data.get("date")
        try:
            time = datetime.time.fromisoformat(self.data.get("time", ""))
        except ValueError:
            return False
        return bool(date) and self._slot_is_booked(date, time)

    def clean_date(self):
        date = self.cleaned_data.get("date")
        if not date:
            return date
        clinic = get_clinic_settings()
        today = timezone.localdate()
        if date < today:
            raise forms.ValidationError("Cannot create an appointment in the past.")
        if clinic and date > today + datetime.timedelta(days=clinic.booking_limit_days):
            raise forms.ValidationError(
                f"Bookings are only allowed up to "
                f"{clinic.booking_limit_days} days in advance."
            )
        return date
