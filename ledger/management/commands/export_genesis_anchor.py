from django.core.management.base import BaseCommand, CommandError

from ledger.genesis_anchor import export_genesis_anchor


class Command(BaseCommand):
    help = "Export the live genesis state into the tracked anchor manifest"

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Overwrite the existing genesis anchor manifest if it already exists.",
        )

    def handle(self, *args, **options):
        try:
            manifest_path, manifest = export_genesis_anchor(force=options["force"])
        except FileExistsError as exc:
            raise CommandError(str(exc)) from exc
        except Exception as exc:  # pragma: no cover - surfaced as CLI error
            raise CommandError(str(exc)) from exc

        self.stdout.write(self.style.SUCCESS(
            f"Genesis anchor written to {manifest_path}\n"
            f"Anchored hash: {manifest['block']['block_hash']}"
        ))
