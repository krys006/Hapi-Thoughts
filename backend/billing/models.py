from django.db import models
from django.utils import timezone


class Service(models.Model):
    """
    Clinic service catalog managed by Admin.
    """

    # ── Status choices ───────────────────────────────────────────────────────
    DRAFT = "draft"
    ACTIVE = "active"
    UNLISTED = "unlisted"
    CLOSED = "closed"
    STATUS_CHOICES = [
        (DRAFT, "Draft"),
        (ACTIVE, "Active"),
        (UNLISTED, "Unlisted"),
        (CLOSED, "Closed"),
    ]

    # ── Pricing type choices ─────────────────────────────────────────────────
    FIXED = "fixed"
    RANGE = "range"
    PRICING_CHOICES = [
        (FIXED, "Fixed"),
        (RANGE, "Range"),
    ]

    # ── Core fields ──────────────────────────────────────────────────────────
    service_name = models.CharField(max_length=100)
    category = models.CharField(max_length=50, blank=True)
    description = models.TextField(blank=True)
    pricing_type = models.CharField(
        max_length=10,
        choices=PRICING_CHOICES,
        default=FIXED,
    )
    base_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
    )
    min_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
    )
    max_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=DRAFT,
    )

    # ── Timestamps ───────────────────────────────────────────────────────────
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["service_name"]

    def __str__(self):
        return self.service_name

    @property
    def display_price(self):
        """Returns a human-readable price string for display."""
        if self.pricing_type == self.FIXED:
            return f"₱{self.base_price:,.2f}"
        if self.pricing_type == self.RANGE:
            return f"₱{self.min_price:,.2f} – ₱{self.max_price:,.2f}"
        return "—"


class BillingReceipt(models.Model):
    """
    Header record for each billing transaction.
    Editable while PENDING. Locked once marked as PAID.
    """

    # ── Payment status choices ───────────────────────────────────────────────
    PENDING = "pending"
    PAID = "paid"
    CANCELLED = "cancelled"
    PAYMENT_STATUS_CHOICES = [
        (PENDING, "Pending"),
        (PAID, "Paid"),
        (CANCELLED, "Cancelled"),
    ]

    # ── Discount type choices ────────────────────────────────────────────────
    PERCENTAGE = "percentage"
    FIXED_DISCOUNT = "fixed"
    DISCOUNT_CHOICES = [
        (PERCENTAGE, "Percentage (%)"),
        (FIXED_DISCOUNT, "Fixed Amount (₱)"),
    ]

    # ── Relationships ────────────────────────────────────────────────────────
    appointment = models.OneToOneField(
        "appointments.Appointment",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="billing_receipt",
    )
    owner = models.ForeignKey(
        "pets.PetOwner",
        on_delete=models.SET_NULL,
        null=True,
        related_name="billing_receipts",
    )
    pet = models.ForeignKey(
        "pets.Pet",
        on_delete=models.SET_NULL,
        null=True,
        related_name="billing_receipts",
    )

    # ── Core fields ──────────────────────────────────────────────────────────
    receipt_number = models.CharField(max_length=50, unique=True)
    billing_date = models.DateField(default=timezone.now)

    # ── Totals ───────────────────────────────────────────────────────────────
    subtotal = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    discount_type = models.CharField(
        max_length=20,
        choices=DISCOUNT_CHOICES,
        blank=True,
    )
    discount_value = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
    )
    discount_note = models.CharField(max_length=100, blank=True)
    total_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
    )

    # ── Payment ──────────────────────────────────────────────────────────────
    payment_method = models.CharField(max_length=50, blank=True, default="Cash")
    payment_status = models.CharField(
        max_length=20,
        choices=PAYMENT_STATUS_CHOICES,
        default=PENDING,
    )
    payment_date = models.DateField(null=True, blank=True)

    # ── Timestamps ───────────────────────────────────────────────────────────
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-billing_date", "-created_at"]

    def __str__(self):
        return self.receipt_number

    @property
    def is_locked(self):
        """Receipt is locked once payment status is PAID."""
        return self.payment_status == self.PAID

    def calculate_totals(self):
        """
        Recalculates subtotal and total from billing items.
        Call this after adding or removing billing items.
        """
        from decimal import Decimal

        # Sum all line item subtotals
        items = self.billing_items.all()
        self.subtotal = sum(item.subtotal for item in items)

        # Apply discount
        discount = Decimal("0")
        if self.discount_value and self.discount_type:
            if self.discount_type == self.PERCENTAGE:
                discount = self.subtotal * (self.discount_value / Decimal("100"))
            elif self.discount_type == self.FIXED_DISCOUNT:
                discount = self.discount_value

        self.total_amount = max(self.subtotal - discount, Decimal("0"))
        self.save()

    @property
    def is_locked(self):
        """Receipt is locked once payment status is PAID."""
        return self.payment_status == self.PAID

    @property
    def discount_amount(self):
        """Returns the actual peso amount deducted as discount."""
        from decimal import Decimal

        if not self.discount_value or not self.discount_type:
            return Decimal("0")
        if self.discount_type == self.PERCENTAGE:
            return self.subtotal * (self.discount_value / Decimal("100"))
        if self.discount_type == self.FIXED_DISCOUNT:
            return self.discount_value
        return Decimal("0")

    @classmethod
    def generate_receipt_number(cls):
        """
        Generates the next receipt number in format HV-YYYY-XXXX.
        Thread-safe enough for a single-clinic single-admin system.
        """
        from django.utils import timezone

        year = timezone.now().year
        prefix = f"HV-{year}-"

        # Find the highest existing number for this year
        last = (
            cls.objects.filter(receipt_number__startswith=prefix)
            .order_by("-receipt_number")
            .first()
        )

        if last:
            try:
                last_seq = int(last.receipt_number.split("-")[-1])
            except ValueError:
                last_seq = 0
        else:
            last_seq = 0

        return f"{prefix}{str(last_seq + 1).zfill(4)}"


class BillingItem(models.Model):
    """
    Individual line items within a billing receipt.
    Supports both catalog services and manual items.
    """

    # ── Relationships ────────────────────────────────────────────────────────
    receipt = models.ForeignKey(
        BillingReceipt,
        on_delete=models.CASCADE,
        related_name="billing_items",
    )
    service = models.ForeignKey(
        Service,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="billing_items",
    )

    # ── Core fields ──────────────────────────────────────────────────────────
    # Description used for manual items or to override service name
    description = models.CharField(max_length=200)
    quantity = models.IntegerField(default=1)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    subtotal = models.DecimalField(max_digits=10, decimal_places=2)

    # ── Timestamp ────────────────────────────────────────────────────────────
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        # Always recalculate subtotal before saving
        self.subtotal = self.quantity * self.unit_price
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.description} x{self.quantity}"
