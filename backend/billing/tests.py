import datetime
import inspect
import re
from decimal import Decimal
from html import unescape
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
from django.utils.html import escape

from appointments.models import Appointment, ClinicSettings
from billing import views as billing_views
from billing.forms import BillingItemForm, BillingReceiptForm, ServiceForm
from billing.models import BillingItem, BillingReceipt, Service
from billing.templatetags.billing_extras import peso
from billing.utils import (
    NO_ITEMS_MESSAGE,
    PICK_PET_MESSAGE,
    RECEIPT_EXISTS,
    RECEIPT_TABS,
    notify_receipt_paid,
    owner_bills_context,
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


def error_line(text):
    """Regex for one error under a dialog field (form_field.html style:
    red text, triangle-alert icon, then the message)."""
    return (
        r'(?s)<p class="flex items-start gap-1 text-sm font-medium text-red-700">'
        r"\s*<svg.*?</svg>\s*" + re.escape(escape(text)) + "</p>"
    )


def page_text(markup):
    """The text of some HTML: tags to spaces, entities decoded, spaces
    collapsed (so "Billed to <a>Ana</a> · for" reads "Billed to Ana · for")."""
    text = " ".join(unescape(re.sub(r"<[^>]+>", " ", markup)).split())
    # "<b>₱450.00</b>." would read "₱450.00 ."
    return re.sub(r" ([.,;:!?])", r"\1", text)


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
                # 6b-2: the option now carries the data-* the script reads
                self.assertContains(
                    response,
                    f'<option value="{consult.pk}" data-name="Consultation" '
                    'data-price="300.00" data-pricing="fixed" '
                    'data-help="Price list: ₱300.00.">Consultation · ₱300.00</option>',
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

NEW_RECEIPT_TEMPLATES = Path(settings.BASE_DIR) / "templates" / "admin" / "billing"
# The page, its includes and step 1's results partial
RECEIPT_NEW_FILES = [
    NEW_RECEIPT_TEMPLATES / "receipt_new.html",
    NEW_RECEIPT_TEMPLATES / "_receipt_owner_results.html",
    *sorted((NEW_RECEIPT_TEMPLATES / "receipt_new_parts").glob("*.html")),
]


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
        # The pet's name follows its species icon
        self.assertRegex(response.content.decode(), r"(?s)</svg>\s*Bantay</li>")
        self.assertContains(response, "No pets yet")
        choose = f'<a href="{self.url}?owner={self.owner.pk}" class="inline-flex'
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
            f'value="{self.pet.pk}" class="size-6 shrink-0 accent-violet-600" '
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
            'value="none" class="size-6 shrink-0 accent-violet-600" required checked>',
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
        links = []
        for path in RECEIPT_NEW_FILES:
            source = path.read_text(encoding="utf-8")
            links += re.findall(r"(?s)<a\b.*?</a>", source)
        self.assertTrue(links)
        for link in links:
            with self.subTest(link=link[:60]):
                self.assertNotIn("\n", link)


# ── Slice 6b-3: New receipt design pass ───────────────────────────────────────

# Whole-page query counts of the New receipt steps, measured: session + user
# + the admin layout's own (4) + the page's. Owner step: the paginator count,
# the owners, their active pets (one prefetch). Pet step: the owner, the pets.
NEW_OWNER_STEP_QUERIES = 9
NEW_PET_STEP_QUERIES = 8

HTMX_RESULTS = {"HTTP_HX_REQUEST": "true", "HTTP_HX_TARGET": "owner-results"}


@PLAIN_STATIC
class NewReceiptDesignTests(NewReceiptTestBase):
    """receipt_new.html and receipt_new_parts/ (mockup receipt_new_v1.html)."""

    def setUp(self):
        super().setUp()
        self.owner_url = f"{self.url}?owner={self.owner.pk}"

    def _visit_url(self, **fields):
        visit = self._visit(**fields)
        return f"{self.url}?appointment={visit.pk}"

    def _dd(self, html, term):
        match = re.search(rf"(?s)<dt[^>]*>{term}</dt>\s*<dd[^>]*>(.*?)</dd>", html)
        self.assertIsNotNone(match, term)
        return match.group(1)

    def _step_items(self, html):
        ol = re.search(r'(?s)<ol class="mb-6[^>]*aria-label="Steps">.*?</ol>', html)
        self.assertIsNotNone(ol)
        return re.findall(r"(?s)<li\b.*?</li>", ol.group(0))

    def _cards(self, html):
        """(opening tag, inner html) of every radio card."""
        return re.findall(
            r'(?s)(<label class="flex min-h-11[^"]*">)(.*?)</label>', html
        )

    # ─── Header and steps ─────────────────────────────────────────────────────

    def test_page_is_one_centered_column_in_every_mode(self):
        """Back link, h1, steps and cards share one mx-auto max-w-3xl column;
        the owner list that HTMX swaps in sits inside it."""
        urls = {
            "visit": self._visit_url(service=self._service("Consultation")),
            "owner": self.url,
            "pet": self.owner_url,
        }
        for mode, url in urls.items():
            with self.subTest(mode=mode):
                html = self._html(url)
                match = re.search(r'<div id="page-column" class="([^"]*)">', html)
                self.assertIsNotNone(match)
                classes = match.group(1).split()
                self.assertIn("mx-auto", classes)
                self.assertIn("max-w-3xl", classes)
                self.assertEqual(html.count("<h1"), 1)
                self.assertGreater(html.index("<h1"), match.end())
                if mode == "owner":
                    self.assertGreater(html.index('id="owner-results"'), match.end())

    def test_header_per_mode(self):
        visit_url = self._visit_url(service=self._service("Consultation"))
        cases = [
            (
                visit_url,
                "For the visit below. Check it, then create the receipt.",
                "Back to the visit",
            ),
            (
                self.url,
                "Not from a visit, for example supplies bought over the counter.",
                "Back to receipts",
            ),
            (
                self.owner_url,
                "Not from a visit, for example supplies bought over the counter.",
                "Back to receipts",
            ),
        ]
        for url, subtitle, back in cases:
            with self.subTest(url=url):
                html = self._html(url)
                self.assertIn(
                    '<h1 class="text-2xl font-bold text-gray-900">New receipt</h1>',
                    html,
                )
                self.assertIn(
                    f'<p class="mt-1 text-base text-gray-700">{subtitle}</p>', html
                )
                self.assertRegex(
                    html,
                    r'(?s)<a href="[^"]+" class="inline-flex min-h-11 items-center '
                    r"gap-2 text-base font-medium text-blue-600 underline "
                    r'underline-offset-2">\s*<svg[^>]*aria-hidden="true".*?</svg>\s*'
                    + back
                    + "</a>",
                )

    def test_steps_in_the_owner_step(self):
        first, second = self._step_items(self._html(self.url))
        self.assertIn('aria-current="step"', first)
        self.assertIn("text-violet-800", first)
        self.assertIn("bg-violet-600", first)
        self.assertIn(">1</span>", first)
        self.assertIn("Who is it for?", first)
        self.assertNotIn("aria-current", second)
        self.assertIn("bg-gray-200 text-gray-700", second)
        self.assertIn("Which pet?", second)

    def test_steps_in_the_pet_step(self):
        first, second = self._step_items(self._html(self.owner_url))
        # Step 1 is done: green, a check, no number, not current
        self.assertNotIn("aria-current", first)
        self.assertIn("bg-green-100 text-green-800", first)
        self.assertIn("<svg", first)
        self.assertIn("Done: step 1", first)
        self.assertNotIn(">1</span>", first)
        self.assertIn('aria-current="step"', second)
        self.assertIn("bg-violet-600", second)
        self.assertIn(">2</span>", second)

    def test_no_steps_in_the_visit_mode(self):
        html = self._html(self._visit_url())
        self.assertNotIn('aria-label="Steps"', html)
        self.assertNotIn("Who is it for?", html)

    # ─── Mode visit ───────────────────────────────────────────────────────────

    def test_visit_card_rows(self):
        self.owner.contact_number = "0917 555 0142"
        self.owner.save()
        Pet.objects.filter(pk=self.pet.pk).update(breed="Aspin")
        html = self._html(self._visit_url(service=self._service("Consultation")))
        self.assertIn("From the visit</h2>", html)
        self.assertRegex(
            html, r'(?s)<h2 id="visit-title"[^>]*>\s*<svg[^>]*aria-hidden="true"'
        )
        visit = self._dd(html, "Visit")
        self.assertEqual(
            page_text(visit), "Today, Saturday, November 7 at 9:00 AM Completed"
        )
        self.assertIn("bg-green-100 text-green-800", visit)
        billed = self._dd(html, "Billed to")
        self.assertIn('role="img" aria-label="Ana Cruz"', billed)
        self.assertIn(
            f'<a href="{reverse("admin_owner_detail", args=[self.owner.pk])}"', billed
        )
        self.assertIn("Ana Cruz</a> · 0917 555 0142", billed)
        pet = self._dd(html, "Pet")
        self.assertIn('aria-label="No photo of Bantay yet"', pet)
        self.assertIn(
            f'<a href="{reverse("admin_pet_detail", args=[self.pet.pk])}"', pet
        )
        self.assertIn("Bantay</a> · Dog · Aspin", pet)
        self.assertNotIn("yrs", pet)
        self.assertEqual(page_text(self._dd(html, "Service")), "Consultation")

    def test_visit_card_without_a_service_or_breed(self):
        html = self._html(self._visit_url())
        self.assertIn('<span class="text-gray-600">No service picked</span>', html)
        # Species only: no breed, so no second "·"
        self.assertIn("Bantay</a> · Dog</span>", self._dd(html, "Pet"))
        self.assertIn("Ana Cruz</a> · No phone number", self._dd(html, "Billed to"))

    def test_visit_card_shows_the_visit_status(self):
        html = self._html(self._visit_url(status=Appointment.CONFIRMED))
        self.assertIn("bg-blue-100 text-blue-800", self._dd(html, "Visit"))
        self.assertIn("Confirmed", self._dd(html, "Visit"))

    def test_visit_date_follows_the_views_today(self):
        yesterday = TODAY - datetime.timedelta(days=1)
        html = self._html(self._visit_url(day=yesterday))
        self.assertEqual(
            page_text(self._dd(html, "Visit")),
            "Friday, November 6 at 9:00 AM Completed",
        )

    def test_checkbox_label_checked_by_default_with_the_price(self):
        service = self._service("Consultation", price="1250.00")
        html = self._html(self._visit_url(service=service))
        label = re.search(r'(?s)<label for="add-service"[^>]*>.*?</label>', html)
        self.assertIsNotNone(label)
        label = label.group(0)
        self.assertIn("min-h-11", label)
        self.assertIn("border-violet-200 bg-violet-50", label)
        self.assertEqual(label.count('type="checkbox"'), 1)
        self.assertIn('id="add-service" name="add_service" value="on"', label)
        self.assertIn("size-6", label)
        self.assertRegex(label, r"(?s)<input[^>]* checked>")
        self.assertEqual(
            page_text(label),
            "Add Consultation (₱1,250.00) as the first item "
            "You can change its price or remove it on the next page.",
        )

    def test_no_checkbox_without_a_fixed_service(self):
        for hour, service in enumerate(
            (None, self._range_service("Surgery", "1000.00", "5000.00")), start=9
        ):
            with self.subTest(service=service):
                html = self._html(self._visit_url(hour=hour, service=service))
                self.assertNotIn('type="checkbox"', html)
                self.assertNotIn("add-service", html)

    # ─── Mode owner ───────────────────────────────────────────────────────────

    def test_search_box_and_heading_line(self):
        self.owner.contact_number = "0917 555 0142"
        self.owner.save()
        html = self._html(self.url)
        self.assertIn(">Find the owner</label>", html)
        self.assertIn('class="input min-h-12 pl-11"', html)
        self.assertRegex(
            html, r'(?s)<div class="relative">\s*<svg[^>]*pointer-events-none'
        )
        self.assertIn(
            '<h2 id="results-title" class="text-lg font-semibold text-gray-900" '
            'aria-live="polite">All pet owners (1)</h2>',
            html,
        )
        self.assertIn("A to Z by first name", html)
        self.assertIn(
            "Someone new? Add them first with "
            f'<a href="{reverse("admin_owner_create")}"',
            html,
        )
        self.assertIn("on the Pet owners page.</p>", html)
        self.assertNotIn("Clear search", html)

    def test_owner_rows(self):
        self.owner.contact_number = "0917 555 0142"
        self.owner.save()
        Pet.objects.create(owner=self.owner, name="Mingming", species="Cat")
        Pet.objects.create(
            owner=self.owner, name="Ghost", species="Cat", is_archived=True
        )
        jun = self._owner("Jun", "Reyes")
        html = self._html(self.url)
        rows = re.findall(
            r'(?s)<li class="flex flex-wrap items-center gap-4.*?</a>\s*</li>', html
        )
        self.assertEqual(len(rows), 2)
        ana, jun_row = rows
        self.assertIn('role="img" aria-label="Ana Cruz"', ana)
        self.assertIn(
            '<p class="text-lg font-semibold text-gray-900">Ana Cruz</p>', ana
        )
        self.assertIn("0917 555 0142", ana)
        self.assertRegex(ana, r"(?s)</svg>\s*Bantay</li>")
        self.assertRegex(ana, r"(?s)</svg>\s*Mingming</li>")
        self.assertNotIn("Ghost", ana)
        self.assertNotIn("No pets yet", ana)
        self.assertIn("No phone number", jun_row)
        self.assertIn("No pets yet", jun_row)
        self.assertNotIn('aria-label="Pets"', jun_row)
        self.assertIn(
            f'<a href="{self.url}?owner={jun.pk}" class="inline-flex min-h-11 '
            "items-center gap-2 rounded-lg border border-violet-300 bg-violet-50 "
            'px-5 text-base font-semibold text-violet-800 hover:bg-violet-100">'
            'Choose<span class="sr-only"> Jun Reyes</span>',
            jun_row,
        )

    def test_search_box_filters_as_you_type(self):
        html = self._html(self.url)
        tag = re.search(r'<input type="search"[^>]*>', html).group(0)
        self.assertIn(f'hx-get="{self.url}"', tag)
        self.assertIn('hx-trigger="input changed delay:500ms, search"', tag)
        self.assertIn('hx-target="#owner-results"', tag)
        self.assertIn('hx-replace-url="true"', tag)
        self.assertNotIn("hx-push-url", html)
        self.assertIn('<div id="owner-results">', html)
        # Without JS the GET form still works
        self.assertIn(f'<form method="get" action="{self.url}"', html)
        self.assertIn('type="submit"', html)

    def test_htmx_request_for_the_results_gets_only_the_partial(self):
        self._owner("Liza", "Cruz")
        response = self.client.get(self.url, {"q": "cruz"}, **HTMX_RESULTS)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "admin/billing/_receipt_owner_results.html")
        self.assertTemplateNotUsed(response, "layouts/admin.html")
        self.assertTemplateNotUsed(response, "admin/billing/receipt_new.html")
        html = response.content.decode()
        self.assertNotIn("<html", html)
        self.assertNotIn('id="receipt-owner-q"', html)
        self.assertIn("2 owners match “cruz”", html)
        self.assertIn("Clear search", html)
        self.assertIn("HX-Request", response.headers["Vary"])

    def test_htmx_request_without_a_query_gets_the_whole_list_partial(self):
        response = self.client.get(self.url, **HTMX_RESULTS)
        self.assertTemplateNotUsed(response, "layouts/admin.html")
        self.assertContains(response, "All pet owners (1)")
        self.assertContains(response, "Showing all 1")

    def test_the_full_page_in_every_other_case(self):
        cases = {
            "plain": {},
            "history restore": {
                **HTMX_RESULTS,
                "HTTP_HX_HISTORY_RESTORE_REQUEST": "true",
            },
            "another target": {"HTTP_HX_REQUEST": "true", "HTTP_HX_TARGET": "main"},
            "no target": {"HTTP_HX_REQUEST": "true"},
        }
        for name, headers in cases.items():
            with self.subTest(case=name):
                response = self.client.get(self.url, {"q": "ana"}, **headers)
                self.assertTemplateUsed(response, "layouts/admin.html")
                self.assertTemplateUsed(response, "admin/billing/receipt_new.html")
                self.assertTemplateUsed(
                    response, "admin/billing/_receipt_owner_results.html"
                )
                self.assertContains(response, 'id="receipt-owner-q"')
                self.assertIn("HX-Request", response.headers["Vary"])

    def test_htmx_does_not_change_the_other_modes(self):
        for url in (self.owner_url, self._visit_url()):
            with self.subTest(url=url):
                response = self.client.get(url, **HTMX_RESULTS)
                self.assertTemplateUsed(response, "layouts/admin.html")

    def test_clear_search_only_with_a_query(self):
        link = (
            f'<a href="{self.url}" class="inline-flex min-h-11 items-center gap-2 '
            'text-base font-medium text-blue-600 underline underline-offset-2">'
        )
        self.assertNotIn("Clear search", self._html(self.url))
        for q in ("ana", "zzz"):
            with self.subTest(q=q):
                html = self._html(f"{self.url}?q={q}")
                self.assertIn(link, html)
                self.assertEqual(html.count("Clear search"), 1)
                # Under the box, above the results
                self.assertLess(
                    html.index('id="receipt-owner-q"'), html.index("Clear search")
                )
                self.assertLess(html.index("Clear search"), html.index("results-title"))

    def test_polite_live_region_only_on_the_results_line(self):
        self._owner("Liza", "Cruz")
        cases = {
            self.url: '<h2 id="results-title"',
            f"{self.url}?q=cruz": '<h2 id="results-title" class="mb-2 text-base text-gray-700" aria-live="polite">2 owners match',
            f"{self.url}?q=zzz": '<p class="text-lg font-semibold text-gray-900" aria-live="polite">No owner matches “zzz”</p>',
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                html = self._html(url)
                self.assertEqual(html.count("aria-live"), 1)
                self.assertIn(expected, html)
        # No match: the heading is screen-reader only and not live
        self.assertIn(
            '<h2 id="results-title" class="sr-only">Search results</h2>',
            self._html(f"{self.url}?q=zzz"),
        )
        # The partial has the same single live region
        partial = self.client.get(self.url, {"q": "cruz"}, **HTMX_RESULTS)
        self.assertEqual(partial.content.decode().count("aria-live"), 1)

    def test_empty_states_in_the_partial(self):
        none = self.client.get(self.url, {"q": "zzz"}, **HTMX_RESULTS)
        self.assertContains(none, "No owner matches “zzz”")
        self.assertContains(none, "Check the spelling, or add them with Add owner.")
        self._archive(self.owner)
        empty = self.client.get(self.url, **HTMX_RESULTS)
        self.assertContains(empty, "No pet owners yet")
        self.assertContains(empty, "All pet owners (0)")

    def test_the_pager_is_inside_the_results(self):
        for number in range(1, 30):
            self._owner(f"Owner{number:02d}", "Test")
        html = self._html(self.url)
        self.assertIn('aria-label="Pages"', html)
        self.assertGreater(
            html.index('id="owner-results"'), html.index('role="search"')
        )
        self.assertGreater(
            html.index('aria-label="Pages"'), html.index('id="owner-results"')
        )
        partial = self.client.get(self.url, {"page": "2"}, **HTMX_RESULTS)
        self.assertContains(partial, "Showing 26–30 of 30")

    # ─── Mode pet ─────────────────────────────────────────────────────────────

    def test_billed_to_card(self):
        html = self._html(self.owner_url)
        self.assertIn("Billed to</p>", html)
        self.assertIn(
            '<p class="text-lg font-semibold text-gray-900">Ana Cruz</p>', html
        )
        self.assertIn('role="img" aria-label="Ana Cruz"', html)
        self.assertIn(f'<a href="{self.url}" class="inline-flex min-h-11', html)
        self.assertIn(">Change owner</a>", html)

    def test_pet_cards(self):
        choco = Pet.objects.create(
            owner=self.owner,
            name="Choco",
            species="dog",
            breed="Shih Tzu",
            date_of_birth=datetime.date(2023, 11, 1),
        )
        html = self._html(self.owner_url)
        self.assertRegex(
            html,
            r'(?s)<legend class="text-base font-semibold text-gray-900">Which pet '
            r'is it for\? <span class="text-red-700" aria-hidden="true">\*</span>'
            r"</legend>",
        )
        cards = self._cards(html)
        self.assertEqual(len(cards), 3)
        for tag, _ in cards:
            for css in (
                "border-2",
                "min-h-11",
                "has-[:checked]:border-violet-600",
                "has-[:checked]:bg-violet-50",
                "has-[:focus-visible]:ring-2",
            ):
                self.assertIn(css, tag)
        bantay, second, none = (inner for _, inner in cards)
        self.assertIn(f'value="{self.pet.pk}"', bantay)
        self.assertIn('aria-label="No photo of Bantay yet"', bantay)
        self.assertIn(
            '<span class="block text-lg font-semibold text-gray-900">Bantay</span>',
            bantay,
        )
        self.assertIn('<span class="block text-sm text-gray-700">Dog</span>', bantay)
        self.assertIn(f'value="{choco.pk}"', second)
        self.assertIn(">Dog · Shih Tzu · 3 yrs</span>", second)
        # "Not for a specific pet" is last, with a receipt icon in a gray tile
        self.assertIn('value="none"', none)
        self.assertIn("Not for a specific pet", none)
        self.assertIn("Supplies or items not for one pet", none)
        self.assertIn("bg-gray-100 text-gray-700", none)
        self.assertEqual(html.count("Not for a specific pet"), 1)
        # Two pets: nothing is picked
        self.assertNotIn(" checked>", html)
        # No JS to style the checked card: CSS only
        self.assertNotIn(
            "<script", html[html.index("<fieldset") :].split("</fieldset>")[0]
        )

    def test_the_pre_picked_card(self):
        html = self._html(self.owner_url)
        bantay, none = (inner for _, inner in self._cards(html))
        self.assertRegex(bantay, r"<input[^>]* checked>")
        self.assertNotIn(" checked>", none)
        jun = self._owner("Jun", "Reyes")
        only = [
            inner for _, inner in self._cards(self._html(f"{self.url}?owner={jun.pk}"))
        ]
        self.assertEqual(len(only), 1)
        self.assertRegex(only[0], r"<input[^>]* checked>")

    def test_error_state(self):
        Pet.objects.create(owner=self.owner, name="Choco", species="Dog")
        response = self.client.post(self.owner_url, {})
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertEqual(html.count('role="alert"'), 1)
        self.assertIn("Please fix the 1 item below.", html)
        self.assertIn("Nothing was saved yet.", html)
        self.assertIn('<fieldset aria-describedby="pet-error">', html)
        self.assertIn(
            '<div class="mt-3 space-y-3 rounded-lg ring-2 ring-red-300 ring-offset-4">',
            html,
        )
        self.assertRegex(
            html,
            r'(?s)<p id="pet-error" class="mt-2 flex items-start gap-1\.5 text-base '
            r'text-red-700">\s*<svg[^>]*aria-hidden="true".*?</svg>\s*'
            + re.escape(PICK_PET_MESSAGE)
            + "</p>",
        )
        # The summary comes first, above the card
        self.assertLess(html.index('role="alert"'), html.index("Billed to"))
        self._nothing_saved()

    def test_no_error_markup_without_an_error(self):
        html = self._html(self.owner_url)
        for text in ("pet-error", "ring-red-300", 'role="alert"', "Please fix"):
            with self.subTest(text=text):
                self.assertNotIn(text, html)

    def test_owner_without_pets(self):
        jun = self._owner("Jun", "Reyes")
        html = self._html(f"{self.url}?owner={jun.pk}")
        self.assertEqual(len(self._cards(html)), 1)
        self.assertIn(
            "Jun Reyes has no pets yet. To bill for a pet, add it on "
            f'<a href="{reverse("admin_owner_detail", args=[jun.pk])}" ',
            html,
        )

    # ─── Buttons ──────────────────────────────────────────────────────────────

    def test_buttons_and_the_note_per_mode(self):
        service = self._service("Consultation")
        pages = {
            "visit with a service": (
                self._visit_url(service=service),
                "You add the other items on the next page.",
            ),
            "visit without a service": (
                self._visit_url(hour=10),
                "The receipt opens empty. You add the items on the next page.",
            ),
            "pet": (
                self.owner_url,
                "The receipt opens empty. You add the items on the next page.",
            ),
        }
        for name, (url, note) in pages.items():
            with self.subTest(page=name):
                html = self._html(url)
                self.assertRegex(
                    html,
                    r'(?s)<p class="flex items-start gap-2 text-sm text-gray-700 '
                    r'sm:mr-auto">\s*<svg[^>]*aria-hidden="true".*?</svg>\s*<span>'
                    + re.escape(note)
                    + "</span></p>",
                )
                self.assertEqual(
                    html.count("The receipt opens empty"), int("empty" in note)
                )
                self.assertIn("flex flex-col-reverse gap-3", html)
                self.assertRegex(
                    html,
                    r'<a href="[^"]+" class="btn btn-outline min-h-12 w-full '
                    r'font-semibold sm:w-auto">Cancel</a>',
                )
                self.assertRegex(
                    html,
                    r'(?s)<button type="submit" class="btn btn-primary min-h-12 '
                    r'w-full font-semibold sm:w-auto">\s*<svg[^>]*aria-hidden="true"'
                    r".*?</svg>\s*Create receipt</button>",
                )
                self.assertEqual(html.count("btn-primary"), 1)

    def test_cancel_goes_back(self):
        visit_url = self._visit_url()
        visit = Appointment.objects.get()
        back = reverse("admin_appointment_detail", args=[visit.pk])
        self.assertIn(f'<a href="{back}" class="btn btn-outline', self._html(visit_url))
        back = reverse("admin_receipt_list")
        self.assertIn(
            f'<a href="{back}" class="btn btn-outline', self._html(self.owner_url)
        )

    def test_no_primary_button_in_the_owner_step(self):
        for url in (self.url, f"{self.url}?q=ana"):
            with self.subTest(url=url):
                html = self._html(url)
                self.assertNotIn("btn-primary", html)
                self.assertNotIn("Create receipt", html)

    # ─── Page rules ───────────────────────────────────────────────────────────

    def test_top_bar_search_is_empty_in_the_owner_step_only(self):
        self.assertNotIn('id="global-search"', self._html(self.url))
        self.assertNotIn('id="global-search"', self._html(f"{self.url}?q=ana"))
        self.assertIn('id="global-search"', self._html(self.owner_url))
        self.assertIn('id="global-search"', self._html(self._visit_url()))

    def test_templates_have_no_inline_handlers_or_faint_text(self):
        for path in RECEIPT_NEW_FILES:
            source = path.read_text(encoding="utf-8")
            with self.subTest(file=path.name):
                self.assertIsNone(re.search(r"\son[a-z]+\s*=", source))
                self.assertNotIn("javascript:", source)
                self.assertNotIn("legacy_css", source)
                self.assertIsNone(re.search(r"text-gray-(100|200|300|400)\b", source))
                self.assertIsNone(re.search(r"\btext-(xs|\[)", source))

    def test_rendered_pages_have_no_inline_handlers(self):
        for url in (
            self.url,
            f"{self.url}?q=ana",
            self.owner_url,
            self._visit_url(),
        ):
            with self.subTest(url=url):
                # The page itself: the layout's own flash and modal markup
                # (shared components) is not part of this page
                body = self._html(url).split(">New receipt</h1>", 1)[1]
                body = body.split("</main>", 1)[0]
                self.assertIn("<form", body)
                self.assertIsNone(re.search(r"\son[a-z]+\s*=\s*[\"']", body))

    def test_no_legacy_css_in_any_mode(self):
        for url in (self.url, self.owner_url, self._visit_url()):
            with self.subTest(url=url):
                self.assertNotIn("layer(legacy)", self._html(url))

    def test_every_link_in_the_pieces_is_on_one_line(self):
        for path in RECEIPT_NEW_FILES:
            for link in re.findall(
                r"(?s)<a\b.*?</a>", path.read_text(encoding="utf-8")
            ):
                with self.subTest(file=path.name, link=link[:50]):
                    self.assertNotIn("\n", link)

    # ─── Queries ──────────────────────────────────────────────────────────────

    def _owner_step_queries(self, total):
        for number in range(PetOwner.objects.count(), total):
            owner = self._owner(f"Owner{number:02d}", "Test", "0917 000 0000")
            Pet.objects.create(owner=owner, name=f"Pet{number}", species="Cat")
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(self.url)
        self.assertEqual(len(response.context["owners"]), min(total, 25))
        return len(queries)

    def test_owner_step_query_count_is_flat(self):
        few, many = self._owner_step_queries(3), self._owner_step_queries(30)
        self.assertEqual(few, many)
        self.assertEqual(many, NEW_OWNER_STEP_QUERIES)

    def _pet_step_queries(self, total):
        for number in range(Pet.objects.filter(owner=self.owner).count(), total):
            Pet.objects.create(owner=self.owner, name=f"Pet{number}", species="Dog")
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(self.owner_url)
        self.assertEqual(len(response.context["pets"]), total)
        return len(queries)

    def test_pet_step_query_count_is_flat(self):
        one, five = self._pet_step_queries(1), self._pet_step_queries(5)
        self.assertEqual(one, five)
        self.assertEqual(five, NEW_PET_STEP_QUERIES)


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

    def test_old_context_keys_are_kept(self):
        """status_filter and counts are read by no template since 6b-1 (the
        page is the new design) but are still returned."""
        self._tabs_fixture()
        response = self.client.get(self.url)
        self.assertTemplateUsed(response, "admin/billing/receipt_list.html")
        context = response.context
        self.assertEqual(context["status_filter"], "pending")
        self.assertEqual(
            context["counts"], {"all": 9, "pending": 3, "paid": 4, "cancelled": 2}
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
                    # 6b-2: the new "help" key (the price_help text)
                    "help": "Price list: ₱150.00.",
                },
                {
                    "value": str(ranged.pk),
                    "label": "grooming · ₱300.00 to ₱800.00",
                    "name": "grooming",
                    "pricing": "range",
                    "price": None,
                    "min_price": Decimal("300.00"),
                    "max_price": Decimal("800.00"),
                    "help": (
                        "Usual price ₱300.00 to ₱800.00. You can bill outside it."
                    ),
                },
            ],
        )
        # The same text the form works out for a re-render
        for option in options:
            with self.subTest(value=option["value"]):
                form = BillingItemForm({"service_choice": option["value"]})
                self.assertEqual(option["help"], form.price_help)

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
        # 6b-2: the page shows the error under the field (was the old
        # page's <p class="form-error">)
        self.assertRegex(response.content.decode(), error_line("Type the price."))

    def test_page_shows_every_item_error(self):
        response = self._post(
            "admin_billing_item_add",
            self.receipt.pk,
            {"service_choice": "", "quantity": "0"},
        )
        html = response.content.decode()
        self.assertRegex(html, error_line("Choose what it is, or Other (type it)."))
        self.assertRegex(html, error_line("The quantity must be from 1 to 999."))

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

# 5 until 8a-2a; the Bills page now also loads the unpaid list and the paid
# History (a count and a page): 3 more, still flat
OWNER_LIST_QUERIES = 8
# 7 until 6b-1; the shared Billing tabs add the receipt counts (one aggregate)
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
        # 6b-2: the restyled printout shows the label as a stamp, upper case
        # (was the "Payment Status:" field)
        self.assertContains(response, '<p class="receipt-print-stamp">UNPAID</p>')


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
        # 6b-2: the restyled printout shows the status as a stamp (was the
        # "Payment Status:" field)
        self.assertIn('<p class="receipt-print-stamp">PAID</p>', html)


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
        # The status is four radio cards now (was a select): Hidden (draft) is checked
        self.assertRegex(
            response.content.decode(),
            r'<input type="radio" name="status" id="id_status_2" value="draft" '
            r'class="[^"]*" checked>',
        )

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


# ── Slice 6b-1: the receipt list page ─────────────────────────────────────────

BILLING_TEMPLATES = Path(settings.BASE_DIR) / "templates" / "admin" / "billing"
RECEIPT_LIST_SOURCES = [
    BILLING_TEMPLATES / "receipt_list.html",
    BILLING_TEMPLATES / "billing_parts" / "tabs.html",
    *sorted((BILLING_TEMPLATES / "receipt_list_parts").glob("*.html")),
]
ACTIVE_NAV = (
    'class="flex min-h-11 items-center gap-3 rounded-lg px-3 text-base bg-white '
    'font-semibold text-violet-800 shadow-sm ring-1 ring-violet-200" '
    'aria-current="page">'
)
ROW = '<li class="border-b border-gray-100 last:border-b-0">'
PHONE_CARD = '<div class="relative p-4'
PILL = "rounded-full {} px-2 text-sm font-semibold"


@PLAIN_STATIC
class ReceiptListPageTests(FrozenBillingTestBase):
    """admin/billing/receipt_list.html on layouts/admin.html (Slice 6b-1),
    at 00:30 Manila on Sat, Nov 7, 2026."""

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

    # ─── Helpers ──────────────────────────────────────────────────────────────

    def _visit(self, day, hour, minute=0):
        return Appointment.objects.create(
            owner=self.owner,
            pet=self.pet,
            date=day,
            time=datetime.time(hour, minute),
            status=Appointment.COMPLETED,
        )

    def _make(
        self, number, status=BillingReceipt.PENDING, hours_ago=0, price=None, **f
    ):
        """A receipt created hours_ago before now (Ana and Bantay unless
        given), with one item at price when price is given."""
        f.setdefault("owner", self.owner)
        f.setdefault("pet", self.pet)
        receipt = BillingReceipt.objects.create(
            receipt_number=number, payment_status=status, **f
        )
        created = self.now - datetime.timedelta(hours=hours_ago)
        BillingReceipt.objects.filter(pk=receipt.pk).update(created_at=created)
        if price:
            self._item(receipt, price)
            receipt.calculate_totals()
        return receipt

    def _fixture(self):
        """Two unpaid, two paid and one cancelled receipt."""
        day = datetime.timedelta(days=1)
        self.a = self._make(
            "HV-2026-0012", price="630.00", appointment=self._visit(TODAY, 9)
        )
        self.b = self._make("HV-2026-0013", hours_ago=48, owner=self.ben, pet=None)
        self.c = self._make(
            "HV-2026-0010",
            BillingReceipt.PAID,
            30,
            price="1450.00",
            appointment=self._visit(TODAY - 2 * day, 14, 30),
            payment_date=TODAY - day,
        )
        self.d = self._make(
            "HV-2026-0007",
            BillingReceipt.PAID,
            72,
            price="385.00",
            owner=self.ben,
            pet=self.luna,
            payment_date=TODAY - 3 * day,
        )
        self.e = self._make(
            "HV-2026-0011", BillingReceipt.CANCELLED, 24, price="300.00"
        )

    def _html(self, **params):
        response = self.client.get(self.url, params)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def _tabs(self, html):
        return re.search(
            r'(?s)<nav [^>]*aria-label="Billing sections">.*?</nav>', html
        ).group()

    def _row(self, html, number):
        """(desktop part, phone card) of one receipt's row."""
        rows = [
            row
            for row in re.findall(rf"(?s){re.escape(ROW)}.*?</li>", html)
            if f"receipt {number}</span>" in row
        ]
        self.assertEqual(len(rows), 1)
        desktop, phone = rows[0].split(PHONE_CARD)
        return desktop, PHONE_CARD + phone

    def _empty(self, html):
        """(title, text) of the page's empty state (text None when absent)."""
        title = re.search(
            r'<p class="text-lg font-semibold text-gray-900">(.*?)</p>', html
        )
        text = re.search(
            r'<p class="mt-1 max-w-sm text-base text-gray-600">(.*?)</p>', html
        )
        return (title.group(1) if title else None, text.group(1) if text else None)

    def _open_link(self, receipt):
        return (
            f'<a href="{reverse("admin_receipt_detail", args=[receipt.pk])}" '
            'class="inline-flex min-h-11 items-center justify-center gap-2 '
            "whitespace-nowrap rounded-lg border border-violet-300 bg-violet-50 px-5 "
            "text-base font-semibold text-violet-800 hover:border-violet-400 "
            f'hover:bg-violet-100">Open<span class="sr-only"> receipt '
            f"{receipt.receipt_number}</span>"
        )

    def _owner_link(self, owner):
        return (
            f'<a href="{reverse("admin_owner_detail", args=[owner.pk])}" '
            'class="font-medium text-blue-600 underline underline-offset-2">'
            f"{owner.full_name}</a>"
        )

    # ─── Layout, header, sidebar ──────────────────────────────────────────────

    def test_new_layout_without_old_files(self):
        self._fixture()
        response = self.client.get(self.url)
        self.assertTemplateUsed(response, "layouts/admin.html")
        self.assertTemplateUsed(response, "admin/billing/receipt_list.html")
        self.assertTemplateUsed(response, "admin/billing/billing_parts/tabs.html")
        self.assertTemplateNotUsed(response, "admin/billing/_billing_tabs.html")
        for old in (
            "receipt_list.css",
            "receipt_list.js",
            "receipt_detail.css",
            "layer(legacy)",
            "admin-page",
            "data-table",
            "filter-tab",
            "SERVICE_DETAILS_URL",
        ):
            with self.subTest(old=old):
                self.assertNotContains(response, old)
        self.assertContains(response, "<title>Billing — Hapi Vet Admin</title>")
        self.assertContains(
            response, '<h1 class="text-2xl font-bold text-gray-900">Billing</h1>'
        )
        self.assertContains(
            response, "Receipts for visits and supplies. Payment is cash."
        )
        source = (BILLING_TEMPLATES / "receipt_list.html").read_text(encoding="utf-8")
        for word in ("legacy_css", "extra_js", "messages"):
            with self.subTest(word=word):
                self.assertNotIn(word, source)

    def test_no_template_loads_the_legacy_files(self):
        for path in BILLING_TEMPLATES.parents[1].rglob("*.html"):
            source = path.read_text(encoding="utf-8")
            for old in (
                "js/admin/receipt_list.js",
                "css/admin/receipt_list.css",
                "_billing_tabs.html",
            ):
                with self.subTest(file=path.name, old=old):
                    self.assertNotIn(old, source)

    def test_header_has_the_one_primary_button(self):
        self._fixture()
        for tab in RECEIPT_TABS:
            with self.subTest(tab=tab):
                html = self._html(status=tab)
                self.assertEqual(html.count("btn-primary"), 1)
                self.assertRegex(
                    html,
                    r'(?s)<a href="'
                    + re.escape(reverse("admin_receipt_create"))
                    + r'" class="btn btn-primary">\s*<svg[^>]*aria-hidden="true"'
                    r"[^>]*>.*?</svg>\s*New receipt</a>",
                )

    def test_sidebar_highlights_billing(self):
        html = self._html()
        self.assertEqual(html.count(ACTIVE_NAV), 1)
        self.assertIn(f'<a href="{self.url}" {ACTIVE_NAV}', html)

    def test_search_forms_with_the_hidden_status(self):
        for tab in RECEIPT_TABS:
            with self.subTest(tab=tab):
                html = self._html(status=tab, q="cruz")
                self.assertEqual(html.count('role="search"'), 2)
                for prefix, form_class in (
                    ("topbar", "hidden min-w-0 flex-1 items-center gap-3 md:flex"),
                    ("page", "mb-4 flex items-center gap-3 md:hidden"),
                ):
                    self.assertIn(
                        f'<form method="get" action="{self.url}" '
                        f'class="{form_class}" role="search">',
                        html,
                    )
                    self.assertIn(
                        f'<label for="{prefix}-receipt-search" class="sr-only">'
                        "Search receipts</label>",
                        html,
                    )
                    self.assertIn(
                        f'<input id="{prefix}-receipt-search" type="search" '
                        'name="q" value="cruz" placeholder="Search receipts: '
                        'number, owner or pet" class="input pl-10">',
                        html,
                    )
                self.assertEqual(
                    html.count(f'<input type="hidden" name="status" value="{tab}">'), 2
                )
                self.assertEqual(
                    html.count(
                        '<button type="submit" class="btn btn-outline font-semibold">'
                    ),
                    2,
                )
        # No ?status=: the open tab is Unpaid
        self.assertIn(
            '<input type="hidden" name="status" value="pending">', self._html()
        )

    # ─── Tabs ─────────────────────────────────────────────────────────────────

    def test_tabs_counts_and_current(self):
        self._fixture()
        for tab in RECEIPT_TABS:
            with self.subTest(tab=tab):
                tabs = self._tabs(self._html(status=tab))
                self.assertEqual(tabs.count('aria-current="page"'), 1)
                self.assertRegex(
                    tabs,
                    rf'<a href="{self.url}\?status={tab}" class="[^"]*" '
                    r'aria-current="page">',
                )
        tabs = self._tabs(self._html(status="paid"))
        self.assertIn(
            '">Unpaid<span class="'
            + PILL.format("bg-yellow-200 text-yellow-900")
            + '">2<span class="sr-only"> waiting</span></span></a>',
            tabs,
        )
        self.assertIn(
            'aria-current="page">Paid<span class="'
            + PILL.format("bg-violet-100 text-violet-800")
            + '">2</span></a>',
            tabs,
        )
        self.assertIn(
            '">Cancelled<span class="'
            + PILL.format("bg-gray-100 text-gray-800")
            + '">1</span></a>',
            tabs,
        )
        self.assertIn(
            '">All<span class="'
            + PILL.format("bg-gray-100 text-gray-800")
            + '">5</span></a>',
            tabs,
        )
        # Unpaid open and with receipts waiting: still yellow
        tabs = self._tabs(self._html())
        self.assertIn(
            'aria-current="page">Unpaid<span class="'
            + PILL.format("bg-yellow-200 text-yellow-900")
            + '">2<span class="sr-only"> waiting</span></span></a>',
            tabs,
        )

    def test_the_unpaid_count_is_yellow_only_above_zero(self):
        self._make("HV-2026-0001", BillingReceipt.PAID, payment_date=TODAY)
        for tab in ("pending", "paid"):
            with self.subTest(tab=tab):
                tabs = self._tabs(self._html(status=tab))
                self.assertNotIn("bg-yellow-200", tabs)
                self.assertNotIn("waiting", tabs)
        self.assertIn(
            'aria-current="page">Unpaid<span class="'
            + PILL.format("bg-violet-100 text-violet-800")
            + '">0</span></a>',
            self._tabs(self._html()),
        )
        self.assertIn(
            '">Unpaid<span class="'
            + PILL.format("bg-gray-100 text-gray-800")
            + '">0</span></a>',
            self._tabs(self._html(status="paid")),
        )

    def test_tab_links_keep_q_and_drop_page(self):
        for number in range(30):
            self._make(f"HV-2026-{number + 1:04d}")
        tabs = self._tabs(self._html(status="all", q="ana cruz", page=2))
        for tab in RECEIPT_TABS:
            with self.subTest(tab=tab):
                self.assertIn(f'href="{self.url}?status={tab}&amp;q=ana%20cruz"', tabs)
        self.assertNotIn("page=", tabs)
        # No search: the links are just the status
        tabs = self._tabs(self._html())
        self.assertIn(f'href="{self.url}?status=paid" ', tabs)
        self.assertNotIn("q=", tabs)

    def test_four_tabs_only_and_no_services_link(self):
        for q in ("", "ana"):
            with self.subTest(q=q):
                tabs = self._tabs(self._html(q=q) if q else self._html())
                self.assertEqual(tabs.count("<a "), 4)
                for label in ("Unpaid", "Paid", "Cancelled", "All"):
                    self.assertIn(f">{label}<span", tabs)
                self.assertNotIn("Services and prices", tabs)
                self.assertNotIn(reverse("admin_service_list"), tabs)
                self.assertNotIn("w-px", tabs)
                self.assertNotIn("<svg", tabs)

    def test_phone_tabs_are_a_two_by_two_grid(self):
        tabs = self._tabs(self._html())
        self.assertIn("grid grid-cols-2 gap-2", tabs)
        self.assertIn("md:flex md:flex-wrap md:items-center", tabs)
        # Was assertIn: the Services link used col-span-2 under the grid
        self.assertNotIn("col-span-2", tabs)

    # ─── Heads, note, results line ────────────────────────────────────────────

    def test_list_heads(self):
        self._fixture()
        heads = {
            "pending": ("Unpaid receipts", 2, "Oldest first"),
            "paid": ("Paid receipts", 2, "Newest paid first"),
            "cancelled": ("Cancelled receipts", 1, "Newest first"),
            "all": ("All receipts", 5, "Newest first"),
        }
        for tab, (title, count, sort) in heads.items():
            with self.subTest(tab=tab):
                html = self._html(status=tab)
                self.assertIn(
                    f'<h2 class="text-xl font-semibold text-gray-900">{title} '
                    f'<span class="font-normal text-gray-700">({count})</span></h2>',
                    html,
                )
                self.assertIn(f'<p class="text-sm text-gray-700">{sort}</p>', html)

    def test_column_names_row(self):
        self._fixture()
        html = self._html()
        self.assertRegex(
            html,
            r'(?s)aria-hidden="true">\s*<span>Receipt</span><span></span>'
            r'<span>Billed to</span><span class="text-right">Total</span><span></span>',
        )
        self.assertNotIn(
            "<span>Billed to</span>", self._html(status="cancelled", q="zzz")
        )

    def test_results_line_one_many_none_and_clear_search(self):
        self._fixture()
        cases = [
            ("pending", "0012", "1 receipt matches", "unpaid receipts"),
            ("pending", "hv", "2 receipts match", "unpaid receipts"),
            ("pending", "zzz", "No receipt matches", "unpaid receipts"),
            ("paid", "hv", "2 receipts match", "paid receipts"),
            ("cancelled", "hv", "1 receipt matches", "cancelled receipts"),
            ("cancelled", "zzz", "No receipt matches", "cancelled receipts"),
            ("all", "hv", "5 receipts match", "all receipts"),
        ]
        for tab, q, start, scope in cases:
            with self.subTest(tab=tab, q=q):
                html = self._html(status=tab, q=q)
                self.assertIn(
                    f'<h2 class="text-base font-normal text-gray-800" '
                    f'aria-live="polite">{start} <strong class="font-semibold">'
                    f"“{q}”</strong> in {scope}</h2>",
                    html,
                )
                # Clear search keeps the tab and drops q
                self.assertRegex(
                    html,
                    rf'(?s)<a href="{self.url}\?status={tab}" class="[^"]*">'
                    r".*?</svg>\s*Clear search</a>",
                )
                # The tab's heading is replaced by the results line
                self.assertNotIn('<span class="font-normal text-gray-700">(', html)

    def test_the_cancelled_note_only_on_cancelled_with_rows(self):
        note = "Cancelled receipts are kept for the clinic. Owners don't see them, and they can't be changed."
        self.assertNotIn(note, self._html(status="cancelled"))
        self._fixture()
        for tab in RECEIPT_TABS:
            with self.subTest(tab=tab):
                html = self._html(status=tab)
                self.assertEqual(html.count(note), 1 if tab == "cancelled" else 0)
        self.assertRegex(
            self._html(status="cancelled"),
            r'(?s)<svg[^>]*aria-hidden="true"[^>]*>(?:(?!</svg>).)*</svg>\s*<p>'
            + re.escape(note),
        )

    # ─── Rows ─────────────────────────────────────────────────────────────────

    def test_unpaid_rows(self):
        self._fixture()
        html = self._html()
        # Oldest first
        self.assertLess(html.index("HV-2026-0013"), html.index("HV-2026-0012"))
        desktop, phone = self._row(html, "HV-2026-0012")
        for text in (
            'text-lg font-semibold text-gray-900">HV-2026-0012</p>',
            '<p class="text-sm text-gray-700">Created Today · From the visit Today '
            "at 9:00 AM</p>",
            self._owner_link(self.owner),
            '<p class="truncate text-sm text-gray-700">for Bantay</p>',
            '<p class="text-lg font-semibold text-gray-900">₱630.00</p>',
            self._open_link(self.a),
        ):
            with self.subTest(text=text):
                self.assertIn(text, desktop)
        self.assertNotIn("No items yet", desktop)
        self.assertNotIn("Paid ", desktop)
        desktop, phone = self._row(html, "HV-2026-0013")
        for text in (
            "Created Thu, Nov 5 · Over the counter</p>",
            self._owner_link(self.ben),
            '<p class="truncate text-sm text-gray-700">not for a specific pet</p>',
            '<p class="text-lg font-semibold text-gray-900">₱0.00</p>',
            '<p class="text-sm text-gray-700">No items yet</p>',
        ):
            with self.subTest(text=text):
                self.assertIn(text, desktop)

    def test_paid_rows_show_when_they_were_paid(self):
        self._fixture()
        html = self._html(status="paid")
        # Newest paid first
        self.assertLess(html.index("HV-2026-0010"), html.index("HV-2026-0007"))
        desktop, _ = self._row(html, "HV-2026-0010")
        self.assertIn(
            '<p class="text-sm text-gray-700">Paid Yesterday · From the visit '
            "Thu, Nov 5 at 2:30 PM</p>",
            desktop,
        )
        self.assertIn("₱1,450.00</p>", desktop)
        self.assertNotIn("Created", desktop)
        desktop, _ = self._row(html, "HV-2026-0007")
        self.assertIn("Paid Wed, Nov 4 · Over the counter</p>", desktop)
        self.assertIn("for Luna</p>", desktop)
        self.assertNotIn("line-through", html)

    def test_a_paid_receipt_without_a_payment_date_says_created(self):
        self._make("HV-2026-0001", BillingReceipt.PAID)
        desktop, _ = self._row(self._html(status="paid"), "HV-2026-0001")
        self.assertIn("Created Today · Over the counter</p>", desktop)

    def test_status_badges_only_on_all(self):
        self._fixture()
        html = self._html(status="all")
        badges = {
            "HV-2026-0012": '<span class="badge bg-yellow-100 text-yellow-800">Unpaid</span>',
            "HV-2026-0010": '<span class="badge bg-green-100 text-green-800">Paid</span>',
            "HV-2026-0011": '<span class="badge bg-red-100 text-red-800">Cancelled</span>',
        }
        for number, badge in badges.items():
            with self.subTest(number=number):
                desktop, phone = self._row(html, number)
                self.assertEqual(desktop.count(badge), 1)
                self.assertEqual(phone.count(badge), 1)
        for tab in ("pending", "paid", "cancelled"):
            with self.subTest(tab=tab):
                self.assertNotIn('<span class="badge ', self._html(status=tab))

    def test_cancelled_rows_are_gray_and_struck_through(self):
        self._fixture()
        html = self._html(status="all")
        tile = "bg-gray-100 text-gray-700 ring-gray-300"
        avatar = (
            "rounded-full bg-gray-100 font-semibold tracking-wide text-gray-700 "
            "ring-1 ring-gray-300"
        )
        struck = (
            '<p class="text-lg font-semibold text-gray-700 line-through '
            'decoration-gray-500">₱300.00</p>'
        )
        desktop, phone = self._row(html, "HV-2026-0011")
        for part in (desktop, phone):
            self.assertIn(avatar, part)
            self.assertIn(struck, part)
        self.assertIn(tile, desktop)
        for number in ("HV-2026-0012", "HV-2026-0010", "HV-2026-0013"):
            with self.subTest(number=number):
                for part in self._row(html, number):
                    self.assertNotIn("line-through", part)
                    self.assertNotIn("ring-gray-300", part)
        # Not on the other tabs
        for tab in ("pending", "paid"):
            with self.subTest(tab=tab):
                other = self._html(status=tab)
                self.assertNotIn("line-through", other)
                self.assertNotIn("ring-gray-300", other)

    def test_a_receipt_without_an_owner_still_lists(self):
        self._make("HV-2026-0001", owner=None, pet=None)
        html = self._html()
        desktop, phone = self._row(html, "HV-2026-0001")
        for part in (desktop, phone):
            self.assertIn("No owner on file", part)
            self.assertNotIn("/admin/pets/owners/", part)

    def test_phone_cards(self):
        self._fixture()
        html = self._html()
        _, phone = self._row(html, "HV-2026-0012")
        self.assertIn(
            '<p class="flex flex-wrap items-center gap-2 text-lg font-semibold '
            'text-gray-900">HV-2026-0012</p>',
            phone,
        )
        self.assertIn(
            '<p class="text-lg font-semibold text-gray-900">₱630.00</p>', phone
        )
        self.assertIn(
            '<p class="text-sm text-gray-700">Created Today · From the visit Today '
            "at 9:00 AM</p>",
            phone,
        )
        # The owner in a gray box, as plain text: no link inside the card link
        self.assertIn(
            '<div class="mt-2 flex min-w-0 items-center gap-3 rounded-lg bg-gray-50 p-3">',
            phone,
        )
        self.assertIn(
            '<p class="truncate text-base font-medium text-gray-900">Ana Cruz</p>',
            phone,
        )
        self.assertNotIn("/admin/pets/owners/", phone)
        # One link: Open, full width, stretched over the whole card
        self.assertEqual(phone.count("<a "), 1)
        self.assertIn(
            f'<a href="{reverse("admin_receipt_detail", args=[self.a.pk])}" '
            'class="mt-3 flex min-h-11 w-full items-center justify-center gap-2 '
            "rounded-lg border border-violet-300 bg-violet-50 text-base "
            "font-semibold text-violet-800 after:absolute after:inset-0 "
            'hover:bg-violet-100">Open',
            phone,
        )
        _, phone = self._row(html, "HV-2026-0013")
        self.assertIn('<p class="mt-2 text-sm text-gray-700">No items yet</p>', phone)
        # The grid row and the card swap at md
        desktop, _ = self._row(html, "HV-2026-0012")
        self.assertIn('<div class="hidden items-center gap-5', desktop)
        self.assertIn(
            "md:grid md:grid-cols-[minmax(0,1.1fr)_1px_minmax(0,1fr)_9rem_7.5rem]",
            desktop,
        )
        self.assertIn("md:hidden", phone)

    def test_dates_follow_the_views_today(self):
        self._fixture()
        # created_at is Sat, Nov 7 00:30 Manila, which is still Nov 6 in UTC
        self.assertIn("Created Today · From the visit Today at 9:00 AM", self._html())
        self.now = JUST_AFTER_MIDNIGHT + datetime.timedelta(days=1)
        html = self._html()
        self.assertIn("Created Yesterday · From the visit Yesterday at 9:00 AM", html)
        self.assertIn("Created Thu, Nov 5 · Over the counter", html)
        self.assertIn("Paid Fri, Nov 6 · From the visit", self._html(status="paid"))

    # ─── Empty states ─────────────────────────────────────────────────────────

    def test_empty_states(self):
        cases = {
            "pending": ("Nothing unpaid", "Every receipt is paid or cancelled."),
            "paid": ("No paid receipts yet", None),
            "cancelled": ("No cancelled receipts", None),
            "all": (
                "No receipts yet",
                "Make one with New receipt, or from a visit's page.",
            ),
        }
        for tab, expected in cases.items():
            with self.subTest(tab=tab):
                html = self._html(status=tab)
                self.assertEqual(self._empty(html), expected)
                self.assertNotIn("text-xl font-semibold", html)
                self.assertNotIn("<span>Billed to</span>", html)
                self.assertNotIn("Showing", html)

    def test_nothing_unpaid_links_to_paid_receipts(self):
        self._make("HV-2026-0001", BillingReceipt.PAID, payment_date=TODAY)
        html = self._html()
        self.assertEqual(self._empty(html)[0], "Nothing unpaid")
        self.assertIn(
            f'<a href="{self.url}?status=paid" class="inline-flex min-h-11 '
            "items-center gap-2 text-base font-medium text-blue-600 underline "
            'underline-offset-2">See paid receipts',
            html,
        )
        self.assertNotIn("See paid receipts", self._html(status="paid"))
        self.assertNotIn("bg-yellow-200", self._tabs(html))

    def test_no_match_states(self):
        self._fixture()
        text = "Check the spelling, try another tab, or clear the search."
        scopes = {
            "pending": "unpaid receipts",
            "paid": "paid receipts",
            "cancelled": "cancelled receipts",
            "all": "all receipts",
        }
        for tab, scope in scopes.items():
            with self.subTest(tab=tab):
                html = self._html(status=tab, q="zzz")
                self.assertEqual(
                    self._empty(html), (f"No receipt matches “zzz” in {scope}", text)
                )
                self.assertEqual(html.count("Clear search"), 1)
        # A search with no rows never shows the Cancelled note
        self.assertNotIn(
            "Cancelled receipts are kept", self._html(status="cancelled", q="zzz")
        )

    def test_the_search_is_escaped(self):
        html = self._html(status="all", q="<i>x</i>")
        self.assertNotIn("<i>x</i>", html)
        self.assertIn("&lt;i&gt;x&lt;/i&gt;", html)

    # ─── Pager ────────────────────────────────────────────────────────────────

    def test_pager_keeps_status_and_q(self):
        for number in range(30):
            self._make(f"HV-2026-{number + 1:04d}")
        html = self._html(status="all", q="cruz")
        self.assertIn("Showing 1–25 of 30", html)
        self.assertIn(
            '<a href="?status=all&amp;q=cruz&amp;page=2" class="btn btn-outline">',
            html,
        )
        self.assertEqual(html.count(ROW), 25)
        html = self._html(status="all", q="cruz", page=2)
        self.assertIn("Showing 26–30 of 30", html)
        self.assertIn(
            '<a href="?status=all&amp;q=cruz&amp;page=1" class="btn btn-outline">', html
        )
        self.assertEqual(html.count(ROW), 5)
        # No search: the status alone
        self.assertIn('href="?status=pending&amp;page=2"', self._html())

    def test_one_page_says_showing_all(self):
        self._fixture()
        self.assertIn("Showing all 2", self._html())

    # ─── Queries, links, other pages ──────────────────────────────────────────

    def _queries_for(self, count):
        BillingReceipt.objects.all().delete()
        Appointment.objects.all().delete()
        for number in range(count):
            self._make(
                f"HV-2026-{number + 1:04d}",
                price="10.00",
                appointment=self._visit(TODAY - datetime.timedelta(days=number), 9),
            )
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(self.url, {"status": "all"})
        self.assertEqual(len(response.context["receipts"]), min(count, 25))
        return len(queries)

    def test_full_page_query_count_is_flat(self):
        few, many = self._queries_for(3), self._queries_for(30)
        self.assertEqual(few, many)
        self.assertEqual(many, LIST_QUERIES)

    def test_every_link_in_the_templates_is_on_one_line(self):
        for path in RECEIPT_LIST_SOURCES:
            links = re.findall(r"(?s)<a\b.*?</a>", path.read_text(encoding="utf-8"))
            for link in links:
                with self.subTest(file=path.name, link=link[:60]):
                    self.assertNotIn("\n", link)
        self.assertTrue(
            any("<a " in p.read_text(encoding="utf-8") for p in RECEIPT_LIST_SOURCES)
        )

    def test_the_dashboards_unpaid_bills_link_opens_the_unpaid_tab(self):
        self._fixture()
        dashboard = self.client.get(reverse("admin_dashboard")).content.decode()
        href = re.search(
            r'<a href="([^"]*)"[^>]*>(?:(?!</a>).)*Unpaid bills</span>', dashboard, re.S
        ).group(1)
        self.assertEqual(href, f"{self.url}?status=pending")
        html = self.client.get(href).content.decode()
        self.assertRegex(
            self._tabs(html),
            rf'<a href="{self.url}\?status=pending" class="[^"]*" aria-current="page">',
        )
        self.assertIn("Unpaid receipts <span", html)

    def test_the_services_page_has_no_billing_tabs(self):
        self._fixture()
        services = reverse("admin_service_list")
        response = self.client.get(services)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "admin/billing/services_list.html")
        self.assertTemplateNotUsed(response, "admin/billing/billing_parts/tabs.html")
        self.assertTemplateNotUsed(response, "admin/billing/_billing_tabs.html")
        html = response.content.decode()
        self.assertNotIn("layer(legacy)", html)
        self.assertNotIn('aria-label="Billing sections"', html)
        # Billing is not highlighted in the sidebar; Services and prices is
        self.assertNotIn(f'<a href="{self.url}" {ACTIVE_NAV}', html)
        self.assertIn(f'<a href="{services}" {ACTIVE_NAV}', html)


# ── Slice 6b-2: the receipt page ──────────────────────────────────────────────

SHARED_BILLING = Path(settings.BASE_DIR) / "templates" / "shared" / "billing"
RECEIPT_PAGE_SOURCES = [
    BILLING_TEMPLATES / "receipt_detail.html",
    *sorted((BILLING_TEMPLATES / "receipt_detail_parts").glob("*.html")),
    SHARED_BILLING / "_receipt_print.html",
]
RECEIPT_JS = Path(settings.BASE_DIR) / "static" / "js" / "admin" / "receipt_detail.js"
LINK = "font-medium text-blue-600 underline underline-offset-2"


def dialogs_in(markup):
    """{dialog id: its markup} for every <dialog> on the page."""
    return {
        m.group(1): m.group(0)
        for m in re.finditer(r'(?s)<dialog id="([^"]+)".*?</dialog>', markup)
    }


def primary_labels(markup):
    """The text of every btn-primary outside the dialogs."""
    outside = re.sub(r"(?s)<dialog.*?</dialog>", "", markup)
    found = re.findall(
        r'(?s)<(?:button|a)\b[^>]*class="[^"]*btn-primary[^"]*"[^>]*>(.*?)</(?:button|a)>',
        outside,
    )
    return [page_text(part) for part in found]


def section(markup, title_id):
    """One card by its aria-labelledby id."""
    return re.search(
        rf'(?s)<section [^>]*aria-labelledby="{title_id}".*?</section>', markup
    ).group()


def button_labels(markup):
    return [page_text(b) for b in re.findall(r"(?s)<button\b.*?</button>", markup)]


@PLAIN_STATIC
class ReceiptPageTests(FrozenBillingTestBase):
    """admin/billing/receipt_detail.html on layouts/admin.html (Slice 6b-2),
    at 00:30 Manila on Sat, Nov 7, 2026."""

    def setUp(self):
        super().setUp()
        PetOwner.objects.filter(pk=self.owner.pk).update(contact_number="0917 555 0142")
        self.owner.refresh_from_db()
        self.consult = self._service("Consultation", "300.00")
        self.visit = Appointment.objects.create(
            owner=self.owner,
            pet=self.pet,
            date=TODAY,
            time=datetime.time(9, 0),
            status=Appointment.COMPLETED,
        )

    # ─── Helpers ──────────────────────────────────────────────────────────────

    def _make(
        self, status=BillingReceipt.PENDING, items=True, visit=None, pet=True, **fields
    ):
        """A receipt: Consultation (from the price list, ₱300.00) and
        Amoxicillin 250 mg (10 × ₱15.00) = ₱450.00, or no items."""
        receipt = BillingReceipt.objects.create(
            receipt_number=BillingReceipt.generate_receipt_number(),
            owner=self.owner,
            pet=self.pet if pet else None,
            appointment=visit,
            payment_status=status,
            billing_date=TODAY,
            payment_date=TODAY if status == BillingReceipt.PAID else None,
            **fields,
        )
        if items:
            self._item(
                receipt, "300.00", description="Consultation", service=self.consult
            )
            self._item(receipt, "15.00", 10, "Amoxicillin 250 mg")
        receipt.calculate_totals()
        return receipt

    def _html(self, receipt):
        response = self.client.get(reverse("admin_receipt_detail", args=[receipt.pk]))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def _header(self, markup):
        return re.search(
            r'(?s)<a href="[^"]*" class="mb-2 inline-flex.*?'
            r'(?=<div class="mb-6 flex items-start gap-3)',
            markup,
        ).group()

    def _banner(self, markup):
        return page_text(
            re.search(
                r'(?s)<div class="mb-6 flex items-start gap-3.*?</div>', markup
            ).group()
        )

    def _rows(self, markup):
        return re.findall(r'(?s)<li class="grid grid-cols.*?</li>', markup)

    def _printout(self, markup):
        return page_text(markup[markup.index('id="printable-receipt"') :])

    # ─── Layout, sidebar, legacy ──────────────────────────────────────────────

    def test_layout_title_and_parts(self):
        receipt = self._make()
        response = self.client.get(reverse("admin_receipt_detail", args=[receipt.pk]))
        self.assertTemplateUsed(response, "layouts/admin.html")
        self.assertTemplateUsed(response, "admin/billing/receipt_detail.html")
        for part in (
            "header",
            "banner",
            "items_card",
            "totals",
            "actions_card",
            "billed_card",
            "info_card",
            "add_item_dialog",
            "discount_dialog",
            "remove_item_dialog",
            "mark_paid_dialog",
            "cancel_dialog",
        ):
            with self.subTest(part=part):
                self.assertTemplateUsed(
                    response, f"admin/billing/receipt_detail_parts/{part}.html"
                )
        self.assertContains(
            response,
            f"<title>Receipt {receipt.receipt_number} — Hapi Vet Admin</title>",
        )
        for old in (
            "receipt_detail.css",
            "layer(legacy)",
            "admin-page",
            "detail-card",
            "SERVICE_DETAILS_URL",
            "service-details",
        ):
            with self.subTest(old=old):
                self.assertNotContains(response, old)
        self.assertContains(response, "js/admin/receipt_detail.js")

    def test_templates_use_no_legacy_files_modals_or_inline_handlers(self):
        source = (BILLING_TEMPLATES / "receipt_detail.html").read_text(encoding="utf-8")
        for word in ("legacy_css", "modals"):
            with self.subTest(word=word):
                self.assertNotIn(word, source)
        folders = [BILLING_TEMPLATES, SHARED_BILLING]
        for folder in folders:
            for path in folder.rglob("*.html"):
                text = path.read_text(encoding="utf-8")
                for old in ("openConfirmModal", "receipt_detail.css", "modals.html"):
                    with self.subTest(file=path.name, old=old):
                        self.assertNotIn(old, text)
                with self.subTest(file=path.name, rule="no inline handlers"):
                    self.assertIsNone(re.search(r"<[^>]*\son[a-z]+\s*=", text))

    def test_the_script_has_no_inline_handlers_urls_or_fetch(self):
        script = RECEIPT_JS.read_text(encoding="utf-8")
        for word in (
            "fetch(",
            "XMLHttpRequest",
            "onclick",
            "{% url",
            "service-details",
        ):
            with self.subTest(word=word):
                self.assertNotIn(word, script)
        self.assertIn("[data-print]", script)

    def test_service_details_view_and_url_are_gone(self):
        self.assertFalse(hasattr(billing_views, "admin_get_service_details"))
        with self.assertRaises(NoReverseMatch):
            reverse("admin_get_service_details")

    def test_sidebar_highlights_billing(self):
        html = self._html(self._make())
        self.assertEqual(html.count(ACTIVE_NAV), 1)
        self.assertIn(f'<a href="{reverse("admin_receipt_list")}" {ACTIVE_NAV}', html)

    # ─── Header ───────────────────────────────────────────────────────────────

    def test_header_with_a_visit_and_a_pet(self):
        receipt = self._make(visit=self.visit)
        header = self._header(self._html(receipt))
        self.assertIn(
            f'<a href="{reverse("admin_receipt_list")}" class="mb-2 inline-flex',
            header,
        )
        text = page_text(header)
        self.assertIn("Back to receipts", text)
        self.assertIn(f"Receipt {receipt.receipt_number} Unpaid", text)
        self.assertIn("Billed to Ana Cruz · for Bantay", text)
        self.assertIn("From the visit Today at 9:00 AM", text)
        owner_url = reverse("admin_owner_detail", args=[self.owner.pk])
        pet_url = reverse("admin_pet_detail", args=[self.pet.pk])
        visit_url = reverse("admin_appointment_detail", args=[self.visit.pk])
        self.assertIn(f'<a href="{owner_url}" class="{LINK}">Ana Cruz</a>', header)
        self.assertIn(f'<a href="{pet_url}" class="{LINK}">Bantay</a>', header)
        self.assertRegex(
            header, rf'<a href="{visit_url}" class="[^"]*">Today at 9:00 AM</a>'
        )
        self.assertNotIn("Not linked to a visit", text)
        self.assertNotIn("not for a specific pet", text)

    def test_header_without_a_visit_or_a_pet(self):
        receipt = self._make(pet=False)
        header = self._header(self._html(receipt))
        text = page_text(header)
        self.assertIn("Billed to Ana Cruz · not for a specific pet", text)
        self.assertIn("Not linked to a visit", text)
        self.assertNotIn("From the visit", text)
        self.assertNotIn(reverse("admin_pet_detail", args=[self.pet.pk]), header)

    def test_header_dates_follow_the_views_today(self):
        """A visit two days ago reads "Thu, Nov 5", not "Today"."""
        old = Appointment.objects.create(
            owner=self.owner,
            pet=self.pet,
            date=TODAY - datetime.timedelta(days=2),
            time=datetime.time(14, 30),
            status=Appointment.COMPLETED,
        )
        header = self._header(self._html(self._make(visit=old)))
        self.assertIn("From the visit Thu, Nov 5 at 2:30 PM", page_text(header))

    def test_badge_and_tile_per_status(self):
        cases = [
            (BillingReceipt.PENDING, "Unpaid", "bg-yellow-100 text-yellow-800"),
            (BillingReceipt.PAID, "Paid", "bg-green-100 text-green-800"),
            (BillingReceipt.CANCELLED, "Cancelled", "bg-red-100 text-red-800"),
        ]
        violet = "bg-violet-50 text-violet-600 ring-violet-200"
        gray = "bg-gray-100 text-gray-600 ring-gray-300"
        for status, label, classes in cases:
            with self.subTest(status=status):
                html = self._html(self._make(status))
                self.assertIn(f'<span class="badge {classes}">{label}</span>', html)
                tile = gray if status == BillingReceipt.CANCELLED else violet
                other = violet if tile == gray else gray
                self.assertIn(tile, html)
                self.assertNotIn(other, html)

    # ─── Banner ───────────────────────────────────────────────────────────────

    def test_banner_per_state(self):
        cases = [
            (
                self._make(),
                "Not paid yet. Check the items, then press Mark as paid when "
                "Ana Cruz pays in cash.",
                "border-yellow-300 bg-yellow-50",
            ),
            (
                self._make(items=False),
                "Not paid yet. Add what Ana Cruz is paying for: services, "
                "medicines or supplies.",
                "border-yellow-300 bg-yellow-50",
            ),
            (
                self._make(BillingReceipt.PAID),
                "Paid in cash today, Saturday, November 7. This receipt is "
                "locked and can't be changed.",
                "border-green-300 bg-green-50",
            ),
            (
                self._make(BillingReceipt.CANCELLED),
                "This receipt was cancelled. It is locked and can't be changed. "
                "Ana Cruz doesn't see it.",
                "border-gray-300 bg-gray-100",
            ),
        ]
        for receipt, wording, classes in cases:
            with self.subTest(wording=wording[:25]):
                html = self._html(receipt)
                self.assertEqual(self._banner(html), wording)
                self.assertIn(
                    f'<div class="mb-6 flex items-start gap-3 rounded-xl border {classes}',
                    html,
                )

    def test_paid_banner_names_another_day(self):
        receipt = self._make(BillingReceipt.PAID)
        BillingReceipt.objects.filter(pk=receipt.pk).update(
            payment_date=datetime.date(2026, 11, 5)
        )
        self.assertIn(
            "Paid in cash Thursday, November 5.", self._banner(self._html(receipt))
        )

    # ─── Items ────────────────────────────────────────────────────────────────

    def test_item_rows_for_an_unpaid_receipt(self):
        receipt = self._make()
        html = self._html(receipt)
        card = section(html, "items-title")
        self.assertIn("Items (2)", page_text(card))
        # The column names row, hidden from screen readers, with the Remove column
        head = re.search(
            r'(?s)<div class="hidden [^"]*" aria-hidden="true">.*?</div>', card
        )
        self.assertEqual(
            page_text(head.group()).split(), ["Item", "Qty", "Unit", "price", "Amount"]
        )
        self.assertIn(
            "md:grid-cols-[minmax(0,1fr)_4rem_8rem_8rem_7.5rem]", head.group()
        )
        rows = self._rows(card)
        self.assertEqual(len(rows), 2)
        consult, amox = (page_text(row) for row in rows)
        self.assertIn("Consultation", consult)
        self.assertIn("From the price list", consult)
        self.assertIn("₱300.00", consult)
        self.assertIn("1 × ₱300.00", consult)
        self.assertIn("Amoxicillin 250 mg", amox)
        self.assertNotIn("From the price list", amox)
        self.assertIn("10 × ₱15.00", amox)
        self.assertIn("₱150.00", amox)
        self.assertEqual(card.count("From the price list"), 1)
        # Remove: one per item, red outline, the item named for screen readers
        for item, row in zip(receipt.billing_items.order_by("pk"), rows):
            with self.subTest(item=item.description):
                self.assertIn(f'data-dialog-open="remove-item-{item.pk}"', row)
                self.assertIn(
                    f'Remove<span class="sr-only"> {item.description}</span>', row
                )
                self.assertIn("border-red-300", row)
        self.assertIn("Add item", button_labels(card))

    def test_locked_receipts_have_no_remove_and_no_add_item(self):
        for status in LOCKED_STATUSES:
            with self.subTest(status=status):
                card = section(self._html(self._make(status)), "items-title")
                self.assertEqual(len(self._rows(card)), 2)
                self.assertEqual(button_labels(card), [])
                self.assertNotIn("Remove", card)
                self.assertIn("md:grid-cols-[minmax(0,1fr)_4rem_8rem_8rem]", card)
                self.assertNotIn("_7.5rem]", card)

    def test_empty_receipt_shows_the_dashed_box(self):
        html = self._html(self._make(items=False))
        card = section(html, "items-title")
        text = page_text(card)
        self.assertIn("Items (0)", text)
        self.assertIn("No items yet", text)
        self.assertIn(
            "Add each service, medicine or supply. The total adds up as you go.", text
        )
        self.assertIn("border-2 border-dashed border-gray-300", card)
        self.assertEqual(button_labels(card), ["Add item", "Add item"])
        self.assertNotIn("Subtotal", card)
        self.assertNotIn("Total", text)
        self.assertEqual(self._rows(card), [])

    def test_an_empty_locked_receipt_has_no_add_button(self):
        card = section(
            self._html(self._make(BillingReceipt.CANCELLED, items=False)), "items-title"
        )
        self.assertIn("No items", page_text(card))
        self.assertNotIn("No items yet", card)
        self.assertEqual(button_labels(card), [])

    # ─── Totals ───────────────────────────────────────────────────────────────

    def test_totals_without_a_discount(self):
        card = section(self._html(self._make()), "items-title")
        text = page_text(card)
        self.assertIn("Subtotal ₱450.00 No discount Total ₱450.00", text)
        self.assertNotIn("−", text)
        self.assertIn("border-t-2 border-gray-200 pt-3 text-xl font-bold", card)

    def test_totals_with_a_percentage_discount_and_its_note(self):
        receipt = self._make(
            discount_type=BillingReceipt.PERCENTAGE,
            discount_value=Decimal("10"),
            discount_note="Loyal client",
        )
        text = page_text(section(self._html(receipt), "items-title"))
        self.assertIn(
            "Subtotal ₱450.00 Discount · 10% · Loyal client −₱45.00 Total ₱405.00",
            text,
        )
        self.assertNotIn("No discount", text)

    def test_totals_with_an_empty_label_have_no_dot(self):
        """A peso discount with no note has an empty label: "Discount", not
        "Discount · "."""
        receipt = self._make(
            discount_type=BillingReceipt.FIXED_DISCOUNT, discount_value=Decimal("50")
        )
        card = section(self._html(receipt), "items-title")
        self.assertIn('<dt class="text-gray-700">Discount</dt><dd>−₱50.00</dd>', card)
        self.assertIn("Total ₱400.00", page_text(card))

    def test_totals_use_the_capped_discount(self):
        """Removing items can shrink a peso discount: the page shows the
        capped amount."""
        receipt = self._make(
            discount_type=BillingReceipt.FIXED_DISCOUNT, discount_value=Decimal("400")
        )
        BillingItem.objects.filter(
            receipt=receipt, description__startswith="Amox"
        ).delete()
        receipt.calculate_totals()
        text = page_text(section(self._html(receipt), "items-title"))
        self.assertIn("Subtotal ₱300.00 Discount −₱300.00 Total ₱0.00", text)

    # ─── Actions ──────────────────────────────────────────────────────────────

    def test_exactly_one_violet_button_per_state(self):
        cases = [
            (self._make(), ["Mark as paid"]),
            (self._make(items=False), ["Add item"]),
            (self._make(BillingReceipt.PAID), ["Print receipt"]),
            (self._make(BillingReceipt.CANCELLED), []),
        ]
        for receipt, expected in cases:
            with self.subTest(status=receipt.payment_status, expected=expected):
                self.assertEqual(primary_labels(self._html(receipt)), expected)

    def test_actions_for_an_unpaid_receipt_with_items(self):
        html = self._html(self._make())
        card = section(html, "actions-title")
        self.assertEqual(
            button_labels(card),
            ["Mark as paid", "Add item", "Add discount", "Print", "Cancel receipt"],
        )
        self.assertEqual(
            re.findall(r'data-dialog-open="([^"]+)"', card),
            ["mark-paid-dialog", "add-item-dialog", "discount-dialog", "cancel-dialog"],
        )
        self.assertEqual(card.count("data-print"), 1)
        self.assertIn("<hr", card)
        # Cancel receipt is the red outline, full width, under the line
        self.assertRegex(
            card,
            r'(?s)<hr[^>]*>\s*<button type="button" class="btn min-h-12 w-full '
            r'border border-red-300[^"]*"[^>]*>.*?Cancel receipt',
        )
        self.assertIn("card order-1", html)

    def test_actions_say_change_discount_with_a_discount(self):
        receipt = self._make(
            discount_type=BillingReceipt.PERCENTAGE, discount_value=Decimal("10")
        )
        labels = button_labels(section(self._html(receipt), "actions-title"))
        self.assertIn("Change discount", labels)
        self.assertNotIn("Add discount", labels)

    def test_actions_for_an_empty_unpaid_receipt(self):
        card = section(self._html(self._make(items=False)), "actions-title")
        self.assertEqual(button_labels(card), ["Add item", "Cancel receipt"])
        self.assertIn(
            "Mark as paid shows once the receipt has an item.", page_text(card)
        )
        self.assertNotIn("data-print", card)
        self.assertNotIn("Add discount", card)

    def test_actions_for_a_paid_receipt(self):
        card = section(self._html(self._make(BillingReceipt.PAID)), "actions-title")
        self.assertEqual(button_labels(card), ["Print receipt"])
        self.assertIn(
            "To save it as a PDF, pick “Save as PDF” in the print window.",
            page_text(card),
        )
        self.assertEqual(card.count("data-print"), 1)
        self.assertNotIn("Cancel receipt", card)

    def test_actions_for_a_cancelled_receipt(self):
        card = section(
            self._html(self._make(BillingReceipt.CANCELLED)), "actions-title"
        )
        self.assertEqual(button_labels(card), ["Print receipt"])
        self.assertIn("btn btn-outline", card)
        self.assertNotIn("btn-primary", card)
        self.assertIn("The printout says “Cancelled”.", page_text(card))
        self.assertEqual(card.count("data-print"), 1)

    def test_every_opener_has_its_dialog(self):
        for receipt in (self._make(), self._make(items=False)):
            with self.subTest(items=receipt.billing_items.count()):
                html = self._html(receipt)
                ids = dialogs_in(html)
                for opener in re.findall(r'data-dialog-open="([^"]+)"', html):
                    self.assertIn(opener, ids)

    # ─── Billed to and info cards ─────────────────────────────────────────────

    def test_billed_card_with_everything(self):
        card = section(self._html(self._make()), "billed-title")
        text = page_text(card)
        self.assertIn("Billed to Ana Cruz", text)
        self.assertIn("0917 555 0142", text)
        self.assertIn("owner@example.com", text)
        self.assertIn("Pet Bantay · Dog", text)
        owner_url = reverse("admin_owner_detail", args=[self.owner.pk])
        self.assertIn(f'<a href="{owner_url}" class="{LINK}">Ana Cruz</a>', card)
        self.assertNotIn("No phone number", text)
        self.assertNotIn("No email", text)

    def test_billed_card_without_phone_email_or_pet(self):
        PetOwner.objects.filter(pk=self.owner.pk).update(contact_number="")
        User.objects.filter(pk=self.owner.user.pk).update(email="")
        card = section(self._html(self._make(pet=False)), "billed-title")
        text = page_text(card)
        self.assertIn("No phone number", text)
        self.assertIn("No email", text)
        self.assertIn("Pet Not for a specific pet", text)

    def test_billed_card_names_the_owner_in_every_status(self):
        for status in (BillingReceipt.PENDING, *LOCKED_STATUSES):
            with self.subTest(status=status):
                card = section(self._html(self._make(status)), "billed-title")
                self.assertIn("Ana Cruz", card)

    def test_info_card_per_status(self):
        cases = [
            (
                BillingReceipt.PENDING,
                "Created Today, 12:30 AM Payment: cash "
                "Ana Cruz sees it as Unpaid in their Billing page.",
            ),
            (
                BillingReceipt.PAID,
                "Created Today, 12:30 AM Paid Today · Cash "
                "Ana Cruz sees it as Paid in their Billing page and can print it.",
            ),
            (
                BillingReceipt.CANCELLED,
                "Created Today, 12:30 AM Payment: cash Hidden from Ana Cruz.",
            ),
        ]
        for status, wording in cases:
            with self.subTest(status=status):
                html = self._html(self._make(status))
                card = re.search(
                    r'(?s)<section class="order-4.*?</section>', html
                ).group()
                self.assertEqual(page_text(card), wording)

    def test_created_and_paid_follow_the_views_today(self):
        receipt = self._make(BillingReceipt.PAID)
        BillingReceipt.objects.filter(pk=receipt.pk).update(
            payment_date=datetime.date(2026, 11, 5)
        )
        self.now = self.now + datetime.timedelta(days=1)  # Sun, Nov 8, 00:30
        card = re.search(
            r'(?s)<section class="order-4.*?</section>', self._html(receipt)
        ).group()
        self.assertIn("Created Yesterday, 12:30 AM", page_text(card))
        self.assertIn("Paid Thu, Nov 5 · Cash", page_text(card))

    # ─── Dialogs ──────────────────────────────────────────────────────────────

    def test_no_dialogs_on_paid_or_cancelled_receipts(self):
        for status in LOCKED_STATUSES:
            with self.subTest(status=status):
                html = self._html(self._make(status))
                self.assertNotIn("<dialog", html)
                self.assertNotIn("data-dialog-open", html)

    def test_dialogs_post_to_the_right_urls_with_csrf(self):
        receipt = self._make()
        first, second = receipt.billing_items.order_by("pk")
        dialogs = dialogs_in(self._html(receipt))
        expected = {
            "add-item-dialog": reverse("admin_billing_item_add", args=[receipt.pk]),
            "discount-dialog": reverse("admin_receipt_edit", args=[receipt.pk]),
            f"remove-item-{first.pk}": reverse(
                "admin_billing_item_delete", args=[first.pk]
            ),
            f"remove-item-{second.pk}": reverse(
                "admin_billing_item_delete", args=[second.pk]
            ),
            "mark-paid-dialog": reverse("admin_receipt_mark_paid", args=[receipt.pk]),
            "cancel-dialog": reverse("admin_receipt_mark_cancelled", args=[receipt.pk]),
        }
        self.assertEqual(set(dialogs), set(expected))
        for dialog_id, url in expected.items():
            with self.subTest(dialog=dialog_id):
                block = dialogs[dialog_id]
                self.assertIn(f'<form method="post" action="{url}"', block)
                self.assertIn('name="csrfmiddlewaretoken"', block)
                self.assertIn(
                    f'aria-labelledby="{dialog_id.split("-dialog")[0]}', block
                )
                self.assertEqual(block.count("autofocus"), 1)
                self.assertIn("m-auto", block)
                self.assertIn("p-0", block)

    def test_the_mark_paid_dialog_needs_items(self):
        dialogs = dialogs_in(self._html(self._make(items=False)))
        self.assertNotIn("mark-paid-dialog", dialogs)
        self.assertEqual(
            set(dialogs), {"add-item-dialog", "discount-dialog", "cancel-dialog"}
        )

    def test_the_mark_paid_dialog(self):
        block = dialogs_in(self._html(self._make()))["mark-paid-dialog"]
        text = page_text(block)
        receipt = BillingReceipt.objects.get()
        self.assertIn(f"Mark {receipt.receipt_number} as paid?", text)
        self.assertIn("Ana Cruz paid ₱450.00 in cash today", text)
        self.assertIn("text-3xl font-bold", block)
        self.assertIn(
            "After this the receipt is locked: no more items, discount changes or "
            "cancelling.",
            text,
        )
        self.assertIn(
            "Ana Cruz gets a notification in the app, and an email if their "
            "receipt emails are on, so they can view or print the receipt.",
            text,
        )
        self.assertNotIn("no email on file", text)
        self.assertIn("Go back", text)
        self.assertIn("Yes, mark as paid", text)
        self.assertIn("btn-primary", block)

    def test_the_mark_paid_dialog_without_an_email(self):
        User.objects.filter(pk=self.owner.user.pk).update(email="")
        text = page_text(dialogs_in(self._html(self._make()))["mark-paid-dialog"])
        self.assertIn(
            "Ana Cruz gets a notification in the app (no email on file).", text
        )
        self.assertNotIn("an email if", text)

    def test_the_cancel_dialog_with_a_visit(self):
        block = dialogs_in(self._html(self._make(visit=self.visit)))["cancel-dialog"]
        text = page_text(block)
        receipt = BillingReceipt.objects.get()
        self.assertIn(f"Cancel receipt {receipt.receipt_number}?", text)
        self.assertIn(
            "Use this for a receipt made by mistake, or when nothing will be charged.",
            text,
        )
        self.assertIn(
            "It can't be changed or paid after this, and it can't be undone.", text
        )
        self.assertIn(
            "Ana Cruz won't see it. It stays on the Cancelled tab for the clinic.", text
        )
        self.assertIn("This visit can't get another receipt.", text)
        self.assertIn("Yes, cancel receipt", text)
        self.assertIn("btn-danger", block)
        # "Keep it" is the go-back button and has the focus
        self.assertRegex(block, r"data-dialog-close autofocus>Keep it</button>")
        self.assertNotIn("Go back", text)

    def test_the_cancel_dialog_without_a_visit(self):
        text = page_text(dialogs_in(self._html(self._make()))["cancel-dialog"])
        self.assertNotIn("This visit can't get another receipt.", text)
        self.assertIn("Keep it", text)

    def test_other_dialogs_keep_their_go_back_label(self):
        """dialog_buttons.html still says "Go back" unless back_text is given."""
        for dialog_id, block in dialogs_in(self._html(self._make())).items():
            if dialog_id == "cancel-dialog":
                continue
            with self.subTest(dialog=dialog_id):
                self.assertIn("data-dialog-close autofocus>Go back</button>", block)

    def test_the_remove_dialog_says_the_new_total_with_a_percentage_discount(self):
        """10% off ₱450.00 is ₱405.00. Without Amoxicillin (₱150.00): 10%
        off ₱300.00 = ₱270.00. Without Consultation: 10% off ₱150.00 = ₱135.00."""
        receipt = self._make(
            discount_type=BillingReceipt.PERCENTAGE, discount_value=Decimal("10")
        )
        consult, amox = receipt.billing_items.order_by("pk")
        dialogs = dialogs_in(self._html(receipt))
        cases = [
            (
                amox,
                "Remove Amoxicillin 250 mg? It comes off this receipt. The total goes from ₱405.00 to ₱270.00.",
            ),
            (
                consult,
                "Remove Consultation? It comes off this receipt. The total goes from ₱405.00 to ₱135.00.",
            ),
        ]
        for item, wording in cases:
            with self.subTest(item=item.description):
                block = dialogs[f"remove-item-{item.pk}"]
                self.assertIn(wording, page_text(block))
                self.assertIn("Yes, remove it", page_text(block))
                self.assertIn("btn-danger", block)

    def test_the_remove_dialog_total_equals_the_total_after_the_real_delete(self):
        receipt = self._make(
            discount_type=BillingReceipt.PERCENTAGE, discount_value=Decimal("10")
        )
        amox = receipt.billing_items.order_by("pk").last()
        self.assertIn(
            "to ₱270.00.",
            page_text(dialogs_in(self._html(receipt))[f"remove-item-{amox.pk}"]),
        )
        self._post("admin_billing_item_delete", amox.pk)
        receipt.refresh_from_db()
        self.assertEqual(receipt.total_amount, Decimal("270.00"))

    # ─── Add item dialog ──────────────────────────────────────────────────────

    def _add_dialog(self, markup):
        return dialogs_in(markup)["add-item-dialog"]

    def test_the_add_item_select_follows_the_forms_choices(self):
        deworm = self._service("Deworming", "150.00")
        grooming = self._range_service("Grooming", "300.00", "800.00")
        self._service("Bath", status=Service.DRAFT)
        receipt = self._make()
        response = self.client.get(reverse("admin_receipt_detail", args=[receipt.pk]))
        block = self._add_dialog(response.content.decode())
        select = re.search(
            r'(?s)<select name="service_choice".*?</select>', block
        ).group()
        values = re.findall(r'<option value="([^"]*)"', select)
        choices = response.context["item_form"].fields["service_choice"].choices
        self.assertEqual(values, [value for value, _label in choices])
        self.assertEqual(values[0], "")
        self.assertEqual(values[-1], "other")
        self.assertEqual(
            values[1:-1], [str(s.pk) for s in (self.consult, deworm, grooming)]
        )
        self.assertIn('<option value="">Choose one</option>', select)
        self.assertIn('<option value="other">Other (type it)</option>', select)
        self.assertIn(
            f'<option value="{self.consult.pk}" data-name="Consultation" '
            'data-price="300.00" data-pricing="fixed" '
            'data-help="Price list: ₱300.00.">Consultation · ₱300.00</option>',
            select,
        )
        self.assertIn(
            f'<option value="{grooming.pk}" data-name="Grooming" data-price="" '
            'data-pricing="range" data-help="Usual price ₱300.00 to ₱800.00. '
            'You can bill outside it.">Grooming · ₱300.00 to ₱800.00</option>',
            select,
        )
        self.assertNotIn("selected", select)

    def test_the_add_item_dialog_wording(self):
        receipt = self._make()
        block = self._add_dialog(self._html(receipt))
        text = page_text(block)
        self.assertIn(f"Add an item to {receipt.receipt_number}", text)
        self.assertIn(
            "Pick it from the price list, or choose “Other (type it)” for a "
            "medicine or supply.",
            text,
        )
        self.assertIn("What is it?", text)
        self.assertIn("Description", text)
        self.assertIn("Printed on the receipt. You can change it.", text)
        self.assertIn("Quantity", text)
        self.assertIn("Unit price", text)
        self.assertIn("Add item", text)
        self.assertNotIn("Please fix", text)
        # The ₱ sits inside the price box; the help line is filled by the script
        self.assertRegex(
            block,
            r'(?s)<span[^>]*aria-hidden="true">₱</span>\s*<input type="text" '
            r'inputmode="decimal" name="unit_price"',
        )
        self.assertIn("data-item-help>", block)
        self.assertRegex(block, r'name="quantity"[^>]*value="1"')
        self.assertIn("max-w-2xl", block)
        self.assertIn("btn-primary", block)
        self.assertNotIn("data-open-on-load", block)

    def test_invalid_add_reopens_with_errors_and_the_typed_values(self):
        grooming = self._range_service("Grooming", "300.00", "800.00")
        receipt = self._make()
        response = self._post(
            "admin_billing_item_add",
            receipt.pk,
            {
                "service_choice": str(grooming.pk),
                "description": "Big dog groom",
                "quantity": "0",
                "unit_price": "",
            },
        )
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        dialogs = dialogs_in(html)
        block = dialogs["add-item-dialog"]
        # Only this dialog opens
        self.assertEqual(html.count("data-open-on-load"), 1)
        self.assertRegex(block, r'^<dialog id="add-item-dialog"[^>]*data-open-on-load')
        self.assertIn("Please fix the 2 items below.", page_text(block))
        self.assertRegex(block, error_line("The quantity must be from 1 to 999."))
        self.assertRegex(block, error_line("Type the price."))
        # Typed values kept; the service still picked; the price help re-rendered
        self.assertRegex(block, r'name="description" value="Big dog groom"')
        self.assertRegex(block, r'name="quantity" value="0"')
        self.assertRegex(block, r'name="unit_price" id="id_unit_price" value=""')
        self.assertRegex(block, rf'<option value="{grooming.pk}"[^>]*selected>')
        self.assertEqual(
            re.search(r"(?s)data-item-help>(.*?)</p>", block).group(1),
            "Usual price ₱300.00 to ₱800.00. You can bill outside it.",
        )
        self.assertEqual(self._messages(response), [])
        self.assertEqual(receipt.billing_items.count(), 2)

    def test_invalid_add_shows_the_pick_error_and_keeps_other(self):
        receipt = self._make()
        response = self._post(
            "admin_billing_item_add",
            receipt.pk,
            {
                "service_choice": "other",
                "description": "",
                "quantity": "2",
                "unit_price": "12.50",
            },
        )
        block = self._add_dialog(response.content.decode())
        self.assertIn("Please fix the 1 item below.", page_text(block))
        self.assertRegex(block, error_line("Type what it is."))
        self.assertRegex(
            block, r'<option value="other" selected>Other \(type it\)</option>'
        )
        self.assertRegex(block, r'name="unit_price" id="id_unit_price" value="12.50"')
        self.assertEqual(re.search(r"(?s)data-item-help>(.*?)</p>", block).group(1), "")
        response = self._post("admin_billing_item_add", receipt.pk, {"quantity": "1"})
        block = self._add_dialog(response.content.decode())
        self.assertRegex(block, error_line("Choose what it is, or Other (type it)."))

    # ─── Discount dialog ──────────────────────────────────────────────────────

    def test_the_discount_dialog(self):
        receipt = self._make()
        block = dialogs_in(self._html(receipt))["discount-dialog"]
        text = page_text(block)
        self.assertIn(f"Discount for {receipt.receipt_number}", text)
        self.assertIn("Subtotal now: ₱450.00.", text)
        self.assertIn("Choose “No discount” to remove it.", text)
        self.assertIn("(% or ₱)", text)
        self.assertIn("Note (optional)", text)
        self.assertIn("Printed next to the discount.", text)
        self.assertIn("Save discount", text)
        self.assertNotIn("Please fix", text)
        self.assertNotIn("data-open-on-load", block)
        for label in ("No discount", "Percent off (%)", "Peso amount off (₱)"):
            self.assertIn(label, text)
        self.assertIn('name="discount_value" id="id_discount_value" value=""', block)
        self.assertIn("data-discount-sign", block)

    def test_the_discount_dialog_shows_the_saved_discount(self):
        receipt = self._make(
            discount_type=BillingReceipt.PERCENTAGE,
            discount_value=Decimal("10"),
            discount_note="Loyal client",
        )
        block = dialogs_in(self._html(receipt))["discount-dialog"]
        self.assertIn(
            'name="discount_value" id="id_discount_value" value="10.00"', block
        )
        self.assertRegex(block, r'<option value="percentage" selected>')
        self.assertIn('value="Loyal client"', block)

    def test_invalid_discount_reopens_with_the_error_and_the_typed_value(self):
        receipt = self._make()
        response = self._post(
            "admin_receipt_edit",
            receipt.pk,
            {
                "discount_type": BillingReceipt.PERCENTAGE,
                "discount_value": "150",
                "discount_note": "Too much",
            },
        )
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        block = dialogs_in(html)["discount-dialog"]
        self.assertEqual(html.count("data-open-on-load"), 1)
        self.assertRegex(block, r'^<dialog id="discount-dialog"[^>]*data-open-on-load')
        self.assertIn("Please fix the 1 item below.", page_text(block))
        self.assertRegex(block, error_line("A discount can't be more than 100%."))
        self.assertIn('name="discount_value" id="id_discount_value" value="150"', block)
        self.assertRegex(block, r'<option value="percentage" selected>')
        self.assertIn('value="Too much"', block)
        # The page itself still shows the saved receipt
        self.assertIn("No discount", page_text(section(html, "items-title")))
        self.assertEqual(self._messages(response), [])

    def test_a_peso_discount_over_the_subtotal_shows_its_error(self):
        receipt = self._make()
        response = self._post(
            "admin_receipt_edit",
            receipt.pk,
            {"discount_type": BillingReceipt.FIXED_DISCOUNT, "discount_value": "500"},
        )
        block = dialogs_in(response.content.decode())["discount-dialog"]
        self.assertRegex(
            block, error_line("A discount can't be more than the subtotal (₱450.00).")
        )

    # ─── Peso filter ──────────────────────────────────────────────────────────

    def test_peso_filter(self):
        cases = [
            (0, "₱0.00"),
            (Decimal("0"), "₱0.00"),
            (Decimal("1250.5"), "₱1,250.50"),
            (1250.5, "₱1,250.50"),
            (Decimal("999999.99"), "₱999,999.99"),
            (Decimal("12"), "₱12.00"),
            (Decimal("-5"), "₱-5.00"),
            (None, ""),
            ("", ""),
            ("abc", ""),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(peso(value), expected)

    def test_the_list_and_the_page_use_the_same_money_format(self):
        receipt = self._make()
        BillingItem.objects.filter(receipt=receipt).update(
            unit_price=Decimal("1000.00")
        )
        for item in receipt.billing_items.all():
            item.save()
        receipt.calculate_totals()
        self.assertIn("₱11,000.00", self._html(receipt))
        listing = self.client.get(reverse("admin_receipt_list")).content.decode()
        self.assertIn("₱11,000.00", listing)
        self.assertNotIn(
            "floatformat",
            (BILLING_TEMPLATES / "receipt_list_parts" / "row.html").read_text(
                encoding="utf-8"
            ),
        )

    def test_owner_name_context_key(self):
        response = self.client.get(
            reverse("admin_receipt_detail", args=[self._make().pk])
        )
        self.assertEqual(response.context["owner_name"], "Ana Cruz")

    # ─── Print partial ────────────────────────────────────────────────────────

    def test_the_printout_for_a_paid_receipt(self):
        ClinicSettings.objects.create(
            clinic_name="Hapi Tutz Vet. Supplies",
            address="Bognuyan, Gasan, Marinduque",
            contact_number="0917 000 0000",
            email="hapitutzvet@gmail.com",
            opening_time=datetime.time(8, 0),
            closing_time=datetime.time(17, 0),
            same_day_cutoff_time=datetime.time(15, 0),
        )
        receipt = self._make(BillingReceipt.PAID)
        printed = self._printout(self._html(receipt))
        for expected in (
            "Hapi Tutz Vet. Supplies Bognuyan, Gasan, Marinduque "
            "0917 000 0000 · hapitutzvet@gmail.com",
            f"RECEIPT Receipt no. {receipt.receipt_number} Date: November 7, 2026",
            "PAID Cash · November 7, 2026",
            "BILLED TO Ana Cruz · 0917 555 0142 Pet: Bantay",
            "Qty Description Unit Amount",
            "1 Consultation ₱300.00 ₱300.00",
            "10 Amoxicillin 250 mg ₱15.00 ₱150.00",
            "Subtotal ₱450.00 TOTAL ₱450.00",
            "Thank you for trusting Hapi Tutz Vet. Supplies! "
            "We care for your pets like our own.",
        ):
            with self.subTest(expected=expected[:30]):
                self.assertIn(expected, printed)
        self.assertNotIn("Discount (", printed)

    def test_the_printout_stamp_per_status(self):
        cases = [
            (BillingReceipt.PENDING, "UNPAID", False),
            (BillingReceipt.PAID, "PAID", True),
            (BillingReceipt.CANCELLED, "CANCELLED", False),
        ]
        for status, stamp, has_cash in cases:
            with self.subTest(status=status):
                html = self._html(self._make(status))
                self.assertIn(f'<p class="receipt-print-stamp">{stamp}</p>', html)
                self.assertEqual("Cash · " in self._printout(html), has_cash)

    def test_the_printout_discount_line_only_with_a_discount(self):
        with_note = self._make(
            discount_type=BillingReceipt.PERCENTAGE,
            discount_value=Decimal("10"),
            discount_note="Loyal client",
        )
        printed = self._printout(self._html(with_note))
        self.assertIn(
            "Subtotal ₱450.00 Discount (10% · Loyal client) −₱45.00 TOTAL ₱405.00",
            printed,
        )
        plain = self._make(
            discount_type=BillingReceipt.FIXED_DISCOUNT, discount_value=Decimal("50")
        )
        # An empty label prints "Discount" alone, with no parentheses
        printed_plain = self._printout(self._html(plain))
        self.assertIn("Discount −₱50.00", printed_plain)
        self.assertNotIn("Discount (", printed_plain)
        self.assertNotIn("Discount", self._printout(self._html(self._make())))

    def test_the_printout_pet_line_only_with_a_pet(self):
        self.assertIn("Pet: Bantay", self._printout(self._html(self._make())))
        printed = self._printout(self._html(self._make(pet=False)))
        self.assertNotIn("Pet:", printed)
        self.assertIn("BILLED TO Ana Cruz · 0917 555 0142 Qty", printed)

    def test_the_printout_money_has_commas(self):
        receipt = self._make(items=False)
        self._item(receipt, "1250.00", description="Surgery")
        receipt.calculate_totals()
        printed = self._printout(self._html(receipt))
        self.assertIn("1 Surgery ₱1,250.00 ₱1,250.00", printed)
        self.assertIn("TOTAL ₱1,250.00", printed)

    def test_the_printout_is_black_on_white_and_self_contained(self):
        source = (SHARED_BILLING / "_receipt_print.html").read_text(encoding="utf-8")
        self.assertIn("<style>", source)
        self.assertIn("color: #000;", source)
        self.assertIn("background: #fff;", source)
        for outside in ("cdn.", "tailwind", "app.css", "gray-", "text-sm", "flex "):
            with self.subTest(outside=outside):
                self.assertNotIn(outside, source.split("</style>")[1])

    def test_the_printout_stays_after_the_page_in_extra_js_on_both_pages(self):
        receipt = self._make(BillingReceipt.PAID)
        admin_html = self._html(receipt)
        self.client.force_login(self.owner.user)
        owner_html = self.client.get(
            reverse("owner_receipt_detail", args=[receipt.pk])
        ).content.decode()
        for name, html in (("admin", admin_html), ("owner", owner_html)):
            with self.subTest(page=name):
                self.assertEqual(html.count('id="printable-receipt"'), 1)
                printout = html.index('id="printable-receipt"')
                self.assertGreater(printout, html.index("</main>"))
                self.assertLess(printout, html.index("</body>"))
        # On the admin page the script comes first, then the printout
        self.assertLess(
            admin_html.index("js/admin/receipt_detail.js"),
            admin_html.index('id="printable-receipt"'),
        )

    def test_the_owner_page_still_renders_and_prints_only_the_receipt(self):
        pending = self._make(
            discount_type=BillingReceipt.PERCENTAGE, discount_value=Decimal("10")
        )
        paid = self._make(BillingReceipt.PAID)
        self.client.force_login(self.owner.user)
        for receipt, stamp in ((pending, "UNPAID"), (paid, "PAID")):
            with self.subTest(status=receipt.payment_status):
                response = self.client.get(
                    reverse("owner_receipt_detail", args=[receipt.pk])
                )
                self.assertEqual(response.status_code, 200)
                html = response.content.decode()
                self.assertIn(f'<p class="receipt-print-stamp">{stamp}</p>', html)
                # The print rules hide everything but the receipt
                self.assertIn("body * {", html)
                self.assertIn(".receipt-print-wrapper,", html)
                self.assertIn("body > *:not(.receipt-print-wrapper)", html)
        printed = self._printout(
            self.client.get(
                reverse("owner_receipt_detail", args=[pending.pk])
            ).content.decode()
        )
        self.assertIn("Discount (10%) −₱45.00 TOTAL ₱405.00", printed)

    # ─── Queries and markup rules ─────────────────────────────────────────────

    def _queries_for(self, item_count):
        receipt = self._make(items=False, visit=None)
        for number in range(item_count):
            self._item(
                receipt, "10.00", description=f"Item {number}", service=self.consult
            )
        receipt.calculate_totals()
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(
                reverse("admin_receipt_detail", args=[receipt.pk])
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["items"]), item_count)
        return len(queries)

    def test_full_page_query_count_is_flat(self):
        none, ten = self._queries_for(0), self._queries_for(10)
        self.assertEqual(none, ten)
        self.assertEqual(ten, DETAIL_QUERIES)

    def test_every_link_is_on_one_line(self):
        for path in RECEIPT_PAGE_SOURCES:
            for link in re.findall(
                r"(?s)<a\b.*?</a>", path.read_text(encoding="utf-8")
            ):
                with self.subTest(file=path.name, link=link[:50]):
                    self.assertNotIn("\n", link)
        self.assertTrue(
            any("<a " in p.read_text(encoding="utf-8") for p in RECEIPT_PAGE_SOURCES)
        )

    def test_text_is_never_gray_400_or_lighter(self):
        for path in RECEIPT_PAGE_SOURCES:
            with self.subTest(file=path.name):
                self.assertIsNone(
                    re.search(
                        r"text-(gray|slate|zinc|neutral)-(50|100|200|300|400)\b",
                        path.read_text(encoding="utf-8"),
                    )
                )


# ── Slice 6b-4: the services list and the service form ───────────────────────

SERVICES_LIST_SOURCES = [
    BILLING_TEMPLATES / "services_list.html",
    *sorted((BILLING_TEMPLATES / "services_list_parts").glob("*.html")),
]
SERVICE_FORM_SOURCES = [
    BILLING_TEMPLATES / "service_form.html",
    *sorted((BILLING_TEMPLATES / "service_form_parts").glob("*.html")),
]
SERVICE_JS = Path(settings.BASE_DIR) / "static" / "js" / "admin" / "service_form.js"
OLD_SERVICE_FILES = (
    "css/admin/service_form.css",
    "css/admin/service_list.css",
    "js/admin/service_list.js",
)


def group_sections(markup):
    """{group key: its <section> markup} for the services page, in page order."""
    return dict(
        (m.group(1), m.group(0))
        for m in re.finditer(
            r'(?s)<section [^>]*aria-labelledby="group-(\w+)">.*?</section>', markup
        )
    )


@PLAIN_STATIC
class ServicesListPageTests(FrozenBillingTestBase):
    """admin/billing/services_list.html on layouts/admin.html (Slice 6b-4)."""

    def setUp(self):
        super().setUp()
        self.url = reverse("admin_service_list")

    def _fixture(self):
        """Three Offered (one range), one Clinic use only, one Hidden (draft),
        none No longer offered."""
        self.consult = self._service(
            "Consultation",
            "300.00",
            category="Check-ups",
            description="General check-up and advice.",
        )
        self.bath = self._service("Bath", "150.00")
        self.wound = self._range_service("Wound cleaning", "300.00", "800.00")
        self.wound.category = "Treatment"
        self.wound.save()
        self.home = self._service("Home visit fee", "500.00", Service.UNLISTED)
        self.dental = self._range_service(
            "Dental cleaning", "1500.00", "3000.00", Service.DRAFT
        )

    def _html(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def _row(self, html, service):
        rows = [
            row
            for row in re.findall(r'(?s)<li class="grid.*?</li>', html)
            if f'<span class="sr-only"> {escape(service.service_name)}</span>' in row
        ]
        self.assertEqual(len(rows), 1)
        return rows[0]

    # ─── Layout, header, tabs ─────────────────────────────────────────────────

    def test_new_layout_without_the_old_files(self):
        self._fixture()
        response = self.client.get(self.url)
        self.assertTemplateUsed(response, "layouts/admin.html")
        self.assertTemplateUsed(response, "admin/billing/services_list.html")
        self.assertTemplateNotUsed(response, "admin/billing/billing_parts/tabs.html")
        self.assertTemplateUsed(
            response, "admin/billing/services_list_parts/group.html"
        )
        self.assertTemplateUsed(response, "admin/billing/services_list_parts/row.html")
        for old in (
            *OLD_SERVICE_FILES,
            "layer(legacy)",
            "admin-page",
            "data-table",
            "filter-tab",
            "status-badge",
        ):
            with self.subTest(old=old):
                self.assertNotContains(response, old)
        self.assertContains(
            response, "<title>Services and prices — Hapi Vet Admin</title>"
        )
        self.assertContains(
            response,
            '<h1 class="text-2xl font-bold text-gray-900">Services and prices</h1>',
        )
        self.assertContains(response, "The price list for receipts and for booking.")
        source = (BILLING_TEMPLATES / "services_list.html").read_text(encoding="utf-8")
        for word in ("legacy_css", "extra_js", "messages"):
            with self.subTest(word=word):
                self.assertNotIn(word, source)

    def test_only_the_layouts_scripts_load(self):
        self._fixture()
        scripts = re.findall(r'<script src="([^"]+)"', self._html())
        self.assertEqual(
            [s.rsplit("/", 1)[-1] for s in scripts],
            ["htmx-2.0.11.min.js", "dialogs.js", "auto_submit.js"],
        )

    def test_one_primary_button_add_service(self):
        for fixture in (True, False):
            if fixture:
                self._fixture()
            with self.subTest(services=fixture):
                html = self._html()
                self.assertEqual(html.count("btn-primary"), 1)
                self.assertRegex(
                    html,
                    r'(?s)<a href="'
                    + re.escape(reverse("admin_service_create"))
                    + r'" class="btn btn-primary font-semibold">\s*<svg[^>]*'
                    r'aria-hidden="true"[^>]*>.*?</svg>\s*Add service</a>',
                )

    def test_sidebar_highlights_services_and_prices(self):
        html = self._html()
        self.assertEqual(html.count(ACTIVE_NAV), 1)
        self.assertIn(f'<a href="{self.url}" {ACTIVE_NAV}', html)
        self.assertNotIn(
            f'<a href="{reverse("admin_receipt_list")}" {ACTIVE_NAV}', html
        )

    def test_no_billing_tabs(self):
        self._empty_receipt()
        html = self._html()
        self.assertNotIn('aria-label="Billing sections"', html)
        self.assertNotIn(">Unpaid<", html)

    def test_header_and_one_primary_button(self):
        html = self._html()
        self.assertIn(
            '<h1 class="text-2xl font-bold text-gray-900">Services and prices</h1>',
            html,
        )
        self.assertIn("The price list for receipts and for booking.", html)
        self.assertNotIn(">Billing</h1>", html)
        self.assertEqual(html.count("btn-primary"), 1)

    # ─── Groups ───────────────────────────────────────────────────────────────

    def test_heading_and_the_groups_in_order(self):
        self._fixture()
        html = self._html()
        self.assertIn(
            '<h2 class="text-xl font-semibold text-gray-900">All services (5)</h2>',
            html,
        )
        self.assertIn("Grouped by who sees them · A to Z", html)
        self.assertEqual(
            list(group_sections(html)), ["active", "unlisted", "draft", "closed"]
        )

    def test_each_group_has_its_pill_count_and_text(self):
        cases = {
            "active": (
                "bg-green-100 px-2.5 py-1 text-sm font-medium text-green-800",
                "Offered",
                "3 services",
                "Pet owners see these when they book. You can put them on receipts.",
            ),
            "unlisted": (
                "bg-blue-100 px-2.5 py-1 text-sm font-medium text-blue-800",
                "Clinic use only",
                "1 service",
                "Hidden from pet owners. You can put them on receipts and pick them "
                "for walk-ins.",
            ),
            "draft": (
                "bg-gray-100 px-2.5 py-1 text-sm font-medium text-gray-800",
                "Hidden (draft)",
                "1 service",
                "Not shown anywhere yet. Finish it, then choose Offered or Clinic "
                "use only.",
            ),
            "closed": (
                "bg-red-100 px-2.5 py-1 text-sm font-medium text-red-800",
                "No longer offered",
                "0 services",
                "Not shown anywhere. Old visits and receipts keep their names and "
                "prices.",
            ),
        }
        self._fixture()
        sections = group_sections(self._html())
        for key, (classes, title, count, text) in cases.items():
            with self.subTest(group=key):
                section = sections[key]
                self.assertRegex(
                    section,
                    rf'(?s){re.escape(classes)}">\s*<svg[^>]*aria-hidden="true"'
                    rf"[^>]*>.*?</svg>\s*{re.escape(title)}</span>",
                )
                self.assertIn(count, page_text(section))
                self.assertIn(text, page_text(section))

    def test_an_empty_group_says_none(self):
        self._fixture()
        sections = group_sections(self._html())
        self.assertIn("None.", sections["closed"])
        for key in ("active", "unlisted", "draft"):
            with self.subTest(group=key):
                self.assertNotIn("None.", sections[key])

    def test_rows_sit_in_their_group_a_to_z(self):
        self._service("zebra care")
        self._service("Alpha")
        self._service("beta")
        self._service("Hidden thing", status=Service.DRAFT)
        sections = group_sections(self._html())
        self.assertEqual(
            re.findall(r'<span class="sr-only"> (.*?)</span>', sections["active"]),
            ["Alpha", "beta", "zebra care"],
        )
        self.assertEqual(
            re.findall(r'<span class="sr-only"> (.*?)</span>', sections["draft"]),
            ["Hidden thing"],
        )

    # ─── Rows ─────────────────────────────────────────────────────────────────

    def test_a_row_with_everything(self):
        self._fixture()
        row = self._row(self._html(), self.consult)
        self.assertIn(
            'text-lg font-semibold text-gray-900">Consultation<span class="rounded-full '
            'bg-gray-100 px-2.5 py-0.5 text-sm font-medium text-gray-800">Check-ups'
            "</span></p>",
            row,
        )
        self.assertIn(
            '<p class="hidden truncate text-base text-gray-700 md:block">'
            "General check-up and advice.</p>",
            row,
        )
        self.assertIn('text-lg font-semibold text-gray-900">₱300.00</p>', row)
        self.assertIn("One price</p>", row)
        self.assertNotIn("Price range", row)
        self.assertRegex(
            row,
            r'(?s)<a href="'
            + re.escape(reverse("admin_service_edit", args=[self.consult.pk]))
            + r'" class="btn btn-outline col-span-2 w-full font-semibold md:col-span-1 '
            r'md:w-auto">\s*<svg[^>]*aria-hidden="true"[^>]*>.*?</svg>\s*Edit'
            r'<span class="sr-only"> Consultation</span></a>',
        )

    def test_a_row_without_category_or_description(self):
        self._fixture()
        row = self._row(self._html(), self.bath)
        self.assertNotIn("rounded-full", row)
        self.assertIn(
            '<p class="hidden truncate text-base text-gray-600 md:block">'
            "No description</p>",
            row,
        )
        self.assertIn("₱150.00", row)

    def test_a_range_row(self):
        self._fixture()
        row = self._row(self._html(), self.wound)
        self.assertIn("₱300.00 to ₱800.00</p>", row)
        self.assertIn("Price range</p>", row)
        self.assertNotIn("One price", row)
        self.assertIn("Treatment</span>", row)
        # Money has commas
        self.assertIn("₱1,500.00 to ₱3,000.00", self._html())

    def test_a_range_missing_a_price_shows_its_base_price(self):
        broken = self._service(
            "Broken", "120.00", pricing_type=Service.RANGE, min_price=Decimal("1.00")
        )
        row = self._row(self._html(), broken)
        self.assertIn("₱120.00</p>", row)
        self.assertIn("One price", row)

    def test_the_phone_layout_in_the_same_row(self):
        self._fixture()
        row = self._row(self._html(), self.consult)
        # Name and price share the first line, Edit is full width below md
        self.assertIn("grid-cols-[minmax(0,1fr)_auto] items-start gap-x-3 gap-y-2", row)
        self.assertIn("col-span-2 w-full", row)
        # The description and the price kind only show from md
        self.assertEqual(row.count("hidden "), 2)

    def test_names_are_escaped(self):
        self._service("<b>Bold</b> & co", category="<i>x</i>")
        html = self._html()
        self.assertIn("&lt;b&gt;Bold&lt;/b&gt; &amp; co", html)
        self.assertIn("&lt;i&gt;x&lt;/i&gt;", html)
        self.assertNotIn("<b>Bold</b>", html)

    # ─── No services, queries, markup rules ───────────────────────────────────

    def test_no_services_at_all(self):
        html = self._html()
        text = page_text(html)
        self.assertIn("No services yet Add the first one with Add service.", text)
        self.assertNotIn("All services", text)
        self.assertNotIn("None.", text)
        self.assertEqual(group_sections(html), {})
        # Was assertIn: the Billing tabs are not on this page any more
        self.assertNotIn('aria-label="Billing sections"', html)

    def _queries(self, count):
        Service.objects.all().delete()
        statuses = [Service.ACTIVE, Service.UNLISTED, Service.DRAFT, Service.CLOSED]
        for number in range(count):
            if number % 2:
                self._range_service(
                    f"Service {number}", "100.00", "900.00", statuses[number % 4]
                )
            else:
                self._service(
                    f"Service {number}",
                    status=statuses[number % 4],
                    category="Care",
                    description="Something",
                )
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["total_count"], count)
        return len(queries)

    def test_query_count_is_flat(self):
        three, thirty = self._queries(3), self._queries(30)
        self.assertEqual(three, thirty)
        self.assertEqual(thirty, SERVICES_LIST_QUERIES)

    def test_every_link_is_on_one_line(self):
        for path in SERVICES_LIST_SOURCES:
            for link in re.findall(
                r"(?s)<a\b.*?</a>", path.read_text(encoding="utf-8")
            ):
                with self.subTest(file=path.name, link=link[:50]):
                    self.assertNotIn("\n", link)
        self.assertTrue(
            any("<a " in p.read_text(encoding="utf-8") for p in SERVICES_LIST_SOURCES)
        )

    def test_text_is_never_gray_400_or_lighter(self):
        for path in [*SERVICES_LIST_SOURCES, *SERVICE_FORM_SOURCES]:
            with self.subTest(file=path.name):
                self.assertIsNone(
                    re.search(
                        r"text-(gray|slate|zinc|neutral)-(50|100|200|300|400)\b",
                        path.read_text(encoding="utf-8"),
                    )
                )

    def test_no_template_loads_the_legacy_service_files(self):
        for path in BILLING_TEMPLATES.parents[1].rglob("*.html"):
            source = path.read_text(encoding="utf-8")
            for old in OLD_SERVICE_FILES:
                with self.subTest(file=path.name, old=old):
                    self.assertNotIn(old, source)

    def test_no_billing_template_uses_legacy_css(self):
        for path in BILLING_TEMPLATES.rglob("*.html"):
            source = path.read_text(encoding="utf-8")
            for old in ("legacy_css", "layer(legacy)"):
                with self.subTest(file=path.name, old=old):
                    self.assertNotIn(old, source)


@PLAIN_STATIC
class ServiceFormPageTests(FrozenBillingTestBase):
    """admin/billing/service_form.html (add and edit) on layouts/admin.html
    (Slice 6b-4)."""

    def setUp(self):
        super().setUp()
        self.add_url = reverse("admin_service_create")
        self.consult = self._service(
            "Consultation",
            "300.00",
            status=Service.ACTIVE,
            category="Check-ups",
            description="General check-up and advice.",
        )
        self.edit_url = reverse("admin_service_edit", args=[self.consult.pk])
        self.range = self._range_service("Wound cleaning", "300.00", "800.00")
        self.range_url = reverse("admin_service_edit", args=[self.range.pk])
        self.closed = self._service("Old thing", "50.00", status=Service.CLOSED)
        self.closed_url = reverse("admin_service_edit", args=[self.closed.pk])

    # ─── Helpers ──────────────────────────────────────────────────────────────

    def _html(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def _data(self, **extra):
        data = {
            "service_name": "Deworming",
            "category": "",
            "description": "",
            "pricing_type": Service.FIXED,
            "base_price": "150",
            "min_price": "",
            "max_price": "",
            "status": Service.ACTIVE,
        }
        data.update(extra)
        return data

    def _bad(self, url, **extra):
        """The page after a POST that failed."""
        response = self.client.post(url, self._data(**extra))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def _checked(self, html, name):
        """The values of the checked radios called name."""
        return [
            m.group(2)
            for m in re.finditer(
                rf'<input type="radio" name="{name}" id="(id_\w+)" '
                r'value="([^"]+)" class="[^"]*"( checked)?>',
                html,
            )
            if m.group(3)
        ]

    def _radios(self, html, name):
        return re.findall(rf'<input type="radio" name="{name}" id="(id_\w+)"', html)

    def _after(self, html, field_id):
        """The page from this field's input to the next label (its errors
        sit in between)."""
        start = html.index(f'id="{field_id}"')
        end = html.find("<label for=", start)
        return html[start : end if end != -1 else len(html)]

    # ─── Layout, header, buttons ──────────────────────────────────────────────

    def test_layout_script_and_no_old_files(self):
        for url in (self.add_url, self.edit_url):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertTemplateUsed(response, "layouts/admin.html")
                self.assertTemplateUsed(response, "admin/billing/service_form.html")
                for part in (
                    "header",
                    "service_card",
                    "price_card",
                    "price_field",
                    "radio_card",
                    "status_card",
                    "buttons",
                ):
                    self.assertTemplateUsed(
                        response, f"admin/billing/service_form_parts/{part}.html"
                    )
                for old in (
                    *OLD_SERVICE_FILES,
                    "layer(legacy)",
                    "admin-page",
                    "billing-form",
                    "form-group",
                ):
                    self.assertNotContains(response, old)
                self.assertContains(response, "js/admin/service_form.js")
                self.assertContains(
                    response,
                    '<form method="post" class="space-y-6" novalidate data-service-form>',
                )
                self.assertContains(response, '<div class="mx-auto max-w-3xl">')
        source = (BILLING_TEMPLATES / "service_form.html").read_text(encoding="utf-8")
        self.assertNotIn("legacy_css", source)

    def test_one_primary_button_with_and_without_errors(self):
        pages = [
            self._html(self.add_url),
            self._html(self.edit_url),
            self._bad(self.add_url, service_name=""),
            self._bad(self.edit_url, base_price=""),
        ]
        for html in pages:
            self.assertEqual(html.count("btn-primary"), 1)

    def test_add_header_subtitle_and_buttons(self):
        html = self._html(self.add_url)
        self.assertIn("<title>Add service — Hapi Vet Admin</title>", html)
        self.assertIn(
            '<h1 class="text-2xl font-bold text-gray-900">Add service</h1>', html
        )
        self.assertIn("A service on the price list. You can change it later.", html)
        self.assertNotIn("Receipts already made keep their prices.", html)
        self.assertRegex(
            html,
            r'(?s)<a href="'
            + re.escape(reverse("admin_service_list"))
            + r'" class="mb-2 inline-flex[^"]*">\s*<svg[^>]*aria-hidden="true"[^>]*>'
            r".*?</svg>\s*Back to services</a>",
        )
        self.assertIn(
            f'<a href="{reverse("admin_service_list")}" class="btn btn-outline '
            'min-h-12 w-full font-semibold sm:w-auto">Cancel</a>',
            html,
        )
        self.assertRegex(
            html,
            r'(?s)<button type="submit" class="btn btn-primary min-h-12 w-full '
            r'font-semibold sm:w-auto">\s*<svg[^>]*aria-hidden="true"[^>]*>.*?</svg>'
            r"\s*Add service</button>",
        )
        self.assertNotIn("Save changes", html)

    def test_edit_header_subtitle_and_buttons(self):
        html = self._html(self.edit_url)
        self.assertIn(
            "<title>Edit service · Consultation — Hapi Vet Admin</title>", html
        )
        self.assertIn(
            '<h1 class="text-2xl font-bold text-gray-900">Edit service · Consultation</h1>',
            html,
        )
        self.assertIn(
            "Changes apply to new receipts and bookings. Receipts already made keep "
            "their prices.",
            html,
        )
        self.assertNotIn("A service on the price list.", html)
        self.assertRegex(
            html,
            r'(?s)<button type="submit" class="btn btn-primary min-h-12 w-full '
            r'font-semibold sm:w-auto">\s*<svg[^>]*aria-hidden="true"[^>]*>.*?</svg>'
            r"\s*Save changes</button>",
        )
        self.assertNotIn(">Add service</", html)
        self.assertIn(">Cancel</a>", html)

    def test_a_failed_save_does_not_change_the_edit_title_or_note(self):
        html = self._bad(
            self.closed_url,
            service_name="Renamed thing",
            base_price="",
            status="closed",
        )
        self.assertIn("Edit service · Old thing</h1>", html)
        self.assertIn("<title>Edit service · Old thing — Hapi Vet Admin</title>", html)
        self.assertNotIn("Edit service · Renamed thing", html)
        self.assertIn("Upcoming visits booked with Old thing keep it.", html)
        # The typed name is still in the box
        self.assertRegex(html, r'name="service_name"[^>]*value="Renamed thing"')
        self.closed.refresh_from_db()
        self.assertEqual(self.closed.service_name, "Old thing")

    # ─── The service card ─────────────────────────────────────────────────────

    def test_the_service_card_fields(self):
        html = self._html(self.add_url)
        self.assertRegex(
            html,
            r'(?s)<h2 id="service-title"[^>]*>\s*<svg[^>]*aria-hidden="true"[^>]*>'
            r".*?</svg>\s*The service</h2>",
        )
        self.assertRegex(
            html,
            r'<label for="id_service_name"[^>]*>\s*Name <span class="text-red-700" '
            r'aria-hidden="true">\*</span>\s*</label>',
        )
        self.assertRegex(
            html,
            r'<label for="id_category"[^>]*>\s*Category <span class="font-normal '
            r'text-gray-600">\(optional\)</span>\s*</label>',
        )
        self.assertRegex(
            html,
            r'<label for="id_description"[^>]*>\s*Description <span class="font-normal '
            r'text-gray-600">\(optional\)</span>\s*</label>',
        )
        for text in (
            "Groups similar services. Type any word.",
            "Pet owners see this when they book.",
        ):
            self.assertIn(f'<p class="text-sm text-gray-600">{text}</p>', html)
        self.assertIn('placeholder="For example: Consultation"', html)
        self.assertIn('placeholder="What it includes, in a sentence"', html)

    def test_edit_shows_the_saved_values(self):
        html = self._html(self.edit_url)
        self.assertRegex(html, r'name="service_name"[^>]*value="Consultation"')
        self.assertRegex(html, r'name="category"[^>]*value="Check-ups"')
        self.assertRegex(html, r">\s*General check-up and advice\.</textarea>")
        self.assertIn('name="base_price" id="id_base_price" value="300.00"', html)

    # ─── The price card ───────────────────────────────────────────────────────

    def test_a_new_services_price_box_starts_empty(self):
        html = self._html(self.add_url)
        self.assertIn('name="base_price" id="id_base_price" value=""', html)
        self.assertNotIn('id="id_base_price" value="0', html)
        self.assertEqual(ServiceForm()["base_price"].value(), None)
        # Edit keeps the saved price, even 0
        free = self._service("Free", "0.00")
        html = self._html(reverse("admin_service_edit", args=[free.pk]))
        self.assertIn('id="id_base_price" value="0.00"', html)

    def test_the_price_card_heading_legend_and_cards(self):
        html = self._html(self.add_url)
        self.assertRegex(
            html,
            r'(?s)<h2 id="price-title"[^>]*>\s*<svg[^>]*aria-hidden="true"[^>]*>'
            r".*?</svg>\s*Price</h2>",
        )
        self.assertIn(
            '<legend class="text-base font-semibold text-gray-900">How is it priced? '
            '<span class="text-red-700" aria-hidden="true">*</span></legend>',
            html,
        )
        self.assertIn('<div class="mt-3 grid gap-3 sm:grid-cols-2">', html)
        text = page_text(html)
        self.assertIn("One price The same price every time.", text)
        self.assertIn(
            "A price range For example ₱300.00 to ₱800.00. You set the real price on "
            "each receipt.",
            text,
        )
        self.assertEqual(
            self._radios(html, "pricing_type"),
            [
                "id_pricing_type_0",
                "id_pricing_type_1",
            ],
        )
        values = re.findall(
            r'name="pricing_type" id="id_pricing_type_\d" value="(\w+)"', html
        )
        self.assertEqual(values, ["fixed", "range"])

    def test_radio_cards_style_the_checked_card_and_the_focus_ring(self):
        html = self._html(self.add_url)
        cards = re.findall(
            r'<label for="id_(?:pricing_type|status)_\d" class="([^"]*)"', html
        )
        self.assertEqual(len(cards), 6)
        for classes in cards:
            for expected in (
                "border-2",
                "min-h-11",
                "has-[:checked]:border-violet-600",
                "has-[:checked]:bg-violet-50",
                "has-[:focus-visible]:ring-2",
                "has-[:focus-visible]:ring-violet-600",
            ):
                with self.subTest(expected=expected):
                    self.assertIn(expected, classes)
        # Every label points at an input that exists, and the radios are 44px-class big
        for target in re.findall(r'<label for="(id_\w+)"', html):
            with self.subTest(target=target):
                self.assertIn(f'id="{target}"', html)
        self.assertEqual(
            html.count('class="mt-0.5 size-6 shrink-0 accent-violet-600"'), 6
        )

    def test_pricing_card_checked_from_initial_saved_and_posted_values(self):
        self.assertEqual(
            self._checked(self._html(self.add_url), "pricing_type"), ["fixed"]
        )
        self.assertEqual(
            self._checked(self._html(self.edit_url), "pricing_type"), ["fixed"]
        )
        self.assertEqual(
            self._checked(self._html(self.range_url), "pricing_type"), ["range"]
        )
        # Posted wins over saved, also when the save failed
        html = self._bad(
            self.edit_url, pricing_type="range", min_price="", max_price=""
        )
        self.assertEqual(self._checked(html, "pricing_type"), ["range"])
        html = self._bad(self.range_url, pricing_type="fixed", base_price="")
        self.assertEqual(self._checked(html, "pricing_type"), ["fixed"])

    def test_only_the_chosen_price_set_shows(self):
        fixed, ranged = "<div data-price-one>", "<div data-price-range>"
        hidden_fixed, hidden_range = (
            '<div data-price-one class="hidden">',
            '<div data-price-range class="hidden">',
        )
        for url, show_one in (
            (self.add_url, True),
            (self.edit_url, True),
            (self.range_url, False),
        ):
            with self.subTest(url=url):
                html = self._html(url)
                self.assertIn(fixed if show_one else hidden_fixed, html)
                self.assertIn(hidden_range if show_one else ranged, html)
                self.assertNotIn(hidden_fixed if show_one else fixed, html)
        # A failed save keeps the posted choice
        html = self._bad(self.add_url, pricing_type="range", min_price="", max_price="")
        self.assertIn(ranged, html)
        self.assertIn(hidden_fixed, html)

    def test_both_sets_show_without_javascript(self):
        html = self._html(self.add_url)
        self.assertIn(
            "<noscript><style>[data-price-one], [data-price-range] "
            "{ display: block; }</style></noscript>",
            html,
        )

    def test_the_price_sets_are_indented_under_the_cards(self):
        html = self._html(self.add_url)
        self.assertIn('<div class="mt-4 border-l-4 border-violet-200 pl-4">', html)
        # One price box is narrow; Lowest and Highest sit side by side from sm
        self.assertIn('form-field space-y-1.5 max-w-xs">', html)
        self.assertIn('<div class="grid gap-4 sm:grid-cols-2">', html)
        for label in ("Price", "Lowest price", "Highest price"):
            with self.subTest(label=label):
                self.assertRegex(
                    html,
                    rf'<label for="id_\w+_price" class="[^"]*">{label} '
                    r'<span class="text-red-700" aria-hidden="true">\*</span></label>',
                )

    def test_the_peso_sign_does_not_cover_the_typed_number(self):
        html = self._html(self.add_url)
        signs = html.count(
            '<span class="pointer-events-none absolute inset-y-0 left-3 flex '
            'items-center text-base text-gray-700" aria-hidden="true">₱</span>'
        )
        self.assertEqual(signs, 3)
        for name in ("base_price", "min_price", "max_price"):
            with self.subTest(name=name):
                self.assertRegex(
                    html,
                    rf'<input type="text" inputmode="decimal" name="{name}" '
                    rf'id="id_{name}" value="[^"]*" class="pl-8">',
                )

    def test_the_range_preview_text(self):
        empty = "Pet owners see the range when they book."
        self.assertIn(empty, page_text(self._html(self.add_url)))
        self.assertIn(
            "Pet owners see “₱300.00 to ₱800.00” when they book.",
            page_text(self._html(self.range_url)),
        )
        cases = [
            (dict(min_price="1,250", max_price="3000"), "“₱1,250.00 to ₱3,000.00”"),
            (dict(min_price="100", max_price=""), None),
            (dict(min_price="", max_price="900"), None),
            (dict(min_price="abc", max_price="900"), None),
            (dict(min_price="NaN", max_price="900"), None),
            (dict(min_price="Infinity", max_price="900"), None),
        ]
        for extra, expected in cases:
            with self.subTest(extra=extra):
                html = self._bad(self.add_url, pricing_type="range", **extra)
                text = page_text(html)
                if expected:
                    self.assertIn(f"Pet owners see {expected} when they book.", text)
                else:
                    self.assertIn(empty, text)
                    self.assertNotIn("Pet owners see “", text)

    # ─── The status card ──────────────────────────────────────────────────────

    def test_the_status_cards_in_order_with_their_lines(self):
        html = self._html(self.add_url)
        self.assertRegex(
            html,
            r'(?s)<h2 id="status-title"[^>]*>\s*<svg[^>]*aria-hidden="true"[^>]*>'
            r".*?</svg>\s*Who sees it</h2>",
        )
        self.assertIn('<legend class="sr-only">Who sees it</legend>', html)
        self.assertEqual(
            re.findall(r'name="status" id="id_status_\d" value="(\w+)"', html),
            ["active", "unlisted", "draft", "closed"],
        )
        text = page_text(html)
        for label, line in (
            (
                "Offered",
                "Pet owners see it when they book. You can put it on receipts.",
            ),
            (
                "Clinic use only",
                "Hidden from pet owners. You can put it on receipts and pick it for "
                "walk-ins.",
            ),
            ("Hidden (draft)", "Not shown anywhere yet."),
            (
                "No longer offered",
                "Not shown anywhere. Old visits and receipts keep it.",
            ),
        ):
            with self.subTest(label=label):
                self.assertIn(f"{label} {line}", text)

    def test_status_card_checked_from_initial_saved_and_posted_values(self):
        self.assertEqual(self._checked(self._html(self.add_url), "status"), ["draft"])
        self.assertEqual(self._checked(self._html(self.edit_url), "status"), ["active"])
        self.assertEqual(
            self._checked(self._html(self.closed_url), "status"), ["closed"]
        )
        html = self._bad(self.add_url, service_name="", status="unlisted")
        self.assertEqual(self._checked(html, "status"), ["unlisted"])

    def test_the_closed_note(self):
        note = (
            "Upcoming visits booked with Old thing keep it. Owners can't pick it for "
            "new bookings."
        )
        # Add: no note at all
        add = self._html(self.add_url)
        self.assertNotIn("data-closed-note", add)
        self.assertNotIn("Upcoming visits booked with", add)
        # Edit, saved closed: showing (flex), with the name and the icon
        shown = self._html(self.closed_url)
        self.assertRegex(
            shown,
            r'(?s)<p class="mt-3 items-start gap-2 rounded-lg bg-gray-50 px-3 py-2 '
            r'text-base text-gray-800 flex" data-closed-note>\s*<svg[^>]*'
            r'aria-hidden="true"[^>]*>.*?</svg>\s*<span>'
            + re.escape(note)
            + "</span></p>",
        )
        # Edit, saved another status: there, but hidden
        hidden = self._html(self.edit_url)
        self.assertIn('text-gray-800 hidden" data-closed-note>', hidden)
        self.assertNotIn('text-gray-800 flex" data-closed-note>', hidden)
        # A failed save follows what was posted
        posted_closed = self._bad(self.edit_url, base_price="", status="closed")
        self.assertIn('text-gray-800 flex" data-closed-note>', posted_closed)
        self.assertIn(
            "Upcoming visits booked with Consultation keep it.", posted_closed
        )
        posted_open = self._bad(self.closed_url, base_price="", status="active")
        self.assertIn('text-gray-800 hidden" data-closed-note>', posted_open)

    # ─── Errors ───────────────────────────────────────────────────────────────

    def test_every_error_sits_under_its_field_and_values_are_kept(self):
        cases = [
            (
                dict(service_name="  "),
                "id_service_name",
                "Type the service name.",
            ),
            (
                dict(service_name="consultation "),
                "id_service_name",
                "There is already a service called “Consultation”.",
            ),
            (dict(base_price=""), "id_base_price", "Type the price."),
            (dict(base_price="-1"), "id_base_price", "Prices can't be negative."),
            (
                dict(pricing_type="range", min_price="", max_price="800"),
                "id_min_price",
                "Type the lowest and the highest price.",
            ),
            (
                dict(pricing_type="range", min_price="300", max_price=""),
                "id_max_price",
                "Type the lowest and the highest price.",
            ),
            (
                dict(pricing_type="range", min_price="-5", max_price="800"),
                "id_min_price",
                "Prices can't be negative.",
            ),
            (
                dict(pricing_type="range", min_price="800", max_price="300"),
                "id_max_price",
                "The highest price must be more than the lowest.",
            ),
            (
                dict(pricing_type="range", min_price="300", max_price="300"),
                "id_max_price",
                "The highest price must be more than the lowest.",
            ),
        ]
        for extra, field_id, message in cases:
            with self.subTest(extra=extra, message=message):
                html = self._bad(
                    self.add_url,
                    category="Typed category",
                    description="Typed description",
                    **extra,
                )
                self.assertRegex(self._after(html, field_id), error_line(message))
                self.assertIn('<div class="form-field space-y-1.5 has-error', html)
                self.assertIn("Please fix the 1 item below.", html)
                self.assertNotIn("Please correct the errors below.", html)
                # Typed values stay
                self.assertRegex(html, r'name="category"[^>]*value="Typed category"')
                self.assertRegex(html, r">\s*Typed description</textarea>")
                for name in ("service_name", "base_price", "min_price", "max_price"):
                    typed = extra.get(name, self._data()[name])
                    if typed.strip():
                        self.assertRegex(
                            html,
                            rf'name="{name}"[^>]*value="{re.escape(typed)}"',
                        )
                self.assertEqual(
                    Service.objects.filter(category="Typed category").count(), 0
                )

    def test_two_errors_give_the_summary_two_items(self):
        html = self._bad(self.add_url, service_name="", base_price="")
        self.assertIn("Please fix the 2 items below.", html)
        self.assertIn(
            "Nothing was saved yet. Everything you typed is still here.", html
        )
        self.assertRegex(
            self._after(html, "id_service_name"), error_line("Type the service name.")
        )
        self.assertRegex(
            self._after(html, "id_base_price"), error_line("Type the price.")
        )
        self.assertNotIn("Please fix", self._html(self.add_url))

    def test_the_typed_name_is_escaped(self):
        html = self._bad(
            self.add_url, service_name='"><script>x</script>', base_price=""
        )
        self.assertNotIn("<script>x</script>", html)
        self.assertIn("&quot;&gt;&lt;script&gt;x&lt;/script&gt;", html)

    # ─── Saving ───────────────────────────────────────────────────────────────

    def test_add_saves_and_flashes(self):
        response = self.client.post(
            self.add_url,
            self._data(
                service_name=" Deworming ",
                category="Preventive care",
                description="Tablet or syrup.",
                status=Service.DRAFT,
            ),
        )
        self.assertRedirects(
            response, reverse("admin_service_list"), fetch_redirect_response=False
        )
        service = Service.objects.get(service_name="Deworming")
        self.assertEqual(
            (service.category, service.base_price, service.status),
            ("Preventive care", Decimal("150.00"), Service.DRAFT),
        )
        followed = self.client.get(reverse("admin_service_list"))
        self.assertContains(followed, "Deworming added.")

    def test_edit_saves_and_flashes(self):
        response = self.client.post(
            self.edit_url,
            self._data(
                service_name="Consultation",
                pricing_type=Service.RANGE,
                min_price="300",
                max_price="900",
                status=Service.UNLISTED,
            ),
        )
        self.assertRedirects(
            response, reverse("admin_service_list"), fetch_redirect_response=False
        )
        self.consult.refresh_from_db()
        self.assertEqual(
            (
                self.consult.pricing_type,
                self.consult.min_price,
                self.consult.max_price,
                self.consult.status,
            ),
            (Service.RANGE, Decimal("300.00"), Decimal("900.00"), Service.UNLISTED),
        )
        followed = self.client.get(reverse("admin_service_list"))
        self.assertContains(followed, "Consultation saved.")
        # Edit again: the range shows, with what owners will see
        html = self._html(self.edit_url)
        self.assertEqual(self._checked(html, "pricing_type"), ["range"])
        self.assertIn(
            "Pet owners see “₱300.00 to ₱900.00” when they book.", page_text(html)
        )

    def test_an_unknown_service_is_a_404(self):
        response = self.client.get(reverse("admin_service_edit", args=[999999]))
        self.assertEqual(response.status_code, 404)

    # ─── Sidebar, script, markup rules ────────────────────────────────────────

    def test_sidebar_highlights_services_and_prices(self):
        for url in (self.add_url, self.edit_url):
            with self.subTest(url=url):
                html = self._html(url)
                self.assertEqual(html.count(ACTIVE_NAV), 1)
                self.assertIn(
                    f'<a href="{reverse("admin_service_list")}" {ACTIVE_NAV}', html
                )

    def test_the_script_switches_the_sets_and_the_note(self):
        script = SERVICE_JS.read_text(encoding="utf-8")
        for word in (
            "[data-price-one]",
            "[data-price-range]",
            "[data-closed-note]",
            "pricing_type",
            "closed",
            "querySelectorAll",
        ):
            with self.subTest(word=word):
                self.assertIn(word, script)
        for word in ("fetch(", "XMLHttpRequest", "onclick", "{% url", "eval("):
            with self.subTest(word=word):
                self.assertNotIn(word, script)

    def test_every_link_is_on_one_line(self):
        for path in SERVICE_FORM_SOURCES:
            for link in re.findall(
                r"(?s)<a\b.*?</a>", path.read_text(encoding="utf-8")
            ):
                with self.subTest(file=path.name, link=link[:50]):
                    self.assertNotIn("\n", link)
        self.assertTrue(
            any("<a " in p.read_text(encoding="utf-8") for p in SERVICE_FORM_SOURCES)
        )

    def test_no_billing_template_has_an_inline_handler(self):
        for path in BILLING_TEMPLATES.rglob("*.html"):
            with self.subTest(file=path.name):
                self.assertIsNone(
                    re.search(r"<[^>]*\son[a-z]+\s*=", path.read_text(encoding="utf-8"))
                )

    def test_the_form_has_the_choice_cards_helpers(self):
        form = ServiceForm()
        self.assertEqual(
            [(c["value"], c["checked"]) for c in form.pricing_cards],
            [("fixed", True), ("range", False)],
        )
        self.assertEqual(
            [(c["value"], c["checked"]) for c in form.status_cards],
            [
                ("active", False),
                ("unlisted", False),
                ("draft", True),
                ("closed", False),
            ],
        )
        self.assertFalse(form.is_closed)
        self.assertEqual(form.pricing_shown, "fixed")
        # A bad pricing value shows One price
        bad = ServiceForm(self._data(pricing_type="nonsense"))
        self.assertEqual(bad.pricing_shown, "fixed")
        self.assertTrue(ServiceForm(instance=self.closed).is_closed)


class OwnerBillsContextTests(FrozenBillingTestBase):
    """Slice 8a-2a: owner_bills_context on Sat, Nov 7, 2026 (00:30 Manila,
    still Nov 6 in UTC). to_pay is never filtered; history is paid only."""

    def setUp(self):
        super().setUp()
        self.number = 0
        self.mingming = Pet.objects.create(
            owner=self.owner, name="Mingming", species="Cat"
        )

    def _bill(self, status, paid=None, billed=None, pet=None, item="Consultation"):
        """A receipt with one item (100.00) in the given status. paid is the
        payment date (paid receipts), billed the billing date."""
        self.number += 1
        receipt = BillingReceipt.objects.create(
            receipt_number=f"HV-2026-{self.number:04d}",
            owner=self.owner,
            pet=pet or self.pet,
            payment_status=status,
            payment_date=paid,
            billing_date=billed or paid or TODAY,
        )
        self._item(receipt, "100.00", description=item)
        receipt.calculate_totals()
        return receipt

    def _paid(self, paid, **fields):
        return self._bill(BillingReceipt.PAID, paid=paid, **fields)

    def _unpaid(self, billed=TODAY, **fields):
        return self._bill(BillingReceipt.PENDING, billed=billed, **fields)

    def _context(self, **params):
        return owner_bills_context(self.owner, params, TODAY)

    def _history(self, **params):
        return [r.pk for r in self._context(**params)["history"]]

    def _days(self, n):
        return TODAY + datetime.timedelta(days=n)

    # ─── To pay ───────────────────────────────────────────────────────────────

    def test_to_pay_is_every_unpaid_receipt_oldest_first_and_never_filtered(self):
        newer = self._unpaid(billed=self._days(-2))
        older = self._unpaid(billed=self._days(-20))
        newest = self._unpaid(billed=self._days(0), pet=self.mingming)
        self._paid(self._days(-1))
        for params in ({}, {"q": "nothing matches this", "period": "last_year"}):
            with self.subTest(params=params):
                context = self._context(**params)
                self.assertEqual(
                    [r.pk for r in context["to_pay"]],
                    [older.pk, newer.pk, newest.pk],
                )
                self.assertEqual(context["to_pay_count"], 3)
                self.assertEqual(context["to_pay_total"], Decimal("300.00"))

    def test_to_pay_is_empty_with_nothing_unpaid(self):
        self._paid(TODAY)
        context = self._context()
        self.assertEqual(context["to_pay"], [])
        self.assertEqual(context["to_pay_count"], 0)
        self.assertEqual(context["to_pay_total"], Decimal("0"))

    # ─── History ──────────────────────────────────────────────────────────────

    def test_history_is_paid_only_newest_paid_first(self):
        old = self._paid(self._days(-40))
        new = self._paid(self._days(-1))
        mid = self._paid(self._days(-10))
        self._unpaid()
        self._bill(BillingReceipt.CANCELLED, billed=TODAY)
        self.assertEqual(self._history(), [new.pk, mid.pk, old.pk])

    def test_a_cancelled_receipt_is_nowhere(self):
        cancelled = self._bill(BillingReceipt.CANCELLED, paid=TODAY)
        self._paid(TODAY)
        self._unpaid()
        context = self._context(q=cancelled.receipt_number)
        every_pk = (
            [r.pk for r in context["to_pay"]]
            + [r.pk for r in context["history"]]
            + [r.pk for r in context["receipts"]]
        )
        self.assertNotIn(cancelled.pk, every_pk)
        self.assertEqual(context["history"], [])

    def test_another_owners_receipts_are_never_listed(self):
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
        BillingReceipt.objects.create(
            receipt_number="HV-2026-9001", owner=other, payment_status="paid"
        )
        BillingReceipt.objects.create(receipt_number="HV-2026-9002", owner=other)
        context = self._context()
        self.assertEqual((context["to_pay"], context["history"]), ([], []))

    # ─── Search ───────────────────────────────────────────────────────────────

    def test_search_by_number_part_pet_and_item(self):
        by_number = self._paid(TODAY, item="Checkup")  # HV-2026-0001
        by_pet = self._paid(TODAY, pet=self.mingming, item="Checkup")
        by_item = self._paid(TODAY, item="Deworming tablets")
        cases = {
            "0001": [by_number],
            "hv-2026-0001": [by_number],
            "mingming": [by_pet],
            "MINGMING": [by_pet],
            "deworm": [by_item],
            "  deworming   ": [by_item],
            "nothing": [],
        }
        for q, expected in cases.items():
            with self.subTest(q=q):
                self.assertEqual(self._history(q=q), [r.pk for r in expected])

    def test_every_word_must_match(self):
        both = self._paid(TODAY, item="Vaccine shot")
        self._paid(TODAY, pet=self.mingming, item="Vaccine shot")
        self._paid(TODAY, item="Checkup")
        self.assertEqual(self._history(q="bantay vaccine"), [both.pk])
        self.assertEqual(self._history(q="bantay mingming"), [])

    def test_a_receipt_with_two_matching_items_is_listed_once(self):
        receipt = self._paid(TODAY, item="Vaccine A")
        self._item(receipt, "50.00", description="Vaccine B")
        self.assertEqual(self._history(q="vaccine"), [receipt.pk])
        self.assertEqual(self._context(q="vaccine")["page_obj"].paginator.count, 1)

    def test_search_never_narrows_to_pay(self):
        unpaid = self._unpaid(item="Vaccine")
        self._paid(TODAY, item="Checkup")
        context = self._context(q="checkup")
        self.assertEqual([r.pk for r in context["to_pay"]], [unpaid.pk])

    # ─── Period ───────────────────────────────────────────────────────────────

    def test_each_period_and_its_edges(self):
        # Today is Nov 7, 2026
        dates = {
            "nov_1": datetime.date(2026, 11, 1),
            "oct_31": datetime.date(2026, 10, 31),
            "aug_8": datetime.date(2026, 8, 8),
            "aug_7": datetime.date(2026, 8, 7),
            "jan_1": datetime.date(2026, 1, 1),
            "dec_31_2025": datetime.date(2025, 12, 31),
            "jan_1_2025": datetime.date(2025, 1, 1),
            "dec_31_2024": datetime.date(2024, 12, 31),
        }
        pks = {key: self._paid(day).pk for key, day in dates.items()}
        expected = {
            "all": set(dates),
            "month": {"nov_1"},
            # after Aug 7: the day three months back itself is out
            "3m": {"nov_1", "oct_31", "aug_8"},
            "year": {"nov_1", "oct_31", "aug_8", "aug_7", "jan_1"},
            "last_year": {"dec_31_2025", "jan_1_2025"},
        }
        for period, keys in expected.items():
            with self.subTest(period=period):
                self.assertEqual(
                    set(self._history(period=period)), {pks[key] for key in keys}
                )

    def test_a_missing_or_unknown_period_is_all_time(self):
        self._paid(datetime.date(2020, 1, 1))
        for params in ({}, {"period": ""}, {"period": "decade"}, {"period": "2025"}):
            with self.subTest(params=params):
                context = self._context(**params)
                self.assertEqual(context["period"], "all")
                self.assertEqual(len(context["history"]), 1)
                self.assertFalse(context["is_filtering"])

    def test_the_three_month_edge_is_cut_to_the_month_length(self):
        # May 31 minus 3 months is Feb 28
        today = datetime.date(2026, 5, 31)
        on_edge = self._paid(datetime.date(2026, 2, 28))
        after = self._paid(datetime.date(2026, 3, 1))
        context = owner_bills_context(self.owner, {"period": "3m"}, today)
        self.assertEqual([r.pk for r in context["history"]], [after.pk])
        self.assertNotIn(on_edge.pk, [r.pk for r in context["history"]])

    def test_a_paid_receipt_without_a_payment_date_uses_its_billing_date(self):
        receipt = self._bill(BillingReceipt.PAID, billed=TODAY)
        self.assertEqual(self._history(period="month"), [receipt.pk])

    def test_period_options_and_labels(self):
        context = self._context()
        self.assertEqual(
            context["period_options"],
            [
                ("all", "All time"),
                ("month", "This month"),
                ("3m", "Last 3 months"),
                ("year", "This year (2026)"),
                ("last_year", "Last year (2025)"),
            ],
        )

    # ─── Keys, paging ─────────────────────────────────────────────────────────

    def test_the_keys_and_is_filtering(self):
        context = self._context(q="  bantay ", period="month")
        for key in (
            "receipts",
            "to_pay",
            "to_pay_count",
            "to_pay_total",
            "history",
            "q",
            "period",
            "period_options",
            "is_filtering",
            "page_obj",
            "prev_page_query",
            "next_page_query",
            "today",
        ):
            with self.subTest(key=key):
                self.assertIn(key, context)
        self.assertEqual((context["q"], context["period"]), ("bantay", "month"))
        self.assertEqual(context["today"], TODAY)
        self.assertTrue(context["is_filtering"])
        self.assertTrue(self._context(q="x")["is_filtering"])
        self.assertTrue(self._context(period="year")["is_filtering"])
        self.assertFalse(self._context()["is_filtering"])

    def test_25_a_page_and_the_links_keep_q_and_period(self):
        for _ in range(30):
            self._paid(TODAY)
        first = self._context(q="consultation", period="year")
        self.assertEqual(len(first["history"]), 25)
        self.assertEqual(first["prev_page_query"], "")
        self.assertEqual(first["next_page_query"], "?q=consultation&period=year&page=2")
        second = self._context(q="consultation", period="year", page="2")
        self.assertEqual(len(second["history"]), 5)
        self.assertEqual(
            second["prev_page_query"], "?q=consultation&period=year&page=1"
        )
        self.assertEqual(second["next_page_query"], "")
        self.assertEqual(
            {r.pk for r in first["history"]} & {r.pk for r in second["history"]},
            set(),
        )

    def test_the_links_without_a_filter_and_with_a_bad_page(self):
        for _ in range(30):
            self._paid(TODAY)
        self.assertEqual(self._context()["next_page_query"], "?page=2")
        # period only, or q only
        self.assertEqual(
            self._context(period="month")["next_page_query"], "?period=month&page=2"
        )
        self.assertEqual(
            self._context(q="bantay")["next_page_query"], "?q=bantay&page=2"
        )
        # A bad or too-high page falls back like Paginator.get_page
        for page in ("abc", "0", "99"):
            with self.subTest(page=page):
                self.assertTrue(self._context(page=page)["history"])

    # ─── The page, query count ────────────────────────────────────────────────

    def test_the_old_page_still_renders(self):
        paid = self._paid(TODAY)
        unpaid = self._unpaid()
        cancelled = self._bill(BillingReceipt.CANCELLED, billed=TODAY)
        self.client.force_login(self.owner.user)
        for query in ("", "?q=bantay&period=month", "?period=nonsense&page=9"):
            with self.subTest(query=query):
                response = self.client.get(reverse("owner_billing_list") + query)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    {r.pk for r in response.context["receipts"]},
                    {paid.pk, unpaid.pk},
                )
                self.assertContains(response, paid.receipt_number)
                self.assertContains(response, unpaid.receipt_number)
                self.assertNotContains(response, cancelled.receipt_number)

    def _helper_queries(self, count):
        BillingReceipt.objects.all().delete()
        for _ in range(count):
            self._paid(TODAY)
        for _ in range(2):
            self._unpaid()
        self._bill(BillingReceipt.CANCELLED, billed=TODAY)
        with CaptureQueriesContext(connection) as queries:
            self._context(q="consultation", period="year")
        return len(queries)

    def test_query_count_is_flat(self):
        three, thirty = self._helper_queries(3), self._helper_queries(30)
        self.assertEqual(three, thirty)
        # The unpaid list, the History count, the History page
        self.assertEqual(thirty, 3)
