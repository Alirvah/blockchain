from django.core.management.base import BaseCommand, CommandError

from ledger.genesis_anchor import (
    STATUS_ANCHOR_MISSING,
    STATUS_MISMATCH,
    get_anchor_status_message,
    get_genesis_anchor_report,
)


class Command(BaseCommand):
    help = "Verify the live genesis state against the committed anchor manifest"

    def handle(self, *args, **options):
        report = get_genesis_anchor_report()
        self.stdout.write(f"Status: {report['status']}")
        self.stdout.write(get_anchor_status_message(report))

        if report.get("anchor"):
            self.stdout.write(f"Anchored hash: {report['anchor']['block']['block_hash']}")
        if report.get("live"):
            self.stdout.write(f"Live hash: {report['live']['block']['block_hash']}")

        git = report["git"]
        if git.get("commit_short"):
            self.stdout.write(f"Anchor commit: {git['commit_short']} {git['commit_subject']}")
        elif git.get("git_available"):
            self.stdout.write("Anchor manifest has not been committed yet.")
        else:
            self.stdout.write("Git metadata unavailable.")

        if git.get("project_url"):
            self.stdout.write(f"Project URL: {git['project_url']}")
        if git.get("commit_url"):
            self.stdout.write(f"Commit URL: {git['commit_url']}")
        if git.get("remote_url"):
            self.stdout.write(f"Remote: {git['remote_url']}")
        if git.get("remote_check_error"):
            self.stdout.write(f"Remote check: {git['remote_check_error']}")


        if report["mismatches"]:
            self.stdout.write("Mismatches:")
            for mismatch in report["mismatches"]:
                self.stdout.write(f" - {mismatch}")

        if report["status"] in {STATUS_ANCHOR_MISSING, STATUS_MISMATCH}:
            raise CommandError("Genesis anchor verification failed.")
