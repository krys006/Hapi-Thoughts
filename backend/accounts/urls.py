from django.urls import path
from . import views

urlpatterns = [
    # Pet Owner auth
    path("login/", views.owner_login, name="owner_login"),
    path("register/", views.owner_register, name="owner_register"),
    path(
        "register/pending/", views.owner_register_pending, name="owner_register_pending"
    ),
    path(
        "verify-email/<uidb64>/<token>/",
        views.owner_verify_email,
        name="owner_verify_email",
    ),
    path("logout/", views.user_logout, name="logout"),
    # Admin auth
    path("admin-login/", views.admin_login, name="admin_login"),
    # Dashboards (placeholders)
    path("owner/dashboard/", views.owner_dashboard, name="owner_dashboard"),
    path("owner/onboarding/", views.owner_onboarding, name="owner_onboarding"),
    path("admin/dashboard/", views.admin_dashboard, name="admin_dashboard"),
    # Walk-in client registration (Admin only)
    path("admin/walkin/create/", views.admin_walkin_create, name="admin_walkin_create"),
]
