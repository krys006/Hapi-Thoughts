GOOGLE SIGN-IN IMPLEMENTATION SUMMARY
Feature: Google OAuth Sign-In for Pet Owners
Date implemented: June 9, 2026
Branch: backend-dev
Hardened: October 31, 2026 (branch fix/google-signin-hardening)

---

CURRENT BEHAVIOUR (as of 2026-10-31)

All rules live in accounts/adapters.py, CustomSocialAccountAdapter.pre_social_login.
Every refusal shows a message on the owner login page (messages.error) and
stops allauth by raising ImmediateHttpResponse(redirect("owner_login")).
Nothing is linked (connect()) until every check has passed.

1. Google account already linked to a user
   - Admin -> refused: "Admin accounts sign in with email and password."
     (admins never use Google sign-in until FEATURES 1.3 is built)
   - Inactive (archived, or registered but not yet verified) -> refused:
     "This account can't sign in right now. If you just registered, check
     your email for the verification link; otherwise contact the clinic."
   - Otherwise -> normal login.

2. Google account not linked yet
   a. Google's email not verified -> refused, before any user lookup:
      "Your Google email address isn't verified. Please sign in or register
      with your email address instead."
      One message for every case, so an unverified Google account learns
      nothing about which local accounts exist (none, several, admin,
      inactive).
   b. Verified: look for a user whose email matches Google's email,
      ignoring case (email__iexact)
      - More than one match -> refused: "We couldn't link your Google
        account. Please contact the clinic." (User.email is only unique
        case-sensitively)
      - One match -> refused if admin or inactive (messages above).
        Otherwise the Google account is linked to that user and they are
        logged in.
      - No match -> allauth auto-signs up a new user; role comes from the
        model default (pet_owner) and allauth redirects to
        ACCOUNT_SIGNUP_REDIRECT_URL (/owner/onboarding/).

"Verified" means Google's own extra_data["email_verified"] (or the older
"verified_email" key) is True or the string "true". Missing or anything else
means not verified. We do not use sociallogin.email_addresses[].verified,
because allauth can mark those verified on its own via the provider
VERIFIED_EMAIL setting (now removed).

Settings (hapivet/settings.py):
- SOCIALACCOUNT_LOGIN_ON_GET removed (default False): a plain GET to the
  Google login URL shows allauth's confirmation page instead of starting
  the login. This prevents login CSRF.
- SOCIALACCOUNT_PROVIDERS["google"]["VERIFIED_EMAIL"] removed.
- SOCIALACCOUNT_EMAIL_AUTHENTICATION left unset (False), so allauth never
  links accounts by email on its own. Only the adapter does.

Template (templates/owner/login.html): the Google button is a POST form to
{% provider_login_url 'google' %} with {% csrf_token %}, not a link.

Removed: accounts/signals.py (dead role receiver), ready() in
accounts/apps.py, and the adapter's save_user and get_connect_redirect_url
overrides.

Sections below are the original June 2026 implementation log. Items marked
[superseded] no longer match the code.

---

WHAT WAS ADDED

1. Google OAuth credentials obtained from Google Cloud Console
   Project: hapi-thoughts-498811
   Redirect URI configured: http://127.0.0.1:8000/accounts/google/login/callback/
   JavaScript origin configured: http://127.0.0.1:8000
   Credentials stored in backend/.env:
   - GOOGLE_CLIENT_ID
   - GOOGLE_CLIENT_SECRET

2. hapivet/settings.py — additions
   Added SOCIALACCOUNT_PROVIDERS block with Google configuration:
   - SCOPE: profile and email
   - VERIFIED_EMAIL: True (skips email verification for Google users) [superseded: removed]
   - APP: reads client_id and secret from .env
   Added SOCIALACCOUNT_AUTO_SIGNUP = True
   Added SOCIALACCOUNT_EMAIL_VERIFICATION = "none"
   Added SOCIALACCOUNT_LOGIN_ON_GET = True [superseded: removed]
   Added SOCIALACCOUNT_EMAIL_REQUIRED = True
   Added SOCIALACCOUNT_ADAPTER pointing to accounts.adapters.CustomSocialAccountAdapter
   Changed LOGIN_REDIRECT_URL from "/" to "/owner/dashboard/"
   Added LOGIN_URL = "/login/"
   Added ACCOUNT_EMAIL_CONFIRMATION_ANONYMOUS_REDIRECT_URL = "/login/"
   Added ACCOUNT_EMAIL_CONFIRMATION_AUTHENTICATED_REDIRECT_URL = "/login/"
   Added ACCOUNT_SIGNUP_REDIRECT_URL = "/owner/onboarding/"

3. accounts/adapters.py — new file created
   CustomSocialAccountAdapter extends DefaultSocialAccountAdapter
   - pre_social_login: connects Google account to existing user if email matches
     [superseded: see CURRENT BEHAVIOUR]
   - save_user: assigns PET_OWNER role automatically on first Google signup
     [superseded: removed; the model default sets the role]
   - get_connect_redirect_url: redirects to /owner/dashboard/ after connect
     [superseded: removed]

4. accounts/signals.py — new file created [superseded: deleted 2026-10-31]
   Role assignment moved to adapters.py
   signals.py exists but role logic is handled by adapter instead

5. accounts/apps.py — updated [superseded: ready() removed 2026-10-31]
   Added ready() method to load accounts.signals on startup

6. hapivet/urls.py — updated
   Added root URL redirect: path("", RedirectView.as_view(url="/login/"))
   Added import: from django.views.generic import RedirectView

7. templates/owner/login.html — updated
   Added {% load socialaccount %} tag
   Added divider between form and Google button
   Added Sign in with Google button using official Google material button HTML
   Button links to {% provider_login_url 'google' %}
   [superseded: now a POST form with {% csrf_token %}]

8. static/css/owner/google-button.css — new file created
   Official Google material button CSS styles

---

DEVIATIONS FROM MD FILES

1. DATA_MODELS.md does not document the adapter pattern —
   role assignment happens in accounts/adapters.py save_user method,
   not via a Django signal as originally discussed.
   [superseded: role now comes from the User.role model default]

2. FEATURES.md Phase 1 item "Sign in with Google" was implemented
   late (after Phase 5) rather than in Phase 1 as planned.
   No impact on other phases.

3. LOGIN_REDIRECT_URL changed from "/" to "/owner/dashboard/" —
   middleware handles onboarding redirect from there.

---

PRODUCTION NOTES (when deploying to Render)

1. Add production redirect URI in Google Cloud Console:
   https://your-render-domain.onrender.com/accounts/google/login/callback/

2. Add production JavaScript origin:
   https://your-render-domain.onrender.com

3. Add GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET to Render
   environment variables dashboard

4. Update SOCIALACCOUNT_PROVIDERS in settings.py if domain changes

---

FILES MODIFIED
- hapivet/settings.py
- hapivet/urls.py
- accounts/apps.py
- templates/owner/login.html

FILES CREATED
- accounts/adapters.py
- accounts/signals.py (deleted 2026-10-31)
- static/css/owner/google-button.css