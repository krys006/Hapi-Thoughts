# pets/views.py

import copy
import logging
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError
from django.shortcuts import (
    get_object_or_404,
    redirect,
    render,
)
from django.urls import reverse
from django.utils import timezone

from .forms import (
    AdminOwnerEmailForm,
    AdminPetArchiveForm,
    AdminPetForm,
    AdminPetOwnerForm,
    ContactLinkFormSet,
    PetDeletionRequestForm,
    PetForm,
    PetOwnerProfileForm,
)

from .models import (
    Pet,
    PetDeletionRequest,
    PetOwner,
)

from .utils import (
    answer_deletion_request,
    archive_pet_directly,
    archived_pet_refusal,
    barangay_choices,
    breed_choices,
    create_owner,
    deletion_answer_refusal,
    deletion_requests_context,
    owner_account_state,
    owner_detail_context,
    owner_list_context,
    pet_detail_context,
    pet_archive_refusal,
    pet_list_context,
    pet_restore_refusal,
)

from accounts.utils import (
    WALKIN_NO_EMAIL_TAKEN,
    WALKIN_SAVE_FAILED,
    send_claim_email,
)

from medical.models import MedicalRecord, Vaccination

logger = logging.getLogger(__name__)

CLAIM_LINK_NOT_SENT = (
    "Email saved, but the link couldn't be sent. Use Send the link again later."
)


def _require_pet_owner(request):
    """
    Shared guard for all pet owner views.
    Returns a redirect response if the user should not be here, or None if ok.
    Usage: guard = _require_pet_owner(request); if guard: return guard
    """
    if request.user.role != "pet_owner":
        return redirect("admin_dashboard")
    return None


# ---------------------------------------------------------------------------
# Onboarding — Step 1: Profile Info
# ---------------------------------------------------------------------------


@login_required
def onboarding_step1(request):
    # Role check — admin must never land here
    guard = _require_pet_owner(request)
    if guard:
        return guard

    # If already onboarded, skip the whole flow
    if request.user.is_onboarded:
        return redirect("owner_dashboard")

    if request.method == "POST":
        profile_form = PetOwnerProfileForm(request.POST, request.FILES)
        link_formset = ContactLinkFormSet(request.POST, prefix="links")

        if profile_form.is_valid() and link_formset.is_valid():
            # Save the PetOwner record linked to this user
            pet_owner = profile_form.save(commit=False)
            pet_owner.user = request.user
            pet_owner.save()

            # Save contact links — skip blank rows
            for form in link_formset:
                if form.is_valid() and not form.is_empty():
                    link = form.save(commit=False)
                    link.owner = pet_owner
                    link.save()

            # Store the new owner's PK in the session so Step 2 can find it
            # without a DB query on the user object.
            # This is cleared after Step 2 completes.
            request.session["onboarding_owner_id"] = pet_owner.pk

            return redirect("owner_onboarding_step2")

    else:
        profile_form = PetOwnerProfileForm()
        link_formset = ContactLinkFormSet(prefix="links")

    return render(
        request,
        "owner/onboarding/step1.html",
        {
            "profile_form": profile_form,
            "link_formset": link_formset,
            # Used by the template to show which step is active
            "current_step": 1,
        },
    )


# ---------------------------------------------------------------------------
# Onboarding — Step 2: Add First Pet
# ---------------------------------------------------------------------------


@login_required
def onboarding_step2(request):
    # Role check
    guard = _require_pet_owner(request)
    if guard:
        return guard

    # If already onboarded, skip the whole flow
    if request.user.is_onboarded:
        return redirect("owner_dashboard")

    # Guard: Step 2 requires Step 1 to have been completed first.
    # If someone navigates directly to step 2 without a session key,
    # send them back to step 1.
    owner_id = request.session.get("onboarding_owner_id")
    if not owner_id:
        return redirect("owner_onboarding_step1")

    # Safely fetch the PetOwner — if something is wrong, restart onboarding
    try:
        pet_owner = PetOwner.objects.get(pk=owner_id, user=request.user)
    except PetOwner.DoesNotExist:
        return redirect("owner_onboarding_step1")

    if request.method == "POST":
        # --- "Skip" button submitted ---
        # The skip button posts a hidden field: action=skip
        if request.POST.get("action") == "skip":
            _complete_onboarding(request)
            return redirect("owner_dashboard")

        # --- Pet form submitted ---
        pet_form = PetForm(request.POST, request.FILES)
        if pet_form.is_valid():
            pet = pet_form.save(commit=False)
            pet.owner = pet_owner
            pet.save()

            _complete_onboarding(request)
            messages.success(
                request,
                f"{pet.name} has been added. Welcome to Hapi Vet!",
            )
            return redirect("owner_dashboard")

    else:
        pet_form = PetForm()

    return render(
        request,
        "owner/onboarding/step2.html",
        {
            "pet_form": pet_form,
            "pet_owner": pet_owner,
            "current_step": 2,
        },
    )


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------


def _complete_onboarding(request):
    """
    Marks the user as onboarded and cleans up the session key.
    Called from Step 2 on both save and skip.
    """
    request.user.is_onboarded = True
    request.user.save(update_fields=["is_onboarded"])
    # Clean up — no longer needed after onboarding completes
    request.session.pop("onboarding_owner_id", None)


# ---------------------------------------------------------------------------
# Profile — View
# ---------------------------------------------------------------------------


@login_required
def owner_profile(request):
    # Role check
    guard = _require_pet_owner(request)
    if guard:
        return guard

    # Fetch the owner record — if missing, something went wrong in onboarding
    try:
        pet_owner = PetOwner.objects.get(user=request.user)
    except PetOwner.DoesNotExist:
        # Safety net — reset onboarding flag and send them back through
        request.user.is_onboarded = False
        request.user.save(update_fields=["is_onboarded"])
        return redirect("owner_onboarding_step1")

    contact_links = pet_owner.contact_links.all()
    preference = request.user.notification_preference

    return render(
        request,
        "owner/profile/index.html",
        {
            "pet_owner": pet_owner,
            "contact_links": contact_links,
            "preference": preference,
        },
    )


# ---------------------------------------------------------------------------
# Profile — Edit
# ---------------------------------------------------------------------------


@login_required
def owner_profile_edit(request):
    # Role check
    guard = _require_pet_owner(request)
    if guard:
        return guard

    try:
        pet_owner = PetOwner.objects.get(user=request.user)
    except PetOwner.DoesNotExist:
        request.user.is_onboarded = False
        request.user.save(update_fields=["is_onboarded"])
        return redirect("owner_onboarding_step1")

    if request.method == "POST":
        profile_form = PetOwnerProfileForm(
            request.POST,
            request.FILES,
            instance=pet_owner,
        )
        link_formset = ContactLinkFormSet(request.POST, prefix="links")

        if profile_form.is_valid() and link_formset.is_valid():
            profile_form.save()

            # Handle contact links:
            # Delete all existing links and replace with new ones.
            # Simpler and safer than trying to match and update individual rows.
            pet_owner.contact_links.all().delete()
            for form in link_formset:
                if form.is_valid() and not form.is_empty():
                    link = form.save(commit=False)
                    link.owner = pet_owner
                    link.save()

            messages.success(request, "Profile updated successfully.")
            return redirect("owner_profile")

    else:
        profile_form = PetOwnerProfileForm(instance=pet_owner)
        # Pre-populate formset with existing contact links
        existing_links = pet_owner.contact_links.all()
        initial_data = [
            {"platform": link.platform, "url_or_handle": link.url_or_handle}
            for link in existing_links
        ]
        link_formset = ContactLinkFormSet(prefix="links", initial=initial_data)

    return render(
        request,
        "owner/profile/edit.html",
        {
            "profile_form": profile_form,
            "link_formset": link_formset,
            "pet_owner": pet_owner,
        },
    )


# ---------------------------------------------------------------------------
# Pets — List
# ---------------------------------------------------------------------------


@login_required
def owner_pet_list(request):
    guard = _require_pet_owner(request)
    if guard:
        return guard

    pet_owner = get_object_or_404(PetOwner, user=request.user)

    # Only show active (non-archived) pets
    pets = Pet.objects.filter(owner=pet_owner, is_archived=False)

    # Check which pets have a pending deletion request
    # Used in template to show the "Pending deletion" badge
    pending_deletion_pks = set(
        PetDeletionRequest.objects.filter(
            pet__in=pets,
            status=PetDeletionRequest.PENDING,
        ).values_list("pet_id", flat=True)
    )

    return render(
        request,
        "owner/pets/list.html",
        {
            "pets": pets,
            "pending_deletion_pks": pending_deletion_pks,
        },
    )


# ---------------------------------------------------------------------------
# Pets — Detail
# ---------------------------------------------------------------------------


@login_required
def owner_pet_detail(request, pk):
    guard = _require_pet_owner(request)
    if guard:
        return guard

    pet_owner = get_object_or_404(PetOwner, user=request.user)
    pet = get_object_or_404(Pet, pk=pk, owner=pet_owner, is_archived=False)

    pending_deletion = PetDeletionRequest.objects.filter(
        pet=pet,
        status=PetDeletionRequest.PENDING,
    ).first()

    # Medical history — public notes only, no private_notes in context
    medical_records = MedicalRecord.objects.filter(
        pet=pet,
    ).order_by("-record_date")

    vaccinations = Vaccination.objects.filter(
        pet=pet,
    ).order_by("-date_administered")

    return render(
        request,
        "owner/pets/detail.html",
        {
            "pet": pet,
            "pending_deletion": pending_deletion,
            "medical_records": medical_records,
            "vaccinations": vaccinations,
        },
    )


# ---------------------------------------------------------------------------
# Pets — Add
# ---------------------------------------------------------------------------


@login_required
def owner_pet_add(request):
    guard = _require_pet_owner(request)
    if guard:
        return guard

    pet_owner = get_object_or_404(PetOwner, user=request.user)

    if request.method == "POST":
        form = PetForm(request.POST, request.FILES)
        if form.is_valid():
            pet = form.save(commit=False)
            pet.owner = pet_owner
            pet.save()
            messages.success(request, f"{pet.name} has been added.")
            return redirect("owner_pet_detail", pk=pet.pk)

    else:
        form = PetForm()

    return render(
        request,
        "owner/pets/add.html",
        {
            "form": form,
        },
    )


# ---------------------------------------------------------------------------
# Pets — Edit
# ---------------------------------------------------------------------------


@login_required
def owner_pet_edit(request, pk):
    guard = _require_pet_owner(request)
    if guard:
        return guard

    pet_owner = get_object_or_404(PetOwner, user=request.user)
    pet = get_object_or_404(Pet, pk=pk, owner=pet_owner, is_archived=False)

    # Block editing if a deletion request is pending
    pending_deletion = PetDeletionRequest.objects.filter(
        pet=pet,
        status=PetDeletionRequest.PENDING,
    ).first()
    if pending_deletion:
        messages.warning(
            request,
            "You cannot edit a pet while a deletion request is pending.",
        )
        return redirect("owner_pet_detail", pk=pet.pk)

    if request.method == "POST":
        form = PetForm(request.POST, request.FILES, instance=pet)
        if form.is_valid():
            form.save()
            messages.success(request, f"{pet.name} has been updated.")
            return redirect("owner_pet_detail", pk=pet.pk)

    else:
        form = PetForm(instance=pet)

    return render(
        request,
        "owner/pets/edit.html",
        {
            "form": form,
            "pet": pet,
        },
    )


# ---------------------------------------------------------------------------
# Pets — Deletion Request
# ---------------------------------------------------------------------------


@login_required
def owner_pet_delete_request(request, pk):
    guard = _require_pet_owner(request)
    if guard:
        return guard

    pet_owner = get_object_or_404(PetOwner, user=request.user)
    pet = get_object_or_404(Pet, pk=pk, owner=pet_owner, is_archived=False)

    # Prevent duplicate pending requests
    existing_request = PetDeletionRequest.objects.filter(
        pet=pet,
        status=PetDeletionRequest.PENDING,
    ).first()
    if existing_request:
        messages.warning(
            request,
            "A deletion request for this pet is already pending.",
        )
        return redirect("owner_pet_detail", pk=pet.pk)

    if request.method == "POST":
        form = PetDeletionRequestForm(request.POST)
        if form.is_valid():
            deletion_request = form.save(commit=False)
            deletion_request.pet = pet
            deletion_request.requested_by = request.user
            deletion_request.status = PetDeletionRequest.PENDING
            deletion_request.save()
            messages.success(
                request,
                f"Deletion request for {pet.name} has been submitted. "
                "The clinic will review your request.",
            )
            return redirect("owner_pet_detail", pk=pet.pk)

    else:
        form = PetDeletionRequestForm()

    return render(
        request,
        "owner/pets/delete_request.html",
        {
            "form": form,
            "pet": pet,
        },
    )


# ---------------------------------------------------------------------------
# Pets — Cancel Deletion Request
# ---------------------------------------------------------------------------


@login_required
def owner_pet_cancel_delete_request(request, pk):
    guard = _require_pet_owner(request)
    if guard:
        return guard

    pet_owner = get_object_or_404(PetOwner, user=request.user)
    pet = get_object_or_404(Pet, pk=pk, owner=pet_owner, is_archived=False)

    # Only allow cancelling PENDING requests
    deletion_request = get_object_or_404(
        PetDeletionRequest,
        pet=pet,
        requested_by=request.user,
        status=PetDeletionRequest.PENDING,
    )

    # Only process on POST — prevents accidental cancellation via GET
    if request.method == "POST":
        deletion_request.delete()
        messages.success(
            request,
            f"Deletion request for {pet.name} has been cancelled.",
        )

    return redirect("owner_pet_detail", pk=pet.pk)


def _require_admin(request):
    """
    Shared guard for all admin views.
    Returns a redirect response if the user is not admin, or None if ok.
    """
    if request.user.role != "admin":
        return redirect("owner_dashboard")
    return None


def _refuse_archived(request, owner, message):
    """Redirect to the owner page with message when the owner is archived,
    or None if ok."""
    if owner.is_archived:
        messages.error(request, message)
        return redirect("admin_owner_detail", pk=owner.pk)
    return None


def _send_claim_link(request, user):
    """Sends the set-password link. Returns False when sending fails; the
    error goes to the log, never to the page."""
    try:
        send_claim_email(request, user)
    except Exception:
        logger.exception("Claim email not sent to user %s", user.pk)
        return False
    return True


# ---------------------------------------------------------------------------
# Admin — Pet Owner List
# ---------------------------------------------------------------------------


@login_required
def admin_owner_list(request):
    guard = _require_admin(request)
    if guard:
        return guard

    return render(
        request,
        "admin/pets/owners_list.html",
        owner_list_context(request.GET),
    )


# ---------------------------------------------------------------------------
# Admin — Pet Owner Detail
# ---------------------------------------------------------------------------


@login_required
def admin_owner_detail(request, pk):
    guard = _require_admin(request)
    if guard:
        return guard

    owner = get_object_or_404(PetOwner.objects.select_related("user"), pk=pk)
    pets = Pet.objects.filter(owner=owner, is_archived=False)
    archived_pets = Pet.objects.filter(owner=owner, is_archived=True)
    contact_links = owner.contact_links.all()

    # Pending deletion requests for this owner's pets
    pending_deletions = PetDeletionRequest.objects.filter(
        pet__owner=owner,
        status=PetDeletionRequest.PENDING,
    ).select_related("pet")

    return render(
        request,
        "admin/pets/owner_detail.html",
        {
            "owner": owner,
            "pets": pets,
            "active_pets": pets,
            "archived_pets": archived_pets,
            "contact_links": contact_links,
            "pending_deletions": pending_deletions,
            **owner_detail_context(owner, timezone.localdate()),
        },
    )


# ---------------------------------------------------------------------------
# Admin — Add owner (no booking)
# ---------------------------------------------------------------------------


def _save_new_owner(request, form):
    """
    Saves the owner (User + PetOwner, one transaction), then sends the
    set-password link after the commit when there is an email. Returns the
    owner, or None when nothing was saved (plain message on the page,
    details in the log).
    """
    email = form.cleaned_data.get("email") or ""
    try:
        owner = create_owner(form)
    except IntegrityError:
        # The atomic block rolled back every write
        logger.exception("Owner not saved (integrity error)")
        messages.error(request, WALKIN_SAVE_FAILED if email else WALKIN_NO_EMAIL_TAKEN)
        return None
    except Exception:
        logger.exception("Owner not saved")
        messages.error(request, WALKIN_SAVE_FAILED)
        return None

    if not email:
        messages.success(request, f"{owner.full_name} added.")
    elif _send_claim_link(request, owner.user):
        messages.success(
            request,
            f"{owner.full_name} added. We sent a link to {email} "
            "so they can set a password.",
        )
    else:
        messages.success(request, f"{owner.full_name} added.")
        messages.warning(request, CLAIM_LINK_NOT_SENT)
    return owner


@login_required
def admin_owner_create(request):
    """
    Add owner from the owners list: a User + PetOwner with no pet and no
    appointment (the walk-in New owner page is for booking a visit).
    """
    guard = _require_admin(request)
    if guard:
        return guard

    form = AdminPetOwnerForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        owner = _save_new_owner(request, form)
        if owner:
            return redirect("admin_owner_detail", pk=owner.pk)

    return render(
        request,
        "admin/pets/owner_form.html",
        {
            "form": form,
            "form_title": "Add owner",
            "submit_label": "Add owner",
        },
    )


@login_required
def admin_address_barangays(request):
    """
    HTMX: the Barangay <option> list for ?town= (the owner form's Town
    select sends its own value as ?address_town=, which works too).
    A plain GET gets the same partial.
    """
    guard = _require_admin(request)
    if guard:
        return guard

    town = request.GET.get("town", request.GET.get("address_town", ""))
    return render(
        request,
        "admin/pets/_barangay_options.html",
        {"options": barangay_choices(town)},
    )


@login_required
def admin_pet_breed_options(request):
    """
    HTMX: the Breed <option> list for ?species= (the pet form's Species
    select sends its own value as ?species_choice=, which works too).
    A plain GET gets the same partial.
    """
    guard = _require_admin(request)
    if guard:
        return guard

    species = request.GET.get("species", request.GET.get("species_choice", ""))
    return render(
        request,
        "admin/pets/_breed_options.html",
        {"options": breed_choices(species)},
    )


# ---------------------------------------------------------------------------
# Admin — Edit Pet Owner
# ---------------------------------------------------------------------------


def _save_owner_email(request, user, new_email):
    """
    Saves the email from the owner edit form to the User (it's not on
    PetOwner). A first email activates the account and sends the
    set-password link; the user is saved before sending, so a send error
    can't lose the email.
    """
    old_email = user.email
    if new_email == old_email:
        return
    user.email = new_email
    if not old_email:
        user.is_active = True
    user.save()
    if not old_email and not _send_claim_link(request, user):
        messages.warning(request, CLAIM_LINK_NOT_SENT)


@login_required
def admin_owner_edit(request, pk):
    guard = _require_admin(request)
    if guard:
        return guard

    owner = get_object_or_404(PetOwner, pk=pk)
    refused = _refuse_archived(
        request, owner, "Restore the account first to change their details."
    )
    if refused:
        return refused

    # A failed POST writes the typed values onto the form's instance; the
    # header, Cancel link and photo show the saved owner from this copy
    saved_owner = copy.copy(owner)

    if request.method == "POST":
        form = AdminPetOwnerForm(
            request.POST,
            request.FILES,
            instance=owner,
            current_user=owner.user,  # passed to __init__ for email uniqueness check
        )
        if form.is_valid():
            form.save()
            _save_owner_email(request, owner.user, form.cleaned_data["email"])
            messages.success(
                request,
                f"{owner.full_name}'s details have been updated.",
            )
            return redirect("admin_owner_detail", pk=owner.pk)
    else:
        form = AdminPetOwnerForm(
            instance=owner,
            current_user=owner.user,  # pre-populates email field
        )

    return render(
        request,
        "admin/pets/owner_form.html",
        {
            "form": form,
            "owner": saved_owner,
            "form_title": "Edit details",
            "submit_label": "Save changes",
        },
    )


# ---------------------------------------------------------------------------
# Admin — Archive Pet Owner
# ---------------------------------------------------------------------------


@login_required
def admin_owner_archive(request, pk):
    guard = _require_admin(request)
    if guard:
        return guard

    owner = get_object_or_404(PetOwner, pk=pk, is_archived=False)

    if request.method == "POST":
        owner.archive()
        messages.success(
            request,
            f"{owner.full_name}'s account has been archived.",
        )
        return redirect("admin_owner_list")

    return redirect("admin_owner_detail", pk=owner.pk)


# ---------------------------------------------------------------------------
# Admin — Restore Pet Owner
# ---------------------------------------------------------------------------


@login_required
def admin_owner_restore(request, pk):
    guard = _require_admin(request)
    if guard:
        return guard

    owner = get_object_or_404(PetOwner, pk=pk, is_archived=True)

    if request.method == "POST":
        owner.restore()
        message = f"{owner.full_name}'s account has been restored."
        if not owner.user.email:
            message += (
                " They still can't log in until they have an email"
                " and set a password."
            )
        messages.success(request, message)
        return redirect("admin_owner_detail", pk=owner.pk)

    return redirect("admin_owner_detail", pk=owner.pk)


# ---------------------------------------------------------------------------
# Admin — email claim
# ---------------------------------------------------------------------------


@login_required
def admin_owner_add_email(request, pk):
    """
    Adds an email address to a walk-in owner account that was created
    without one. Activates the account and sends the claim email.

    Only accessible when owner.user.email is blank and the owner is not
    archived.
    URL: /admin/pets/owners/<pk>/add-email/
    """
    guard = _require_admin(request)
    if guard:
        return guard

    owner = get_object_or_404(PetOwner, pk=pk)
    refused = _refuse_archived(
        request, owner, "Restore the account first to add an email."
    )
    if refused:
        return refused

    # Guard: if email already exists, nothing to do here
    if owner.user.email:
        messages.info(request, "This account already has an email address.")
        return redirect("admin_owner_detail", pk=pk)

    form = AdminOwnerEmailForm(
        request.POST or None,
        current_user=owner.user,
    )

    if request.method == "POST" and form.is_valid():
        email = form.cleaned_data["email"]

        # Update the User record before sending, so a send error keeps it
        owner.user.email = email
        owner.user.is_active = True  # Activate — was inactive without email
        owner.user.save()

        if _send_claim_link(request, owner.user):
            messages.success(
                request,
                f"Email added for {owner.full_name}. We sent a link to "
                f"{email} so they can set a password.",
            )
        else:
            messages.warning(request, CLAIM_LINK_NOT_SENT)
        return redirect("admin_owner_detail", pk=pk)

    return render(
        request,
        "admin/pets/owner_add_email.html",
        {
            "owner": owner,
            "form": form,
        },
    )


# ---------------------------------------------------------------------------
# Admin — resend the set-password link
# ---------------------------------------------------------------------------


@login_required
def admin_owner_resend_link(request, pk):
    """
    Sends the set-password (claim) link again, for an owner who has an
    email but has not set a password yet. POST only; a GET sends nothing.

    URL: /admin/pets/owners/<pk>/resend-link/
    """
    guard = _require_admin(request)
    if guard:
        return guard

    owner = get_object_or_404(PetOwner.objects.select_related("user"), pk=pk)
    if request.method != "POST":
        return redirect("admin_owner_detail", pk=owner.pk)

    user = owner.user
    if owner_account_state(owner) != "unclaimed":
        messages.info(request, "This owner doesn't need a new link.")
    elif _send_claim_link(request, user):
        messages.success(request, f"Sent the link again to {user.email}.")
    else:
        messages.warning(
            request,
            "The email couldn't be sent. Check the address, or try again later.",
        )
    return redirect("admin_owner_detail", pk=owner.pk)


# ---------------------------------------------------------------------------
# Admin — Add Pet (to existing owner)
# ---------------------------------------------------------------------------


@login_required
def admin_pet_add(request, owner_pk):
    """
    Admin manually adds a pet to an existing pet owner account.
    Redirects to the owner detail page after successful creation, or back
    to walk-in step 2 with the new pet chosen when ?return=walkin.

    URL: /admin/pets/add/<owner_pk>/
    """
    guard = _require_admin(request)
    if guard:
        return guard

    owner = get_object_or_404(PetOwner, pk=owner_pk)
    refused = _refuse_archived(
        request, owner, "Restore the account first to add a pet."
    )
    if refused:
        return refused

    # A fixed value, never a URL. The form posts back to the same URL and
    # carries a hidden "return" field, so it survives a failed POST.
    return_to = request.POST.get("return") or request.GET.get("return")
    return_to = "walkin" if return_to == "walkin" else ""

    if request.method == "POST":
        form = AdminPetForm(request.POST, request.FILES)
        if form.is_valid():
            pet = form.save(commit=False)
            pet.owner = owner
            pet.save()
            if return_to == "walkin":
                messages.success(request, f"{pet.name} added.")
                query = urlencode({"owner": owner.pk, "pet": pet.pk})
                return redirect(f"{reverse('admin_walkin_appointment_visit')}?{query}")
            messages.success(
                request,
                f"{pet.name} has been added to {owner.full_name}'s account.",
            )
            return redirect("admin_owner_detail", pk=owner.pk)
    else:
        form = AdminPetForm()

    return _render_pet_form(request, form, owner, return_to=return_to)


def _render_pet_form(request, form, owner, pet=None, return_to=""):
    """Add pet (pet=None) or Edit pet, with the back / Cancel target:
    the owner page, walk-in step 2 (?return=walkin) or the pet page."""
    if pet:
        back_url = reverse("admin_pet_detail", args=[pet.pk])
        back_label = f"Back to {pet.name}"
    elif return_to == "walkin":
        query = urlencode({"owner": owner.pk})
        back_url = f"{reverse('admin_walkin_appointment_visit')}?{query}"
        back_label = "Back to the walk-in"
    else:
        back_url = reverse("admin_owner_detail", args=[owner.pk])
        back_label = f"Back to {owner.full_name}"

    return render(
        request,
        "admin/pets/pet_form.html",
        {
            "form": form,
            "owner": owner,
            "pet": pet,
            "is_add": pet is None,
            "return_to": return_to,
            "return_to_walkin": return_to == "walkin",
            "back_url": back_url,
            "back_label": back_label,
        },
    )


# ---------------------------------------------------------------------------
# Admin — Pet List
# ---------------------------------------------------------------------------


@login_required
def admin_pet_list(request):
    guard = _require_admin(request)
    if guard:
        return guard

    return render(
        request,
        "admin/pets/pets_list.html",
        pet_list_context(request.GET),
    )


# ---------------------------------------------------------------------------
# Admin — Pet Detail
# ---------------------------------------------------------------------------


def _render_pet_detail(request, pet, archive_form=None):
    """
    The pet page. Shared by admin_pet_detail and admin_pet_archive, which
    passes its bound form so the archive dialog reopens with the errors.
    """
    return render(
        request,
        "admin/pets/pet_detail.html",
        {
            "pet": pet,
            # Old keys (every record / vaccination); lazy, so unused ones cost nothing
            "medical_records": MedicalRecord.objects.filter(pet=pet).order_by(
                "-record_date"
            ),
            "vaccinations": Vaccination.objects.filter(pet=pet).order_by(
                "-date_administered"
            ),
            "archive_form": archive_form or AdminPetArchiveForm(),
            **pet_detail_context(pet, timezone.localdate()),
        },
    )


@login_required
def admin_pet_detail(request, pk):
    guard = _require_admin(request)
    if guard:
        return guard

    pet = get_object_or_404(Pet.objects.select_related("owner", "owner__user"), pk=pk)
    return _render_pet_detail(request, pet)


# ---------------------------------------------------------------------------
# Admin — Archive Pet (no owner request)
# ---------------------------------------------------------------------------


@login_required
def admin_pet_archive(request, pk):
    """
    Archives a pet directly, with a reason (e.g. the owner said at the
    counter that the pet passed away). Saved as an already-approved
    PetDeletionRequest made by the clinic. POST only; a bad form reopens
    the dialog on the pet page.

    URL: /admin/pets/<pk>/archive/
    """
    guard = _require_admin(request)
    if guard:
        return guard

    pet = get_object_or_404(Pet.objects.select_related("owner", "owner__user"), pk=pk)
    if request.method != "POST":
        return redirect("admin_pet_detail", pk=pet.pk)

    refusal = pet_archive_refusal(pet)
    if refusal:
        messages.error(request, refusal)
        return redirect("admin_pet_detail", pk=pet.pk)

    form = AdminPetArchiveForm(request.POST)
    if not form.is_valid():
        return _render_pet_detail(request, pet, archive_form=form)

    archive_pet_directly(
        pet,
        request.user,
        form.cleaned_data["reason"],
        form.cleaned_data["reason_detail"],
    )
    messages.success(
        request, f"Archived {pet.name}. {pet.owner.full_name} was notified."
    )
    return redirect("admin_pet_detail", pk=pet.pk)


# ---------------------------------------------------------------------------
# Admin — Restore Pet
# ---------------------------------------------------------------------------


@login_required
def admin_pet_restore(request, pk):
    """
    Restores a pet archived on its own (after a deletion request). A pet
    whose owner is archived comes back with the owner instead. The old
    deletion requests stay as they are (history). POST only.

    URL: /admin/pets/<pk>/restore/
    """
    guard = _require_admin(request)
    if guard:
        return guard

    pet = get_object_or_404(Pet.objects.select_related("owner"), pk=pk)
    if request.method != "POST":
        return redirect("admin_pet_detail", pk=pet.pk)

    refusal = pet_restore_refusal(pet)
    if refusal:
        messages.error(request, refusal)
    else:
        pet.is_archived = False
        pet.archived_at = None
        pet.save()
        messages.success(request, f"Restored {pet.name}.")
    return redirect("admin_pet_detail", pk=pet.pk)


# ---------------------------------------------------------------------------
# Admin — Edit Pet
# ---------------------------------------------------------------------------


@login_required
def admin_pet_edit(request, pk):
    guard = _require_admin(request)
    if guard:
        return guard

    pet = get_object_or_404(Pet.objects.select_related("owner"), pk=pk)
    refusal = archived_pet_refusal(pet)
    if refusal:
        messages.error(request, refusal)
        return redirect("admin_pet_detail", pk=pet.pk)

    # A failed POST writes the typed values onto the form's instance; the
    # header, back link and photo show the saved pet from this copy
    saved_pet = copy.copy(pet)

    if request.method == "POST":
        form = AdminPetForm(request.POST, request.FILES, instance=pet)
        if form.is_valid():
            form.save()
            messages.success(
                request,
                f"{pet.name} has been updated.",
            )
            return redirect("admin_pet_detail", pk=pet.pk)
    else:
        form = AdminPetForm(instance=pet)

    return _render_pet_form(request, form, pet.owner, pet=saved_pet)


# ---------------------------------------------------------------------------
# Admin — Deletion Requests List
# ---------------------------------------------------------------------------


@login_required
def admin_deletion_requests(request):
    guard = _require_admin(request)
    if guard:
        return guard

    return render(
        request,
        "admin/pets/deletion_requests.html",
        deletion_requests_context(request.GET),
    )


# ---------------------------------------------------------------------------
# Admin — Answer a Deletion Request ("Archive" approves, "Keep" rejects)
# ---------------------------------------------------------------------------


def _answer_deletion(request, pk, approve):
    """
    Shared by Archive and Keep. POST only; refused unless the request is
    still pending (Archive also needs an active pet and owner). Goes back
    to the pet page when the POST field return_to is "pet", else to the
    requests page (a fixed choice, never a URL).
    """
    deletion_request = get_object_or_404(
        PetDeletionRequest.objects.select_related("pet__owner__user"), pk=pk
    )
    pet = deletion_request.pet
    if request.method != "POST":
        return redirect("admin_deletion_requests")

    refusal = deletion_answer_refusal(deletion_request, approve)
    if refusal:
        messages.error(request, refusal)
    else:
        answer_deletion_request(deletion_request, request.user, approve)
        verb = "Archived" if approve else "Kept"
        messages.success(
            request, f"{verb} {pet.name}. {pet.owner.full_name} was notified."
        )

    if request.POST.get("return_to") == "pet":
        return redirect("admin_pet_detail", pk=pet.pk)
    return redirect("admin_deletion_requests")


@login_required
def admin_approve_deletion(request, pk):
    guard = _require_admin(request)
    if guard:
        return guard
    return _answer_deletion(request, pk, approve=True)


@login_required
def admin_reject_deletion(request, pk):
    guard = _require_admin(request)
    if guard:
        return guard
    return _answer_deletion(request, pk, approve=False)
