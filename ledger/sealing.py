from dataclasses import dataclass

from django.db import connection, transaction

from .health import invalidate_chain_health_cache
from .models import Block, Transfer

SEAL_ADVISORY_LOCK_ID = 8_217_341


@dataclass(slots=True)
class SealPendingTransfersResult:
    status: str
    block: Block | None = None
    transfer_count: int = 0

    @property
    def was_sealed(self) -> bool:
        return self.status == "sealed"


def _try_acquire_advisory_lock() -> bool:
    if connection.vendor != "postgresql":
        return True

    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_xact_lock(%s)", [SEAL_ADVISORY_LOCK_ID])
        row = cursor.fetchone()
    return bool(row and row[0])


def seal_pending_transfers(*, user=None) -> SealPendingTransfersResult:
    with transaction.atomic():
        if not _try_acquire_advisory_lock():
            return SealPendingTransfersResult(status="locked")

        pending_ids = list(
            Transfer.objects.select_for_update()
            .filter(status=Transfer.PENDING)
            .order_by("created_at", "id")
            .values_list("id", flat=True)
        )
        if not pending_ids:
            return SealPendingTransfersResult(status="empty")

        tip = (
            Block.objects.select_for_update()
            .filter(status__in=[Block.SEALED, Block.GENESIS])
            .order_by("-index")
            .first()
        )
        block = Block.objects.create(
            index=(tip.index + 1) if tip else 1,
            status=Block.PENDING,
            previous_hash=tip.block_hash if tip else "0" * 64,
        )

        Transfer.objects.filter(id__in=pending_ids).update(block=block)
        block.seal(user=user)
        transaction.on_commit(invalidate_chain_health_cache)
        return SealPendingTransfersResult(
            status="sealed",
            block=block,
            transfer_count=len(pending_ids),
        )
