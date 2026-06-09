from django import forms
from django.utils import timezone

from .models import Service, BillingReceipt, BillingItem


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


class BillingReceiptForm(forms.ModelForm):
    """
    Admin form — create or edit a billing receipt header.
    Items are added separately via BillingItemForm.
    """

    class Meta:
        model = BillingReceipt
        fields = [
            "billing_date",
            "discount_type",
            "discount_value",
            "discount_note",
            "payment_method",
            "payment_status",
            "payment_date",
        ]
        widgets = {
            "billing_date": forms.DateInput(attrs={"type": "date"}),
            "payment_date": forms.DateInput(attrs={"type": "date"}),
        }

    def clean(self):
        cleaned_data = super().clean()
        payment_status = cleaned_data.get("payment_status")
        payment_date = cleaned_data.get("payment_date")
        discount_type = cleaned_data.get("discount_type")
        discount_value = cleaned_data.get("discount_value")

        # Payment date required when marking as paid
        if payment_status == BillingReceipt.PAID and not payment_date:
            cleaned_data["payment_date"] = timezone.now().date()

        # Discount value required when discount type is set
        if discount_type and (discount_value is None or discount_value <= 0):
            raise forms.ValidationError(
                "Please provide a discount value when a discount type is selected."
            )

        return cleaned_data


class BillingItemForm(forms.Form):
    """
    Admin form — add a billing line item to a receipt.
    Supports both catalog services and manual items.
    """

    # Optional — if set, description auto-fills from service name
    service = forms.ModelChoiceField(
        queryset=Service.objects.filter(
            status__in=[Service.ACTIVE, Service.UNLISTED]
        ),
        required=False,
        empty_label="Manual item (no service)",
    )
    description = forms.CharField(
        max_length=200,
        help_text="Required for manual items. Auto-filled when service is selected.",
    )
    quantity = forms.IntegerField(min_value=1, initial=1)
    unit_price = forms.DecimalField(
        max_digits=10,
        decimal_places=2,
        min_value=0,
    )

    def clean(self):
        cleaned_data = super().clean()
        service = cleaned_data.get("service")
        description = cleaned_data.get("description", "").strip()

        # If no service selected, description is required
        if not service and not description:
            raise forms.ValidationError(
                "Please provide a description for manual items."
            )

        # Auto-fill description from service name if not provided
        if service and not description:
            cleaned_data["description"] = service.service_name

        return cleaned_data