"""Helpers for the medical views."""

import os
from datetime import timedelta
from urllib.parse import urlencode

from django.contrib import messages
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Count, Exists, Max, OuterRef, Q
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.utils.dateformat import format as date_format

from appointments.models import Appointment, ClinicSettings
from appointments.utils import paging_links
from notifications.models import Notification
from notifications.utils import EMAIL_NO_ADDRESS, EMAIL_OFF, EMAIL_SENT, notify
from pets.models import Pet
from pets.utils import ARCHIVED_PET, pet_state

from .models import MedicalRecord, Vaccination

RECORD_VISIT_STATUSES = (Appointment.CONFIRMED, Appointment.COMPLETED)
RECORD_EXISTS = "A medical record already exists for this appointment."
# "This week" on the dashboard and the Due / Follow-ups tabs: today to today + 7
DUE_DAYS = 7
LIST_PAGE_SIZE = 25
VACCINATION_TABS = ("due", "overdue", "all")
VACCINATION_DEFAULT_TAB = "due"
RECORD_TABS = ("followups", "all")
RECORD_DEFAULT_TAB = "all"


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


# ── Reminders ────────────────────────────────────────────────────────────────
# The "last reminder" line is per pet and kind: a Notification is not linked
# to its Vaccination or MedicalRecord (a schema change, see HANDOFF).


def last_reminders(kind, pet_ids):
    """{pet id: created_at of the newest Notification of kind
    (VACCINATION_REMINDER / FOLLOWUP_REMINDER) about that pet}, in one
    grouped query. Pets never reminded are missing."""
    rows = (
        Notification.objects.filter(notification_type=kind, related_pet_id__in=pet_ids)
        .values("related_pet")
        .annotate(last=Max("created_at"))
        .order_by()
    )
    return {row["related_pet"]: row["last"] for row in rows}


def last_reminder(pet, kind):
    """last_reminders for one pet: a datetime, or None."""
    return last_reminders(kind, [pet.pk]).get(pet.pk)


def message_date(value):
    """The date in a notification message: 'Thursday, November 5, 2026'."""
    return date_format(value, "l, F j, Y")


def reminder_flash(owner, notification):
    """(message level, text) saying how the reminder reached the owner,
    from what notify() did (email_outcome)."""
    name = owner.full_name
    outcome = notification.email_outcome
    if outcome == EMAIL_SENT:
        return messages.SUCCESS, f"Reminder sent to {name} (app and email)."
    if outcome == EMAIL_NO_ADDRESS:
        return (
            messages.SUCCESS,
            f"Reminder sent to {name} in the app only (no email on file).",
        )
    if outcome == EMAIL_OFF:
        return (
            messages.SUCCESS,
            f"Reminder sent to {name} in the app only "
            "(their email reminders are off).",
        )
    return (
        messages.WARNING,
        f"Reminder sent to {name} in the app. The email couldn't be sent.",
    )


def send_vaccination_reminder(vaccination):
    """notify() the owner that the next dose is due; reminder_flash's
    (level, text). Needs a next_due_date (vaccination_reminder_refusal)."""
    pet = vaccination.pet
    notification = notify(
        recipient=pet.owner.user,
        notification_type=Notification.VACCINATION_REMINDER,
        title=f"Vaccination Reminder — {pet.name}",
        message=(
            f"This is a reminder that {pet.name}'s "
            f"{vaccination.display_vaccine_name} vaccination is due on "
            f"{message_date(vaccination.next_due_date)}. "
            f"Please contact the clinic to schedule an appointment."
        ),
        related_pet=pet,
        email_subject="Vaccination Reminder — Hapi Vet",
    )
    return reminder_flash(pet.owner, notification)


def send_followup_reminder(record):
    """notify() the owner about the record's follow-up visit;
    reminder_flash's (level, text)."""
    pet = record.pet
    if record.follow_up_date:
        message = (
            f"This is a reminder that {pet.name} has a follow-up visit "
            f"scheduled for {message_date(record.follow_up_date)}. "
            f"Please contact the clinic to confirm your appointment."
        )
    else:
        message = (
            f"This is a reminder that {pet.name} has a follow-up visit "
            f"recommended. Please contact the clinic to schedule."
        )
    notification = notify(
        recipient=pet.owner.user,
        notification_type=Notification.FOLLOWUP_REMINDER,
        title=f"Follow-up Reminder — {pet.name}",
        message=message,
        related_appointment=record.appointment,
        related_pet=pet,
        email_subject="Follow-up Reminder — Hapi Vet",
    )
    return reminder_flash(pet.owner, notification)


def _list_return_url(url_name, tabs, default_tab, post):
    """A list's URL from reverse(), with the posted tab (re-validated
    against that list's own tabs) and the stripped q, urlencoded."""
    tab = post.get("tab")
    query = {"tab": tab if tab in tabs else default_tab}
    q = post.get("q", "").strip()
    if q:
        query["q"] = q
    return f"{reverse(url_name)}?{urlencode(query)}"


def vaccination_reminder_return(post, vaccination):
    """
    Where the vaccination Send reminder goes back to, from the POST field
    return_to (a fixed choice, never a URL from the request): "list" → the
    Vaccinations list with its tab and q; anything else ("history", the
    default) → the pet's vaccinations.
    """
    if post.get("return_to") == "list":
        return _list_return_url(
            "admin_vaccination_list", VACCINATION_TABS, VACCINATION_DEFAULT_TAB, post
        )
    return reverse("admin_pet_vaccination_history", args=[vaccination.pet_id])


def followup_reminder_return(post, record):
    """
    Where the follow-up Send reminder goes back to (return_to, a fixed
    choice): "list" → the Medical records list with its tab and q;
    "record" → the record; anything else ("history", the default) → the
    pet's medical history.
    """
    return_to = post.get("return_to")
    if return_to == "list":
        return _list_return_url(
            "admin_medical_record_list", RECORD_TABS, RECORD_DEFAULT_TAB, post
        )
    if return_to == "record":
        return reverse("admin_medical_record_detail", args=[record.pk])
    return reverse("admin_pet_medical_history", args=[record.pet_id])


def _owner_contact(pet):
    """owner_has_email / owner_phone for the reminder dialogs."""
    return {
        "owner_has_email": bool(pet.owner.user.email),
        "owner_phone": pet.owner.contact_number,
    }


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
    active_tab = tab if tab in VACCINATION_TABS else VACCINATION_DEFAULT_TAB
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
    reminders = last_reminders(
        Notification.VACCINATION_REMINDER,
        {vaccination.pet_id for vaccination in page_obj.object_list},
    )
    for vaccination in page_obj.object_list:
        vaccination.due_state, vaccination.days = vaccination_due_state(
            vaccination, today
        )
        vaccination.last_reminder = reminders.get(vaccination.pet_id)
    return {
        "page_obj": page_obj,
        "vaccinations": page_obj.object_list,
        "q": q,
        "is_searching": bool(q),
        "active_tab": active_tab,
        **counts,
        **links,
        "today": today,
        # Last day of the Due tab ("Today to {due_until}")
        "due_until": today + timedelta(days=DUE_DAYS),
        # Old template's keys (5a-4); the 5b-1 template doesn't use them
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


def _follow_up_days(record, today):
    """Days until the follow-up ("Follow-up in N days" on the pill; negative
    when past), or None when none is required or it has no date."""
    if record.follow_up_required and record.follow_up_date:
        return (record.follow_up_date - today).days
    return None


def record_list_context(params, today):
    """
    Context for the admin Medical records list from the request's GET
    params: ?tab=followups|all (anything else → all), ?q= (the old ?search=
    when q is missing) and ?page=. The search stays inside the tab; the
    tab counts ignore it.
    """
    tab = params.get("tab")
    active_tab = tab if tab in RECORD_TABS else RECORD_DEFAULT_TAB
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
    reminders = last_reminders(
        Notification.FOLLOWUP_REMINDER,
        {record.pet_id for record in page_obj.object_list},
    )
    for record in page_obj.object_list:
        record.follow_up_days = _follow_up_days(record, today)
        record.last_reminder = reminders.get(record.pet_id)
    return {
        "page_obj": page_obj,
        "records": page_obj.object_list,
        "q": q,
        "is_searching": bool(q),
        "active_tab": active_tab,
        **counts,
        **links,
        "today": today,
        # Last day of the Follow-ups tab ("Today to {due_until}")
        "due_until": today + timedelta(days=DUE_DAYS),
        # Old template's keys (5a-4); the 5b-1 template doesn't use them
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
            f"on {message_date(record.record_date)}. "
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


# ── Record page ──────────────────────────────────────────────────────────────


def _file_rows(files):
    """The files as a list, each with title (the description, else the file's
    name) and file_type ("PDF", "JPG" or "PNG", from the extension)."""
    rows = list(files)
    for row in rows:
        name = os.path.basename(row.file.name)
        extension = os.path.splitext(name)[1].lstrip(".").upper()
        row.title = row.description or name
        row.file_type = "JPG" if extension == "JPEG" else extension
    return rows


# Written and saved within this much counts as never edited (created_at and
# updated_at are set a few microseconds apart on the first save)
EDIT_GRACE = timedelta(minutes=1)


def record_last_changed(record):
    """updated_at when the record was edited after it was written, else None."""
    if record.updated_at - record.created_at > EDIT_GRACE:
        return record.updated_at
    return None


def record_detail_context(record, today):
    """
    Context for the admin record page (5b-2). Needs pet__owner__user
    selected. Old keys kept: prescription_items, test_files (the same
    objects as medicines / active_files).
    """
    pet = record.pet
    state = pet_state(pet)
    medicines = record.prescription_items.order_by("created_at", "pk")
    active_files = _file_rows(
        record.test_result_files.filter(is_archived=False).order_by("uploaded_at", "pk")
    )
    follow_up = None
    if record.follow_up_required:
        follow_up = {
            "date": record.follow_up_date,
            "days": _follow_up_days(record, today),
        }
    return {
        "record": record,
        "pet_state": state,
        "can_add": state == "active",
        "can_remove_medicines": can_remove_medicines(record, today),
        "medicines": medicines,
        "active_files": active_files,
        "removed_files": _file_rows(
            record.test_result_files.filter(is_archived=True).order_by(
                "-archived_at", "-pk"
            )
        ),
        "vaccinations": record.vaccinations.order_by("-date_administered", "-pk"),
        "last_changed": record_last_changed(record),
        "follow_up": follow_up,
        "last_followup_reminder": last_reminder(pet, Notification.FOLLOWUP_REMINDER),
        **_owner_contact(pet),
        "today": today,
        "prescription_items": medicines,
        "test_files": active_files,
    }


# ── New vaccination ──────────────────────────────────────────────────────────


def record_for_vaccination(value, pet):
    """The record named by ?record= (or the POST field), or None when it's
    empty. It must be this pet's; anything else is a 404."""
    if not value:
        return None
    try:
        pk = int(value)
    except (TypeError, ValueError):
        raise Http404("No such record.")
    return get_object_or_404(
        MedicalRecord.objects.select_related("appointment"), pk=pk, pet=pet
    )


def clinic_vet_name():
    """ClinicSettings.veterinarian_name, or "" when there are no settings."""
    clinic = ClinicSettings.objects.first()
    return clinic.veterinarian_name if clinic else ""


def new_vaccination_initial(record, today):
    """A new vaccination starts on the record's date (else today), given by
    the clinic's vet. Never used on edit."""
    return {
        "date_administered": record.record_date if record else today,
        "administered_by": clinic_vet_name(),
    }


def save_new_vaccination(form, pet, record=None):
    """Save a valid VaccinationForm for the pet, linked to the record and
    its visit when there is one."""
    vaccination = form.save(commit=False)
    vaccination.pet = pet
    if record:
        vaccination.medical_record = record
        vaccination.appointment = record.appointment
    vaccination.save()
    return vaccination


def vaccination_history_url(vaccination):
    """The pet's vaccinations with this dose highlighted and scrolled to."""
    url = reverse("admin_pet_vaccination_history", args=[vaccination.pet_id])
    return f"{url}?highlight={vaccination.pk}#vaccination-{vaccination.pk}"


# ── Pet history pages ────────────────────────────────────────────────────────


def _history_header(pet):
    """The shared header of both history pages (one query for the counts)."""
    state = pet_state(pet)
    counts = Pet.objects.filter(pk=pet.pk).aggregate(
        records_count=Count("medical_records", distinct=True),
        vaccinations_count=Count("vaccinations", distinct=True),
    )
    return {"pet": pet, "pet_state": state, "can_add": state == "active", **counts}


def medical_history_context(pet, today):
    """Context for the pet's medical history (5b-5). Needs owner selected."""
    records = (
        MedicalRecord.objects.filter(pet=pet)
        .annotate(
            medicine_count=Count("prescription_items", distinct=True),
            active_file_count=Count(
                "test_result_files",
                filter=Q(test_result_files__is_archived=False),
                distinct=True,
            ),
            vaccination_count=Count("vaccinations", distinct=True),
        )
        .order_by("-record_date", "-created_at", "-pk")
    )
    return {
        **_history_header(pet),
        "records": records,
        "next_follow_up": followups_due(today)
        .filter(pet=pet)
        .order_by("follow_up_date", "pk")
        .first(),
        "last_followup_reminder": last_reminder(pet, Notification.FOLLOWUP_REMINDER),
        "today": today,
    }


def _highlight_id(value, vaccinations):
    """The pk from ?highlight= when it is one of these doses, else None."""
    try:
        pk = int(value)
    except (TypeError, ValueError):
        return None
    return pk if any(v.pk == pk for v in vaccinations) else None


def vaccination_history_context(pet, today, highlight=None):
    """
    Context for the pet's vaccinations (5b-5). Needs owner and owner.user
    selected. Each dose gets is_superseded, due_state, days and can_remind
    (due or overdue by the due rule, so it has a next date, isn't
    superseded and the pet and owner are active).
    """
    vaccinations = list(
        Vaccination.objects.filter(pet=pet)
        .annotate(is_superseded=later_dose_exists())
        .order_by("-date_administered", "-pk")
    )
    for vaccination in vaccinations:
        vaccination.pet = pet
        vaccination.due_state, vaccination.days = vaccination_due_state(
            vaccination, today
        )
        vaccination.can_remind = vaccination.due_state in ("due", "overdue")
    return {
        **_history_header(pet),
        "vaccinations": vaccinations,
        "highlight_id": _highlight_id(highlight, vaccinations),
        "last_vaccination_reminder": last_reminder(
            pet, Notification.VACCINATION_REMINDER
        ),
        **_owner_contact(pet),
        "today": today,
    }
