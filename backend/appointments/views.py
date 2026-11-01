from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.exceptions import ObjectDoesNotExist
from django.core.paginator import Paginator
from django.db import transaction
from django.utils import timezone
from django.utils.cache import patch_vary_headers
from django.utils.dateformat import format as date_format

from .models import ClinicSettings, BlockedDate, Appointment
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
    SHOW_OPTIONS,
    WALKIN_PAGE_SIZE,
    WALKIN_SEARCH_LIMIT,
    create_walkin_appointment,
    filter_appointments,
    get_admin_calendar_context,
    get_available_slots,
    get_walkin_owner,
    group_by_day,
    list_links,
    list_period,
    list_tabs,
    paging_links,
    resolve_list_tab,
    search_walkin_owners,
    slot_in_progress,
    tab_counts,
    unfinished_visits,
    walkin_initial,
    walkin_owner_list,
    walkin_slot_context,
    walkin_values,
)

from notifications.utils import notify
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

    settings_obj, created = ClinicSettings.objects.get_or_create(
        pk=1,
        defaults={
            "clinic_name": "Hapi Tutz Vet. Supplies",
            "address": "Bognuyan, Gasan, Marinduque",
            "opening_time": "08:00",
            "closing_time": "17:00",
            "same_day_cutoff_time": "15:00",
            "working_days": [0, 1, 2, 3, 4],
        },
    )

    if request.method == "POST":
        form = ClinicSettingsForm(
            request.POST,
            request.FILES,
            instance=settings_obj,
        )
        if form.is_valid():
            form.save()
            messages.success(request, "Clinic settings updated successfully.")
            return redirect("admin_clinic_settings")
        else:
            messages.error(request, "Please correct the errors below.")
    else:
        initial = {"working_days": settings_obj.working_days}
        form = ClinicSettingsForm(instance=settings_obj, initial=initial)

    blocked_dates = BlockedDate.objects.all()
    blocked_form = BlockedDateForm()

    return render(
        request,
        "admin/settings/clinic_settings.html",
        {
            "form": form,
            "blocked_dates": blocked_dates,
            "blocked_form": blocked_form,
            "settings_obj": settings_obj,
        },
    )


@login_required
def admin_blocked_date_add(request):
    """Admin view — add a blocked date. Returns HTMX partial on success."""
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    if request.method == "POST":
        form = BlockedDateForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Date blocked successfully.")
        else:
            settings_obj = ClinicSettings.objects.first()
            blocked_dates = BlockedDate.objects.all()
            return render(
                request,
                "admin/settings/clinic_settings.html",
                {
                    "form": ClinicSettingsForm(instance=settings_obj),
                    "blocked_dates": blocked_dates,
                    "blocked_form": form,
                    "settings_obj": settings_obj,
                },
            )

    blocked_dates = BlockedDate.objects.all()
    return render(
        request,
        "admin/settings/_blocked_dates_list.html",
        {"blocked_dates": blocked_dates},
    )


@login_required
def admin_blocked_date_delete(request, pk):
    """Admin view — delete a blocked date. Returns updated HTMX partial."""
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    blocked_date = get_object_or_404(BlockedDate, pk=pk)

    if request.method == "POST":
        blocked_date.delete()
        messages.success(request, "Blocked date removed.")

    blocked_dates = BlockedDate.objects.all()
    return render(
        request,
        "admin/settings/_blocked_dates_list.html",
        {"blocked_dates": blocked_dates},
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

    clinic = ClinicSettings.objects.first()

    maps_embed_url = ""
    if clinic and clinic.address:
        maps_embed_url = (
            f"https://www.google.com/maps?q={quote(clinic.address)}&output=embed"
        )

    return render(
        request,
        "shared/clinic_info.html",
        {
            "clinic": clinic,
            "maps_embed_url": maps_embed_url,
            "base_template": (
                "layouts/admin.html" if request.user.is_admin else "base_dashboard.html"
            ),
        },
    )


# ── Pet Owner — Appointments ──────────────────────────────────────────────────


@login_required
def owner_appointment_list(request):
    """
    Pet Owner view — list all their appointments.
    Grouped into upcoming and past for clarity.
    """
    if request.user.role != "pet_owner":
        return redirect("admin_dashboard")

    owner = request.user.petowner
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
def owner_book_appointment(request):
    """
    Pet Owner view — book a new appointment.
    On GET: show the booking form.
    On POST: validate and create the appointment as PENDING.
    """
    if request.user.role != "pet_owner":
        return redirect("admin_dashboard")

    owner = request.user.petowner

    if request.method == "POST":
        form = AppointmentBookingForm(owner, request.POST)
        if form.is_valid():
            appointment = Appointment.objects.create(
                owner=owner,
                pet=form.cleaned_data["pet"],
                service=form.cleaned_data.get("service"),
                date=form.cleaned_data["date"],
                time=form.cleaned_data["time"],
                reason=form.cleaned_data.get("reason", ""),
                status=Appointment.PENDING,
            )

            # Notify admin of new appointment request
            User = get_user_model()
            admin_user = User.objects.filter(role="admin").first()
            clinic = ClinicSettings.objects.first()

            if admin_user:
                admin_notification_email = (
                    clinic.notification_email
                    if clinic and clinic.notification_email
                    else admin_user.email
                )

                notify(
                    recipient=admin_user,
                    notification_type="appointment_requested",
                    title="New Appointment Request",
                    message=(
                        f"{owner.first_name} {owner.last_name} has requested "
                        f"an appointment for {appointment.pet.name} on "
                        f"{appointment.date.strftime('%B %d, %Y')} "
                        f"at {appointment.time.strftime('%I:%M %p')}."
                    ),
                    related_appointment=appointment,
                    related_pet=appointment.pet,
                    email_subject="New Appointment Request — Hapi Vet",
                    recipient_email=admin_notification_email,
                )

            messages.success(
                request,
                "Appointment request submitted. "
                "You will be notified once it is confirmed.",
            )
            return redirect("owner_appointment_detail", pk=appointment.pk)
        else:
            messages.error(request, "Please correct the errors below.")
    else:
        form = AppointmentBookingForm(owner)

    clinic = ClinicSettings.objects.first()

    return render(
        request,
        "owner/appointments/book.html",
        {
            "form": form,
            "clinic": clinic,
        },
    )


@login_required
def owner_appointment_detail(request, pk):
    """
    Pet Owner view — view a single appointment's details.
    Owners can only view their own appointments.
    """
    if request.user.role != "pet_owner":
        return redirect("admin_dashboard")

    owner = request.user.petowner
    appointment = get_object_or_404(Appointment, pk=pk, owner=owner)

    return render(
        request,
        "owner/appointments/detail.html",
        {"appointment": appointment},
    )


@login_required
def owner_appointment_cancel(request, pk):
    """
    Pet Owner view — cancel an appointment.
    Free cancellation if more than 24 hours before.
    Within 24 hours — still cancelled but flagged for admin awareness.
    Only PENDING or CONFIRMED appointments can be cancelled.
    """
    if request.user.role != "pet_owner":
        return redirect("admin_dashboard")

    owner = request.user.petowner
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
        },
    )


@login_required
def owner_appointment_reschedule(request, pk):
    """
    Pet Owner view — request a reschedule.
    Creates a new PENDING appointment linked to the original.
    Original appointment is cancelled and replaced by the new one.
    Only PENDING or CONFIRMED appointments can be rescheduled.
    """
    if request.user.role != "pet_owner":
        return redirect("admin_dashboard")

    owner = request.user.petowner
    appointment = get_object_or_404(Appointment, pk=pk, owner=owner)

    if appointment.status not in [Appointment.PENDING, Appointment.CONFIRMED]:
        messages.error(request, "This appointment cannot be rescheduled.")
        return redirect("owner_appointment_detail", pk=pk)

    if request.method == "POST":
        form = AppointmentRescheduleForm(appointment, request.POST)
        if form.is_valid():
            # Cancel the original appointment
            appointment.status = Appointment.CANCELLED
            appointment.cancellation_reason = Appointment.OTHER
            appointment.cancellation_detail = "Rescheduled by pet owner."
            appointment.cancelled_by = request.user
            appointment.save()

            # Create a new appointment linked to the original
            new_appointment = Appointment.objects.create(
                owner=owner,
                pet=appointment.pet,
                service=appointment.service,
                date=form.cleaned_data["date"],
                time=form.cleaned_data["time"],
                reason=appointment.reason,
                reschedule_reason=form.cleaned_data["reschedule_reason"],
                rescheduled_from=appointment,
                status=Appointment.PENDING,
            )

            # Notify admin of reschedule request
            User = get_user_model()
            admin_user = User.objects.filter(role="admin").first()
            if admin_user:
                notify(
                    recipient=admin_user,
                    notification_type="appointment_requested",
                    title="Appointment Reschedule Request",
                    message=(
                        f"{owner.first_name} {owner.last_name} has requested "
                        f"to reschedule their appointment for "
                        f"{new_appointment.pet.name} "
                        f"to {new_appointment.date.strftime('%B %d, %Y')} "
                        f"at {new_appointment.time.strftime('%I:%M %p')}."
                    ),
                    related_appointment=new_appointment,
                    related_pet=new_appointment.pet,
                    email_subject="Appointment Reschedule Request — Hapi Vet",
                )

            messages.success(
                request,
                "Reschedule request submitted. "
                "You will be notified once it is confirmed.",
            )
            return redirect("owner_appointment_detail", pk=new_appointment.pk)
        else:
            messages.error(request, "Please correct the errors below.")
    else:
        form = AppointmentRescheduleForm(appointment)

    clinic = ClinicSettings.objects.first()

    return render(
        request,
        "owner/appointments/detail.html",
        {
            "appointment": appointment,
            "reschedule_form": form,
            "show_reschedule_form": True,
            "clinic": clinic,
        },
    )


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


def _notification_when(appointment):
    """'November 02, 2026 at 02:00 PM', the format of the older owner messages."""
    return (
        f"{appointment.date.strftime('%B %d, %Y')} "
        f"at {appointment.time.strftime('%I:%M %p')}"
    )


def _notify_owner(appointment, notification_type, title, message, email_subject):
    """In-app notification to the appointment's owner, plus email per their
    preferences (see notifications.utils.notify)."""
    notify(
        recipient=appointment.owner.user,
        notification_type=notification_type,
        title=title,
        message=message,
        related_appointment=appointment,
        related_pet=appointment.pet,
        email_subject=email_subject,
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
        "receipt": _related_or_none(appointment, "billing_receipt"),
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
    new appointment fails, the original stays as it was.
    Returns the new appointment.
    """
    with transaction.atomic():
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
        "show_options": SHOW_OPTIONS,
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
        if appointment.status != Appointment.PENDING:
            messages.error(request, "Only pending appointments can be approved.")
        elif appointment.date < timezone.localdate():
            messages.error(
                request,
                f"This request was for {date_format(appointment.date, 'D, M j')}, "
                "which has passed. Move it to another time or decline it.",
            )
        else:
            appointment.status = Appointment.CONFIRMED
            appointment.save()

            # Notify pet owner of approval
            _notify_owner(
                appointment,
                "appointment_approved",
                "Appointment Confirmed",
                f"Your appointment for {appointment.pet.name} on "
                f"{_notification_when(appointment)} has been confirmed.",
                "Appointment Confirmed — Hapi Vet",
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
            appointment.cancellation_reason = Appointment.OTHER
            appointment.cancellation_detail = clinic_message or "Rejected by clinic."
            appointment.cancelled_by = request.user
            appointment.save()

            owner_message = (
                f"Your appointment request for {appointment.pet.name} on "
                f"{_notification_when(appointment)} "
                f"was not confirmed by the clinic. "
                f"Please contact the clinic for more information."
            )
            if clinic_message:
                owner_message += f" Message from the clinic: {clinic_message}"

            # Notify pet owner of rejection
            _notify_owner(
                appointment,
                "appointment_rejected",
                "Appointment Not Confirmed",
                owner_message,
                "Appointment Update — Hapi Vet",
            )

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

        # Notify pet owner of cancellation by clinic
        _notify_owner(
            appointment,
            "appointment_cancelled",
            "Appointment Cancelled by Clinic",
            f"Your appointment for {appointment.pet.name} on "
            f"{_notification_when(appointment)} has been cancelled by the clinic. "
            "Please contact the clinic for more information.",
            "Appointment Cancelled — Hapi Vet",
        )

        messages.success(
            request,
            f"Cancelled {appointment.pet.name}'s appointment on "
            f"{_when(appointment)}. The owner has been notified.",
        )

    return redirect("admin_appointment_detail", pk=pk)


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

        new_appointment = _reschedule_appointment(appointment, form, request.user)

        # Notify pet owner of reschedule by clinic. Absolute date, since the
        # email may be read on another day.
        _notify_owner(
            new_appointment,
            "appointment_approved",
            "Appointment moved by the clinic",
            f"Your appointment for {new_appointment.pet.name} was moved to "
            f"{date_format(new_appointment.date, 'D, M j')} at "
            f"{date_format(new_appointment.time, 'g:i A')} by the clinic. "
            "The new time is confirmed.",
            "Appointment moved — Hapi Vet",
        )

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


def _is_owner_results_swap(request):
    """True for the filter-as-you-type request from step 1's search box,
    which only needs the results. A history restore (or a request without
    HTMX) gets the full page."""
    headers = request.headers
    return (
        bool(headers.get("HX-Request"))
        and headers.get("HX-Target") == "owner-results"
        and not headers.get("HX-History-Restore-Request")
    )


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
        if _is_owner_results_swap(request)
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
        # Everything kept except the time, which must be picked again
        initial = walkin_values(request.POST, keep_time=False)
        taken = _taken_time(request.POST.get("time", ""))
        return _render_walkin_visit(
            request, owner, AdminWalkInAppointmentForm(owner, initial=initial), taken
        )
    if not form.is_valid():
        return _render_walkin_visit(request, owner, form)
    if action != "book":
        return _render_walkin_review(request, owner, form)

    appointment = create_walkin_appointment(owner, form.cleaned_data)
    messages.success(request, _walk_in_booked_message(appointment))
    return redirect("admin_appointment_detail", pk=appointment.pk)


@login_required
def admin_get_pets_for_owner(request):
    """
    HTMX view — returns pet <option> elements for a selected owner.
    Used in the walk-in appointment form to filter pets by owner.
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    from pets.models import Pet

    owner_id = request.GET.get("owner", "")
    pets = Pet.objects.none()

    if owner_id:
        try:
            pets = Pet.objects.filter(
                owner_id=int(owner_id),
                is_archived=False,
            ).order_by("name")
        except (ValueError, TypeError):
            pass

    return render(
        request,
        "admin/appointments/_pet_options.html",
        {"pets": pets},
    )


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
