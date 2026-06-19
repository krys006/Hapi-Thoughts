from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.utils import timezone

from appointments.utils import get_admin_calendar_context
from appointments.models import Appointment
from billing.models import BillingReceipt
from medical.models import MedicalRecord, Vaccination
from notifications.models import Notification
from pets.models import Pet, PetOwner

from django.db.models import Q


def _run_global_search(query):
    """
    Searches pet owners, pets, appointments, and medical records for the
    given query string. Each group capped at 5 results — this is a quick
    lookup tool for the header, not a full search results page.
    Archived owners/pets excluded, consistent with dashboard stats.
    """
    owners = PetOwner.objects.filter(
        Q(first_name__icontains=query)
        | Q(last_name__icontains=query)
        | Q(contact_number__icontains=query),
        is_archived=False,
    )[:5]

    pets = Pet.objects.select_related("owner").filter(
        Q(name__icontains=query)
        | Q(species__icontains=query)
        | Q(breed__icontains=query),
        is_archived=False,
    )[:5]

    appointments = (
        Appointment.objects.select_related("pet", "owner")
        .filter(
            Q(pet__name__icontains=query)
            | Q(owner__first_name__icontains=query)
            | Q(owner__last_name__icontains=query)
            | Q(reason__icontains=query)
        )
        .order_by("-date", "-time")[:5]
    )

    medical_records = (
        MedicalRecord.objects.select_related("pet", "pet__owner")
        .filter(
            Q(pet__name__icontains=query)
            | Q(diagnosis__icontains=query)
            | Q(symptoms__icontains=query)
        )
        .order_by("-record_date")[:5]
    )

    return {
        "search_query": query,
        "search_owners": owners,
        "search_pets": pets,
        "search_appointments": appointments,
        "search_medical_records": medical_records,
        "has_results": any([owners, pets, appointments, medical_records]),
    }


@login_required
def admin_global_search(request):
    """
    HTMX view — header global search. Returns a grouped dropdown of
    matching pet owners, pets, appointments, and medical records.
    Minimum 2 characters required to avoid noisy single-letter queries.
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    query = request.GET.get("q", "").strip()

    if len(query) < 2:
        return render(
            request,
            "shared/_global_search_results.html",
            {"search_query": query, "has_results": False, "too_short": True},
        )

    context = _run_global_search(query)
    return render(request, "shared/_global_search_results.html", context)


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
    """Admin dashboard — full page, stats/widgets/calendar render server-side first."""
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    context = _get_admin_dashboard_context(request)

    # Calendar widget — month view, current date, on initial load only.
    # After this, navigation happens entirely via HTMX against the
    # appointments app's endpoints — it's deliberately NOT part of the
    # 30s polling partial, so the admin's nav position never resets.
    context.update(get_admin_calendar_context("month", None))

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
