from .genesis_anchor import get_genesis_anchor_report
from .models import Block


def chain_status(request):
    """Inject chain integrity and anchor status into every authenticated page."""
    if not hasattr(request, "user") or not request.user.is_authenticated:
        return {}

    is_valid, chain_errors = Block.validate_chain()
    anchor_report = get_genesis_anchor_report()
    anchor_ok = anchor_report["status"] == "valid"
    anchor_local = anchor_report["status"] in ("remote_unverified", "git_unavailable")

    # Overall health: green only when both chain AND anchor are fully verified
    if is_valid and anchor_ok:
        health = "ok"
    elif is_valid and anchor_local:
        health = "local"
    elif not is_valid:
        health = "broken"
    else:
        health = "warning"

    return {
        "chain_health": health,
        "chain_is_valid": is_valid,
        "chain_error_count": len(chain_errors),
        "anchor_status": anchor_report["status"],
    }
