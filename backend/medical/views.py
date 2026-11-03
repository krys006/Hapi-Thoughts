from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.utils import timezone

from .models import MedicalRecord, PrescriptionItem, TestResultFile, Vaccination
from .forms import (
    MedicalRecordForm,
    PrescriptionItemForm,
    TestResultFileForm,
    VaccinationForm,
    VaccinationEditForm,
)
from .utils import (
    RECORD_EXISTS,
    can_remove_medicines,
    medical_history_context,
    new_vaccination_initial,
    notify_record_created,
    record_detail_context,
    record_for_vaccination,
    record_list_context,
    save_new_record,
    save_new_vaccination,
    save_vaccination_edit,
    send_followup_reminder,
    send_vaccination_reminder,
    vaccination_history_context,
    vaccination_history_url,
    vaccination_list_context,
    vaccination_reminder_refusal,
    visit_for_record,
    visit_stop,
)

from django.db.models import Prefetch

from pets.utils import archived_pet_refusal

# ── Admin — Medical Records ───────────────────────────────────────────────────


@login_required
def admin_medical_record_create(request, pet_pk):
    """
    Admin view — create a medical record for a pet.
    Can be linked to a visit via ?appointment=<pk> (or the POST field): the
    visit must be this pet's, confirmed or completed and not in the future;
    a visit with a record goes to that record (visit_stop, save_new_record).
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    from pets.models import Pet

    pet = get_object_or_404(Pet.objects.select_related("owner"), pk=pet_pk)
    refusal = archived_pet_refusal(pet)
    if refusal:
        messages.error(request, refusal)
        return redirect("admin_pet_detail", pk=pet.pk)

    appointment = visit_for_record(
        request.GET.get("appointment") or request.POST.get("appointment")
    )
    today = timezone.localdate()
    stop = visit_stop(pet, appointment, today) if appointment else None
    if stop:
        message, url_name, pk = stop
        messages.error(request, message)
        return redirect(url_name, pk=pk)

    if request.method == "POST":
        form = MedicalRecordForm(request.POST)
        if form.is_valid():
            record, created = save_new_record(form, pet, appointment, request.user)
            if created:
                notify_record_created(record)
                messages.success(request, "Medical record created.")
            else:
                messages.error(request, RECORD_EXISTS)
            return redirect("admin_medical_record_detail", pk=record.pk)
        messages.error(request, "Please correct the errors below.")
    else:
        record_date = appointment.date if appointment else today
        form = MedicalRecordForm(initial={"record_date": record_date})

    return render(
        request,
        "admin/medical/record_form.html",
        {"form": form, "pet": pet, "appointment": appointment},
    )


RECORD_PAGE_RELATED = ("pet__owner__user", "appointment", "created_by")


def _render_record_detail(
    request, record, prescription_form=None, file_form=None, open_dialog=None
):
    """
    The record page. Shared by admin_medical_record_detail and the add
    medicine / add file views, which pass their bound form and the dialog
    to reopen ("add-medicine" / "add-file") when it has errors.
    """
    return render(
        request,
        "admin/medical/record_detail.html",
        {
            **record_detail_context(record, timezone.localdate()),
            "prescription_form": prescription_form or PrescriptionItemForm(),
            "file_form": file_form or TestResultFileForm(),
            # The old inline vaccination form (removed in 5b-2)
            "vaccination_form": VaccinationForm(),
            "open_dialog": open_dialog,
        },
    )


@login_required
def admin_medical_record_detail(request, pk):
    """
    Admin view — a medical record with its medicines, files, vaccinations
    and follow-up (record_detail_context).
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    record = get_object_or_404(
        MedicalRecord.objects.select_related(*RECORD_PAGE_RELATED), pk=pk
    )
    return _render_record_detail(request, record)


@login_required
def admin_medical_record_edit(request, pk):
    """
    Admin view — edit an existing medical record.
    Note: records are editable but never deletable.
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    record = get_object_or_404(MedicalRecord, pk=pk)

    if request.method == "POST":
        form = MedicalRecordForm(request.POST, instance=record)
        if form.is_valid():
            form.save()
            messages.success(request, "Medical record updated.")
            return redirect("admin_medical_record_detail", pk=pk)
        else:
            messages.error(request, "Please correct the errors below.")
    else:
        form = MedicalRecordForm(instance=record)

    return render(
        request,
        "admin/medical/record_form.html",
        {
            "form": form,
            "pet": record.pet,
            "appointment": record.appointment,
            "record": record,
            "is_edit": True,
        },
    )


@login_required
def admin_prescription_item_add(request, record_pk):
    """
    Admin view — add a prescription item to a medical record (POST).
    Errors re-render the record page with the Add medicine dialog open.
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    record = get_object_or_404(
        MedicalRecord.objects.select_related(*RECORD_PAGE_RELATED), pk=record_pk
    )
    refusal = archived_pet_refusal(record.pet)
    if refusal:
        messages.error(request, refusal)
        return redirect("admin_pet_detail", pk=record.pet.pk)

    if request.method == "POST":
        form = PrescriptionItemForm(request.POST)
        if not form.is_valid():
            return _render_record_detail(
                request, record, prescription_form=form, open_dialog="add-medicine"
            )
        item = form.save(commit=False)
        item.record = record
        item.save()
        messages.success(request, "Prescription item added.")

    return redirect("admin_medical_record_detail", pk=record_pk)


@login_required
def admin_prescription_item_delete(request, pk):
    """
    Admin view — delete a prescription item (POST only).
    Allowed only on the day the record was written (Manila date of
    created_at), to fix a same-day mistake; after that the list is kept.
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    item = get_object_or_404(
        PrescriptionItem.objects.select_related("record__pet__owner"), pk=pk
    )
    record = item.record
    refusal = archived_pet_refusal(record.pet)
    if refusal:
        messages.error(request, refusal)
        return redirect("admin_pet_detail", pk=record.pet.pk)

    if request.method == "POST":
        if can_remove_medicines(record, timezone.localdate()):
            item.delete()
            messages.success(request, "Prescription item removed.")
        else:
            messages.error(
                request,
                "Medicines can only be removed on the day the record was written.",
            )

    return redirect("admin_medical_record_detail", pk=record.pk)


@login_required
def admin_test_result_file_upload(request, record_pk):
    """
    Admin view — upload a test result file (PDF, JPG or PNG, up to 10 MB)
    to a medical record (POST). Errors re-render the record page with the
    Add file dialog open.
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    record = get_object_or_404(
        MedicalRecord.objects.select_related(*RECORD_PAGE_RELATED), pk=record_pk
    )
    refusal = archived_pet_refusal(record.pet)
    if refusal:
        messages.error(request, refusal)
        return redirect("admin_pet_detail", pk=record.pet.pk)

    if request.method == "POST":
        form = TestResultFileForm(request.POST, request.FILES)
        if not form.is_valid():
            return _render_record_detail(
                request, record, file_form=form, open_dialog="add-file"
            )
        file_obj = form.save(commit=False)
        file_obj.record = record
        file_obj.save()
        messages.success(request, "File uploaded.")

    return redirect("admin_medical_record_detail", pk=record_pk)


@login_required
def admin_test_result_file_archive(request, pk):
    """
    Admin view — soft delete (archive) a test result file (POST only).
    Used when a file was uploaded by mistake. A file already archived is
    left as it is (archived_at kept).
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    file_obj = get_object_or_404(
        TestResultFile.objects.select_related("record__pet__owner"), pk=pk
    )
    pet = file_obj.record.pet
    refusal = archived_pet_refusal(pet)
    if refusal:
        messages.error(request, refusal)
        return redirect("admin_pet_detail", pk=pet.pk)

    if request.method == "POST":
        if file_obj.is_archived:
            messages.info(request, "That file was already removed.")
        else:
            file_obj.archive()
            messages.success(
                request, f"File removed. It's hidden from {pet.owner.full_name}."
            )

    return redirect("admin_medical_record_detail", pk=file_obj.record.pk)


@login_required
def admin_vaccination_add(request, record_pk):
    """
    Admin view — add a vaccination record linked to a medical record.
    The pet is derived from the medical record. Only the old record page's
    inline form posts here; 5b-2 removes both (the new page links to
    admin_vaccination_standalone_create?record=).
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    record = get_object_or_404(
        MedicalRecord.objects.select_related("pet__owner"), pk=record_pk
    )
    refusal = archived_pet_refusal(record.pet)
    if refusal:
        messages.error(request, refusal)
        return redirect("admin_pet_detail", pk=record.pet.pk)

    if request.method == "POST":
        form = VaccinationForm(request.POST)
        if form.is_valid():
            save_new_vaccination(form, record.pet, record)
            messages.success(request, "Vaccination record added.")
        else:
            messages.error(request, "Please correct the errors.")

    return redirect("admin_medical_record_detail", pk=record_pk)


@login_required
def admin_vaccination_standalone_create(request, pet_pk):
    """
    Admin view — add a vaccination from the pet page, or for a visit record
    with ?record=<pk> (or the POST field; the record must be this pet's,
    else 404). Given by starts with the clinic's vet and the date given
    with the record's date (else today). Saved: back to the record, or to
    the pet's vaccinations with the new dose highlighted.
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    from pets.models import Pet

    pet = get_object_or_404(Pet.objects.select_related("owner"), pk=pet_pk)
    refusal = archived_pet_refusal(pet)
    if refusal:
        messages.error(request, refusal)
        return redirect("admin_pet_detail", pk=pet.pk)
    record = record_for_vaccination(
        request.GET.get("record") or request.POST.get("record"), pet
    )

    if request.method == "POST":
        form = VaccinationForm(request.POST)
        if form.is_valid():
            vaccination = save_new_vaccination(form, pet, record)
            messages.success(request, "Vaccination record added.")
            if record:
                return redirect("admin_medical_record_detail", pk=record.pk)
            return redirect(vaccination_history_url(vaccination))
        messages.error(request, "Please correct the errors below.")
    else:
        initial = new_vaccination_initial(record, timezone.localdate())
        form = VaccinationForm(initial=initial)

    return render(
        request,
        "admin/medical/vaccination_form.html",
        {"form": form, "pet": pet, "record": record},
    )


@login_required
def admin_pet_vaccination_history(request, pk):
    """
    Admin view — every vaccination of a pet, newest first, with its due
    state and Send reminder rule (vaccination_history_context).
    ?highlight=<pk> marks the dose the admin opened; only this pet's
    doses count, anything else is ignored.
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    from pets.models import Pet

    pet = get_object_or_404(Pet.objects.select_related("owner__user"), pk=pk)
    context = vaccination_history_context(
        pet, timezone.localdate(), request.GET.get("highlight")
    )
    # Old template's key (until the 5b-5 redesign)
    context["last_reminder"] = context["last_vaccination_reminder"]
    return render(request, "admin/medical/vaccination_history.html", context)


@login_required
def admin_pet_medical_history(request, pk):
    """
    Admin view — every medical record of a pet, newest first, with counts
    and the next follow-up (medical_history_context).
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    from pets.models import Pet

    pet = get_object_or_404(Pet.objects.select_related("owner__user"), pk=pk)
    context = medical_history_context(pet, timezone.localdate())
    return render(request, "admin/medical/pet_history.html", context)


@login_required
def admin_medical_record_list(request):
    """
    Admin view — every medical record, tabs Follow-ups in 7 days / All
    (default), searchable, 25 per page (record_list_context).
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    context = record_list_context(request.GET, timezone.localdate())
    return render(request, "admin/medical/record_list.html", context)


@login_required
def admin_vaccination_list(request):
    """
    Admin view — every vaccination, tabs Due in 7 days (default) / Overdue /
    All, searchable, 25 per page (vaccination_list_context).
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    context = vaccination_list_context(request.GET, timezone.localdate())
    return render(request, "admin/medical/vaccination_list.html", context)


@login_required
def admin_vaccination_edit(request, pk):
    """
    Admin view — edit a vaccination record in place.
    A clinical change needs a reason; the save marks the record corrected
    and adds a dated line to correction_note (save_vaccination_edit).
    Redirects back to where the admin came from via ?next= param (the
    history opens with this dose highlighted).
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    vaccination = get_object_or_404(Vaccination, pk=pk)

    # Determine redirect target after save
    # ?next=record redirects to the linked medical record
    # ?next=history (default) redirects to pet vaccination history
    next_param = request.GET.get("next") or request.POST.get("next", "history")

    if request.method == "POST":
        form = VaccinationEditForm(request.POST, instance=vaccination)
        if form.is_valid():
            save_vaccination_edit(form, timezone.localdate())
            messages.success(request, "Vaccination record updated.")

            if next_param == "record" and vaccination.medical_record:
                return redirect(
                    "admin_medical_record_detail",
                    pk=vaccination.medical_record.pk,
                )
            return redirect(vaccination_history_url(vaccination))
        else:
            messages.error(request, "Please correct the errors below.")
    else:
        form = VaccinationEditForm(instance=vaccination)

    return render(
        request,
        "admin/medical/vaccination_edit.html",
        {
            "form": form,
            "vaccination": vaccination,
            "pet": vaccination.pet,
            "next": next_param,
        },
    )


@login_required
def admin_trigger_vaccination_reminder(request, pk):
    """
    Admin action — manually send a vaccination reminder to the pet owner.
    Bypasses the duplicate-prevention check used by the automated cron job,
    since this is an intentional resend. Called from the vaccination
    history page. A dose with no next dose date or with a later dose of the
    same vaccine is refused (vaccination_reminder_refusal).
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    vaccination = get_object_or_404(
        Vaccination.objects.select_related("pet__owner__user"), pk=pk
    )
    refusal = archived_pet_refusal(vaccination.pet)
    if refusal:
        messages.error(request, refusal)
        return redirect("admin_pet_detail", pk=vaccination.pet.pk)

    if request.method == "POST":
        refusal = vaccination_reminder_refusal(vaccination)
        if refusal:
            messages.error(request, refusal)
            return redirect("admin_pet_vaccination_history", pk=vaccination.pet.pk)

        # The flash says how it reached the owner (app, email, or why not)
        level, text = send_vaccination_reminder(vaccination)
        messages.add_message(request, level, text)

    return redirect("admin_pet_vaccination_history", pk=vaccination.pet.pk)


@login_required
def admin_trigger_followup_reminder(request, pk):
    """
    Admin action — manually send a follow-up reminder to the pet owner.
    Bypasses the duplicate-prevention check used by the automated cron job.
    The flash says how it reached the owner (send_followup_reminder).
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    record = get_object_or_404(
        MedicalRecord.objects.select_related("pet__owner__user", "appointment"),
        pk=pk,
    )
    refusal = archived_pet_refusal(record.pet)
    if refusal:
        messages.error(request, refusal)
        return redirect("admin_pet_detail", pk=record.pet.pk)

    if request.method == "POST":
        if record.follow_up_required:
            level, text = send_followup_reminder(record)
            messages.add_message(request, level, text)
        else:
            messages.error(request, "This record does not have a follow-up scheduled.")

    return redirect("admin_medical_record_detail", pk=pk)


# ── Pet Owner — Medical Records ───────────────────────────────────────────────


@login_required
def owner_pet_medical_history(request, pk):
    """
    Pet Owner view — view their pet's medical history.
    Read-only. The template never shows private_notes, and archived test
    files never reach it (active_test_files).
    """
    if request.user.role != "pet_owner":
        return redirect("admin_dashboard")

    from pets.models import Pet

    owner = request.user.petowner
    pet = get_object_or_404(Pet, pk=pk, owner=owner, is_archived=False)

    records = (
        MedicalRecord.objects.filter(pet=pet)
        .order_by("-record_date")
        .prefetch_related(
            "prescription_items",
            Prefetch(
                "test_result_files",
                queryset=TestResultFile.objects.filter(is_archived=False),
                to_attr="active_test_files",
            ),
        )
    )

    return render(
        request,
        "owner/medical/history.html",
        {
            "pet": pet,
            "records": records,
        },
    )


@login_required
def owner_vaccination_history(request, pk):
    """
    Pet Owner view — view their pet's vaccination history.
    Read-only.
    """
    if request.user.role != "pet_owner":
        return redirect("admin_dashboard")

    from pets.models import Pet

    owner = request.user.petowner
    pet = get_object_or_404(Pet, pk=pk, owner=owner, is_archived=False)

    vaccinations = Vaccination.objects.filter(pet=pet).order_by("-date_administered")

    return render(
        request,
        "owner/medical/vaccination_history.html",
        {
            "pet": pet,
            "vaccinations": vaccinations,
        },
    )
