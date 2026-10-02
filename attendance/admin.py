from django.contrib import admin

from .models import AttendanceLog, Holiday, LeaveRequest, LeaveType, RegularizationRequest, Shift

admin.site.register(Shift)
admin.site.register(Holiday)
admin.site.register(LeaveType)
admin.site.register(LeaveRequest)
admin.site.register(RegularizationRequest)


@admin.register(AttendanceLog)
class AttendanceLogAdmin(admin.ModelAdmin):
    list_display = ("device_user_id", "employee", "punch_time", "device", "source")
    list_filter = ("source", "device")
    search_fields = ("device_user_id", "employee__name")
    date_hierarchy = "punch_time"

    def has_delete_permission(self, request, obj=None):
        # Device punches are an immutable copy of the device log.
        return obj is not None and obj.source != AttendanceLog.SOURCE_DEVICE
