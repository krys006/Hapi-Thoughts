from django.urls import path, include
from django.contrib.auth import views as auth_views

# imports for image
from django.conf import settings
from django.conf.urls.static import static

from django.views.generic import RedirectView

urlpatterns = [

    # notifications (bell panel, mark read, clear)
    path("", include("notifications.urls")),
    
    # Root URL — redirect to login
    path("", RedirectView.as_view(url="/login/"), name="home"),

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

    # allauth (Google OAuth, email verification, etc.)
    path("accounts/", include("allauth.urls")),

    # ─── Password Reset (Django built-in views) ───────────────────────────
    path(
        "forgot-password/",
        auth_views.PasswordResetView.as_view(
            template_name="owner/forgot_password.html",
            email_template_name="owner/emails/password_reset_email.txt",
            subject_template_name="owner/emails/password_reset_subject.txt",
            success_url="/forgot-password/sent/",
        ),
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
