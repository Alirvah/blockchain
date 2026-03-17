import threading

from django.core.cache import cache
from django.db import close_old_connections
from django.utils import timezone

from .genesis_anchor import get_genesis_anchor_report
from .models import Block

CACHE_TTL_SECONDS = 300
CHAIN_VALIDATION_CACHE_KEY = "ledger:chain-validation:v1"
ANCHOR_REPORT_CACHE_KEY = "ledger:anchor-report:v1"
LAYOUT_STATUS_CACHE_KEY = "ledger:layout-status:v1"
VALIDATION_WARMUP_LOCK_KEY = "ledger:validation-warmup-lock:v1"
VALIDATION_WARMUP_LOCK_TIMEOUT = 30
LAST_VALIDATED_AT_CACHE_KEY = "ledger:last-validated-at:v1"


def _build_layout_status(
    chain_validation: dict[str, object],
    anchor_report: dict[str, object] | None,
) -> dict[str, object]:
    if anchor_report is None:
        health = "local" if chain_validation["is_valid"] else "broken"
        anchor_status = "unchecked"
    else:
        anchor_ok = anchor_report["status"] == "valid"
        anchor_local = anchor_report["status"] in ("remote_unverified", "git_unavailable")

        if chain_validation["is_valid"] and anchor_ok:
            health = "ok"
        elif chain_validation["is_valid"] and anchor_local:
            health = "local"
        elif not chain_validation["is_valid"]:
            health = "broken"
        else:
            health = "warning"
        anchor_status = anchor_report["status"]

    return {
        "chain_health": health,
        "chain_is_valid": chain_validation["is_valid"],
        "chain_error_count": len(chain_validation["errors"]),
        "anchor_status": anchor_status,
    }


def invalidate_chain_health_cache() -> None:
    cache.delete_many(
        [
            CHAIN_VALIDATION_CACHE_KEY,
            ANCHOR_REPORT_CACHE_KEY,
            LAYOUT_STATUS_CACHE_KEY,
            LAST_VALIDATED_AT_CACHE_KEY,
            VALIDATION_WARMUP_LOCK_KEY,
        ]
    )


def get_cached_chain_validation(force_refresh: bool = False) -> dict[str, object]:
    if force_refresh:
        cache.delete(CHAIN_VALIDATION_CACHE_KEY)

    cached = cache.get(CHAIN_VALIDATION_CACHE_KEY)
    if cached is not None:
        return cached

    is_valid, errors = Block.validate_chain()
    cached = {
        "is_valid": is_valid,
        "errors": errors,
    }
    cache.set(CHAIN_VALIDATION_CACHE_KEY, cached, CACHE_TTL_SECONDS)
    return cached


def get_last_validation_completed_at() -> str | None:
    return cache.get(LAST_VALIDATED_AT_CACHE_KEY)


def is_background_validation_running() -> bool:
    return bool(cache.get(VALIDATION_WARMUP_LOCK_KEY))


def get_cached_anchor_report(
    force_refresh: bool = False,
    allow_stale_only: bool = False,
) -> dict[str, object] | None:
    if force_refresh:
        cache.delete(ANCHOR_REPORT_CACHE_KEY)

    cached = cache.get(ANCHOR_REPORT_CACHE_KEY)
    if cached is not None:
        return cached
    if allow_stale_only:
        return None

    cached = get_genesis_anchor_report()
    cache.set(ANCHOR_REPORT_CACHE_KEY, cached, CACHE_TTL_SECONDS)
    cache.delete(LAYOUT_STATUS_CACHE_KEY)
    cache.set(LAST_VALIDATED_AT_CACHE_KEY, timezone.now().isoformat(), CACHE_TTL_SECONDS)
    return cached
def _run_background_validation_refresh() -> None:
    close_old_connections()
    try:
        is_valid, errors = Block.validate_chain()
        chain_validation = {
            "is_valid": is_valid,
            "errors": errors,
        }
        anchor_report = get_genesis_anchor_report()
        layout_status = _build_layout_status(chain_validation, anchor_report)

        cache.set(CHAIN_VALIDATION_CACHE_KEY, chain_validation, CACHE_TTL_SECONDS)
        cache.set(ANCHOR_REPORT_CACHE_KEY, anchor_report, CACHE_TTL_SECONDS)
        cache.set(LAYOUT_STATUS_CACHE_KEY, layout_status, CACHE_TTL_SECONDS)
        cache.set(LAST_VALIDATED_AT_CACHE_KEY, timezone.now().isoformat(), CACHE_TTL_SECONDS)
    finally:
        cache.delete(VALIDATION_WARMUP_LOCK_KEY)
        close_old_connections()


def ensure_background_validation() -> bool:
    if cache.get(ANCHOR_REPORT_CACHE_KEY) is not None:
        return False
    if not cache.add(VALIDATION_WARMUP_LOCK_KEY, "1", VALIDATION_WARMUP_LOCK_TIMEOUT):
        return False

    threading.Thread(
        target=_run_background_validation_refresh,
        name="patcoin-anchor-refresh",
        daemon=True,
    ).start()
    return True


def get_cached_layout_chain_status(force_refresh: bool = False) -> dict[str, object]:
    if force_refresh:
        invalidate_chain_health_cache()

    cached = cache.get(LAYOUT_STATUS_CACHE_KEY)
    if cached is not None:
        return cached

    chain_validation = get_cached_chain_validation(force_refresh=force_refresh)
    anchor_report = get_cached_anchor_report(
        force_refresh=force_refresh,
        allow_stale_only=not force_refresh,
    )
    cached = _build_layout_status(chain_validation, anchor_report)
    cache.set(LAYOUT_STATUS_CACHE_KEY, cached, CACHE_TTL_SECONDS)
    return cached
