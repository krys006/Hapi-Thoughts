# pets/urls.py

from django.urls import path

from . import views

urlpatterns = [
    # --- Onboarding ---
    path(
        "owner/onboarding/step1/",
        views.onboarding_step1,
        name="owner_onboarding_step1",
    ),
    path(
        "owner/onboarding/step2/",
        views.onboarding_step2,
        name="owner_onboarding_step2",
    ),
    # --- Profile ---
    path(
        "owner/profile/",
        views.owner_profile,
        name="owner_profile",
    ),
    path(
        "owner/profile/edit/",
        views.owner_profile_edit,
        name="owner_profile_edit",
    ),
    # --- Pet Owner: Pets ---
    path(
        "owner/pets/",
        views.owner_pet_list,
        name="owner_pet_list",
    ),
    path(
        "owner/pets/add/",
        views.owner_pet_add,
        name="owner_pet_add",
    ),
    path(
        "owner/pets/<int:pk>/",
        views.owner_pet_detail,
        name="owner_pet_detail",
    ),
    path(
        "owner/pets/<int:pk>/edit/",
        views.owner_pet_edit,
        name="owner_pet_edit",
    ),
    path(
        "owner/pets/<int:pk>/delete-request/",
        views.owner_pet_delete_request,
        name="owner_pet_delete_request",
    ),
    path(
        "owner/pets/<int:pk>/cancel-delete-request/",
        views.owner_pet_cancel_delete_request,
        name="owner_pet_cancel_delete_request",
    ),
    # --- Admin: Pet Owners ---
    path(
        "admin/pets/owners/",
        views.admin_owner_list,
        name="admin_owner_list",
    ),
    path(
        "admin/pets/owners/create/",
        views.admin_owner_create,
        name="admin_owner_create",
    ),
    path(
        "admin/pets/owners/<int:pk>/",
        views.admin_owner_detail,
        name="admin_owner_detail",
    ),
    path(
        "admin/pets/owners/<int:pk>/edit/",
        views.admin_owner_edit,
        name="admin_owner_edit",
    ),
    path(
        "admin/pets/owners/<int:pk>/archive/",
        views.admin_owner_archive,
        name="admin_owner_archive",
    ),
    path(
        "admin/pets/owners/<int:pk>/restore/",
        views.admin_owner_restore,
        name="admin_owner_restore",
    ),
    # --- Admin: Pets ---
    path(
        "admin/pets/",
        views.admin_pet_list,
        name="admin_pet_list",
    ),

    path(
        "admin/pets/add/<int:owner_pk>/",
        views.admin_pet_add,
        name="admin_pet_add",
    ),

    path(
        "admin/pets/<int:pk>/",
        views.admin_pet_detail,
        name="admin_pet_detail",
    ),
    path(
        "admin/pets/<int:pk>/edit/",
        views.admin_pet_edit,
        name="admin_pet_edit",
    ),

    # --- Admin: Deletion Requests ---
    path(
        "admin/pets/deletion-requests/",
        views.admin_deletion_requests,
        name="admin_deletion_requests",
    ),
    path(
        "admin/pets/deletion-requests/<int:pk>/approve/",
        views.admin_approve_deletion,
        name="admin_approve_deletion",
    ),
    path(
        "admin/pets/deletion-requests/<int:pk>/reject/",
        views.admin_reject_deletion,
        name="admin_reject_deletion",
    ),
    # --- Admin: email claim ---
    path(
        "admin/pets/owners/<int:pk>/add-email/",
        views.admin_owner_add_email,
        name="admin_owner_add_email",
    ),
]
