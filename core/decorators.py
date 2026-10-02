from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect
from django.utils.http import url_has_allowed_host_and_scheme


def safe_next(request, url, fallback):
    """`url` if it points to this site, else `fallback` (prevents open redirects)."""
    if url and url_has_allowed_host_and_scheme(url, allowed_hosts={request.get_host()},
                                               require_https=request.is_secure()):
        return url
    return fallback


def staff_required(view):
    """Admin / HR pages."""
    @wraps(view)
    @login_required
    def wrapper(request, *args, **kwargs):
        if not request.user.is_staff:
            if hasattr(request.user, "employee"):
                return redirect("selfservice:home")
            raise PermissionDenied
        return view(request, *args, **kwargs)
    return wrapper


def employee_required(view):
    """Employee self-service pages (request.employee is set)."""
    @wraps(view)
    @login_required
    def wrapper(request, *args, **kwargs):
        employee = getattr(request.user, "employee", None)
        if employee is not None and not employee.is_active:
            raise PermissionDenied("Your employee account is inactive.")
        if employee is None:
            if request.user.is_staff:
                return redirect("dashboard")
            raise PermissionDenied("Your account is not linked to an employee.")
        request.employee = employee
        return view(request, *args, **kwargs)
    return wrapper


def superuser_required(view):
    """Backups contain all personal and biometric data: superusers only."""
    @wraps(view)
    @login_required
    def wrapper(request, *args, **kwargs):
        if not request.user.is_superuser:
            raise PermissionDenied("Only the main administrator can manage backups.")
        return view(request, *args, **kwargs)
    return wrapper
