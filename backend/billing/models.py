from decimal import ROUND_HALF_UP, Decimal

from django.db import models
from django.utils import timezone

# Money is rounded to the centavo
CENT = Decimal("0.01")


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
    Editable while PENDING. Locked once PAID or CANCELLED.
    Status changes only via the mark-paid / mark-cancelled actions.
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

    def calculate_totals(self):
        """
        Recalculates subtotal and total from billing items.
        Call this after adding or removing billing items, or after a
        discount change. Saves only the totals (update_fields), so a stale
        copy of the receipt can never write an old payment_status back.
        """
        items = self.billing_items.all()
        subtotal = sum((item.subtotal for item in items), Decimal("0"))
        self.subtotal = subtotal.quantize(CENT, rounding=ROUND_HALF_UP)
        self.total_amount = self.total_for(self.subtotal)
        self.save(update_fields=["subtotal", "total_amount", "updated_at"])

    def total_for(self, subtotal):
        """The total for this subtotal with this receipt's discount (the
        same rules as calculate_totals: percentage worked out again, peso
        amount capped, rounded half up), never below 0. Also used for
        "the total if this item is removed"."""
        subtotal = Decimal(subtotal or 0).quantize(CENT, rounding=ROUND_HALF_UP)
        return max(subtotal - self._discount_for(subtotal), Decimal("0.00"))

    def _discount_for(self, subtotal):
        """The peso discount on this subtotal: a percentage of it or the
        peso amount, capped at the subtotal (removing an item later can
        shrink it), rounded to 2 decimals with ROUND_HALF_UP."""
        subtotal = Decimal(subtotal or 0)
        value = Decimal(self.discount_value or 0)
        if value <= 0 or subtotal <= 0:
            return Decimal("0.00")
        if self.discount_type == self.PERCENTAGE:
            raw = subtotal * value / Decimal("100")
        elif self.discount_type == self.FIXED_DISCOUNT:
            raw = value
        else:
            return Decimal("0.00")
        return min(raw, subtotal).quantize(CENT, rounding=ROUND_HALF_UP)

    @property
    def is_locked(self):
        """Receipt is locked (read-only) once PAID or CANCELLED."""
        return self.payment_status in (self.PAID, self.CANCELLED)

    @property
    def discount_amount(self):
        """The peso amount taken off, the same capped and rounded value
        calculate_totals() uses, so subtotal - discount = total on the
        page and on the printout."""
        return self._discount_for(self.subtotal)

    @property
    def has_discount(self):
        """True when a discount actually takes something off."""
        return self.discount_amount > 0

    @classmethod
    def generate_receipt_number(cls):
        """
        Next receipt number for this Manila year: HV-YYYY-0001 … HV-YYYY-9999,
        then HV-YYYY-10000. The largest number after the prefix is found
        numerically (a string sort would put 9999 after 10000); numbers
        whose suffix isn't a whole number are ignored.
        """
        prefix = f"HV-{timezone.localdate().year}-"
        numbers = cls.objects.filter(receipt_number__startswith=prefix).values_list(
            "receipt_number", flat=True
        )
        highest = 0
        for number in numbers:
            suffix = number[len(prefix) :]
            if suffix.isascii() and suffix.isdigit():
                highest = max(highest, int(suffix))
        return f"{prefix}{highest + 1:04d}"


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
