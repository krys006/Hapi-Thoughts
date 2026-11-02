# pets/utils.py

from urllib.parse import urlencode

from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Prefetch, Q, Value
from django.db.models.functions import Replace

from accounts.utils import create_owner_user
from appointments.models import Appointment
from appointments.utils import paging_links

from .models import Pet, PetOwner

OWNER_PAGE_SIZE = 25
OWNER_TABS = ("active", "archived")
RECENT_VISITS_LIMIT = 5


# ── Admin owners list ────────────────────────────────────────────────────────


def _active_pets_prefetch():
    """Each owner's non-archived pets, by name, as owner.active_pets."""
    pets = Pet.objects.filter(is_archived=False).order_by("name")
    return Prefetch("pets", queryset=pets, to_attr="active_pets")


def search_owners(owners, q):
    """
    Every word in q must match the first name, last name, email or phone.
    Spaces in the stored phone are ignored, so "0917 123 4567" and
    "09171234567" find each other.
    """
    owners = owners.annotate(
        phone_no_spaces=Replace("contact_number", Value(" "), Value(""))
    )
    for word in q.split():
        owners = owners.filter(
            Q(first_name__icontains=word)
            | Q(last_name__icontains=word)
            | Q(user__email__icontains=word)
            | Q(phone_no_spaces__icontains=word)
        )
    return owners


def owner_list_context(params):
    """
    Context for the admin owners list from the request's GET params:
    ?status=active|archived (anything else → active), ?q= (the old ?search=
    when q is missing) and ?page=. The search stays inside the tab; the
    tab counts ignore it.
    """
    status = params.get("status")
    active_tab = status if status in OWNER_TABS else "active"
    q = params.get("q")
    if q is None:
        q = params.get("search", "")
    q = q.strip()

    counts = PetOwner.objects.aggregate(
        active_count=Count("pk", filter=Q(is_archived=False)),
        archived_count=Count("pk", filter=Q(is_archived=True)),
    )

    owners = PetOwner.objects.filter(is_archived=active_tab == "archived")
    owners = search_owners(owners.select_related("user"), q)
    owners = owners.annotate(
        active_pet_count=Count("pets", filter=Q(pets__is_archived=False))
    ).prefetch_related(_active_pets_prefetch())
    if active_tab == "archived":
        owners = owners.order_by("-archived_at", "pk")
    else:
        owners = owners.order_by("first_name", "last_name", "pk")

    page_obj = Paginator(owners, OWNER_PAGE_SIZE).get_page(params.get("page"))
    # The pager keeps the tab and the search
    page_query = urlencode(
        {"status": active_tab, "q": q} if q else {"status": active_tab}
    )
    return {
        "page_obj": page_obj,
        "owners": page_obj.object_list,
        "q": q,
        "is_searching": bool(q),
        "active_tab": active_tab,
        **counts,
        **paging_links(page_obj, page_query),
        # Kept for the old template until the 4a-3 redesign
        "search_query": q,
        "status_filter": active_tab,
        "total_count": page_obj.paginator.count,
    }


# ── Admin owner detail ───────────────────────────────────────────────────────


def account_state(owner):
    """
    "archived", "no_email" (walk-in without an email), "unclaimed" (email
    but no password set yet) or "active".
    """
    if owner.is_archived:
        return "archived"
    if not owner.user.email:
        return "no_email"
    if not owner.user.has_usable_password():
        return "unclaimed"
    return "active"


def owner_detail_context(owner, today):
    """Extra context for the owner page (for the 4a-3 redesign)."""
    visits = owner.appointments.select_related("pet", "service")
    upcoming = list(
        visits.filter(
            status__in=[Appointment.PENDING, Appointment.CONFIRMED],
            date__gte=today,
        ).order_by("date", "time", "pk")
    )
    state = account_state(owner)
    return {
        "recent_visits": visits.order_by("-date", "-time", "-pk")[:RECENT_VISITS_LIMIT],
        # The appointments list searched by this owner's name, on "All"
        "visits_query": urlencode({"status": "all", "q": owner.full_name}),
        "account_state": state,
        "can_resend": state == "unclaimed",
        "upcoming_visits": upcoming,
        "next_upcoming_visit": upcoming[0] if upcoming else None,
        "upcoming_count": len(upcoming),
    }


# ── Admin Add owner ──────────────────────────────────────────────────────────


def create_owner(form):
    """
    Saves a valid AdminPetOwnerForm as a new User + PetOwner in one
    transaction (no pet, no appointment). Sends no email.
    """
    data = form.cleaned_data
    with transaction.atomic():
        owner = form.save(commit=False)
        owner.user = create_owner_user(
            data["first_name"], data["last_name"], data.get("email") or ""
        )
        owner.save()
    return owner
