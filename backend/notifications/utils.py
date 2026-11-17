# notifications/utils.py

from django.core.mail import send_mail
from django.conf import settings
from django.core.paginator import Paginator
from django.urls import reverse
from django.utils import timezone
from django.utils.dateformat import format as date_format
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


# ── Compact owner text (Slice 8a-2b) ─────────────────────────────────────────
# Owner notifications use a short title and a compact message
# ("Visit confirmed" / "Bantay · Mon, Nov 2 at 3:00 PM"); the email gets the
# full sentence through notify(email_body=...). Old rows keep their long text.


def short_day(day, today):
    """'Nov 9', with the year when it isn't today's year ('Nov 9, 2027')."""
    text = date_format(day, "M j")
    return text if day.year == today.year else f"{text}, {day.year}"


def when_line(date, time, today):
    """'Mon, Nov 2 at 3:00 PM'; the year only when it isn't today's year
    ('Tue, Jan 5, 2027 at 3:00 PM'); just the date when there is no time."""
    text = date_format(date, "D, M j")
    if date.year != today.year:
        text = f"{text}, {date.year}"
    return f"{text} at {date_format(time, 'g:i A')}" if time else text


def compact_message(pet_name, detail):
    """'Bantay · Mon, Nov 2 at 3:00 PM' (an empty pet name leaves the detail)."""
    return f"{pet_name} · {detail}" if pet_name else detail


# ── Where a notification leads, and its icon ─────────────────────────────────

# Icon names from components/icon.html, by notification_type. The follow-up
# icon is calendar-clock because the mockup's stethoscope isn't in the set.
NOTIFICATION_ICONS = {
    Notification.APPOINTMENT_APPROVED: "calendar-check",
    Notification.APPOINTMENT_REJECTED: "calendar-x",
    Notification.APPOINTMENT_CANCELLED: "calendar-x",
    Notification.APPOINTMENT_REMINDER: "clock",
    Notification.BILLING_GENERATED: "receipt",
    Notification.VACCINATION_REMINDER: "syringe",
    Notification.FOLLOWUP_REMINDER: "calendar-clock",
    Notification.PET_DELETION_ANSWERED: "paw-print",
}
DEFAULT_NOTIFICATION_ICON = "bell"


def notification_icon(notification):
    """The icon name for the notification's type ('bell' for any other)."""
    return NOTIFICATION_ICONS.get(
        notification.notification_type, DEFAULT_NOTIFICATION_ICON
    )


def notification_target(notification):
    """
    The URL the recipient should land on: the visit, then the bill, then the
    pet (admin or owner pages by the recipient's role). None when the
    notification points at nothing (e.g. a removed pet). Use select_related
    ("recipient") when calling this for a list.
    """
    admin = notification.recipient.is_admin
    if notification.related_appointment_id:
        name = "admin_appointment_detail" if admin else "owner_appointment_detail"
        return reverse(name, kwargs={"pk": notification.related_appointment_id})
    if notification.related_billing_id:
        name = "admin_receipt_detail" if admin else "owner_receipt_detail"
        return reverse(name, kwargs={"pk": notification.related_billing_id})
    if notification.related_pet_id:
        name = "admin_pet_detail" if admin else "owner_pet_detail"
        return reverse(name, kwargs={"pk": notification.related_pet_id})
    return None


# ── Owner notifications page ─────────────────────────────────────────────────

OWNER_NOTIFICATIONS_PAGE_SIZE = 25


def owner_notifications_context(user, params, today):
    """
    The owner notifications page: the user's notifications, newest first, 25
    a page, split into today's ("Today") and the rest ("Earlier") by Manila
    date. Each row: notification, title, message, icon, is_unread, target
    (a notification_open URL, so a click marks it read; None when
    notification_target is None) and created_at. Also unread_count (all
    pages) and the pager links. 3 queries however many rows.
    """
    from appointments.utils import paging_links  # appointments imports this module

    queryset = (
        Notification.objects.filter(recipient=user)
        .select_related("recipient")
        .order_by("-created_at", "-pk")
    )
    page_obj = Paginator(queryset, OWNER_NOTIFICATIONS_PAGE_SIZE).get_page(
        params.get("page")
    )
    today_rows, earlier_rows = [], []
    for notification in page_obj:
        opens = notification_target(notification) is not None
        row = {
            "notification": notification,
            "title": notification.title,
            "message": notification.message,
            "icon": notification_icon(notification),
            "is_unread": not notification.is_read,
            "target": (
                reverse("notification_open", kwargs={"pk": notification.pk})
                if opens
                else None
            ),
            "created_at": notification.created_at,
        }
        is_today = timezone.localtime(notification.created_at).date() == today
        (today_rows if is_today else earlier_rows).append(row)
    return {
        "today_rows": today_rows,
        "earlier_rows": earlier_rows,
        "page_obj": page_obj,
        "unread_count": Notification.objects.filter(
            recipient=user, is_read=False
        ).count(),
        **paging_links(page_obj, ""),
    }
