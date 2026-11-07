import datetime
import inspect
import re
from decimal import Decimal
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.messages import constants as message_levels
from django.contrib.messages import get_messages
from django.db import IntegrityError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from appointments.models import Appointment
from billing import views as billing_views
from billing.forms import BillingItemForm, BillingReceiptForm
from billing.models import BillingItem, BillingReceipt, Service
from billing.utils import (
    NO_ITEMS_MESSAGE,
    PICK_PET_MESSAGE,
    RECEIPT_EXISTS,
    save_new_receipt,
)
from billing.views import LOCKED_MESSAGE
from notifications.models import Notification
from pets.models import Pet, PetOwner

User = get_user_model()

LOCKED_STATUSES = [BillingReceipt.PAID, BillingReceipt.CANCELLED]

MANILA = ZoneInfo("Asia/Manila")
# Sat, Nov 7, 2026, 00:30 Manila: still Fri, Nov 6 in UTC
JUST_AFTER_MIDNIGHT = datetime.datetime(2026, 11, 7, 0, 30, tzinfo=MANILA)
TODAY = datetime.date(2026, 11, 7)

# Pages render with plain static storage (no collectstatic manifest needed)
PLAIN_STATIC = override_settings(
    STORAGES={
        **settings.STORAGES,
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
        },
    }
)


class BillingTestBase(TestCase):
    """Admin logged in, plus one owner/pet to bill."""

    def setUp(self):
        self.admin = User.objects.create_user(
            username="admin",
            email="admin@example.com",
            password="unused-pass-123",
            role=User.ADMIN,
            is_onboarded=True,
        )
        owner_user = User.objects.create_user(
            username="owner",
            email="owner@example.com",
            password="unused-pass-123",
            role=User.PET_OWNER,
            is_onboarded=True,
        )
        self.owner = PetOwner.objects.create(
            user=owner_user, first_name="Ana", last_name="Cruz"
        )
        self.pet = Pet.objects.create(owner=self.owner, name="Bantay", species="Dog")
        self.client.force_login(self.admin)

    def _receipt(self, status=BillingReceipt.PENDING):
        """A receipt with one item (2 x 100.00) in the given status."""
        receipt = BillingReceipt.objects.create(
            receipt_number=BillingReceipt.generate_receipt_number(),
            owner=self.owner,
            pet=self.pet,
            payment_status=status,
            payment_date=(
                timezone.localdate() if status == BillingReceipt.PAID else None
            ),
        )
        BillingItem.objects.create(
            receipt=receipt,
            description="Consultation",
            quantity=2,
            unit_price=Decimal("100.00"),
            subtotal=0,  # Overwritten by BillingItem.save()
        )
        receipt.calculate_totals()
        return receipt

    def _messages(self, response):
        return [str(m) for m in get_messages(response.wsgi_request)]

    def _edit_data(self, **extra):
        """Valid BillingReceiptForm POST data with a 50.00 fixed discount."""
        data = {
            "billing_date": timezone.localdate().isoformat(),
            "discount_type": BillingReceipt.FIXED_DISCOUNT,
            "discount_value": "50.00",
            "discount_note": "Test discount",
            "payment_method": "Cash",
        }
        data.update(extra)
        return data


class ReceiptModelTests(BillingTestBase):

    def test_is_locked_by_status(self):
        """is_locked is False for PENDING, True for PAID and CANCELLED."""
        self.assertFalse(self._receipt(BillingReceipt.PENDING).is_locked)
        for status in LOCKED_STATUSES:
            with self.subTest(status=status):
                self.assertTrue(self._receipt(status).is_locked)

    def test_is_locked_defined_once(self):
        """The class body defines is_locked only once (a later duplicate
        would silently replace the first, so __dict__ can't tell)."""
        source = inspect.getsource(BillingReceipt)
        self.assertEqual(source.count("def is_locked"), 1)

    def test_receipt_number_uses_the_manila_year(self):
        """00:30 on Jan 1, 2027 in Manila is still Dec 31, 2026 in UTC."""
        new_year = datetime.datetime(2027, 1, 1, 0, 30, tzinfo=ZoneInfo("Asia/Manila"))
        with mock.patch("django.utils.timezone.now", return_value=new_year):
            number = BillingReceipt.generate_receipt_number()
        self.assertEqual(number, "HV-2027-0001")


class LockedReceiptTests(BillingTestBase):
    """Every write path refuses PAID and CANCELLED receipts."""

    def test_edit_get_redirects_to_detail(self):
        """GET edit on a locked receipt redirects with LOCKED_MESSAGE."""
        for status in LOCKED_STATUSES:
            with self.subTest(status=status):
                receipt = self._receipt(status)
                response = self.client.get(
                    reverse("admin_receipt_edit", args=[receipt.pk])
                )
                self.assertRedirects(
                    response,
                    reverse("admin_receipt_detail", args=[receipt.pk]),
                    fetch_redirect_response=False,
                )
                self.assertIn(LOCKED_MESSAGE, self._messages(response))

    def test_edit_post_redirects_and_changes_nothing(self):
        """POST edit on a locked receipt redirects; fields stay the same."""
        for status in LOCKED_STATUSES:
            with self.subTest(status=status):
                receipt = self._receipt(status)
                response = self.client.post(
                    reverse("admin_receipt_edit", args=[receipt.pk]),
                    self._edit_data(payment_status=BillingReceipt.PENDING),
                )
                self.assertRedirects(
                    response,
                    reverse("admin_receipt_detail", args=[receipt.pk]),
                    fetch_redirect_response=False,
                )
                self.assertIn(LOCKED_MESSAGE, self._messages(response))
                receipt.refresh_from_db()
                self.assertEqual(receipt.payment_status, status)
                self.assertEqual(receipt.discount_type, "")
                self.assertEqual(receipt.discount_value, Decimal("0"))
                self.assertEqual(receipt.total_amount, Decimal("200.00"))

    def test_item_add_creates_nothing(self):
        """POST item add on a locked receipt creates no item."""
        for status in LOCKED_STATUSES:
            with self.subTest(status=status):
                receipt = self._receipt(status)
                response = self.client.post(
                    reverse("admin_billing_item_add", args=[receipt.pk]),
                    {"description": "Extra", "quantity": 1, "unit_price": "10.00"},
                )
                self.assertIn(LOCKED_MESSAGE, self._messages(response))
                self.assertEqual(receipt.billing_items.count(), 1)

    def test_item_delete_keeps_item(self):
        """POST item delete on a locked receipt leaves the item."""
        for status in LOCKED_STATUSES:
            with self.subTest(status=status):
                receipt = self._receipt(status)
                item = receipt.billing_items.get()
                response = self.client.post(
                    reverse("admin_billing_item_delete", args=[item.pk])
                )
                self.assertIn(LOCKED_MESSAGE, self._messages(response))
                self.assertTrue(BillingItem.objects.filter(pk=item.pk).exists())

    def test_mark_paid_on_cancelled_refused(self):
        """CANCELLED -> PAID is refused and no notification is sent."""
        receipt = self._receipt(BillingReceipt.CANCELLED)
        response = self.client.post(
            reverse("admin_receipt_mark_paid", args=[receipt.pk])
        )
        self.assertIn(LOCKED_MESSAGE, self._messages(response))
        receipt.refresh_from_db()
        self.assertEqual(receipt.payment_status, BillingReceipt.CANCELLED)
        self.assertIsNone(receipt.payment_date)
        self.assertEqual(Notification.objects.count(), 0)

    def test_cancel_on_paid_refused(self):
        """PAID -> CANCELLED is refused."""
        receipt = self._receipt(BillingReceipt.PAID)
        response = self.client.post(
            reverse("admin_receipt_mark_cancelled", args=[receipt.pk])
        )
        self.assertIn(LOCKED_MESSAGE, self._messages(response))
        receipt.refresh_from_db()
        self.assertEqual(receipt.payment_status, BillingReceipt.PAID)


class PendingReceiptTests(BillingTestBase):

    def test_edit_saves_and_recalculates_totals(self):
        """Editing a PENDING receipt saves the discount and recalculates."""
        receipt = self._receipt()
        response = self.client.post(
            reverse("admin_receipt_edit", args=[receipt.pk]), self._edit_data()
        )
        self.assertRedirects(
            response,
            reverse("admin_receipt_detail", args=[receipt.pk]),
            fetch_redirect_response=False,
        )
        receipt.refresh_from_db()
        self.assertEqual(receipt.discount_value, Decimal("50.00"))
        self.assertEqual(receipt.subtotal, Decimal("200.00"))
        self.assertEqual(receipt.total_amount, Decimal("150.00"))

    def test_edit_ignores_payment_status(self):
        """payment_status in the edit POST is ignored (not a form field)."""
        receipt = self._receipt()
        self.client.post(
            reverse("admin_receipt_edit", args=[receipt.pk]),
            self._edit_data(
                payment_status=BillingReceipt.PAID,
                payment_date=timezone.localdate().isoformat(),
            ),
        )
        receipt.refresh_from_db()
        self.assertEqual(receipt.payment_status, BillingReceipt.PENDING)
        self.assertIsNone(receipt.payment_date)

    def test_mark_paid(self):
        """Mark paid sets PAID and payment_date, and notifies the owner once."""
        receipt = self._receipt()
        self.client.post(reverse("admin_receipt_mark_paid", args=[receipt.pk]))
        receipt.refresh_from_db()
        self.assertEqual(receipt.payment_status, BillingReceipt.PAID)
        self.assertEqual(receipt.payment_date, timezone.localdate())
        self.assertEqual(
            Notification.objects.filter(related_billing=receipt).count(), 1
        )

    def test_cancel(self):
        """Cancel sets CANCELLED."""
        receipt = self._receipt()
        self.client.post(reverse("admin_receipt_mark_cancelled", args=[receipt.pk]))
        receipt.refresh_from_db()
        self.assertEqual(receipt.payment_status, BillingReceipt.CANCELLED)


class CreateReceiptTests(BillingTestBase):

    def test_create_ignores_payment_status(self):
        """A new receipt is always PENDING, even if the POST says paid.
        (6a-1: posted with no discount; a new receipt has no items, so a
        peso discount is now refused as more than the ₱0.00 subtotal.
        6a-2: posted to the new flow, "Which pet?" (?owner=) and from a
        visit (?appointment=); neither reads the discount, so the posted
        one is ignored too.)"""
        visit = Appointment.objects.create(
            owner=self.owner,
            pet=self.pet,
            date=timezone.localdate(),
            time=datetime.time(9, 0),
            status=Appointment.COMPLETED,
        )
        url = reverse("admin_receipt_create")
        cases = [
            (f"{url}?owner={self.owner.pk}", {"pet": self.pet.pk}),
            (f"{url}?appointment={visit.pk}", {}),
        ]
        for create_url, extra in cases:
            with self.subTest(url=create_url):
                data = self._edit_data(
                    payment_status=BillingReceipt.PAID,
                    payment_date=timezone.localdate().isoformat(),
                    **extra,
                )
                response = self.client.post(create_url, data)
                receipt = BillingReceipt.objects.latest("pk")
                self.assertRedirects(
                    response,
                    reverse("admin_receipt_detail", args=[receipt.pk]),
                    fetch_redirect_response=False,
                )
                self.assertEqual(receipt.payment_status, BillingReceipt.PENDING)
                self.assertIsNone(receipt.payment_date)
                self.assertEqual(receipt.discount_type, "")
                self.assertEqual(receipt.discount_value, Decimal("0"))
        self.assertEqual(BillingReceipt.objects.count(), 2)


class BillingAccessTests(BillingTestBase):

    def test_pet_owner_cannot_open_admin_receipt(self):
        """A logged-in pet owner is redirected away from admin receipts."""
        receipt = self._receipt()
        self.client.force_login(self.owner.user)
        response = self.client.get(reverse("admin_receipt_detail", args=[receipt.pk]))
        self.assertEqual(response.status_code, 302)


# ── Slice 6a-1: receipt rules ─────────────────────────────────────────────────


class FrozenBillingTestBase(BillingTestBase):
    """BillingTestBase with time frozen at self.now (default: 00:30 Manila
    on Sat, Nov 7, 2026, still Nov 6 in UTC)."""

    def setUp(self):
        self.now = JUST_AFTER_MIDNIGHT
        patcher = mock.patch("django.utils.timezone.now", side_effect=lambda: self.now)
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()

    def _empty_receipt(self, **fields):
        return BillingReceipt.objects.create(
            receipt_number=BillingReceipt.generate_receipt_number(),
            owner=self.owner,
            pet=self.pet,
            **fields,
        )

    def _item(self, receipt, price, quantity=1, description="Item", service=None):
        return BillingItem.objects.create(
            receipt=receipt,
            service=service,
            description=description,
            quantity=quantity,
            unit_price=Decimal(price),
            subtotal=0,  # Overwritten by BillingItem.save()
        )

    def _service(self, name, price="300.00", status=Service.ACTIVE, **fields):
        return Service.objects.create(
            service_name=name, base_price=Decimal(price), status=status, **fields
        )

    def _range_service(self, name, low, high, status=Service.ACTIVE):
        return self._service(
            name,
            price=low,
            status=status,
            pricing_type=Service.RANGE,
            min_price=Decimal(low),
            max_price=Decimal(high),
        )

    def _post(self, name, pk, data=None):
        return self.client.post(reverse(name, args=[pk]), data or {})


class CalculateTotalsTests(FrozenBillingTestBase):

    def _with_discount(self, price, kind, value):
        receipt = self._empty_receipt(discount_type=kind, discount_value=Decimal(value))
        self._item(receipt, price)
        receipt.calculate_totals()
        receipt.refresh_from_db()
        return receipt

    def test_percentage_is_rounded_half_up(self):
        """33.33% of ₱100.00 is ₱33.33; of ₱100.10 it is ₱33.36 (33.3633);
        12.5% of ₱0.20 is ₱0.025 → ₱0.03 (half up, not half even)."""
        cases = [
            ("100.00", "33.33", "33.33", "66.67"),
            ("100.10", "33.33", "33.36", "66.74"),
            ("0.20", "12.50", "0.03", "0.17"),
        ]
        for price, percent, discount, total in cases:
            with self.subTest(price=price, percent=percent):
                receipt = self._with_discount(price, BillingReceipt.PERCENTAGE, percent)
                self.assertEqual(receipt.subtotal, Decimal(price))
                self.assertEqual(receipt.discount_amount, Decimal(discount))
                self.assertEqual(receipt.total_amount, Decimal(total))
                self.assertEqual(
                    receipt.subtotal - receipt.discount_amount, receipt.total_amount
                )

    def test_peso_discount_capped_after_an_item_is_removed(self):
        """₱250 off ₱300; removing the ₱100 item leaves ₱200 - ₱200 = ₱0."""
        receipt = self._empty_receipt()
        self._item(receipt, "200.00")
        extra = self._item(receipt, "100.00")
        receipt.calculate_totals()
        self._post(
            "admin_receipt_edit",
            receipt.pk,
            {"discount_type": BillingReceipt.FIXED_DISCOUNT, "discount_value": "250"},
        )
        receipt.refresh_from_db()
        self.assertEqual(receipt.total_amount, Decimal("50.00"))

        self._post("admin_billing_item_delete", extra.pk)
        receipt.refresh_from_db()
        self.assertEqual(receipt.subtotal, Decimal("200.00"))
        self.assertEqual(receipt.discount_value, Decimal("250.00"))  # kept as typed
        self.assertEqual(receipt.discount_amount, Decimal("200.00"))  # shown capped
        self.assertEqual(receipt.total_amount, Decimal("0.00"))

    def test_total_never_below_zero(self):
        receipt = self._with_discount("200.00", BillingReceipt.FIXED_DISCOUNT, "500")
        self.assertEqual(receipt.discount_amount, Decimal("200.00"))
        self.assertEqual(receipt.total_amount, Decimal("0.00"))

    def test_no_items_is_zero(self):
        receipt = self._empty_receipt(
            discount_type=BillingReceipt.FIXED_DISCOUNT, discount_value=Decimal("50")
        )
        receipt.calculate_totals()
        receipt.refresh_from_db()
        self.assertEqual(receipt.subtotal, Decimal("0.00"))
        self.assertEqual(receipt.total_amount, Decimal("0.00"))

    def test_saves_only_the_totals(self):
        """calculate_totals() writes subtotal, total and updated_at only."""
        receipt = self._empty_receipt(discount_note="Saved note")
        self._item(receipt, "120.00")
        receipt.discount_note = "Not saved"
        receipt.payment_status = BillingReceipt.CANCELLED
        receipt.calculate_totals()
        receipt.refresh_from_db()
        self.assertEqual(receipt.total_amount, Decimal("120.00"))
        self.assertEqual(receipt.discount_note, "Saved note")
        self.assertEqual(receipt.payment_status, BillingReceipt.PENDING)

    def test_stale_copy_does_not_undo_paid(self):
        """A copy loaded before mark paid, recalculated after it, leaves the
        receipt PAID; the locked views then refuse it."""
        receipt = self._receipt()
        stale = BillingReceipt.objects.get(pk=receipt.pk)
        self._post("admin_receipt_mark_paid", receipt.pk)
        stale.calculate_totals()  # stale.payment_status is still PENDING
        receipt.refresh_from_db()
        self.assertEqual(receipt.payment_status, BillingReceipt.PAID)
        self.assertEqual(receipt.payment_date, TODAY)

        response = self._post(
            "admin_billing_item_add",
            receipt.pk,
            {"service_choice": "other", "description": "Extra", "unit_price": "10"},
        )
        self.assertIn(LOCKED_MESSAGE, self._messages(response))
        self.assertEqual(receipt.billing_items.count(), 1)
        response = self._post("admin_receipt_edit", receipt.pk, self._edit_data())
        self.assertIn(LOCKED_MESSAGE, self._messages(response))
        receipt.refresh_from_db()
        self.assertEqual(receipt.discount_value, Decimal("0"))


class DiscountAmountTests(FrozenBillingTestBase):

    def _receipt_with(self, kind, value, price="200.00"):
        receipt = self._empty_receipt(discount_type=kind, discount_value=Decimal(value))
        if price:
            self._item(receipt, price)
        receipt.calculate_totals()
        return receipt

    def test_no_discount(self):
        receipt = self._receipt_with("", "0")
        self.assertEqual(receipt.discount_amount, Decimal("0"))
        self.assertFalse(receipt.has_discount)

    def test_a_value_without_a_type_is_no_discount(self):
        receipt = self._receipt_with("", "50")
        self.assertEqual(receipt.discount_amount, Decimal("0"))
        self.assertFalse(receipt.has_discount)
        self.assertEqual(receipt.total_amount, Decimal("200.00"))

    def test_a_type_without_a_value_is_no_discount(self):
        receipt = self._receipt_with(BillingReceipt.PERCENTAGE, "0")
        self.assertFalse(receipt.has_discount)

    def test_percentage_and_peso(self):
        receipt = self._receipt_with(BillingReceipt.PERCENTAGE, "10")
        self.assertEqual(receipt.discount_amount, Decimal("20.00"))
        self.assertTrue(receipt.has_discount)
        receipt = self._receipt_with(BillingReceipt.FIXED_DISCOUNT, "35.50")
        self.assertEqual(receipt.discount_amount, Decimal("35.50"))
        self.assertTrue(receipt.has_discount)

    def test_peso_discount_on_no_items_is_nothing(self):
        receipt = self._receipt_with(BillingReceipt.FIXED_DISCOUNT, "50", price=None)
        self.assertEqual(receipt.discount_amount, Decimal("0.00"))
        self.assertFalse(receipt.has_discount)


class DiscountFormTests(FrozenBillingTestBase):

    def setUp(self):
        super().setUp()
        # ₱700.00 in items; the stored subtotal is left stale on purpose
        # (the form must use the items)
        self.receipt = self._empty_receipt()
        self._item(self.receipt, "200.00")
        self._item(self.receipt, "500.00")

    def _form(self, kind="", value="", note="", receipt=None):
        return BillingReceiptForm(
            {"discount_type": kind, "discount_value": value, "discount_note": note},
            instance=receipt or self.receipt,
        )

    def _error(self, form):
        self.assertFalse(form.is_valid())
        return form.errors["discount_value"]

    def test_fields_labels_and_choices(self):
        form = BillingReceiptForm(instance=self.receipt)
        self.assertEqual(
            list(form.fields), ["discount_type", "discount_value", "discount_note"]
        )
        self.assertEqual(form.fields["discount_type"].label, "Discount")
        self.assertEqual(form.fields["discount_value"].label, "How much")
        self.assertEqual(form.fields["discount_note"].label, "Note")
        self.assertEqual(
            list(form.fields["discount_type"].choices),
            [
                ("", "No discount"),
                ("percentage", "Percent off (%)"),
                ("fixed", "Peso amount off (₱)"),
            ],
        )
        self.assertFalse(form.fields["discount_value"].required)
        self.assertEqual(
            form.fields["discount_note"].help_text, "Printed next to the discount."
        )
        self.assertEqual(
            form.fields["discount_note"].widget.attrs["placeholder"],
            "For example: Loyal client",
        )

    def test_negative_amount(self):
        for kind in ["", BillingReceipt.PERCENTAGE, BillingReceipt.FIXED_DISCOUNT]:
            with self.subTest(kind=kind):
                self.assertEqual(
                    self._error(self._form(kind, "-5")),
                    ["The amount can't be negative."],
                )

    def test_type_with_no_amount(self):
        for kind in [BillingReceipt.PERCENTAGE, BillingReceipt.FIXED_DISCOUNT]:
            for value in ["", "0", "0.00"]:
                with self.subTest(kind=kind, value=value):
                    self.assertEqual(
                        self._error(self._form(kind, value)), ["Type how much."]
                    )

    def test_amount_with_no_type(self):
        self.assertEqual(
            self._error(self._form("", "10")),
            ["Choose the kind of discount, or clear the amount."],
        )

    def test_percentage_over_100(self):
        self.assertEqual(
            self._error(self._form(BillingReceipt.PERCENTAGE, "100.01")),
            ["A discount can't be more than 100%."],
        )
        self.assertTrue(self._form(BillingReceipt.PERCENTAGE, "100").is_valid())

    def test_peso_over_the_items_subtotal(self):
        self.assertEqual(
            self._error(self._form(BillingReceipt.FIXED_DISCOUNT, "700.01")),
            ["A discount can't be more than the subtotal (₱700.00)."],
        )
        form = self._form(BillingReceipt.FIXED_DISCOUNT, "700")
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data["discount_value"], Decimal("700"))

    def test_subtotal_with_a_comma(self):
        receipt = self._empty_receipt()
        self._item(receipt, "1250.00")
        self.assertEqual(
            self._error(
                self._form(BillingReceipt.FIXED_DISCOUNT, "1300", receipt=receipt)
            ),
            ["A discount can't be more than the subtotal (₱1,250.00)."],
        )

    def test_no_discount_clears_value_and_note(self):
        form = self._form("", "", "Loyal client")
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data["discount_value"], Decimal("0"))
        self.assertEqual(form.cleaned_data["discount_note"], "")
        form = self._form("", "0", "Loyal client")
        self.assertTrue(form.is_valid())

    def test_no_discount_saved_through_the_page(self):
        self.receipt.discount_type = BillingReceipt.PERCENTAGE
        self.receipt.discount_value = Decimal("10")
        self.receipt.discount_note = "Loyal client"
        self.receipt.save()
        self.receipt.calculate_totals()
        self._post(
            "admin_receipt_edit",
            self.receipt.pk,
            {
                "discount_type": "",
                "discount_value": "",
                "discount_note": "Loyal client",
            },
        )
        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.discount_type, "")
        self.assertEqual(self.receipt.discount_value, Decimal("0"))
        self.assertEqual(self.receipt.discount_note, "")
        self.assertEqual(self.receipt.total_amount, Decimal("700.00"))

    def test_edit_saves_the_discount_and_totals(self):
        self._post(
            "admin_receipt_edit",
            self.receipt.pk,
            {
                "discount_type": BillingReceipt.PERCENTAGE,
                "discount_value": "15",
                "discount_note": "Senior",
            },
        )
        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.discount_note, "Senior")
        self.assertEqual(self.receipt.subtotal, Decimal("700.00"))
        self.assertEqual(self.receipt.total_amount, Decimal("595.00"))

    @PLAIN_STATIC
    def test_edit_with_errors_shows_the_page_and_saves_nothing(self):
        response = self._post(
            "admin_receipt_edit",
            self.receipt.pk,
            {"discount_type": BillingReceipt.FIXED_DISCOUNT, "discount_value": "900"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context["form"].errors["discount_value"],
            ["A discount can't be more than the subtotal (₱700.00)."],
        )
        # The page shows the saved values, not the refused ones
        self.assertEqual(response.context["receipt"].discount_value, Decimal("0"))
        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.discount_type, "")
        self.assertEqual(self.receipt.discount_value, Decimal("0"))


class ItemFormTests(FrozenBillingTestBase):

    def setUp(self):
        super().setUp()
        self.consult = self._service("Consultation", "300.00")
        self.acupuncture = self._service("acupuncture", "1250.00")
        self.grooming = self._range_service(
            "Grooming", "300.00", "800.00", status=Service.UNLISTED
        )
        self.draft = self._service("Bath", status=Service.DRAFT)
        self.closed = self._service("X-ray", status=Service.CLOSED)

    def _form(self, choice, description="", quantity="1", price=""):
        return BillingItemForm(
            {
                "service_choice": str(choice),
                "description": description,
                "quantity": quantity,
                "unit_price": price,
            }
        )

    def _valid(self, *args, **kwargs):
        form = self._form(*args, **kwargs)
        self.assertTrue(form.is_valid(), form.errors)
        return form.cleaned_data

    def _errors(self, field, *args, **kwargs):
        form = self._form(*args, **kwargs)
        self.assertFalse(form.is_valid())
        return form.errors.get(field)

    def test_choices_order_and_labels(self):
        form = BillingItemForm()
        self.assertEqual(
            list(form.fields["service_choice"].choices),
            [
                ("", "Choose one"),
                (str(self.acupuncture.pk), "acupuncture · ₱1,250.00"),
                (str(self.consult.pk), "Consultation · ₱300.00"),
                (str(self.grooming.pk), "Grooming · ₱300.00 to ₱800.00"),
                ("other", "Other (type it)"),
            ],
        )

    def test_fields_and_labels(self):
        form = BillingItemForm()
        self.assertEqual(
            list(form.fields),
            ["service_choice", "description", "quantity", "unit_price"],
        )
        self.assertEqual(form.fields["service_choice"].label, "What is it?")
        self.assertEqual(form.fields["description"].label, "Description")
        self.assertEqual(
            form.fields["description"].help_text,
            "Printed on the receipt. You can change it.",
        )
        self.assertFalse(form.fields["description"].required)
        self.assertEqual(form.fields["quantity"].label, "Quantity")
        self.assertEqual(form.fields["unit_price"].label, "Unit price")
        self.assertFalse(form.fields["unit_price"].required)

    def test_nothing_picked(self):
        for choice in ["", "abc", "99999"]:
            with self.subTest(choice=choice):
                self.assertEqual(
                    self._errors("service_choice", choice, "Thing", price="10"),
                    ["Choose what it is, or Other (type it)."],
                )

    def test_draft_or_closed_service_posted_by_hand(self):
        for service in [self.draft, self.closed]:
            with self.subTest(status=service.status):
                self.assertEqual(
                    self._errors("service_choice", service.pk, price="10"),
                    ["Choose what it is, or Other (type it)."],
                )

    def test_fixed_fills_description_and_price(self):
        data = self._valid(self.consult.pk)
        self.assertEqual(data["service"], self.consult)
        self.assertEqual(data["description"], "Consultation")
        self.assertEqual(data["unit_price"], Decimal("300.00"))

    def test_typed_description_and_price_are_kept(self):
        data = self._valid(self.consult.pk, "  Consultation (senior)  ", price="250")
        self.assertEqual(data["description"], "Consultation (senior)")
        self.assertEqual(data["unit_price"], Decimal("250"))

    def test_range_needs_a_price(self):
        self.assertEqual(
            self._errors("unit_price", self.grooming.pk), ["Type the price."]
        )
        data = self._valid(self.grooming.pk, price="950")  # outside the range: fine
        self.assertEqual(data["description"], "Grooming")
        self.assertEqual(data["unit_price"], Decimal("950"))

    def test_other_needs_a_description_and_a_price(self):
        form = self._form("other", "   ")
        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors["description"], ["Type what it is."])
        self.assertEqual(form.errors["unit_price"], ["Type the price."])
        data = self._valid("other", "Dog food 1 kg", "2", "185.50")
        self.assertIsNone(data["service"])
        self.assertEqual(data["description"], "Dog food 1 kg")

    def test_quantity_from_1_to_999(self):
        for quantity in ["0", "1000", "", "-1", "abc", "1.5"]:
            with self.subTest(quantity=quantity):
                self.assertEqual(
                    self._errors("quantity", "other", "Thing", quantity, "10"),
                    ["The quantity must be from 1 to 999."],
                )
        form = BillingItemForm({"service_choice": "other", "description": "Thing"})
        self.assertFalse(form.is_valid())
        self.assertEqual(
            form.errors["quantity"], ["The quantity must be from 1 to 999."]
        )
        for quantity in ["1", "999"]:
            with self.subTest(quantity=quantity):
                self._valid("other", "Thing", quantity, "10")

    def test_negative_price(self):
        self.assertEqual(
            self._errors("unit_price", self.consult.pk, price="-0.01"),
            ["The price can't be negative."],
        )

    def test_zero_price_is_allowed(self):
        data = self._valid("other", "Free check", price="0")
        self.assertEqual(data["unit_price"], Decimal("0"))

    def test_item_amount_limit(self):
        message = [
            "This item comes to more than ₱999,999.99. Check the quantity and the price."
        ]
        self._valid("other", "Big", "1", "999999.99")
        self._valid("other", "Big", "999", "1001.00")  # ₱999,999.00
        self.assertEqual(
            self._errors("unit_price", "other", "Big", "1", "1000000"), message
        )
        self.assertEqual(
            self._errors("unit_price", "other", "Big", "2", "500000"), message
        )
        self.assertEqual(
            self._errors("unit_price", "other", "Big", "999", "1001.01"), message
        )


@PLAIN_STATIC
class ItemViewTests(FrozenBillingTestBase):

    def test_add_item_from_a_fixed_service(self):
        consult = self._service("Consultation", "300.00")
        receipt = self._empty_receipt()
        response = self._post(
            "admin_billing_item_add",
            receipt.pk,
            {"service_choice": consult.pk, "quantity": "2"},
        )
        self.assertIn("Item added.", self._messages(response))
        item = receipt.billing_items.get()
        self.assertEqual(item.service, consult)
        self.assertEqual(item.description, "Consultation")
        self.assertEqual(item.subtotal, Decimal("600.00"))
        receipt.refresh_from_db()
        self.assertEqual(receipt.total_amount, Decimal("600.00"))

    def test_add_item_with_errors_flashes_them_and_adds_nothing(self):
        receipt = self._empty_receipt()
        response = self._post(
            "admin_billing_item_add",
            receipt.pk,
            {"service_choice": "other", "quantity": "0"},
        )
        self.assertEqual(
            self._messages(response),
            ["Type what it is. The quantity must be from 1 to 999. Type the price."],
        )
        self.assertFalse(receipt.billing_items.exists())

    def test_delete_removes_the_item_and_recalculates(self):
        receipt = self._receipt()
        extra = self._item(receipt, "50.00")
        other = self._receipt()
        self._post("admin_billing_item_delete", extra.pk)
        self.assertFalse(BillingItem.objects.filter(pk=extra.pk).exists())
        receipt.refresh_from_db()
        self.assertEqual(receipt.total_amount, Decimal("200.00"))
        self.assertEqual(other.billing_items.count(), 1)

    def test_delete_of_a_missing_item_is_404(self):
        response = self._post("admin_billing_item_delete", 99999)
        self.assertEqual(response.status_code, 404)

    def test_delete_of_another_receipts_item_is_404(self):
        """Only an item of the receipt that is locked can go: when the
        locked receipt isn't the item's (simulated), it is a 404."""
        receipt = self._receipt()
        other = self._receipt()
        item = receipt.billing_items.get()
        real_lock = billing_views.lock_pending_receipt
        with mock.patch.object(
            billing_views,
            "lock_pending_receipt",
            side_effect=lambda pk: real_lock(other.pk),
        ):
            response = self._post("admin_billing_item_delete", item.pk)
        self.assertEqual(response.status_code, 404)
        self.assertTrue(BillingItem.objects.filter(pk=item.pk).exists())

    def test_items_come_back_in_the_order_added(self):
        """Same created_at (time frozen): the pk breaks the tie."""
        receipt = self._empty_receipt()
        for description in ["Zeta shot", "Alpha tablet", "Mid check"]:
            self._item(receipt, "10.00", description=description)
        expected = ["Zeta shot", "Alpha tablet", "Mid check"]

        for name in ["admin_receipt_detail", "admin_receipt_edit"]:
            with self.subTest(page=name):
                response = self.client.get(reverse(name, args=[receipt.pk]))
                self.assertEqual(
                    [i.description for i in response.context["items"]], expected
                )
                html = response.content.decode()
                self.assertLess(html.index("Zeta shot"), html.index("Alpha tablet"))
                self.assertLess(html.index("Alpha tablet"), html.index("Mid check"))

        self.client.force_login(self.owner.user)
        response = self.client.get(reverse("owner_receipt_detail", args=[receipt.pk]))
        self.assertEqual([i.description for i in response.context["items"]], expected)

    def test_old_pages_still_render(self):
        """The old pages render; their Add item select posts service_choice
        with the form's choices; the edit/new form has no Billing Date or
        Payment Method field any more."""
        receipt = self._receipt()
        consult = self._service("Consultation")
        for name in ["admin_receipt_detail", "admin_receipt_edit"]:
            with self.subTest(page=name):
                response = self.client.get(reverse(name, args=[receipt.pk]))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'name="service_choice"', count=1)
                self.assertContains(
                    response,
                    f'<option value="{consult.pk}">Consultation · ₱300.00</option>',
                )
                self.assertContains(
                    response, '<option value="other">Other (type it)</option>'
                )
        for url in [
            reverse("admin_receipt_edit", args=[receipt.pk]),
            reverse("admin_receipt_create"),
        ]:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, "Billing Date")
                self.assertNotContains(response, 'name="payment_method"')
        self.client.force_login(self.owner.user)
        response = self.client.get(reverse("owner_receipt_detail", args=[receipt.pk]))
        self.assertEqual(response.status_code, 200)


class MarkPaidAndCancelTests(FrozenBillingTestBase):

    def test_no_items_is_refused(self):
        receipt = self._empty_receipt(payment_method="")
        response = self._post("admin_receipt_mark_paid", receipt.pk)
        self.assertRedirects(
            response,
            reverse("admin_receipt_detail", args=[receipt.pk]),
            fetch_redirect_response=False,
        )
        self.assertEqual(self._messages(response), [NO_ITEMS_MESSAGE])
        self.assertEqual(NO_ITEMS_MESSAGE, "Add at least one item first.")
        receipt.refresh_from_db()
        self.assertEqual(receipt.payment_status, BillingReceipt.PENDING)
        self.assertIsNone(receipt.payment_date)
        self.assertEqual(receipt.payment_method, "")
        self.assertEqual(Notification.objects.count(), 0)

    def test_paid_in_cash_on_the_manila_date(self):
        """00:30 Manila on Nov 7 is still Nov 6 in UTC: paid on Nov 7."""
        receipt = self._empty_receipt(payment_method="")
        self._item(receipt, "100.00")
        receipt.calculate_totals()
        self._post("admin_receipt_mark_paid", receipt.pk)
        receipt.refresh_from_db()
        self.assertEqual(receipt.payment_status, BillingReceipt.PAID)
        self.assertEqual(receipt.payment_date, TODAY)
        self.assertEqual(receipt.payment_method, "Cash")
        self.assertEqual(
            Notification.objects.filter(related_billing=receipt).count(), 1
        )

    def test_zero_total_with_items_is_allowed(self):
        receipt = self._empty_receipt()
        self._item(receipt, "0.00", description="Free check")
        receipt.calculate_totals()
        self._post("admin_receipt_mark_paid", receipt.pk)
        receipt.refresh_from_db()
        self.assertEqual(receipt.payment_status, BillingReceipt.PAID)
        self.assertEqual(receipt.total_amount, Decimal("0.00"))

    def test_cancel_saves_only_the_status(self):
        receipt = self._receipt()
        self._post("admin_receipt_mark_cancelled", receipt.pk)
        receipt.refresh_from_db()
        self.assertEqual(receipt.payment_status, BillingReceipt.CANCELLED)
        self.assertIsNone(receipt.payment_date)

    def test_every_write_view_refuses_locked_receipts(self):
        # The redirects aren't followed, so the flashes pile up: one more
        # LOCKED_MESSAGE (and nothing else) per refused post
        refused = 0
        for status in LOCKED_STATUSES:
            receipt = self._receipt(status)
            item = receipt.billing_items.get()
            posts = [
                ("admin_receipt_edit", receipt.pk, self._edit_data()),
                (
                    "admin_billing_item_add",
                    receipt.pk,
                    {"service_choice": "other", "description": "X", "unit_price": "1"},
                ),
                ("admin_billing_item_delete", item.pk, {}),
                ("admin_receipt_mark_paid", receipt.pk, {}),
                ("admin_receipt_mark_cancelled", receipt.pk, {}),
            ]
            for name, pk, data in posts:
                with self.subTest(status=status, view=name):
                    response = self._post(name, pk, data)
                    self.assertRedirects(
                        response,
                        reverse("admin_receipt_detail", args=[receipt.pk]),
                        fetch_redirect_response=False,
                    )
                    refused += 1
                    self.assertEqual(
                        self._messages(response), [LOCKED_MESSAGE] * refused
                    )
            receipt.refresh_from_db()
            self.assertEqual(receipt.payment_status, status)
            self.assertEqual(receipt.discount_value, Decimal("0"))
            self.assertEqual(list(receipt.billing_items.all()), [item])
        self.assertEqual(Notification.objects.count(), 0)


class ReceiptNumberTests(FrozenBillingTestBase):

    def _numbers(self, *numbers):
        for number in numbers:
            BillingReceipt.objects.create(receipt_number=number)

    def test_first_of_the_year(self):
        self.assertEqual(BillingReceipt.generate_receipt_number(), "HV-2026-0001")

    def test_after_0009_is_0010(self):
        self._numbers("HV-2026-0002", "HV-2026-0009")
        self.assertEqual(BillingReceipt.generate_receipt_number(), "HV-2026-0010")

    def test_past_9999(self):
        self._numbers("HV-2026-0500", "HV-2026-9999")
        self.assertEqual(BillingReceipt.generate_receipt_number(), "HV-2026-10000")
        self._numbers("HV-2026-10000")
        self.assertEqual(BillingReceipt.generate_receipt_number(), "HV-2026-10001")

    def test_a_new_year_starts_at_0001(self):
        self._numbers("HV-2025-0042")
        self.assertEqual(BillingReceipt.generate_receipt_number(), "HV-2026-0001")

    def test_junk_suffixes_are_ignored(self):
        self._numbers("HV-2026-0003", "HV-2026-0007-B", "HV-2026-ABC", "HV-2026-")
        self.assertEqual(BillingReceipt.generate_receipt_number(), "HV-2026-0004")

    def test_create_page_numbers_and_dates_a_new_receipt(self):
        """billing_date is today (Manila) and the method Cash, whatever is
        posted. 6a-2: posted to "Which pet?" (?owner=); the view now sets
        billing_date from timezone.localdate(), so the frozen clock reaches
        it and the date is exactly TODAY (was: the model default's real
        timezone.now, checked against the real Manila date)."""
        self._numbers("HV-2026-0009")
        self.client.post(
            f"{reverse('admin_receipt_create')}?owner={self.owner.pk}",
            {
                "pet": self.pet.pk,
                "billing_date": "2020-01-01",
                "payment_method": "GCash",
            },
        )
        receipt = BillingReceipt.objects.get(owner=self.owner)
        self.assertEqual(receipt.receipt_number, "HV-2026-0010")
        self.assertEqual(receipt.billing_date, TODAY)
        self.assertEqual(receipt.payment_method, "Cash")
        self.assertEqual(receipt.payment_status, BillingReceipt.PENDING)

    def test_a_collision_retries_once(self):
        """Someone else takes the number between generating and saving."""
        real = BillingReceipt.generate_receipt_number
        picked = []

        def racing():
            number = real()
            if not picked:
                BillingReceipt.objects.create(receipt_number=number)
            picked.append(number)
            return number

        with mock.patch.object(
            BillingReceipt, "generate_receipt_number", side_effect=racing
        ):
            receipt = save_new_receipt(BillingReceipt(owner=self.owner))
        self.assertEqual(picked, ["HV-2026-0001", "HV-2026-0002"])
        self.assertEqual(receipt.receipt_number, "HV-2026-0002")
        self.assertTrue(BillingReceipt.objects.filter(pk=receipt.pk).exists())

    def test_retries_only_once(self):
        real = BillingReceipt.generate_receipt_number

        def always_taken():
            number = real()
            BillingReceipt.objects.create(receipt_number=number)
            return number

        with mock.patch.object(
            BillingReceipt, "generate_receipt_number", side_effect=always_taken
        ) as generate:
            with self.assertRaises(IntegrityError):
                save_new_receipt(BillingReceipt(owner=self.owner))
        self.assertEqual(generate.call_count, 2)

    def test_other_integrity_errors_are_raised(self):
        with mock.patch.object(
            BillingReceipt, "save", side_effect=IntegrityError("something else")
        ) as save:
            with self.assertRaises(IntegrityError):
                save_new_receipt(BillingReceipt(owner=self.owner))
        self.assertEqual(save.call_count, 1)


# ── Slice 6a-2: New receipt ───────────────────────────────────────────────────

RECEIPT_NEW_FILE = (
    Path(settings.BASE_DIR) / "templates" / "admin" / "billing" / "receipt_new.html"
)


@PLAIN_STATIC
class NewReceiptTestBase(FrozenBillingTestBase):
    """admin_receipt_create at 00:30 Manila on Sat, Nov 7, 2026."""

    def setUp(self):
        super().setUp()
        self.url = reverse("admin_receipt_create")

    def _visit(self, status=Appointment.COMPLETED, day=TODAY, hour=9, **fields):
        fields.setdefault("pet", self.pet)
        return Appointment.objects.create(
            owner=fields["pet"].owner,
            date=day,
            time=datetime.time(hour, 0),
            status=status,
            **fields,
        )

    def _owner(self, first, last, phone=""):
        # No password (no hashing): the paging test makes 29 of them
        username = f"{first}-{last}-{PetOwner.objects.count()}".lower()
        user = User.objects.create(
            username=username, email=f"{username}@example.com", role=User.PET_OWNER
        )
        return PetOwner.objects.create(
            user=user, first_name=first, last_name=last, contact_number=phone
        )

    def _send(self, method, url, data=None):
        """GET url as it is (a data dict would replace its query string),
        or POST data to it."""
        if method == "get":
            return self.client.get(url)
        return self.client.post(url, data or {})

    def _archive(self, obj):
        type(obj).objects.filter(pk=obj.pk).update(is_archived=True)
        obj.refresh_from_db()

    def _flashes(self, response):
        return [(m.level, str(m)) for m in get_messages(response.wsgi_request)]

    def _nothing_saved(self):
        self.assertFalse(BillingReceipt.objects.exists())
        self.assertFalse(BillingItem.objects.exists())

    def _html(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()


class VisitReceiptTests(NewReceiptTestBase):
    """Mode "visit": ?appointment=<pk>."""

    def _url(self, visit):
        return f"{self.url}?appointment={visit.pk}"

    def _check_refused(self, visit, message):
        """GET and POST both go back to the visit with message; nothing
        is saved."""
        for method in ("get", "post"):
            with self.subTest(method=method):
                self.client.cookies.pop("messages", None)
                response = getattr(self.client, method)(self._url(visit))
                self.assertRedirects(
                    response,
                    reverse("admin_appointment_detail", args=[visit.pk]),
                    fetch_redirect_response=False,
                )
                self.assertEqual(
                    self._flashes(response), [(message_levels.ERROR, message)]
                )
                self._nothing_saved()

    def test_bad_and_missing_ids_are_404(self):
        for value in ("abc", "1.5", "1e3", "%20", "999999"):
            for method in ("get", "post"):
                with self.subTest(value=value, method=method):
                    response = getattr(self.client, method)(
                        f"{self.url}?appointment={value}"
                    )
                    self.assertEqual(response.status_code, 404)
        self._nothing_saved()

    def test_a_visit_with_a_receipt_opens_it(self):
        """Pending, paid and cancelled receipts all hold their visit."""
        for hour, status in enumerate(
            (BillingReceipt.PENDING, BillingReceipt.PAID, BillingReceipt.CANCELLED),
            start=9,
        ):
            visit = self._visit(hour=hour)
            receipt = self._empty_receipt(appointment=visit, payment_status=status)
            for method in ("get", "post"):
                with self.subTest(status=status, method=method):
                    self.client.cookies.pop("messages", None)
                    response = self._send(
                        method, self._url(visit), {"add_service": "on"}
                    )
                    self.assertRedirects(
                        response,
                        reverse("admin_receipt_detail", args=[receipt.pk]),
                        fetch_redirect_response=False,
                    )
                    self.assertEqual(
                        self._flashes(response),
                        [(message_levels.INFO, RECEIPT_EXISTS)],
                    )
        self.assertEqual(RECEIPT_EXISTS, "A receipt already exists for this visit.")
        self.assertEqual(BillingReceipt.objects.count(), 3)
        self.assertFalse(BillingItem.objects.exists())

    def test_archived_owner_or_pet_is_refused(self):
        visit = self._visit()
        self._archive(self.pet)
        self._check_refused(visit, "Restore Bantay first.")
        self._archive(self.owner)
        # Both archived: the owner is named first
        self._check_refused(visit, "Restore Ana Cruz first.")
        Pet.objects.filter(pk=self.pet.pk).update(is_archived=False)
        self._check_refused(visit, "Restore Ana Cruz first.")

    def test_pending_cancelled_and_no_show_visits_are_refused(self):
        for hour, status in enumerate(
            (Appointment.PENDING, Appointment.CANCELLED, Appointment.NO_SHOW), start=9
        ):
            with self.subTest(status=status):
                self._check_refused(
                    self._visit(status, hour=hour),
                    "A receipt can only be made for a confirmed or completed visit.",
                )

    def test_a_future_visit_is_refused(self):
        cases = [
            (datetime.date(2026, 11, 8), "Sun, Nov 8"),
            (datetime.date(2027, 1, 5), "Tue, Jan 5, 2027"),
        ]
        for day, shown in cases:
            with self.subTest(day=day):
                self._check_refused(
                    self._visit(Appointment.CONFIRMED, day=day),
                    "This visit hasn't happened yet. "
                    f"Make the receipt on or after {shown}.",
                )

    def test_todays_and_yesterdays_visits_get_a_receipt(self):
        """00:30 Manila on Nov 7 is still Nov 6 in UTC: today's visit is not
        the future, and the receipt is billed on Nov 7."""
        cases = [
            (TODAY, Appointment.CONFIRMED),
            (TODAY, Appointment.COMPLETED),
            (TODAY - datetime.timedelta(days=1), Appointment.CONFIRMED),
            (TODAY - datetime.timedelta(days=1), Appointment.COMPLETED),
        ]
        for number, (day, status) in enumerate(cases, start=1):
            with self.subTest(day=day, status=status):
                visit = self._visit(status, day=day, hour=8 + number)
                page = self.client.get(self._url(visit))
                self.assertEqual(page.status_code, 200)
                self.assertEqual(page.context["mode"], "visit")
                self.client.cookies.pop("messages", None)
                response = self.client.post(self._url(visit))
                receipt = BillingReceipt.objects.get(appointment=visit)
                self.assertRedirects(
                    response,
                    reverse("admin_receipt_detail", args=[receipt.pk]),
                    fetch_redirect_response=False,
                )
                self.assertEqual(
                    self._flashes(response),
                    [
                        (
                            message_levels.SUCCESS,
                            f"Receipt HV-2026-000{number} created.",
                        )
                    ],
                )
                self.assertEqual(receipt.owner, self.owner)
                self.assertEqual(receipt.pet, self.pet)
                self.assertEqual(receipt.billing_date, TODAY)
                utc_date = self.now.astimezone(datetime.timezone.utc).date()
                self.assertNotEqual(receipt.billing_date, utc_date)
                self.assertEqual(receipt.payment_method, "Cash")
                self.assertEqual(receipt.payment_status, BillingReceipt.PENDING)
                self.assertFalse(receipt.billing_items.exists())

    def test_context(self):
        service = self._service("Consultation")
        visit = self._visit(service=service)
        response = self.client.get(self._url(visit))
        context = response.context
        self.assertEqual(context["mode"], "visit")
        self.assertEqual(context["appointment"], visit)
        self.assertEqual(context["visit_service"], service)
        self.assertEqual(
            context["back_url"], reverse("admin_appointment_detail", args=[visit.pk])
        )
        self.assertEqual(context["back_label"], "Back to the visit")
        self.assertContains(response, "For the visit below. Check it, then create")
        self.assertContains(response, "Today, Saturday, November 7 at 9:00 AM")
        self.assertContains(response, "Ana Cruz</a> · No phone number")
        self.assertContains(response, "Bantay</a> · Dog")

    def test_checkbox_only_for_a_fixed_offered_or_clinic_use_service(self):
        cases = [
            (self._service("Consultation"), True),
            (self._service("Deworming", status=Service.UNLISTED), True),
            (self._service("Grooming", status=Service.DRAFT), False),
            (self._service("Boarding", status=Service.CLOSED), False),
            (self._range_service("Surgery", "1000.00", "5000.00"), False),
            (None, False),
        ]
        for hour, (service, shown) in enumerate(cases, start=9):
            name = service.service_name if service else "no service"
            with self.subTest(service=name):
                html = self._html(self._url(self._visit(hour=hour, service=service)))
                self.assertEqual('name="add_service"' in html, shown)
                if shown:
                    self.assertIn(f"Add {name} (₱300.00) as the first item", html)
                    self.assertIn(
                        'name="add_service" value="on" class="mt-0.5 size-6 '
                        'shrink-0 accent-violet-600" checked>',
                        html,
                    )
                    self.assertIn(
                        "You can change its price or remove it on the next page.",
                        html,
                    )
                    self.assertIn("You add the other items on the next page.", html)
                else:
                    self.assertIn(
                        "The receipt opens empty. You add the items on the next page.",
                        html,
                    )
        html = self._html(self._url(self._visit(hour=16)))
        self.assertIn("No service picked", html)

    def test_ticked_adds_the_service_as_the_first_item(self):
        service = self._service("Consultation", price="1250.00")
        visit = self._visit(service=service)
        self.client.post(self._url(visit), {"add_service": "on"})
        receipt = BillingReceipt.objects.get(appointment=visit)
        item = receipt.billing_items.get()
        self.assertEqual(item.service, service)
        self.assertEqual(item.description, "Consultation")
        self.assertEqual(item.quantity, 1)
        self.assertEqual(item.unit_price, Decimal("1250.00"))
        self.assertEqual(receipt.subtotal, Decimal("1250.00"))
        self.assertEqual(receipt.total_amount, Decimal("1250.00"))

    def test_unticked_adds_no_item(self):
        visit = self._visit(service=self._service("Consultation"))
        self.client.post(self._url(visit))
        receipt = BillingReceipt.objects.get(appointment=visit)
        self.assertFalse(receipt.billing_items.exists())
        self.assertEqual(receipt.total_amount, Decimal("0.00"))

    def test_ticked_by_hand_without_a_fixed_service_adds_no_item(self):
        for hour, service in enumerate(
            (self._range_service("Surgery", "1000.00", "5000.00"), None), start=9
        ):
            with self.subTest(service=service):
                visit = self._visit(hour=hour, service=service)
                self.client.post(self._url(visit), {"add_service": "on"})
                receipt = BillingReceipt.objects.get(appointment=visit)
                self.assertFalse(receipt.billing_items.exists())

    def test_two_creates_at_once_open_the_existing_receipt(self):
        # The other save lands after this request's check: the OneToOne
        # raises IntegrityError, which must become a redirect, not a 500,
        # and the first item must not be saved either
        visit = self._visit(service=self._service("Consultation"))
        existing = self._empty_receipt(appointment=visit)
        with mock.patch("billing.utils.visit_receipt", return_value=None):
            response = self.client.post(self._url(visit), {"add_service": "on"})
        self.assertRedirects(
            response,
            reverse("admin_receipt_detail", args=[existing.pk]),
            fetch_redirect_response=False,
        )
        self.assertEqual(
            self._flashes(response), [(message_levels.INFO, RECEIPT_EXISTS)]
        )
        self.assertEqual(list(BillingReceipt.objects.all()), [existing])
        self.assertFalse(BillingItem.objects.exists())


class OwnerStepTests(NewReceiptTestBase):
    """Mode "owner" (step 1, "Who is it for?"): no ?appointment, no ?owner."""

    def _names(self, response):
        return [owner.full_name for owner in response.context["owners"]]

    def test_active_owners_a_to_z(self):
        self._owner("Carmen", "Villanueva", "0998 112 0045")
        self._owner("Ben", "Reyes")
        self._archive(self._owner("Aaron", "Archived"))
        response = self.client.get(self.url)
        self.assertEqual(
            self._names(response), ["Ana Cruz", "Ben Reyes", "Carmen Villanueva"]
        )
        context = response.context
        self.assertEqual(context["mode"], "owner")
        self.assertEqual(context["owner_count"], 3)
        self.assertFalse(context["is_searching"])
        self.assertEqual(context["q"], "")
        self.assertEqual(context["back_url"], reverse("admin_receipt_list"))
        self.assertEqual(context["back_label"], "Back to receipts")
        self.assertTrue(context["empty_topbar_search"])
        self.assertContains(response, "All pet owners (3)")
        self.assertContains(response, "A to Z by first name")
        self.assertContains(response, "No phone number")
        self.assertContains(response, "0998 112 0045")
        self.assertContains(response, "<li>Bantay</li>")
        self.assertContains(response, "No pets yet")
        choose = f'<a href="{self.url}?owner={self.owner.pk}" class="btn btn-outline'
        self.assertContains(response, choose)
        self.assertContains(response, 'href="' + reverse("admin_owner_create") + '"')

    def test_search_every_word(self):
        other = self._owner("Liza", "Santos", "0917 123 4567")
        Pet.objects.create(owner=other, name="Choco", species="Dog")
        Pet.objects.create(owner=other, name="Ghost", species="Cat", is_archived=True)
        cases = [
            ("ana", ["Ana Cruz"]),
            ("SANTOS", ["Liza Santos"]),
            ("0917", ["Liza Santos"]),
            ("bantay", ["Ana Cruz"]),
            ("ana bantay", ["Ana Cruz"]),
            ("liza bantay", []),
            ("ghost", []),
        ]
        for q, expected in cases:
            with self.subTest(q=q):
                response = self.client.get(self.url, {"q": q})
                self.assertEqual(self._names(response), expected)
                self.assertTrue(response.context["is_searching"])
                self.assertIsNone(response.context["page_obj"])
                self.assertContains(response, "Clear search")

    def test_search_results_line_and_order(self):
        self._owner("Liza", "Cruz")
        response = self.client.get(self.url, {"q": "cruz"})
        self.assertEqual(self._names(response), ["Ana Cruz", "Liza Cruz"])
        self.assertContains(response, "2 owners match “cruz”")
        self.assertNotContains(response, "All pet owners")

    def test_archived_owners_are_not_found(self):
        self._archive(self._owner("Liza", "Cruz"))
        response = self.client.get(self.url, {"q": "liza"})
        self.assertEqual(self._names(response), [])

    def test_25_per_page(self):
        for number in range(1, 30):
            self._owner(f"Owner{number:02d}", "Test")
        response = self.client.get(self.url)
        self.assertEqual(len(response.context["owners"]), 25)
        self.assertEqual(response.context["owner_count"], 30)
        self.assertEqual(response.context["prev_page_query"], "")
        self.assertEqual(response.context["next_page_query"], "?page=2")
        self.assertContains(response, "Showing 1–25 of 30")
        page_2 = self.client.get(self.url, {"page": "2"})
        self.assertEqual(len(page_2.context["owners"]), 5)
        self.assertEqual(page_2.context["prev_page_query"], "?page=1")
        self.assertEqual(page_2.context["next_page_query"], "")
        self.assertEqual(self._names(page_2)[-1], "Owner29 Test")

    def test_empty_states(self):
        html = self._html(f"{self.url}?q=zzz")
        self.assertIn("No owner matches “zzz”", html)
        self.assertIn("Check the spelling, or add them with Add owner.", html)
        self._archive(self.owner)
        html = self._html(self.url)
        self.assertIn("No pet owners yet", html)
        self.assertIn("All pet owners (0)", html)

    def test_top_bar_search_is_empty_on_this_step_only(self):
        self.assertNotIn('id="global-search"', self._html(self.url))
        self.assertNotIn('id="global-search"', self._html(f"{self.url}?q=ana"))
        owner_page = f"{self.url}?owner={self.owner.pk}"
        self.assertIn('id="global-search"', self._html(owner_page))


class PetStepTests(NewReceiptTestBase):
    """Mode "pet" (step 2, "Which pet?"): ?owner=<pk>."""

    def setUp(self):
        super().setUp()
        self.owner_url = f"{self.url}?owner={self.owner.pk}"

    def test_bad_and_missing_owner_ids_are_404(self):
        for value in ("abc", "1.5", "%20", "999999"):
            for method in ("get", "post"):
                with self.subTest(value=value, method=method):
                    response = self._send(
                        method, f"{self.url}?owner={value}", {"pet": "none"}
                    )
                    self.assertEqual(response.status_code, 404)
        self._nothing_saved()

    def test_archived_owner_goes_back_to_step_1(self):
        self._archive(self.owner)
        for method in ("get", "post"):
            with self.subTest(method=method):
                self.client.cookies.pop("messages", None)
                response = self._send(method, self.owner_url, {"pet": "none"})
                self.assertRedirects(response, self.url, fetch_redirect_response=False)
                self.assertEqual(
                    self._flashes(response),
                    [(message_levels.ERROR, "Restore Ana Cruz first.")],
                )
        self._nothing_saved()

    def test_context(self):
        response = self.client.get(self.owner_url)
        context = response.context
        self.assertEqual(context["mode"], "pet")
        self.assertEqual(context["owner"], self.owner)
        self.assertEqual(context["pets"], [self.pet])
        self.assertEqual(context["pet_error"], "")
        self.assertEqual(context["back_url"], reverse("admin_receipt_list"))
        self.assertEqual(context["back_label"], "Back to receipts")
        self.assertEqual(context["change_owner_url"], self.url)
        self.assertContains(response, "Billed to")
        self.assertContains(response, "Change owner")
        self.assertContains(response, "Which pet is it for?")
        self.assertContains(response, "Not for a specific pet")
        self.assertContains(response, "Supplies or items not for one pet")

    def test_one_pet_is_picked(self):
        response = self.client.get(self.owner_url)
        self.assertEqual(response.context["picked"], str(self.pet.pk))
        self.assertContains(
            response,
            f'value="{self.pet.pk}" class="size-5 shrink-0 accent-violet-600" '
            "required checked>",
        )

    def test_two_pets_nothing_picked_archived_left_out(self):
        choco = Pet.objects.create(
            owner=self.owner,
            name="Choco",
            species="dog",
            breed="Shih Tzu",
            date_of_birth=datetime.date(2023, 11, 1),
        )
        Pet.objects.create(
            owner=self.owner, name="Ghost", species="Cat", is_archived=True
        )
        response = self.client.get(self.owner_url)
        self.assertEqual(response.context["picked"], "")
        self.assertEqual(response.context["pets"], [self.pet, choco])
        self.assertNotContains(response, " checked>")
        self.assertNotContains(response, "Ghost")
        # Species · Breed · age, empty parts left out
        lines = [pet.detail_line for pet in response.context["pets"]]
        self.assertEqual(lines, ["Dog", "Dog · Shih Tzu · 3 yrs"])

    def test_no_pets_picks_not_for_a_specific_pet(self):
        owner = self._owner("Jun", "Reyes")
        response = self.client.get(f"{self.url}?owner={owner.pk}")
        self.assertEqual(response.context["picked"], "none")
        self.assertContains(
            response,
            'value="none" class="size-5 shrink-0 accent-violet-600" required checked>',
        )
        self.assertContains(
            response,
            "Jun Reyes has no pets yet. To bill for a pet, add it on "
            f'<a href="{reverse("admin_owner_detail", args=[owner.pk])}"',
        )
        # The apostrophe is template text here (not escaped)
        self.assertContains(response, "Jun Reyes's page</a> first.")

    def test_nothing_picked_is_refused(self):
        Pet.objects.create(owner=self.owner, name="Choco", species="Dog")
        for data in ({}, {"pet": ""}, {"pet": "  "}):
            with self.subTest(data=data):
                response = self.client.post(self.owner_url, data)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["pet_error"], PICK_PET_MESSAGE)
                self.assertEqual(response.context["picked"], "")
                self.assertContains(response, PICK_PET_MESSAGE)
                self.assertContains(response, "Please fix the 1 item below.")
        self.assertEqual(PICK_PET_MESSAGE, "Choose a pet, or “Not for a specific pet”.")
        self._nothing_saved()

    def test_another_owners_or_an_archived_pet_is_refused(self):
        other = self._owner("Liza", "Santos")
        others_pet = Pet.objects.create(owner=other, name="Choco", species="Dog")
        archived = Pet.objects.create(
            owner=self.owner, name="Ghost", species="Cat", is_archived=True
        )
        for pet in (others_pet, archived):
            with self.subTest(pet=pet.name):
                response = self.client.post(self.owner_url, {"pet": pet.pk})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    response.context["pet_error"], "Choose one of Ana Cruz's pets."
                )
                self.assertEqual(response.context["picked"], "")
                self.assertContains(response, "Choose one of Ana Cruz&#x27;s pets.")
        self._nothing_saved()

    def test_a_pet_that_isnt_a_whole_number_or_doesnt_exist_is_404(self):
        for value in ("abc", "1.5", "999999"):
            with self.subTest(value=value):
                response = self.client.post(self.owner_url, {"pet": value})
                self.assertEqual(response.status_code, 404)
        self._nothing_saved()

    def _check_created(self, response, pet):
        receipt = BillingReceipt.objects.get()
        self.assertRedirects(
            response,
            reverse("admin_receipt_detail", args=[receipt.pk]),
            fetch_redirect_response=False,
        )
        self.assertEqual(
            self._flashes(response),
            [(message_levels.SUCCESS, "Receipt HV-2026-0001 created.")],
        )
        self.assertEqual(receipt.owner, self.owner)
        self.assertEqual(receipt.pet, pet)
        self.assertIsNone(receipt.appointment)
        self.assertEqual(receipt.billing_date, TODAY)
        self.assertEqual(receipt.payment_method, "Cash")
        self.assertEqual(receipt.payment_status, BillingReceipt.PENDING)
        self.assertFalse(receipt.billing_items.exists())

    def test_not_for_a_specific_pet_creates_a_receipt_without_a_pet(self):
        self._check_created(self.client.post(self.owner_url, {"pet": "none"}), None)

    def test_a_pet_creates_a_receipt_for_that_pet(self):
        self._check_created(
            self.client.post(self.owner_url, {"pet": self.pet.pk}), self.pet
        )


class NewReceiptPageTests(NewReceiptTestBase):
    """The structural template (design pass is 6b-3)."""

    def _pages(self):
        visit = self._visit(service=self._service("Consultation"))
        return {
            "visit": f"{self.url}?appointment={visit.pk}",
            "owner": self.url,
            "pet": f"{self.url}?owner={self.owner.pk}",
        }

    def test_layout_title_and_one_primary_button(self):
        primary = {"visit": 1, "owner": 0, "pet": 1}
        for mode, url in self._pages().items():
            with self.subTest(mode=mode):
                response = self.client.get(url)
                self.assertTemplateUsed(response, "layouts/admin.html")
                self.assertTemplateUsed(response, "admin/billing/receipt_new.html")
                html = response.content.decode()
                self.assertNotIn("layer(legacy)", html)
                self.assertIn(
                    '<h1 class="text-2xl font-bold text-gray-900">New receipt</h1>',
                    html,
                )
                self.assertEqual(html.count("btn-primary"), primary[mode])
                if primary[mode]:
                    self.assertIn("Create receipt</button>", html)
                    self.assertIn(">Cancel</a>", html)

    def test_subtitles_and_steps(self):
        pages = self._pages()
        html = self._html(pages["visit"])
        self.assertIn("For the visit below. Check it, then create the receipt.", html)
        self.assertNotIn('aria-label="Steps"', html)
        for mode in ("owner", "pet"):
            with self.subTest(mode=mode):
                html = self._html(pages[mode])
                self.assertIn(
                    "Not from a visit, for example supplies bought over the counter.",
                    html,
                )
                self.assertIn('aria-label="Steps"', html)
                self.assertIn("Who is it for?", html)
                self.assertIn("Which pet?", html)

    def test_owner_search_form(self):
        html = self._html(self.url)
        self.assertIn(
            f'<form method="get" action="{self.url}" class="mb-3 flex flex-wrap '
            'items-end gap-3" role="search">',
            html,
        )
        self.assertIn('<label for="receipt-owner-q" ', html)
        self.assertIn(">Find the owner</label>", html)
        self.assertIn('placeholder="Owner name, phone or pet name"', html)

    def test_sidebar_highlights_billing(self):
        for mode, url in self._pages().items():
            with self.subTest(mode=mode):
                html = self._html(url)
                active = re.findall(r'<a href="([^"]*)"[^>]*aria-current="page">', html)
                self.assertEqual(active, [reverse("admin_receipt_list")])

    def test_every_link_is_on_one_line(self):
        source = RECEIPT_NEW_FILE.read_text(encoding="utf-8")
        links = re.findall(r"(?s)<a\b.*?</a>", source)
        self.assertTrue(links)
        for link in links:
            with self.subTest(link=link[:60]):
                self.assertNotIn("\n", link)
