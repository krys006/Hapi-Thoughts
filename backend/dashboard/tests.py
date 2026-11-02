import re
from datetime import date, datetime, time, timedelta
from datetime import timezone as dt_timezone
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.template import engines
from django.template.loader import render_to_string
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from appointments.models import Appointment
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
    month_cells,
    month_day_counts,
    resolve_month,
    resolve_picked,
)
from dashboard.views import _greeting, _parse_day
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

    # Frozen in June so ?date=2026-06-01 is inside the clamped month range.
    @mock.patch("django.utils.timezone.now", return_value=FROZEN_NOW)
    def test_calendar_flags_days_with_pending(self, _now):
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

    def test_corrected_vaccination_is_not_counted(self):
        self._vaccination(is_corrected=True)
        self.assertEqual(self._stats()["vaccinations_due"], 0)
        self._vaccination(is_corrected=False)
        self.assertEqual(self._stats()["vaccinations_due"], 1)

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
        self.assertEqual(response.context["anchor_date"], date(2026, 12, 1))

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
                self.assertEqual(response.context["anchor_date"], expected)

    def test_old_date_param_still_works(self, _now):
        response = self._get(date="2026-10-05")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["cal_month"], date(2026, 10, 1))
        self.assertEqual(response.context["calendar_period_label"], "October 2026")

    def test_picked_param(self, _now):
        response = self._get(month="2026-11", picked="2026-11-05")
        self.assertEqual(response.context["cal_picked"], date(2026, 11, 5))
        self.assertFalse(response.context["cal_is_home"])

    def test_non_admin_htmx_gets_403(self, _now):
        self.client.force_login(self.owner.user)
        response = self.client.get(
            reverse("admin_dashboard_calendar"),
            {"month": "2026-12"},
            headers={"HX-Request": "true"},
        )
        self.assertEqual(response.status_code, 403)
