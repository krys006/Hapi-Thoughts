"""Helpers for the medical views."""

from datetime import timedelta
from urllib.parse import urlencode

from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Count, Exists, OuterRef, Q
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateformat import format as date_format

from appointments.models import Appointment
from appointments.utils import paging_links
from notifications.utils import notify
from pets.utils import ARCHIVED_PET, pet_state

from .models import MedicalRecord, Vaccination

RECORD_VISIT_STATUSES = (Appointment.CONFIRMED, Appointment.COMPLETED)
RECORD_EXISTS = "A medical record already exists for this appointment."
# "This week" on the dashboard and the Due / Follow-ups tabs: today to today + 7
DUE_DAYS = 7
LIST_PAGE_SIZE = 25
VACCINATION_TABS = ("due", "overdue", "all")
RECORD_TABS = ("followups", "all")


# ── Due rule (one shared helper) ─────────────────────────────────────────────
# Every helper takes today (timezone.localdate() from the caller) and never
# reads the clock.


def _prefixed(q, prefix):
    """A copy of q with prefix in front of every lookup:
    Q(is_archived=True) → Q(pet__is_archived=True)."""
    children = [
        (
            _prefixed(child, prefix)
            if isinstance(child, Q)
            else (prefix + child[0], child[1])
        )
        for child in q.children
    ]
    return Q(*children, _connector=q.connector, _negated=q.negated)


def active_pet_filter(prefix="pet__"):
    """Not ARCHIVED_PET (pets/utils.py) through prefix: the pet and its
    owner are both active."""
    return ~_prefixed(ARCHIVED_PET, prefix)


def _later_doses(pet, vaccine_name, custom_vaccine_name, date_administered):
    return Vaccination.objects.filter(
        pet=pet,
        vaccine_name=vaccine_name,
        custom_vaccine_name=custom_vaccine_name,
        date_administered__gt=date_administered,
    )


def later_dose_exists():
    """Exists: the same pet got the same vaccine (and the same typed name)
    on a later date, so this dose is superseded."""
    return Exists(
        _later_doses(
            OuterRef("pet"),
            OuterRef("vaccine_name"),
            OuterRef("custom_vaccine_name"),
            OuterRef("date_administered"),
        )
    )


def is_superseded(vaccination):
    """later_dose_exists for one dose."""
    return _later_doses(
        vaccination.pet_id,
        vaccination.vaccine_name,
        vaccination.custom_vaccine_name,
        vaccination.date_administered,
    ).exists()


def _open_doses():
    """Doses that can be due: not superseded, pet and owner active.
    Corrected doses count like any other."""
    return Vaccination.objects.filter(active_pet_filter(), ~later_dose_exists())


def vaccinations_due(today, days=DUE_DAYS):
    """next_due_date from today to today + days (both included)."""
    return _open_doses().filter(
        next_due_date__range=(today, today + timedelta(days=days))
    )


def vaccinations_overdue(today):
    """next_due_date before today."""
    return _open_doses().filter(next_due_date__lt=today)


def followups_due(today, days=DUE_DAYS):
    """Required follow-ups dated today to today + days, pet and owner active."""
    return MedicalRecord.objects.filter(
        active_pet_filter(),
        follow_up_required=True,
        follow_up_date__range=(today, today + timedelta(days=days)),
    )


def vaccination_reminder_refusal(vaccination):
    """Why the Send reminder button can't remind about this dose, or None.
    (Archived pets and owners are refused before this.)"""
    if vaccination.next_due_date is None:
        return (
            "This vaccination has no next dose date, so there is nothing "
            "to remind about."
        )
    if is_superseded(vaccination):
        return (
            f"A later dose of {vaccination.display_vaccine_name} was already "
            "given, so no reminder is needed."
        )
    return None


# ── Admin lists (Vaccinations, Medical records) ──────────────────────────────


def _list_search(params):
    """?q=, or the old ?search= when q is missing, stripped."""
    q = params.get("q")
    if q is None:
        q = params.get("search", "")
    return q.strip()


def _list_page(rows, params, active_tab, q):
    """(page_obj, paging links); the pager keeps the tab and the search."""
    page_obj = Paginator(rows, LIST_PAGE_SIZE).get_page(params.get("page"))
    page_query = urlencode({"tab": active_tab, "q": q} if q else {"tab": active_tab})
    return page_obj, paging_links(page_obj, page_query)


def _vaccine_label_values(word):
    """The listed vaccines whose label contains word ("rabies" →
    ["Anti-rabies"]). Other is left out: its label is the typed name."""
    return [
        value
        for value, label in Vaccination.VACCINE_CHOICES
        if value != Vaccination.OTHER and word.casefold() in label.casefold()
    ]


def search_vaccinations(vaccinations, q):
    """Every word in q must match the pet's name, the owner's first or last
    name, the vaccine's label or the typed vaccine name."""
    for word in q.split():
        vaccinations = vaccinations.filter(
            Q(pet__name__icontains=word)
            | Q(pet__owner__first_name__icontains=word)
            | Q(pet__owner__last_name__icontains=word)
            | Q(vaccine_name__in=_vaccine_label_values(word))
            | Q(custom_vaccine_name__icontains=word)
        )
    return vaccinations


def vaccination_due_state(vaccination, today):
    """
    ("due" | "overdue" | "", days) by the due rule: days until the next
    dose, or days overdue; None when the state is "". Needs the
    is_superseded annotation and pet__owner selected.
    """
    due = vaccination.next_due_date
    # Same rule as vaccinations_due / vaccinations_overdue, on the loaded row
    if (
        due is None
        or vaccination.is_superseded
        or pet_state(vaccination.pet) != "active"
    ):
        return "", None
    if due < today:
        return "overdue", (today - due).days
    if due <= today + timedelta(days=DUE_DAYS):
        return "due", (due - today).days
    return "", None


def vaccination_list_context(params, today):
    """
    Context for the admin Vaccinations list from the request's GET params:
    ?tab=due|overdue|all (anything else → due), ?q= (the old ?search= when
    q is missing) and ?page=. The search stays inside the tab; the tab
    counts ignore it.
    """
    tab = params.get("tab")
    active_tab = tab if tab in VACCINATION_TABS else "due"
    q = _list_search(params)

    # One query: the due and overdue counts reuse the due rule as subqueries
    counts = Vaccination.objects.aggregate(
        due_count=Count("pk", filter=Q(pk__in=vaccinations_due(today).values("pk"))),
        overdue_count=Count(
            "pk", filter=Q(pk__in=vaccinations_overdue(today).values("pk"))
        ),
        all_count=Count("pk"),
    )

    if active_tab == "due":
        rows = vaccinations_due(today).order_by("next_due_date", "pk")
    elif active_tab == "overdue":
        rows = vaccinations_overdue(today).order_by("-next_due_date", "-pk")
    else:
        rows = Vaccination.objects.order_by("-date_administered", "-pk")
    rows = search_vaccinations(rows, q).select_related(
        "pet", "pet__owner", "pet__owner__user"
    )
    rows = rows.annotate(is_superseded=later_dose_exists())

    page_obj, links = _list_page(rows, params, active_tab, q)
    page_obj.object_list = list(page_obj.object_list)
    for vaccination in page_obj.object_list:
        vaccination.due_state, vaccination.days = vaccination_due_state(
            vaccination, today
        )
    return {
        "page_obj": page_obj,
        "vaccinations": page_obj.object_list,
        "q": q,
        "is_searching": bool(q),
        "active_tab": active_tab,
        **counts,
        **links,
        "today": today,
        # Old template's keys (until the 5b-1 redesign)
        "search_query": q,
        "total_count": page_obj.paginator.count,
    }


def search_records(records, q):
    """Every word in q must match the pet's name, the owner's first or last
    name or the diagnosis. private_notes is never searched."""
    for word in q.split():
        records = records.filter(
            Q(pet__name__icontains=word)
            | Q(pet__owner__first_name__icontains=word)
            | Q(pet__owner__last_name__icontains=word)
            | Q(diagnosis__icontains=word)
        )
    return records


def record_list_context(params, today):
    """
    Context for the admin Medical records list from the request's GET
    params: ?tab=followups|all (anything else → all), ?q= (the old ?search=
    when q is missing) and ?page=. The search stays inside the tab; the
    tab counts ignore it.
    """
    tab = params.get("tab")
    active_tab = tab if tab in RECORD_TABS else "all"
    q = _list_search(params)

    counts = MedicalRecord.objects.aggregate(
        followups_count=Count("pk", filter=Q(pk__in=followups_due(today).values("pk"))),
        all_count=Count("pk"),
    )

    if active_tab == "followups":
        rows = followups_due(today).order_by("follow_up_date", "pk")
    else:
        rows = MedicalRecord.objects.order_by("-record_date", "-created_at", "-pk")
    rows = search_records(rows, q).select_related(
        "pet", "pet__owner", "pet__owner__user"
    )

    page_obj, links = _list_page(rows, params, active_tab, q)
    page_obj.object_list = list(page_obj.object_list)
    for record in page_obj.object_list:
        # "Follow-up in N days" on the pill
        record.follow_up_days = (
            (record.follow_up_date - today).days
            if record.follow_up_required and record.follow_up_date
            else None
        )
    return {
        "page_obj": page_obj,
        "records": page_obj.object_list,
        "q": q,
        "is_searching": bool(q),
        "active_tab": active_tab,
        **counts,
        **links,
        "today": today,
        # Old template's keys (until the 5b-1 redesign)
        "search_query": q,
        "total_count": page_obj.paginator.count,
    }


def correction_line(reason, today):
    """One dated correction line: 'Tue, Nov 3, 2026: wrong date typed'.
    The reason is kept on one line."""
    return f"{date_format(today, 'D, M j, Y')}: {' '.join(reason.split())}"


def save_vaccination_edit(form, today):
    """
    Save a valid VaccinationEditForm.
    A clinical change marks the vaccination corrected and adds the reason
    to correction_note as a new dated line. Earlier lines are kept exactly;
    correction_note is never cleared. Without a clinical change the
    correction fields are untouched (a reason typed anyway is ignored).
    """
    vaccination = form.save(commit=False)
    if form.changed_clinical:
        line = correction_line(form.cleaned_data["correction_reason"], today)
        vaccination.is_corrected = True
        if vaccination.correction_note:
            vaccination.correction_note = f"{vaccination.correction_note}\n{line}"
        else:
            vaccination.correction_note = line
    vaccination.save()
    return vaccination


# ── Record from a visit ──────────────────────────────────────────────────────


def visit_for_record(value):
    """The appointment named by ?appointment= (or the POST field), or None
    when it's empty. Anything that isn't a whole number is a 404."""
    if not value:
        return None
    try:
        pk = int(value)
    except (TypeError, ValueError):
        raise Http404("No such visit.")
    return get_object_or_404(Appointment, pk=pk)


def existing_visit_record(appointment):
    """The record already written for this visit, or None."""
    return MedicalRecord.objects.filter(appointment=appointment).first()


def short_date(value, today):
    """'Mon, Nov 9', with the year when it isn't this year."""
    if value.year == today.year:
        return date_format(value, "D, M j")
    return date_format(value, "D, M j, Y")


def visit_stop(pet, appointment, today):
    """
    Why a new record can't be started for this visit, as
    (message, url name, pk) for the redirect, or None.
    The visit must be this pet's, confirmed or completed, and not after
    today (Manila date); a visit that already has a record goes to that
    record.
    """
    if appointment.pet_id != pet.pk:
        return (
            "That visit belongs to another pet.",
            "admin_appointment_detail",
            appointment.pk,
        )
    if appointment.status not in RECORD_VISIT_STATUSES:
        return (
            "A record can only be added to a confirmed or completed visit.",
            "admin_appointment_detail",
            appointment.pk,
        )
    if appointment.date > today:
        return (
            "This visit hasn't happened yet. Write the record on or after "
            f"{short_date(appointment.date, today)}.",
            "admin_appointment_detail",
            appointment.pk,
        )
    existing = existing_visit_record(appointment)
    if existing:
        return (RECORD_EXISTS, "admin_medical_record_detail", existing.pk)
    return None


def save_new_record(form, pet, appointment, user):
    """
    Save a valid MedicalRecordForm for the pet (and visit, if any).
    Returns (record, True), or (the visit's existing record, False) when
    another save for the same visit got there first (the OneToOne on
    appointment raises IntegrityError; atomic keeps the request usable).
    """
    record = form.save(commit=False)
    record.pet = pet
    record.appointment = appointment
    record.created_by = user
    try:
        with transaction.atomic():
            record.save()
    except IntegrityError:
        if appointment is None:
            raise
        return MedicalRecord.objects.get(appointment=appointment), False
    return record, True


def notify_record_created(record):
    """Tell the owner a medical record was written for their pet."""
    pet = record.pet
    notify(
        recipient=pet.owner.user,
        notification_type="appointment_approved",
        title="Medical Record Created",
        message=(
            f"A medical record has been created for {pet.name} "
            f"on {record.record_date.strftime('%B %d, %Y')}. "
            f"You can view the details in your pet's medical history."
        ),
        related_appointment=record.appointment,
        related_pet=pet,
        email_subject="Medical Record Created — Hapi Vet",
    )


# ── Medicines ────────────────────────────────────────────────────────────────


def can_remove_medicines(record, today):
    """Medicines can be removed only on the day the record was written
    (the Manila date of created_at). After that the list is kept."""
    return timezone.localtime(record.created_at).date() == today
