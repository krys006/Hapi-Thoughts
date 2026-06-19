from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.utils import timezone

from appointments.models import Appointment
from billing.models import BillingReceipt
from medical.models import MedicalRecord, Vaccination
from notifications.models import Notification
from pets.models import Pet, PetOwner


def _get_admin_dashboard_context(request):
    """
    Builds the stats + widget context for the admin dashboard.
    Shared by the full-page view and the HTMX polling partial so
    both always stay in sync with the same query logic.
    """
    today = timezone.now().date()
    week_end = today + timezone.timedelta(days=7)

    # ── Summary stats ──────────────────────────────────────────────────────
    todays_appointments = Appointment.objects.filter(
        date=today,
        status__in=[Appointment.CONFIRMED, Appointment.PENDING],
    ).count()

    pending_approvals = Appointment.objects.filter(
        status=Appointment.PENDING,
    ).count()

    total_owners = PetOwner.objects.filter(is_archived=False).count()

    total_pets = Pet.objects.filter(is_archived=False).count()

    unpaid_bills = BillingReceipt.objects.filter(
        payment_status=BillingReceipt.PENDING,
    ).count()

    vaccinations_due = Vaccination.objects.filter(
        next_due_date__range=[today, week_end],
        pet__is_archived=False,
    ).count()

    followups_due = MedicalRecord.objects.filter(
        follow_up_required=True,
        follow_up_date__range=[today, week_end],
    ).count()

    # ── Pending approvals widget ───────────────────────────────────────────
    pending_appointments = (
        Appointment.objects.filter(
            status=Appointment.PENDING,
        )
        .select_related("pet", "owner", "service")
        .order_by("date", "time")[:5]
    )

    # ── Recent notifications widget ────────────────────────────────────────
    recent_notifications = Notification.objects.filter(
        recipient=request.user,
    ).order_by("is_read", "-created_at")[:5]

    return {
        "todays_appointments": todays_appointments,
        "pending_approvals": pending_approvals,
        "total_owners": total_owners,
        "total_pets": total_pets,
        "unpaid_bills": unpaid_bills,
        "vaccinations_due": vaccinations_due,
        "followups_due": followups_due,
        "today": today,
        "pending_appointments": pending_appointments,
        "recent_notifications": recent_notifications,
    }


@login_required
def admin_dashboard(request):
    """Admin dashboard — full page, stats and widgets render server-side first."""
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    context = _get_admin_dashboard_context(request)
    return render(request, "admin/dashboard/index.html", context)


@login_required
def admin_dashboard_partial(request):
    """
    Returns the stats + widgets block only.
    Polled automatically every 30s by the block itself (see
    _dashboard_partial.html) — single-partial polling, not per-section.
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    context = _get_admin_dashboard_context(request)
    return render(request, "admin/dashboard/_dashboard_partial.html", context)


@login_required
def owner_dashboard(request):
    """Placeholder pet owner dashboard."""
    if request.user.role != "pet_owner":
        return redirect("admin_dashboard")
    if not request.user.is_onboarded:
        return redirect("owner_onboarding")
    return render(request, "owner/dashboard/index.html")
