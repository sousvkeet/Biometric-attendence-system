from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

from attendance import selfservice
from attendance.views import holiday_crud, leavetype_crud, shift_crud
from core import views as core_views
from core.login import ProtectedLoginView
from employees.views import department_crud, designation_crud

admin.site.site_header = "Biometric Attendance - Database admin"

urlpatterns = [
    path("", core_views.home, name="home"),
    path("setup/", core_views.first_run, name="first_run"),
    path("login/", ProtectedLoginView.as_view(), name="login"),
    path("logout/", core_views.logout_page, name="logout_page"),
    path("password/", auth_views.PasswordChangeView.as_view(success_url="/"), name="password_change"),
    path("dashboard/", core_views.dashboard, name="dashboard"),
    path("dashboard/live.json", core_views.live_feed, name="live_feed"),
    path("live/", core_views.live_monitor, name="live_monitor"),
    path("settings/", core_views.settings_view, name="settings"),
    path("activity/", core_views.activity_log, name="activity_log"),
    path("employees/", include("employees.urls")),
    path("devices/", include("devices.urls")),
    path("attendance/", include("attendance.urls")),
    path("me/", include((selfservice.urlpatterns, "selfservice"))),
    path("backups/", include("backups.urls")),
    path("django-admin/", admin.site.urls),
]
urlpatterns += department_crud.urls("setup/departments")
urlpatterns += designation_crud.urls("setup/designations")
urlpatterns += shift_crud.urls("setup/shifts")
urlpatterns += holiday_crud.urls("setup/holidays")
urlpatterns += leavetype_crud.urls("setup/leave-types")
