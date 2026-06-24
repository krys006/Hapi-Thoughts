from django.contrib.auth.decorators import login_required
from django.db.models import Q
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


def _run_global_search(query):
    """
    Admin global search — pet owners, pets, appointments, medical records.
    Each group capped at 5 results. Archived owners/pets excluded.
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


def _get_owner_dashboard_context(request):
    """
    Builds the widget context for the pet owner dashboard.
    Shared by the full-page view and the HTMX polling partial.
    """
    owner = request.user.petowner
    today = timezone.now().date()

    upcoming_appointments = (
        Appointment.objects.filter(owner=owner, date__gte=today)
        .exclude(status=Appointment.CANCELLED)
        .select_related("pet", "service")
        .order_by("date", "time")[:5]
    )

    my_pets = Pet.objects.filter(owner=owner, is_archived=False).order_by("name")[:6]

    recent_notifications = Notification.objects.filter(
        recipient=request.user,
    ).order_by("is_read", "-created_at")[:5]

    return {
        "today": today,
        "upcoming_appointments": upcoming_appointments,
        "my_pets": my_pets,
        "recent_notifications": recent_notifications,
    }


def _run_owner_search(owner, query):
    """
    Pet owner scoped search — own pets, appointments, billing, and
    medical records only. Each group capped at 5 results.
    """
    pets = Pet.objects.filter(
        Q(name__icontains=query)
        | Q(species__icontains=query)
        | Q(breed__icontains=query),
        owner=owner,
        is_archived=False,
    )[:5]

    appointments = (
        Appointment.objects.filter(
            Q(pet__name__icontains=query) | Q(reason__icontains=query),
            owner=owner,
        )
        .select_related("pet")
        .order_by("-date", "-time")[:5]
    )

    receipts = (
        BillingReceipt.objects.filter(
            Q(receipt_number__icontains=query) | Q(pet__name__icontains=query),
            owner=owner,
        )
        .select_related("pet")
        .order_by("-billing_date")[:5]
    )

    # Matches on diagnosis/symptoms text only — private_notes is never
    # searched or displayed, consistent with the public/private note rule.
    medical_records = (
        MedicalRecord.objects.filter(
            Q(pet__name__icontains=query)
            | Q(diagnosis__icontains=query)
            | Q(symptoms__icontains=query),
            pet__owner=owner,
        )
        .select_related("pet")
        .order_by("-record_date")[:5]
    )

    return {
        "search_query": query,
        "search_pets": pets,
        "search_appointments": appointments,
        "search_receipts": receipts,
        "search_medical_records": medical_records,
        "has_results": any([pets, appointments, receipts, medical_records]),
    }


@login_required
def admin_dashboard(request):
    """Admin dashboard — full page, stats/widgets/calendar render server-side first."""
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    from appointments.utils import get_admin_calendar_context

    context = _get_admin_dashboard_context(request)
    context.update(get_admin_calendar_context("month", None))

    return render(request, "admin/dashboard/index.html", context)


@login_required
def admin_dashboard_partial(request):
    """
    Returns the stats + widgets block only.
    Polled automatically every 30s by the block itself.
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    context = _get_admin_dashboard_context(request)
    return render(request, "admin/dashboard/_dashboard_partial.html", context)


@login_required
def admin_global_search(request):
    """
    HTMX view — header global search. Returns a grouped dropdown of
    matching pet owners, pets, appointments, and medical records.
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


@login_required
def owner_dashboard(request):
    """Owner dashboard — full page, widgets render server-side first."""
    if request.user.role != "pet_owner":
        return redirect("admin_dashboard")
    if not request.user.is_onboarded:
        return redirect("owner_onboarding")

    context = _get_owner_dashboard_context(request)
    return render(request, "owner/dashboard/index.html", context)


@login_required
def owner_dashboard_partial(request):
    """
    Returns the owner dashboard widgets block only.
    Polled automatically every 30s by the block itself.
    """
    if request.user.role != "pet_owner":
        return redirect("admin_dashboard")
    if not request.user.is_onboarded:
        return redirect("owner_onboarding")

    context = _get_owner_dashboard_context(request)
    return render(request, "owner/dashboard/_dashboard_partial.html", context)


@login_required
def owner_global_search(request):
    """
    HTMX view — header scoped search for pet owners. Returns a grouped
    dropdown of the owner's own pets, appointments, billing, and
    medical records only.
    """
    if request.user.role != "pet_owner":
        return redirect("admin_dashboard")

    owner = request.user.petowner
    query = request.GET.get("q", "").strip()

    if len(query) < 2:
        return render(
            request,
            "shared/_owner_search_results.html",
            {"search_query": query, "has_results": False, "too_short": True},
        )

    context = _run_owner_search(owner, query)
    return render(request, "shared/_owner_search_results.html", context)
