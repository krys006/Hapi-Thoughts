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
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=[])


# ─── Production Security Hardening ───────────────────────────────────────────
if not DEBUG:
    # Render terminates HTTPS at its proxy and forwards as HTTP internally;
    # this header tells Django the original request was actually secure.
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True


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
    "anymail",

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

    # Cloud storage for media files (supabase)
    "storages",
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
USE_BREVO_EMAIL = env.bool(
    "USE_BREVO_EMAIL",
    default=not DEBUG,
)

DEFAULT_FROM_EMAIL = env(
    "DEFAULT_FROM_EMAIL",
    default="Hapi Tutz <hapitutzvet@gmail.com>",
)

SERVER_EMAIL = DEFAULT_FROM_EMAIL

if USE_BREVO_EMAIL:
    # Development email testing and production:
    # send through Brevo's HTTPS API using Anymail.
    EMAIL_BACKEND = "anymail.backends.brevo.EmailBackend"

    ANYMAIL = {
        "BREVO_API_KEY": env("BREVO_API_KEY"),
    }
else:
    # Normal local development:
    # print emails to the terminal without consuming Brevo quota.
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# ─── Static & Media Files ─────────────────────────────────────────────────────
STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"  # collectstatic output for production
#STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"


# ─── Static & Media Files (Cloud Storage) ────────────────────────────────────
if env.bool("USE_S3", default=False):
    AWS_ACCESS_KEY_ID = env("AWS_ACCESS_KEY_ID")
    AWS_SECRET_ACCESS_KEY = env("AWS_SECRET_ACCESS_KEY")
    AWS_STORAGE_BUCKET_NAME = env("AWS_STORAGE_BUCKET_NAME")
    AWS_S3_ENDPOINT_URL = env("AWS_S3_ENDPOINT_URL")
    AWS_S3_REGION_NAME = env("AWS_S3_REGION_NAME")
    AWS_S3_FILE_OVERWRITE = False
    AWS_DEFAULT_ACL = "public-read"
    AWS_QUERYSTRING_AUTH = False
    AWS_S3_CUSTOM_DOMAIN = env("AWS_S3_CUSTOM_DOMAIN", default="")
    AWS_LOCATION = ""
    
    MEDIA_URL = env("MEDIA_URL")

    STORAGES = {
        "default": {
            "BACKEND": "storages.backends.s3boto3.S3Boto3Storage",
        },
        "staticfiles": {
            "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
        },
    }
else:
    MEDIA_URL = "/media/"
    MEDIA_ROOT = BASE_DIR / "media"

    STORAGES = {
        "default": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
        },
        "staticfiles": {
            "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
        },
    }


# ─── Internationalization ─────────────────────────────────────────────────────
LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Manila"  # Philippines timezone
USE_I18N = True
USE_TZ = True

# ─── Default Primary Key ──────────────────────────────────────────────────────
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
