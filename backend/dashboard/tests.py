import re
from datetime import date, datetime, time, timedelta
from datetime import timezone as dt_timezone
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.db import connection
from django.template import engines
from django.template.loader import render_to_string
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from appointments.models import Appointment
from billing.models import Service
from dashboard.context_processors import shell
from dashboard.templatetags.friendly_dates import (
    friendly_date,
    friendly_datetime,
    long_date,
)
from dashboard.utils import (
    booking_limit,
    calendar_month_range,
    dashboard_calendar_context,
    day_schedule,
    month_cells,
    month_day_counts,
    pending_requests,
    resolve_month,
    resolve_picked,
    slot_minutes,
)
from dashboard.views import _greeting
from medical.models import MedicalRecord, Vaccination
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

    def test_long_date(self, _now):
        """Today and tomorrow keep their label; only other years get the year."""
        today = date(2026, 6, 15)
        cases = {
            today: "Today, Monday, June 15",
            today + timedelta(days=1): "Tomorrow, Tuesday, June 16",
            date(2026, 11, 2): "Monday, November 2",
            date(2027, 1, 4): "Monday, January 4, 2027",
            date(2025, 11, 2): "Sunday, November 2, 2025",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(long_date(value), expected)

    def test_long_date_across_new_year(self, _now):
        """Dec 31 in Manila: Jan 1 is 'Tomorrow', Jan 2 needs the new year."""
        # 04:00 UTC = 12:00 Manila on Dec 31, 2026
        _now.return_value = datetime(2026, 12, 31, 4, 0, tzinfo=dt_timezone.utc)
        cases = {
            date(2026, 12, 30): "Wednesday, December 30",
            date(2027, 1, 1): "Tomorrow, Friday, January 1",
            date(2027, 1, 2): "Saturday, January 2, 2027",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(long_date(value), expected)
        # On Jan 1, 2027, the day before is last year
        _now.return_value = datetime(2027, 1, 1, 4, 0, tzinfo=dt_timezone.utc)
        self.assertEqual(long_date(date(2026, 12, 31)), "Thursday, December 31, 2026")

    def test_mid_sentence_lowercases_relative_labels_only(self, _now):
        """With "mid", today/tomorrow/yesterday are lowercase; others unchanged."""
        today = date(2026, 6, 15)
        friendly_cases = {
            today: "today",
            today - timedelta(days=1): "yesterday",
            today + timedelta(days=1): "tomorrow",
            date(2026, 3, 2): "Mon, Mar 2",
            date(2025, 11, 2): "Sun, Nov 2, 2025",
        }
        for value, expected in friendly_cases.items():
            with self.subTest(filter="friendly_date", value=value):
                self.assertEqual(friendly_date(value, "mid"), expected)
        long_cases = {
            today: "today, Monday, June 15",
            today + timedelta(days=1): "tomorrow, Tuesday, June 16",
            today - timedelta(days=1): "Sunday, June 14",
            date(2026, 11, 2): "Monday, November 2",
            date(2027, 1, 4): "Monday, January 4, 2027",
        }
        for value, expected in long_cases.items():
            with self.subTest(filter="long_date", value=value):
                self.assertEqual(long_date(value, "mid"), expected)

    def test_mid_sentence_empty_values(self, _now):
        for value in (None, ""):
            with self.subTest(value=value):
                self.assertEqual(friendly_date(value, "mid"), "")
                self.assertEqual(long_date(value, "mid"), "")

    def test_empty_values(self, _now):
        """None and "" render as an empty string."""
        for value in (None, ""):
            with self.subTest(value=value):
                self.assertEqual(friendly_date(value), "")
                self.assertEqual(friendly_datetime(value), "")
                self.assertEqual(long_date(value), "")

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


class TopbarSearchTests(ShellTestBase):
    """The global search sits in {% block topbar_search %}, so a page can drop it."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin)

    def test_normal_admin_page_has_global_search(self):
        response = self.client.get(reverse("admin_dashboard"))
        self.assertContains(response, 'id="global-search"')
        self.assertContains(response, 'placeholder="Find a pet or owner"')
        self.assertContains(
            response,
            '<label for="global-search" class="sr-only">Find a pet or owner</label>',
        )

    def test_page_can_empty_the_block(self):
        page = engines["django"].from_string(
            '{% extends "layouts/admin.html" %}{% block topbar_search %}{% endblock %}'
        )
        request = self.factory.get(reverse("admin_dashboard"))
        request.user = self.admin
        html = page.render({}, request)
        self.assertNotIn("Find a pet or owner", html)
        self.assertNotIn('id="global-search', html)
        # The rest of the top bar stays
        self.assertIn('id="bell-button"', html)

    def test_layout_loads_dialogs_js(self):
        response = self.client.get(reverse("admin_dashboard"))
        self.assertRegex(
            response.content.decode(),
            r'<script src="/static/js/admin/dialogs\.[0-9a-f]+\.js" defer></script>',
        )


class ShellLayoutTests(ShellTestBase):
    """Sidebar width, and the logo block and top bar sharing one height."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin)
        self.html = self.client.get(reverse("admin_dashboard")).content.decode()

    def _classes(self, pattern):
        """The class list of the first opening tag matching pattern."""
        match = re.search(pattern, self.html)
        self.assertIsNotNone(match, f"No tag matching {pattern}")
        return match.group(1).split()

    def test_sidebar_is_w_56(self):
        classes = self._classes(r'<aside id="admin-sidebar" class="([^"]*)"')
        self.assertIn("w-56", classes)
        self.assertNotIn("w-72", classes)

    def test_logo_block_and_top_bar_are_both_h_16(self):
        # The logo block is the first <div> inside the sidebar
        logo = self._classes(r'<aside id="admin-sidebar"[^>]*>\s*<div class="([^"]*)"')
        header = self._classes(r'<header class="([^"]*)"')
        for name, classes in (("logo block", logo), ("top bar", header)):
            with self.subTest(block=name):
                for cls in ("h-16", "shrink-0", "flex", "items-center"):
                    self.assertIn(cls, classes)
                for cls in ("py-3", "py-4"):
                    self.assertNotIn(cls, classes)
        self.assertIn("border-b", header)

    def test_logo_block_has_no_bottom_border(self):
        logo = self._classes(r'<aside id="admin-sidebar"[^>]*>\s*<div class="([^"]*)"')
        self.assertNotIn("border-b", logo)
        self.assertNotIn("border-violet-200", logo)

    def test_clinic_name_wraps_to_two_lines_with_title(self):
        self.assertIn(
            '<p class="line-clamp-2 text-base font-semibold leading-5 text-gray-900" '
            'title="Hapi Vet">Hapi Vet</p>',
            self.html,
        )

    def _logo_link(self):
        """The sidebar's logo + clinic name link (the first <a> in the sidebar)."""
        sidebar = self.html[self.html.index('<aside id="admin-sidebar"') :]
        match = re.search(r"<a [^>]*>.*?</a>", sidebar, re.S)
        self.assertIsNotNone(match, "No link in the sidebar")
        return match.group(0)

    def test_logo_and_name_link_to_the_dashboard(self):
        link = self._logo_link()
        self.assertTrue(link.startswith(f'<a href="{reverse("admin_dashboard")}"'))
        self.assertIn(">Hapi Vet</p>", link)
        self.assertIn('<span class="sr-only">, go to Dashboard</span>', link)

    def test_close_menu_button_is_outside_the_link(self):
        self.assertNotIn("Close menu", self._logo_link())
        self.assertNotIn("data-sidebar-close", self._logo_link())
        # Still in the logo block, right after the link
        sidebar = self.html[self.html.index('<aside id="admin-sidebar"') :]
        after_link = sidebar[sidebar.index("</a>") :]
        self.assertRegex(
            after_link,
            r'^</a>\s*<button type="button" class="btn btn-outline px-3 lg:hidden" '
            r"data-sidebar-close>",
        )

    def test_no_admin_subtitle(self):
        self.assertNotIn('<p class="text-sm text-gray-700">Admin</p>', self.html)
        self.assertNotRegex(self.html, r">\s*Admin\s*</p>")


def _squash(html):
    """Rendered markup with indentation and blank lines removed."""
    return "".join(line.strip() for line in html.splitlines())


class IconComponentTests(SimpleTestCase):

    def _icon(self, **context):
        return _squash(render_to_string("components/icon.html", context))

    def test_existing_icon_renders_unchanged(self):
        self.assertEqual(
            self._icon(name="x"),
            '<svg class="size-5 shrink-0" xmlns="http://www.w3.org/2000/svg" '
            'width="20" height="20" viewBox="0 0 24 24" fill="none" '
            'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
            'stroke-linejoin="round" aria-hidden="true" focusable="false">'
            '<path d="M18 6 6 18" /><path d="m6 6 12 12" /></svg>',
        )

    def test_stroke_param(self):
        self.assertIn('stroke-width="1.5"', self._icon(name="x", stroke="1.5"))
        self.assertNotIn('stroke-width="2"', self._icon(name="x", stroke="1.5"))

    def test_new_icon_renders(self):
        html = self._icon(name="dog", **{"class": "size-9"})
        self.assertIn('class="size-9"', html)
        self.assertIn('<path d="M11.25 16.25h1.5L12 17z" />', html)
        self.assertEqual(html.count("<path"), 5)


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
        """The calendar cell for one date."""
        for cell in response.context["cal_cells"]:
            if cell["in_month"] and cell["date"] == day:
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

    # Frozen in June so ?date=2026-06-20 is inside the clamped month range.
    @mock.patch("django.utils.timezone.now", return_value=FROZEN_NOW)
    def test_day_lists_that_day_in_time_order_without_cancelled(self, _now):
        day = date(2026, 6, 20)
        later = self._booking(day, 10, Appointment.CONFIRMED)
        self._booking(day, 9, Appointment.PENDING)
        earlier = self._booking(day, 8, Appointment.COMPLETED)
        self._booking(day, 11, Appointment.CANCELLED)
        self._booking(date(2026, 6, 21), 9, Appointment.CONFIRMED)
        response = self.client.get(
            reverse("admin_dashboard_day"), {"date": "2026-06-20"}
        )
        self.assertEqual(response.status_code, 200)
        schedule = response.context["schedule"]
        self.assertEqual(schedule["day"], day)
        # A Pending request is not a row; it counts as waiting
        self.assertEqual(schedule["rows"], [earlier, later])
        self.assertEqual(schedule["waiting_count"], 1)

    def test_day_without_date_is_today(self):
        response = self.client.get(reverse("admin_dashboard_day"))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["schedule"]["is_today"])
        self.assertEqual(response.context["schedule"]["day"], timezone.localdate())

    # Frozen in June so ?date=2026-06-01 is inside the clamped month range.
    @mock.patch("django.utils.timezone.now", return_value=FROZEN_NOW)
    def test_calendar_flags_days_with_pending(self, _now):
        """Yellow pill only on days with a request waiting for approval."""
        self._booking(date(2026, 6, 20), 9, Appointment.PENDING)
        self._booking(date(2026, 6, 22), 9, Appointment.CONFIRMED)
        response = self.client.get(
            reverse("admin_dashboard_calendar"), {"date": "2026-06-01"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self._cell(response, date(2026, 6, 20))["has_waiting"])
        self.assertFalse(self._cell(response, date(2026, 6, 22))["has_waiting"])

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
        self.assertEqual(response.context["schedule"]["day"], today)
        self.assertEqual(response.context["schedule"]["rows"], [kept])

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


# ─── Dashboard v5 calendar (batch D1) ─────────────────────────────────────────

# Monday, Nov 2, 2026. The pure helpers take it as `today`.
CAL_TODAY = date(2026, 11, 2)
# Noon in Manila on CAL_TODAY (04:00 UTC), for the views.
CAL_NOW = datetime(2026, 11, 2, 4, 0, tzinfo=dt_timezone.utc)
# Aug 2026 to Dec 2026: the range on CAL_TODAY with the default 30 days.
CAL_MONTHS = [date(2026, m, 1) for m in (8, 9, 10, 11, 12)]


class CalendarMonthRangeTests(SimpleTestCase):

    def test_three_months_back_to_the_booking_limit(self):
        self.assertEqual(calendar_month_range(CAL_TODAY, 30), CAL_MONTHS)

    def test_longer_limit_reaches_next_year(self):
        months = calendar_month_range(CAL_TODAY, 60)
        self.assertEqual(months[0], date(2026, 8, 1))
        self.assertEqual(months[-1], date(2027, 1, 1))
        self.assertEqual(len(months), 6)

    def test_booking_limit_without_settings(self):
        self.assertEqual(booking_limit(None), 30)


class ResolveMonthTests(SimpleTestCase):

    def _resolve(self, value):
        return resolve_month(value, CAL_TODAY, CAL_MONTHS)

    def test_valid_month(self):
        self.assertEqual(self._resolve("2026-12"), date(2026, 12, 1))

    def test_missing_and_bad_values_give_todays_month(self):
        for value in (None, "", "abc", "2026-13", "2026-00", "2026-1", "2026-12-01"):
            with self.subTest(value=value):
                self.assertEqual(self._resolve(value), date(2026, 11, 1))

    def test_clamped_to_the_range(self):
        cases = {
            "2026-07": date(2026, 8, 1),
            "2027-01": date(2026, 12, 1),
            "9999-12": date(2026, 12, 1),
            "0001-01": date(2026, 8, 1),
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(self._resolve(value), expected)


class ResolvePickedTests(SimpleTestCase):

    def _resolve(self, value):
        return resolve_picked(value, CAL_TODAY, CAL_MONTHS)

    def test_valid_day(self):
        self.assertEqual(self._resolve("2026-11-05"), date(2026, 11, 5))

    def test_range_edges_are_allowed(self):
        self.assertEqual(self._resolve("2026-08-01"), date(2026, 8, 1))
        self.assertEqual(self._resolve("2026-12-31"), date(2026, 12, 31))

    def test_bad_values(self):
        for value in (None, "", "abc", "2026-02-30", "2026-11", "20261105"):
            with self.subTest(value=value):
                self.assertIsNone(self._resolve(value))

    def test_outside_the_range(self):
        for value in ("2026-07-31", "2027-01-01", "9999-12-31", "0001-01-01"):
            with self.subTest(value=value):
                self.assertIsNone(self._resolve(value))

    def test_today_is_not_a_pick(self):
        self.assertIsNone(self._resolve("2026-11-02"))


class MonthCellsTests(SimpleTestCase):

    NOV = date(2026, 11, 1)

    def _day(self, cells, n):
        return next(c for c in cells if c["in_month"] and c["day"] == n)

    def test_always_42_cells(self):
        for month in (self.NOV, date(2027, 2, 1)):
            with self.subTest(month=month):
                self.assertEqual(len(month_cells(month, CAL_TODAY, None, {})), 42)

    def test_monday_first(self):
        """Nov 1, 2026 is a Sunday, so six blanks come before it."""
        cells = month_cells(self.NOV, CAL_TODAY, None, {})
        self.assertEqual(cells[:6], [{"in_month": False}] * 6)
        self.assertEqual(cells[6]["date"], self.NOV)
        self.assertEqual(cells[7]["date"], date(2026, 11, 2))
        self.assertEqual(cells[35]["date"], date(2026, 11, 30))
        self.assertFalse(cells[36]["in_month"])

    def test_counts_and_pills(self):
        counts = {
            date(2026, 11, 5): {"visits": 2, "waiting": 1},
            date(2026, 11, 6): {"visits": 12, "waiting": 0},
        }
        cells = month_cells(self.NOV, CAL_TODAY, None, counts)
        empty, three, twelve = (self._day(cells, n) for n in (4, 5, 6))
        self.assertEqual((empty["total"], empty["pill"]), (0, ""))
        self.assertFalse(empty["has_waiting"])
        self.assertEqual(
            (three["visits"], three["waiting"], three["total"], three["pill"]),
            (2, 1, 3, "3"),
        )
        self.assertTrue(three["has_waiting"])
        self.assertEqual((twelve["total"], twelve["pill"]), (12, "9+"))
        self.assertFalse(twelve["has_waiting"])

    def test_today_picked_and_weekend_flags(self):
        cells = month_cells(self.NOV, CAL_TODAY, date(2026, 11, 5), {})
        self.assertTrue(self._day(cells, 2)["is_today"])
        self.assertFalse(self._day(cells, 3)["is_today"])
        self.assertTrue(self._day(cells, 5)["is_picked"])
        self.assertFalse(self._day(cells, 2)["is_picked"])
        for n, weekend in ((6, False), (7, True), (8, True), (9, False)):
            with self.subTest(day=n):
                self.assertEqual(self._day(cells, n)["is_weekend"], weekend)

    def test_aria_labels(self):
        counts = {
            date(2026, 11, 5): {"visits": 2, "waiting": 1},
            date(2026, 11, 6): {"visits": 1, "waiting": 3},
        }
        cells = month_cells(self.NOV, CAL_TODAY, None, counts)
        self.assertEqual(
            self._day(cells, 5)["aria_label"],
            "Thursday, November 5: 2 visits, 1 request waiting",
        )
        self.assertEqual(
            self._day(cells, 6)["aria_label"],
            "Friday, November 6: 1 visit, 3 requests waiting",
        )
        self.assertEqual(
            self._day(cells, 2)["aria_label"], "Today, Monday, November 2: no visits"
        )


class MonthDayCountsTests(ShellTestBase):

    def _booking(self, day, status, hour=9):
        Appointment.objects.create(
            owner=self.owner, pet=self.pet, date=day, time=time(hour, 0), status=status
        )

    def test_one_query_and_only_that_month(self):
        self._booking(date(2026, 11, 5), Appointment.CONFIRMED, 9)
        self._booking(date(2026, 11, 5), Appointment.COMPLETED, 10)
        self._booking(date(2026, 11, 5), Appointment.NO_SHOW, 11)
        self._booking(date(2026, 11, 5), Appointment.PENDING, 12)
        self._booking(date(2026, 11, 5), Appointment.CANCELLED, 13)
        self._booking(date(2026, 11, 30), Appointment.CANCELLED)
        self._booking(date(2026, 10, 31), Appointment.CONFIRMED)
        self._booking(date(2026, 12, 1), Appointment.PENDING)
        with self.assertNumQueries(1):
            counts = month_day_counts(date(2026, 11, 1))
        self.assertEqual(counts, {date(2026, 11, 5): {"visits": 3, "waiting": 1}})


class DashboardCalendarContextTests(TestCase):

    def test_home_on_open(self):
        cal = dashboard_calendar_context(CAL_TODAY)
        self.assertEqual(cal["cal_month"], date(2026, 11, 1))
        self.assertEqual(cal["cal_month_value"], "2026-11")
        self.assertEqual(cal["cal_today_month_value"], "2026-11")
        self.assertIsNone(cal["cal_picked"])
        self.assertTrue(cal["cal_is_home"])
        self.assertEqual(len(cal["cal_cells"]), 42)

    def test_another_month_is_not_home(self):
        cal = dashboard_calendar_context(CAL_TODAY, "2026-12")
        self.assertEqual(cal["cal_month_value"], "2026-12")
        self.assertFalse(cal["cal_is_home"])

    def test_picked_day_is_not_home(self):
        cal = dashboard_calendar_context(CAL_TODAY, "2026-11", "2026-11-05")
        self.assertEqual(cal["cal_picked"], date(2026, 11, 5))
        self.assertFalse(cal["cal_is_home"])

    def test_picked_day_without_month_opens_its_month(self):
        cal = dashboard_calendar_context(CAL_TODAY, None, "2026-12-10")
        self.assertEqual(cal["cal_month"], date(2026, 12, 1))
        self.assertEqual(cal["cal_picked"], date(2026, 12, 10))

    def test_month_options(self):
        cal = dashboard_calendar_context(CAL_TODAY, "2026-10")
        self.assertEqual(
            [o["label"] for o in cal["cal_month_options"]],
            [
                "August 2026",
                "September 2026",
                "October 2026",
                "November 2026",
                "December 2026",
            ],
        )
        self.assertEqual(cal["cal_month_options"][2]["value"], "2026-10")
        self.assertEqual(
            [o["value"] for o in cal["cal_month_options"] if o["selected"]],
            ["2026-10"],
        )


class DashboardStatFixTests(ShellTestBase):
    """Vaccinations and follow-ups due this week (today to today + 7)."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin)
        self.archived = Pet.objects.create(
            owner=self.owner, name="Old", species="Cat", is_archived=True
        )
        self.soon = timezone.localdate() + timedelta(days=3)

    def _stats(self):
        return self.client.get(reverse("admin_dashboard_refresh")).context

    def _vaccination(self, is_corrected):
        Vaccination.objects.create(
            pet=self.pet,
            vaccine_name=Vaccination.ANTI_RABIES,
            date_administered=timezone.localdate() - timedelta(days=300),
            next_due_date=self.soon,
            is_corrected=is_corrected,
        )

    def _followup(self, pet):
        MedicalRecord.objects.create(
            pet=pet,
            record_date=timezone.localdate(),
            follow_up_required=True,
            follow_up_date=self.soon,
        )

    def test_corrected_vaccination_is_counted(self):
        """Batch 5a-3: a corrected dose counts like any other (D1 left it out)."""
        self._vaccination(is_corrected=True)
        self.assertEqual(self._stats()["vaccinations_due"], 1)
        self._vaccination(is_corrected=False)
        self.assertEqual(self._stats()["vaccinations_due"], 2)

    def test_archived_owner_is_not_counted(self):
        self._vaccination(is_corrected=False)
        self._followup(self.pet)
        stats = self._stats()
        self.assertEqual((stats["vaccinations_due"], stats["followups_due"]), (1, 1))
        self.owner.archive()
        stats = self._stats()
        self.assertEqual((stats["vaccinations_due"], stats["followups_due"]), (0, 0))

    def test_followup_for_archived_pet_is_not_counted(self):
        self._followup(self.archived)
        self.assertEqual(self._stats()["followups_due"], 0)
        self._followup(self.pet)
        self.assertEqual(self._stats()["followups_due"], 1)


@mock.patch("django.utils.timezone.now", return_value=CAL_NOW)
class CalendarEndpointTests(ShellTestBase):
    """admin_dashboard_calendar with "now" at noon on Mon, Nov 2, 2026 (Manila)."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin)

    def _get(self, **params):
        return self.client.get(reverse("admin_dashboard_calendar"), params)

    def test_month_param(self, _now):
        response = self._get(month="2026-12")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["cal_month"], date(2026, 12, 1))

    def test_far_off_values_are_clamped(self, _now):
        cases = (
            ({"month": "9999-12"}, date(2026, 12, 1)),
            ({"date": "9999-12-15"}, date(2026, 12, 1)),
            ({"date": "0001-01-15"}, date(2026, 8, 1)),
        )
        for params, expected in cases:
            with self.subTest(params=params):
                response = self._get(**params)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["cal_month"], expected)

    def test_old_date_param_still_works(self, _now):
        response = self._get(date="2026-10-05")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["cal_month"], date(2026, 10, 1))
        selected = [o for o in response.context["cal_month_options"] if o["selected"]]
        self.assertEqual([o["label"] for o in selected], ["October 2026"])

    def test_picked_param(self, _now):
        response = self._get(month="2026-11", picked="2026-11-05")
        self.assertEqual(response.context["cal_picked"], date(2026, 11, 5))
        self.assertFalse(response.context["cal_is_home"])

    def test_day_param_is_the_picked_day(self, _now):
        """The month dropdown's form sends the picked day as ?day."""
        response = self._get(month="2026-12", day="2026-11-05")
        self.assertEqual(response.context["cal_month"], date(2026, 12, 1))
        self.assertEqual(response.context["cal_picked"], date(2026, 11, 5))
        self.assertFalse(response.context["cal_is_home"])

    def test_picked_wins_over_day(self, _now):
        response = self._get(month="2026-11", picked="2026-11-05", day="2026-11-09")
        self.assertEqual(response.context["cal_picked"], date(2026, 11, 5))

    def test_non_admin_htmx_gets_403(self, _now):
        self.client.force_login(self.owner.user)
        response = self.client.get(
            reverse("admin_dashboard_calendar"),
            {"month": "2026-12"},
            headers={"HX-Request": "true"},
        )
        self.assertEqual(response.status_code, 403)


# ─── Dashboard v5 schedule and Needs approval (batch D2) ──────────────────────

# Thursday, Nov 5, 2026: a picked day inside the calendar range.
PICKED_DAY = date(2026, 11, 5)


def _manila(utc_hour, minute):
    """Nov 2, 2026 at utc_hour:minute UTC as Manila local time (UTC+8)."""
    value = datetime(2026, 11, 2, utc_hour, minute, tzinfo=dt_timezone.utc)
    return timezone.localtime(value)


class DashboardV5TestBase(ShellTestBase):

    def _booking(self, day, hour, status, minute=0):
        return Appointment.objects.create(
            owner=self.owner,
            pet=self.pet,
            date=day,
            time=time(hour, minute),
            status=status,
        )

    def _schedule(self, day, now=None, clinic=None):
        return day_schedule(day, CAL_TODAY, now or timezone.localtime(CAL_NOW), clinic)


class DayScheduleTests(DashboardV5TestBase):

    def test_rows_counts_and_order(self):
        """Visits are rows by time then id; Pending only counts as waiting."""
        first_ten = self._booking(CAL_TODAY, 10, Appointment.CONFIRMED)
        completed = self._booking(CAL_TODAY, 8, Appointment.COMPLETED)
        no_show = self._booking(CAL_TODAY, 9, Appointment.NO_SHOW)
        second_ten = self._booking(CAL_TODAY, 10, Appointment.CONFIRMED)
        self._booking(CAL_TODAY, 11, Appointment.PENDING)
        self._booking(CAL_TODAY, 12, Appointment.CANCELLED)
        self._booking(PICKED_DAY, 9, Appointment.CONFIRMED)
        schedule = self._schedule(CAL_TODAY)
        self.assertEqual(schedule["rows"], [completed, no_show, first_ten, second_ten])
        self.assertTrue(schedule["is_today"])
        self.assertEqual(schedule["visit_count"], 4)
        self.assertEqual(schedule["done_count"], 2)
        self.assertEqual(schedule["to_go_count"], 2)
        self.assertEqual(schedule["waiting_count"], 1)
        self.assertEqual(schedule["summary"], "4 visits · 2 done · 2 to go")

    def test_today_with_one_visit_is_singular(self):
        self._booking(CAL_TODAY, 15, Appointment.CONFIRMED)
        self.assertEqual(
            self._schedule(CAL_TODAY)["summary"], "1 visit · 0 done · 1 to go"
        )

    def test_picked_day_with_waiting(self):
        self._booking(PICKED_DAY, 9, Appointment.CONFIRMED)
        self._booking(PICKED_DAY, 10, Appointment.CONFIRMED)
        self._booking(PICKED_DAY, 11, Appointment.PENDING)
        schedule = self._schedule(PICKED_DAY)
        self.assertFalse(schedule["is_today"])
        self.assertEqual(schedule["summary"], "2 visits · 1 waiting for approval")

    def test_picked_day_without_waiting(self):
        self._booking(PICKED_DAY, 9, Appointment.CONFIRMED)
        self._booking(PICKED_DAY, 10, Appointment.COMPLETED)
        self.assertEqual(self._schedule(PICKED_DAY)["summary"], "2 visits")

    def test_no_visits_today(self):
        """Today's summary never mentions waiting requests."""
        self._booking(CAL_TODAY, 11, Appointment.PENDING)
        self.assertEqual(self._schedule(CAL_TODAY)["summary"], "No visits")

    def test_no_visits_on_a_picked_day_with_one_waiting(self):
        self._booking(PICKED_DAY, 11, Appointment.PENDING)
        self._booking(PICKED_DAY, 12, Appointment.CANCELLED)
        schedule = self._schedule(PICKED_DAY)
        self.assertEqual(schedule["rows"], [])
        self.assertEqual(schedule["summary"], "No visits · 1 waiting for approval")

    def test_picked_busy_day_shows_three_rows(self):
        rows = [
            self._booking(PICKED_DAY, h, Appointment.CONFIRMED) for h in range(8, 13)
        ]
        schedule = self._schedule(PICKED_DAY)
        self.assertEqual(schedule["shown_rows"], rows[:3])
        self.assertEqual(schedule["more_count"], 2)

    def test_today_shows_every_row(self):
        rows = [
            self._booking(CAL_TODAY, h, Appointment.CONFIRMED) for h in range(8, 13)
        ]
        schedule = self._schedule(CAL_TODAY)
        self.assertEqual(schedule["shown_rows"], rows)
        self.assertEqual(schedule["more_count"], 0)


class NextVisitTests(DashboardV5TestBase):
    """Nov 2, 2026 with 60-minute slots: 8:00 Completed, 10:00 and 14:00
    Confirmed. Times below are Manila time."""

    def setUp(self):
        super().setUp()
        self.eight = self._booking(CAL_TODAY, 8, Appointment.COMPLETED)
        self.ten = self._booking(CAL_TODAY, 10, Appointment.CONFIRMED)
        self.two_pm = self._booking(CAL_TODAY, 14, Appointment.CONFIRMED)

    def _next(self, now, clinic=None):
        return self._schedule(CAL_TODAY, now, clinic)["next_id"]

    def test_before_the_visit(self):
        # 01:40 UTC = 9:40 Manila
        self.assertEqual(self._next(_manila(1, 40)), self.ten.id)

    def test_visit_in_progress_still_counts(self):
        # 02:20 UTC = 10:20 Manila
        self.assertEqual(self._next(_manila(2, 20)), self.ten.id)

    def test_completed_visit_is_skipped(self):
        self.ten.status = Appointment.COMPLETED
        self.ten.save()
        # 02:20 UTC = 10:20 Manila
        self.assertEqual(self._next(_manila(2, 20)), self.two_pm.id)

    def test_none_after_the_last_slot(self):
        # 07:10 UTC = 15:10 Manila
        self.assertIsNone(self._next(_manila(7, 10)))

    def test_picked_day_has_no_next(self):
        self._booking(PICKED_DAY, 10, Appointment.CONFIRMED)
        # 01:40 UTC = 9:40 Manila
        self.assertIsNone(self._schedule(PICKED_DAY, _manila(1, 40))["next_id"])

    def test_clinic_none_uses_60_minutes(self):
        self.assertEqual(slot_minutes(None), 60)
        # 02:50 UTC = 10:50 Manila: inside a 60-minute slot, past a 30-minute one
        self.assertEqual(self._next(_manila(2, 50)), self.ten.id)
        short_slots = SimpleNamespace(slot_duration_minutes=30)
        self.assertEqual(self._next(_manila(2, 50), short_slots), self.two_pm.id)


class PendingRequestsTests(DashboardV5TestBase):

    def test_every_pending_request_no_cap(self):
        for n in range(7):
            self._booking(CAL_TODAY + timedelta(days=n), 9, Appointment.PENDING)
        self.assertEqual(len(pending_requests()), 7)

    def test_order_by_date_then_time(self):
        late = self._booking(PICKED_DAY, 14, Appointment.PENDING)
        early = self._booking(PICKED_DAY, 9, Appointment.PENDING)
        first = self._booking(CAL_TODAY, 16, Appointment.PENDING)
        self.assertEqual(pending_requests(), [first, early, late])

    def test_non_pending_excluded(self):
        kept = self._booking(PICKED_DAY, 9, Appointment.PENDING)
        for status in (
            Appointment.CONFIRMED,
            Appointment.COMPLETED,
            Appointment.NO_SHOW,
            Appointment.CANCELLED,
        ):
            self._booking(PICKED_DAY, 10, status)
        self.assertEqual(pending_requests(), [kept])

    def test_date_has_passed_only_for_past_dates(self):
        """The Needs approval markup flags rows with `appointment.date < today`."""
        self._booking(CAL_TODAY - timedelta(days=1), 9, Appointment.PENDING)
        self._booking(CAL_TODAY, 9, Appointment.PENDING)
        self._booking(PICKED_DAY, 9, Appointment.PENDING)
        flags = engines["django"].from_string(
            "{% for appointment in pending_requests %}"
            "{% if appointment.date < today %}past{% else %}ok{% endif %} "
            "{% endfor %}"
        )
        html = flags.render(
            {"pending_requests": pending_requests(), "today": CAL_TODAY}
        )
        self.assertEqual(html.split(), ["past", "ok", "ok"])


@mock.patch("django.utils.timezone.now", return_value=CAL_NOW)
class DayEndpointTests(DashboardV5TestBase):
    """admin_dashboard_day with "now" at noon on Mon, Nov 2, 2026 (Manila)."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin)

    def _get(self, **params):
        return self.client.get(
            reverse("admin_dashboard_day"), params, headers={"HX-Request": "true"}
        )

    def test_picked_day_returns_schedule_and_oob_calendar(self, _now):
        visit = self._booking(PICKED_DAY, 9, Appointment.CONFIRMED)
        response = self._get(date="2026-11-05")
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "admin/dashboard/_day_response.html")
        self.assertTemplateUsed(response, "admin/dashboard/_day_schedule.html")
        self.assertEqual(response.context["schedule"]["day"], PICKED_DAY)
        self.assertEqual(response.context["schedule"]["rows"], [visit])
        self.assertEqual(response.context["cal_picked"], PICKED_DAY)
        self.assertEqual(response.context["cal_month"], date(2026, 11, 1))
        self.assertContains(
            response,
            '<section id="mini-calendar" class="card p-5" aria-label="Calendar" '
            'hx-swap-oob="outerHTML">',
        )
        self.assertContains(response, 'id="day-schedule"')

    def test_picked_day_in_another_month_opens_that_month(self, _now):
        response = self._get(date="2026-12-10")
        self.assertEqual(response.context["cal_month"], date(2026, 12, 1))

    def test_no_date_is_today(self, _now):
        response = self._get()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["schedule"]["day"], CAL_TODAY)
        self.assertTrue(response.context["schedule"]["is_today"])
        self.assertIsNone(response.context["cal_picked"])
        self.assertTrue(response.context["cal_is_home"])
        self.assertContains(response, 'hx-swap-oob="outerHTML"')

    def test_bad_or_out_of_range_date_is_today(self, _now):
        for value in ("nonsense", "2026-02-30", "2026-07-31", "2027-01-01"):
            with self.subTest(value=value):
                response = self._get(date=value)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["schedule"]["day"], CAL_TODAY)
                self.assertIsNone(response.context["cal_picked"])

    def test_non_admin_htmx_gets_403(self, _now):
        self.client.force_login(self.owner.user)
        self.assertEqual(self._get(date="2026-11-05").status_code, 403)


@mock.patch("django.utils.timezone.now", return_value=CAL_NOW)
class FullPageDayParamTests(DashboardV5TestBase):
    """admin_dashboard ?day= with "now" at noon on Mon, Nov 2, 2026 (Manila)."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin)

    def test_day_param_picks_the_day(self, _now):
        response = self.client.get(reverse("admin_dashboard"), {"day": "2026-11-05"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["schedule"]["day"], PICKED_DAY)
        self.assertEqual(response.context["cal_picked"], PICKED_DAY)
        self.assertNotContains(response, "hx-swap-oob")

    def test_no_day_param_is_today(self, _now):
        response = self.client.get(reverse("admin_dashboard"))
        self.assertEqual(response.context["schedule"]["day"], CAL_TODAY)
        self.assertIsNone(response.context["cal_picked"])

    def test_new_context_keys(self, _now):
        pending = self._booking(PICKED_DAY, 9, Appointment.PENDING)
        self._notification(self.admin)
        response = self.client.get(reverse("admin_dashboard"))
        self.assertEqual(response.context["pending_requests"], [pending])
        self.assertNotIn("recent_activity", response.context)

    def test_report_query_count(self, _now):
        """Prints the query count of one full page load (reported, not asserted)."""
        self._booking(CAL_TODAY, 9, Appointment.CONFIRMED)
        self._booking(PICKED_DAY, 9, Appointment.PENDING)
        self._notification(self.admin)
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(reverse("admin_dashboard"))
        self.assertEqual(response.status_code, 200)
        print(f"\nadmin_dashboard full page: {len(queries)} queries")


# ─── Dashboard v5 templates (batch D3) ────────────────────────────────────────

# 9:40 AM in Manila on Mon, Nov 2, 2026 (01:40 UTC), as in the mockup.
MARKUP_NOW = datetime(2026, 11, 2, 1, 40, tzinfo=dt_timezone.utc)
BUSY_DAY = date(2026, 11, 26)


def _part(html, marker):
    """From the <section> holding marker to the end of that section."""
    start = html.rindex("<section", 0, html.index(marker))
    return html[start : html.index("</section>", start) + len("</section>")]


@mock.patch("django.utils.timezone.now", return_value=MARKUP_NOW)
class DashboardMarkupTests(DashboardV5TestBase):
    """The rendered dashboard at 9:40 AM on Mon, Nov 2, 2026 (Manila)."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin)
        self.checkup = Service.objects.create(
            service_name="Check-up", status=Service.ACTIVE
        )

    def _html(self, url_name="admin_dashboard", **params):
        response = self.client.get(reverse(url_name), params)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def _stats(self, html):
        return _part(html, 'id="dashboard-stats"')

    def _schedule(self, html):
        return _part(html, 'id="day-schedule"')

    def _calendar(self, html):
        return _part(html, 'id="mini-calendar"')

    def _today_button(self, html):
        return re.search(r"<a [^>]*>Today</a>", self._calendar(html)).group(0)

    # Stat cards

    def test_four_stat_cards(self, _now):
        html = self._html()
        stats = self._stats(html)
        self.assertEqual(stats.count("<a "), 4)
        links = {
            "Waiting for approval": reverse("admin_appointments") + "?status=pending",
            "Unpaid bills": reverse("admin_receipt_list") + "?status=pending",
            "Vaccinations due this week": reverse("admin_vaccination_list")
            + "?tab=due",
            "Follow-ups due this week": reverse("admin_medical_record_list")
            + "?tab=followups",
        }
        for label, url in links.items():
            with self.subTest(label=label):
                # The label inside this card's own <a>
                pattern = (
                    rf'<a href="{re.escape(url)}"[^>]*>(?:(?!</a>).)*{label}</span>'
                )
                self.assertRegex(stats, re.compile(pattern, re.S))
        self.assertNotIn("Today's appointments", html)
        self.assertNotIn("pet owner", stats)
        self.assertNotIn(reverse("admin_owner_list"), stats)
        self.assertNotIn(reverse("admin_pet_list"), stats)

    def test_waiting_card_is_yellow_only_with_requests(self, _now):
        def waiting_card():
            stats = self._stats(self._html())
            return re.search(r"<a [^>]*status=pending[^>]*>", stats).group(0)

        self.assertNotIn("border-yellow-400", waiting_card())
        self.assertIn("border-gray-200", waiting_card())
        self._booking(PICKED_DAY, 9, Appointment.PENDING)
        self.assertIn("border-yellow-400 ring-1 ring-yellow-300", waiting_card())

    # Needs approval

    def test_needs_approval_shown_without_requests(self, _now):
        card = _part(self._html(), 'id="needs-approval-title"')
        self.assertIn("Needs approval", card)
        self.assertIn("None waiting", card)
        self.assertIn(
            "No visit requests to review right now. "
            "New requests from pet owners will show up here.",
            card,
        )
        self.assertNotIn("Review", card)
        self.assertNotIn("<a ", card)
        self.assertNotIn("border-yellow-300", card)

    def test_needs_approval_with_one_request(self, _now):
        self._booking(PICKED_DAY, 9, Appointment.PENDING)
        card = _part(self._html(), 'id="needs-approval-title"')
        self.assertIn("1 waiting", card)
        self.assertIn("border-yellow-300", card)
        self.assertIn(">Review</a>", card)
        self.assertNotIn("None waiting", card)

    def test_needs_approval_lists_every_request(self, _now):
        past = self._booking(CAL_TODAY - timedelta(days=2), 9, Appointment.PENDING)
        soon = self._booking(date(2026, 11, 3), 9, Appointment.PENDING, minute=30)
        soon.service = self.checkup
        soon.save()
        later = [
            self._booking(PICKED_DAY + timedelta(days=n), 13, Appointment.PENDING)
            for n in range(5)
        ]
        card = _part(self._html(), 'id="needs-approval-title"')
        self.assertIn("7 waiting", card)
        for appointment in [past, soon, *later]:
            with self.subTest(appointment=appointment.pk):
                url = reverse("admin_appointment_detail", args=[appointment.pk])
                self.assertIn(
                    f'<a href="{url}" class="btn btn-outline shrink-0" '
                    'aria-label="Review Bantay\'s request">Review</a>',
                    card,
                )
        self.assertIn("· Check-up", card)
        self.assertIn("Tomorrow, 9:30 AM", card)
        self.assertNotIn("See all", card)
        # Soonest first, so the past request is the first row
        rows = card.split("<li ")[1:]
        self.assertEqual(len(rows), 7)
        self.assertIn("Date has passed", rows[0])
        for row in rows[1:]:
            self.assertNotIn("Date has passed", row)

    # Schedule

    def test_todays_schedule_marks_the_next_visit(self, _now):
        eight = self._booking(CAL_TODAY, 8, Appointment.COMPLETED)
        eight.is_walk_in = True
        eight.save()
        ten = self._booking(CAL_TODAY, 10, Appointment.CONFIRMED)
        self._booking(CAL_TODAY, 14, Appointment.CONFIRMED)
        schedule = self._schedule(self._html())
        self.assertIn("Today's schedule", schedule)
        self.assertIn("3 visits · 1 done · 2 to go", schedule)
        self.assertIn(f'<a href="{reverse("admin_appointments")}" ', schedule)
        self.assertIn(">All appointments</a>", schedule)
        self.assertNotIn("Back to today", schedule)
        self.assertEqual(schedule.count("<li>"), 3)
        self.assertEqual(schedule.count(">Next<"), 1)
        ten_url = reverse("admin_appointment_detail", args=[ten.pk])
        row = re.search(rf'<a href="{ten_url}".*?</a></li>', schedule, re.S)
        row = row.group(0)
        self.assertIn("bg-violet-50 ring-2 ring-violet-600", row)
        self.assertIn("10:00 AM", row)
        self.assertIn(">Next<", row)
        self.assertIn(">Walk-in<", schedule)

    def test_picked_busy_day_shows_three_rows(self, _now):
        for hour in range(8, 13):
            self._booking(BUSY_DAY, hour, Appointment.CONFIRMED)
        schedule = self._schedule(self._html(day="2026-11-26"))
        self.assertIn("Thursday, November 26", schedule)
        self.assertNotIn("Today's schedule", schedule)
        self.assertIn("5 visits", schedule)
        self.assertEqual(schedule.count("<li>"), 3)
        self.assertIn("and 2 more · ", schedule)
        self.assertIn(
            f'<a href="{reverse("admin_appointments")}?date=2026-11-26"', schedule
        )
        self.assertIn("See all on Appointments", schedule)
        self.assertIn("Back to today", schedule)
        self.assertIn(f'hx-get="{reverse("admin_dashboard_day")}"', schedule)
        self.assertNotIn(">Next<", schedule)

    def test_empty_day(self, _now):
        schedule = self._schedule(self._html(day="2026-11-10"))
        self.assertIn("No visits booked on this day.", schedule)
        self.assertNotIn("<li>", schedule)

    # Calendar

    def test_month_dropdown(self, _now):
        calendar = self._calendar(self._html())
        self.assertEqual(calendar.count("<select"), 1)
        options = re.findall(
            r'<option value="([\d-]+)"( selected)?>([^<]+)</option>', calendar
        )
        self.assertEqual(
            [(value, label) for value, _, label in options],
            [
                ("2026-08", "August 2026"),
                ("2026-09", "September 2026"),
                ("2026-10", "October 2026"),
                ("2026-11", "November 2026"),
                ("2026-12", "December 2026"),
            ],
        )
        self.assertEqual([value for value, sel, _ in options if sel], ["2026-11"])
        self.assertIn('<span class="sr-only">Month</span>', calendar)
        self.assertIn(f'hx-get="{reverse("admin_dashboard_calendar")}"', calendar)
        self.assertIn('hx-include="closest form"', calendar)

    def test_42_cells_and_today(self, _now):
        calendar = self._calendar(self._html())
        grid = calendar[calendar.index("gap-0.5 text-center") :]
        days = re.findall(r'<a href="[^"]*\?month=2026-11&day=[^"]*"', grid)
        blanks = grid.count('<span class="h-12" aria-hidden="true"></span>')
        self.assertEqual(len(days), 30)
        self.assertEqual(len(days) + blanks, 42)
        current = re.findall(r'<a [^>]*aria-current="date"[^>]*>', grid)
        self.assertEqual(len(current), 1)
        self.assertIn("?month=2026-11&day=2026-11-02", current[0])
        self.assertIn("bg-violet-600", current[0])
        self.assertNotIn("ring-inset", grid)

    def test_picked_day_has_the_inside_outline(self, _now):
        calendar = self._calendar(self._html(day="2026-11-05"))
        picked = re.search(r'<a [^>]*day=2026-11-05"[^>]*>', calendar).group(0)
        self.assertIn("ring-2 ring-inset ring-violet-600", picked)
        self.assertIn('?date=2026-11-05" hx-target="#day-schedule"', picked)
        self.assertIn('<input type="hidden" name="day" value="2026-11-05">', calendar)

    def test_calendar_has_no_legend(self, _now):
        self._booking(CAL_TODAY, 9, Appointment.CONFIRMED)
        self._booking(PICKED_DAY, 9, Appointment.PENDING)
        calendar = self._calendar(self._html())
        self.assertNotIn("Has a request waiting", calendar)
        self.assertNotIn("Visits</span>", calendar)
        self.assertNotIn("border-t", calendar)

    def test_today_button_colour(self, _now):
        home = self._today_button(self._html())
        self.assertIn("border-gray-300", home)
        self.assertNotIn("border-violet-600", home)
        self.assertIn(f'href="{reverse("admin_dashboard")}"', home)
        self.assertIn(f'hx-get="{reverse("admin_dashboard_day")}"', home)
        for params in ({"day": "2026-11-05"}, {"month": "2026-12"}):
            with self.subTest(params=params):
                away = self._today_button(self._html(**params))
                self.assertIn("border-violet-600 bg-violet-50 text-violet-800", away)

    def test_day_endpoint_returns_schedule_and_oob_calendar(self, _now):
        html = self._html("admin_dashboard_day", date="2026-11-05")
        self.assertIn('<section id="day-schedule"', html)
        self.assertIn('hx-swap-oob="outerHTML"', self._calendar(html))

    # Recent activity (removed in D5)

    def test_dashboard_has_no_recent_activity(self, _now):
        self._notification(self.admin)
        html = self._html()
        self.assertNotIn("Recent activity", html)
        self.assertNotIn("View all", html)
        button = re.search(r'<button [^>]*id="bell-button".*?</button>', html, re.S)
        self.assertIsNotNone(button)
        self.assertIn("Notifications</span>", button.group(0))
        self.assertRegex(
            button.group(0),
            r'<span id="bell-count" class="rounded-full[^"]*">1 new</span>',
        )

    # Top bar

    def test_bell_count_reads_n_new(self, _now):
        self.assertRegex(
            self._html(), r'<span id="bell-count" class="hidden [^"]*">0 new</span>'
        )
        self._notification(self.admin)
        self._notification(self.admin)
        self.assertRegex(
            self._html(),
            r'<span id="bell-count" class="rounded-full[^"]*">2 new</span>',
        )
