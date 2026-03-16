from django import template
from django.utils.timesince import timesince

register = template.Library()


@register.filter
def patcoin(value):
    """Format a number as PatCoin amount."""
    try:
        return f"{value:,.2f}"
    except (TypeError, ValueError):
        return "0.00"


@register.filter
def hash_short(value, length=12):
    """Truncate a hash for display."""
    if not value:
        return "—"
    try:
        length = int(length)
    except (TypeError, ValueError):
        length = 12
    return f"{value[:length]}..."


@register.filter
def address_short(value, length=14):
    """Truncate an address for display."""
    if not value:
        return "—"
    try:
        length = int(length)
    except (TypeError, ValueError):
        length = 14
    return f"{value[:length]}..."


@register.filter
def status_badge_class(status):
    """Return CSS class for status badge."""
    mapping = {
        "genesis": "badge-genesis",
        "pending": "badge-pending",
        "sealed": "badge-sealed",
        "confirmed": "badge-confirmed",
        "failed": "badge-failed",
    }
    return mapping.get(status, "badge-default")


@register.filter
def anchor_status_badge_class(status):
    """Return CSS class for genesis anchor trust state."""
    mapping = {
        "valid": "badge-confirmed",
        "remote_unverified": "badge-pending",
        "git_unavailable": "badge-pending",
        "anchor_missing": "badge-default",
        "mismatch": "badge-failed",
    }
    return mapping.get(status, "badge-default")


@register.filter
def anchor_status_label(status):
    """Return human-readable label for genesis anchor trust state."""
    mapping = {
        "valid": "Remote Verified",
        "remote_unverified": "Local Only",
        "git_unavailable": "Git Unavailable",
        "anchor_missing": "Anchor Missing",
        "mismatch": "Mismatch",
    }
    return mapping.get(status, "Unknown")


@register.filter
def time_ago(value):
    """Return human-readable time since."""
    if not value:
        return "—"
    return timesince(value) + " ago"
