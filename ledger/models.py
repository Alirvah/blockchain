import hashlib
import json
import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone


class Block(models.Model):
    """A block in the PatCoin chain. Can be genesis, pending, or sealed."""

    GENESIS = "genesis"
    PENDING = "pending"
    SEALED = "sealed"
    STATUS_CHOICES = [
        (GENESIS, "Genesis"),
        (PENDING, "Pending"),
        (SEALED, "Sealed"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    index = models.PositiveIntegerField(unique=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=PENDING)
    previous_hash = models.CharField(max_length=64, blank=True, default="")
    block_hash = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)
    sealed_at = models.DateTimeField(null=True, blank=True)
    sealed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="sealed_blocks",
    )
    nonce = models.CharField(max_length=64, default="0")

    class Meta:
        ordering = ["-index"]

    def __str__(self):
        return f"Block #{self.index} ({self.status})"

    def compute_hash(self):
        """Compute SHA-256 hash of block contents."""
        transfers_data = []
        for t in self.transfers.order_by("created_at"):
            transfers_data.append(
                {
                    "id": str(t.id),
                    "sender": str(t.sender_id) if t.sender_id else None,
                    "recipient": str(t.recipient_id),
                    "amount": str(t.amount),
                    "created_at": t.created_at.isoformat(),
                }
            )
        payload = json.dumps(
            {
                "index": self.index,
                "previous_hash": self.previous_hash,
                "created_at": self.created_at.isoformat(),
                "nonce": self.nonce,
                "transfers": transfers_data,
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    def seal(self, user=None):
        """Seal this block, computing its hash and marking it immutable."""
        if self.status == self.SEALED:
            raise ValueError("Block is already sealed.")
        if self.status == self.GENESIS:
            raise ValueError("Cannot re-seal genesis block.")

        previous = Block.objects.filter(index=self.index - 1).first()
        if previous:
            self.previous_hash = previous.block_hash

        self.block_hash = self.compute_hash()
        self.status = self.SEALED
        self.sealed_at = timezone.now()
        self.sealed_by = user
        self.save()

        self.transfers.filter(status=Transfer.PENDING).update(
            status=Transfer.CONFIRMED
        )

    @classmethod
    def get_chain_tip(cls):
        """Return the latest sealed or genesis block."""
        return cls.objects.filter(
            status__in=[cls.SEALED, cls.GENESIS]
        ).order_by("-index").first()

    @classmethod
    def validate_chain(cls):
        """Validate hash linkage of the entire chain. Returns (valid, errors)."""
        blocks = list(cls.objects.filter(
            status__in=[cls.SEALED, cls.GENESIS]
        ).order_by("index"))
        errors = []
        for i, block in enumerate(blocks):
            expected_hash = block.compute_hash()
            if block.block_hash != expected_hash:
                errors.append(
                    f"Block #{block.index}: hash mismatch "
                    f"(stored={block.block_hash[:12]}... expected={expected_hash[:12]}...)"
                )
            if i > 0:
                prev = blocks[i - 1]
                if block.previous_hash != prev.block_hash:
                    errors.append(
                        f"Block #{block.index}: previous_hash mismatch "
                        f"(points to {block.previous_hash[:12]}... "
                        f"but Block #{prev.index} hash is {prev.block_hash[:12]}...)"
                    )
        return len(errors) == 0, errors


class Wallet(models.Model):
    """A PatCoin wallet. Can be treasury or customer-owned."""

    TREASURY = "treasury"
    CUSTOMER = "customer"
    TYPE_CHOICES = [
        (TREASURY, "Treasury"),
        (CUSTOMER, "Customer"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    label = models.CharField(max_length=100)
    wallet_type = models.CharField(max_length=10, choices=TYPE_CHOICES, default=CUSTOMER)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="wallets",
    )
    created_at = models.DateTimeField(default=timezone.now)
    address = models.CharField(max_length=42, unique=True, editable=False)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.label} ({self.address[:10]}...)"

    def save(self, *args, **kwargs):
        if not self.address:
            self.address = "0x" + uuid.uuid4().hex[:40]
        super().save(*args, **kwargs)

    @property
    def balance(self):
        """Compute balance from confirmed transfers."""
        received = (
            self.incoming_transfers.filter(status=Transfer.CONFIRMED)
            .aggregate(total=models.Sum("amount"))["total"]
            or Decimal("0")
        )
        sent = (
            self.outgoing_transfers.filter(status=Transfer.CONFIRMED)
            .aggregate(total=models.Sum("amount"))["total"]
            or Decimal("0")
        )
        return received - sent

    @property
    def pending_balance(self):
        """Balance including pending transfers."""
        received = (
            self.incoming_transfers.exclude(status=Transfer.FAILED)
            .aggregate(total=models.Sum("amount"))["total"]
            or Decimal("0")
        )
        sent = (
            self.outgoing_transfers.exclude(status=Transfer.FAILED)
            .aggregate(total=models.Sum("amount"))["total"]
            or Decimal("0")
        )
        return received - sent


class Transfer(models.Model):
    """A PatCoin transfer between wallets."""

    PENDING = "pending"
    CONFIRMED = "confirmed"
    FAILED = "failed"
    STATUS_CHOICES = [
        (PENDING, "Pending"),
        (CONFIRMED, "Confirmed"),
        (FAILED, "Failed"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sender = models.ForeignKey(
        Wallet,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="outgoing_transfers",
    )
    recipient = models.ForeignKey(
        Wallet,
        on_delete=models.PROTECT,
        related_name="incoming_transfers",
    )
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    memo = models.CharField(max_length=255, blank=True, default="")
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=PENDING)
    block = models.ForeignKey(
        Block,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="transfers",
    )
    created_at = models.DateTimeField(default=timezone.now)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_transfers",
    )
    tx_hash = models.CharField(max_length=64, unique=True, editable=False)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        sender_label = self.sender.label if self.sender else "MINT"
        return f"{sender_label} → {self.recipient.label}: {self.amount} PAT"

    def save(self, *args, **kwargs):
        if not self.tx_hash:
            raw = f"{self.sender_id}:{self.recipient_id}:{self.amount}:{self.created_at.isoformat()}:{uuid.uuid4()}"
            self.tx_hash = hashlib.sha256(raw.encode()).hexdigest()
        super().save(*args, **kwargs)
