# pets/views.py

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import send_mail
from django.db.models import Q
from django.shortcuts import (
    get_object_or_404,
    redirect,
    render,
)
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from .forms import (
    AdminOwnerEmailForm,
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

    return render(
        request,
        "owner/profile/index.html",
        {
            "pet_owner": pet_owner,
            "contact_links": contact_links,
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

    # Ensure the pet belongs to this owner
    pet = get_object_or_404(Pet, pk=pk, owner=pet_owner, is_archived=False)

    # Check for a pending deletion request on this pet
    pending_deletion = PetDeletionRequest.objects.filter(
        pet=pet,
        status=PetDeletionRequest.PENDING,
    ).first()

    return render(
        request,
        "owner/pets/detail.html",
        {
            "pet": pet,
            "pending_deletion": pending_deletion,
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


# ---------------------------------------------------------------------------
# Admin — Pet Owner List
# ---------------------------------------------------------------------------


@login_required
def admin_owner_list(request):
    guard = _require_admin(request)
    if guard:
        return guard

    # Search and filter
    search_query = request.GET.get("search", "").strip()
    status_filter = request.GET.get("status", "active")

    owners = PetOwner.objects.select_related("user").all()

    # Apply status filter
    if status_filter == "archived":
        owners = owners.filter(is_archived=True)
    else:
        # Default to active
        owners = owners.filter(is_archived=False)

    # Apply search — matches first name, last name, or email
    if search_query:
        owners = owners.filter(
            Q(first_name__icontains=search_query)
            | Q(last_name__icontains=search_query)
            | Q(user__email__icontains=search_query)
        )

    return render(
        request,
        "admin/pets/owners_list.html",
        {
            "owners": owners,
            "search_query": search_query,
            "status_filter": status_filter,
            "total_count": owners.count(),
        },
    )


# ---------------------------------------------------------------------------
# Admin — Pet Owner Detail
# ---------------------------------------------------------------------------


@login_required
def admin_owner_detail(request, pk):
    guard = _require_admin(request)
    if guard:
        return guard

    owner = get_object_or_404(PetOwner, pk=pk)
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
            "archived_pets": archived_pets,
            "contact_links": contact_links,
            "pending_deletions": pending_deletions,
        },
    )


# ---------------------------------------------------------------------------
# Admin — Create Pet Owner
# ---------------------------------------------------------------------------


@login_required
def admin_owner_create(request):
    guard = _require_admin(request)
    if guard:
        return guard

    if request.method == "POST":
        form = AdminPetOwnerForm(request.POST, request.FILES)
        if form.is_valid():
            # Note: this creates a PetOwner record only.
            # Full walk-in flow (User account + appointment) is in Phase 2.5.
            # For now Admin can create the profile details only.
            owner = form.save(commit=False)
            owner.save()
            messages.success(
                request,
                f"{owner.full_name} has been added.",
            )
            return redirect("admin_owner_detail", pk=owner.pk)
    else:
        form = AdminPetOwnerForm()

    return render(
        request,
        "admin/pets/owner_form.html",
        {
            "form": form,
            "form_title": "Add Pet Owner",
            "submit_label": "Create Owner",
        },
    )


# ---------------------------------------------------------------------------
# Admin — Edit Pet Owner
# ---------------------------------------------------------------------------


@login_required
def admin_owner_edit(request, pk):
    guard = _require_admin(request)
    if guard:
        return guard

    owner = get_object_or_404(PetOwner, pk=pk)

    if request.method == "POST":
        form = AdminPetOwnerForm(
            request.POST,
            request.FILES,
            instance=owner,
            current_user=owner.user,  # passed to __init__ for email uniqueness check
        )
        if form.is_valid():
            form.save()

            # Save email to User model separately — it's not on PetOwner
            new_email = form.cleaned_data.get("email", "").strip()
            old_email = owner.user.email

            if new_email != old_email:
                owner.user.email = new_email
                # If email is being added for the first time, activate the account
                if not old_email and new_email:
                    owner.user.is_active = True
                    _send_claim_email_for_owner(request, owner.user)
                owner.user.save()

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
            "owner": owner,
            "form_title": f"Edit {owner.full_name}",
            "submit_label": "Save Changes",
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
        messages.success(
            request,
            f"{owner.full_name}'s account has been restored.",
        )
        return redirect("admin_owner_detail", pk=owner.pk)

    return redirect("admin_owner_detail", pk=owner.pk)


# ---------------------------------------------------------------------------
# Admin — email claim
# ---------------------------------------------------------------------------


def admin_owner_add_email(request, pk):
    """
    Adds an email address to a walk-in owner account that was created
    without one. Activates the account and sends the claim email.

    Only accessible when owner.user.email is blank.
    URL: /admin/pets/owners/<pk>/add-email/
    """
    if not request.user.is_authenticated or request.user.role != "admin":
        return redirect("owner_dashboard")

    owner = get_object_or_404(PetOwner, pk=pk)

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

        # Update the User record
        owner.user.email = email
        owner.user.is_active = True  # Activate — was inactive without email
        owner.user.save()

        # Send claim account email
        _send_claim_email_for_owner(request, owner.user)

        messages.success(
            request,
            f"Email added for {owner.full_name}. "
            f"A claim account email has been sent to {email}.",
        )
        return redirect("admin_owner_detail", pk=pk)

    return render(
        request,
        "admin/pets/owner_add_email.html",
        {
            "owner": owner,
            "form": form,
        },
    )


def _send_claim_email_for_owner(request, user):
    """
    Sends the claim account email for walk-in owners when email is added.
    Shared by both the walk-in create flow and the add-email flow.
    """
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)

    claim_link = request.build_absolute_uri(f"/reset-password/{uid}/{token}/")

    subject = render_to_string("owner/emails/claim_account_subject.txt", {}).strip()

    body = render_to_string(
        "owner/emails/claim_account_body.txt",
        {
            "user": user,
            "claim_link": claim_link,
            "clinic_name": "Hapi Tutz Vet. Supplies",
        },
    )

    send_mail(
        subject=subject,
        message=body,
        from_email=None,
        recipient_list=[user.email],
        fail_silently=False,
    )





# ---------------------------------------------------------------------------
# Admin — Add Pet (to existing owner)
# ---------------------------------------------------------------------------

@login_required
def admin_pet_add(request, owner_pk):
    """
    Admin manually adds a pet to an existing pet owner account.
    Redirects to the owner detail page after successful creation.

    URL: /admin/pets/owners/<owner_pk>/add-pet/
    """
    guard = _require_admin(request)
    if guard:
        return guard

    owner = get_object_or_404(PetOwner, pk=owner_pk)

    if request.method == "POST":
        form = AdminPetForm(request.POST, request.FILES)
        if form.is_valid():
            pet = form.save(commit=False)
            pet.owner = owner
            pet.save()
            messages.success(
                request,
                f"{pet.name} has been added to {owner.full_name}'s account.",
            )
            return redirect("admin_owner_detail", pk=owner.pk)
    else:
        form = AdminPetForm()

    return render(
        request,
        "admin/pets/pet_form.html",
        {
            "form": form,
            "owner": owner,        # used for cancel button and context
            "pet": None,           # signals template this is a create, not edit
            "form_title": f"Add Pet for {owner.full_name}",
            "submit_label": "Add Pet",
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

    search_query = request.GET.get("search", "").strip()
    species_filter = request.GET.get("species", "").strip()

    pets = Pet.objects.select_related("owner").filter(is_archived=False)

    if search_query:
        pets = pets.filter(
            Q(name__icontains=search_query)
            | Q(owner__first_name__icontains=search_query)
            | Q(owner__last_name__icontains=search_query)
        )

    if species_filter:
        pets = pets.filter(species__icontains=species_filter)

    # Filter by owner PK when coming from owner detail page
    owner_pk = request.GET.get("owner", "").strip()
    if owner_pk:
        pets = pets.filter(owner__pk=owner_pk)

    # Get distinct species for the filter dropdown
    species_list = (
        Pet.objects.filter(is_archived=False)
        .values_list("species", flat=True)
        .distinct()
        .order_by("species")
    )

    return render(
        request,
        "admin/pets/pets_list.html",
        {
            "pets": pets,
            "search_query": search_query,
            "species_filter": species_filter,
            "species_list": species_list,
            "total_count": pets.count(),
        },
    )


# ---------------------------------------------------------------------------
# Admin — Pet Detail
# ---------------------------------------------------------------------------


@login_required
def admin_pet_detail(request, pk):
    guard = _require_admin(request)
    if guard:
        return guard

    # Admin can view archived pets too
    pet = get_object_or_404(Pet, pk=pk)

    pending_deletion = PetDeletionRequest.objects.filter(
        pet=pet,
        status=PetDeletionRequest.PENDING,
    ).first()

    return render(
        request,
        "admin/pets/pet_detail.html",
        {
            "pet": pet,
            "pending_deletion": pending_deletion,
        },
    )


# ---------------------------------------------------------------------------
# Admin — Edit Pet
# ---------------------------------------------------------------------------


@login_required
def admin_pet_edit(request, pk):
    guard = _require_admin(request)
    if guard:
        return guard

    pet = get_object_or_404(Pet, pk=pk)

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

    return render(
        request,
        "admin/pets/pet_form.html",
        {
            "form": form,
            "pet": pet,
            "form_title": f"Edit {pet.name}",
            "submit_label": "Save Changes",
        },
    )


# ---------------------------------------------------------------------------
# Admin — Deletion Requests List
# ---------------------------------------------------------------------------


@login_required
def admin_deletion_requests(request):
    guard = _require_admin(request)
    if guard:
        return guard

    # Show pending requests first, then resolved ones
    pending = (
        PetDeletionRequest.objects.filter(
            status=PetDeletionRequest.PENDING,
        )
        .select_related("pet", "pet__owner", "requested_by")
        .order_by("-created_at")
    )

    resolved = (
        PetDeletionRequest.objects.exclude(
            status=PetDeletionRequest.PENDING,
        )
        .select_related("pet", "pet__owner", "requested_by", "reviewed_by")
        .order_by("-reviewed_at")
    )

    return render(
        request,
        "admin/pets/deletion_requests.html",
        {
            "pending": pending,
            "resolved": resolved,
            "pending_count": pending.count(),
        },
    )


# ---------------------------------------------------------------------------
# Admin — Approve Deletion Request
# ---------------------------------------------------------------------------


@login_required
def admin_approve_deletion(request, pk):
    guard = _require_admin(request)
    if guard:
        return guard

    deletion_request = get_object_or_404(
        PetDeletionRequest,
        pk=pk,
        status=PetDeletionRequest.PENDING,
    )

    if request.method == "POST":
        # Soft delete the pet
        pet = deletion_request.pet
        pet.is_archived = True
        pet.archived_at = timezone.now()
        pet.save()

        # Mark request as approved
        deletion_request.status = PetDeletionRequest.APPROVED
        deletion_request.reviewed_by = request.user
        deletion_request.reviewed_at = timezone.now()
        deletion_request.save()

        messages.success(
            request,
            f"Deletion request for {pet.name} has been approved. "
            "The pet has been archived.",
        )
        return redirect("admin_deletion_requests")

    return redirect("admin_deletion_requests")


# ---------------------------------------------------------------------------
# Admin — Reject Deletion Request
# ---------------------------------------------------------------------------


@login_required
def admin_reject_deletion(request, pk):
    guard = _require_admin(request)
    if guard:
        return guard

    deletion_request = get_object_or_404(
        PetDeletionRequest,
        pk=pk,
        status=PetDeletionRequest.PENDING,
    )

    if request.method == "POST":
        deletion_request.status = PetDeletionRequest.REJECTED
        deletion_request.reviewed_by = request.user
        deletion_request.reviewed_at = timezone.now()
        deletion_request.save()

        messages.success(
            request,
            f"Deletion request for {deletion_request.pet.name} "
            "has been rejected. The pet remains active.",
        )
        return redirect("admin_deletion_requests")

    return redirect("admin_deletion_requests")
