"""Helpers for the medical views."""

from django.utils.dateformat import format as date_format


def correction_line(reason, today):
    """One dated correction line: 'Tue, Nov 3, 2026: wrong date typed'.
    The reason is kept on one line."""
    return f"{date_format(today, 'D, M j, Y')}: {' '.join(reason.split())}"


def save_vaccination_edit(form, today):
    """
    Save a valid VaccinationEditForm.
    A clinical change marks the vaccination corrected and adds the reason
    to correction_note as a new dated line. Earlier lines are kept exactly;
    correction_note is never cleared. Without a clinical change the
    correction fields are untouched (a reason typed anyway is ignored).
    """
    vaccination = form.save(commit=False)
    if form.changed_clinical:
        line = correction_line(form.cleaned_data["correction_reason"], today)
        vaccination.is_corrected = True
        if vaccination.correction_note:
            vaccination.correction_note = f"{vaccination.correction_note}\n{line}"
        else:
            vaccination.correction_note = line
    vaccination.save()
    return vaccination
