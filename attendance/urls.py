from django.urls import path

from . import views

app_name = "attendance"

urlpatterns = [
    path("", views.attendance_list, name="list"),
    path("logs/", views.log_list, name="logs"),
    path("logs/add/", views.manual_punch, name="manual_punch"),
    path("logs/<int:pk>/delete/", views.delete_manual_punch, name="delete_punch"),
    path("daily/", views.daily_report, name="daily"),
    path("monthly/", views.monthly_register, name="monthly"),
    path("exceptions/", views.late_report, name="exceptions"),
    path("employee/<int:pk>/", views.employee_card, name="employee_card"),
    path("leaves/", views.leave_list, name="leaves"),
    path("leaves/new/", views.leave_create, name="leave_create"),
    path("leaves/<int:pk>/review/", views.leave_review, name="leave_review"),
    path("regularizations/", views.regularization_list, name="regularizations"),
    path("regularizations/<int:pk>/review/", views.regularization_review, name="regularization_review"),
]
