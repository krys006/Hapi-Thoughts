from django.urls import path
from . import views

urlpatterns = [
    # ── Admin — Medical Records ───────────────────────────────────────────
    path(
        "admin/medical/pet/<int:pet_pk>/create/",
        views.admin_medical_record_create,
        name="admin_medical_record_create",
    ),
    path(
        "admin/medical/<int:pk>/",
        views.admin_medical_record_detail,
        name="admin_medical_record_detail",
    ),
    path(
        "admin/medical/<int:pk>/edit/",
        views.admin_medical_record_edit,
        name="admin_medical_record_edit",
    ),
    path(
        "admin/medical/<int:pk>/history/",
        views.admin_pet_medical_history,
        name="admin_pet_medical_history",
    ),

    # ── Admin — Prescription Items ────────────────────────────────────────
    path(
        "admin/medical/<int:record_pk>/prescription/add/",
        views.admin_prescription_item_add,
        name="admin_prescription_item_add",
    ),
    path(
        "admin/medical/prescription/<int:pk>/delete/",
        views.admin_prescription_item_delete,
        name="admin_prescription_item_delete",
    ),

    # ── Admin — Test Result Files ─────────────────────────────────────────
    path(
        "admin/medical/<int:record_pk>/files/upload/",
        views.admin_test_result_file_upload,
        name="admin_test_result_file_upload",
    ),
    path(
        "admin/medical/files/<int:pk>/archive/",
        views.admin_test_result_file_archive,
        name="admin_test_result_file_archive",
    ),

    # ── Admin — Vaccinations ──────────────────────────────────────────────
    path(
        "admin/medical/<int:record_pk>/vaccination/add/",
        views.admin_vaccination_add,
        name="admin_vaccination_add",
    ),
    path(
        "admin/medical/vaccination/<int:pk>/correct/",
        views.admin_vaccination_correct,
        name="admin_vaccination_correct",
    ),
    path(
        "admin/pets/<int:pet_pk>/vaccination/create/",
        views.admin_vaccination_standalone_create,
        name="admin_vaccination_standalone_create",
    ),
    path(
        "admin/pets/<int:pk>/vaccinations/",
        views.admin_pet_vaccination_history,
        name="admin_pet_vaccination_history",
    ),

    path(
    "admin/medical/vaccination/<int:pk>/edit/",
    views.admin_vaccination_edit,
    name="admin_vaccination_edit",
    ),

    # ── Pet Owner — Medical History ───────────────────────────────────────
    path(
        "owner/pets/<int:pk>/medical/",
        views.owner_pet_medical_history,
        name="owner_pet_medical_history",
    ),
    path(
        "owner/pets/<int:pk>/vaccinations/",
        views.owner_vaccination_history,
        name="owner_vaccination_history",
    ),
]