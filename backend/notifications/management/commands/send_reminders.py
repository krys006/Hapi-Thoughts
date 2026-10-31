# notifications/management/commands/send_reminders.py

import datetime
from django.core.management.base import BaseCommand
from django.utils import timezone

from appointments.models import Appointment
from medical.models import MedicalRecord, Vaccination
from notifications.models import Notification, NotificationPreference
from notifications.utils import notify


class Command(BaseCommand):
    help = "Sends appointment, vaccination, and follow-up reminders."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview reminders without sending anything.",
        )

    def handle(self, *args, **options):
        self.dry_run = options["dry_run"]
        today = timezone.now().date()

        if self.dry_run:
            self.stdout.write(
                self.style.WARNING("DRY RUN — no notifications will be sent.\n")
            )

        appt_count = self._send_appointment_reminders(today)
        vacc_count = self._send_vaccination_reminders(today)
        followup_count = self._send_followup_reminders(today)

        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(
                f"Appointment reminders : {appt_count} sent\n"
                f"Vaccination reminders : {vacc_count} sent\n"
                f"Follow-up reminders   : {followup_count} sent"
            )
        )

    # ── Appointment Reminders ─────────────────────────────────────────────────

    def _send_appointment_reminders(self, today):
        """
        Sends reminders for upcoming CONFIRMED appointments.
        Reminder timing is per pet owner's NotificationPreference.
        Also sends a day-of reminder as a safety net.
        """
        count = 0

        # Get all confirmed future appointments
        upcoming = Appointment.objects.filter(
            status=Appointment.CONFIRMED,
            date__gte=today,
        ).select_related("owner", "owner__user", "pet")

        for appointment in upcoming:
            owner_user = appointment.owner.user
            days_until = (appointment.date - today).days

            # Get this owner's reminder preference
            try:
                pref = owner_user.notification_preference
                reminder_days = pref.appointment_reminder_days
            except NotificationPreference.DoesNotExist:
                reminder_days = 1

            # Fire on the configured day OR day-of (day 0) as safety net
            if days_until not in [reminder_days, 0]:
                continue

            # Duplicate prevention — skip if already sent for this appointment
            already_sent = Notification.objects.filter(
                recipient=owner_user,
                notification_type=Notification.APPOINTMENT_REMINDER,
                related_appointment=appointment,
                created_at__date=today,
            ).exists()

            if already_sent:
                self.stdout.write(
                    f"  SKIP (already sent today): appointment {appointment.pk} "
                    f"for {appointment.pet.name}"
                )
                continue

            if days_until == 0:
                title = f"Your appointment is today"
                message = (
                    f"Reminder: {appointment.pet.name} has an appointment today "
                    f"at {appointment.time.strftime('%I:%M %p')}. "
                    f"We look forward to seeing you!"
                )
            else:
                title = f"Upcoming appointment in {days_until} day{'s' if days_until != 1 else ''}"
                message = (
                    f"Reminder: {appointment.pet.name} has an appointment on "
                    f"{appointment.date.strftime('%B %d, %Y')} "
                    f"at {appointment.time.strftime('%I:%M %p')}."
                )

            if self.dry_run:
                self.stdout.write(
                    f"  [DRY RUN] Appointment reminder → {owner_user.email} "
                    f"| {appointment.pet.name} on {appointment.date}"
                )
            else:
                notify(
                    recipient=owner_user,
                    notification_type=Notification.APPOINTMENT_REMINDER,
                    title=title,
                    message=message,
                    related_appointment=appointment,
                    related_pet=appointment.pet,
                    email_subject=f"Appointment Reminder — Hapi Vet",
                )
                self.stdout.write(
                    f"  SENT: Appointment reminder → {owner_user.email} "
                    f"| {appointment.pet.name} on {appointment.date}"
                )

            count += 1

        return count

    # ── Vaccination Reminders ─────────────────────────────────────────────────

    def _send_vaccination_reminders(self, today):
        """
        Sends reminders for vaccinations coming due.
        Reminder timing is per pet owner's NotificationPreference.
        Also sends a day-of reminder as a safety net.
        """
        count = 0

        # Get all vaccinations with a future or today due date
        upcoming = Vaccination.objects.filter(
            next_due_date__gte=today,
        ).select_related("pet", "pet__owner", "pet__owner__user")

        for vaccination in upcoming:
            owner_user = vaccination.pet.owner.user
            days_until = (vaccination.next_due_date - today).days

            # Get this owner's reminder preference
            try:
                pref = owner_user.notification_preference
                reminder_days = pref.vaccination_reminder_days
            except NotificationPreference.DoesNotExist:
                reminder_days = 7

            # Fire on the configured day OR day-of (day 0) as safety net
            if days_until not in [reminder_days, 0]:
                continue

            # Duplicate prevention
            already_sent = Notification.objects.filter(
                recipient=owner_user,
                notification_type=Notification.VACCINATION_REMINDER,
                related_pet=vaccination.pet,
                created_at__date=today,
            ).exists()

            if already_sent:
                self.stdout.write(
                    f"  SKIP (already sent today): vaccination reminder "
                    f"for {vaccination.pet.name}"
                )
                continue

            vaccine_display = vaccination.display_vaccine_name

            if days_until == 0:
                title = f"Vaccination due today for {vaccination.pet.name}"
                message = (
                    f"{vaccination.pet.name}'s {vaccine_display} vaccination "
                    f"is due today. Please contact the clinic to schedule an appointment."
                )
            else:
                title = f"Vaccination due in {days_until} day{'s' if days_until != 1 else ''}"
                message = (
                    f"{vaccination.pet.name}'s {vaccine_display} vaccination "
                    f"is due on {vaccination.next_due_date.strftime('%B %d, %Y')}. "
                    f"Please contact the clinic to schedule an appointment."
                )

            if self.dry_run:
                self.stdout.write(
                    f"  [DRY RUN] Vaccination reminder → {owner_user.email} "
                    f"| {vaccination.pet.name} — {vaccine_display} "
                    f"due {vaccination.next_due_date}"
                )
            else:
                notify(
                    recipient=owner_user,
                    notification_type=Notification.VACCINATION_REMINDER,
                    title=title,
                    message=message,
                    related_pet=vaccination.pet,
                    email_subject=f"Vaccination Reminder — Hapi Vet",
                )
                self.stdout.write(
                    f"  SENT: Vaccination reminder → {owner_user.email} "
                    f"| {vaccination.pet.name} — {vaccine_display} "
                    f"due {vaccination.next_due_date}"
                )

            count += 1

        return count

    # ── Follow-up Reminders ───────────────────────────────────────────────────

    def _send_followup_reminders(self, today):
        """
        Sends reminders for medical record follow-ups.
        Fixed timing: 3 days before + day-of as safety net.
        """
        count = 0

        # Get all records with a future or today follow-up date
        upcoming = MedicalRecord.objects.filter(
            follow_up_required=True,
            follow_up_date__gte=today,
        ).select_related("pet", "pet__owner", "pet__owner__user")

        for record in upcoming:
            owner_user = record.pet.owner.user
            days_until = (record.follow_up_date - today).days

            # Fixed: fire 3 days before or day-of only
            if days_until not in [3, 0]:
                continue

            # Duplicate prevention
            already_sent = Notification.objects.filter(
                recipient=owner_user,
                notification_type=Notification.FOLLOWUP_REMINDER,
                related_pet=record.pet,
                created_at__date=today,
            ).exists()

            if already_sent:
                self.stdout.write(
                    f"  SKIP (already sent today): follow-up reminder "
                    f"for {record.pet.name}"
                )
                continue

            if days_until == 0:
                title = f"Follow-up appointment today for {record.pet.name}"
                message = (
                    f"{record.pet.name} has a follow-up visit scheduled for today. "
                    f"Please contact the clinic if you have any questions."
                )
            else:
                title = (
                    f"Follow-up appointment in {days_until} days for {record.pet.name}"
                )
                message = (
                    f"{record.pet.name} has a follow-up visit scheduled for "
                    f"{record.follow_up_date.strftime('%B %d, %Y')}. "
                    f"Please contact the clinic to confirm your appointment."
                )

            if self.dry_run:
                self.stdout.write(
                    f"  [DRY RUN] Follow-up reminder → {owner_user.email} "
                    f"| {record.pet.name} on {record.follow_up_date}"
                )
            else:
                notify(
                    recipient=owner_user,
                    notification_type=Notification.FOLLOWUP_REMINDER,
                    title=title,
                    message=message,
                    related_appointment=record.appointment,
                    related_pet=record.pet,
                    email_subject=f"Follow-up Reminder — Hapi Vet",
                )
                self.stdout.write(
                    f"  SENT: Follow-up reminder → {owner_user.email} "
                    f"| {record.pet.name} on {record.follow_up_date}"
                )

            count += 1

        return count
