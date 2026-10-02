from django.contrib import admin

from .models import Department, Designation, Employee, FingerprintTemplate

admin.site.register(Department)
admin.site.register(Designation)


@admin.register(Employee)
class EmployeeAdmin(admin.ModelAdmin):
    list_display = ("device_user_id", "name", "department", "status", "privilege")
    search_fields = ("device_user_id", "name")
    list_filter = ("status", "department")


@admin.register(FingerprintTemplate)
class FingerprintTemplateAdmin(admin.ModelAdmin):
    list_display = ("employee", "finger_index", "size", "source_device", "created_at")
    exclude = ("template",)
