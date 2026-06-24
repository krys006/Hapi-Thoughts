# notifications/utils.py

from django.core.mail import send_mail
from django.conf import settings
from notifications.models import Notification, NotificationPreference

# Maps notification_type to the NotificationPreference field that controls it.
# Types not listed here always send email (e.g. admin-facing notifications).
EMAIL_PREFERENCE_MAP = {
    "appointment_approved": "email_appointment_status",
    "appointment_rejected": "email_appointment_status",
    "appointment_reminder": "email_appointment_reminders",
    "billing_generated": "email_billing",
    "vaccination_reminder": "email_vaccination",
    "followup_reminder": "email_followup",
}


def notify(
    recipient,
    notification_type,
    title,
    message,
    related_appointment=None,
    related_pet=None,
    related_billing=None,
    email_subject=None,
    email_body=None,
):
    """
    Central notification function for Hapi Vet.

    Always creates an in-app Notification record.
    Sends email based on recipient's NotificationPreference.

    Args:
        recipient:            User instance to notify
        notification_type:    String matching Notification.TYPE_CHOICES
        title:                Short notification title (in-app)
        message:              Full notification message (in-app)
        related_appointment:  Optional Appointment instance
        related_pet:          Optional Pet instance
        related_billing:      Optional BillingReceipt instance
        email_subject:        Subject line for email (defaults to title)
        email_body:           Email body text (defaults to message)
    """

    # --- 1. Always create the in-app notification ---
    notification = Notification.objects.create(
        recipient=recipient,
        notification_type=notification_type,
        title=title,
        message=message,
        related_appointment=related_appointment,
        related_pet=related_pet,
        related_billing=related_billing,
    )

    # --- 2. Check email preference for this notification type ---
    should_send_email = _should_send_email(recipient, notification_type)

    if not should_send_email:
        return notification

    # --- 3. Determine email content ---
    subject = email_subject or title
    body = email_body or message
    recipient_email = recipient.email

    if not recipient_email:
        return notification

    # --- 4. Attempt to send email ---
    try:
        send_mail(
            subject=subject,
            message=body,
            from_email=settings.EMAIL_HOST_USER,
            recipient_list=[recipient_email],
            fail_silently=False,
        )
        notification.email_sent = True
        notification.save(update_fields=["email_sent"])

    except Exception as e:
        notification.email_failed = True
        notification.email_error = str(e)
        notification.save(update_fields=["email_failed", "email_error"])

    return notification


def _should_send_email(recipient, notification_type):
    """
    Returns True if an email should be sent for this notification type.
    Checks recipient's NotificationPreference if applicable.
    Admin-facing notifications (appointment_requested) always send.
    """
    preference_field = EMAIL_PREFERENCE_MAP.get(notification_type)

    # No preference field = always send (e.g. appointment_requested to admin)
    if not preference_field:
        return True

    try:
        preference = recipient.notification_preference
        return getattr(preference, preference_field, True)
    except NotificationPreference.DoesNotExist:
        # Preference row missing — fail open (send the email)
        return True
