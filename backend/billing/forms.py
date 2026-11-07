from decimal import Decimal

from django import forms
from django.db.models import Sum
from django.db.models.functions import Lower

from .models import Service, BillingReceipt


class ServiceForm(forms.ModelForm):
    """
    Admin form — create or edit a service in the catalog.
    """

    class Meta:
        model = Service
        fields = [
            "service_name",
            "category",
            "description",
            "pricing_type",
            "base_price",
            "min_price",
            "max_price",
            "status",
        ]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 3}),
        }

    def clean(self):
        cleaned_data = super().clean()
        pricing_type = cleaned_data.get("pricing_type")
        base_price = cleaned_data.get("base_price")
        min_price = cleaned_data.get("min_price")
        max_price = cleaned_data.get("max_price")

        if pricing_type == Service.FIXED:
            if base_price is None or base_price < 0:
                raise forms.ValidationError(
                    "Please provide a valid base price for fixed pricing."
                )

        if pricing_type == Service.RANGE:
            if min_price is None or max_price is None:
                raise forms.ValidationError(
                    "Please provide both min and max price for range pricing."
                )
            if min_price < 0 or max_price < 0:
                raise forms.ValidationError("Prices cannot be negative.")
            if min_price > max_price:
                raise forms.ValidationError(
                    "Minimum price cannot be greater than maximum price."
                )

        return cleaned_data


# Discount labels live here, not in the model's choices (that would be a
# migration). "" is "No discount".
DISCOUNT_FORM_CHOICES = [
    ("", "No discount"),
    (BillingReceipt.PERCENTAGE, "Percent off (%)"),
    (BillingReceipt.FIXED_DISCOUNT, "Peso amount off (₱)"),
]


def peso(amount):
    """₱1,234.50"""
    return f"₱{amount:,.2f}"


class BillingReceiptForm(forms.ModelForm):
    """
    Admin form — the receipt's discount. Items are added separately via
    BillingItemForm. Billing date (today on create) and payment method
    (always Cash) are set by the system; payment status/date change only
    via the mark-paid / mark-cancelled actions.
    """

    discount_type = forms.ChoiceField(
        label="Discount", choices=DISCOUNT_FORM_CHOICES, required=False
    )
    discount_value = forms.DecimalField(
        label="How much", required=False, max_digits=10, decimal_places=2
    )

    class Meta:
        model = BillingReceipt
        fields = ["discount_type", "discount_value", "discount_note"]
        labels = {"discount_note": "Note"}
        help_texts = {"discount_note": "Printed next to the discount."}
        widgets = {
            "discount_note": forms.TextInput(
                attrs={"placeholder": "For example: Loyal client"}
            ),
        }

    def items_subtotal(self):
        """The receipt's current subtotal: the sum of its items."""
        if not self.instance.pk:
            return Decimal("0.00")
        total = self.instance.billing_items.aggregate(total=Sum("subtotal"))["total"]
        return total or Decimal("0.00")

    def clean(self):
        cleaned_data = super().clean()
        if "discount_value" in self.errors:
            return cleaned_data
        kind = cleaned_data.get("discount_type") or ""
        value = cleaned_data.get("discount_value")
        value = Decimal("0") if value is None else value
        error = self._discount_error(kind, value)
        if error:
            self.add_error("discount_value", error)
        elif not kind:
            # "No discount" clears the amount and the note
            cleaned_data["discount_value"] = Decimal("0")
            cleaned_data["discount_note"] = ""
        else:
            cleaned_data["discount_value"] = value
        return cleaned_data

    def _discount_error(self, kind, value):
        if value < 0:
            return "The amount can't be negative."
        if kind and value == 0:
            return "Type how much."
        if not kind and value > 0:
            return "Choose the kind of discount, or clear the amount."
        if kind == BillingReceipt.PERCENTAGE and value > 100:
            return "A discount can't be more than 100%."
        if kind == BillingReceipt.FIXED_DISCOUNT:
            subtotal = self.items_subtotal()
            if value > subtotal:
                return f"A discount can't be more than the subtotal ({peso(subtotal)})."
        return None


# The "What is it?" value for an item that isn't on the price list
OTHER_ITEM = "other"
PICK_ITEM_MESSAGE = "Choose what it is, or Other (type it)."
QUANTITY_MESSAGE = "The quantity must be from 1 to 999."
ITEM_AMOUNT_LIMIT = Decimal("999999.99")


def billable_services():
    """Services that can go on a receipt: Offered (active) and Clinic use
    only (unlisted), A to Z."""
    return Service.objects.filter(
        status__in=[Service.ACTIVE, Service.UNLISTED]
    ).order_by(Lower("service_name"), "pk")


def service_choice_label(service):
    """ "Consultation · ₱300.00" or "Grooming · ₱300.00 to ₱800.00"."""
    if (
        service.pricing_type == Service.RANGE
        and service.min_price is not None
        and service.max_price is not None
    ):
        price = f"{peso(service.min_price)} to {peso(service.max_price)}"
    else:
        price = peso(service.base_price)
    return f"{service.service_name} · {price}"


class BillingItemForm(forms.Form):
    """
    Admin form — add a billing line item to a receipt.
    "What is it?" lists the billable services and Other (type it). A picked
    service fills a blank description, and a blank price for a Fixed
    service, on the server (works without JS).
    """

    service_choice = forms.ChoiceField(
        label="What is it?",
        error_messages={
            "required": PICK_ITEM_MESSAGE,
            "invalid_choice": PICK_ITEM_MESSAGE,
        },
    )
    description = forms.CharField(
        label="Description",
        max_length=200,
        required=False,
        help_text="Printed on the receipt. You can change it.",
    )
    quantity = forms.IntegerField(
        label="Quantity",
        initial=1,
        required=False,
        error_messages={"invalid": QUANTITY_MESSAGE},
    )
    unit_price = forms.DecimalField(
        label="Unit price", required=False, max_digits=10, decimal_places=2
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Built per form, so a service hidden or closed since is refused
        self.services = {str(s.pk): s for s in billable_services()}
        self.fields["service_choice"].choices = [
            ("", "Choose one"),
            *[(pk, service_choice_label(s)) for pk, s in self.services.items()],
            (OTHER_ITEM, "Other (type it)"),
        ]

    def clean_quantity(self):
        quantity = self.cleaned_data.get("quantity")
        if quantity is None or not 1 <= quantity <= 999:
            raise forms.ValidationError(QUANTITY_MESSAGE)
        return quantity

    def clean_unit_price(self):
        price = self.cleaned_data.get("unit_price")
        if price is not None and price < 0:
            raise forms.ValidationError("The price can't be negative.")
        return price

    def clean(self):
        cleaned_data = super().clean()
        if "service_choice" not in cleaned_data:
            return cleaned_data
        service = self.services.get(cleaned_data["service_choice"])
        cleaned_data["service"] = service
        self._fill_description(cleaned_data, service)
        if "unit_price" not in self.errors:
            self._fill_price(cleaned_data, service)
        self._check_amount(cleaned_data)
        return cleaned_data

    def _fill_description(self, cleaned_data, service):
        description = (cleaned_data.get("description") or "").strip()
        if not description and service:
            description = service.service_name
        elif not description and "description" not in self.errors:
            self.add_error("description", "Type what it is.")
        cleaned_data["description"] = description

    def _fill_price(self, cleaned_data, service):
        if cleaned_data.get("unit_price") is not None:
            return
        if service and service.pricing_type == Service.FIXED:
            cleaned_data["unit_price"] = service.base_price
        else:
            self.add_error("unit_price", "Type the price.")

    def _check_amount(self, cleaned_data):
        quantity = cleaned_data.get("quantity")
        price = cleaned_data.get("unit_price")
        if quantity is None or price is None:
            return
        if quantity * price > ITEM_AMOUNT_LIMIT:
            self.add_error(
                "unit_price",
                "This item comes to more than ₱999,999.99. "
                "Check the quantity and the price.",
            )
