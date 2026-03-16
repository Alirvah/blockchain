from decimal import Decimal

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from ledger.models import Block, Transfer, Wallet


class Command(BaseCommand):
    help = "Bootstrap the genesis block and treasury wallet with 1,000,000 PatCoin"

    def handle(self, *args, **options):
        if Block.objects.filter(status=Block.GENESIS).exists():
            self.stdout.write(self.style.WARNING("Genesis block already exists. Skipping."))
            return

        total_supply = Decimal(str(settings.PATCOIN_TOTAL_SUPPLY))

        with transaction.atomic():
            # Create genesis block
            genesis = Block.objects.create(
                index=0,
                status=Block.GENESIS,
                previous_hash="0" * 64,
                created_at=timezone.now(),
                sealed_at=timezone.now(),
                nonce="GENESIS",
            )

            # Create treasury wallet
            treasury = Wallet.objects.create(
                label="Treasury",
                wallet_type=Wallet.TREASURY,
            )

            # Create mint transfer (no sender = minted from nothing)
            mint_tx = Transfer.objects.create(
                sender=None,
                recipient=treasury,
                amount=total_supply,
                memo="Genesis mint: 1,000,000 PatCoin created",
                status=Transfer.CONFIRMED,
                block=genesis,
                created_at=genesis.created_at,
            )

            # Compute and store genesis block hash
            genesis.block_hash = genesis.compute_hash()
            genesis.save(update_fields=["block_hash"])

            # Create default admin user if none exists
            if not User.objects.filter(is_superuser=True).exists():
                User.objects.create_superuser(
                    username=settings.BOOTSTRAP_ADMIN_USERNAME,
                    email=settings.BOOTSTRAP_ADMIN_EMAIL,
                    password=settings.BOOTSTRAP_ADMIN_PASSWORD,
                    is_staff=True,
                )
                self.stdout.write(
                    self.style.SUCCESS(
                        "Created bootstrap admin user "
                        f"({settings.BOOTSTRAP_ADMIN_USERNAME}) from environment settings"
                    )
                )

        self.stdout.write(
            self.style.SUCCESS(
                f"Genesis block created (hash: {genesis.block_hash[:16]}...)\n"
                f"Treasury wallet: {treasury.address}\n"
                f"Minted: {total_supply:,.2f} PatCoin"
            )
        )
