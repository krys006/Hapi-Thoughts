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
    VaccinationCorrectionForm,
    VaccinationEditForm,
)

from notifications.utils import notify

from django.db.models import Q

from notifications.models import Notification

# ── Admin — Medical Records ───────────────────────────────────────────────────


@login_required
def admin_medical_record_create(request, pet_pk):
    """
    Admin view — create a medical record for a pet.
    Can be linked to an appointment via ?appointment=<pk> query param.
    One record per appointment — enforced here.
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    from pets.models import Pet
    from appointments.models import Appointment

    pet = get_object_or_404(Pet, pk=pet_pk)

    # Check if linking to an appointment
    appointment = None
    appointment_pk = request.GET.get("appointment") or request.POST.get("appointment")
    if appointment_pk:
        appointment = get_object_or_404(Appointment, pk=appointment_pk)

        # Enforce one record per appointment
        if hasattr(appointment, "medical_record"):
            messages.error(
                request,
                "A medical record already exists for this appointment.",
            )
            return redirect(
                "admin_medical_record_detail",
                pk=appointment.medical_record.pk,
            )

    if request.method == "POST":
        form = MedicalRecordForm(request.POST)
        if form.is_valid():
            record = form.save(commit=False)
            record.pet = pet
            record.appointment = appointment
            record.created_by = request.user
            record.save()

            # Notify pet owner that a medical record has been created
            notify(
                recipient=pet.owner.user,
                notification_type="appointment_approved",
                title="Medical Record Created",
                message=(
                    f"A medical record has been created for {pet.name} "
                    f"on {record.record_date.strftime('%B %d, %Y')}. "
                    f"You can view the details in your pet's medical history."
                ),
                related_appointment=appointment,
                related_pet=pet,
                email_subject="Medical Record Created — Hapi Vet",
            )

            messages.success(request, "Medical record created.")
            return redirect("admin_medical_record_detail", pk=record.pk)
        else:
            messages.error(request, "Please correct the errors below.")
    else:
        # Pre-fill record date with today
        initial = {"record_date": timezone.now().date()}
        # If linked to appointment, pre-fill date with appointment date
        if appointment:
            initial["record_date"] = appointment.date
        form = MedicalRecordForm(initial=initial)

    return render(
        request,
        "admin/medical/record_form.html",
        {
            "form": form,
            "pet": pet,
            "appointment": appointment,
        },
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

    record = get_object_or_404(MedicalRecord, pk=record_pk)

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
    Admin view — delete a prescription item.
    Prescription items are part of a medical record but are individually
    removable (unlike the record itself which is immutable).
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    item = get_object_or_404(PrescriptionItem, pk=pk)
    record_pk = item.record.pk

    if request.method == "POST":
        item.delete()
        messages.success(request, "Prescription item removed.")

    return redirect("admin_medical_record_detail", pk=record_pk)


@login_required
def admin_test_result_file_upload(request, record_pk):
    """Admin view — upload a test result file to a medical record."""
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    record = get_object_or_404(MedicalRecord, pk=record_pk)

    if request.method == "POST":
        form = TestResultFileForm(request.POST, request.FILES)
        if form.is_valid():
            file_obj = form.save(commit=False)
            file_obj.record = record
            file_obj.save()
            messages.success(request, "File uploaded.")
        else:
            messages.error(request, "Please correct the errors.")

    return redirect("admin_medical_record_detail", pk=record_pk)


@login_required
def admin_test_result_file_archive(request, pk):
    """
    Admin view — soft delete (archive) a test result file.
    Used when a file was uploaded by mistake.
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    file_obj = get_object_or_404(TestResultFile, pk=pk)
    record_pk = file_obj.record.pk

    if request.method == "POST":
        file_obj.archive()
        messages.success(request, "File removed.")

    return redirect("admin_medical_record_detail", pk=record_pk)


@login_required
def admin_vaccination_add(request, record_pk):
    """
    Admin view — add a vaccination record linked to a medical record.
    The pet is derived from the medical record.
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    record = get_object_or_404(MedicalRecord, pk=record_pk)

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

    pet = get_object_or_404(Pet, pk=pet_pk)

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
        form = VaccinationForm(initial={"date_administered": timezone.now().date()})

    return render(
        request,
        "admin/medical/vaccination_form.html",
        {
            "form": form,
            "pet": pet,
        },
    )


@login_required
def admin_vaccination_correct(request, pk):
    """
    Admin view — mark a vaccination record as corrected.
    Does not alter the original data — only flags it and adds a note.
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    vaccination = get_object_or_404(Vaccination, pk=pk)

    if request.method == "POST":
        form = VaccinationCorrectionForm(request.POST, instance=vaccination)
        if form.is_valid():
            vaccination = form.save(commit=False)
            vaccination.is_corrected = True
            vaccination.save()
            messages.success(request, "Vaccination record marked as corrected.")
        else:
            messages.error(request, "Please provide a correction note.")

    # Redirect back to wherever we came from
    if vaccination.medical_record:
        return redirect("admin_medical_record_detail", pk=vaccination.medical_record.pk)
    return redirect("admin_pet_vaccination_history", pk=vaccination.pet.pk)


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
    Admin view — edit a vaccination record (Option C).
    Overwrites fields in place.
    Sets is_corrected = True automatically if any clinical field changed.
    Correction note required when clinical fields are changed.
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
            updated = form.save(commit=False)

            # Auto-flag as corrected if any clinical field changed
            if form.clinical_changed:
                updated.is_corrected = True

            updated.save()
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

    vaccination = get_object_or_404(Vaccination, pk=pk)

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

    record = get_object_or_404(MedicalRecord, pk=pk)

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
    Read-only. Public notes only — private_notes never sent to template.
    """
    if request.user.role != "pet_owner":
        return redirect("admin_dashboard")

    from pets.models import Pet

    owner = request.user.petowner
    pet = get_object_or_404(Pet, pk=pk, owner=owner, is_archived=False)

    records = MedicalRecord.objects.filter(pet=pet).order_by("-record_date")

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
