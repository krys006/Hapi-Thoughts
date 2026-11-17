# notifications/views.py

from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.http import HttpResponse
from django.utils import timezone

from accounts.utils import owner_profile_required

from .models import Notification
from .utils import notification_target, owner_notifications_context

from .forms import NotificationPreferenceForm

from django.contrib import messages


def _panel_template(request):
    """New-layout admin pages get the new panel; everyone else keeps the shared one.

    Admin pages still on base_dashboard.html don't send X-Admin-Layout, so
    they keep the old panel until their slice is migrated.
    """
    if request.user.is_admin and request.headers.get("X-Admin-Layout") == "1":
        return "admin/notifications/_panel.html"
    return "shared/notifications/_panel.html"


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
        _panel_template(request),
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
    Returns the full refreshed panel. POST return_to=page (a fixed choice,
    not a URL) is the owner notifications page's button: it goes back to
    that page instead.
    """
    if request.method != "POST":
        return HttpResponse(status=405)

    Notification.objects.filter(
        recipient=request.user,
        is_read=False,
    ).update(is_read=True, read_at=timezone.now())

    if request.POST.get("return_to") == "page" and not request.user.is_admin:
        return redirect("owner_notifications")

    # Reload the full panel with updated state
    notifications = Notification.objects.filter(
        recipient=request.user,
    ).order_by(
        "-created_at"
    )[:30]

    return render(
        request,
        _panel_template(request),
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
        _panel_template(request),
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

    # The visit, bill or pet it is about (notification_target)
    target = notification_target(notification)
    if target:
        return redirect(target)

    # Fallback — no related object, go to the dashboard
    if request.user.is_admin:
        return redirect("admin_dashboard")
    return redirect("owner_dashboard")


@login_required
@owner_profile_required
def owner_notifications(request):
    """Pet Owner view — the notifications page opened from the bell."""
    if request.user.role != "pet_owner":
        return redirect("admin_dashboard")

    today = timezone.localdate()  # one read of the clock (Manila)
    context = owner_notifications_context(request.user, request.GET, today)
    return render(request, "owner/notifications/list.html", context)


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
