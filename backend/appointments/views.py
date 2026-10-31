from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.utils import timezone
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

from .utils import get_available_slots, get_admin_calendar_context

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
    today = timezone.now().date()

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
    """'Mon, Nov 2 at 2:00 PM' / 'Today at 9:00 AM' for messages."""
    time = date_format(appointment.time, "g:i A")
    return f"{friendly_date(appointment.date)} at {time}"


def _too_early_message(appointment):
    """Error for completing / no-showing an appointment before its date."""
    return (
        "You can mark this after the appointment date "
        f"({friendly_date(appointment.date)})."
    )


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
    return {
        "appointment": appointment,
        "notes_form": notes_form,
        "cancel_form": cancel_form,
        "reschedule_form": reschedule_form,
    }


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
    Admin view — list all appointments with status filter.
    Supports filtering by status via GET parameter.
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    status_filter = request.GET.get("status", "")
    today = timezone.now().date()

    appointments = Appointment.objects.select_related(
        "owner", "pet", "service"
    ).order_by("-date", "-time")

    if status_filter:
        appointments = appointments.filter(status=status_filter)

    # Counts for filter badges
    counts = {
        "all": Appointment.objects.count(),
        "pending": Appointment.objects.filter(status=Appointment.PENDING).count(),
        "confirmed": Appointment.objects.filter(status=Appointment.CONFIRMED).count(),
        "completed": Appointment.objects.filter(status=Appointment.COMPLETED).count(),
        "cancelled": Appointment.objects.filter(status=Appointment.CANCELLED).count(),
        "no_show": Appointment.objects.filter(status=Appointment.NO_SHOW).count(),
    }

    # Check if this is an HTMX request — return partial only
    if request.headers.get("HX-Request"):
        return render(
            request,
            "admin/appointments/_appointment_list_partial.html",
            {
                "appointments": appointments,
                "status_filter": status_filter,
                "today": today,
            },
        )

    return render(
        request,
        "admin/appointments/list.html",
        {
            "appointments": appointments,
            "status_filter": status_filter,
            "counts": counts,
            "today": today,
            "status_choices": Appointment.STATUS_CHOICES,
        },
    )


@login_required
def admin_appointment_detail(request, pk):
    """
    Admin view — view full appointment details.
    All actions (approve, reject, complete, etc.) POST to separate views.
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    appointment = get_object_or_404(
        Appointment.objects.select_related("owner", "pet", "service"),
        pk=pk,
    )

    return render(
        request,
        "admin/appointments/detail.html",
        _admin_detail_context(appointment),
    )


@login_required
def admin_appointment_approve(request, pk):
    """Admin action — approve a PENDING appointment → CONFIRMED."""
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    appointment = get_object_or_404(Appointment, pk=pk)

    if request.method == "POST":
        if appointment.status != Appointment.PENDING:
            messages.error(request, "Only pending appointments can be approved.")
        else:
            appointment.status = Appointment.CONFIRMED
            appointment.save()

            # Notify pet owner of approval
            notify(
                recipient=appointment.owner.user,
                notification_type="appointment_approved",
                title="Appointment Confirmed",
                message=(
                    f"Your appointment for {appointment.pet.name} on "
                    f"{appointment.date.strftime('%B %d, %Y')} "
                    f"at {appointment.time.strftime('%I:%M %p')} "
                    f"has been confirmed."
                ),
                related_appointment=appointment,
                related_pet=appointment.pet,
                email_subject="Appointment Confirmed — Hapi Vet",
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
                f"{appointment.date.strftime('%B %d, %Y')} "
                f"at {appointment.time.strftime('%I:%M %p')} "
                f"was not confirmed by the clinic. "
                f"Please contact the clinic for more information."
            )
            if clinic_message:
                owner_message += f" Message from the clinic: {clinic_message}"

            # Notify pet owner of rejection
            notify(
                recipient=appointment.owner.user,
                notification_type="appointment_rejected",
                title="Appointment Not Confirmed",
                message=owner_message,
                related_appointment=appointment,
                related_pet=appointment.pet,
                email_subject="Appointment Update — Hapi Vet",
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
            appointment.save()
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
            return render(
                request,
                "admin/appointments/detail.html",
                _admin_detail_context(appointment, cancel_form=form),
            )

        appointment.status = Appointment.CANCELLED
        appointment.cancellation_reason = form.cleaned_data["cancellation_reason"]
        appointment.cancellation_detail = form.cleaned_data.get(
            "cancellation_detail", ""
        )
        appointment.cancelled_by = request.user
        appointment.save()

        # Notify pet owner of cancellation by clinic
        notify(
            recipient=appointment.owner.user,
            notification_type="appointment_cancelled",
            title="Appointment Cancelled by Clinic",
            message=(
                f"Your appointment for {appointment.pet.name} on "
                f"{appointment.date.strftime('%B %d, %Y')} "
                f"at {appointment.time.strftime('%I:%M %p')} "
                f"has been cancelled by the clinic. "
                f"Please contact the clinic for more information."
            ),
            related_appointment=appointment,
            related_pet=appointment.pet,
            email_subject="Appointment Cancelled — Hapi Vet",
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
            return render(
                request,
                "admin/appointments/detail.html",
                _admin_detail_context(appointment, reschedule_form=form),
            )

        new_appointment = _reschedule_appointment(appointment, form, request.user)

        # Notify pet owner of reschedule by clinic
        notify(
            recipient=new_appointment.owner.user,
            notification_type="appointment_approved",
            title="Appointment Rescheduled by Clinic",
            message=(
                f"Your appointment for {new_appointment.pet.name} has been "
                f"rescheduled to {new_appointment.date.strftime('%B %d, %Y')} "
                f"at {new_appointment.time.strftime('%I:%M %p')} by the clinic."
            ),
            related_appointment=new_appointment,
            related_pet=new_appointment.pet,
            email_subject="Appointment Rescheduled — Hapi Vet",
        )

        messages.success(
            request,
            f"Rescheduled {new_appointment.pet.name} to "
            f"{_when(new_appointment)}. The owner has been notified.",
        )
        return redirect("admin_appointment_detail", pk=new_appointment.pk)

    return redirect("admin_appointment_detail", pk=pk)


@login_required
def admin_walkin_appointment_create(request):
    """
    Admin view — create a walk-in appointment directly as CONFIRMED.
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

    if request.method == "POST":
        form = AdminWalkInAppointmentForm(request.POST)
        if form.is_valid():
            appointment = Appointment.objects.create(
                owner=form.cleaned_data["owner"],
                pet=form.cleaned_data["pet"],
                service=form.cleaned_data.get("service"),
                date=form.cleaned_data["date"],
                time=form.cleaned_data["time"],
                reason=form.cleaned_data.get("reason", ""),
                status=Appointment.CONFIRMED,
                is_walk_in=True,
            )

            # Notify pet owner of their walk-in appointment if they have an email
            if appointment.owner.user.email and appointment.owner.user.is_active:
                notify(
                    recipient=appointment.owner.user,
                    notification_type="appointment_approved",
                    title="Walk-in Appointment Created",
                    message=(
                        f"A walk-in appointment has been created for "
                        f"{appointment.pet.name} on "
                        f"{appointment.date.strftime('%B %d, %Y')} "
                        f"at {appointment.time.strftime('%I:%M %p')}."
                    ),
                    related_appointment=appointment,
                    related_pet=appointment.pet,
                    email_subject="Appointment Created — Hapi Vet",
                )

            messages.success(
                request,
                f"Walk-in appointment created for {appointment.pet.name}.",
            )
            return redirect("admin_appointment_detail", pk=appointment.pk)
        else:
            messages.error(request, "Please correct the errors below.")
    else:
        form = AdminWalkInAppointmentForm()

    return render(
        request,
        "admin/appointments/walkin_form.html",
        {"form": form},
    )


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
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")

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
        "admin/appointments/_slot_options.html",
        {"slots": slots},
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
