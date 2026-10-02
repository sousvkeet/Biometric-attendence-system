from django.contrib import admin

from .models import ActivityLog, SystemSetting

admin.site.register(SystemSetting)


@admin.register(ActivityLog)
class ActivityLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "user", "action", "target")
    search_fields = ("action", "target", "details")
