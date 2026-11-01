# pets/models.py

from django.db import models
from django.utils import timezone


class PetOwner(models.Model):
    # --- Relationship ---
    user = models.OneToOneField(
        "accounts.User",
        on_delete=models.CASCADE,
        related_name="petowner",
    )

    # --- Core fields ---
    first_name = models.CharField(max_length=50)
    last_name = models.CharField(max_length=50)
    contact_number = models.CharField(max_length=20, blank=True)
    address_line = models.CharField(max_length=255, blank=True)
    barangay = models.CharField(max_length=100, blank=True)
    city_municipality = models.CharField(max_length=100, blank=True)
    province = models.CharField(max_length=100, blank=True)
    profile_photo = models.ImageField(upload_to="owners/", blank=True, null=True)

    # --- Dates ---
    date_registered = models.DateField(auto_now_add=True)

    # --- Flags ---
    is_archived = models.BooleanField(default=False)
    archived_at = models.DateTimeField(null=True, blank=True)

    # --- Timestamps ---
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["last_name", "first_name"]

    def __str__(self):
        return f"{self.first_name} {self.last_name}"

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}"

    @property
    def full_address(self):
        """Assembles a formatted address string from structured fields."""
        parts = [
            self.address_line,
            self.barangay,
            self.city_municipality,
            self.province,
        ]
        # Filter out empty parts and join with comma
        return ", ".join(part for part in parts if part.strip())

    def archive(self):
        """Soft-delete this owner and deactivate their user account."""
        self.is_archived = True
        self.archived_at = timezone.now()
        self.save()
        self.user.is_active = False
        self.user.save()

    def restore(self):
        """Restore an archived owner and reactivate their user account."""
        self.is_archived = False
        self.archived_at = None
        self.save()
        self.user.is_active = True
        self.user.save()


class ContactLink(models.Model):
    # --- Relationship ---
    owner = models.ForeignKey(
        PetOwner,
        on_delete=models.CASCADE,
        related_name="contact_links",
    )

    # --- Core fields ---
    platform = models.CharField(max_length=50)  # e.g. Facebook, Instagram
    url_or_handle = models.CharField(max_length=255)

    # --- Timestamps ---
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.platform}: {self.url_or_handle}"


class Pet(models.Model):
    # --- Gender choices ---
    MALE = "male"
    FEMALE = "female"
    UNKNOWN = "unknown"
    GENDER_CHOICES = [
        (MALE, "Male"),
        (FEMALE, "Female"),
        (UNKNOWN, "Unknown"),
    ]

    # --- Relationship ---
    owner = models.ForeignKey(
        PetOwner,
        on_delete=models.CASCADE,
        related_name="pets",
    )

    # --- Core fields ---
    name = models.CharField(max_length=50)
    species = models.CharField(max_length=50)
    breed = models.CharField(max_length=50, blank=True)
    color = models.CharField(max_length=50, blank=True)
    gender = models.CharField(max_length=10, choices=GENDER_CHOICES, default=UNKNOWN)
    date_of_birth = models.DateField(null=True, blank=True)
    weight = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    registration_number = models.CharField(max_length=100, blank=True)
    photo = models.ImageField(upload_to="pets/", blank=True, null=True)

    # --- Flags ---
    is_archived = models.BooleanField(default=False)
    archived_at = models.DateTimeField(null=True, blank=True)

    # --- Timestamps ---
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.owner.full_name})"

    @property
    def age(self):
        """Calculate age dynamically. Returns a human-readable string."""
        if not self.date_of_birth:
            return "Unknown"
        today = timezone.localdate()
        delta = today - self.date_of_birth
        years = delta.days // 365
        months = (delta.days % 365) // 30
        if years > 0:
            return f"{years} yr{'s' if years != 1 else ''}"
        if months > 0:
            return f"{months} mo{'s' if months != 1 else ''}"
        return f"{delta.days} day{'s' if delta.days != 1 else ''}"


class PetDeletionRequest(models.Model):
    # --- Reason choices ---
    PASSED_AWAY = "passed_away"
    REHOMED = "rehomed"
    DUPLICATE = "duplicate"
    OTHER = "other"
    REASON_CHOICES = [
        (PASSED_AWAY, "Passed Away"),
        (REHOMED, "Rehomed"),
        (DUPLICATE, "Duplicate Entry"),
        (OTHER, "Other"),
    ]

    # --- Status choices ---
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    STATUS_CHOICES = [
        (PENDING, "Pending"),
        (APPROVED, "Approved"),
        (REJECTED, "Rejected"),
    ]

    # --- Relationships ---
    pet = models.ForeignKey(
        Pet,
        on_delete=models.CASCADE,
        related_name="deletion_requests",
    )
    requested_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.CASCADE,
        related_name="pet_deletion_requests",
    )
    reviewed_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_deletion_requests",
    )

    # --- Core fields ---
    reason = models.CharField(max_length=50, choices=REASON_CHOICES)
    reason_detail = models.TextField(blank=True)  # Used when reason = OTHER
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=PENDING)

    # --- Dates ---
    reviewed_at = models.DateTimeField(null=True, blank=True)

    # --- Timestamps ---
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Deletion request for {self.pet.name} — {self.status}"
