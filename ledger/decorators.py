from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse


def staff_required(view_func):
    """Require authenticated staff user. Returns 403 for non-staff."""
    @wraps(view_func)
    @login_required
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_staff:
            raise PermissionDenied
        return view_func(request, *args, **kwargs)
    return _wrapped


def rate_limit(key_prefix, max_requests, period_seconds):
    """
    Simple cache-based rate limiter keyed by client IP.
    Only limits POST requests. Returns 429 when exceeded.
    Disabled when settings.TESTING is True.
    """
    def decorator(view_func):
        @wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            from django.conf import settings as django_settings
            if getattr(django_settings, "TESTING", False):
                return view_func(request, *args, **kwargs)
            if request.method != "POST":
                return view_func(request, *args, **kwargs)
            forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
            ip = forwarded.split(",")[0].strip() if forwarded else request.META.get("REMOTE_ADDR", "")
            cache_key = f"ratelimit:{key_prefix}:{ip}"
            hits = cache.get(cache_key, 0)
            if hits >= max_requests:
                return HttpResponse(
                    "Too many requests. Please try again later.",
                    status=429,
                    content_type="text/plain",
                )
            cache.set(cache_key, hits + 1, timeout=period_seconds)
            return view_func(request, *args, **kwargs)
        return _wrapped
    return decorator
