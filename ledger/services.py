from django.db import transaction

from .models import Transfer, Wallet


def create_transfer(*, sender, recipient, amount, memo="", created_by=None):
    """
    Create a transfer with row-level locking to prevent double-spend.

    Raises ValueError if the sender has insufficient balance (including
    pending outgoing transfers).
    """
    with transaction.atomic():
        locked_sender = Wallet.objects.select_for_update().get(pk=sender.pk)

        available = locked_sender.pending_balance
        if available < amount:
            raise ValueError(
                f"Insufficient balance. Available: {available:,.2f} PAT."
            )

        return Transfer.objects.create(
            sender=locked_sender,
            recipient=recipient,
            amount=amount,
            memo=memo,
            status=Transfer.PENDING,
            created_by=created_by,
        )
