from django.db import models
from django.utils import timezone


class MedicalRecord(models.Model):
    """
    Clinical documentation for each pet visit.
    Immutable — no deletion allowed. Ever.
    Admin creates and edits. Pet owners see all fields except private_notes.
    """

    # ── Relationships ────────────────────────────────────────────────────────
    pet = models.ForeignKey(
        "pets.Pet",
        on_delete=models.CASCADE,
        related_name="medical_records",
    )
    appointment = models.OneToOneField(
        "appointments.Appointment",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="medical_record",
    )
    created_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        related_name="created_medical_records",
    )

    # ── Core fields ──────────────────────────────────────────────────────────
    record_date = models.DateField()
    diagnosis = models.TextField(blank=True)
    symptoms = models.TextField(blank=True)
    treatment_given = models.TextField(blank=True)

    # ── Notes ────────────────────────────────────────────────────────────────
    public_notes = models.TextField(blank=True)
    private_notes = models.TextField(blank=True)

    # ── Follow-up ────────────────────────────────────────────────────────────
    follow_up_required = models.BooleanField(default=False)
    follow_up_date = models.DateField(null=True, blank=True)

    # ── Timestamps ───────────────────────────────────────────────────────────
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-record_date", "-created_at"]

    def __str__(self):
        return f"{self.pet.name} — {self.record_date}"


class PrescriptionItem(models.Model):
    """
    Individual medicine items within a medical record.
    Multiple items allowed per record.
    Immutable — follows the parent MedicalRecord.
    """

    # ── Relationship ─────────────────────────────────────────────────────────
    record = models.ForeignKey(
        MedicalRecord,
        on_delete=models.CASCADE,
        related_name="prescription_items",
    )

    # ── Core fields ──────────────────────────────────────────────────────────
    medicine_name = models.CharField(max_length=100)
    dosage = models.CharField(max_length=100, blank=True)
    frequency = models.CharField(max_length=100, blank=True)
    duration = models.CharField(max_length=100, blank=True)
    notes = models.TextField(blank=True)

    # ── Timestamp ────────────────────────────────────────────────────────────
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.medicine_name} — {self.record}"


class TestResultFile(models.Model):
    """
    File uploads attached to a medical record.
    Soft delete only — wrong uploads can be hidden but never deleted.
    """

    # ── Relationship ─────────────────────────────────────────────────────────
    record = models.ForeignKey(
        MedicalRecord,
        on_delete=models.CASCADE,
        related_name="test_result_files",
    )

    # ── Core fields ──────────────────────────────────────────────────────────
    file = models.FileField(upload_to="test_results/")
    description = models.CharField(max_length=100, blank=True)

    # ── Flags ────────────────────────────────────────────────────────────────
    is_archived = models.BooleanField(default=False)
    archived_at = models.DateTimeField(null=True, blank=True)

    # ── Timestamp ────────────────────────────────────────────────────────────
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"File for {self.record} — {self.description or 'No description'}"

    def archive(self):
        """Soft delete — hide this file from views without deleting it."""
        self.is_archived = True
        self.archived_at = timezone.now()
        self.save()


class Vaccination(models.Model):
    """
    Vaccination records for each pet.
    Can be created through an appointment or as a standalone entry.
    Immutable — use is_corrected + correction_note if wrong.
    """

    # ── Predefined vaccine choices ───────────────────────────────────────────
    DEWORMING = "Deworming"
    IVERMECTIN = "Ivermectin"
    ANTI_RABIES = "Anti-rabies"
    SIX_IN_ONE = "6-in-1"
    SEVEN_IN_ONE = "7-in-1"
    EIGHT_IN_ONE = "8-in-1"
    BORDETELLA = "Bordetella"
    OTHER = "Other"
    VACCINE_CHOICES = [
        (DEWORMING, "Deworming"),
        (IVERMECTIN, "Ivermectin"),
        (ANTI_RABIES, "Anti-rabies"),
        (SIX_IN_ONE, "6-in-1"),
        (SEVEN_IN_ONE, "7-in-1"),
        (EIGHT_IN_ONE, "8-in-1"),
        (BORDETELLA, "Bordetella"),
        (OTHER, "Other (specify below)"),
    ]

    # ── Relationships ────────────────────────────────────────────────────────
    pet = models.ForeignKey(
        "pets.Pet",
        on_delete=models.CASCADE,
        related_name="vaccinations",
    )
    medical_record = models.ForeignKey(
        MedicalRecord,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="vaccinations",
    )
    appointment = models.ForeignKey(
        "appointments.Appointment",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="vaccinations",
    )

    # ── Core fields ──────────────────────────────────────────────────────────
    vaccine_name = models.CharField(max_length=20, choices=VACCINE_CHOICES)
    # Custom name used when vaccine_name == OTHER
    custom_vaccine_name = models.CharField(max_length=100, blank=True)
    date_administered = models.DateField()
    weight_at_vaccination = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        null=True,
        blank=True,
    )
    next_due_date = models.DateField(null=True, blank=True)
    batch_number = models.CharField(max_length=50, blank=True)
    manufacturer = models.CharField(max_length=100, blank=True)
    administered_by = models.CharField(max_length=100, blank=True)
    site_of_injection = models.CharField(max_length=50, blank=True)

    # ── Correction flags ─────────────────────────────────────────────────────
    is_corrected = models.BooleanField(default=False)
    correction_note = models.TextField(blank=True)

    # ── Timestamps ───────────────────────────────────────────────────────────
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-date_administered"]

    def __str__(self):
        name = (
            self.custom_vaccine_name
            if self.vaccine_name == self.OTHER
            else self.vaccine_name
        )
        return f"{name} — {self.pet.name} ({self.date_administered})"

    @property
    def display_vaccine_name(self):
        """Returns custom name if Other, otherwise the predefined name."""
        if self.vaccine_name == self.OTHER and self.custom_vaccine_name:
            return self.custom_vaccine_name
        return self.get_vaccine_name_display()
