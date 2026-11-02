# pets/utils.py

import json
import logging
from datetime import timedelta
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlencode

from allauth.socialaccount.models import SocialAccount
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Exists, F, OuterRef, Prefetch, Q, Value
from django.db.models.functions import Replace
from django.utils import timezone

from accounts.utils import create_owner_user
from appointments.models import Appointment
from appointments.utils import paging_links
from medical.models import Vaccination
from notifications.models import Notification
from notifications.utils import notify

from .models import Pet, PetDeletionRequest, PetOwner

logger = logging.getLogger(__name__)

OWNER_PAGE_SIZE = 25
OWNER_TABS = ("active", "archived")
RECENT_VISITS_LIMIT = 5
PET_PAGE_SIZE = 25
PET_TABS = ("active", "archived")
REQUEST_PAGE_SIZE = 25
RECENT_PET_ITEMS = 3
VACCINATION_DUE_DAYS = 30
PSGC_FILE = Path(__file__).resolve().parent / "data" / "psgc_marinduque.json"


# ── Marinduque addresses (PSA PSGC list) ─────────────────────────────────────


@lru_cache(maxsize=1)
def psgc_marinduque():
    """The PSA list in data/psgc_marinduque.json, read once per process."""
    with open(PSGC_FILE, encoding="utf-8") as f:
        return json.load(f)


def _match_key(value):
    """Ignores case and extra spaces: " santa  CRUZ " → "santa cruz"."""
    return " ".join((value or "").split()).casefold()


def _match(value, names):
    """The official spelling in names that matches value, or None."""
    key = _match_key(value)
    if not key:
        return None
    return next((name for name in names if _match_key(name) == key), None)


def marinduque_towns():
    """The 6 towns, A to Z, in the PSA spelling."""
    return list(psgc_marinduque()["towns"])


def match_town(value):
    """The official spelling of a Marinduque town, or None."""
    return _match(value, marinduque_towns())


def barangays_for(town):
    """The barangays of a town (A to Z, PSA spelling); [] for an unknown town."""
    official = match_town(town)
    if official is None:
        return []
    return list(psgc_marinduque()["towns"][official])


def match_barangay(town, value):
    """The official spelling of a barangay inside town, or None."""
    return _match(value, barangays_for(town))


def barangay_choices(town):
    """Options for the Barangay select: the placeholder first."""
    barangays = barangays_for(town)
    if not barangays:
        return [("", "Choose the town first")]
    return [("", "Choose a barangay")] + [(name, name) for name in barangays]


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
        # Old template's keys; no template uses them since 4a-2 (tests still read them)
        "search_query": q,
        "status_filter": active_tab,
        "total_count": page_obj.paginator.count,
    }


# ── Admin owner detail ───────────────────────────────────────────────────────


def owner_account_state(owner):
    """
    "archived", "no_email" (walk-in without an email), "unclaimed" (email
    but the account was never set up) or "active".

    Google sign-ups have no usable password, so "unclaimed" also needs no
    login yet and no linked social account.
    """
    user = owner.user
    if owner.is_archived:
        return "archived"
    if not user.email:
        return "no_email"
    if (
        not user.has_usable_password()
        and user.last_login is None
        and not SocialAccount.objects.filter(user=user).exists()
    ):
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
    state = owner_account_state(owner)
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


# ── Admin pets list ──────────────────────────────────────────────────────────

# A pet is on the Archived tab when it is archived or its owner is
ARCHIVED_PET = Q(is_archived=True) | Q(owner__is_archived=True)


def search_pets(pets, q):
    """Every word in q must match the pet's name, species or breed, or the
    owner's first or last name."""
    for word in q.split():
        pets = pets.filter(
            Q(name__icontains=word)
            | Q(species__icontains=word)
            | Q(breed__icontains=word)
            | Q(owner__first_name__icontains=word)
            | Q(owner__last_name__icontains=word)
        )
    return pets


def list_owner(value):
    """The PetOwner for ?owner=<pk>, or None when value is missing, not a
    number or unknown (ignored, never an error page)."""
    try:
        pk = int(value)
    except (TypeError, ValueError):
        return None
    return PetOwner.objects.filter(pk=pk).first()


def _pet_list_query(active_tab, q, species, owner):
    """The pager's query string: tab, search and owner, plus the old
    ?species= when an old link used it."""
    params = {
        "status": active_tab,
        "q": q,
        "species": species,
        "owner": owner.pk if owner else "",
    }
    return urlencode({key: value for key, value in params.items() if value})


def pet_list_context(params):
    """
    Context for the admin pets list from the request's GET params:
    ?status=active|archived (anything else → active), ?q= (the old ?search=
    when q is missing), the old ?species=, ?owner=<pk> and ?page=.
    The tab counts follow ?owner= but ignore the search.
    """
    status = params.get("status")
    active_tab = status if status in PET_TABS else "active"
    q = params.get("q")
    if q is None:
        q = params.get("search", "")
    q = q.strip()
    species = params.get("species", "").strip()
    owner = list_owner(params.get("owner"))

    pets = Pet.objects.all()
    if owner:
        pets = pets.filter(owner=owner)
    counts = pets.aggregate(
        active_count=Count("pk", filter=~ARCHIVED_PET),
        archived_count=Count("pk", filter=ARCHIVED_PET),
    )

    if active_tab == "archived":
        pets = pets.filter(ARCHIVED_PET)
    else:
        pets = pets.filter(is_archived=False, owner__is_archived=False)
    pets = search_pets(pets, q)
    if species:
        pets = pets.filter(species__icontains=species)
    waiting = PetDeletionRequest.objects.filter(
        pet=OuterRef("pk"), status=PetDeletionRequest.PENDING
    )
    pets = (
        pets.select_related("owner", "owner__user")
        .annotate(has_pending_deletion=Exists(waiting))
        .order_by("name", "owner__first_name", "pk")
    )

    page_obj = Paginator(pets, PET_PAGE_SIZE).get_page(params.get("page"))
    page_query = _pet_list_query(active_tab, q, species, owner)
    return {
        "page_obj": page_obj,
        "pets": page_obj.object_list,
        "q": q,
        "is_searching": bool(q),
        "active_tab": active_tab,
        "owner_filter": owner,
        **counts,
        "pending_requests_count": PetDeletionRequest.objects.filter(
            status=PetDeletionRequest.PENDING
        ).count(),
        **paging_links(page_obj, page_query),
        # Old template's keys (until the 4b-2 redesign)
        "search_query": q,
        "species_filter": species,
        "species_list": Pet.objects.filter(is_archived=False)
        .values_list("species", flat=True)
        .distinct()
        .order_by("species"),
        "total_count": page_obj.paginator.count,
    }


# ── Deletion requests ────────────────────────────────────────────────────────


def deletion_requests_context(params):
    """Waiting requests oldest first (all of them); answered ones newest
    first by reviewed_at, 25 per page."""
    requests = PetDeletionRequest.objects.select_related(
        "pet", "pet__owner", "requested_by"
    )
    pending = list(
        requests.filter(status=PetDeletionRequest.PENDING).order_by("created_at", "pk")
    )
    resolved = (
        requests.exclude(status=PetDeletionRequest.PENDING)
        .select_related("reviewed_by")
        .order_by(F("reviewed_at").desc(nulls_last=True), "-pk")
    )
    page_obj = Paginator(resolved, REQUEST_PAGE_SIZE).get_page(params.get("page"))
    return {
        "pending": pending,
        "pending_count": len(pending),
        "page_obj": page_obj,
        "resolved": page_obj.object_list,
        **paging_links(page_obj, ""),
    }


def deletion_answer_refusal(deletion_request, approve):
    """Why Archive (approve) or Keep can't be saved, or None."""
    pet = deletion_request.pet
    if deletion_request.status != PetDeletionRequest.PENDING:
        return "This request was already answered."
    if approve and pet.is_archived:
        return f"{pet.name} is already archived."
    if approve and pet.owner.is_archived:
        return f"Restore {pet.owner.full_name} first."
    return None


def answer_deletion_request(deletion_request, reviewer, approve):
    """
    Archive (approve) or Keep (reject). The pet and the request are saved
    in one transaction; the owner is told after it commits.
    """
    pet = deletion_request.pet
    now = timezone.now()
    with transaction.atomic():
        if approve:
            pet.is_archived = True
            pet.archived_at = now
            pet.save()
        deletion_request.status = (
            PetDeletionRequest.APPROVED if approve else PetDeletionRequest.REJECTED
        )
        deletion_request.reviewed_by = reviewer
        deletion_request.reviewed_at = now
        deletion_request.save()
    notify_deletion_answer(pet, approve)


def notify_deletion_answer(pet, approved):
    """
    Tells the owner in the app, and by email when they have one: this type
    has no NotificationPreference toggle, so notify() always emails it.
    The Archive answer has no related_pet (the owner can't open an archived
    pet, so the link would 404). Never raises: a failure is logged and the
    answer stays saved. notify() itself records a failed send on the row.
    """
    if approved:
        title = f"{pet.name} was removed"
        message = f"The clinic removed {pet.name} from your account, as you asked."
    else:
        title = f"{pet.name} stays on your account"
        message = (
            f"The clinic kept {pet.name} on your account. If you still want it "
            "removed, please contact the clinic."
        )
    try:
        notify(
            recipient=pet.owner.user,
            notification_type=Notification.PET_DELETION_ANSWERED,
            title=title,
            message=message,
            related_pet=None if approved else pet,
            email_subject=f"{title} — Hapi Vet",
        )
    except Exception:
        logger.exception("Deletion answer not sent to the owner of pet %s", pet.pk)


# ── Archived pets ────────────────────────────────────────────────────────────


def pet_restore_refusal(pet):
    """Why the pet can't be restored, or None. A pet whose owner is
    archived comes back with the owner, never on its own."""
    if not pet.is_archived:
        return "This pet isn't archived."
    if pet.owner.is_archived:
        return f"Restore {pet.owner.full_name} first. Their pets come back with them."
    return None


def archived_pet_refusal(pet):
    """
    For pages that change a pet (edit, new record, new vaccination): the
    message when the pet or its owner is archived, or None. The owner comes
    first, since an archived pet can't be restored while its owner is.
    """
    if pet.owner.is_archived:
        return f"Restore {pet.owner.full_name} first."
    if pet.is_archived:
        return f"Restore {pet.name} first."
    return None


def pet_state(pet):
    """ "archived", "owner_archived" or "active"."""
    if pet.is_archived:
        return "archived"
    if pet.owner.is_archived:
        return "owner_archived"
    return "active"


# ── Admin pet detail ─────────────────────────────────────────────────────────


def vaccination_due_soon(pet, today):
    """
    The vaccination with the earliest next_due_date from today to today + 30
    days, or None. A shot already followed by a later dose of the same
    vaccine doesn't count. Corrected entries count like any other: a
    correction edits the real record, it stays valid.
    """
    later_dose = Vaccination.objects.filter(
        pet=OuterRef("pet"),
        vaccine_name=OuterRef("vaccine_name"),
        custom_vaccine_name=OuterRef("custom_vaccine_name"),
        date_administered__gt=OuterRef("date_administered"),
    )
    return (
        pet.vaccinations.filter(
            next_due_date__range=(today, today + timedelta(days=VACCINATION_DUE_DAYS)),
        )
        .filter(~Exists(later_dose))
        .order_by("next_due_date", "pk")
        .first()
    )


def pet_detail_context(pet, today):
    """Extra context for the pet page (for the 4b-3 redesign)."""
    visits = pet.appointments.select_related("service")
    upcoming = list(
        visits.filter(
            status__in=[Appointment.PENDING, Appointment.CONFIRMED],
            date__gte=today,
        ).order_by("date", "time", "pk")
    )
    records = pet.medical_records.order_by("-record_date", "-pk")
    vaccinations = pet.vaccinations.order_by("-date_administered", "-pk")
    return {
        "recent_visits": visits.order_by("-date", "-time", "-pk")[:RECENT_PET_ITEMS],
        # The appointments list searched by the pet's name, on "All"
        "visits_query": urlencode({"status": "all", "q": pet.name}),
        "recent_records": records[:RECENT_PET_ITEMS],
        "records_count": records.count(),
        "recent_vaccinations": vaccinations[:RECENT_PET_ITEMS],
        "vaccinations_count": vaccinations.count(),
        "vaccination_due_soon": vaccination_due_soon(pet, today),
        "owner_archived": pet.owner.is_archived,
        "pet_state": pet_state(pet),
        "upcoming_count": len(upcoming),
        "next_upcoming_visit": upcoming[0] if upcoming else None,
    }
