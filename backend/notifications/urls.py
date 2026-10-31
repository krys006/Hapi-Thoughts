# notifications/urls.py

from django.urls import path
from . import views

urlpatterns = [
    path(
        "notifications/panel/",
        views.notification_panel,
        name="notifications_panel",
    ),
    path(
        "notifications/<int:pk>/read/",
        views.notification_mark_read,
        name="notification_mark_read",
    ),
    path(
        "notifications/mark-all-read/",
        views.notification_mark_all_read,
        name="notification_mark_all_read",
    ),
    path(
        "notifications/clear/",
        views.notification_clear,
        name="notification_clear",
    ),
    path(
        "notifications/close/",
        views.notification_close,
        name="notification_close",
    ),
    path(
        "notifications/<int:pk>/open/",
        views.notification_open,
        name="notification_open",
    ),
    path(
        "owner/profile/notifications/",
        views.owner_notification_preferences,
        name="owner_notification_preferences",
    ),
]
