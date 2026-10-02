from django.contrib import admin

from .models import Device, DeviceJob, DeviceUserLink


@admin.register(Device)
class DeviceAdmin(admin.ModelAdmin):
    list_display = ("name", "ip_address", "port", "driver", "status", "is_active", "read_only")


@admin.register(DeviceUserLink)
class DeviceUserLinkAdmin(admin.ModelAdmin):
    list_display = ("device", "employee", "uid", "fingers", "synced_at")
    list_filter = ("device",)


@admin.register(DeviceJob)
class DeviceJobAdmin(admin.ModelAdmin):
    list_display = ("pk", "job_type", "device", "status", "created_at", "finished_at")
    list_filter = ("status", "job_type")
