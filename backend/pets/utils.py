# pets/utils.py

import json
import logging
import secrets
from datetime import timedelta
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlencode

from allauth.socialaccount.models import SocialAccount
from django.contrib import messages
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Exists, F, OuterRef, Prefetch, Q, Value
from django.db.models.functions import Replace
from django.http import Http404
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.dateformat import format as date_format
from django.utils.text import Truncator

from accounts.utils import create_owner_user
from appointments.models import Appointment
from appointments.utils import get_clinic_settings, paging_links
from notifications.models import Notification
from notifications.utils import compact_message, notify

from .models import Pet, PetDeletionRequest, PetOwner
from .templatetags.pet_display import weight_kg

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


# ── Pet species and breeds ───────────────────────────────────────────────────

# The species / breed lists for the pet forms (no JS copy any more; the
# forms render the dropdowns on the server). "Other" is not a species; the
# forms add it.
PET_BREEDS_BY_SPECIES = {
    "Dog": [
        "Aspin (Native)",
        "Labrador Retriever",
        "Shih Tzu",
        "Chihuahua",
        "Poodle",
        "Siberian Husky",
        "Golden Retriever",
        "Beagle",
        "Pomeranian",
        "Dachshund",
        "German Shepherd",
        "Rottweiler",
        "Shiba Inu",
        "French Bulldog",
        "Pug",
        "Mixed Breed",
    ],
    "Cat": [
        "Puspin (Native)",
        "Persian",
        "Siamese",
        "British Shorthair",
        "Maine Coon",
        "Ragdoll",
        "American Shorthair",
        "Scottish Fold",
        "Munchkin",
        "Mixed Breed",
    ],
    "Bird": [
        "Parakeet (Budgerigar)",
        "Lovebird",
        "Cockatiel",
        "Cockatoo",
        "Philippine Hanging Parrot (Colasisi)",
        "Mynah",
        "Canary",
    ],
    "Rabbit": ["Holland Lop", "Netherland Dwarf", "Rex", "Dutch", "Mixed Breed"],
    "Hamster": ["Syrian", "Dwarf Campbell", "Roborovski", "Winter White"],
}
PET_OTHER = "__other__"


def species_options():
    """The species in the list, in order (no "Other")."""
    return list(PET_BREEDS_BY_SPECIES)


def match_species(value):
    """The official spelling of a species in the list, or None."""
    return _match(value, species_options())


def breeds_for(species):
    """The breeds of a species in the list (any case); [] for another species."""
    official = match_species(species)
    if official is None:
        return []
    return list(PET_BREEDS_BY_SPECIES[official])


def match_breed(species, value):
    """The official spelling of a breed in that species' list, or None."""
    return _match(value, breeds_for(species))


def breed_choices(species):
    """Options for the Breed select: "Not sure" first and "Other (type it)"
    last, or only "Choose the species first" for an empty or unknown one."""
    if not match_species(species):
        return [("", "Choose the species first")]
    breeds = [(name, name) for name in breeds_for(species)]
    return [("", "Not sure")] + breeds + [(PET_OTHER, "Other (type it)")]


def town_from_params(params):
    """The town an HTMX barangay request asks for: ?town=, or the Town
    select's own name ?address_town= (the admin and owner endpoints)."""
    return params.get("town", params.get("address_town", ""))


def species_from_params(params):
    """The species an HTMX breed request asks for: ?species=, or the Species
    select's own name ?species_choice= (the admin and owner endpoints)."""
    return params.get("species", params.get("species_choice", ""))


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


def contact_link_initial(owner):
    """Initial rows for the contact links formset (none without a row)."""
    if owner is None:
        return []
    return [
        {"platform": link.platform, "url_or_handle": link.url_or_handle}
        for link in owner.contact_links.all()
    ]


def onboarding_owner(session, user):
    """The PetOwner saved by step 1 (session key onboarding_owner_id), or
    None when there is no key or it isn't this user's row."""
    owner_id = session.get("onboarding_owner_id")
    if not owner_id:
        return None
    return PetOwner.objects.filter(pk=owner_id, user=user).first()


def onboarding_step1_context(profile_form, link_formset):
    """Context of onboarding step 1: both forms, the number of things to fix
    for the red box (fields, plus link rows with a problem), and whether the
    links box opens (saved or mistyped links)."""
    bad_rows = [form for form in link_formset if form.errors]
    has_text = any(
        form["platform"].value() or form["url_or_handle"].value()
        for form in link_formset
    )
    return {
        "profile_form": profile_form,
        "link_formset": link_formset,
        "error_count": profile_form.error_count + len(bad_rows),
        "links_open": bool(bad_rows) or bool(has_text),
    }


def save_owner_profile(profile_form, link_formset, user):
    """
    Saves the owner's profile (onboarding step 1 and profile edit) and
    replaces their contact links, all in one transaction: if re-adding a
    link fails, the old links and the old profile stay. Returns the owner.
    """
    with transaction.atomic():
        owner = profile_form.save(commit=False)
        owner.user = user
        owner.save()
        owner.contact_links.all().delete()
        for form in link_formset:
            if form.is_valid() and not form.is_empty():
                link = form.save(commit=False)
                link.owner = owner
                link.save()
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
    # Only answers: a request the owner took back (CANCELLED) was never
    # answered by the clinic, so it is not history here
    resolved = (
        requests.filter(
            status__in=[PetDeletionRequest.APPROVED, PetDeletionRequest.REJECTED]
        )
        .select_related("reviewed_by")
        .order_by(F("reviewed_at").desc(nulls_last=True), "-pk")
    )
    page_obj = Paginator(resolved, REQUEST_PAGE_SIZE).get_page(params.get("page"))
    # The page shares the pets list's tabs, so it needs their counts too
    tab_counts = Pet.objects.aggregate(
        active_count=Count("pk", filter=~ARCHIVED_PET),
        archived_count=Count("pk", filter=ARCHIVED_PET),
    )
    return {
        "pending": pending,
        "pending_count": len(pending),
        "page_obj": page_obj,
        "resolved": page_obj.object_list,
        **paging_links(page_obj, ""),
        "active_tab": "requests",
        "q": "",
        **tab_counts,
        "pending_requests_count": len(pending),
    }


def take_back_removal_request(pet, user):
    """
    The owner takes back their waiting removal request for pet: it becomes
    CANCELLED (reviewed_by and reviewed_at stay empty; the row is kept).
    Returns False when the user has no PENDING request for the pet. The row
    is locked while it changes, so a clinic answer at the same moment wins
    or loses cleanly.
    """
    with transaction.atomic():
        waiting = (
            PetDeletionRequest.objects.select_for_update()
            .filter(pet=pet, requested_by=user, status=PetDeletionRequest.PENDING)
            .first()
        )
        if waiting is None:
            return False
        waiting.status = PetDeletionRequest.CANCELLED
        waiting.save(update_fields=["status"])
    return True


def deletion_answer_refusal(deletion_request, approve):
    """Why Archive (approve) or Keep can't be saved, or None."""
    pet = deletion_request.pet
    if deletion_request.status == PetDeletionRequest.CANCELLED:
        return "The owner took this request back."
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
        title = "Pet removed"
        message = compact_message(pet.name, "Removed from your account, as you asked.")
        email_body = f"The clinic removed {pet.name} from your account, as you asked."
    else:
        title = "Pet stays on your account"
        message = compact_message(
            pet.name, "The clinic kept your pet. Call us if you still want it removed."
        )
        email_body = (
            f"The clinic kept {pet.name} on your account. If you still want it "
            "removed, please contact the clinic."
        )
    # The email subject names the pet; the in-app title stays short
    subject = (
        f"{pet.name} was removed — Hapi Vet"
        if approved
        else f"{pet.name} stays on your account — Hapi Vet"
    )
    _send_removal_notice(
        pet,
        title,
        message,
        email_body,
        related_pet=None if approved else pet,
        subject=subject,
    )


def _send_removal_notice(pet, title, message, email_body, related_pet, subject):
    """notify() the pet's owner (PET_DELETION_ANSWERED): a short title and
    compact message in the app, the full sentence in the email, whose subject
    names the pet. Never raises."""
    try:
        notify(
            recipient=pet.owner.user,
            notification_type=Notification.PET_DELETION_ANSWERED,
            title=title,
            message=message,
            related_pet=related_pet,
            email_subject=subject,
            email_body=email_body,
        )
    except Exception:
        logger.exception("Deletion answer not sent to the owner of pet %s", pet.pk)


# ── Direct archive (no owner request) ────────────────────────────────────────


def pet_archive_refusal(pet):
    """Why the clinic can't archive the pet directly, or None. A waiting
    owner request is answered instead (Archive / Keep)."""
    if pet.is_archived:
        return "This pet is already archived."
    if pet.owner.is_archived:
        return f"Restore {pet.owner.full_name} first."
    if pet.deletion_requests.filter(status=PetDeletionRequest.PENDING).exists():
        return "Answer the waiting request instead."
    return None


def archive_pet_directly(pet, admin, reason, reason_detail):
    """
    Archives the pet with no owner request. The reason is kept on a
    PetDeletionRequest made by the clinic (requested_by = the admin), saved
    already APPROVED, so it shows in the deletion history. The request and
    the pet are saved in one transaction; the owner is told after it.
    """
    now = timezone.now()
    with transaction.atomic():
        deletion_request = PetDeletionRequest.objects.create(
            pet=pet,
            requested_by=admin,
            reason=reason,
            reason_detail=reason_detail,
            status=PetDeletionRequest.APPROVED,
            reviewed_by=admin,
            reviewed_at=now,
        )
        pet.is_archived = True
        pet.archived_at = now
        pet.save()
    reason_label = deletion_request.get_reason_display().lower()
    _send_removal_notice(
        pet,
        "Pet removed",
        compact_message(pet.name, f"Removed by the clinic ({reason_label})."),
        f"The clinic removed {pet.name} from your account (reason: {reason_label}).",
        related_pet=None,
        subject=f"{pet.name} was removed — Hapi Vet",
    )
    return deletion_request


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
    correction edits the real record, it stays valid. Always None for an
    archived pet or a pet whose owner is archived (reminders are refused
    there, so the line isn't shown).
    """
    # Imported here: medical.utils imports ARCHIVED_PET from this module
    from medical.utils import later_dose_exists

    if pet_state(pet) != "active":
        return None
    return (
        pet.vaccinations.filter(
            next_due_date__range=(today, today + timedelta(days=VACCINATION_DUE_DAYS)),
        )
        .filter(~later_dose_exists())
        .order_by("next_due_date", "pk")
        .first()
    )


def archive_request(pet):
    """For an archived pet: the latest APPROVED deletion request (the one
    that archived it, by the owner or by the clinic), or None."""
    if not pet.is_archived:
        return None
    return (
        pet.deletion_requests.filter(status=PetDeletionRequest.APPROVED)
        .select_related("requested_by")
        .order_by(F("reviewed_at").desc(nulls_last=True), "-pk")
        .first()
    )


def pet_detail_context(pet, today):
    """Extra context for the pet page (4b-1, used by the 4b-3 redesign)."""
    visits = pet.appointments.select_related("service")
    upcoming = list(
        visits.filter(
            status__in=[Appointment.PENDING, Appointment.CONFIRMED],
            date__gte=today,
        ).order_by("date", "time", "pk")
    )
    records = pet.medical_records.order_by("-record_date", "-pk")
    vaccinations = pet.vaccinations.order_by("-date_administered", "-pk")
    due_soon = vaccination_due_soon(pet, today)
    pending_deletion = pet.deletion_requests.filter(
        status=PetDeletionRequest.PENDING
    ).first()
    state = pet_state(pet)
    return {
        "recent_visits": visits.order_by("-date", "-time", "-pk")[:RECENT_PET_ITEMS],
        # The appointments list searched by the pet's name, on "All"
        "visits_query": urlencode({"status": "all", "q": pet.name}),
        "recent_records": records[:RECENT_PET_ITEMS],
        "records_count": records.count(),
        "recent_vaccinations": vaccinations[:RECENT_PET_ITEMS],
        "vaccinations_count": vaccinations.count(),
        "vaccination_due_soon": due_soon,
        # "(in N days)" on the due-soon line
        "due_soon_days": (due_soon.next_due_date - today).days if due_soon else None,
        "owner_archived": pet.owner.is_archived,
        "pet_state": state,
        "upcoming_count": len(upcoming),
        "next_upcoming_visit": upcoming[0] if upcoming else None,
        "pending_deletion": pending_deletion,
        "archive_request": archive_request(pet),
        "show_archive_button": state == "active" and pending_deletion is None,
    }


# ── Owner My Pets (Slice 8b-4) ───────────────────────────────────────────────

# The type chips, in order: (key, label). "other" is any species that is not
# Dog or Cat, including an empty one.
OWNER_PET_TYPES = (
    ("all", "All"),
    ("dog", "Dogs"),
    ("cat", "Cats"),
    ("other", "Others"),
)
# The search box and chips show from this many pets up
OWNER_PET_FILTERS_FROM = 4

_PET_TYPE_FILTERS = {
    "dog": Q(species__iexact="Dog"),
    "cat": Q(species__iexact="Cat"),
    "other": ~(Q(species__iexact="Dog") | Q(species__iexact="Cat")),
}


def _owner_pet_chips(counts, active_type, q):
    """The chips to show: All, and every type the owner has (or has open).
    Each link keeps the search text."""
    chips = []
    for key, label in OWNER_PET_TYPES:
        if key != "all" and not counts[key] and key != active_type:
            continue
        query = {"type": "" if key == "all" else key, "q": q}
        chips.append(
            {
                "key": key,
                "label": label,
                "count": counts[key],
                "is_active": key == active_type,
                "query": urlencode({k: v for k, v in query.items() if v}),
            }
        )
    return chips


def owner_pets_context(owner, params):
    """
    Context for the owner's My Pets page from the request's GET params: the
    owner's own pets that are not archived, by name then pk. ?q= matches the
    pet's name (icontains, spaces collapsed); ?type=all|dog|cat|other (default
    and anything unknown: all). The chip counts cover all of the owner's pets
    and ignore ?q=. Two queries however many pets: the counts and the list.
    """
    q = " ".join(params.get("q", "").split())
    active_type = params.get("type")
    if active_type not in _PET_TYPE_FILTERS:
        active_type = "all"

    own = Pet.objects.filter(owner=owner, is_archived=False)
    totals = own.aggregate(
        total=Count("pk"),
        dog=Count("pk", filter=_PET_TYPE_FILTERS["dog"]),
        cat=Count("pk", filter=_PET_TYPE_FILTERS["cat"]),
        other=Count("pk", filter=_PET_TYPE_FILTERS["other"]),
    )
    counts = {
        "all": totals["total"],
        "dog": totals["dog"],
        "cat": totals["cat"],
        "other": totals["other"],
    }

    pets = own
    if q:
        pets = pets.filter(name__icontains=q)
    if active_type != "all":
        pets = pets.filter(_PET_TYPE_FILTERS[active_type])
    waiting = PetDeletionRequest.objects.filter(
        pet=OuterRef("pk"), status=PetDeletionRequest.PENDING
    )
    pets = list(
        pets.annotate(has_pending_removal=Exists(waiting)).order_by("name", "pk")
    )
    return {
        "pets": pets,
        "total_count": counts["all"],
        "counts": counts,
        "chips": _owner_pet_chips(counts, active_type, q),
        "show_filters": counts["all"] >= OWNER_PET_FILTERS_FROM,
        "q": q,
        "active_type": active_type,
        "is_filtering": bool(q) or active_type != "all",
    }


# ── Owner pet profile, edit and remove (Slice 8b-5) ──────────────────────────
# Every helper takes today / now from the view and never reads the clock.

PROFILE_TABS = (
    ("about", "About"),
    ("vaccines", "Vaccines"),
    ("records", "Records"),
)
# The four reasons on the remove sheet: (value saved, words shown). They are
# the model's own choices, so nothing changes in the database.
REMOVAL_REASONS = (
    (PetDeletionRequest.PASSED_AWAY, "My pet has passed away"),
    (PetDeletionRequest.REHOMED, "My pet has a new home"),
    (PetDeletionRequest.DUPLICATE, "I added this pet twice"),
    (PetDeletionRequest.OTHER, "Other reason"),
)
# What the yellow "Removal requested" box says the owner chose
REMOVAL_REASON_LINES = {
    PetDeletionRequest.PASSED_AWAY: "You said {name} has passed away.",
    PetDeletionRequest.REHOMED: "You said {name} has a new home.",
    PetDeletionRequest.DUPLICATE: "You said {name} was added twice.",
}
RECORD_HEADING_CHARS = 60
RECORD_LINE_CHARS = 90


def pet_age_phrase(born, today):
    """The pet's age in words: "3 years", "1 year", "8 months", "1 month" or
    "under 1 month". "" without a birthday."""
    if born is None:
        return ""
    months = (today.year - born.year) * 12 + today.month - born.month
    if today.day < born.day:
        months -= 1
    if months >= 12:
        years = months // 12
        return f"{years} year{'s' if years != 1 else ''}"
    if months >= 1:
        return f"{months} month{'s' if months != 1 else ''}"
    return "under 1 month"


def pet_age_chip(born, today):
    """ "3 years old", "8 months old" or "Under 1 month old"; "" without a
    birthday (no chip)."""
    phrase = pet_age_phrase(born, today)
    if not phrase:
        return ""
    if phrase == "under 1 month":
        return "Under 1 month old"
    return f"{phrase} old"


def pet_facts(pet, today):
    """The chips under the pet's name: sex (left out when not known), age and
    weight (each left out when empty)."""
    sex = {Pet.MALE: "Male", Pet.FEMALE: "Female"}.get(pet.gender, "")
    weight = f"{weight_kg(pet.weight)} kg" if pet.weight is not None else ""
    return [
        chip for chip in (sex, pet_age_chip(pet.date_of_birth, today), weight) if chip
    ]


def _sex_words(pet):
    return {Pet.MALE: "Male", Pet.FEMALE: "Female"}.get(pet.gender, "Not sure")


def pet_detail_rows(pet, today):
    """The About tab's Details card: (label, value) pairs; an empty value is
    None, which the template shows as a gray "None"."""
    birthday = None
    if pet.date_of_birth:
        day = date_format(pet.date_of_birth, "M j, Y")
        birthday = f"{day} ({pet_age_phrase(pet.date_of_birth, today)})"
    weight = f"{weight_kg(pet.weight)} kg" if pet.weight is not None else None
    return [
        ("Species", pet.species or None),
        ("Breed", pet.breed or None),
        ("Color", pet.color or None),
        ("Male or female", _sex_words(pet)),
        ("Birthday", birthday),
        ("Weight", weight),
        ("Registration no.", pet.registration_number or None),
    ]


def pet_locked_rows(pet):
    """The edit page's "Only the clinic can change these" rows."""
    birthday = date_format(pet.date_of_birth, "M j, Y") if pet.date_of_birth else None
    return [
        ("Species", pet.species or None),
        ("Male or female", _sex_words(pet)),
        ("Birthday", birthday),
        ("Registration no.", pet.registration_number or None),
    ]


def owner_pet_next_visit(owner, pet, now):
    """The pet's soonest Pending or Confirmed visit whose slot hasn't ended
    (one in progress still counts), or None. Two queries: the settings (the
    slot length) and the visit."""
    # Imported here: dashboard.utils imports this app's models
    from dashboard.utils import _owner_visits_not_ended, slot_minutes

    minutes = slot_minutes(get_clinic_settings())
    return (
        _owner_visits_not_ended(owner, now, minutes)
        .filter(pet=pet)
        .order_by("date", "time", "pk")
        .first()
    )


def _vaccine_due_note(pet, row):
    """The orange note for the dose that is due (or overdue)."""
    day = date_format(row["next"], "M j")
    days = row["days"]
    protect = f"Book a visit so {pet.name} stays protected."
    if row["state"] == "overdue":
        return {"title": f"{row['name']} was due on {day}", "text": protect}
    if days == 0:
        when = "That's today."
    else:
        when = f"That's in {days} day{'s' if days != 1 else ''}."
    return {"title": f"{row['name']} is due on {day}", "text": f"{when} {protect}"}


def owner_pet_vaccines(pet, today):
    """
    The Vaccines tab: (history rows, due note). One query. Rows are newest
    given first: name, given, next, is_due (the shared due rule, so a dose a
    later one replaced is never due), is_corrected (never the correction
    note). The note is for the earliest due or overdue dose, or None.
    pet.owner must already be loaded.
    """
    # Imported here: medical.utils imports this module
    from medical.utils import later_dose_exists, vaccination_due_state

    doses = (
        pet.vaccinations.defer("correction_note")
        .annotate(is_superseded=later_dose_exists())
        .order_by("-date_administered", "-pk")
    )
    rows = []
    for dose in doses:
        state, days = vaccination_due_state(dose, today)
        rows.append(
            {
                "name": dose.display_vaccine_name,
                "given": dose.date_administered,
                "next": dose.next_due_date,
                "state": state,
                "days": days,
                "is_due": bool(state),
                "is_corrected": dose.is_corrected,
            }
        )
    due = [row for row in rows if row["is_due"]]
    note = _vaccine_due_note(pet, min(due, key=lambda r: r["next"])) if due else None
    return rows, note


def _record_line(record, heading):
    """One short line from the public fields only, never the heading again."""
    for text in (record.treatment_given, record.symptoms, record.public_notes):
        text = " ".join(text.split())
        if text and text != heading:
            return Truncator(text).chars(RECORD_LINE_CHARS)
    return ""


def owner_pet_records(pet):
    """
    The Records tab: one dict per record, newest first. One query: private_notes
    is deferred (owner_record_queryset), files are counted, and the visit's
    service is joined. heading is the diagnosis (the record page's own title
    when there is none), line is one short public line, follow_up is the date
    when a follow-up is required, files counts the files not removed.
    """
    # Imported here: medical.utils imports this module
    from medical.utils import owner_record_queryset

    records = (
        owner_record_queryset()
        .select_related("appointment__service")
        .filter(pet=pet)
        .annotate(
            file_count=Count(
                "test_result_files", filter=Q(test_result_files__is_archived=False)
            )
        )
        .order_by("-record_date", "-pk")
    )
    rows = []
    for record in records:
        service = record.appointment.service if record.appointment else None
        diagnosis = " ".join(record.diagnosis.split())
        heading = Truncator(diagnosis).chars(RECORD_HEADING_CHARS) or "Visit record"
        follow_up = record.follow_up_date if record.follow_up_required else None
        rows.append(
            {
                "pk": record.pk,
                "date": record.record_date,
                "kind": service.service_name if service else "Visit",
                "heading": heading,
                "line": _record_line(record, diagnosis),
                "follow_up": follow_up,
                "files": record.file_count,
            }
        )
    return rows


def removal_pending_line(pet, deletion_request):
    """The yellow box's line naming the reason in plain words."""
    line = REMOVAL_REASON_LINES.get(deletion_request.reason)
    if line is None:
        return "The clinic will review your request soon."
    return f"{line.format(name=pet.name)} The clinic will review it soon."


def _profile_sheets(pet):
    """The words on the remove and take-back sheets."""
    return {
        "remove_title": f"Remove {pet.name} from your pets?",
        "remove_text": (
            "The clinic will check your request first. "
            f"{pet.name}'s health records are kept safe either way."
        ),
        "remove_keep": f"Keep {pet.name}",
        "take_back_title": "Take back your request?",
        "take_back_text": f"{pet.name} stays on your account.",
    }


def owner_pet_profile_context(owner, pet, params, today, now):
    """
    Context for the owner's pet profile. 404 unless the pet is the owner's
    own and not archived. ?tab=about|vaccines|records (anything else: about);
    only the open tab's queries run. Queries: the waiting removal request, plus
    About 2 (settings, visit), Vaccines 1, Records 1, however many rows.
    """
    if pet.owner_id != owner.pk or pet.is_archived:
        raise Http404("No pet matches the given query.")
    pet.owner = owner  # already known: the due rule reads pet.owner
    tab = params.get("tab")
    if tab not in dict(PROFILE_TABS):
        tab = "about"
    base = reverse("owner_pet_detail", args=[pet.pk])
    pending = PetDeletionRequest.objects.filter(
        pet=pet, status=PetDeletionRequest.PENDING
    ).first()
    context = {
        "pet": pet,
        "today": today,
        "tab": tab,
        "tabs": [
            {"key": key, "label": label, "url": f"{base}?tab={key}"}
            for key, label in PROFILE_TABS
        ],
        "kind_line": " · ".join(part for part in (pet.species, pet.breed) if part),
        "facts": pet_facts(pet, today),
        "pending": pending,
        "pending_line": removal_pending_line(pet, pending) if pending else "",
        "removal_reasons": REMOVAL_REASONS,
        "sheets": _profile_sheets(pet),
    }
    if tab == "about":
        context["next_visit"] = owner_pet_next_visit(owner, pet, now)
        context["details"] = pet_detail_rows(pet, today)
    elif tab == "vaccines":
        context["vaccines"], context["due_note"] = owner_pet_vaccines(pet, today)
    else:
        context["records"] = owner_pet_records(pet)
    return context


def request_pet_removal(pet, user, reason):
    """
    The owner asks the clinic to remove the pet. Returns (message level, text)
    for the flash. No reason, or one that isn't on the sheet, creates nothing.
    A waiting request makes a second one impossible (the pet row is locked
    while it is checked, so two taps can't both pass).
    """
    if reason not in dict(REMOVAL_REASONS):
        return messages.ERROR, "Choose what happened, then send the request."
    with transaction.atomic():
        Pet.objects.select_for_update().get(pk=pet.pk)
        waiting = PetDeletionRequest.objects.filter(
            pet=pet, status=PetDeletionRequest.PENDING
        )
        if waiting.exists():
            return (
                messages.WARNING,
                f"A removal request for {pet.name} is already waiting.",
            )
        PetDeletionRequest.objects.create(
            pet=pet,
            requested_by=user,
            reason=reason,
            status=PetDeletionRequest.PENDING,
        )
    return messages.SUCCESS, "Request sent. The clinic will review it soon."


def owner_pet_edit_context(pet):
    """
    What the edit page shows besides the form, read from the saved pet (call
    it before the form touches the pet): name, photo_url, the locked rows,
    and the clinic's phone ("" when there is none).
    """
    clinic = get_clinic_settings()
    return {
        "pet": pet,
        "pet_name": pet.name,
        "photo_url": pet.photo.url if pet.photo else "",
        "locked_rows": pet_locked_rows(pet),
        "clinic_phone": (clinic.contact_number if clinic else "").strip(),
        "profile_url": reverse("owner_pet_detail", args=[pet.pk]),
    }


# ── Owner list states (Town -> barangays, Species -> breeds) ─────────────────

LIST_TIMEOUT_MS = 10000


def list_state_attrs(target_id, kind):
    """
    The extra attributes of an owner select that swaps a list over HTMX
    (static/js/owner/list_states.js reads them): data-list-field (barangays or
    breeds), the 10 s time-out (matches the slow box of 10b-1), the newest
    request wins, the loading line (hx-indicator) and the "couldn't load"
    line (data-list-error), both written in the page next to the target select.
    """
    return {
        "data-list-field": kind,
        "hx-request": json.dumps({"timeout": LIST_TIMEOUT_MS}),
        "hx-sync": "this:replace",
        "hx-indicator": f"#{target_id}-loading",
        "data-list-error": f"#{target_id}-failed",
    }


# ── One-time form tokens (Add pet and onboarding step 2) ─────────────────────
# A cut-off request followed by "Try again" must not add the pet twice. The
# page carries a hidden form_token; the view spends it once, right before the
# pet is created. Tokens live in the session (no model): a dict of lists keyed
# by form name, at most FORM_TOKEN_CAP per name, the oldest dropped.

FORM_TOKENS_KEY = "form_tokens"
FORM_TOKEN_CAP = 5
ADD_PET_FORM = "add_pet"
ONBOARDING_PET_FORM = "onboarding_pet"
PET_ALREADY_ADDED = "That pet was already added."


def _issued_tokens(request):
    """The session's token dict (a fresh one when it is missing or damaged)."""
    tokens = request.session.get(FORM_TOKENS_KEY)
    return tokens if isinstance(tokens, dict) else {}


def _store_tokens(request, name, issued):
    tokens = _issued_tokens(request)
    tokens[name] = issued[-FORM_TOKEN_CAP:]
    request.session[FORM_TOKENS_KEY] = tokens


def issue_form_token(request, name):
    """A new random token for the form `name`; the 6th oldest one is dropped."""
    value = secrets.token_urlsafe(16)
    issued = list(_issued_tokens(request).get(name, []))
    _store_tokens(request, name, issued + [value])
    return value


def spend_form_token(request, name, value):
    """True once for an issued token (and it is removed); False for a missing,
    wrong or already spent one."""
    issued = list(_issued_tokens(request).get(name, []))
    if not value or value not in issued:
        return False
    issued.remove(value)
    _store_tokens(request, name, issued)
    return True


def page_form_token(request, name):
    """The token the page carries: the posted one while it is still valid (an
    invalid POST keeps it), else a new one."""
    posted = request.POST.get("form_token", "") if request.method == "POST" else ""
    if posted and posted in _issued_tokens(request).get(name, []):
        return posted
    return issue_form_token(request, name)


def save_pet_once(request, form, owner, name):
    """
    Creates the pet of a valid OwnerPetForm for `owner`, once per token: the
    token is spent right before the pet is saved, in the same transaction.
    Returns the pet, or None (nothing created) for a missing, wrong or spent
    token. If the save fails the token is put back, so Try again still works.
    """
    value = request.POST.get("form_token", "")
    try:
        with transaction.atomic():
            if not spend_form_token(request, name, value):
                return None
            pet = form.save(commit=False)
            pet.owner = owner
            pet.save()
    except Exception:
        _store_tokens(request, name, _issued_tokens(request).get(name, []) + [value])
        raise
    return pet


def already_added_redirect(request, target):
    """The repeat of an add: nothing was created; a plain info line and a redirect
    to `target` (a URL name or a path)."""
    messages.info(request, PET_ALREADY_ADDED)
    return redirect(target)


def after_onboarding_add_target(session):
    """Where a repeated onboarding add goes: step 3 when a pet was just added
    in this session, else the dashboard."""
    if session.get("onboarding_pet_id"):
        return "owner_onboarding_step3"
    return "owner_dashboard"


def onboarded_step2_response(request):
    """An already onboarded user on step 2. A POST that isn't Skip is a repeat
    of the add (Try again after a cut-off request): the info line and step 3 (or
    the dashboard). Anything else goes to the dashboard as before."""
    if request.method == "POST" and request.POST.get("action") != "skip":
        target = after_onboarding_add_target(request.session)
        return already_added_redirect(request, target)
    return redirect("owner_dashboard")
