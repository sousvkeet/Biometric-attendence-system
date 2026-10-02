from django.urls import path

from . import views

app_name = "employees"

urlpatterns = [
    path("", views.employee_list, name="list"),
    path("new/", views.employee_form, name="create"),
    path("deactivate-stale/", views.deactivate_stale, name="deactivate_stale"),
    path("bulk-delete/", views.employee_bulk_delete, name="bulk_delete"),
    path("removed/", views.removed_list, name="removed"),
    path("removed/<int:pk>/restore/", views.removed_restore, name="restore"),
    path("<int:pk>/delete/", views.employee_delete, name="delete"),
    path("<int:pk>/", views.employee_detail, name="detail"),
    path("<int:pk>/edit/", views.employee_form, name="edit"),
    path("<int:pk>/push/", views.employee_push, name="push"),
    path("<int:pk>/enroll/", views.employee_enroll, name="enroll"),
    path("<int:pk>/login/", views.employee_login, name="login"),
]
