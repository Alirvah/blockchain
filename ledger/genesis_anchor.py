from __future__ import annotations

import json
import subprocess
from decimal import Decimal
from pathlib import Path
from typing import Any

from django.conf import settings
from django.utils import timezone

from .models import Block, Transfer, Wallet

STATUS_VALID = "valid"
STATUS_MISMATCH = "mismatch"
STATUS_ANCHOR_MISSING = "anchor_missing"
STATUS_GIT_UNAVAILABLE = "git_unavailable"
STATUS_REMOTE_UNVERIFIED = "remote_unverified"


class GenesisAnchorError(Exception):
    """Raised when the live genesis state cannot be collected."""


def get_anchor_manifest_path() -> Path:
    configured = Path(str(settings.GENESIS_ANCHOR_PATH))
    if configured.is_absolute():
        return configured
    return Path(settings.BASE_DIR) / configured


def _format_amount(value: Decimal | str | int | float) -> str:
    return f"{Decimal(str(value)).quantize(Decimal('0.01'))}"


def _serialize_datetime(value) -> str | None:
    if not value:
        return None
    return value.isoformat()


def collect_live_genesis_data() -> dict[str, Any]:
    genesis = Block.objects.filter(status=Block.GENESIS).first()
    if not genesis:
        raise GenesisAnchorError("Genesis block not found.")

    treasury = Wallet.objects.filter(wallet_type=Wallet.TREASURY).first()
    if not treasury:
        raise GenesisAnchorError("Treasury wallet not found.")

    mint_tx = Transfer.objects.filter(
        sender__isnull=True,
        recipient=treasury,
        block=genesis,
    ).first()
    if not mint_tx:
        raise GenesisAnchorError("Genesis mint transfer not found.")

    return {
        "manifest_version": 1,
        "network": "PatCoin",
        "exported_at": timezone.now().isoformat(),
        "total_supply": _format_amount(settings.PATCOIN_TOTAL_SUPPLY),
        "block": {
            "id": str(genesis.id),
            "index": genesis.index,
            "status": genesis.status,
            "previous_hash": genesis.previous_hash,
            "block_hash": genesis.block_hash,
            "created_at": _serialize_datetime(genesis.created_at),
            "sealed_at": _serialize_datetime(genesis.sealed_at),
            "nonce": genesis.nonce,
        },
        "treasury_wallet": {
            "id": str(treasury.id),
            "label": treasury.label,
            "wallet_type": treasury.wallet_type,
            "address": treasury.address,
            "created_at": _serialize_datetime(treasury.created_at),
        },
        "mint_transfer": {
            "id": str(mint_tx.id),
            "tx_hash": mint_tx.tx_hash,
            "amount": _format_amount(mint_tx.amount),
            "memo": mint_tx.memo,
            "status": mint_tx.status,
            "created_at": _serialize_datetime(mint_tx.created_at),
        },
    }


def export_genesis_anchor(force: bool = False, manifest_path: Path | None = None) -> tuple[Path, dict[str, Any]]:
    manifest_path = manifest_path or get_anchor_manifest_path()
    if manifest_path.exists() and not force:
        raise FileExistsError(f"Genesis anchor already exists at {manifest_path}.")

    manifest = collect_live_genesis_data()
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest_path, manifest


def load_genesis_anchor(manifest_path: Path | None = None) -> dict[str, Any]:
    manifest_path = manifest_path or get_anchor_manifest_path()
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def _compare_field(mismatches: list[str], label: str, expected: Any, actual: Any) -> None:
    if expected != actual:
        mismatches.append(f"{label}: anchored={expected!r} live={actual!r}")


def compare_anchor_to_live(anchor: dict[str, Any], live: dict[str, Any]) -> list[str]:
    mismatches: list[str] = []

    _compare_field(mismatches, "total_supply", anchor.get("total_supply"), live.get("total_supply"))

    for section, fields in {
        "block": ["id", "index", "status", "previous_hash", "block_hash", "created_at", "sealed_at", "nonce"],
        "treasury_wallet": ["id", "label", "wallet_type", "address", "created_at"],
        "mint_transfer": ["id", "tx_hash", "amount", "memo", "status", "created_at"],
    }.items():
        anchored_section = anchor.get(section, {})
        live_section = live.get(section, {})
        for field in fields:
            _compare_field(
                mismatches,
                f"{section}.{field}",
                anchored_section.get(field),
                live_section.get(field),
            )

    return mismatches


def _run_git(*args: str) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", "-c", f"safe.directory={settings.BASE_DIR}", *args],
            cwd=settings.BASE_DIR,
            check=False,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, NotADirectoryError):
        return None


def get_git_anchor_metadata(manifest_path: Path | None = None) -> dict[str, Any]:
    manifest_path = manifest_path or get_anchor_manifest_path()
    repo_check = _run_git("rev-parse", "--show-toplevel")
    if not repo_check or repo_check.returncode != 0:
        return {
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

    repo_root = Path(repo_check.stdout.strip())
    try:
        relative_manifest = manifest_path.relative_to(repo_root)
    except ValueError:
        relative_manifest = manifest_path

    commit_info = _run_git("log", "-1", "--format=%H%n%h%n%ct%n%s", "--", str(relative_manifest))
    commit_sha = commit_short = commit_subject = commit_timestamp = None
    manifest_committed = False
    if commit_info and commit_info.returncode == 0 and commit_info.stdout.strip():
        lines = commit_info.stdout.strip().splitlines()
        if len(lines) >= 4:
            commit_sha, commit_short, commit_timestamp, commit_subject = lines[:4]
            manifest_committed = True

    remote_name = None
    remote_url = None
    remote_verified = False
    remote_info = _run_git("remote", "get-url", "origin")
    if remote_info and remote_info.returncode == 0:
        remote_name = "origin"
        remote_url = remote_info.stdout.strip() or None

    if remote_url and commit_sha:
        contains = _run_git("branch", "-r", "--contains", commit_sha)
        if contains and contains.returncode == 0:
            remote_verified = any(
                line.strip().startswith("origin/")
                for line in contains.stdout.splitlines()
            )

    return {
        "git_available": True,
        "repo_root": str(repo_root),
        "manifest_committed": manifest_committed,
        "commit_sha": commit_sha,
        "commit_short": commit_short,
        "commit_subject": commit_subject,
        "commit_timestamp": commit_timestamp,
        "remote_name": remote_name,
        "remote_url": remote_url,
        "remote_verified": remote_verified,
    }


def get_genesis_anchor_report() -> dict[str, Any]:
    manifest_path = get_anchor_manifest_path()
    report: dict[str, Any] = {
        "status": STATUS_ANCHOR_MISSING,
        "manifest_path": str(manifest_path),
        "anchor_exists": manifest_path.exists(),
        "mismatches": [],
        "live": None,
        "anchor": None,
        "db_matches_anchor": False,
        "git": get_git_anchor_metadata(manifest_path),
    }

    try:
        report["live"] = collect_live_genesis_data()
    except GenesisAnchorError as exc:
        report["mismatches"] = [str(exc)]
        report["status"] = STATUS_MISMATCH
        return report

    if not manifest_path.exists():
        return report

    report["anchor"] = load_genesis_anchor(manifest_path)
    report["mismatches"] = compare_anchor_to_live(report["anchor"], report["live"])
    report["db_matches_anchor"] = not report["mismatches"]

    if report["mismatches"]:
        report["status"] = STATUS_MISMATCH
    elif not report["git"]["git_available"]:
        report["status"] = STATUS_GIT_UNAVAILABLE
    elif not report["git"]["remote_verified"]:
        report["status"] = STATUS_REMOTE_UNVERIFIED
    else:
        report["status"] = STATUS_VALID

    return report


def get_anchor_status_message(report: dict[str, Any]) -> str:
    status = report["status"]
    if status == STATUS_VALID:
        return "Live genesis matches the committed manifest, and the anchor commit is visible from a remote Git history."
    if status == STATUS_REMOTE_UNVERIFIED:
        return "Live genesis matches the local Git anchor, but no pushed remote copy of that anchor commit could be confirmed yet."
    if status == STATUS_GIT_UNAVAILABLE:
        return "Live genesis matches the manifest, but Git metadata is unavailable on this server."
    if status == STATUS_ANCHOR_MISSING:
        return "No genesis anchor manifest has been committed yet, so users can only trust the live database."
    return "Live genesis does not match the anchored manifest. Treat the chain as tampered until the mismatch is explained."
