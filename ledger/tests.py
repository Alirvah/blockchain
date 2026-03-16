from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from decimal import Decimal

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from .genesis_anchor import (
    STATUS_ANCHOR_MISSING,
    STATUS_GIT_UNAVAILABLE,
    STATUS_MISMATCH,
    STATUS_REMOTE_UNVERIFIED,
    STATUS_VALID,
    get_anchor_manifest_path,
    get_genesis_anchor_report,
)
from .models import Block, Transfer, Wallet


@override_settings(PATCOIN_TOTAL_SUPPLY=1_000_000)
class GenesisBootstrapTest(TestCase):
    def test_bootstrap_creates_genesis(self):
        """Genesis bootstrap creates one genesis block and one treasury wallet."""
        call_command("bootstrap_genesis")

        self.assertEqual(Block.objects.filter(status=Block.GENESIS).count(), 1)
        genesis = Block.objects.get(status=Block.GENESIS)
        self.assertEqual(genesis.index, 0)
        self.assertTrue(genesis.block_hash)

        treasury = Wallet.objects.get(wallet_type=Wallet.TREASURY)
        self.assertEqual(treasury.balance, Decimal("1000000.00"))

        mint_tx = Transfer.objects.get(sender=None)
        self.assertEqual(mint_tx.amount, Decimal("1000000.00"))
        self.assertEqual(mint_tx.status, Transfer.CONFIRMED)

    def test_bootstrap_idempotent(self):
        """Re-running bootstrap does not create duplicate genesis data."""
        call_command("bootstrap_genesis")
        call_command("bootstrap_genesis")

        self.assertEqual(Block.objects.filter(status=Block.GENESIS).count(), 1)
        self.assertEqual(Wallet.objects.filter(wallet_type=Wallet.TREASURY).count(), 1)
        self.assertEqual(Transfer.objects.filter(sender=None).count(), 1)


class TransferSupplyTest(TestCase):
    def setUp(self):
        call_command("bootstrap_genesis")
        self.treasury = Wallet.objects.get(wallet_type=Wallet.TREASURY)

        self.user = User.objects.create_user("customer1", password="test")
        self.wallet_a = Wallet.objects.create(
            label="Wallet A", wallet_type=Wallet.CUSTOMER, owner=self.user
        )
        self.wallet_b = Wallet.objects.create(
            label="Wallet B", wallet_type=Wallet.CUSTOMER
        )

    def _make_transfer(self, sender, recipient, amount, confirm=True):
        tx = Transfer.objects.create(
            sender=sender,
            recipient=recipient,
            amount=Decimal(str(amount)),
            status=Transfer.PENDING,
        )
        if confirm:
            tx.status = Transfer.CONFIRMED
            tx.save()
        return tx

    def test_transfer_preserves_supply(self):
        """Transfers preserve total supply."""
        self._make_transfer(self.treasury, self.wallet_a, 5000)
        self._make_transfer(self.wallet_a, self.wallet_b, 2000)

        total = sum(
            w.balance for w in Wallet.objects.all()
        )
        self.assertEqual(total, Decimal("1000000.00"))

    def test_pending_not_in_sealed_history(self):
        """Pending transfers don't appear in sealed block history."""
        tx = self._make_transfer(self.treasury, self.wallet_a, 1000, confirm=False)
        sealed_blocks = Block.objects.filter(status=Block.SEALED)
        for block in sealed_blocks:
            self.assertNotIn(tx, block.transfers.all())


class BlockSealingTest(TestCase):
    def setUp(self):
        call_command("bootstrap_genesis")
        self.treasury = Wallet.objects.get(wallet_type=Wallet.TREASURY)
        self.admin = User.objects.get(username="admin")
        self.wallet = Wallet.objects.create(label="Test", wallet_type=Wallet.CUSTOMER)

    def test_seal_creates_valid_block(self):
        """Sealing creates a block with correct previous-hash linkage."""
        Transfer.objects.create(
            sender=self.treasury,
            recipient=self.wallet,
            amount=Decimal("100"),
            status=Transfer.PENDING,
        )

        genesis = Block.objects.get(status=Block.GENESIS)
        block = Block.objects.create(
            index=1, status=Block.PENDING, previous_hash=genesis.block_hash
        )
        Transfer.objects.filter(status=Transfer.PENDING).update(block=block)
        block.seal(user=self.admin)

        self.assertEqual(block.status, Block.SEALED)
        self.assertEqual(block.previous_hash, genesis.block_hash)
        self.assertTrue(block.block_hash)
        self.assertEqual(block.sealed_by, self.admin)

    def test_chain_validation(self):
        """Chain validation passes for valid chain."""
        is_valid, errors = Block.validate_chain()
        self.assertTrue(is_valid)
        self.assertEqual(len(errors), 0)

    def test_chain_detects_tampering(self):
        """Chain validation detects hash tampering."""
        genesis = Block.objects.get(status=Block.GENESIS)
        genesis.block_hash = "tampered" + genesis.block_hash[8:]
        genesis.save(update_fields=["block_hash"])

        is_valid, errors = Block.validate_chain()
        self.assertFalse(is_valid)
        self.assertTrue(len(errors) > 0)


class AuthorizationTest(TestCase):
    def setUp(self):
        call_command("bootstrap_genesis")
        self.treasury = Wallet.objects.get(wallet_type=Wallet.TREASURY)

        self.user1 = User.objects.create_user("user1", password="test")
        self.wallet1 = Wallet.objects.create(
            label="User1 Wallet", wallet_type=Wallet.CUSTOMER, owner=self.user1
        )

        self.user2 = User.objects.create_user("user2", password="test")
        self.wallet2 = Wallet.objects.create(
            label="User2 Wallet", wallet_type=Wallet.CUSTOMER, owner=self.user2
        )

    def test_customer_cannot_view_other_wallet(self):
        """Customer cannot access another user's wallet detail."""
        client = Client()
        client.login(username="user1", password="test")

        response = client.get(reverse("wallet_detail", args=[self.wallet2.id]))
        self.assertEqual(response.status_code, 302)

    def test_customer_cannot_view_other_provenance(self):
        """Customer cannot access another user's provenance page."""
        client = Client()
        client.login(username="user1", password="test")

        response = client.get(reverse("provenance", args=[self.wallet2.id]))
        self.assertEqual(response.status_code, 302)

    def test_customer_cannot_access_admin_pages(self):
        """Customer cannot access admin operational pages."""
        client = Client()
        client.login(username="user1", password="test")

        for url_name in ["wallet_list", "user_list", "transfer_list", "pending_queue"]:
            response = client.get(reverse(url_name))
            self.assertEqual(
                response.status_code, 302, f"Customer should be redirected from {url_name}"
            )


class GenesisAnchorTest(TestCase):
    def setUp(self):
        self.tempdir = TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.override = override_settings(
            GENESIS_ANCHOR_PATH=str(Path(self.tempdir.name) / "anchors" / "genesis.json")
        )
        self.override.enable()
        self.addCleanup(self.override.disable)

        call_command("bootstrap_genesis")

    def test_export_command_writes_manifest(self):
        call_command("export_genesis_anchor")

        manifest_path = get_anchor_manifest_path()
        self.assertTrue(manifest_path.exists())

        report = get_genesis_anchor_report()
        self.assertEqual(report["anchor"]["block"]["index"], 0)
        self.assertEqual(report["anchor"]["mint_transfer"]["amount"], "1000000.00")

    def test_export_command_refuses_overwrite_without_force(self):
        call_command("export_genesis_anchor")
        with self.assertRaises(CommandError):
            call_command("export_genesis_anchor")

    @mock.patch("ledger.genesis_anchor.get_git_anchor_metadata")
    def test_verification_reports_valid_when_manifest_matches_and_remote_confirmed(self, metadata_mock):
        metadata_mock.return_value = {
            "git_available": True,
            "repo_root": "/tmp/repo",
            "manifest_committed": True,
            "commit_sha": "abc123",
            "commit_short": "abc123",
            "commit_subject": "anchor genesis",
            "commit_timestamp": "1710000000",
            "remote_name": "origin",
            "remote_url": "https://example.com/repo.git",
            "remote_verified": True,
        }
        call_command("export_genesis_anchor")

        report = get_genesis_anchor_report()
        self.assertEqual(report["status"], STATUS_VALID)
        self.assertEqual(report["mismatches"], [])

    @mock.patch("ledger.genesis_anchor.get_git_anchor_metadata")
    def test_verification_reports_remote_unverified_for_local_only_git(self, metadata_mock):
        metadata_mock.return_value = {
            "git_available": True,
            "repo_root": "/tmp/repo",
            "manifest_committed": True,
            "commit_sha": "abc123",
            "commit_short": "abc123",
            "commit_subject": "anchor genesis",
            "commit_timestamp": "1710000000",
            "remote_name": None,
            "remote_url": None,
            "remote_verified": False,
        }
        call_command("export_genesis_anchor")

        report = get_genesis_anchor_report()
        self.assertEqual(report["status"], STATUS_REMOTE_UNVERIFIED)

    @mock.patch("ledger.genesis_anchor.get_git_anchor_metadata")
    def test_verification_reports_git_unavailable(self, metadata_mock):
        metadata_mock.return_value = {
            "git_available": False,
            "repo_root": None,
            "manifest_committed": False,
            "commit_sha": None,
            "commit_short": None,
            "commit_subject": None,
            "commit_timestamp": None,
            "remote_name": None,
            "remote_url": None,
            "remote_verified": False,
        }
        call_command("export_genesis_anchor")

        report = get_genesis_anchor_report()
        self.assertEqual(report["status"], STATUS_GIT_UNAVAILABLE)

    def test_verification_reports_anchor_missing(self):
        report = get_genesis_anchor_report()
        self.assertEqual(report["status"], STATUS_ANCHOR_MISSING)

    @mock.patch("ledger.genesis_anchor.get_git_anchor_metadata")
    def test_verification_reports_mismatch_when_genesis_is_tampered(self, metadata_mock):
        metadata_mock.return_value = {
            "git_available": True,
            "repo_root": "/tmp/repo",
            "manifest_committed": True,
            "commit_sha": "abc123",
            "commit_short": "abc123",
            "commit_subject": "anchor genesis",
            "commit_timestamp": "1710000000",
            "remote_name": "origin",
            "remote_url": "https://example.com/repo.git",
            "remote_verified": True,
        }
        call_command("export_genesis_anchor")

        treasury = Wallet.objects.get(wallet_type=Wallet.TREASURY)
        treasury.address = "0x" + "f" * 40
        treasury.save(update_fields=["address"])

        report = get_genesis_anchor_report()
        self.assertEqual(report["status"], STATUS_MISMATCH)
        self.assertTrue(any("treasury_wallet.address" in mismatch for mismatch in report["mismatches"]))


class ViewRenderTest(TestCase):
    def setUp(self):
        self.tempdir = TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.override = override_settings(
            GENESIS_ANCHOR_PATH=str(Path(self.tempdir.name) / "anchors" / "genesis.json")
        )
        self.override.enable()
        self.addCleanup(self.override.disable)
        call_command("bootstrap_genesis")
        self.admin = User.objects.get(username="admin")
        self.client = Client()
        self.client.login(username="admin", password="admin")

    def test_login_page_renders(self):
        client = Client()
        response = client.get(reverse("login"))
        self.assertEqual(response.status_code, 200)

    def test_admin_dashboard_renders(self):
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)

    @mock.patch("ledger.views.get_genesis_anchor_report")
    @mock.patch("ledger.views.get_anchor_status_message")
    def test_explorer_renders(self, message_mock, report_mock):
        report_mock.return_value = {
            "status": STATUS_REMOTE_UNVERIFIED,
            "manifest_path": "anchors/genesis.json",
            "anchor_exists": True,
            "mismatches": [],
            "db_matches_anchor": True,
            "anchor": {"block": {"block_hash": "anchored-hash"}},
            "live": {"block": {"block_hash": "live-hash"}},
            "git": {
                "git_available": True,
                "commit_short": "abc123",
                "remote_verified": False,
                "remote_url": None,
                "remote_name": None,
            },
        }
        message_mock.return_value = "Local-only verification."
        response = self.client.get(reverse("explorer"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Genesis Anchor")
        self.assertContains(response, "Local Only")
        self.assertContains(response, "Local-only verification.")

    def test_block_list_renders(self):
        response = self.client.get(reverse("block_list"))
        self.assertEqual(response.status_code, 200)

    def test_wallet_list_renders(self):
        response = self.client.get(reverse("wallet_list"))
        self.assertEqual(response.status_code, 200)

    def test_chain_validate_renders(self):
        response = self.client.get(reverse("chain_validate"))
        self.assertEqual(response.status_code, 200)

    def test_pending_queue_renders(self):
        response = self.client.get(reverse("pending_queue"))
        self.assertEqual(response.status_code, 200)

    @mock.patch("ledger.views.get_genesis_anchor_report")
    @mock.patch("ledger.views.get_anchor_status_message")
    def test_provenance_renders(self, message_mock, report_mock):
        report_mock.return_value = {
            "status": STATUS_MISMATCH,
            "manifest_path": "anchors/genesis.json",
            "anchor_exists": True,
            "mismatches": ["block.block_hash: anchored='a' live='b'"],
            "db_matches_anchor": False,
            "anchor": {"block": {"block_hash": "anchored-hash"}},
            "live": {"block": {"block_hash": "live-hash"}},
            "git": {
                "git_available": True,
                "commit_short": "abc123",
                "commit_subject": "anchor genesis",
                "remote_verified": False,
                "remote_url": None,
                "remote_name": None,
            },
        }
        message_mock.return_value = "Mismatch warning."
        treasury = Wallet.objects.get(wallet_type=Wallet.TREASURY)
        response = self.client.get(reverse("provenance", args=[treasury.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Git Anchor")
        self.assertContains(response, "Mismatch")
        self.assertContains(response, "Mismatch warning.")
