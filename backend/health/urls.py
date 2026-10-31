from django.urls import path

from . import views

urlpatterns = [
    # ── Admin — Health Library ──────────────────────────────────────────────
    path(
        "admin/health/",
        views.admin_health_article_list,
        name="admin_health_article_list",
    ),
    path(
        "admin/health/create/",
        views.admin_health_article_create,
        name="admin_health_article_create",
    ),
    path(
        "admin/health/<int:pk>/edit/",
        views.admin_health_article_edit,
        name="admin_health_article_edit",
    ),
    path(
        "admin/health/<int:pk>/toggle-publish/",
        views.admin_health_article_toggle_publish,
        name="admin_health_article_toggle_publish",
    ),
    path(
        "admin/health/<int:pk>/delete/",
        views.admin_health_article_delete,
        name="admin_health_article_delete",
    ),
    # ── Pet Owner — Health Library ───────────────────────────────────────────
    path(
        "owner/health/",
        views.owner_health_article_list,
        name="owner_health_article_list",
    ),
    path(
        "owner/health/<int:pk>/",
        views.owner_health_article_detail,
        name="owner_health_article_detail",
    ),
]
