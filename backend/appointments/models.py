from django.db import models


class ClinicSettings(models.Model):
    """
    Single-row table storing all clinic configuration.
    Always retrieved via ClinicSettings.objects.first().
    Never create more than one instance.
    """

    # ── Clinic Identity ──────────────────────────────────────────────────────
    clinic_name = models.CharField(max_length=100)
    address = models.TextField()
    contact_number = models.CharField(max_length=20, blank=True)
    email = models.EmailField(blank=True)
    logo = models.ImageField(
        upload_to="clinic/",
        blank=True,
        null=True,
    )

    # ── Schedule Configuration ───────────────────────────────────────────────
    opening_time = models.TimeField()
    closing_time = models.TimeField()
    slot_duration_minutes = models.IntegerField(default=60)
    working_days = models.JSONField(default=list)
    same_day_cutoff_time = models.TimeField()
    booking_limit_days = models.IntegerField(default=30)

    # ── Notification ─────────────────────────────────────────────────────────
    notification_email = models.EmailField(blank=True)

    # ── Timestamp ────────────────────────────────────────────────────────────
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Clinic Settings"
        verbose_name_plural = "Clinic Settings"

    def __str__(self):
        return self.clinic_name

    def get_working_day_names(self):
        """Returns human-readable working day names for display."""
        day_names = [
            "Monday",
            "Tuesday",
            "Wednesday",
            "Thursday",
            "Friday",
            "Saturday",
            "Sunday",
        ]
        return [day_names[d] for d in self.working_days if 0 <= d <= 6]


class BlockedDate(models.Model):
    """
    Dates blocked by Admin — holidays, rest days, etc.
    No appointments can be booked on blocked dates.
    """

    date = models.DateField(unique=True)
    reason = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["date"]

    def __str__(self):
        return f"{self.date} — {self.reason or 'Blocked'}"


class Appointment(models.Model):
    """
    Records all appointment bookings.
    Status flow: PENDING → CONFIRMED → COMPLETED or NO_SHOW
                 PENDING → CANCELLED
    """

    # ── Status choices ───────────────────────────────────────────────────────
    PENDING = "pending"
    CONFIRMED = "confirmed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    NO_SHOW = "no_show"
    STATUS_CHOICES = [
        (PENDING, "Pending"),
        (CONFIRMED, "Confirmed"),
        (COMPLETED, "Completed"),
        (CANCELLED, "Cancelled"),
        (NO_SHOW, "No Show"),
    ]

    # ── Cancellation reason choices ──────────────────────────────────────────
    CHANGE_OF_PLANS = "change_of_plans"
    PET_UNWELL = "pet_unwell"
    CANNOT_MAKE_TIME = "cannot_make_time"
    OTHER = "other"
    CANCEL_CHOICES = [
        (CHANGE_OF_PLANS, "Change of Plans"),
        (PET_UNWELL, "Pet is Unwell"),
        (CANNOT_MAKE_TIME, "Cannot Make the Time"),
        (OTHER, "Other"),
    ]

    # ── Relationships ────────────────────────────────────────────────────────
    owner = models.ForeignKey(
        "pets.PetOwner",
        on_delete=models.CASCADE,
        related_name="appointments",
    )
    pet = models.ForeignKey(
        "pets.Pet",
        on_delete=models.CASCADE,
        related_name="appointments",
    )
    # Service can be null — walk-ins may not have a service selected upfront
    service = models.ForeignKey(
        "billing.Service",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="appointments",
    )

    # ── Core fields ──────────────────────────────────────────────────────────
    date = models.DateField()
    time = models.TimeField()
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=PENDING,
    )
    reason = models.TextField(blank=True)
    notes = models.TextField(blank=True)

    # ── Flags ────────────────────────────────────────────────────────────────
    is_walk_in = models.BooleanField(default=False)

    # ── Cancellation fields ──────────────────────────────────────────────────
    cancellation_reason = models.CharField(
        max_length=50,
        blank=True,
        choices=CANCEL_CHOICES,
    )
    cancellation_detail = models.TextField(blank=True)
    cancelled_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="cancelled_appointments",
    )

    # ── Reschedule fields ────────────────────────────────────────────────────
    reschedule_reason = models.TextField(blank=True)
    rescheduled_from = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="rescheduled_to",
    )

    # ── Timestamps ───────────────────────────────────────────────────────────
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-date", "-time"]

    def __str__(self):
        return f"{self.pet.name} — {self.date} {self.time}"

    @property
    def is_upcoming(self):
        from django.utils import timezone
        return self.date >= timezone.now().date()

    @property
    def is_cancellable_freely(self):
        """
        Pet owner can cancel freely if more than 24 hours before appointment.
        Within 24 hours requires Admin approval (handled at view level).
        """
        from django.utils import timezone
        import datetime

        appointment_datetime = timezone.make_aware(
            datetime.datetime.combine(self.date, self.time)
        )
        hours_until = (
            appointment_datetime - timezone.now()
        ).total_seconds() / 3600
        return hours_until > 24