from pathlib import Path
import environ

# ─── Base Directory ───────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent

# ─── Environment Variables ────────────────────────────────────────────────────
env = environ.Env(
    DEBUG=(bool, False),
)
environ.Env.read_env(BASE_DIR / ".env")

# ─── Core Settings ────────────────────────────────────────────────────────────
SECRET_KEY = env("SECRET_KEY")
DEBUG = env("DEBUG")
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])

# ─── Custom User Model ────────────────────────────────────────────────────────
AUTH_USER_MODEL = "accounts.User"

# ─── Installed Apps ───────────────────────────────────────────────────────────
INSTALLED_APPS = [
    # Django built-ins (admin disabled — we use custom dashboard)
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Required by django-allauth
    "django.contrib.sites",
    # Third party
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    "allauth.socialaccount.providers.google",
    # Project apps
    "accounts",
    "pets",
    "appointments",
    "medical",
    "billing",
    "notifications",
    "health",
    "dashboard",
    # Optional utilities
    "django_extensions",
]

SITE_ID = 1

# ─── Middleware ───────────────────────────────────────────────────────────────
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",  # serves static files
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "allauth.account.middleware.AccountMiddleware",  # required by allauth
    "accounts.middleware.RoleAccessMiddleware",
]

ROOT_URLCONF = "hapivet.urls"

# ─── Templates ────────────────────────────────────────────────────────────────
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        # Centralized templates folder — not per-app
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "hapivet.wsgi.application"

# ─── Database ─────────────────────────────────────────────────────────────────
DATABASES = {
    "default": env.db("DATABASE_URL"),
}

# ─── Password Validation ──────────────────────────────────────────────────────
AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"
    },
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# ─── Authentication ───────────────────────────────────────────────────────────
AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]

# allauth configuration
ACCOUNT_LOGIN_METHODS = {"email"}  # login with email, not username
ACCOUNT_SIGNUP_FIELDS = ["email*", "password1*", "password2*"]
ACCOUNT_EMAIL_VERIFICATION = "mandatory"  # require email confirmation
ACCOUNT_UNIQUE_EMAIL = True
LOGIN_REDIRECT_URL = "/owner/dashboard/"  # we'll override this with role-based redirect
ACCOUNT_LOGOUT_REDIRECT_URL = "/login/"


# Override allauth default URLs to use our custom pages
LOGIN_URL = "/login/"
ACCOUNT_EMAIL_CONFIRMATION_ANONYMOUS_REDIRECT_URL = "/login/"
ACCOUNT_EMAIL_CONFIRMATION_AUTHENTICATED_REDIRECT_URL = "/login/"
ACCOUNT_SIGNUP_REDIRECT_URL = "/owner/onboarding/"


# ─── Google OAuth ─────────────────────────────────────────────────────────────
SOCIALACCOUNT_PROVIDERS = {
    "google": {
        "SCOPE": ["profile", "email"],
        "AUTH_PARAMS": {"access_type": "online"},
        "VERIFIED_EMAIL": True,  # tell allauth Google emails are pre-verified
        "APP": {
            "client_id": env("GOOGLE_CLIENT_ID", default=""),
            "secret": env("GOOGLE_CLIENT_SECRET", default=""),
            "key": "",
        },
    }
}

SOCIALACCOUNT_AUTO_SIGNUP = True
SOCIALACCOUNT_EMAIL_VERIFICATION = "none"  # Google already verified the email
SOCIALACCOUNT_LOGIN_ON_GET = True  # skip the allauth confirmation page
SOCIALACCOUNT_EMAIL_REQUIRED = True

SOCIALACCOUNT_ADAPTER = "accounts.adapters.CustomSocialAccountAdapter"


# ─── Email ────────────────────────────────────────────────────────────────────
if DEBUG:
    # Development — print emails to terminal instead of sending
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
    DEFAULT_FROM_EMAIL = "hapitutzvet@gmail.com"
else:
    # Production — send real emails via Gmail SMTP
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    EMAIL_HOST = "smtp.gmail.com"
    EMAIL_PORT = 587
    EMAIL_USE_TLS = True
    EMAIL_HOST_USER = env("EMAIL_HOST_USER", default="")
    EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", default="")
    DEFAULT_FROM_EMAIL = EMAIL_HOST_USER

# ─── Static & Media Files ─────────────────────────────────────────────────────
STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"  # collectstatic output for production
STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

# ─── Internationalization ─────────────────────────────────────────────────────
LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Manila"  # Philippines timezone
USE_I18N = True
USE_TZ = True

# ─── Default Primary Key ──────────────────────────────────────────────────────
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
