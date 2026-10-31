import re
from datetime import date, datetime, time, timedelta
from datetime import timezone as dt_timezone
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from appointments.models import Appointment
from dashboard.context_processors import shell
from dashboard.templatetags.friendly_dates import friendly_date, friendly_datetime
from dashboard.views import _greeting, _parse_day
from notifications.models import Notification
from pets.models import Pet, PetDeletionRequest, PetOwner

User = get_user_model()

# Noon in Manila on Jun 15 2026 (04:00 UTC). Patched in as "now" so the
# date tests give the same result on any day they run.
FROZEN_NOW = datetime(2026, 6, 15, 4, 0, tzinfo=dt_timezone.utc)


class ShellTestBase(TestCase):
    """One admin, plus one owner with a pet."""

    def setUp(self):
        self.factory = RequestFactory()
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

    # ─── Helpers ──────────────────────────────────────────────────────────────

    def _context(self, user):
        """What the shell context processor returns for this user."""
        request = self.factory.get("/")
        request.user = user
        return shell(request)

    def _shell(self, user):
        return self._context(user)["shell"]

    def _appointment(self, status):
        return Appointment.objects.create(
            owner=self.owner,
            pet=self.pet,
            date=date(2026, 6, 20),
            time=time(9, 0),
            status=status,
        )

    def _deletion_request(self, status):
        return PetDeletionRequest.objects.create(
            pet=self.pet,
            requested_by=self.owner.user,
            reason=PetDeletionRequest.PASSED_AWAY,
            status=status,
        )

    def _notification(self, user, is_read=False):
        return Notification.objects.create(
            recipient=user,
            notification_type=Notification.APPOINTMENT_REQUESTED,
            title="Test",
            message="Test",
            is_read=is_read,
        )


class ShellContextProcessorTests(ShellTestBase):

    def test_anonymous_gets_no_shell(self):
        """Anonymous pages get no "shell" key."""
        self.assertNotIn("shell", self._context(AnonymousUser()))

    def test_no_queries_until_a_value_is_read(self):
        """Building the context runs no queries (HTMX partials stay cheap)."""
        with self.assertNumQueries(0):
            self._context(self.admin)

    def test_owner_sees_zero_admin_counts(self):
        """Owners get 0 for the admin sidebar counts, even with pending rows."""
        self._appointment(Appointment.PENDING)
        self._deletion_request(PetDeletionRequest.PENDING)
        owner_shell = self._shell(self.owner.user)
        self.assertEqual(owner_shell.pending_appointments, 0)
        self.assertEqual(owner_shell.pending_deletions, 0)

    def test_admin_counts_only_pending(self):
        """Admins see pending appointments and pending deletion requests."""
        self._appointment(Appointment.PENDING)
        self._appointment(Appointment.PENDING)
        self._appointment(Appointment.CONFIRMED)
        self._deletion_request(PetDeletionRequest.PENDING)
        self._deletion_request(PetDeletionRequest.REJECTED)
        admin_shell = self._shell(self.admin)
        self.assertEqual(admin_shell.pending_appointments, 2)
        self.assertEqual(admin_shell.pending_deletions, 1)

    def test_unread_count_is_own_unread_only(self):
        """Read notifications and other users' notifications are not counted."""
        self._notification(self.admin)
        self._notification(self.admin)
        self._notification(self.admin, is_read=True)
        self._notification(self.owner.user)
        self.assertEqual(self._shell(self.admin).unread_count, 2)


@mock.patch("django.utils.timezone.now", return_value=FROZEN_NOW)
class FriendlyDateTests(SimpleTestCase):

    def test_friendly_date(self, _now):
        """Relative labels near today, short dates otherwise."""
        today = date(2026, 6, 15)
        cases = {
            today: "Today",
            today - timedelta(days=1): "Yesterday",
            today + timedelta(days=1): "Tomorrow",
            date(2026, 3, 2): "Mon, Mar 2",
            date(2025, 11, 2): "Sun, Nov 2, 2025",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(friendly_date(value), expected)

    def test_empty_values(self, _now):
        """None and "" render as an empty string."""
        for value in (None, ""):
            with self.subTest(value=value):
                self.assertEqual(friendly_date(value), "")
                self.assertEqual(friendly_datetime(value), "")

    def test_just_after_midnight_manila_is_today(self, _now):
        """16:30 UTC on Jun 14 is 00:30 on Jun 15 in Manila."""
        value = datetime(2026, 6, 14, 16, 30, tzinfo=dt_timezone.utc)
        self.assertEqual(friendly_date(value), "Today")
        self.assertEqual(friendly_datetime(value), "Today, 12:30 AM")

    def test_friendly_datetime(self, _now):
        """Aware datetimes are shown in Manila time with a 12-hour clock."""
        cases = {
            # 01:05 UTC = 09:05 Manila
            datetime(2026, 6, 14, 1, 5, tzinfo=dt_timezone.utc): "Yesterday, 9:05 AM",
            # 07:58 UTC = 15:58 Manila
            datetime(2026, 3, 2, 7, 58, tzinfo=dt_timezone.utc): "Mon, Mar 2, 3:58 PM",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(friendly_datetime(value), expected)


class LogoutRedirectTests(ShellTestBase):

    def test_admin_goes_to_admin_login(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("logout"))
        self.assertRedirects(
            response, reverse("admin_login"), fetch_redirect_response=False
        )

    def test_owner_goes_to_owner_login(self):
        self.client.force_login(self.owner.user)
        response = self.client.get(reverse("logout"))
        self.assertRedirects(
            response, reverse("owner_login"), fetch_redirect_response=False
        )


class ClinicInfoFrameTests(ShellTestBase):
    """The shared clinic info page picks its frame by role."""

    def test_admin_gets_admin_layout(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("clinic_info"))
        self.assertTemplateUsed(response, "layouts/admin.html")

    def test_owner_gets_owner_dashboard(self):
        self.client.force_login(self.owner.user)
        response = self.client.get(reverse("clinic_info"))
        self.assertTemplateUsed(response, "base_dashboard.html")


class SidebarTests(ShellTestBase):
    """Nine sidebar links in three groups; tab-only pages light up their parent."""

    LABELS = (
        "Dashboard",
        "Appointments",
        "Pet owners",
        "Pets",
        "Medical records",
        "Vaccinations",
        "Health library",
        "Billing",
        "Clinic settings",
    )

    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin)

    def _sidebar(self, response):
        """Just the <nav aria-label="Admin"> markup."""
        html = response.content.decode()
        start = html.rindex("<nav", 0, html.index('aria-label="Admin"'))
        return html[start : html.index("</nav>", start)]

    def _link(self, nav, url_name):
        """The opening <a> tag of one sidebar link."""
        match = re.search(rf'<a href="{re.escape(reverse(url_name))}"[^>]*>', nav)
        self.assertIsNotNone(match, f"No sidebar link to {url_name}")
        return match.group(0)

    def test_nine_links_and_no_tab_pages(self):
        nav = self._sidebar(self.client.get(reverse("admin_dashboard")))
        for label in self.LABELS:
            with self.subTest(label=label):
                self.assertIn(f'<span class="flex-1">{label}</span>', nav)
        self.assertEqual(nav.count("<a "), 9)
        for name in ("admin_deletion_requests", "admin_service_list", "clinic_info"):
            with self.subTest(name=name):
                self.assertNotIn(f'href="{reverse(name)}"', nav)

    def test_deletion_requests_highlights_pets(self):
        nav = self._sidebar(self.client.get(reverse("admin_deletion_requests")))
        self.assertIn('aria-current="page"', self._link(nav, "admin_pet_list"))

    def test_services_highlights_billing(self):
        nav = self._sidebar(self.client.get(reverse("admin_service_list")))
        self.assertIn('aria-current="page"', self._link(nav, "admin_receipt_list"))


class GreetingTests(SimpleTestCase):

    def test_greeting_by_manila_hour(self):
        """Morning before 12:00, afternoon before 18:00, evening after."""
        cases = {
            # 01:00 UTC = 09:00 Manila
            datetime(2026, 6, 15, 1, 0, tzinfo=dt_timezone.utc): "Good morning",
            # 06:00 UTC = 14:00 Manila
            datetime(2026, 6, 15, 6, 0, tzinfo=dt_timezone.utc): "Good afternoon",
            # 11:00 UTC = 19:00 Manila
            datetime(2026, 6, 15, 11, 0, tzinfo=dt_timezone.utc): "Good evening",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(_greeting(value), expected)


@mock.patch("django.utils.timezone.now", return_value=FROZEN_NOW)
class ParseDayTests(SimpleTestCase):

    def test_valid_iso_date(self, _now):
        self.assertEqual(_parse_day("2026-06-20"), date(2026, 6, 20))

    def test_bad_values_fall_back_to_today(self, _now):
        """Missing, malformed and impossible dates all give today (Manila)."""
        for value in (None, "", "nonsense", "2026-02-30"):
            with self.subTest(value=value):
                self.assertEqual(_parse_day(value), date(2026, 6, 15))


class DashboardViewTests(ShellTestBase):
    """The dashboard page and its two HTMX partials."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin)

    def _booking(self, day, hour, status):
        return Appointment.objects.create(
            owner=self.owner, pet=self.pet, date=day, time=time(hour, 0), status=status
        )

    def _cell(self, response, day):
        """The mini calendar cell for one date."""
        for week in response.context["calendar_weeks"]:
            for cell in week:
                if cell["date"] == day:
                    return cell
        self.fail(f"No calendar cell for {day}")

    def test_partials_are_admin_only(self):
        """Owners: HTMX gets 403, a normal page load is redirected."""
        self.client.force_login(self.owner.user)
        for name in ("admin_dashboard_calendar", "admin_dashboard_day"):
            with self.subTest(name=name):
                url = reverse(name)
                htmx = self.client.get(url, headers={"HX-Request": "true"})
                self.assertEqual(htmx.status_code, 403)
                self.assertRedirects(
                    self.client.get(url),
                    reverse("owner_dashboard"),
                    fetch_redirect_response=False,
                )

    def test_day_lists_that_day_in_time_order_without_cancelled(self):
        day = date(2026, 6, 20)
        later = self._booking(day, 10, Appointment.CONFIRMED)
        earlier = self._booking(day, 9, Appointment.PENDING)
        self._booking(day, 11, Appointment.CANCELLED)
        self._booking(date(2026, 6, 21), 9, Appointment.CONFIRMED)
        response = self.client.get(
            reverse("admin_dashboard_day"), {"date": "2026-06-20"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_date"], day)
        self.assertEqual(list(response.context["day_appointments"]), [earlier, later])

    def test_day_without_date_is_today(self):
        response = self.client.get(reverse("admin_dashboard_day"))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["is_today"])
        self.assertEqual(response.context["selected_date"], timezone.localdate())

    def test_calendar_flags_days_with_pending(self):
        """Yellow dot only on days with a request waiting for approval."""
        self._booking(date(2026, 6, 20), 9, Appointment.PENDING)
        self._booking(date(2026, 6, 22), 9, Appointment.CONFIRMED)
        response = self.client.get(
            reverse("admin_dashboard_calendar"), {"date": "2026-06-01"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self._cell(response, date(2026, 6, 20))["has_pending"])
        self.assertFalse(self._cell(response, date(2026, 6, 22))["has_pending"])

    def test_full_page_has_greeting_and_todays_schedule(self):
        today = timezone.localdate()
        kept = self._booking(today, 9, Appointment.CONFIRMED)
        self._booking(today, 10, Appointment.CANCELLED)
        response = self.client.get(reverse("admin_dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            response.context["greeting"],
            ("Good morning", "Good afternoon", "Good evening"),
        )
        self.assertEqual(response.context["selected_date"], today)
        self.assertEqual(list(response.context["day_appointments"]), [kept])

    def test_refresh_renders_stats_partial(self):
        response = self.client.get(reverse("admin_dashboard_refresh"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "admin/dashboard/_stats.html")

    def test_full_page_uses_layout_and_partials(self):
        response = self.client.get(reverse("admin_dashboard"))
        for template in (
            "layouts/admin.html",
            "admin/dashboard/_stats.html",
            "admin/dashboard/_day_schedule.html",
            "admin/dashboard/_mini_calendar.html",
        ):
            with self.subTest(template=template):
                self.assertTemplateUsed(response, template)

    def test_past_pending_request_is_flagged(self):
        """A request whose date has passed gets a "Date has passed" badge."""
        self._booking(timezone.localdate() - timedelta(days=3), 9, Appointment.PENDING)
        response = self.client.get(reverse("admin_dashboard"))
        self.assertContains(response, "Date has passed")


class RoleAccessMiddlewareTests(ShellTestBase):
    """Wrong-role requests: HTMX gets 403, a normal page load is redirected.
    Logged-out requests go to the matching login page; HTMX gets HX-Redirect."""

    def test_admin_on_owner_url(self):
        self.client.force_login(self.admin)
        url = reverse("owner_dashboard")
        htmx = self.client.get(url, headers={"HX-Request": "true"})
        self.assertEqual(htmx.status_code, 403)
        self.assertRedirects(
            self.client.get(url),
            reverse("admin_dashboard"),
            fetch_redirect_response=False,
        )

    def test_logged_out_admin_page_goes_to_admin_login(self):
        """admin_login ignores ?next=, so none is added."""
        response = self.client.get(reverse("admin_dashboard"))
        self.assertRedirects(
            response, reverse("admin_login"), fetch_redirect_response=False
        )

    def test_logged_out_htmx_gets_hx_redirect(self):
        """Empty 200 with HX-Redirect, so HTMX loads the whole login page."""
        response = self.client.get(
            reverse("admin_dashboard_refresh"), headers={"HX-Request": "true"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"")
        self.assertEqual(response.headers["HX-Redirect"], reverse("admin_login"))

    def test_logged_out_owner_page_goes_to_owner_login(self):
        url = reverse("owner_dashboard")
        self.assertRedirects(
            self.client.get(url),
            f"{reverse('owner_login')}?next={url}",
            fetch_redirect_response=False,
        )
