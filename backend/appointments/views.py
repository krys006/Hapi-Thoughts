from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.exceptions import ObjectDoesNotExist
from django.core.paginator import Paginator
from django.db import transaction
from django.http import HttpResponse
from django.urls import reverse
from django.utils import timezone
from django.utils.cache import patch_vary_headers
from django.utils.dateformat import format as date_format

from .models import BlockedDate, Appointment
from .forms import (
    ClinicSettingsForm,
    BlockedDateForm,
    OwnerBookingStepOneForm,
    AdminAppointmentNotesForm,
    AdminAppointmentCancelForm,
    AdminAppointmentRescheduleForm,
    AdminWalkInAppointmentForm,
)

from .utils import (
    CANNOT_CHANGE_MESSAGE,
    LIST_PAGE_SIZE,
    RESCHEDULE_SENT_MESSAGE,
    SETTINGS_TAB_FIELDS,
    SLOT_NO_LONGER_AVAILABLE,
    WALKIN_PAGE_SIZE,
    WALKIN_SEARCH_LIMIT,
    SlotTaken,
    apply_picked_date,
    bookable_services,
    booking_cancel_url,
    booking_data,
    booking_problem,
    booking_summary,
    booking_time_context,
    cancel_by_owner,
    clear_booking_draft,
    clinic_hours_line,
    create_owner_booking,
    day_area_redirect,
    create_walkin_appointment,
    filter_appointments,
    first_error_tab,
    get_admin_calendar_context,
    get_available_slots,
    get_booking_draft,
    get_clinic_settings,
    get_walkin_owner,
    group_by_day,
    is_owner_results_swap,
    is_slot_available,
    list_links,
    list_period,
    list_tabs,
    lock_schedule,
    notify_admin_of_request,
    notify_request_declined,
    owner_booking_pets,
    owner_can_change,
    owner_clinic_info_context,
    owner_reschedule_context,
    owner_visit_detail_context,
    owner_visits_context,
    paging_links,
    pick_booking_time,
    problems_label,
    render_day_area,
    reschedule_attempt,
    resolve_list_tab,
    save_booking_draft,
    schedule_warning,
    search_walkin_owners,
    settings_tab,
    settings_tab_errors,
    slot_in_progress,
    start_booking_draft,
    step_one_initial,
    tab_counts,
    time_label,
    unfinished_visits,
    visit_receipt_state,
    visits_on_date,
    walkin_initial,
    walkin_owner_list,
    walkin_slot_context,
    walkin_values,
)

from accounts.utils import owner_profile_required
from billing.utils import BILL_ARCHIVED, BILL_STARTED, start_visit_bill
from notifications.utils import compact_message, notify, when_line
from dashboard.templatetags.friendly_dates import friendly_date

import datetime


# ── Admin — Clinic Settings ───────────────────────────────────────────────────
@login_required
def admin_clinic_settings(request):
    """
    Admin view — edit clinic schedule configuration and contact details.
    Creates the single ClinicSettings row if it doesn't exist yet.
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    settings_obj = get_clinic_settings(create=True)
    warning = None

    if request.method == "POST":
        done, form, active_tab, warning = _save_settings(request, settings_obj)
        if done:
            return done
    else:
        active_tab = settings_tab(request.GET.get("tab"))
        initial = {"working_days": settings_obj.working_days}
        form = ClinicSettingsForm(instance=settings_obj, initial=initial)

    tab_errors = settings_tab_errors(form)
    # A failed form changes form.instance; the banner shows what is saved
    saved_settings = get_clinic_settings()
    return render(
        request,
        "admin/settings/clinic_settings.html",
        {
            "form": form,
            "blocked_dates": BlockedDate.objects.all(),
            "blocked_form": BlockedDateForm(),
            "settings_obj": saved_settings,
            "saved_settings": saved_settings,
            "hours_line": clinic_hours_line(saved_settings),
            "active_tab": active_tab,
            "tab_errors": tab_errors,
            "tab_problems": {
                tab: problems_label(count) for tab, count in tab_errors.items() if count
            },
            "schedule_warning": warning,
        },
    )


def _save_settings(request, settings_obj):
    """
    Handle a settings POST. A posted tab (details, schedule or vet) validates
    and saves only that tab's fields; no tab means every field (the old page).
    Returns (redirect or None, form, active tab, schedule warning or None).
    A valid change to the hours, days, slot length or cutoff with visits
    booked saves nothing until confirm_schedule=1; go_back=1 saves nothing.
    """
    posted = request.POST.get("tab")
    tab = posted if posted in SETTINGS_TAB_FIELDS else None
    form = ClinicSettingsForm(
        request.POST, request.FILES, instance=settings_obj, tab=tab
    )
    if request.POST.get("go_back"):
        return None, form, tab or "schedule", None
    if not form.is_valid():
        messages.error(request, "Please correct the errors below.")
        return None, form, tab or first_error_tab(settings_tab_errors(form)), None
    if not request.POST.get("confirm_schedule"):
        warning = schedule_warning(form)
        if warning:
            return None, form, tab or "schedule", warning
    form.save()
    messages.success(request, "Clinic settings updated successfully.")
    url = f"{reverse('admin_clinic_settings')}?tab={tab or 'details'}"
    return redirect(url), form, tab, None


def _blocked_tab_url():
    """The clinic settings page with the Blocked dates tab open (?tab=blocked;
    the other tabs are details, schedule and vet)."""
    return f"{reverse('admin_clinic_settings')}?tab=blocked"


def _blocked_panel(request, form=None, busy_visits=None):
    """
    Only the blocked-dates panel (add form + list), never the whole page.
    With busy_visits the busy-day warning shows instead of the Block button,
    and the date and reason are locked so "Block anyway" confirms exactly
    the day that was checked.
    """
    if form is None:
        form = BlockedDateForm(initial=request.GET.dict())
    if busy_visits:
        for field in ("date", "reason"):
            form.fields[field].widget.attrs["readonly"] = True
    return render(
        request,
        "admin/settings/_blocked_dates_panel.html",
        {
            "blocked_dates": BlockedDate.objects.all(),
            "blocked_form": form,
            "busy_visits": busy_visits,
            "busy_date": form.cleaned_data["date"] if busy_visits else None,
        },
    )


def _blocked_done(request, message):
    """
    Success: flash the message and show the full settings page, so the flash
    appears there. HTMX gets an HX-Redirect header; other requests a redirect.
    """
    messages.success(request, message)
    if request.headers.get("HX-Request"):
        return HttpResponse(headers={"HX-Redirect": _blocked_tab_url()})
    return redirect(_blocked_tab_url())


@login_required
def admin_blocked_date_add(request):
    """
    Admin view — add a blocked date. A form error, or a day that already has
    CONFIRMED / PENDING visits (until confirm_busy=1 is posted), returns only
    the panel. The visits are never changed. GET returns the panel with
    ?date= and ?reason= filled in (the warning's "Go back").
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    if request.method != "POST":
        return _blocked_panel(request)

    form = BlockedDateForm(request.POST)
    if not form.is_valid():
        return _blocked_panel(request, form)

    visits = list(visits_on_date(form.cleaned_data["date"]))
    if visits and request.POST.get("confirm_busy") != "1":
        return _blocked_panel(request, form, busy_visits=visits)

    blocked = form.save()
    return _blocked_done(
        request, f"{date_format(blocked.date, 'D, M j, Y')} is now blocked."
    )


@login_required
def admin_blocked_date_delete(request, pk):
    """Admin view — remove a blocked date (a hard delete: it is a schedule
    setting, not a record). POST only; anything else goes back to the tab."""
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    blocked_date = get_object_or_404(BlockedDate, pk=pk)
    if request.method != "POST":
        return redirect(_blocked_tab_url())

    blocked_date.delete()
    return _blocked_done(
        request, f"{date_format(blocked_date.date, 'D, M j, Y')} is open again."
    )


# ── Shared — Clinic Info & About Page ──────────────────────────────────────


@login_required
def clinic_info(request):
    """
    Clinic info & about page — visible to both Admin and Pet Owner.
    Shows clinic identity, veterinarian profile, and operating hours.
    Google Maps embed is built dynamically from the address field using
    Google's keyless embed pattern — no API key, no cost.
    """
    from urllib.parse import quote

    clinic = get_clinic_settings()

    maps_embed_url = ""
    if clinic and clinic.address:
        maps_embed_url = (
            f"https://www.google.com/maps?q={quote(clinic.address)}&output=embed"
        )

    context = {
        "clinic": clinic,
        "maps_embed_url": maps_embed_url,
    }
    if request.user.is_admin:
        # Admins get their own page on the admin layout (Tailwind, no legacy CSS)
        context["hours_line"] = clinic_hours_line(clinic) if clinic else ""
        return render(request, "admin/clinic_info.html", context)

    # Owners: the owner layout, plain text and links (no map, no script)
    context = owner_clinic_info_context(clinic, timezone.localdate())
    return render(request, "owner/clinic/info.html", context)


# ── Pet Owner — Appointments ──────────────────────────────────────────────────


@login_required
@owner_profile_required
def owner_appointment_list(request):
    """
    Pet Owner view — the Visits tab: Upcoming / Past (?show=), a pet filter
    (?pet=) and 25 past visits a page (?page=). The clock is read once.
    """
    if request.user.role != "pet_owner":
        return redirect("admin_dashboard")

    now = timezone.now()
    context = owner_visits_context(
        request.owner, request.GET, timezone.localdate(now), now
    )
    return render(request, "owner/visits/list.html", context)


@login_required
@owner_profile_required
def owner_book_appointment(request):
    """
    Pet Owner view — booking step 1, "Pet and reason". A plain GET starts a
    fresh session draft (keeping ?pet=); ?back=1 keeps the draft. POST checks
    the answers, stores them in the draft and goes to step 2. Nothing is
    saved to the database before Send (step 3).
    """
    if request.user.role != "pet_owner":
        return redirect("admin_dashboard")

    owner = request.owner
    draft = get_booking_draft(request.session)
    pets, services = owner_booking_pets(owner), bookable_services()
    if request.method == "POST":
        form = OwnerBookingStepOneForm(owner, services, request.POST)
        if form.is_valid():
            draft = draft or {}
            data = form.cleaned_data
            draft.update(
                pet=data["pet"].pk, service=data["service"], note=data["reason"]
            )
            save_booking_draft(request.session, draft)
            return redirect("owner_book_time")
    else:
        if request.GET.get("back") != "1" or draft is None:
            draft = start_booking_draft(request.session, owner, request.GET.get("pet"))
        form = OwnerBookingStepOneForm(
            owner, services, initial=step_one_initial(pets, draft)
        )

    return render(
        request,
        "owner/booking/step1.html",
        {
            "form": form,
            "pets": pets,
            "services": services,
            "selected_pet": str(form["pet"].value() or ""),
            "selected_service": str(form["service"].value() or ""),
            "cancel_url": booking_cancel_url(draft),
        },
    )


def _booking_back(request, draft, problem):
    """Back to the step a draft problem names, with a plain flash, or with
    the "was just taken" box (the time cleared, the day kept)."""
    step, message, taken = problem
    if taken:
        draft["taken"] = taken
        draft.pop("time", None)
        save_booking_draft(request.session, draft)
    elif message:
        messages.warning(request, message)
    if step == 1:
        return day_area_redirect(request, f"{reverse('owner_book_appointment')}?back=1")
    return day_area_redirect(request, reverse("owner_book_time"))


@login_required
@owner_profile_required
def owner_book_time(request):
    """
    Pet Owner view — booking step 2, "Date and time", without JavaScript:
    ?month= and ?date= are plain links, the times are radio cards in a POST
    form. Every visit checks the draft again; a problem goes back to the
    step that needs it.
    """
    if request.user.role != "pet_owner":
        return redirect("admin_dashboard")

    owner = request.owner
    now = timezone.localtime()  # the one read of the clock
    today = now.date()
    draft = get_booking_draft(request.session)
    problem = booking_problem(owner, draft, 1, today, now)
    if problem:
        return _booking_back(request, draft, problem)

    clinic = get_clinic_settings()  # read once, passed to every helper below
    if request.method == "POST":
        errors = pick_booking_time(draft, request.POST, today, now, clinic)
        if not errors:
            save_booking_draft(request.session, draft)
            return redirect("owner_book_check")
    else:
        date = request.GET.get("date")
        errors = {"date": apply_picked_date(draft, date, today, now, clinic)}
    save_booking_draft(request.session, draft)

    month = request.GET.get("month")
    context = booking_time_context(draft, month, errors, today, now, clinic)
    context.update(booking_summary(owner, draft))
    context["back_url"] = f"{reverse('owner_book_appointment')}?back=1"
    context["cancel_url"] = booking_cancel_url(draft)
    return render_day_area(request, "owner/booking/step2.html", context)


@login_required
@owner_profile_required
def owner_book_check(request):
    """
    Pet Owner view — booking step 3, "Check and send". GET shows the whole
    request. POST sends it through the locked create (create_owner_booking):
    a PENDING visit, the draft cleared, then the "Request sent" page. A POST
    with no draft (a second tap) creates nothing.
    """
    if request.user.role != "pet_owner":
        return redirect("admin_dashboard")

    owner = request.owner
    now = timezone.localtime()  # the one read of the clock
    draft = get_booking_draft(request.session)
    if request.method == "POST" and draft is None:
        messages.info(request, "Your request was already sent. It's in your visits.")
        return redirect("owner_appointments")
    problem = booking_problem(owner, draft, 2, now.date(), now, get_clinic_settings())
    if problem:
        return _booking_back(request, draft, problem)
    if request.method == "POST":
        return _send_booking(request, owner, draft)

    context = booking_summary(owner, draft)
    context["back_url"] = f"{reverse('owner_book_time')}?back=1"
    context["cancel_url"] = booking_cancel_url(draft)
    return render(request, "owner/booking/step3.html", context)


def _send_booking(request, owner, draft):
    """Saves the draft as a PENDING visit under the schedule lock. A slot
    taken meanwhile goes back to step 2 with the red box, nothing saved."""
    data = booking_data(owner, draft)
    try:
        appointment = create_owner_booking(owner, data)
    except SlotTaken:
        return _booking_back(request, draft, (2, "", time_label(data["time"])))
    # After the transaction has committed, never inside it
    notify_admin_of_request(appointment)
    clear_booking_draft(request.session)
    return redirect("owner_book_sent", pk=appointment.pk)


@login_required
@owner_profile_required
def owner_book_sent(request, pk):
    """Pet Owner view — "Request sent": the owner's own visit only (404 for
    anyone else's), with its status as it is now."""
    if request.user.role != "pet_owner":
        return redirect("admin_dashboard")

    appointment = get_object_or_404(
        Appointment.objects.select_related("pet", "service"),
        pk=pk,
        owner=request.owner,
    )
    return render(request, "owner/booking/sent.html", {"appointment": appointment})


@login_required
@owner_profile_required
def owner_appointment_detail(request, pk):
    """
    Pet Owner view — one visit, in any status, with the cancel sheet.
    Owners can only view their own visits (404 for anyone else's).
    """
    if request.user.role != "pet_owner":
        return redirect("admin_dashboard")

    now = timezone.now()
    appointment = get_object_or_404(
        Appointment.objects.select_related(
            "pet", "service", "cancelled_by", "rescheduled_from__cancelled_by"
        ),
        pk=pk,
        owner=request.owner,
    )
    context = owner_visit_detail_context(
        request.owner, appointment, timezone.localdate(now), now
    )
    return render(request, "owner/visits/detail.html", context)


@login_required
@owner_profile_required
def owner_appointment_cancel(request, pk):
    """
    Pet Owner view — the cancel sheet's POST (a GET goes back to the visit).
    Only a Pending or Confirmed visit, with a reason (Other needs a detail);
    cancel_by_owner does the work. Every answer goes back to the visit page
    with a flash; the late note is only on the sheet, the cancel still works.
    """
    if request.user.role != "pet_owner":
        return redirect("admin_dashboard")

    appointment = get_object_or_404(
        Appointment.objects.select_related("pet", "owner"),
        pk=pk,
        owner=request.owner,
    )
    if request.method == "POST":
        ok, text = cancel_by_owner(request.user, appointment, request.POST)
        (messages.success if ok else messages.error)(request, text)
    return redirect("owner_appointment_detail", pk=pk)


@login_required
@owner_profile_required
def owner_appointment_reschedule(request, pk):
    """
    Pet Owner view — Change day or time, one page. A GET shows the calendar
    (?date= picks the day, ?month= the month) and, for that day, the times,
    the "Why" box and Send. A POST sends the new time: reschedule_attempt
    (the locked reschedule_by_owner) cancels the original and makes a new
    PENDING visit; the page comes back with the problem when there is one.
    Only the owner's own Pending / Confirmed visit whose slot hasn't started.
    """
    if request.user.role != "pet_owner":
        return redirect("admin_dashboard")

    now = timezone.now()  # the one read of the clock
    today = timezone.localdate(now)
    appointment = get_object_or_404(
        Appointment.objects.select_related("pet", "service"),
        pk=pk,
        owner=request.owner,
    )
    if not owner_can_change(appointment, now):
        messages.error(request, CANNOT_CHANGE_MESSAGE)
        return day_area_redirect(
            request, reverse("owner_appointment_detail", args=[pk])
        )

    params, errors = request.GET, None
    if request.method == "POST":
        params = request.POST
        kind, value = reschedule_attempt(
            request.user, request.owner, appointment, params, today, now
        )
        if kind == "gone":
            messages.error(request, CANNOT_CHANGE_MESSAGE)
            return redirect("owner_appointment_detail", pk=pk)
        if kind == "sent":
            # After the transaction has committed, never inside it
            notify_admin_of_request(value, reschedule=True)
            messages.success(request, RESCHEDULE_SENT_MESSAGE)
            return redirect("owner_appointment_detail", pk=value.pk)
        errors = value
    context = owner_reschedule_context(
        request.owner, appointment, params, today, now, errors
    )
    return render_day_area(request, "owner/visits/reschedule.html", context)


# ── Admin — Appointments ──────────────────────────────────────────────────────

# Longest clinic message kept when declining a request (longer is trimmed).
REJECT_MESSAGE_MAX_LENGTH = 500


def _when(appointment):
    """'Mon, Nov 2 at 2:00 PM' / 'today at 9:00 AM', for use mid-sentence
    in messages."""
    time = date_format(appointment.time, "g:i A")
    return f"{friendly_date(appointment.date, 'mid')} at {time}"


def _too_early_message(appointment):
    """Error for completing / no-showing an appointment before its date."""
    return (
        "You can mark this after the appointment date "
        f"({friendly_date(appointment.date, 'mid')})."
    )


def _notification_when(appointment, today):
    """'Mon, Nov 2 at 2:00 PM' (the year when not this year): an absolute
    date, since an email may be read on another day."""
    return when_line(appointment.date, appointment.time, today)


def _notify_owner(
    appointment, notification_type, title, message, email_subject, email_body=None
):
    """In-app notification to the appointment's owner (compact title and
    message), plus email per their preferences with email_body (the full
    sentence; see notifications.utils.notify)."""
    notify(
        recipient=appointment.owner.user,
        notification_type=notification_type,
        title=title,
        message=message,
        related_appointment=appointment,
        related_pet=appointment.pet,
        email_subject=email_subject,
        email_body=email_body,
    )


def _related_or_none(obj, name):
    """A reverse one-to-one (e.g. appointment.medical_record), or None when
    there isn't one yet."""
    try:
        return getattr(obj, name)
    except ObjectDoesNotExist:
        return None


def _open_dialog(cancel_form, reschedule_form):
    """Which dialog the detail page reopens after a form error, or None."""
    if reschedule_form.is_bound and reschedule_form.errors:
        return "move"
    if cancel_form.is_bound and cancel_form.errors:
        return "cancel"
    return None


def _reschedule_slot_context(form):
    """
    Free times for the submitted date and the time the admin picked, so a
    bound reschedule form can be shown again. reschedule_slots is None when
    the form isn't bound or its date isn't valid (nothing to show yet).
    """
    if not form.is_bound:
        return {"reschedule_slots": None, "reschedule_date": None, "selected_time": ""}
    # Checking form.errors first runs validation, which fills cleaned_data
    valid_date = None if "date" in form.errors else form.cleaned_data.get("date")
    return {
        "reschedule_slots": (
            get_available_slots(valid_date, move=True) if valid_date else None
        ),
        "reschedule_date": valid_date,
        "selected_time": form.data.get("time", ""),
    }


def _admin_detail_context(
    appointment, notes_form=None, cancel_form=None, reschedule_form=None
):
    """
    Context for admin/appointments/detail.html. Pass a bound form to
    re-render the page with that form's errors and typed values.
    """
    if notes_form is None:
        notes_form = AdminAppointmentNotesForm(initial={"notes": appointment.notes})
    if cancel_form is None:
        cancel_form = AdminAppointmentCancelForm()
    if reschedule_form is None:
        reschedule_form = AdminAppointmentRescheduleForm(appointment)

    today = timezone.localdate()
    cancelled_by = appointment.cancelled_by
    receipt = _related_or_none(appointment, "billing_receipt")
    context = {
        "appointment": appointment,
        "notes_form": notes_form,
        "cancel_form": cancel_form,
        "reschedule_form": reschedule_form,
        "date_has_passed": appointment.date < today,
        "can_mark_visit": (
            appointment.status == Appointment.CONFIRMED and appointment.date <= today
        ),
        "owner_has_email": bool(appointment.owner.user.email),
        "cancelled_by_owner": cancelled_by is not None and cancelled_by.is_pet_owner,
        # At most one: a moved appointment is cancelled, so it can't move again
        "moved_to": appointment.rescheduled_to.first(),
        "medical_record": _related_or_none(appointment, "medical_record"),
        "receipt": receipt,
        # None / "unpaid" / "paid" / "cancelled" (banner and details row)
        "receipt_state": visit_receipt_state(receipt),
        # The Complete dialog says a bill will be started (no bill yet, and
        # neither the owner nor the pet is archived)
        "bill_will_start": (
            receipt is None
            and not appointment.owner.is_archived
            and not appointment.pet.is_archived
        ),
        "open_dialog": _open_dialog(cancel_form, reschedule_form),
    }
    context.update(_reschedule_slot_context(reschedule_form))
    return context


def _render_detail(request, appointment, **forms):
    """Renders the admin detail page; pass a bound form to show its errors."""
    return render(
        request,
        "admin/appointments/detail.html",
        _admin_detail_context(appointment, **forms),
    )


def _reschedule_appointment(appointment, form, user):
    """
    Cancels the original and creates the new appointment as CONFIRMED
    (the clinic chose the slot). Runs in one transaction: if creating the
    new appointment fails, the original stays as it was. The slot is
    checked again under lock_schedule(); SlotTaken when it went meanwhile
    (nothing changed). Returns the new appointment.
    """
    with transaction.atomic():
        lock_schedule()
        if not is_slot_available(
            form.cleaned_data["date"],
            form.cleaned_data["time"],
            exclude_appointment_pk=appointment.pk,
            move=True,
        ):
            raise SlotTaken(form.cleaned_data["time"])
        appointment.status = Appointment.CANCELLED
        appointment.cancellation_reason = Appointment.OTHER
        appointment.cancellation_detail = "Rescheduled by clinic."
        appointment.cancelled_by = user
        appointment.save()

        return Appointment.objects.create(
            owner=appointment.owner,
            pet=appointment.pet,
            service=appointment.service,
            date=form.cleaned_data["date"],
            time=form.cleaned_data["time"],
            reason=appointment.reason,
            reschedule_reason=form.cleaned_data["reschedule_reason"],
            rescheduled_from=appointment,
            status=Appointment.CONFIRMED,
        )


@login_required
def admin_appointment_list(request):
    """
    Admin view — appointments list with status tabs, period, search and paging.
    A search (?q=) looks in every status and shows the "All" tab. The period
    (?show=&from=, see resolve_period) narrows whichever tab is open.
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    today = timezone.localdate()
    q = request.GET.get("q", "").strip()
    status = request.GET.get("status", "")
    period, is_old_date_link = list_period(request.GET, today)
    counts = tab_counts(today)
    # An old ?date= link without a status meant every status on that day
    show_all = q or (is_old_date_link and not status)
    active_tab = "all" if show_all else resolve_list_tab(status, counts)

    rows = filter_appointments(active_tab, today, q, period)
    page_obj = Paginator(rows, LIST_PAGE_SIZE).get_page(request.GET.get("page"))

    links = list_links(active_tab, status, q, period)
    context = {
        "active_tab": active_tab,
        "tabs": list_tabs(counts),
        "day_groups": group_by_day(page_obj.object_list),
        "page_obj": page_obj,
        **links,
        **paging_links(page_obj, links["page_query"]),
        "q": q,
        "period": period,
        "is_searching": bool(q),
        **unfinished_visits(today, counts["unfinished"]),
        # The yellow notice belongs on the work tabs, not on search results
        "show_unfinished_notice": (
            counts["unfinished"] > 0 and not q and active_tab in ("pending", "upcoming")
        ),
        # Day cards (today's is outlined) and the "Date has passed" tag
        "today": today,
    }
    return render(request, "admin/appointments/list.html", context)


@login_required
def admin_appointment_detail(request, pk):
    """
    Admin view — view full appointment details.
    All actions (approve, reject, complete, etc.) POST to separate views.
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    appointment = get_object_or_404(
        Appointment.objects.select_related(
            "owner__user",
            "pet",
            "service",
            "cancelled_by",
            "medical_record",
            "billing_receipt",
        ),
        pk=pk,
    )

    return _render_detail(request, appointment)


@login_required
def admin_appointment_approve(request, pk):
    """Admin action — approve a PENDING appointment → CONFIRMED."""
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    appointment = get_object_or_404(Appointment, pk=pk)

    if request.method == "POST":
        today = timezone.localdate()  # one read of the clock
        if appointment.status != Appointment.PENDING:
            messages.error(request, "Only pending appointments can be approved.")
        elif appointment.date < today:
            messages.error(
                request,
                f"This request was for {date_format(appointment.date, 'D, M j')}, "
                "which has passed. Move it to another time or decline it.",
            )
        else:
            appointment.status = Appointment.CONFIRMED
            appointment.save()

            # Notify pet owner of approval (compact text; the email keeps
            # the full sentence)
            when = _notification_when(appointment, today)
            _notify_owner(
                appointment,
                "appointment_approved",
                "Visit confirmed",
                compact_message(appointment.pet.name, when),
                "Appointment Confirmed — Hapi Vet",
                email_body=(
                    f"Your appointment for {appointment.pet.name} on {when} "
                    "has been confirmed."
                ),
            )

            messages.success(
                request,
                f"Confirmed {appointment.pet.name} for {_when(appointment)}. "
                "The owner has been notified.",
            )

    return redirect("admin_appointment_detail", pk=pk)


@login_required
def admin_appointment_reject(request, pk):
    """
    Admin action — decline a PENDING appointment → CANCELLED.
    An optional POST "message" is stored as the cancellation detail and
    sent to the owner.
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    appointment = get_object_or_404(Appointment, pk=pk)

    if request.method == "POST":
        if appointment.status != Appointment.PENDING:
            messages.error(request, "Only pending appointments can be rejected.")
        else:
            clinic_message = request.POST.get("message", "").strip()
            clinic_message = clinic_message[:REJECT_MESSAGE_MAX_LENGTH]

            appointment.status = Appointment.CANCELLED
            appointment.cancellation_reason = Appointment.DECLINED
            appointment.cancellation_detail = clinic_message or "Rejected by clinic."
            appointment.cancelled_by = request.user
            appointment.save()

            notify_request_declined(appointment, clinic_message, timezone.localdate())

            messages.success(
                request,
                f"Declined {appointment.pet.name}'s request for "
                f"{_when(appointment)}. The owner has been notified.",
            )

    return redirect("admin_appointment_detail", pk=pk)


def _completed_message(appointment, outcome, name):
    """The success flash of Complete, by what happened to the visit's bill."""
    if outcome == BILL_STARTED:
        return "Visit marked as completed. A bill was started for it."
    if outcome == BILL_ARCHIVED:
        return (
            "Visit marked as completed. "
            f"No bill was started because {name} is archived."
        )
    return (
        f"Marked {appointment.pet.name}'s appointment on "
        f"{_when(appointment)} as completed."
    )


@login_required
def admin_appointment_complete(request, pk):
    """
    Admin action — mark a CONFIRMED appointment as COMPLETED, on or after
    the appointment date, and start the visit's bill (start_visit_bill) in
    the same transaction.
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    appointment = get_object_or_404(
        Appointment.objects.select_related("owner", "pet", "service"), pk=pk
    )

    if request.method == "POST":
        if appointment.status != Appointment.CONFIRMED:
            messages.error(
                request, "Only confirmed appointments can be marked as completed."
            )
        elif appointment.date > timezone.localdate():
            messages.error(request, _too_early_message(appointment))
        else:
            with transaction.atomic():
                appointment.status = Appointment.COMPLETED
                appointment.save()
                outcome, name = start_visit_bill(appointment)
            messages.success(request, _completed_message(appointment, outcome, name))

    return redirect("admin_appointment_detail", pk=pk)


@login_required
def admin_appointment_no_show(request, pk):
    """
    Admin action — mark a CONFIRMED appointment as NO_SHOW, on or after
    the appointment date.
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    appointment = get_object_or_404(Appointment, pk=pk)

    if request.method == "POST":
        if appointment.status != Appointment.CONFIRMED:
            messages.error(
                request, "Only confirmed appointments can be marked as no-show."
            )
        elif appointment.date > timezone.localdate():
            messages.error(request, _too_early_message(appointment))
        else:
            appointment.status = Appointment.NO_SHOW
            appointment.save()
            messages.success(
                request,
                f"Marked {appointment.pet.name}'s appointment on "
                f"{_when(appointment)} as a no-show.",
            )

    return redirect("admin_appointment_detail", pk=pk)


@login_required
def admin_appointment_notes(request, pk):
    """Admin action — save notes on an appointment."""
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    appointment = get_object_or_404(Appointment, pk=pk)

    if request.method == "POST":
        form = AdminAppointmentNotesForm(request.POST)
        if form.is_valid():
            appointment.notes = form.cleaned_data["notes"]
            appointment.save(update_fields=["notes", "updated_at"])
            messages.success(request, "Notes saved.")
        else:
            messages.error(request, "Could not save notes.")

    return redirect("admin_appointment_detail", pk=pk)


@login_required
def admin_appointment_cancel(request, pk):
    """
    Admin action — cancel a PENDING or CONFIRMED appointment for the owner.
    Invalid input re-renders the detail page with the form's errors.
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    appointment = get_object_or_404(Appointment, pk=pk)

    if request.method == "POST":
        if appointment.status not in [Appointment.PENDING, Appointment.CONFIRMED]:
            messages.error(request, "This appointment cannot be cancelled.")
            return redirect("admin_appointment_detail", pk=pk)

        form = AdminAppointmentCancelForm(request.POST)
        if not form.is_valid():
            messages.error(request, "Please correct the errors below.")
            return _render_detail(request, appointment, cancel_form=form)

        appointment.status = Appointment.CANCELLED
        appointment.cancellation_reason = form.cleaned_data["cancellation_reason"]
        appointment.cancellation_detail = form.cleaned_data.get(
            "cancellation_detail", ""
        )
        appointment.cancelled_by = request.user
        appointment.save()

        _notify_cancelled(appointment, timezone.localdate())

        messages.success(
            request,
            f"Cancelled {appointment.pet.name}'s appointment on "
            f"{_when(appointment)}. The owner has been notified.",
        )

    return redirect("admin_appointment_detail", pk=pk)


def _notify_cancelled(appointment, today):
    """Tells the owner the clinic cancelled their visit."""
    when = _notification_when(appointment, today)
    _notify_owner(
        appointment,
        "appointment_cancelled",
        "The clinic cancelled a visit",
        compact_message(
            appointment.pet.name, f"{when}. Please call us for a new time."
        ),
        "Appointment Cancelled — Hapi Vet",
        email_body=(
            f"Your appointment for {appointment.pet.name} on {when} "
            "has been cancelled by the clinic. "
            "Please contact the clinic for more information."
        ),
    )


def _notify_moved(new_appointment, today):
    """Tells the owner their visit was moved by the clinic. Absolute date,
    since the email may be read on another day."""
    when = _notification_when(new_appointment, today)
    _notify_owner(
        new_appointment,
        "appointment_approved",
        "The clinic moved your visit",
        compact_message(
            new_appointment.pet.name, f"now {when}. The new time is confirmed."
        ),
        "Appointment moved — Hapi Vet",
        email_body=(
            f"Your appointment for {new_appointment.pet.name} was moved to "
            f"{when} by the clinic. The new time is confirmed."
        ),
    )


@login_required
def admin_appointment_reschedule(request, pk):
    """
    Admin action — reschedule a PENDING or CONFIRMED appointment for the
    owner. The new appointment is CONFIRMED, since the clinic chose the
    slot (see _reschedule_appointment).
    Invalid input re-renders the detail page with the form's errors.
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    appointment = get_object_or_404(Appointment, pk=pk)

    if request.method == "POST":
        if appointment.status not in [Appointment.PENDING, Appointment.CONFIRMED]:
            messages.error(request, "This appointment cannot be rescheduled.")
            return redirect("admin_appointment_detail", pk=pk)

        form = AdminAppointmentRescheduleForm(appointment, request.POST)
        if not form.is_valid():
            messages.error(request, "Please correct the errors below.")
            return _render_detail(request, appointment, reschedule_form=form)

        try:
            new_appointment = _reschedule_appointment(appointment, form, request.user)
        except SlotTaken:
            # Taken since the form was checked: the same error the form gives
            form.add_error(None, SLOT_NO_LONGER_AVAILABLE)
            messages.error(request, "Please correct the errors below.")
            return _render_detail(request, appointment, reschedule_form=form)

        _notify_moved(new_appointment, timezone.localdate())
        messages.success(
            request,
            f"Rescheduled {new_appointment.pet.name} to "
            f"{_when(new_appointment)}. The owner has been notified.",
        )
        return redirect("admin_appointment_detail", pk=new_appointment.pk)

    return redirect("admin_appointment_detail", pk=pk)


def _walk_in_booked_message(appointment):
    """'Walk-in booked for Max today at 10:00 AM.' / '… on Mon, Nov 2 at …'."""
    days_away = abs((appointment.date - timezone.localdate()).days)
    on = "" if days_away <= 1 else "on "  # today/tomorrow/yesterday need no "on"
    return f"Walk-in booked for {appointment.pet.name} {on}{_when(appointment)}."


@login_required
def admin_walkin_appointment_create(request):
    """
    Admin view — walk-in step 1, "Who is it for?". With no ?q= every
    non-archived owner is listed (first name A to Z, WALKIN_PAGE_SIZE per
    page); with ?q= the search results (no pager). Each owner links to
    step 2 (admin_walkin_appointment_visit). The search box filters as you
    type: those HTMX requests get only _walkin_owner_results.html.
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    q = request.GET.get("q", "").strip()
    context = {"q": q, "search_limit": WALKIN_SEARCH_LIMIT, "page_obj": None}
    if q:
        context["owners"], context["is_cut"] = search_walkin_owners(q)
    else:
        paginator = Paginator(walkin_owner_list(), WALKIN_PAGE_SIZE)
        page_obj = paginator.get_page(request.GET.get("page"))
        context.update(
            owners=page_obj.object_list,
            owner_count=paginator.count,
            page_obj=page_obj,
            **paging_links(page_obj, ""),
        )

    template = (
        "admin/appointments/_walkin_owner_results.html"
        if is_owner_results_swap(request)
        else "admin/appointments/walkin_find.html"
    )
    response = render(request, template, context)
    # The same URL answers with the partial or the full page
    patch_vary_headers(response, ["HX-Request"])
    return response


def _taken_time(time_value):
    """The time that was just booked by someone else, for the step 2 banner
    ("10:00 AM was just taken.")."""
    try:
        return date_format(datetime.time.fromisoformat(time_value), "g:i A")
    except ValueError:
        return "That time"


def _render_walkin_visit(request, owner, form, taken_time=""):
    """Step 2 page (the visit); pass a bound form to show its errors."""
    context = {
        "owner": owner,
        "form": form,
        "pets": form.fields["pet"].queryset,
        "selected_pet": str(form["pet"].value() or ""),
        "taken_time": taken_time,
    }
    context.update(walkin_slot_context(form))
    return render(request, "admin/appointments/walkin_visit.html", context)


def _walkin_slot_taken(request, owner):
    """Step 2 again after the chosen time was taken: every value kept except
    the time, which must be picked again, with the "was just taken" banner."""
    initial = walkin_values(request.POST, keep_time=False)
    taken = _taken_time(request.POST.get("time", ""))
    return _render_walkin_visit(
        request, owner, AdminWalkInAppointmentForm(owner, initial=initial), taken
    )


def _render_walkin_review(request, owner, form):
    """Step 3 page (check and book): a summary, nothing saved yet."""
    data = form.cleaned_data
    return render(
        request,
        "admin/appointments/walkin_review.html",
        {
            "owner": owner,
            "pet": data["pet"],
            "date": data["date"],
            "time": data["time"],
            "service": data.get("service"),
            "reason": data.get("reason", ""),
            "owner_has_email": bool(owner.user.email),
        },
    )


@login_required
def admin_walkin_appointment_visit(request):
    """
    Admin view — walk-in steps 2 and 3 (?owner=<pk>[&pet=<pk>]).
    GET shows step 2. POST "action" picks what happens: "review" (the
    default) shows step 3 when the form is valid, "change" goes back to
    step 2 with every value kept, "book" checks everything again and saves.
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    params = request.POST if request.method == "POST" else request.GET
    owner = get_walkin_owner(params.get("owner"))
    if owner is None:
        messages.error(request, "That owner could not be found. Please search again.")
        return redirect("admin_walkin_appointment_create")

    if request.method != "POST":
        form = AdminWalkInAppointmentForm(owner)
        form.initial = walkin_initial(form.fields["pet"].queryset, params.get("pet"))
        return _render_walkin_visit(request, owner, form)

    action = request.POST.get("action", "review")
    if action == "change":
        form = AdminWalkInAppointmentForm(owner, initial=walkin_values(request.POST))
        return _render_walkin_visit(request, owner, form)

    form = AdminWalkInAppointmentForm(owner, request.POST)
    if form.slot_was_taken():
        return _walkin_slot_taken(request, owner)
    if not form.is_valid():
        return _render_walkin_visit(request, owner, form)
    if action != "book":
        return _render_walkin_review(request, owner, form)

    try:
        appointment = create_walkin_appointment(owner, form.cleaned_data)
    except SlotTaken:
        # Booked by someone else after the check, before the save
        return _walkin_slot_taken(request, owner)
    messages.success(request, _walk_in_booked_message(appointment))
    return redirect("admin_appointment_detail", pk=appointment.pk)


@login_required
def admin_get_slots_for_date(request):
    """
    HTMX view — returns slot <option> elements for a selected date.
    Used in admin forms (walk-in, reschedule).
    ?for=walk_in uses the walk-in rules (see get_available_slots); without
    it (the Move dialog) the Move rules: no same-day cutoff, only slots
    that haven't started. Only walk-ins get now_slot (the "Now" label).
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    import datetime

    date_str = request.GET.get("date", "")
    date = None
    slots = []
    now_slot = None

    if date_str:
        try:
            date = datetime.date.fromisoformat(date_str)
            walk_in = request.GET.get("for") == "walk_in"
            slots = get_available_slots(date, walk_in=walk_in, move=not walk_in)
            if walk_in:
                now_slot = slot_in_progress(date)
        except ValueError:
            pass

    # ?as=buttons: large radio buttons (detail page); default: <option>s (walk-in)
    template = (
        "admin/appointments/_slot_buttons.html"
        if request.GET.get("as") == "buttons"
        else "admin/appointments/_slot_options.html"
    )
    return render(
        request,
        template,
        {
            "slots": slots,
            "slots_date": date,
            "now_slot": now_slot,
            "selected": request.GET.get("selected", ""),
        },
    )


@login_required
def admin_appointment_calendar_grid(request):
    """
    HTMX view — returns the calendar grid + nav controls for the admin
    dashboard calendar widget. e.g. ?view=month&date=2026-06-01
    Self-targeting swap — triggered by click (nav/toggle), not a timer,
    so it never collides with the dashboard's 30s stats polling.
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    view = request.GET.get("view", "month")
    date_str = request.GET.get("date", "")

    context = get_admin_calendar_context(view, date_str)

    return render(request, "admin/appointments/_calendar_grid.html", context)


@login_required
def admin_appointment_calendar_day(request):
    """
    HTMX view — returns the day-detail panel for the admin calendar
    widget. Shows all appointments for the selected date, all statuses
    (including cancelled, for the full picture). View-only — links out
    to admin_appointment_detail, where all actions already live.
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    date_str = request.GET.get("date", "")
    selected_date = None
    appointments = Appointment.objects.none()

    if date_str:
        try:
            selected_date = datetime.date.fromisoformat(date_str)
            appointments = (
                Appointment.objects.filter(date=selected_date)
                .select_related("owner", "pet", "service")
                .order_by("time")
            )
        except ValueError:
            pass

    return render(
        request,
        "admin/appointments/_calendar_day_panel.html",
        {
            "selected_date": selected_date,
            "day_appointments": appointments,
        },
    )
