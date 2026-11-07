from decimal import Decimal

from django import forms
from django.db.models import Sum
from django.db.models.functions import Lower, Trim

from .models import Service, BillingReceipt

# Service labels live here, not in the model's choices (that would be a
# migration). Status order is the order the pages show the groups in.
SERVICE_PRICING_CHOICES = [
    (Service.FIXED, "One price"),
    (Service.RANGE, "A price range"),
]
SERVICE_STATUS_CHOICES = [
    (Service.ACTIVE, "Offered"),
    (Service.UNLISTED, "Clinic use only"),
    (Service.DRAFT, "Hidden (draft)"),
    (Service.CLOSED, "No longer offered"),
]
PRICE_FIELDS = ("base_price", "min_price", "max_price")
NEGATIVE_PRICE = "Prices can't be negative."


class ServiceForm(forms.ModelForm):
    """
    Admin form — create or edit a service in the catalog (rules 29–31).
    The name is unique ignoring letter case and outer spaces. Saving clears
    the other pricing type: One price keeps base_price only; a range keeps
    min / max and sets base_price to the lowest (the column has no blank).
    """

    pricing_type = forms.ChoiceField(
        label="How is it priced?", choices=SERVICE_PRICING_CHOICES
    )
    status = forms.ChoiceField(
        label="Who sees it", choices=SERVICE_STATUS_CHOICES, initial=Service.DRAFT
    )

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
        labels = {
            "service_name": "Name",
            "category": "Category",
            "description": "Description",
            "base_price": "Price",
            "min_price": "Lowest price",
            "max_price": "Highest price",
        }
        help_texts = {
            "category": "Groups similar services. Type any word.",
            "description": "Pet owners see this when they book.",
        }
        error_messages = {"service_name": {"required": "Type the service name."}}
        widgets = {
            "service_name": forms.TextInput(
                attrs={"placeholder": "For example: Consultation"}
            ),
            "category": forms.TextInput(attrs={"placeholder": "For example: Vaccines"}),
            "description": forms.Textarea(
                attrs={"rows": 3, "placeholder": "What it includes, in a sentence"}
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Checked per pricing type in clean(), not at field level
        for name in PRICE_FIELDS:
            self.fields[name].required = False

    def clean_service_name(self):
        name = self.cleaned_data["service_name"].strip()
        if not name:
            raise forms.ValidationError("Type the service name.")
        taken = (
            Service.objects.annotate(plain_name=Lower(Trim("service_name")))
            .filter(plain_name=name.lower())
            .exclude(pk=self.instance.pk)
            .first()
        )
        if taken:
            raise forms.ValidationError(
                f"There is already a service called “{taken.service_name.strip()}”."
            )
        return name

    def clean(self):
        cleaned_data = super().clean()
        pricing_type = cleaned_data.get("pricing_type")
        if pricing_type == Service.FIXED:
            self._clean_one_price(cleaned_data)
        elif pricing_type == Service.RANGE:
            self._clean_price_range(cleaned_data)
        return cleaned_data

    def _negative(self, cleaned_data, names):
        """Adds "Prices can't be negative." to each of these fields that is;
        True if any was."""
        found = False
        for name in names:
            value = cleaned_data.get(name)
            if value is not None and value < 0:
                self.add_error(name, NEGATIVE_PRICE)
                found = True
        return found

    def _clean_one_price(self, cleaned_data):
        if "base_price" in self.errors or self._negative(cleaned_data, ["base_price"]):
            return
        if cleaned_data.get("base_price") is None:
            self.add_error("base_price", "Type the price.")
            return
        cleaned_data["min_price"] = None
        cleaned_data["max_price"] = None

    def _clean_price_range(self, cleaned_data):
        names = ["min_price", "max_price"]
        if any(name in self.errors for name in names):
            return
        if self._negative(cleaned_data, names):
            return
        low, high = cleaned_data.get("min_price"), cleaned_data.get("max_price")
        if low is None or high is None:
            missing = "min_price" if low is None else "max_price"
            self.add_error(missing, "Type the lowest and the highest price.")
        elif high <= low:
            self.add_error(
                "max_price", "The highest price must be more than the lowest."
            )
        else:
            cleaned_data["base_price"] = low

    def save(self, commit=True):
        # Set from cleaned_data even when a price field wasn't posted at all
        # (the model form would keep the old value or the default then)
        service = super().save(commit=False)
        for name in PRICE_FIELDS:
            setattr(service, name, self.cleaned_data[name])
        if commit:
            service.save()
        return service


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


def _has_range(service):
    """A Range service with both prices set (otherwise its base price is
    shown, as before)."""
    return (
        service.pricing_type == Service.RANGE
        and service.min_price is not None
        and service.max_price is not None
    )


def service_choice_label(service):
    """ "Consultation · ₱300.00" or "Grooming · ₱300.00 to ₱800.00"."""
    if _has_range(service):
        price = f"{peso(service.min_price)} to {peso(service.max_price)}"
    else:
        price = peso(service.base_price)
    return f"{service.service_name} · {price}"


def service_options(services):
    """
    One dict per service for "What is it?": the form's choices and the
    receipt page's Add item options (data-* in 6b-2) both come from here.
    pricing "range" (with min_price / max_price, price None) or "fixed"
    (with price, min / max None).
    """
    options = []
    for service in services:
        is_range = _has_range(service)
        options.append(
            {
                "value": str(service.pk),
                "label": service_choice_label(service),
                "name": service.service_name,
                "pricing": Service.RANGE if is_range else Service.FIXED,
                "price": None if is_range else service.base_price,
                "min_price": service.min_price if is_range else None,
                "max_price": service.max_price if is_range else None,
            }
        )
    return options


def price_help(service):
    """Rule 11: the Unit price help for the picked service ("" for none
    or Other)."""
    if service is None:
        return ""
    if _has_range(service):
        return (
            f"Usual price {peso(service.min_price)} to {peso(service.max_price)}. "
            "You can bill outside it."
        )
    return f"Price list: {peso(service.base_price)}."


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
        options = service_options(self.services.values())
        self.fields["service_choice"].choices = [
            ("", "Choose one"),
            *[(option["value"], option["label"]) for option in options],
            (OTHER_ITEM, "Other (type it)"),
        ]
        # Rule 11: the help under Unit price for the posted choice ("" when
        # unbound, nothing picked, Other or not a billable service)
        picked = (
            self.data.get(self.add_prefix("service_choice")) if self.is_bound else ""
        )
        self.price_help = price_help(self.services.get(str(picked or "")))

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
