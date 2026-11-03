"""Helpers for the medical views."""

from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef, Q
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateformat import format as date_format

from appointments.models import Appointment
from notifications.utils import notify
from pets.utils import ARCHIVED_PET

from .models import MedicalRecord, Vaccination

RECORD_VISIT_STATUSES = (Appointment.CONFIRMED, Appointment.COMPLETED)
RECORD_EXISTS = "A medical record already exists for this appointment."
# "This week" on the dashboard and the Due / Follow-ups tabs: today to today + 7
DUE_DAYS = 7


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
