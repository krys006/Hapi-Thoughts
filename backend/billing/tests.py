import datetime
import inspect
from decimal import Decimal
from unittest import mock
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.db import IntegrityError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from billing import views as billing_views
from billing.forms import BillingItemForm, BillingReceiptForm
from billing.models import BillingItem, BillingReceipt, Service
from billing.utils import NO_ITEMS_MESSAGE, save_new_receipt
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
        peso discount is now refused as more than the ₱0.00 subtotal.)"""
        data = self._edit_data(
            owner=self.owner.pk,
            pet=self.pet.pk,
            payment_status=BillingReceipt.PAID,
            discount_type="",
            discount_value="",
            discount_note="",
        )
        response = self.client.post(reverse("admin_receipt_create"), data)
        receipt = BillingReceipt.objects.get()
        self.assertRedirects(
            response,
            reverse("admin_receipt_detail", args=[receipt.pk]),
            fetch_redirect_response=False,
        )
        self.assertEqual(receipt.payment_status, BillingReceipt.PENDING)
        self.assertIsNone(receipt.payment_date)


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
        posted. The billing_date default holds the real timezone.now (bound
        when the model loads), so the frozen clock doesn't reach it: the
        date is checked against the real Manila date."""
        self._numbers("HV-2026-0009")
        real_today = datetime.datetime.now(MANILA).date()
        self.client.post(
            reverse("admin_receipt_create"),
            {
                "owner": self.owner.pk,
                "pet": self.pet.pk,
                "billing_date": "2020-01-01",
                "payment_method": "GCash",
            },
        )
        receipt = BillingReceipt.objects.get(owner=self.owner)
        self.assertEqual(receipt.receipt_number, "HV-2026-0010")
        self.assertIn(
            receipt.billing_date, [real_today, datetime.datetime.now(MANILA).date()]
        )
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
