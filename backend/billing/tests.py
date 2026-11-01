import datetime
import inspect
from decimal import Decimal
from unittest import mock
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from billing.models import BillingItem, BillingReceipt
from billing.views import LOCKED_MESSAGE
from notifications.models import Notification
from pets.models import Pet, PetOwner

User = get_user_model()

LOCKED_STATUSES = [BillingReceipt.PAID, BillingReceipt.CANCELLED]


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
        """A new receipt is always PENDING, even if the POST says paid."""
        data = self._edit_data(
            owner=self.owner.pk,
            pet=self.pet.pk,
            payment_status=BillingReceipt.PAID,
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
