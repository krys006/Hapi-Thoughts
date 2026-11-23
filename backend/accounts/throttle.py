"""Throttling for login, forgot-password and register (SEC-2).

Counters live in Django's cache (the database cache table, so every worker
shares them). Rules:

* No client IP anywhere (it can be spoofed). Counters use the email or the
  whole site only.
* A raw email never goes into a cache key: keys use sha256(email) in hex.
* Fail open: any cache error is logged (class name only) and the request
  carries on as if it were not throttled.
* Counters keep their own start time in the value, so a window never
  stretches when the counter is updated.
"""

import hashlib
import logging
import time

from django.core.cache import cache

logger = logging.getLogger(__name__)

LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_SECONDS = 900
LOGIN_LOCK_SECONDS = 900
FORGOT_PER_EMAIL = 3
FORGOT_SITE = 60
REGISTER_SITE = 30
HOUR_SECONDS = 3600

LOGIN_LOCKED_MESSAGE = "Too many tries. Please wait 15 minutes, then try again."
REGISTER_BUSY_MESSAGE = (
    "Sign-ups are very busy right now. "
    "Please try again in an hour, or call the clinic."
)


def _now():
    """The clock for every window (tests patch this)."""
    return time.time()


def _email_hash(email):
    return hashlib.sha256((email or "").strip().lower().encode("utf-8")).hexdigest()


def _key(kind, scope, email=None):
    """A readable prefix and, for an email, only its hash."""
    parts = ["throttle", kind, scope]
    if email is not None:
        parts.append(_email_hash(email))
    return ":".join(parts)


def _safe(action, default):
    """Run a cache action; on any error log the class name and return default."""
    try:
        return action()
    except Exception as error:
        logger.warning("Throttle cache error (%s)", type(error).__name__)
        return default


def _count_hit(key, window):
    """Add one hit to a counter that lasts `window` seconds from its first
    hit. Returns the new count."""
    now = _now()
    stored = cache.get(key)
    if stored is None or now - stored[1] >= window:
        count, started = 1, now
    else:
        count, started = stored[0] + 1, stored[1]
    remaining = max(1, int(window - (now - started)))
    cache.set(key, (count, started), remaining)
    return count


# ─── Login ────────────────────────────────────────────────────────────────────


def login_is_locked(scope, email):
    """True while this email is locked on this login ("owner" or "admin")."""

    def check():
        until = cache.get(_key("lock", scope, email))
        return until is not None and until > _now()

    return _safe(check, False)


def record_login_failure(scope, email):
    """Count one failed login; the 5th within the window sets the lock."""

    def record():
        count = _count_hit(_key("fail", scope, email), LOGIN_WINDOW_SECONDS)
        if count >= LOGIN_MAX_FAILURES:
            until = _now() + LOGIN_LOCK_SECONDS
            cache.set(_key("lock", scope, email), until, LOGIN_LOCK_SECONDS)

    _safe(record, None)


def clear_login_failures(scope, email):
    """A successful login clears that email's count."""
    _safe(lambda: cache.delete(_key("fail", scope, email)), None)


# ─── Forgot-password and register ─────────────────────────────────────────────


def _hit_within(key, limit, window):
    return _count_hit(key, window) <= limit


def allow_forgot(email):
    """Count this forgot-password request; False when it must not send.
    Known and unknown emails count alike, so nothing leaks."""
    over = []

    def check():
        email_key = _key("forgot", "email", email)
        if not _hit_within(email_key, FORGOT_PER_EMAIL, HOUR_SECONDS):
            over.append("email")
        elif not _hit_within(_key("forgot", "site"), FORGOT_SITE, HOUR_SECONDS):
            over.append("site")

    _safe(check, None)
    if over:
        # The scope name only, never the email
        logger.warning("Forgot-password send blocked (scope=%s)", over[0])
        return False
    return True


def allow_register():
    """Count one new registration; False when the hourly site cap is hit."""
    return _safe(
        lambda: _hit_within(_key("register", "site"), REGISTER_SITE, HOUR_SECONDS),
        True,
    )
