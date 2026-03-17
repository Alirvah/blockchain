from .health import (
    ensure_background_validation,
    get_cached_layout_chain_status,
    get_last_validation_completed_at,
    is_background_validation_running,
)


def chain_status(request):
    """Inject chain integrity and anchor status into every authenticated page."""
    if not hasattr(request, "user") or not request.user.is_authenticated:
        return {}

    status = get_cached_layout_chain_status(
        force_refresh=request.GET.get("refresh") == "1"
    )
    if request.GET.get("refresh") != "1" and status["anchor_status"] == "unchecked":
        ensure_background_validation()

    status = dict(status)
    if status["anchor_status"] == "unchecked" and is_background_validation_running():
        status["anchor_status"] = "validating"

    status["background_validation_running"] = is_background_validation_running()
    status["last_validation_completed_at"] = get_last_validation_completed_at()
    return status
