from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render


@login_required
def admin_dashboard(request):
    """Placeholder admin dashboard."""
    if request.user.role != "admin":
        return redirect("owner_dashboard")
    return render(request, "admin/dashboard/index.html")


@login_required
def owner_dashboard(request):
    """Placeholder pet owner dashboard."""
    if request.user.role != "pet_owner":
        return redirect("admin_dashboard")
    if not request.user.is_onboarded:
        return redirect("owner_onboarding")
    return render(request, "owner/dashboard/index.html")
