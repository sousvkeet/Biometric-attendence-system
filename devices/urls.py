from django.urls import path

from . import views

app_name = "devices"

urlpatterns = [
    path("", views.device_list, name="list"),
    path("new/", views.device_form, name="create"),
    path("<int:pk>/", views.device_detail, name="detail"),
    path("<int:pk>/edit/", views.device_form, name="edit"),
    path("<int:pk>/action/<str:action>/", views.device_action, name="action"),
    path("<int:pk>/delete/", views.device_delete, name="delete"),
    path("<int:pk>/reboot/", views.device_reboot, name="reboot"),
    path("<int:pk>/clear-attendance/", views.device_clear_attendance, name="clear_attendance"),
    path("<int:pk>/simulate-punch/", views.simulate_punch, name="simulate_punch"),
    path("maintenance/<str:action>/", views.devices_maintenance, name="maintenance"),
    path("sync/", views.sync_center, name="sync_center"),
    path("sync/<str:action>/", views.sync_action, name="sync_action"),
    path("jobs/", views.job_list, name="jobs"),
    path("jobs/<int:pk>/", views.job_detail, name="job_detail"),
    path("jobs/<int:pk>/status.json", views.job_status, name="job_status"),
    path("jobs/<int:pk>/cancel/", views.job_cancel, name="job_cancel"),
]
