from django.core.management.base import BaseCommand

from ledger.sealing import seal_pending_transfers


class Command(BaseCommand):
    help = "Seal all pending transfers into a new block if any are waiting"

    def handle(self, *args, **options):
        result = seal_pending_transfers()

        if result.status == "empty":
            self.stdout.write("No pending transfers to seal.")
            return

        if result.status == "locked":
            self.stdout.write("Skipping auto-seal because another sealing run is already in progress.")
            return

        self.stdout.write(
            f"Block #{result.block.index} sealed with {result.transfer_count} transfer(s). "
            f"Hash: {result.block.block_hash[:16]}..."
        )
