from django.urls import path, include
from django.contrib.auth import views as auth_views

# imports for image
from django.conf import settings
from django.conf.urls.static import static

from django.views.generic import RedirectView

from accounts import views as account_views

urlpatterns = [
    path("", include("health.urls")),
    # notifications (bell panel, mark read, clear)
    path("", include("notifications.urls")),
    # Root URL — redirect to login
    path("", RedirectView.as_view(url="/login/"), name="home"),
    # dashboard (admin and pet owner dashboard homes)
    path("", include("dashboard.urls")),
    # accounts (login, register, verification, dashboards)
    path("", include("accounts.urls")),
    # pets (onboarding, pet management)
    path("", include("pets.urls")),
    # appointments (booking, calendar, clinic settings)
    path("", include("appointments.urls")),
    # medical records (clinical documentation, prescriptions, test results)
    path("", include("medical.urls")),
    # billing (services, receipts, payment status)
    path("", include("billing.urls")),
    # allauth's own login and signup pages go to ours (before the include)
    path(
        "accounts/login/",
        account_views.allauth_login_redirect,
        name="allauth_login_redirect",
    ),
    path(
        "accounts/signup/",
        account_views.allauth_signup_redirect,
        name="allauth_signup_redirect",
    ),
    # allauth's password reset, confirm-email, inactive and logout pages go
    # to ours too (before the include). Google login and callback stay.
    path(
        "accounts/password/reset/",
        account_views.allauth_password_redirect,
        name="allauth_password_reset_redirect",
    ),
    path(
        "accounts/password/reset/done/",
        account_views.allauth_password_redirect,
        name="allauth_password_reset_done_redirect",
    ),
    # Covers key/done/ and key/<uidb36>-<key>/
    path(
        "accounts/password/reset/key/<path:key>/",
        account_views.allauth_password_redirect,
        name="allauth_password_reset_key_redirect",
    ),
    path(
        "accounts/confirm-email/",
        account_views.allauth_confirm_email_redirect,
        name="allauth_confirm_email_redirect",
    ),
    path(
        "accounts/confirm-email/<str:key>/",
        account_views.allauth_confirm_email_redirect,
        name="allauth_confirm_email_key_redirect",
    ),
    path(
        "accounts/inactive/",
        account_views.allauth_inactive_redirect,
        name="allauth_inactive_redirect",
    ),
    # Our own logout view (not a redirect), so a POST here still logs out
    path("accounts/logout/", account_views.user_logout, name="allauth_logout"),
    # allauth (Google OAuth, email verification, etc.)
    path("accounts/", include("allauth.urls")),
    # ─── Password Reset (Django built-in views) ───────────────────────────
    path(
        "forgot-password/",
        account_views.HapiVetPasswordResetView.as_view(),
        name="forgot_password",
    ),
    path(
        "forgot-password/sent/",
        auth_views.PasswordResetDoneView.as_view(
            template_name="owner/forgot_password_sent.html",
        ),
        name="password_reset_done",
    ),
    path(
        "reset-password/<uidb64>/<token>/",
        auth_views.PasswordResetConfirmView.as_view(
            template_name="owner/reset_password.html",
            success_url="/reset-password/complete/",
        ),
        name="password_reset_confirm",
    ),
    path(
        "reset-password/complete/",
        auth_views.PasswordResetCompleteView.as_view(
            template_name="owner/reset_password_complete.html",
        ),
        name="password_reset_complete",
    ),
]


# Serve media files in development only
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
