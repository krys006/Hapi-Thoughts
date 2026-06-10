# notifications/views.py

from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, render
from django.http import HttpResponse
from django.utils import timezone

from .models import Notification


@login_required
def notification_panel(request):
    """
    Returns the notification panel partial.
    Loaded via HTMX when the bell icon is clicked.
    """
    notifications = Notification.objects.filter(
        recipient=request.user,
    ).order_by("-created_at")[:30]

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
    ).order_by("-created_at")[:30]

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
    ).order_by("-created_at")[:30]

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