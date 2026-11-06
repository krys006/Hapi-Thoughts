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

# What notify() did about the email, set on the returned Notification as
# email_outcome (not saved), so a caller can say how the owner was reached.
EMAIL_SENT = "sent"
EMAIL_FAILED = "failed"
EMAIL_NO_ADDRESS = "no_address"
EMAIL_OFF = "off"


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
    recipient_email=None,
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

    Returns the Notification, with email_outcome (not saved) set to
    EMAIL_NO_ADDRESS (no email on file, checked first), EMAIL_OFF (the
    recipient's preference for this type is off), EMAIL_SENT or
    EMAIL_FAILED (email_error has the reason).
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

    # --- 2. No address, then the email preference for this type ---
    recipient_email = recipient_email or recipient.email
    if not recipient_email:
        notification.email_outcome = EMAIL_NO_ADDRESS
        return notification

    if not _should_send_email(recipient, notification_type):
        notification.email_outcome = EMAIL_OFF
        return notification

    # --- 3. Determine email content ---
    subject = email_subject or title
    body = email_body or message

    # --- 4. Attempt to send email ---
    try:
        sent_count = send_mail(
            subject=subject,
            message=body,
            from_email=None,
            recipient_list=[recipient_email],
            fail_silently=False,
        )

        if sent_count == 1:
            notification.email_sent = True
            notification.email_failed = False
            notification.email_error = ""
            notification.save(
                update_fields=[
                    "email_sent",
                    "email_failed",
                    "email_error",
                ]
            )
        else:
            notification.email_sent = False
            notification.email_failed = True
            notification.email_error = "Email provider returned no successful sends."
            notification.save(
                update_fields=[
                    "email_sent",
                    "email_failed",
                    "email_error",
                ]
            )

    except Exception as exc:
        notification.email_sent = False
        notification.email_failed = True
        notification.email_error = str(exc)
        notification.save(
            update_fields=[
                "email_sent",
                "email_failed",
                "email_error",
            ]
        )

    notification.email_outcome = EMAIL_SENT if notification.email_sent else EMAIL_FAILED
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
