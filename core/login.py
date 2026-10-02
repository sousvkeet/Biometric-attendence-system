"""Login view with brute-force protection (lockout per username + IP, shared across worker processes)."""
import math
import time

from django.conf import settings
from django.contrib.auth import views as auth_views
from django.core.cache import cache


def _key(request):
    user = (request.POST.get("username") or "").strip().lower()[:150]
    return f"login-fail:{request.META.get('REMOTE_ADDR', '')}:{user}"


class ProtectedLoginView(auth_views.LoginView):
    redirect_authenticated_user = True

    def post(self, request, *args, **kwargs):
        key = _key(request)
        state = cache.get(key) or {"count": 0, "until": 0}
        if state["until"] > time.time():
            minutes = math.ceil((state["until"] - time.time()) / 60)
            form = self.get_form()
            form.is_bound = True
            form._errors = {"__all__": form.error_class([
                f"Too many failed sign-in attempts. Try again in {minutes} minute{'s' if minutes != 1 else ''}."])}
            response = self.render_to_response(self.get_context_data(form=form, locked=True))
            response.status_code = 429
            return response
        return super().post(request, *args, **kwargs)

    def form_invalid(self, form):
        key = _key(self.request)
        lock_seconds = settings.LOGIN_LOCK_MINUTES * 60
        state = cache.get(key) or {"count": 0, "until": 0}
        state["count"] += 1
        if state["count"] >= settings.LOGIN_MAX_FAILURES:
            state = {"count": 0, "until": time.time() + lock_seconds}
            from core.models import ActivityLog
            ActivityLog.objects.create(action="Login locked (too many failures)",
                                       target=(self.request.POST.get("username") or "")[:200],
                                       ip_address=self.request.META.get("REMOTE_ADDR"))
        cache.set(key, state, lock_seconds)
        return super().form_invalid(form)

    def form_valid(self, form):
        cache.delete(_key(self.request))
        return super().form_valid(form)
