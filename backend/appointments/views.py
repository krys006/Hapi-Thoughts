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
    AppointmentBookingForm,
    AppointmentRescheduleForm,
    AppointmentCancellationForm,
    AdminAppointmentNotesForm,
    AdminAppointmentCancelForm,
    AdminAppointmentRescheduleForm,
    AdminWalkInAppointmentForm,
)

from .utils import (
    LIST_PAGE_SIZE,
    SETTINGS_TAB_FIELDS,
    SLOT_NO_LONGER_AVAILABLE,
    WALKIN_PAGE_SIZE,
    WALKIN_SEARCH_LIMIT,
    SlotTaken,
    clinic_hours_line,
    create_owner_booking,
    create_walkin_appointment,
    filter_appointments,
    first_error_tab,
    form_with_taken_time,
    get_admin_calendar_context,
    get_available_slots,
    get_clinic_settings,
    get_walkin_owner,
    group_by_day,
    is_owner_results_swap,
    is_slot_available,
    is_within_24h,
    list_links,
    list_period,
    list_tabs,
    lock_schedule,
    notify_admin_of_request,
    notify_request_declined,
    paging_links,
    picked_pet_initial,
    problems_label,
    reschedule_by_owner,
    resolve_list_tab,
    schedule_warning,
    search_walkin_owners,
    settings_tab,
    settings_tab_errors,
    slot_in_progress,
    tab_counts,
    unfinished_visits,
    visit_receipt_state,
    visits_on_date,
    walkin_initial,
    walkin_owner_list,
    walkin_slot_context,
    walkin_values,
)

from accounts.utils import owner_profile_required
from notifications.utils import compact_message, notify, when_line
from dashboard.templatetags.friendly_dates import friendly_date
from django.contrib.auth import get_user_model

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

    context["base_template"] = "base_dashboard.html"
    return render(request, "shared/clinic_info.html", context)


# ── Pet Owner — Appointments ──────────────────────────────────────────────────


@login_required
@owner_profile_required
def owner_appointment_list(request):
    """
    Pet Owner view — list all their appointments.
    Grouped into upcoming and past for clarity.
    """
    if request.user.role != "pet_owner":
        return redirect("admin_dashboard")

    owner = request.owner
    today = timezone.localdate()

    upcoming = (
        Appointment.objects.filter(
            owner=owner,
            date__gte=today,
        )
        .exclude(status=Appointment.CANCELLED)
        .order_by("date", "time")
    )

    past = Appointment.objects.filter(
        owner=owner,
        date__lt=today,
    ).order_by("-date", "-time")

    cancelled = Appointment.objects.filter(
        owner=owner,
        status=Appointment.CANCELLED,
    ).order_by("-date", "-time")

    return render(
        request,
        "owner/appointments/list.html",
        {
            "upcoming": upcoming,
            "past": past,
            "cancelled": cancelled,
        },
    )


@login_required
@owner_profile_required
def owner_book_appointment(request):
    """
    Pet Owner view — book a new appointment.
    On GET: show the booking form.
    On POST: validate and create the appointment as PENDING.
    """
    if request.user.role != "pet_owner":
        return redirect("admin_dashboard")

    owner = request.owner

    if request.method == "POST":
        form = AppointmentBookingForm(owner, request.POST)
        if form.is_valid():
            try:
                appointment = create_owner_booking(owner, form.cleaned_data)
            except SlotTaken:
                # Taken since the form was checked: keep every value but the time
                form = form_with_taken_time(
                    AppointmentBookingForm,
                    (owner,),
                    request.POST,
                    form.cleaned_data["time"],
                )
                messages.error(request, "Please correct the errors below.")
            else:
                notify_admin_of_request(appointment)
                messages.success(
                    request,
                    "Appointment request submitted. "
                    "You will be notified once it is confirmed.",
                )
                return redirect("owner_appointment_detail", pk=appointment.pk)
        else:
            messages.error(request, "Please correct the errors below.")
    else:
        # ?pet= starts the form with that pet (one of the owner's own)
        form = AppointmentBookingForm(
            owner, initial=picked_pet_initial(owner, request.GET.get("pet"))
        )

    clinic = get_clinic_settings()

    return render(
        request,
        "owner/appointments/book.html",
        {
            "form": form,
            "clinic": clinic,
        },
    )


@login_required
@owner_profile_required
def owner_appointment_detail(request, pk):
    """
    Pet Owner view — view a single appointment's details.
    Owners can only view their own appointments.
    """
    if request.user.role != "pet_owner":
        return redirect("admin_dashboard")

    owner = request.owner
    appointment = get_object_or_404(Appointment, pk=pk, owner=owner)

    return render(
        request,
        "owner/appointments/detail.html",
        {"appointment": appointment},
    )


@login_required
@owner_profile_required
def owner_appointment_cancel(request, pk):
    """
    Pet Owner view — cancel an appointment.
    Free cancellation if more than 24 hours before.
    Within 24 hours — still cancelled but flagged for admin awareness.
    Only PENDING or CONFIRMED appointments can be cancelled.
    """
    if request.user.role != "pet_owner":
        return redirect("admin_dashboard")

    owner = request.owner
    now = timezone.now()  # one read of the clock, for the late note
    appointment = get_object_or_404(Appointment, pk=pk, owner=owner)

    # Only pending or confirmed appointments can be cancelled
    if appointment.status not in [Appointment.PENDING, Appointment.CONFIRMED]:
        messages.error(
            request,
            "This appointment cannot be cancelled.",
        )
        return redirect("owner_appointment_detail", pk=pk)

    if request.method == "POST":
        form = AppointmentCancellationForm(request.POST)
        if form.is_valid():
            appointment.status = Appointment.CANCELLED
            appointment.cancellation_reason = form.cleaned_data["cancellation_reason"]
            appointment.cancellation_detail = form.cleaned_data.get(
                "cancellation_detail", ""
            )
            appointment.cancelled_by = request.user
            appointment.save()

            # Notify admin of cancellation
            User = get_user_model()
            admin_user = User.objects.filter(role="admin").first()
            if admin_user:
                notify(
                    recipient=admin_user,
                    notification_type="appointment_cancelled",
                    title="Appointment Cancelled",
                    message=(
                        f"{owner.first_name} {owner.last_name} has cancelled "
                        f"their appointment for {appointment.pet.name} on "
                        f"{appointment.date.strftime('%B %d, %Y')} "
                        f"at {appointment.time.strftime('%I:%M %p')}."
                    ),
                    related_appointment=appointment,
                    related_pet=appointment.pet,
                    email_subject="Appointment Cancelled — Hapi Vet",
                )

            messages.success(request, "Your appointment has been cancelled.")
            return redirect("owner_appointments")
        else:
            messages.error(request, "Please correct the errors below.")
    else:
        form = AppointmentCancellationForm()

    return render(
        request,
        "owner/appointments/detail.html",
        {
            "appointment": appointment,
            "cancellation_form": form,
            "show_cancel_form": True,
            "is_late": is_within_24h(appointment, now),
        },
    )


@login_required
@owner_profile_required
def owner_appointment_reschedule(request, pk):
    """
    Pet Owner view — request a reschedule.
    Creates a new PENDING appointment linked to the original.
    Original appointment is cancelled and replaced by the new one.
    Only PENDING or CONFIRMED appointments can be rescheduled.
    """
    if request.user.role != "pet_owner":
        return redirect("admin_dashboard")

    owner = request.owner
    now = timezone.now()  # one read of the clock, for the late note
    appointment = get_object_or_404(Appointment, pk=pk, owner=owner)

    if appointment.status not in [Appointment.PENDING, Appointment.CONFIRMED]:
        messages.error(request, "This appointment cannot be rescheduled.")
        return redirect("owner_appointment_detail", pk=pk)

    if request.method == "POST":
        form = AppointmentRescheduleForm(appointment, request.POST)
        if form.is_valid():
            response, form = _submit_owner_reschedule(request, appointment, form)
            if response:
                return response
        else:
            messages.error(request, "Please correct the errors below.")
    else:
        form = AppointmentRescheduleForm(appointment)

    return render(
        request,
        "owner/appointments/detail.html",
        {
            "appointment": appointment,
            "reschedule_form": form,
            "show_reschedule_form": True,
            "clinic": get_clinic_settings(),
            "is_late": is_within_24h(appointment, now),
        },
    )


def _submit_owner_reschedule(request, appointment, form):
    """
    Saves a valid owner reschedule form (see reschedule_by_owner).
    Returns (redirect, form): a redirect when the visit page is next, else
    None and the form to show again (the time taken meanwhile).
    """
    try:
        new_appointment = reschedule_by_owner(
            request.user, request.owner, appointment.pk, form.cleaned_data
        )
    except SlotTaken:
        # Taken since the form was checked: keep every value but the time
        messages.error(request, "Please correct the errors below.")
        again = form_with_taken_time(
            AppointmentRescheduleForm,
            (appointment,),
            request.POST,
            form.cleaned_data["time"],
        )
        return None, again
    if new_appointment is None:
        messages.error(request, "This visit can't be changed any more.")
        return redirect("owner_appointment_detail", pk=appointment.pk), form
    # After the transaction has committed, never inside it
    notify_admin_of_request(new_appointment, reschedule=True)
    messages.success(
        request,
        "Reschedule request submitted. You will be notified once it is confirmed.",
    )
    return redirect("owner_appointment_detail", pk=new_appointment.pk), form


@login_required
def owner_get_slots(request):
    """
    HTMX view — returns available time slots for a selected date.
    Called when the pet owner picks a date on the booking form.
    Returns a partial HTML snippet of <option> elements.
    """
    if request.user.role != "pet_owner":
        return redirect("admin_dashboard")

    import datetime

    date_str = request.GET.get("date", "")
    slots = []

    if date_str:
        try:
            date = datetime.date.fromisoformat(date_str)
            slots = get_available_slots(date)
        except ValueError:
            pass

    return render(
        request,
        "owner/appointments/_slot_options.html",
        {"slots": slots},
    )


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


@login_required
def admin_appointment_complete(request, pk):
    """
    Admin action — mark a CONFIRMED appointment as COMPLETED, on or after
    the appointment date.
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    appointment = get_object_or_404(Appointment, pk=pk)

    if request.method == "POST":
        if appointment.status != Appointment.CONFIRMED:
            messages.error(
                request, "Only confirmed appointments can be marked as completed."
            )
        elif appointment.date > timezone.localdate():
            messages.error(request, _too_early_message(appointment))
        else:
            appointment.status = Appointment.COMPLETED
            appointment.save()
            messages.success(
                request,
                f"Marked {appointment.pet.name}'s appointment on "
                f"{_when(appointment)} as completed.",
            )

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
