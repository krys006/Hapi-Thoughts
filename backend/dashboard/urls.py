from django.urls import path
from . import views

urlpatterns = [
    path("owner/dashboard/", views.owner_dashboard, name="owner_dashboard"),
    path("admin/dashboard/", views.admin_dashboard, name="admin_dashboard"),
    path(
        "admin/dashboard/refresh/",
        views.admin_dashboard_partial,
        name="admin_dashboard_refresh",
    ),
    path("admin/search/", views.admin_global_search, name="admin_global_search"),
    path(
        "owner/dashboard/refresh/",
        views.owner_dashboard_partial,
        name="owner_dashboard_refresh",
    ),
    path("owner/search/", views.owner_global_search, name="owner_global_search"),
]
