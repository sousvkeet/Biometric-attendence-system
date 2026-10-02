from django.urls import path

from . import views

app_name = "backups"

urlpatterns = [
    path("", views.index, name="index"),
    path("now/", views.backup_now, name="now"),
    path("runs/<int:pk>/", views.run_detail, name="run"),
    path("runs/<int:pk>/status.json", views.run_status, name="run_status"),
    path("restore/", views.restore_confirm, name="restore"),
    path("restore/upload/", views.restore_upload, name="restore_upload"),
    path("destinations/new/", views.destination_form, name="destination_create"),
    path("destinations/<int:pk>/edit/", views.destination_form, name="destination_edit"),
    path("destinations/<int:pk>/test/", views.destination_test, name="destination_test"),
    path("destinations/<int:pk>/delete/", views.destination_delete, name="destination_delete"),
    path("destinations/<int:pk>/forget-key/", views.destination_forget_key, name="destination_forget_key"),
    path("destinations/<int:pk>/files/", views.destination_files, name="files"),
    path("destinations/<int:pk>/files/<str:name>/", views.destination_download, name="download"),
]
