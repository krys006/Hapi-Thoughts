from django.urls import path
from . import views

urlpatterns = [
    path("owner/dashboard/", views.owner_dashboard, name="owner_dashboard"),
    path("admin/dashboard/", views.admin_dashboard, name="admin_dashboard"),
]
