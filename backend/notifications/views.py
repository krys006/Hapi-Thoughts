# notifications/views.py

from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.http import HttpResponse
from django.utils import timezone
from django.core.mail import send_mail
from django.conf import settings

from .models import Notification
from .forms import NotificationPreferenceForm
from django.contrib import messages


@login_required
def notification_panel(request):
    """
    Returns the notification panel partial.
    Loaded via HTMX when the bell icon is clicked.
    """
    notifications = Notification.objects.filter(
        recipient=request.user,
    ).order_by(
        "-created_at"
    )[:30]

    return render(
        request,
        "shared/notifications/_panel.html",
        {"notifications": notifications},
    )


@login_required
def notification_mark_read(request, pk):
    """
    Marks a single notification as read.
    Called via HTMX POST when a notification row is clicked.
    Returns the updated notification row partial.
    """
    if request.method != "POST":
        return HttpResponse(status=405)

    notification = get_object_or_404(
        Notification,
        pk=pk,
        recipient=request.user,
    )
    notification.mark_as_read()

    return render(
        request,
        "shared/notifications/_notification_row.html",
        {"notification": notification},
    )

@login_required
def notification_mark_all_read(request):
    """
    Marks all unread notifications as read.
    Called via HTMX POST from the Mark All Read button.
    Returns the full refreshed panel.
    """
    if request.method != "POST":
        return HttpResponse(status=405)

    Notification.objects.filter(
        recipient=request.user,
        is_read=False,
    ).update(is_read=True, read_at=timezone.now())

    # Reload the full panel with updated state
    notifications = Notification.objects.filter(
        recipient=request.user,
    ).order_by(
        "-created_at"
    )[:30]

    return render(
        request,
        "shared/notifications/_panel.html",
        {"notifications": notifications},
    )


@login_required
def notification_clear(request):
    """
    Clears notifications for admin.
    POST with ?mode=all clears everything.
    POST with ?mode=read clears read-only.
    Returns the full refreshed panel.
    """
    if request.method != "POST":
        return HttpResponse(status=405)

    # Only admin can clear notifications
    if not request.user.is_admin:
        return HttpResponse(status=403)

    mode = request.GET.get("mode", "read")

    if mode == "all":
        Notification.objects.filter(recipient=request.user).delete()
    else:
        Notification.objects.filter(
            recipient=request.user,
            is_read=True,
        ).delete()

    # Return empty panel after clearing
    notifications = Notification.objects.filter(
        recipient=request.user,
    ).order_by(
        "-created_at"
    )[:30]

    return render(
        request,
        "shared/notifications/_panel.html",
        {"notifications": notifications},
    )


@login_required
def notification_close(request):
    """
    Returns an empty response to clear the notification panel container.
    Called via HTMX when the close button is clicked on mobile.
    """
    return HttpResponse("")


@login_required
def notification_open(request, pk):
    """
    Marks a notification as read and redirects to the related object.
    Called when a notification row is clicked.
    """
    notification = get_object_or_404(
        Notification,
        pk=pk,
        recipient=request.user,
    )

    notification.mark_as_read()

    # Redirect to the relevant page based on notification type and related objects
    if notification.related_appointment:
        if request.user.is_admin:
            return redirect(
                "admin_appointment_detail", pk=notification.related_appointment.pk
            )
        else:
            return redirect(
                "owner_appointment_detail", pk=notification.related_appointment.pk
            )

    if notification.related_billing:
        if request.user.is_admin:
            return redirect("admin_receipt_detail", pk=notification.related_billing.pk)
        else:
            return redirect("owner_receipt_detail", pk=notification.related_billing.pk)

    if notification.related_pet:
        if request.user.is_admin:
            return redirect("admin_pet_detail", pk=notification.related_pet.pk)
        else:
            return redirect("owner_pet_detail", pk=notification.related_pet.pk)

    # Fallback — no related object, go to notifications page
    if request.user.is_admin:
        return redirect("admin_dashboard")
    return redirect("owner_dashboard")


@login_required
def owner_notification_preferences(request):
    """
    Pet Owner view — update email notification preferences.
    The NotificationPreference row always exists (created via signal on registration).
    """
    if request.user.role != "pet_owner":
        return redirect("admin_dashboard")

    preference = request.user.notification_preference

    if request.method == "POST":
        form = NotificationPreferenceForm(request.POST, instance=preference)
        if form.is_valid():
            form.save()
            messages.success(request, "Notification preferences updated.")
            return redirect("owner_profile")
        else:
            messages.error(request, "Please correct the errors below.")
    else:
        form = NotificationPreferenceForm(instance=preference)

    return render(
        request,
        "owner/profile/notification_preferences.html",
        {"form": form},
    )


@login_required
def notification_resend_email(request, pk):
    """
    Admin action — retries sending the email for a notification that
    previously failed. pk refers to the EMAIL_FAILED alert notification,
    not the original — the original is reached via related_notification.
    Returns the alert's row partial, re-rendered with updated state.
    """
    if request.method != "POST":
        return HttpResponse(status=405)

    if not request.user.is_admin:
        return HttpResponse(status=403)

    alert = get_object_or_404(
        Notification,
        pk=pk,
        recipient=request.user,
        notification_type=Notification.EMAIL_FAILED,
    )
    original = alert.related_notification

    if original and original.email_failed and original.recipient.email:
        try:
            send_mail(
                subject=original.title,
                message=original.message,
                from_email=settings.EMAIL_HOST_USER,
                recipient_list=[original.recipient.email],
                fail_silently=False,
            )
            original.email_sent = True
            original.email_failed = False
            original.email_error = ""
            original.save(
                update_fields=["email_sent", "email_failed", "email_error"]
            )
            messages.success(request, "Email resent successfully.")
        except Exception as e:
            original.email_error = str(e)
            original.save(update_fields=["email_error"])
            messages.error(request, "Resend failed again.")

    return render(
        request,
        "shared/notifications/_notification_row.html",
        {"notification": alert},
    )