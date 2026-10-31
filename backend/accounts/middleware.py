from django.http import HttpResponseForbidden
from django.shortcuts import redirect
from django.urls import reverse

# URLs that don't require login
PUBLIC_URLS = [
    "/login/",
    "/register/",
    "/register/pending/",
    "/admin-login/",
    "/forgot-password/",
    "/forgot-password/sent/",
    "/reset-password/complete/",
    "/accounts/",  # allauth URLs (Google OAuth callbacks)
]

# URLs that start with these prefixes are also public
PUBLIC_PREFIXES = [
    "/verify-email/",
    "/reset-password/",
    "/accounts/",
]


class RoleAccessMiddleware:
    """
    Enforces role-based access control across all views.

    Rules:
    - Unauthenticated users can only access PUBLIC_URLS
    - Pet owners cannot access /admin/* URLs
    - Admins cannot access /owner/* URLs
    - Wrong-role HTMX requests get 403 instead of a redirect
    - Unauthenticated users hitting protected URLs are redirected to login
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path

        # Allow public URLs through without any checks
        if self._is_public(path):
            return self.get_response(request)

        # Unauthenticated user trying to access a protected URL
        if not request.user.is_authenticated:
            login_url = reverse("owner_login")
            return redirect(f"{login_url}?next={path}")

        # Pet owner trying to access admin URLs. HTMX gets a 403: the browser
        # would follow a redirect and swap a whole page into the target.
        if request.user.role == "pet_owner" and path.startswith("/admin/"):
            if request.headers.get("HX-Request"):
                return HttpResponseForbidden()
            return redirect("/owner/dashboard/")

        # Admin trying to access pet owner URLs. HTMX gets a 403: the browser
        # would follow a redirect and swap a whole page into the target.
        if request.user.role == "admin" and path.startswith("/owner/"):
            if request.headers.get("HX-Request"):
                return HttpResponseForbidden()
            return redirect("/admin/dashboard/")

        return self.get_response(request)

    def _is_public(self, path):
        """Returns True if the path is publicly accessible without login."""
        if path in PUBLIC_URLS:
            return True
        for prefix in PUBLIC_PREFIXES:
            if path.startswith(prefix):
                return True
        return False
