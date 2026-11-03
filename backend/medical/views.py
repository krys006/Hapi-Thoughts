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
    notify_record_created,
    save_new_record,
    save_vaccination_edit,
    visit_for_record,
    visit_stop,
)

from notifications.utils import notify

from django.db.models import Prefetch, Q

from notifications.models import Notification

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


@login_required
def admin_medical_record_detail(request, pk):
    """
    Admin view — view a medical record with all related data.
    Also handles adding prescription items and uploading test result files.
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    record = get_object_or_404(
        MedicalRecord.objects.select_related("pet", "appointment", "created_by"),
        pk=pk,
    )

    prescription_items = record.prescription_items.all()
    test_files = record.test_result_files.filter(is_archived=False)
    vaccinations = record.vaccinations.all()

    prescription_form = PrescriptionItemForm()
    file_form = TestResultFileForm()
    vaccination_form = VaccinationForm()

    last_followup_reminder = (
        Notification.objects.filter(
            recipient=record.pet.owner.user,
            notification_type=Notification.FOLLOWUP_REMINDER,
            related_pet=record.pet,
        )
        .order_by("-created_at")
        .first()
    )

    return render(
        request,
        "admin/medical/record_detail.html",
        {
            "record": record,
            "prescription_items": prescription_items,
            "test_files": test_files,
            "vaccinations": vaccinations,
            "prescription_form": prescription_form,
            "file_form": file_form,
            "vaccination_form": vaccination_form,
            "last_followup_reminder": last_followup_reminder,
        },
    )


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
    """Admin view — add a prescription item to a medical record."""
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
        form = PrescriptionItemForm(request.POST)
        if form.is_valid():
            item = form.save(commit=False)
            item.record = record
            item.save()
            messages.success(request, "Prescription item added.")
        else:
            messages.error(request, "Please correct the errors.")

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
    """Admin view — upload a test result file (PDF, JPG or PNG, up to
    10 MB) to a medical record."""
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
        form = TestResultFileForm(request.POST, request.FILES)
        if form.is_valid():
            file_obj = form.save(commit=False)
            file_obj.record = record
            file_obj.save()
            messages.success(request, "File uploaded.")
        else:
            # The page isn't re-rendered yet (5a-5), so flash the real errors
            errors = [error for field in form.errors.values() for error in field]
            messages.error(request, " ".join(errors))

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
    The pet is derived from the medical record.
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
            vaccination = form.save(commit=False)
            vaccination.pet = record.pet
            vaccination.medical_record = record
            vaccination.appointment = record.appointment
            vaccination.save()
            messages.success(request, "Vaccination record added.")
        else:
            messages.error(request, "Please correct the errors.")

    return redirect("admin_medical_record_detail", pk=record_pk)


@login_required
def admin_vaccination_standalone_create(request, pet_pk):
    """
    Admin view — create a standalone vaccination record (no appointment needed).
    Accessed from the pet detail page.
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    from pets.models import Pet

    pet = get_object_or_404(Pet.objects.select_related("owner"), pk=pet_pk)
    refusal = archived_pet_refusal(pet)
    if refusal:
        messages.error(request, refusal)
        return redirect("admin_pet_detail", pk=pet.pk)

    if request.method == "POST":
        form = VaccinationForm(request.POST)
        if form.is_valid():
            vaccination = form.save(commit=False)
            vaccination.pet = pet
            vaccination.save()
            messages.success(request, "Vaccination record added.")
            return redirect("admin_pet_vaccination_history", pk=pet_pk)
        else:
            messages.error(request, "Please correct the errors below.")
    else:
        form = VaccinationForm(initial={"date_administered": timezone.localdate()})

    return render(
        request,
        "admin/medical/vaccination_form.html",
        {
            "form": form,
            "pet": pet,
        },
    )


@login_required
def admin_pet_vaccination_history(request, pk):
    """
    Admin view — view all vaccination records for a specific pet.
    Accessible from the pet detail page.
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    from pets.models import Pet

    pet = get_object_or_404(Pet, pk=pk)
    vaccinations = Vaccination.objects.filter(pet=pet).order_by("-date_administered")

    last_reminder = (
        Notification.objects.filter(
            recipient=pet.owner.user,
            notification_type=Notification.VACCINATION_REMINDER,
            related_pet=pet,
        )
        .order_by("-created_at")
        .first()
    )

    return render(
        request,
        "admin/medical/vaccination_history.html",
        {
            "pet": pet,
            "vaccinations": vaccinations,
            "last_reminder": last_reminder,
        },
    )


@login_required
def admin_pet_medical_history(request, pk):
    """
    Admin view — view all medical records for a specific pet.
    Accessible from the pet detail page.
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    from pets.models import Pet

    pet = get_object_or_404(Pet, pk=pk)
    records = MedicalRecord.objects.filter(pet=pet).order_by("-record_date")

    return render(
        request,
        "admin/medical/pet_history.html",
        {
            "pet": pet,
            "records": records,
        },
    )


@login_required
def admin_medical_record_list(request):
    """
    Admin view — list all medical records across all pets.
    Searchable by pet name and pet owner name.
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    search_query = request.GET.get("search", "").strip()

    records = MedicalRecord.objects.select_related("pet", "pet__owner").order_by(
        "-record_date", "-created_at"
    )

    if search_query:
        records = records.filter(
            Q(pet__name__icontains=search_query)
            | Q(pet__owner__first_name__icontains=search_query)
            | Q(pet__owner__last_name__icontains=search_query)
        )

    return render(
        request,
        "admin/medical/record_list.html",
        {
            "records": records,
            "search_query": search_query,
            "total_count": records.count(),
        },
    )


@login_required
def admin_vaccination_list(request):
    """
    Admin view — list all vaccination records across all pets.
    Searchable by pet name and pet owner name.
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    search_query = request.GET.get("search", "").strip()

    vaccinations = Vaccination.objects.select_related("pet", "pet__owner").order_by(
        "-date_administered"
    )

    if search_query:
        vaccinations = vaccinations.filter(
            Q(pet__name__icontains=search_query)
            | Q(pet__owner__first_name__icontains=search_query)
            | Q(pet__owner__last_name__icontains=search_query)
        )

    return render(
        request,
        "admin/medical/vaccination_list.html",
        {
            "vaccinations": vaccinations,
            "search_query": search_query,
            "total_count": vaccinations.count(),
        },
    )


@login_required
def admin_vaccination_edit(request, pk):
    """
    Admin view — edit a vaccination record in place.
    A clinical change needs a reason; the save marks the record corrected
    and adds a dated line to correction_note (save_vaccination_edit).
    Redirects back to where the admin came from via ?next= param.
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
            return redirect("admin_pet_vaccination_history", pk=vaccination.pet.pk)
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
    history page.
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    vaccination = get_object_or_404(
        Vaccination.objects.select_related("pet__owner"), pk=pk
    )
    refusal = archived_pet_refusal(vaccination.pet)
    if refusal:
        messages.error(request, refusal)
        return redirect("admin_pet_detail", pk=vaccination.pet.pk)

    if request.method == "POST":
        owner_user = vaccination.pet.owner.user
        vaccine_display = vaccination.display_vaccine_name

        if vaccination.next_due_date:
            message = (
                f"This is a reminder that {vaccination.pet.name}'s "
                f"{vaccine_display} vaccination is due on "
                f"{vaccination.next_due_date.strftime('%B %d, %Y')}. "
                f"Please contact the clinic to schedule an appointment."
            )
        else:
            message = (
                f"This is a reminder regarding {vaccination.pet.name}'s "
                f"{vaccine_display} vaccination. "
                f"Please contact the clinic for more information."
            )

        notify(
            recipient=owner_user,
            notification_type=Notification.VACCINATION_REMINDER,
            title=f"Vaccination Reminder — {vaccination.pet.name}",
            message=message,
            related_pet=vaccination.pet,
            email_subject="Vaccination Reminder — Hapi Vet",
        )
        messages.success(
            request,
            f"Reminder sent to {vaccination.pet.owner.full_name}.",
        )

    return redirect("admin_pet_vaccination_history", pk=vaccination.pet.pk)


@login_required
def admin_trigger_followup_reminder(request, pk):
    """
    Admin action — manually send a follow-up reminder to the pet owner.
    Bypasses the duplicate-prevention check used by the automated cron job.
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    record = get_object_or_404(
        MedicalRecord.objects.select_related("pet__owner"), pk=pk
    )
    refusal = archived_pet_refusal(record.pet)
    if refusal:
        messages.error(request, refusal)
        return redirect("admin_pet_detail", pk=record.pet.pk)

    if request.method == "POST":
        if not record.follow_up_required:
            messages.error(request, "This record does not have a follow-up scheduled.")
            return redirect("admin_medical_record_detail", pk=pk)

        owner_user = record.pet.owner.user

        if record.follow_up_date:
            message = (
                f"This is a reminder that {record.pet.name} has a follow-up "
                f"visit scheduled for "
                f"{record.follow_up_date.strftime('%B %d, %Y')}. "
                f"Please contact the clinic to confirm your appointment."
            )
        else:
            message = (
                f"This is a reminder that {record.pet.name} has a follow-up "
                f"visit recommended. Please contact the clinic to schedule."
            )

        notify(
            recipient=owner_user,
            notification_type=Notification.FOLLOWUP_REMINDER,
            title=f"Follow-up Reminder — {record.pet.name}",
            message=message,
            related_appointment=record.appointment,
            related_pet=record.pet,
            email_subject="Follow-up Reminder — Hapi Vet",
        )
        messages.success(
            request,
            f"Follow-up reminder sent to {record.pet.owner.full_name}.",
        )

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
