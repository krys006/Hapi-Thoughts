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
from django.core import mail
from django.db import IntegrityError, connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from appointments.models import Appointment
from billing import views as billing_views
from billing.forms import BillingItemForm, BillingReceiptForm, ServiceForm
from billing.models import BillingItem, BillingReceipt, Service
from billing.utils import (
    NO_ITEMS_MESSAGE,
    PICK_PET_MESSAGE,
    RECEIPT_EXISTS,
    RECEIPT_TABS,
    notify_receipt_paid,
    receipt_paid_message,
    save_new_receipt,
    services_list_context,
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
        """GET edit on a locked receipt redirects to the receipt page.
        6a-3: the edit page is gone, so a GET just opens the receipt page
        with no flash (was: LOCKED_MESSAGE flashed), like the other action
        URLs since 6a-1."""
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
                self.assertEqual(self._messages(response), [])

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
        # 6a-3: the receipt page with the Discount dialog open (was the edit
        # page, whose form was context["form"])
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "admin/billing/receipt_detail.html")
        self.assertEqual(response.context["open_dialog"], "discount")
        self.assertEqual(
            response.context["discount_form"].errors["discount_value"],
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

    def test_add_item_with_errors_reopens_the_dialog_and_adds_nothing(self):
        """6a-3: the receipt page is re-rendered with the bound form and the
        Add item dialog open, no flash (was: a redirect flashing "Type what
        it is. The quantity must be from 1 to 999. Type the price.")."""
        receipt = self._empty_receipt()
        response = self._post(
            "admin_billing_item_add",
            receipt.pk,
            {"service_choice": "other", "quantity": "0"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["open_dialog"], "add-item")
        form = response.context["item_form"]
        self.assertEqual(form.errors["description"], ["Type what it is."])
        self.assertEqual(
            form.errors["quantity"], ["The quantity must be from 1 to 999."]
        )
        self.assertEqual(form.errors["unit_price"], ["Type the price."])
        self.assertEqual(self._messages(response), [])
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

        # 6a-3: the edit URL now redirects to the receipt page (followed)
        for name in ["admin_receipt_detail", "admin_receipt_edit"]:
            with self.subTest(page=name):
                response = self.client.get(
                    reverse(name, args=[receipt.pk]), follow=True
                )
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
        # 6a-3: the edit URL redirects to the receipt page (followed below)
        for name in ["admin_receipt_detail", "admin_receipt_edit"]:
            with self.subTest(page=name):
                response = self.client.get(
                    reverse(name, args=[receipt.pk]), follow=True
                )
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
                response = self.client.get(url, follow=True)
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


# ── Slice 6a-3: receipt list and receipt page contexts ────────────────────────

# Whole-page query counts, measured: session + user + the admin layout's
# own (4 on these pages) + the page's. List: counts aggregate, paginator
# count, the page. Receipt page: receipt (owner, user, pet, visit), items
# (with service), clinic settings, service options, the item form's services.
LIST_QUERIES = 9
DETAIL_QUERIES = 11


@PLAIN_STATIC
class ReceiptListTests(FrozenBillingTestBase):
    """admin_receipt_list: tabs, order, search, counts, pages."""

    def setUp(self):
        super().setUp()
        self.url = reverse("admin_receipt_list")
        ben_user = User.objects.create(
            username="ben", email="ben@example.com", role=User.PET_OWNER
        )
        self.ben = PetOwner.objects.create(
            user=ben_user, first_name="Ben", last_name="Reyes"
        )
        self.luna = Pet.objects.create(owner=self.ben, name="Luna", species="Cat")

    def _make(self, number, status=BillingReceipt.PENDING, hours_ago=0, **fields):
        """A receipt created hours_ago before now (Ana and Bantay unless
        owner / pet are given)."""
        fields.setdefault("owner", self.owner)
        fields.setdefault("pet", self.pet)
        receipt = BillingReceipt.objects.create(
            receipt_number=number, payment_status=status, **fields
        )
        created = self.now - datetime.timedelta(hours=hours_ago)
        BillingReceipt.objects.filter(pk=receipt.pk).update(created_at=created)
        return receipt

    def _paid(self, number, day, hours_ago):
        return self._make(
            number, BillingReceipt.PAID, hours_ago, payment_date=datetime.date(*day)
        )

    def _context(self, query=""):
        response = self.client.get(f"{self.url}{query}")
        self.assertEqual(response.status_code, 200)
        return response.context

    def _numbers(self, context):
        return [r.receipt_number for r in context["receipts"]]

    def _tabs_fixture(self):
        """Pending, paid and cancelled receipts with created_at ties (the pk
        breaks them)."""
        made = {}
        for key, hours in [("p1", 5), ("p2", 1), ("p3", 1)]:
            made[key] = self._make(f"P-{key}", hours_ago=hours)
        made["a"] = self._paid("PD-a", (2026, 11, 5), 1)
        made["b"] = self._paid("PD-b", (2026, 11, 6), 6)
        made["c"] = self._paid("PD-c", (2026, 11, 6), 2)
        made["d"] = self._paid("PD-d", (2026, 11, 6), 2)
        for key in ("x", "y"):
            made[key] = self._make(f"C-{key}", BillingReceipt.CANCELLED, 3)
        return made

    def test_default_and_unknown_status_is_pending(self):
        made = self._tabs_fixture()
        for query in [
            "",
            "?status=",
            "?status=junk",
            "?status=PAID",
            "?status=pending",
        ]:
            with self.subTest(query=query):
                context = self._context(query)
                self.assertEqual(context["active_tab"], "pending")
                self.assertEqual(
                    self._numbers(context),
                    [made[key].receipt_number for key in ("p1", "p2", "p3")],
                )

    def test_each_tab_rows_and_order(self):
        made = self._tabs_fixture()
        expected = {
            # Oldest first; p2 / p3 tie on created_at → pk
            "pending": ["p1", "p2", "p3"],
            # Newest paid first; c / d tie on both dates → -pk
            "paid": ["d", "c", "b", "a"],
            "cancelled": ["y", "x"],
            "all": ["a", "p3", "p2", "d", "c", "y", "x", "p1", "b"],
        }
        for tab, keys in expected.items():
            with self.subTest(tab=tab):
                context = self._context(f"?status={tab}")
                self.assertEqual(context["active_tab"], tab)
                self.assertEqual(
                    self._numbers(context), [made[k].receipt_number for k in keys]
                )

    def test_counts_in_context(self):
        self._tabs_fixture()
        context = self._context()
        self.assertEqual(
            [context[f"{name}_count"] for name in RECEIPT_TABS], [3, 4, 2, 9]
        )

    def _search_fixture(self):
        self._make("HV-2026-0012")
        self._make("HV-2026-0120", owner=self.ben, pet=self.luna)
        self._make(
            "HV-2026-0003",
            BillingReceipt.PAID,
            pet=None,
            payment_date=TODAY,
        )

    def test_search(self):
        self._search_fixture()
        cases = {
            "0012": ["HV-2026-0012"],
            "HV-2026-0012": ["HV-2026-0012"],
            "hv-2026-0120": ["HV-2026-0120"],
            "ana": ["HV-2026-0012"],
            "CRUZ": ["HV-2026-0012"],
            "ben": ["HV-2026-0120"],
            "reyes": ["HV-2026-0120"],
            "luna": ["HV-2026-0120"],
            "bantay": ["HV-2026-0012"],
            "ana  bantay": ["HV-2026-0012"],
            "ana luna": [],
            "nobody": [],
        }
        for q, numbers in cases.items():
            with self.subTest(q=q):
                context = self.client.get(self.url, {"q": q}).context
                self.assertEqual(self._numbers(context), numbers)
                self.assertEqual(context["q"], " ".join(q.split()))
                self.assertTrue(context["is_searching"])

    def test_search_stays_in_the_tab_and_counts_ignore_it(self):
        self._search_fixture()
        cases = [
            ("paid", ["HV-2026-0003"]),
            ("pending", ["HV-2026-0012"]),
            ("cancelled", []),
            ("all", ["HV-2026-0003", "HV-2026-0012"]),
        ]
        for tab, numbers in cases:
            with self.subTest(tab=tab):
                context = self.client.get(self.url, {"status": tab, "q": "ana"}).context
                self.assertEqual(sorted(self._numbers(context)), numbers)
                self.assertEqual(
                    [context[f"{name}_count"] for name in RECEIPT_TABS], [2, 1, 0, 3]
                )
        context = self._context()
        self.assertEqual(context["q"], "")
        self.assertFalse(context["is_searching"])

    def test_paging_keeps_status_and_q(self):
        for number in range(27):
            self._make(f"HV-2026-{number + 1:04d}")
        context = self._context()
        self.assertEqual(len(context["receipts"]), 25)
        self.assertEqual(context["page_obj"].paginator.count, 27)
        self.assertEqual(context["prev_page_query"], "")
        self.assertEqual(context["next_page_query"], "?status=pending&page=2")

        context = self.client.get(
            self.url, {"status": "all", "q": "ana  cruz", "page": "2"}
        ).context
        self.assertEqual(len(context["receipts"]), 2)
        self.assertEqual(context["prev_page_query"], "?status=all&q=ana+cruz&page=1")
        self.assertEqual(context["next_page_query"], "")
        # Junk and out-of-range pages: get_page's first / last page
        self.assertEqual(self._context("?page=abc")["page_obj"].number, 1)
        self.assertEqual(self._context("?page=99")["page_obj"].number, 2)

    def test_item_count(self):
        empty = self._make("HV-2026-0001", hours_ago=2)
        full = self._make("HV-2026-0002", hours_ago=1)
        for _ in range(3):
            self._item(full, "10.00")
        rows = {r.pk: r.item_count for r in self._context()["receipts"]}
        self.assertEqual(rows, {empty.pk: 0, full.pk: 3})

    def test_old_template_still_renders(self):
        self._tabs_fixture()
        response = self.client.get(self.url)
        self.assertTemplateUsed(response, "admin/billing/receipt_list.html")
        context = response.context
        self.assertEqual(context["status_filter"], "pending")
        self.assertEqual(
            context["counts"], {"all": 9, "pending": 3, "paid": 4, "cancelled": 2}
        )
        self.assertContains(
            response,
            'class="filter-tab filter-tab--active">Pending '
            '<span class="filter-count">3</span>',
        )
        self.assertContains(response, "P-p1")
        self.assertNotContains(response, "PD-a")
        self.assertEqual(self._context("?status=all")["status_filter"], "")
        self.assertEqual(self._context("?status=paid")["status_filter"], "paid")
        self.assertContains(self.client.get(self.url, {"status": "all"}), "PD-a")

    def test_context_keys(self):
        context = self._context()
        for key in [
            "page_obj",
            "receipts",
            "q",
            "is_searching",
            "active_tab",
            "pending_count",
            "paid_count",
            "cancelled_count",
            "all_count",
            "prev_page_query",
            "next_page_query",
            "today",
        ]:
            with self.subTest(key=key):
                self.assertIn(key, context)
        self.assertEqual(context["today"], TODAY)

    def _queries_for(self, count):
        BillingReceipt.objects.all().delete()
        for number in range(count):
            receipt = self._make(f"HV-2026-{number + 1:04d}", appointment=None)
            self._item(receipt, "10.00")
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(self.url, {"status": "all"})
        self.assertEqual(len(response.context["receipts"]), min(count, 25))
        return len(queries)

    def test_query_count_is_flat(self):
        few, many = self._queries_for(3), self._queries_for(30)
        self.assertEqual(few, many)
        self.assertEqual(many, LIST_QUERIES)


@PLAIN_STATIC
class ReceiptDetailContextTests(FrozenBillingTestBase):
    """receipt_detail_context through admin_receipt_detail."""

    def _context(self, receipt):
        response = self.client.get(reverse("admin_receipt_detail", args=[receipt.pk]))
        self.assertEqual(response.status_code, 200)
        return response.context

    def _two_items(self, **fields):
        """₱200.00 (from a service) and ₱100.00 (Other), with the totals."""
        receipt = self._empty_receipt(**fields)
        consult = self._service("Consultation", "200.00")
        self._item(receipt, "200.00", description="Consultation", service=consult)
        self._item(receipt, "100.00", description="Dog food")
        receipt.calculate_totals()
        return receipt

    def test_every_key(self):
        visit = Appointment.objects.create(
            owner=self.owner,
            pet=self.pet,
            date=TODAY,
            time=datetime.time(9, 30),
            status=Appointment.COMPLETED,
        )
        PetOwner.objects.filter(pk=self.owner.pk).update(contact_number="0917 123 4567")
        self.owner.refresh_from_db()
        receipt = self._two_items(appointment=visit)
        context = self._context(receipt)
        self.assertEqual(context["receipt"], receipt)
        self.assertEqual(context["status_label"], "Unpaid")
        self.assertTrue(context["is_pending"])
        self.assertTrue(context["can_mark_paid"])
        self.assertEqual(
            [i.description for i in context["items"]], ["Consultation", "Dog food"]
        )
        self.assertEqual(context["item_count"], 2)
        first, second = context["items"]
        self.assertEqual(first.amount, Decimal("200.00"))
        self.assertTrue(first.from_price_list)
        self.assertFalse(second.from_price_list)
        self.assertEqual(context["subtotal"], Decimal("300.00"))
        self.assertEqual(context["discount_amount"], Decimal("0.00"))
        self.assertFalse(context["has_discount"])
        self.assertEqual(context["discount_label"], "")
        self.assertEqual(context["total"], Decimal("300.00"))
        self.assertEqual(context["owner"], self.owner)
        self.assertTrue(context["owner_has_email"])
        self.assertEqual(context["owner_phone"], "0917 123 4567")
        self.assertEqual(context["pet"], self.pet)
        self.assertEqual(context["appointment"], visit)
        self.assertEqual(context["appointment"].date, TODAY)
        self.assertEqual(context["appointment"].time, datetime.time(9, 30))
        self.assertEqual(context["created_at"], receipt.created_at)
        self.assertIsNone(context["payment_date"])
        self.assertIn("clinic_settings", context)
        self.assertEqual(context["today"], TODAY)
        self.assertIsNone(context["open_dialog"])
        self.assertFalse(context["item_form"].is_bound)
        self.assertFalse(context["discount_form"].is_bound)

    def test_no_pet_no_visit_no_email(self):
        User.objects.filter(pk=self.owner.user.pk).update(email="")
        receipt = BillingReceipt.objects.create(
            receipt_number=BillingReceipt.generate_receipt_number(), owner=self.owner
        )
        context = self._context(receipt)
        self.assertIsNone(context["pet"])
        self.assertIsNone(context["appointment"])
        self.assertFalse(context["owner_has_email"])
        self.assertEqual(context["owner_phone"], "")
        self.assertEqual(context["items"], [])
        self.assertEqual(context["item_count"], 0)

    def test_status_labels_and_can_mark_paid(self):
        empty = self._empty_receipt()
        context = self._context(empty)
        self.assertTrue(context["is_pending"])
        self.assertFalse(context["can_mark_paid"])  # no items
        cases = [
            (BillingReceipt.PENDING, "Unpaid", True, True),
            (BillingReceipt.PAID, "Paid", False, False),
            (BillingReceipt.CANCELLED, "Cancelled", False, False),
        ]
        for status, label, is_pending, can_mark_paid in cases:
            with self.subTest(status=status):
                context = self._context(self._receipt(status))
                self.assertEqual(context["status_label"], label)
                self.assertEqual(context["is_pending"], is_pending)
                self.assertEqual(context["can_mark_paid"], can_mark_paid)
        paid = self._receipt(BillingReceipt.PAID)
        self.assertEqual(self._context(paid)["payment_date"], TODAY)

    def _removed_totals(self, receipt):
        return [i.total_if_removed for i in self._context(receipt)["items"]]

    def test_total_if_removed_with_no_discount(self):
        receipt = self._two_items()
        self.assertEqual(
            self._removed_totals(receipt), [Decimal("100.00"), Decimal("200.00")]
        )

    def test_total_if_removed_with_a_percentage(self):
        """10% off ₱300.00 is ₱270.00; without the ₱200.00 item 10% of
        ₱100.00 (₱90.00), without the ₱100.00 one 10% of ₱200.00."""
        receipt = self._two_items(
            discount_type=BillingReceipt.PERCENTAGE, discount_value=Decimal("10")
        )
        receipt.calculate_totals()
        self.assertEqual(self._context(receipt)["total"], Decimal("270.00"))
        self.assertEqual(
            self._removed_totals(receipt), [Decimal("90.00"), Decimal("180.00")]
        )

    def test_total_if_removed_rounds_half_up(self):
        """12.5% of ₱0.20 is ₱0.025 → ₱0.03: ₱0.17 left."""
        receipt = self._empty_receipt(
            discount_type=BillingReceipt.PERCENTAGE, discount_value=Decimal("12.5")
        )
        self._item(receipt, "10.00")
        self._item(receipt, "0.20")
        receipt.calculate_totals()
        self.assertEqual(self._removed_totals(receipt)[0], Decimal("0.17"))

    def test_total_if_removed_with_a_capped_peso_discount(self):
        """₱250 off ₱300.00 is ₱50.00; without either item the discount is
        capped at what is left, so ₱0.00."""
        receipt = self._two_items(
            discount_type=BillingReceipt.FIXED_DISCOUNT, discount_value=Decimal("250")
        )
        receipt.calculate_totals()
        context = self._context(receipt)
        self.assertEqual(context["total"], Decimal("50.00"))
        self.assertEqual(
            [i.total_if_removed for i in context["items"]],
            [Decimal("0.00"), Decimal("0.00")],
        )
        receipt.discount_value = Decimal("60")
        receipt.save()
        receipt.calculate_totals()
        self.assertEqual(
            self._removed_totals(receipt), [Decimal("40.00"), Decimal("140.00")]
        )

    def test_total_if_removed_matches_calculate_totals(self):
        """What the Remove dialog says is what the total becomes."""
        receipt = self._two_items(
            discount_type=BillingReceipt.PERCENTAGE, discount_value=Decimal("33.33")
        )
        receipt.calculate_totals()
        first = self._context(receipt)["items"][0]
        self._post("admin_billing_item_delete", first.pk)
        receipt.refresh_from_db()
        self.assertEqual(receipt.total_amount, first.total_if_removed)

    def test_discount_label(self):
        """6a-4: a peso discount's label has no amount (the amount column
        shows the capped amount): its note or "". Was "₱50.00" and
        "₱1,250.00 · Loyal client". Percentages unchanged."""
        receipt = self._two_items()
        cases = [
            (BillingReceipt.PERCENTAGE, "10", "", "10%", "₱30.00"),
            (BillingReceipt.PERCENTAGE, "12.5", "Senior", "12.5% · Senior", "₱37.50"),
            (BillingReceipt.FIXED_DISCOUNT, "50", "", "", "₱50.00"),
            (
                BillingReceipt.FIXED_DISCOUNT,
                "1250",
                "Loyal client",
                "Loyal client",
                "₱300.00",
            ),
        ]
        for kind, value, note, label, amount in cases:
            with self.subTest(kind=kind, value=value, note=note):
                receipt.discount_type = kind
                receipt.discount_value = Decimal(value)
                receipt.discount_note = note
                receipt.save()
                receipt.calculate_totals()
                context = self._context(receipt)
                self.assertEqual(context["discount_label"], label)
                self.assertTrue(context["has_discount"])
                self.assertEqual(f"₱{context['discount_amount']:,.2f}", amount)

    def test_service_options_match_the_form(self):
        fixed = self._service("Consultation", "150.00")
        ranged = self._range_service("grooming", "300.00", "800.00", Service.UNLISTED)
        self._service("Bath", status=Service.DRAFT)
        self._service("X-ray", status=Service.CLOSED)
        context = self._context(self._empty_receipt())
        options = context["service_options"]
        choices = list(context["item_form"].fields["service_choice"].choices)
        self.assertEqual([(o["value"], o["label"]) for o in options], choices[1:-1])
        self.assertEqual(
            options,
            [
                {
                    "value": str(fixed.pk),
                    "label": "Consultation · ₱150.00",
                    "name": "Consultation",
                    "pricing": "fixed",
                    "price": Decimal("150.00"),
                    "min_price": None,
                    "max_price": None,
                },
                {
                    "value": str(ranged.pk),
                    "label": "grooming · ₱300.00 to ₱800.00",
                    "name": "grooming",
                    "pricing": "range",
                    "price": None,
                    "min_price": Decimal("300.00"),
                    "max_price": Decimal("800.00"),
                },
            ],
        )

    def _queries_for(self, item_count):
        receipt = self._empty_receipt()
        visit = Appointment.objects.create(
            owner=self.owner,
            pet=self.pet,
            date=TODAY,
            time=datetime.time(8 + item_count // 5, 0),
            status=Appointment.COMPLETED,
        )
        BillingReceipt.objects.filter(pk=receipt.pk).update(appointment=visit)
        for number in range(item_count):
            service = self._service(f"Service {item_count}-{number}")
            self._item(receipt, "10.00", service=service)
        receipt.calculate_totals()
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(
                reverse("admin_receipt_detail", args=[receipt.pk])
            )
        self.assertEqual(response.context["item_count"], item_count)
        return len(queries)

    def test_query_count_is_flat(self):
        none, ten = self._queries_for(0), self._queries_for(10)
        self.assertEqual(none, ten)
        self.assertEqual(ten, DETAIL_QUERIES)


class PriceHelpTests(FrozenBillingTestBase):
    """Rule 11: BillingItemForm.price_help for the picked service."""

    def test_price_help(self):
        fixed = self._service("Consultation", "150.00")
        ranged = self._range_service("Grooming", "300.00", "800.00")
        draft = self._service("Bath", status=Service.DRAFT)
        cases = [
            (fixed.pk, "Price list: ₱150.00."),
            (str(fixed.pk), "Price list: ₱150.00."),
            (ranged.pk, "Usual price ₱300.00 to ₱800.00. You can bill outside it."),
            ("other", ""),
            ("", ""),
            ("abc", ""),
            (draft.pk, ""),
        ]
        for choice, help_text in cases:
            with self.subTest(choice=choice):
                form = BillingItemForm({"service_choice": choice})
                self.assertEqual(form.price_help, help_text)
        self.assertEqual(BillingItemForm({}).price_help, "")
        self.assertEqual(BillingItemForm().price_help, "")
        unbound = BillingItemForm(initial={"service_choice": fixed.pk})
        self.assertEqual(unbound.price_help, "")


@PLAIN_STATIC
class ReceiptDialogTests(FrozenBillingTestBase):
    """Item add and discount errors re-render the receipt page with the
    dialog open; the edit URL is the discount endpoint."""

    def setUp(self):
        super().setUp()
        self.receipt = self._receipt()  # one ₱200.00 item
        self.grooming = self._range_service("Grooming", "300.00", "800.00")

    def test_invalid_item_add_reopens_the_dialog_with_the_typed_values(self):
        data = {
            "service_choice": str(self.grooming.pk),
            "description": "Big dog groom",
            "quantity": "2",
            "unit_price": "",
        }
        response = self._post("admin_billing_item_add", self.receipt.pk, data)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "admin/billing/receipt_detail.html")
        self.assertEqual(response.context["open_dialog"], "add-item")
        form = response.context["item_form"]
        self.assertTrue(form.is_bound)
        self.assertEqual(form.errors, {"unit_price": ["Type the price."]})
        for name, value in data.items():
            with self.subTest(field=name):
                self.assertEqual(form[name].value(), value)
        self.assertEqual(
            form.price_help, "Usual price ₱300.00 to ₱800.00. You can bill outside it."
        )
        self.assertFalse(response.context["discount_form"].is_bound)
        self.assertEqual(self._messages(response), [])
        self.assertEqual(self.receipt.billing_items.count(), 1)
        self.assertEqual(response.context["total"], Decimal("200.00"))
        # The old page shows the error under the field
        self.assertContains(response, '<p class="form-error">Type the price.</p>')

    def test_old_page_shows_every_item_error(self):
        response = self._post(
            "admin_billing_item_add",
            self.receipt.pk,
            {"service_choice": "", "quantity": "0"},
        )
        self.assertContains(
            response,
            '<p class="form-error">Choose what it is, or Other (type it).</p>',
        )
        self.assertContains(
            response, '<p class="form-error">The quantity must be from 1 to 999.</p>'
        )

    def test_invalid_discount_reopens_the_dialog(self):
        response = self._post(
            "admin_receipt_edit",
            self.receipt.pk,
            {"discount_type": BillingReceipt.PERCENTAGE, "discount_value": "150"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "admin/billing/receipt_detail.html")
        self.assertTemplateNotUsed(response, "admin/billing/receipt_form.html")
        self.assertEqual(response.context["open_dialog"], "discount")
        form = response.context["discount_form"]
        self.assertEqual(
            form.errors["discount_value"], ["A discount can't be more than 100%."]
        )
        self.assertEqual(form["discount_value"].value(), "150")
        self.assertFalse(response.context["item_form"].is_bound)
        # The page shows the saved receipt, not the refused discount
        self.assertFalse(response.context["has_discount"])
        self.assertEqual(response.context["total"], Decimal("200.00"))
        self.assertEqual(self._messages(response), [])
        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.discount_type, "")

    def test_valid_discount_and_no_discount_flash(self):
        url = reverse("admin_receipt_detail", args=[self.receipt.pk])
        response = self._post(
            "admin_receipt_edit",
            self.receipt.pk,
            {"discount_type": BillingReceipt.PERCENTAGE, "discount_value": "10"},
        )
        self.assertRedirects(response, url, fetch_redirect_response=False)
        self.assertEqual(self._messages(response), ["Discount saved."])
        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.total_amount, Decimal("180.00"))

        self.client.cookies.pop("messages", None)
        response = self._post(
            "admin_receipt_edit",
            self.receipt.pk,
            {"discount_type": "", "discount_value": ""},
        )
        self.assertRedirects(response, url, fetch_redirect_response=False)
        self.assertEqual(self._messages(response), ["Discount removed."])
        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.total_amount, Decimal("200.00"))

    def test_get_on_the_edit_url_opens_the_receipt_page(self):
        url = reverse("admin_receipt_detail", args=[self.receipt.pk])
        response = self.client.get(
            reverse("admin_receipt_edit", args=[self.receipt.pk])
        )
        self.assertRedirects(response, url, fetch_redirect_response=False)
        self.assertEqual(self._messages(response), [])
        response = self.client.get(
            reverse("admin_receipt_edit", args=[self.receipt.pk]), follow=True
        )
        self.assertTemplateUsed(response, "admin/billing/receipt_detail.html")
        self.assertTemplateNotUsed(response, "admin/billing/receipt_form.html")

    def test_locked_receipts_are_still_refused(self):
        for status in LOCKED_STATUSES:
            receipt = self._receipt(status)
            url = reverse("admin_receipt_detail", args=[receipt.pk])
            posts = [
                (
                    "admin_billing_item_add",
                    {"service_choice": "other", "quantity": "0"},
                ),
                (
                    "admin_receipt_edit",
                    {
                        "discount_type": BillingReceipt.PERCENTAGE,
                        "discount_value": "150",
                    },
                ),
            ]
            for name, data in posts:
                with self.subTest(status=status, view=name):
                    self.client.cookies.pop("messages", None)
                    response = self._post(name, receipt.pk, data)
                    self.assertRedirects(response, url, fetch_redirect_response=False)
                    self.assertEqual(self._messages(response), [LOCKED_MESSAGE])
            receipt.refresh_from_db()
            self.assertEqual(receipt.billing_items.count(), 1)
            self.assertEqual(receipt.discount_type, "")

    def test_pets_for_owner_url_is_gone(self):
        """6a-3: admin_get_pets_for_owner (unused since 6a-2) was removed."""
        with self.assertRaises(NoReverseMatch):
            reverse("admin_get_pets_for_owner")


# ── Slice 6a-4: owner side, notification, services ──────────────────────────

OWNER_LIST_QUERIES = 5
SERVICES_LIST_QUERIES = 7


@PLAIN_STATIC
class StatusLabelTests(FrozenBillingTestBase):

    def test_status_label(self):
        cases = [
            (BillingReceipt.PENDING, "Unpaid"),
            (BillingReceipt.PAID, "Paid"),
            (BillingReceipt.CANCELLED, "Cancelled"),
        ]
        for status, label in cases:
            with self.subTest(status=status):
                self.assertEqual(self._receipt(status).status_label, label)
        self.assertEqual(BillingReceipt(payment_status="junk").status_label, "")

    def test_print_partial_shows_the_label(self):
        receipt = self._receipt()
        response = self.client.get(reverse("admin_receipt_detail", args=[receipt.pk]))
        self.assertContains(response, "<span>Payment Status:</span><span>Unpaid</span>")


@PLAIN_STATIC
class OwnerBillingTests(FrozenBillingTestBase):
    """Rule 23 / 25: owners see Unpaid and Paid only; the printout is a
    direct child of <body>."""

    def setUp(self):
        super().setUp()
        self.pending = self._receipt(BillingReceipt.PENDING)
        self.paid = self._receipt(BillingReceipt.PAID)
        self.cancelled = self._receipt(BillingReceipt.CANCELLED)
        self.client.force_login(self.owner.user)

    def _detail(self, receipt):
        return self.client.get(reverse("owner_receipt_detail", args=[receipt.pk]))

    def test_list_leaves_out_cancelled(self):
        response = self.client.get(reverse("owner_billing_list"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            {r.pk for r in response.context["receipts"]},
            {self.pending.pk, self.paid.pk},
        )
        self.assertNotContains(response, self.cancelled.receipt_number)
        # Badges: the labels, with the existing badge classes
        html = response.content.decode()
        self.assertRegex(html, r'class="status-badge status-pending">\s*Unpaid\s*<')
        self.assertRegex(html, r'class="status-badge status-paid">\s*Paid\s*<')
        self.assertNotRegex(html, r">\s*Pending\s*<")

    def _list_queries(self, count):
        BillingReceipt.objects.all().delete()
        for _ in range(count):
            self._receipt(BillingReceipt.PAID)
        self._receipt(BillingReceipt.CANCELLED)
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(reverse("owner_billing_list"))
        self.assertEqual(len(response.context["receipts"]), count)
        return len(queries)

    def test_list_query_count_is_flat(self):
        three, thirty = self._list_queries(3), self._list_queries(30)
        self.assertEqual(three, thirty)
        self.assertEqual(thirty, OWNER_LIST_QUERIES)

    def test_detail_shows_unpaid_and_paid(self):
        for receipt, label in [(self.pending, "Unpaid"), (self.paid, "Paid")]:
            with self.subTest(label=label):
                response = self._detail(receipt)
                self.assertEqual(response.status_code, 200)
                self.assertRegex(
                    response.content.decode(),
                    rf'class="status-badge status-{receipt.payment_status}">'
                    rf"\s*{label}\s*<",
                )

    def test_detail_of_a_cancelled_receipt_is_a_404(self):
        self.assertEqual(self._detail(self.cancelled).status_code, 404)

    def test_detail_of_another_owners_receipt_is_a_404(self):
        other_user = User.objects.create_user(
            username="other",
            email="other@example.com",
            password="unused-pass-123",
            role=User.PET_OWNER,
            is_onboarded=True,
        )
        other = PetOwner.objects.create(
            user=other_user, first_name="Ben", last_name="Reyes"
        )
        theirs = BillingReceipt.objects.create(
            receipt_number=BillingReceipt.generate_receipt_number(), owner=other
        )
        self.assertEqual(self._detail(theirs).status_code, 404)

    def test_printout_is_a_direct_child_of_body(self):
        html = self._detail(self.paid).content.decode()
        printout = html.index('id="printable-receipt"')
        # After the page's frame closes (</main>), before </body>
        self.assertGreater(printout, html.index("</main>"))
        self.assertLess(printout, html.index("</body>"))
        self.assertEqual(html.count('id="printable-receipt"'), 1)
        self.assertIn("<span>Payment Status:</span><span>Paid</span>", html)


class ReceiptPaidNotificationTests(FrozenBillingTestBase):
    """Rule 24: one notification, at mark paid; nothing on create."""

    def _mark_paid(self, receipt):
        return self._post("admin_receipt_mark_paid", receipt.pk)

    def test_title_message_and_subject_with_a_pet(self):
        receipt = self._empty_receipt()
        self._item(receipt, "1250.00")
        receipt.calculate_totals()
        self._mark_paid(receipt)
        notification = Notification.objects.get()
        title = f"Receipt {receipt.receipt_number} for Bantay"
        self.assertEqual(notification.notification_type, "billing_generated")
        self.assertEqual(notification.title, title)
        # 00:30 Manila on Sat, Nov 7 (still Nov 6 in UTC): paid on Nov 7
        self.assertEqual(
            notification.message,
            "Paid ₱1,250.00 on Sat, Nov 7. You can view or print it in Billing.",
        )
        self.assertEqual(notification.related_billing, receipt)
        self.assertEqual(notification.related_pet, self.pet)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].subject, f"{title} — Hapi Vet")
        self.assertEqual(mail.outbox[0].body, notification.message)

    def test_title_without_a_pet(self):
        receipt = BillingReceipt.objects.create(
            receipt_number=BillingReceipt.generate_receipt_number(), owner=self.owner
        )
        self._item(receipt, "100.00")
        receipt.calculate_totals()
        self._mark_paid(receipt)
        notification = Notification.objects.get()
        self.assertEqual(notification.title, f"Receipt {receipt.receipt_number}")
        self.assertIsNone(notification.related_pet)
        self.assertEqual(
            mail.outbox[0].subject, f"Receipt {receipt.receipt_number} — Hapi Vet"
        )

    def test_the_year_shows_when_it_isnt_this_year(self):
        receipt = self._receipt(BillingReceipt.PAID)
        receipt.payment_date = datetime.date(2025, 11, 7)
        self.assertEqual(
            receipt_paid_message(receipt, TODAY),
            "Paid ₱200.00 on Fri, Nov 7, 2025. You can view or print it in Billing.",
        )
        notify_receipt_paid(receipt)
        self.assertIn("on Fri, Nov 7, 2025.", Notification.objects.get().message)

    def test_sent_once(self):
        receipt = self._receipt()
        self._mark_paid(receipt)
        self._mark_paid(receipt)  # refused: already paid
        self.assertEqual(Notification.objects.count(), 1)
        self.assertEqual(len(mail.outbox), 1)

    def test_nothing_on_create(self):
        url = reverse("admin_receipt_create")
        self.client.post(f"{url}?owner={self.owner.pk}", {"pet": self.pet.pk})
        visit = Appointment.objects.create(
            owner=self.owner,
            pet=self.pet,
            date=TODAY,
            time=datetime.time(9, 0),
            status=Appointment.COMPLETED,
        )
        self.client.post(f"{url}?appointment={visit.pk}")
        self.assertEqual(BillingReceipt.objects.count(), 2)
        self.assertEqual(Notification.objects.count(), 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_sent_after_the_commit(self):
        """The receipt is already PAID in the database when notify runs."""
        receipt = self._receipt()
        seen = []

        def check(*args, **kwargs):
            seen.append(BillingReceipt.objects.get(pk=receipt.pk).payment_status)

        with mock.patch.object(billing_views, "notify_receipt_paid", check):
            self._mark_paid(receipt)
        self.assertEqual(seen, [BillingReceipt.PAID])


class ServiceFormTests(FrozenBillingTestBase):
    """Rules 29–31: ServiceForm labels, messages, unique names, pricing."""

    def _data(self, **extra):
        data = {
            "service_name": "Consultation",
            "category": "Check-ups",
            "description": "",
            "pricing_type": Service.FIXED,
            "base_price": "300.00",
            "min_price": "",
            "max_price": "",
            "status": Service.ACTIVE,
        }
        data.update(extra)
        return data

    def _form(self, instance=None, **extra):
        return ServiceForm(self._data(**extra), instance=instance)

    def _range(self, **extra):
        data = self._data(
            pricing_type=Service.RANGE, min_price="300.00", max_price="800.00"
        )
        data.update(extra)
        return data

    def test_labels_choices_help_and_placeholders(self):
        form = ServiceForm()
        labels = {name: field.label for name, field in form.fields.items()}
        self.assertEqual(
            labels,
            {
                "service_name": "Name",
                "category": "Category",
                "description": "Description",
                "pricing_type": "How is it priced?",
                "base_price": "Price",
                "min_price": "Lowest price",
                "max_price": "Highest price",
                "status": "Who sees it",
            },
        )
        self.assertEqual(
            list(form.fields["pricing_type"].choices),
            [("fixed", "One price"), ("range", "A price range")],
        )
        self.assertEqual(
            list(form.fields["status"].choices),
            [
                ("active", "Offered"),
                ("unlisted", "Clinic use only"),
                ("draft", "Hidden (draft)"),
                ("closed", "No longer offered"),
            ],
        )
        placeholders = {
            "service_name": "For example: Consultation",
            "category": "For example: Vaccines",
            "description": "What it includes, in a sentence",
        }
        for name, text in placeholders.items():
            with self.subTest(field=name):
                self.assertEqual(form.fields[name].widget.attrs["placeholder"], text)
        self.assertEqual(
            form.fields["category"].help_text, "Groups similar services. Type any word."
        )
        self.assertEqual(
            form.fields["description"].help_text, "Pet owners see this when they book."
        )
        for name in ["base_price", "min_price", "max_price"]:
            self.assertFalse(form.fields[name].required)

    def test_a_new_service_starts_as_draft(self):
        self.assertEqual(ServiceForm()["status"].value(), Service.DRAFT)
        service = self._service("Bath", status=Service.ACTIVE)
        self.assertEqual(ServiceForm(instance=service)["status"].value(), "active")

    def test_name_is_needed_and_stripped(self):
        for name in ["", "   "]:
            with self.subTest(name=repr(name)):
                form = self._form(service_name=name)
                self.assertEqual(
                    form.errors["service_name"], ["Type the service name."]
                )
        form = self._form(service_name="  Deworming  ")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().service_name, "Deworming")

    def test_name_is_unique_ignoring_case_and_spaces(self):
        self._service("Consultation")
        Service.objects.create(service_name=" Grooming ", base_price=Decimal("1"))
        cases = [
            ("consultation", "Consultation"),
            ("  CONSULTATION ", "Consultation"),
            ("grooming", "Grooming"),
        ]
        for typed, existing in cases:
            with self.subTest(typed=typed):
                form = self._form(service_name=typed)
                self.assertEqual(
                    form.errors["service_name"],
                    [f"There is already a service called “{existing}”."],
                )
        self.assertTrue(self._form(service_name="Consultation plus").is_valid())

    def test_edit_keeps_its_own_name(self):
        service = self._service("Consultation")
        self._service("Bath")
        form = self._form(instance=service, service_name=" consultation ")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().service_name, "consultation")
        form = self._form(instance=service, service_name="BATH")
        self.assertEqual(
            form.errors["service_name"], ["There is already a service called “Bath”."]
        )

    def test_one_price_needs_the_price(self):
        form = self._form(base_price="")
        self.assertEqual(form.errors, {"base_price": ["Type the price."]})

    def test_one_price_clears_the_range(self):
        form = self._form(min_price="100.00", max_price="900.00")
        self.assertTrue(form.is_valid(), form.errors)
        service = form.save()
        service.refresh_from_db()
        self.assertEqual(service.base_price, Decimal("300.00"))
        self.assertIsNone(service.min_price)
        self.assertIsNone(service.max_price)

    def test_a_range_needs_both_prices(self):
        message = ["Type the lowest and the highest price."]
        cases = [
            ({"min_price": ""}, "min_price"),
            ({"max_price": ""}, "max_price"),
            ({"min_price": "", "max_price": ""}, "min_price"),
        ]
        for extra, field in cases:
            with self.subTest(extra=extra):
                form = ServiceForm(self._range(**extra))
                self.assertEqual(form.errors, {field: message})

    def test_highest_must_be_more_than_lowest(self):
        for high in ["300.00", "299.99"]:
            with self.subTest(high=high):
                form = ServiceForm(self._range(max_price=high))
                self.assertEqual(
                    form.errors,
                    {"max_price": ["The highest price must be more than the lowest."]},
                )

    def test_a_range_sets_base_price_to_the_lowest(self):
        form = ServiceForm(self._range(base_price="999.00"))
        self.assertTrue(form.is_valid(), form.errors)
        service = form.save()
        service.refresh_from_db()
        self.assertEqual(service.min_price, Decimal("300.00"))
        self.assertEqual(service.max_price, Decimal("800.00"))
        self.assertEqual(service.base_price, Decimal("300.00"))

    def test_a_range_without_a_posted_base_price(self):
        """base_price missing from the POST (not just empty): still the lowest."""
        data = self._range()
        del data["base_price"]
        form = ServiceForm(data)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().base_price, Decimal("300.00"))

    def test_edit_from_one_price_to_a_range_and_back(self):
        service = self._service("Grooming", "500.00")
        form = ServiceForm(self._range(service_name="Grooming"), instance=service)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        service.refresh_from_db()
        self.assertEqual(
            (service.base_price, service.min_price, service.max_price),
            (Decimal("300.00"), Decimal("300.00"), Decimal("800.00")),
        )
        form = self._form(instance=service, service_name="Grooming", base_price="450")
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        service.refresh_from_db()
        self.assertEqual(
            (service.base_price, service.min_price, service.max_price),
            (Decimal("450.00"), None, None),
        )

    def test_negative_prices(self):
        message = ["Prices can't be negative."]
        form = self._form(base_price="-1")
        self.assertEqual(form.errors, {"base_price": message})
        form = ServiceForm(self._range(min_price="-1"))
        self.assertEqual(form.errors, {"min_price": message})
        form = ServiceForm(self._range(max_price="-5"))
        self.assertEqual(form.errors, {"max_price": message})
        form = ServiceForm(self._range(min_price="-5", max_price="-1"))
        self.assertEqual(form.errors, {"min_price": message, "max_price": message})


@PLAIN_STATIC
class ServicePagesTests(FrozenBillingTestBase):
    """services_list_context and the old services pages."""

    def test_groups_order_text_and_counts(self):
        self._service("consultation")
        self._service("Bath")
        self._service("X-ray", status=Service.UNLISTED)
        self._service("Dental", status=Service.DRAFT)
        context = services_list_context({})
        groups = context["groups"]
        self.assertEqual(
            [(g["key"], g["title"]) for g in groups],
            [
                ("active", "Offered"),
                ("unlisted", "Clinic use only"),
                ("draft", "Hidden (draft)"),
                ("closed", "No longer offered"),
            ],
        )
        self.assertEqual(
            [g["text"] for g in groups],
            [
                "Pet owners see these when they book. You can put them on receipts.",
                "Hidden from pet owners. You can put them on receipts and pick them "
                "for walk-ins.",
                "Not shown anywhere yet. Finish it, then choose Offered or Clinic use "
                "only.",
                "Not shown anywhere. Old visits and receipts keep their names and "
                "prices.",
            ],
        )
        self.assertEqual(
            [[s.service_name for s in g["services"]] for g in groups],
            [["Bath", "consultation"], ["X-ray"], ["Dental"], []],
        )
        self.assertEqual([g["count"] for g in groups], [2, 1, 1, 0])
        self.assertEqual(context["total_count"], 4)
        self.assertEqual(
            context["counts"],
            {"all": 4, "active": 2, "unlisted": 1, "draft": 1, "closed": 0},
        )
        self.assertEqual(context["status_filter"], "")
        self.assertEqual(len(context["services"]), 4)
        self.assertEqual(context["status_choices"], Service.STATUS_CHOICES)

    def test_old_status_filter_still_works(self):
        self._service("Bath")
        self._service("Dental", status=Service.DRAFT)
        context = services_list_context({"status": "draft"})
        self.assertEqual([s.service_name for s in context["services"]], ["Dental"])
        self.assertEqual(context["status_filter"], "draft")
        self.assertEqual(context["counts"]["all"], 2)

    def _list_queries(self, count):
        Service.objects.all().delete()
        for number in range(count):
            status = [Service.ACTIVE, Service.UNLISTED, Service.DRAFT, Service.CLOSED]
            self._service(f"Service {number}", status=status[number % 4])
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(reverse("admin_service_list"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["total_count"], count)
        return len(queries)

    def test_query_count_is_flat(self):
        three, thirty = self._list_queries(3), self._list_queries(30)
        self.assertEqual(three, thirty)
        self.assertEqual(thirty, SERVICES_LIST_QUERIES)

    def test_old_list_page_renders(self):
        self._service("Bath")
        response = self.client.get(reverse("admin_service_list") + "?status=active")
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "admin/billing/services_list.html")
        self.assertContains(response, "Bath")

    def test_old_form_renders_with_the_posted_names(self):
        service = self._service("Bath")
        for url in [
            reverse("admin_service_create"),
            reverse("admin_service_edit", args=[service.pk]),
        ]:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertTemplateUsed(response, "admin/billing/service_form.html")
                for name in [
                    "service_name",
                    "category",
                    "description",
                    "pricing_type",
                    "base_price",
                    "min_price",
                    "max_price",
                    "status",
                ]:
                    self.assertContains(response, f'name="{name}"')
        response = self.client.get(reverse("admin_service_create"))
        self.assertContains(response, '<option value="draft" selected>Hidden (draft)')

    def test_create_and_edit_save_with_the_flash(self):
        data = {
            "service_name": " Deworming ",
            "category": "",
            "description": "",
            "pricing_type": Service.FIXED,
            "base_price": "150",
            "min_price": "",
            "max_price": "",
            "status": Service.DRAFT,
        }
        response = self.client.post(reverse("admin_service_create"), data)
        self.assertRedirects(
            response, reverse("admin_service_list"), fetch_redirect_response=False
        )
        self.assertEqual(self._messages(response), ["Deworming added."])
        service = Service.objects.get()
        self.assertEqual(service.service_name, "Deworming")
        self.assertEqual(service.status, Service.DRAFT)

        self.client.cookies.pop("messages", None)
        data.update(
            service_name="Deworming",
            pricing_type=Service.RANGE,
            min_price="100",
            max_price="250",
            status=Service.ACTIVE,
        )
        response = self.client.post(
            reverse("admin_service_edit", args=[service.pk]), data
        )
        self.assertEqual(self._messages(response), ["Deworming saved."])
        service.refresh_from_db()
        self.assertEqual(service.status, Service.ACTIVE)
        self.assertEqual(service.base_price, Decimal("100.00"))
        self.assertEqual(service.max_price, Decimal("250.00"))

    def test_errors_show_on_the_old_form(self):
        self._service("Bath")
        response = self.client.post(
            reverse("admin_service_create"),
            {"service_name": "bath", "pricing_type": Service.FIXED, "status": "draft"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "There is already a service called “Bath”.")
        self.assertContains(response, "Type the price.")
        self.assertEqual(Service.objects.count(), 1)
