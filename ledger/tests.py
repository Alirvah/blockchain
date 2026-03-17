from types import SimpleNamespace
from io import StringIO
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
    get_git_anchor_metadata,
    get_anchor_manifest_path,
    get_genesis_anchor_report,
)
from .context_processors import chain_status
from .health import (
    ensure_background_validation,
    get_cached_anchor_report,
    get_cached_chain_validation,
    get_cached_layout_chain_status,
    invalidate_chain_health_cache,
    is_background_validation_running,
)
from .models import Block, InviteLink, Transfer, Wallet
from .sealing import seal_pending_transfers


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

    def test_shared_sealer_seals_all_pending_transfers(self):
        Transfer.objects.create(
            sender=self.treasury,
            recipient=self.wallet,
            amount=Decimal("50"),
            status=Transfer.PENDING,
        )

        result = seal_pending_transfers()

        self.assertTrue(result.was_sealed)
        self.assertEqual(result.transfer_count, 1)
        self.assertEqual(result.block.status, Block.SEALED)
        self.assertIsNone(result.block.sealed_by)
        self.assertEqual(Transfer.objects.get(recipient=self.wallet).status, Transfer.CONFIRMED)

    def test_auto_seal_command_seals_pending_transfers(self):
        Transfer.objects.create(
            sender=self.treasury,
            recipient=self.wallet,
            amount=Decimal("75"),
            status=Transfer.PENDING,
        )
        stdout = StringIO()

        call_command("auto_seal_blocks", stdout=stdout)

        sealed_block = Block.objects.get(status=Block.SEALED)
        tx = Transfer.objects.get(recipient=self.wallet)
        self.assertEqual(tx.status, Transfer.CONFIRMED)
        self.assertEqual(tx.block, sealed_block)
        self.assertIsNone(sealed_block.sealed_by)
        self.assertIn("Block #1 sealed with 1 transfer(s).", stdout.getvalue())

    def test_auto_seal_command_is_noop_when_queue_empty(self):
        stdout = StringIO()

        call_command("auto_seal_blocks", stdout=stdout)

        self.assertEqual(Block.objects.filter(status=Block.SEALED).count(), 0)
        self.assertIn("No pending transfers to seal.", stdout.getvalue())

    @mock.patch("ledger.sealing._try_acquire_advisory_lock", return_value=False)
    def test_auto_seal_command_skips_when_lock_is_held(self, _lock_mock):
        Transfer.objects.create(
            sender=self.treasury,
            recipient=self.wallet,
            amount=Decimal("80"),
            status=Transfer.PENDING,
        )
        stdout = StringIO()

        call_command("auto_seal_blocks", stdout=stdout)

        self.assertEqual(Block.objects.filter(status=Block.SEALED).count(), 0)
        self.assertEqual(Transfer.objects.get(recipient=self.wallet).status, Transfer.PENDING)
        self.assertIn("another sealing run is already in progress", stdout.getvalue())

    @mock.patch("ledger.health.Block.validate_chain", return_value=(True, []))
    def test_auto_seal_command_refreshes_validation_cache(self, validate_mock):
        Transfer.objects.create(
            sender=self.treasury,
            recipient=self.wallet,
            amount=Decimal("33"),
            status=Transfer.PENDING,
        )

        call_command("auto_seal_blocks")
        cached = get_cached_chain_validation()

        self.assertEqual(cached, {"is_valid": True, "errors": []})
        self.assertEqual(validate_mock.call_count, 1)

    def test_manual_seal_view_still_seals_pending_transfers(self):
        client = Client()
        client.login(username="admin", password="admin")
        Transfer.objects.create(
            sender=self.treasury,
            recipient=self.wallet,
            amount=Decimal("60"),
            status=Transfer.PENDING,
        )

        response = client.post(reverse("seal_block"))

        sealed_block = Block.objects.get(status=Block.SEALED)
        self.assertRedirects(response, reverse("block_detail", args=[sealed_block.id]))
        self.assertEqual(sealed_block.sealed_by, self.admin)
        self.assertEqual(Transfer.objects.get(recipient=self.wallet).status, Transfer.CONFIRMED)

    def test_auto_sealed_blocks_render_as_system_sealed(self):
        client = Client()
        client.login(username="admin", password="admin")
        Transfer.objects.create(
            sender=self.treasury,
            recipient=self.wallet,
            amount=Decimal("90"),
            status=Transfer.PENDING,
        )
        call_command("auto_seal_blocks")
        sealed_block = Block.objects.get(status=Block.SEALED)

        detail_response = client.get(reverse("block_detail", args=[sealed_block.id]))
        list_response = client.get(reverse("block_list"))

        self.assertContains(detail_response, "System")
        self.assertContains(list_response, "System")


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
        self.assertEqual(response.status_code, 403)

    def test_customer_can_view_other_provenance(self):
        """Any logged-in user can view provenance (transparency)."""
        client = Client()
        client.login(username="user1", password="test")

        response = client.get(reverse("provenance", args=[self.wallet2.id]))
        self.assertEqual(response.status_code, 200)

    def test_customer_can_view_chain_validate(self):
        """Any logged-in user can open chain validation."""
        client = Client()
        client.login(username="user1", password="test")

        response = client.get(reverse("chain_validate"))
        self.assertEqual(response.status_code, 200)

    @mock.patch("ledger.views.get_cached_chain_validation")
    def test_customer_refresh_request_uses_cached_validation_only(self, validation_mock):
        validation_mock.return_value = {"is_valid": True, "errors": []}
        client = Client()
        client.login(username="user1", password="test")

        response = client.get(f"{reverse('chain_validate')}?refresh=1")

        self.assertEqual(response.status_code, 200)
        validation_mock.assert_called_once_with(force_refresh=False)
        self.assertNotContains(response, "Refresh Validation")

    def test_customer_cannot_access_admin_pages(self):
        """Customer cannot access admin operational pages."""
        client = Client()
        client.login(username="user1", password="test")

        for url_name in ["wallet_list", "user_list", "transfer_list", "pending_queue"]:
            response = client.get(reverse(url_name))
            self.assertEqual(
                response.status_code, 403, f"Customer should get 403 from {url_name}"
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

    @mock.patch("ledger.genesis_anchor._verify_commit_url_online")
    @mock.patch("ledger.genesis_anchor._run_git")
    def test_git_anchor_metadata_derives_project_and_commit_links(self, run_git_mock, verify_mock):
        manifest_path = get_anchor_manifest_path()
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text("{}", encoding="utf-8")

        verify_mock.return_value = (True, None)
        run_git_mock.side_effect = [
            mock.Mock(returncode=0, stdout=f"{settings.BASE_DIR}\n", stderr=""),
            mock.Mock(returncode=0, stdout="a" * 40 + "\nabc1234\n1710000000\nanchor genesis\n", stderr=""),
            mock.Mock(returncode=0, stdout="https://github.com/Alirvah/blockchain.git\n", stderr=""),
        ]

        metadata = get_git_anchor_metadata(manifest_path)

        self.assertEqual(metadata["project_url"], "https://github.com/Alirvah/blockchain")
        self.assertEqual(metadata["commit_url"], f"https://github.com/Alirvah/blockchain/commit/{'a' * 40}")
        self.assertTrue(metadata["remote_verified"])


class InviteFlowTest(TestCase):
    def setUp(self):
        call_command("bootstrap_genesis")
        self.admin = User.objects.get(username="admin")
        self.client = Client()

    def test_admin_can_create_invite_link(self):
        self.client.login(username="admin", password="admin")
        response = self.client.post(reverse("invite_create"), {"note": "Launch wave"})

        self.assertEqual(response.status_code, 302)
        invite = InviteLink.objects.get()
        self.assertEqual(invite.note, "Launch wave")
        self.assertEqual(invite.bonus_amount, Decimal("10.00"))
        self.assertEqual(invite.created_by, self.admin)

    def test_admin_cannot_create_invite_when_treasury_cannot_cover_new_liability(self):
        self.client.login(username="admin", password="admin")
        treasury = Wallet.objects.get(wallet_type=Wallet.TREASURY)
        treasury_depletion = Wallet.objects.create(label="Drain", wallet_type=Wallet.CUSTOMER)
        Transfer.objects.create(
            sender=treasury,
            recipient=treasury_depletion,
            amount=Decimal("999995.00"),
            status=Transfer.CONFIRMED,
        )

        response = self.client.post(reverse("invite_create"), {"note": "Should fail"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(InviteLink.objects.count(), 0)
        self.assertContains(response, "Treasury cannot safely cover another invite bonus")

    def test_admin_cannot_create_invite_when_existing_open_invites_already_use_remaining_coverage(self):
        self.client.login(username="admin", password="admin")
        treasury = Wallet.objects.get(wallet_type=Wallet.TREASURY)
        treasury_depletion = Wallet.objects.create(label="Drain", wallet_type=Wallet.CUSTOMER)
        Transfer.objects.create(
            sender=treasury,
            recipient=treasury_depletion,
            amount=Decimal("999980.00"),
            status=Transfer.CONFIRMED,
        )
        InviteLink.objects.create(created_by=self.admin, bonus_amount=Decimal("10.00"))
        InviteLink.objects.create(created_by=self.admin, bonus_amount=Decimal("10.00"))

        response = self.client.post(reverse("invite_create"), {"note": "Third invite blocked"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(InviteLink.objects.count(), 2)
        self.assertContains(response, "Treasury cannot safely cover another invite bonus")

    def test_invite_registration_creates_wallet_and_bonus(self):
        invite = InviteLink.objects.create(created_by=self.admin, bonus_amount=Decimal("10.00"))
        treasury = Wallet.objects.get(wallet_type=Wallet.TREASURY)
        treasury_before = treasury.balance

        response = self.client.post(
            reverse("invite_register", args=[invite.token]),
            {
                "username": "invited_user",
                "email": "invite@example.com",
                "password": "test-pass-123",
                "confirm_password": "test-pass-123",
            },
        )

        self.assertEqual(response.status_code, 302)
        user = User.objects.get(username="invited_user")
        wallet = Wallet.objects.get(owner=user)
        invite.refresh_from_db()
        treasury.refresh_from_db()
        bonus_tx = Transfer.objects.get(recipient=wallet, memo__contains="Invite bonus")

        self.assertEqual(wallet.balance, Decimal("0.00"))
        self.assertEqual(wallet.pending_balance, Decimal("10.00"))
        self.assertEqual(invite.used_by, user)
        self.assertIsNotNone(invite.used_at)
        self.assertEqual(bonus_tx.status, Transfer.PENDING)
        self.assertIsNone(bonus_tx.block)
        self.assertEqual(treasury.balance, treasury_before)
        self.assertEqual(treasury.pending_balance, treasury_before - Decimal("10.00"))

    def test_invite_registration_mentions_pending_bonus(self):
        invite = InviteLink.objects.create(created_by=self.admin, bonus_amount=Decimal("10.00"))

        response = self.client.post(
            reverse("invite_register", args=[invite.token]),
            {
                "username": "pending_bonus_user",
                "email": "invite@example.com",
                "password": "test-pass-123",
                "confirm_password": "test-pass-123",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "invite bonus is pending and will confirm within 5 minutes")

    def test_pending_invite_bonus_counts_against_new_invite_capacity(self):
        self.client.login(username="admin", password="admin")
        treasury = Wallet.objects.get(wallet_type=Wallet.TREASURY)
        treasury_depletion = Wallet.objects.create(label="Drain", wallet_type=Wallet.CUSTOMER)
        Transfer.objects.create(
            sender=treasury,
            recipient=treasury_depletion,
            amount=Decimal("999990.00"),
            status=Transfer.CONFIRMED,
        )
        invite = InviteLink.objects.create(created_by=self.admin, bonus_amount=Decimal("10.00"))
        anonymous_client = Client()
        registration = anonymous_client.post(
            reverse("invite_register", args=[invite.token]),
            {
                "username": "queued_invite_user",
                "password": "test-pass-123",
                "confirm_password": "test-pass-123",
            },
        )
        self.assertEqual(registration.status_code, 302)

        response = self.client.post(reverse("invite_create"), {"note": "Should stay blocked"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(InviteLink.objects.count(), 1)
        self.assertContains(response, "Treasury cannot safely cover another invite bonus")

    def test_used_invite_cannot_register_twice(self):
        invite = InviteLink.objects.create(created_by=self.admin, bonus_amount=Decimal("10.00"))

        first = self.client.post(
            reverse("invite_register", args=[invite.token]),
            {
                "username": "first_user",
                "password": "test-pass-123",
                "confirm_password": "test-pass-123",
            },
        )
        self.assertEqual(first.status_code, 302)

        anonymous_client = Client()
        second = anonymous_client.get(reverse("invite_register", args=[invite.token]))
        self.assertEqual(second.status_code, 410)
        self.assertContains(second, "no longer available", status_code=410)

    def test_invite_list_shows_shortfall_warning(self):
        self.client.login(username="admin", password="admin")
        treasury = Wallet.objects.get(wallet_type=Wallet.TREASURY)
        treasury_depletion = Wallet.objects.create(label="Drain", wallet_type=Wallet.CUSTOMER)
        Transfer.objects.create(
            sender=treasury,
            recipient=treasury_depletion,
            amount=Decimal("999995.00"),
            status=Transfer.CONFIRMED,
        )
        InviteLink.objects.create(created_by=self.admin, bonus_amount=Decimal("10.00"))

        response = self.client.get(reverse("invite_list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Invite treasury shortfall detected")

    def test_invite_list_shows_copy_and_qr_actions(self):
        self.client.login(username="admin", password="admin")
        invite = InviteLink.objects.create(created_by=self.admin, bonus_amount=Decimal("10.00"))

        response = self.client.get(reverse("invite_list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Copy Link")
        self.assertContains(response, "Show QR")
        self.assertContains(response, reverse("invite_register", args=[invite.token]))
        self.assertContains(response, "mobile-invite-list")


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

    @mock.patch("ledger.views.get_cached_anchor_report")
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

    @mock.patch("ledger.views.get_cached_anchor_report")
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
        self.assertContains(response, "Verify My PatCoin")
        self.assertContains(response, "Mismatch")


class HealthCacheTest(TestCase):
    def setUp(self):
        invalidate_chain_health_cache()

    def tearDown(self):
        invalidate_chain_health_cache()

    @mock.patch("ledger.health.get_genesis_anchor_report")
    def test_anchor_report_is_cached_until_invalidated(self, report_mock):
        report_mock.side_effect = [
            {"status": STATUS_REMOTE_UNVERIFIED},
            {"status": STATUS_VALID},
        ]

        first = get_cached_anchor_report()
        second = get_cached_anchor_report()
        self.assertEqual(first["status"], STATUS_REMOTE_UNVERIFIED)
        self.assertEqual(second["status"], STATUS_REMOTE_UNVERIFIED)
        self.assertEqual(report_mock.call_count, 1)

        invalidate_chain_health_cache()
        refreshed = get_cached_anchor_report()
        self.assertEqual(refreshed["status"], STATUS_VALID)
        self.assertEqual(report_mock.call_count, 2)

    @mock.patch("ledger.health.Block.validate_chain")
    @mock.patch("ledger.health.get_genesis_anchor_report")
    def test_layout_status_uses_cached_chain_and_anchor_results(self, report_mock, validate_mock):
        validate_mock.return_value = (True, [])
        report_mock.return_value = {"status": STATUS_REMOTE_UNVERIFIED}

        get_cached_anchor_report()

        first = get_cached_layout_chain_status()
        second = get_cached_layout_chain_status()

        self.assertEqual(first["chain_health"], "local")
        self.assertEqual(second["chain_health"], "local")
        self.assertEqual(validate_mock.call_count, 1)
        self.assertEqual(report_mock.call_count, 1)

    @mock.patch("ledger.health.Block.validate_chain")
    @mock.patch("ledger.health.get_genesis_anchor_report")
    def test_layout_status_skips_remote_anchor_fetch_on_cold_cache(self, report_mock, validate_mock):
        validate_mock.return_value = (True, [])

        status = get_cached_layout_chain_status()

        self.assertEqual(status["chain_health"], "local")
        self.assertEqual(status["anchor_status"], "unchecked")
        report_mock.assert_not_called()

    @mock.patch("ledger.health.threading.Thread")
    def test_background_validation_starts_only_once(self, thread_mock):
        thread_instance = mock.Mock()
        thread_mock.return_value = thread_instance

        started = ensure_background_validation()
        duplicate = ensure_background_validation()

        self.assertTrue(started)
        self.assertFalse(duplicate)
        thread_mock.assert_called_once()
        thread_instance.start.assert_called_once()
        self.assertTrue(is_background_validation_running())

    @mock.patch("ledger.context_processors.is_background_validation_running", return_value=True)
    @mock.patch("ledger.context_processors.ensure_background_validation")
    @mock.patch("ledger.context_processors.get_cached_layout_chain_status")
    def test_context_processor_marks_validation_running(
        self,
        status_mock,
        ensure_mock,
        running_mock,
    ):
        status_mock.return_value = {
            "chain_health": "local",
            "chain_is_valid": True,
            "chain_error_count": 0,
            "anchor_status": "unchecked",
        }
        request = SimpleNamespace(
            user=SimpleNamespace(is_authenticated=True),
            GET={},
        )

        result = chain_status(request)

        ensure_mock.assert_called_once()
        self.assertEqual(result["anchor_status"], "validating")
        self.assertTrue(result["background_validation_running"])

    @mock.patch("ledger.context_processors.ensure_background_validation")
    @mock.patch("ledger.context_processors.get_cached_layout_chain_status")
    def test_context_processor_ignores_force_refresh_for_nonstaff(
        self,
        status_mock,
        ensure_mock,
    ):
        status_mock.return_value = {
            "chain_health": "local",
            "chain_is_valid": True,
            "chain_error_count": 0,
            "anchor_status": "valid",
        }
        request = SimpleNamespace(
            user=SimpleNamespace(is_authenticated=True, is_staff=False),
            GET={"refresh": "1"},
        )

        chain_status(request)

        status_mock.assert_called_once_with(force_refresh=False)
        ensure_mock.assert_not_called()

    @mock.patch("ledger.context_processors.ensure_background_validation")
    @mock.patch("ledger.context_processors.get_cached_layout_chain_status")
    def test_context_processor_allows_force_refresh_for_staff(
        self,
        status_mock,
        ensure_mock,
    ):
        status_mock.return_value = {
            "chain_health": "ok",
            "chain_is_valid": True,
            "chain_error_count": 0,
            "anchor_status": "valid",
        }
        request = SimpleNamespace(
            user=SimpleNamespace(is_authenticated=True, is_staff=True),
            GET={"refresh": "1"},
        )

        chain_status(request)

        status_mock.assert_called_once_with(force_refresh=True)
        ensure_mock.assert_not_called()


class CustomerQrRenderTest(TestCase):
    def setUp(self):
        call_command("bootstrap_genesis")
        self.user = User.objects.create_user("qruser", password="test-pass-123")
        self.wallet = Wallet.objects.create(
            label="QR Wallet",
            wallet_type=Wallet.CUSTOMER,
            owner=self.user,
        )
        self.client = Client()
        self.client.login(username="qruser", password="test-pass-123")

    def test_customer_dashboard_shows_receive_qr_section(self):
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Receive PatCoin")
        self.assertContains(response, reverse("patcoin_pay", args=[self.wallet.address]))

    def test_customer_send_shows_qr_scan_actions(self):
        response = self.client.get(reverse("customer_send"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Scan QR")
        self.assertContains(response, "Scan Image")

    def test_pending_balance_hidden_when_no_pending_transfers(self):
        dashboard = self.client.get(reverse("dashboard"))
        send = self.client.get(reverse("customer_send"))
        detail = self.client.get(reverse("wallet_detail", args=[self.wallet.id]))

        self.assertNotContains(dashboard, "Including unconfirmed")
        self.assertNotContains(send, "Pending Balance")
        self.assertNotContains(detail, "Pending Balance")

    def test_pending_balance_shown_when_wallet_has_pending_transfer(self):
        treasury = Wallet.objects.get(wallet_type=Wallet.TREASURY)
        Transfer.objects.create(
            sender=treasury,
            recipient=self.wallet,
            amount=Decimal("12.00"),
            status=Transfer.PENDING,
        )

        dashboard = self.client.get(reverse("dashboard"))
        send = self.client.get(reverse("customer_send"))
        detail = self.client.get(reverse("wallet_detail", args=[self.wallet.id]))

        self.assertContains(dashboard, "Including unconfirmed")
        self.assertContains(send, "Pending Balance")
        self.assertContains(detail, "Pending Balance")

    def test_wallet_detail_shows_receive_qr_section(self):
        response = self.client.get(reverse("wallet_detail", args=[self.wallet.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Receive QR")
        self.assertContains(response, "Copy URL")

    def test_pay_link_redirects_customer_to_prefilled_send_form(self):
        response = self.client.get(reverse("patcoin_pay", args=[self.wallet.address]))
        self.assertRedirects(
            response,
            f"{reverse('customer_send')}?to={self.wallet.address}",
        )

    def test_send_form_prefills_address_from_share_link(self):
        response = self.client.get(f"{reverse('customer_send')}?to={self.wallet.address}")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'value="{self.wallet.address}"')

    def test_share_link_redirects_to_login_with_next_when_logged_out(self):
        logged_out = Client()
        response = logged_out.get(reverse("patcoin_pay", args=[self.wallet.address]))
        self.assertRedirects(
            response,
            f"{reverse('login')}?next={reverse('patcoin_pay', args=[self.wallet.address])}",
        )
