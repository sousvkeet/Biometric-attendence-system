class HealthCheckMiddleware:
    """Answers /healthz before host validation and HTTPS redirects, so load balancers and Docker health checks
    (which use an internal address over plain HTTP) always get a real answer."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path == "/healthz":
            from backups import maintenance
            from django.http import JsonResponse
            if maintenance.is_active():
                resp = JsonResponse({"status": "maintenance"}, status=503)
                resp._has_been_logged = True
                return resp
            from .views import healthz
            return healthz(request)
        return self.get_response(request)
