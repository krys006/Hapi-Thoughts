# notifications/models.py

from django.db import models
from django.utils import timezone


class Notification(models.Model):

    # --- Notification type choices ---
    APPOINTMENT_REQUESTED = "appointment_requested"
    APPOINTMENT_APPROVED = "appointment_approved"
    APPOINTMENT_REJECTED = "appointment_rejected"
    APPOINTMENT_REMINDER = "appointment_reminder"
    APPOINTMENT_CANCELLED = "appointment_cancelled"
    BILLING_GENERATED = "billing_generated"
    VACCINATION_REMINDER = "vaccination_reminder"
    FOLLOWUP_REMINDER = "followup_reminder"
    EMAIL_FAILED = "email_failed"

    TYPE_CHOICES = [
        (APPOINTMENT_REQUESTED, "Appointment Requested"),
        (APPOINTMENT_APPROVED, "Appointment Approved"),
        (APPOINTMENT_REJECTED, "Appointment Rejected"),
        (APPOINTMENT_REMINDER, "Appointment Reminder"),
        (APPOINTMENT_CANCELLED, "Appointment Cancelled"),
        (BILLING_GENERATED, "Billing Generated"),
        (VACCINATION_REMINDER, "Vaccination Reminder"),
        (FOLLOWUP_REMINDER, "Follow-up Reminder"),
        (EMAIL_FAILED, "Email Failed"),
    ]

    # --- Relationships ---
    recipient = models.ForeignKey(
        "accounts.User",
        on_delete=models.CASCADE,
        related_name="notifications",
    )
    related_appointment = models.ForeignKey(
        "appointments.Appointment",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    related_pet = models.ForeignKey(
        "pets.Pet",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    related_billing = models.ForeignKey(
        "billing.BillingReceipt",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )

    # --- Core fields ---
    notification_type = models.CharField(
        max_length=50,
        choices=TYPE_CHOICES,
    )
    title = models.CharField(max_length=100)
    message = models.TextField()

    # --- Status flags ---
    is_read = models.BooleanField(default=False)
    read_at = models.DateTimeField(null=True, blank=True)
    email_sent = models.BooleanField(default=False)
    email_failed = models.BooleanField(default=False)
    email_error = models.TextField(blank=True)

    # --- Timestamps ---
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.notification_type} → {self.recipient.email}"

    def mark_as_read(self):
        if not self.is_read:
            self.is_read = True
            self.read_at = timezone.now()
            self.save(update_fields=["is_read", "read_at"])


class NotificationPreference(models.Model):
    # --- Relationship ---
    user = models.OneToOneField(
        "accounts.User",
        on_delete=models.CASCADE,
        related_name="notification_preference",
    )

    # --- Email toggles ---
    email_appointment_reminders = models.BooleanField(default=True)
    email_appointment_status = models.BooleanField(default=True)
    email_billing = models.BooleanField(default=True)
    email_vaccination = models.BooleanField(default=True)
    email_followup = models.BooleanField(default=True)

    # --- Reminder timing ---
    appointment_reminder_days = models.IntegerField(default=1)
    vaccination_reminder_days = models.IntegerField(default=7)

    # --- Timestamps ---
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Notification preferences — {self.user.email}"
