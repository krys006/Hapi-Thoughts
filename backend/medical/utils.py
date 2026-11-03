"""Helpers for the medical views."""

from django.db import IntegrityError, transaction
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateformat import format as date_format

from appointments.models import Appointment
from notifications.utils import notify

from .models import MedicalRecord

RECORD_VISIT_STATUSES = (Appointment.CONFIRMED, Appointment.COMPLETED)
RECORD_EXISTS = "A medical record already exists for this appointment."


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
