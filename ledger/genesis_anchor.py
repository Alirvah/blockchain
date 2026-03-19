from __future__ import annotations

import json
import hashlib
import subprocess
from decimal import Decimal
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
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


def get_anchor_proof_path(manifest_path: Path | None = None) -> Path:
    manifest_path = manifest_path or get_anchor_manifest_path()
    return manifest_path.with_name(f"{manifest_path.stem}-proof.json")


def get_anchor_ots_path(manifest_path: Path | None = None) -> Path:
    manifest_path = manifest_path or get_anchor_manifest_path()
    return manifest_path.with_name(f"{manifest_path.name}.ots")


def _sha256_file(file_path: Path) -> str | None:
    if not file_path.exists() or not file_path.is_file():
        return None

    digest = hashlib.sha256()
    with file_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def load_genesis_anchor_proof(proof_path: Path | None = None) -> dict[str, Any]:
    proof_path = proof_path or get_anchor_proof_path()
    return json.loads(proof_path.read_text(encoding="utf-8"))


def _normalize_anchor_attestations(anchor: dict[str, Any] | None) -> list[dict[str, Any]]:
    anchor = anchor or {}
    attestations = anchor.get("attestations")
    if isinstance(attestations, list):
        normalized = [item for item in attestations if isinstance(item, dict)]
    else:
        normalized = []

    # Backward compatibility with the older single-attestation shape.
    if not normalized and any(
        anchor.get(field) is not None
        for field in ("txid", "block_height", "block_hash", "block_time", "explorer_url")
    ):
        normalized = [
            {
                "txid": anchor.get("txid"),
                "block_height": anchor.get("block_height"),
                "block_hash": anchor.get("block_hash"),
                "block_time": anchor.get("block_time"),
                "explorer_url": anchor.get("explorer_url"),
            }
        ]

    def _sort_key(item: dict[str, Any]) -> tuple[int, str]:
        height = item.get("block_height")
        if isinstance(height, int):
            return (height, item.get("txid") or "")
        return (10**12, item.get("txid") or "")

    return sorted(normalized, key=_sort_key)


def get_bitcoin_anchor_report(manifest_path: Path | None = None) -> dict[str, Any]:
    manifest_path = manifest_path or get_anchor_manifest_path()
    proof_path = get_anchor_proof_path(manifest_path)
    ots_path = get_anchor_ots_path(manifest_path)
    report: dict[str, Any] = {
        "status": "missing",
        "proof_path": str(proof_path),
        "proof_exists": proof_path.exists(),
        "proof": None,
        "ots_path": str(ots_path),
        "ots_exists": ots_path.exists(),
        "subject": None,
        "anchor": None,
        "attestations": [],
        "primary_attestation": None,
        "verification": None,
        "error": None,
    }

    if proof_path.exists():
        try:
            proof = load_genesis_anchor_proof(proof_path)
        except (json.JSONDecodeError, OSError) as exc:
            report["status"] = "invalid"
            report["error"] = str(exc)
            return report

        report["proof"] = proof
        report["subject"] = proof.get("subject")
        report["anchor"] = proof.get("bitcoin_anchor")
        report["verification"] = proof.get("verification")

    anchor = report.get("anchor") or {}
    verification = report.get("verification") or {}
    report["attestations"] = _normalize_anchor_attestations(anchor)
    report["primary_attestation"] = report["attestations"][0] if report["attestations"] else None

    if verification.get("bitcoin_proof_verified"):
        report["status"] = "verified"
    elif report["attestations"]:
        report["status"] = "recorded"
    elif report["ots_exists"] or report["proof_exists"]:
        report["status"] = "proof_file_present"

    return report


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


def _normalize_remote_web_url(remote_url: str | None) -> str | None:
    if not remote_url:
        return None

    remote_url = remote_url.strip()
    if remote_url.startswith("git@"):
        host_and_path = remote_url.split("git@", 1)[1]
        host, _, repo_path = host_and_path.partition(":")
        if host and repo_path:
            normalized = f"https://{host}/{repo_path}"
        else:
            return None
    elif remote_url.startswith("http://") or remote_url.startswith("https://"):
        normalized = remote_url
    else:
        return None

    if normalized.endswith(".git"):
        normalized = normalized[:-4]
    return normalized.rstrip("/")


def _build_commit_url(project_url: str | None, commit_sha: str | None) -> str | None:
    if not project_url or not commit_sha:
        return None

    parsed = urlparse(project_url)
    host = parsed.netloc.lower()
    if "github.com" in host or "gitlab.com" in host:
        return f"{project_url}/commit/{commit_sha}"
    return None


def _build_git_file_url(project_url: str | None, commit_sha: str | None, relative_path: str | None) -> str | None:
    if not project_url or not commit_sha or not relative_path:
        return None

    parsed = urlparse(project_url)
    host = parsed.netloc.lower()
    path = parsed.path.strip("/")
    if "github.com" in host:
        return f"{project_url}/blob/{commit_sha}/{relative_path}"
    if "gitlab.com" in host:
        return f"{project_url}/-/blob/{commit_sha}/{relative_path}"
    return None


def _build_git_raw_file_url(project_url: str | None, commit_sha: str | None, relative_path: str | None) -> str | None:
    if not project_url or not commit_sha or not relative_path:
        return None

    parsed = urlparse(project_url)
    host = parsed.netloc.lower()
    path = parsed.path.strip("/")
    if "github.com" in host and path:
        return f"https://raw.githubusercontent.com/{path}/{commit_sha}/{relative_path}"
    if "gitlab.com" in host:
        return f"{project_url}/-/raw/{commit_sha}/{relative_path}"
    return None


def _verify_commit_url_online(commit_url: str | None) -> tuple[bool, str | None]:
    if not commit_url:
        return False, "Remote commit URL is unavailable for online verification."

    request = Request(
        commit_url,
        headers={"User-Agent": "PatCoin-Genesis-Anchor/1.0"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=10) as response:
            if 200 <= response.status < 400:
                return True, None
            return False, f"Remote commit URL responded with HTTP {response.status}."
    except HTTPError as exc:
        return False, f"Remote commit URL responded with HTTP {exc.code}."
    except URLError as exc:
        return False, f"Remote commit URL could not be reached: {exc.reason}."


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
            "project_url": None,
            "commit_url": None,
            "remote_verified": False,
            "remote_check_error": None,
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
    project_url = None
    commit_url = None
    remote_verified = False
    remote_check_error = None
    remote_info = _run_git("remote", "get-url", "origin")
    if remote_info and remote_info.returncode == 0:
        remote_name = "origin"
        remote_url = remote_info.stdout.strip() or None
        project_url = _normalize_remote_web_url(remote_url)
        commit_url = _build_commit_url(project_url, commit_sha)

    if commit_url and commit_sha:
        remote_verified, remote_check_error = _verify_commit_url_online(commit_url)
    elif remote_url and commit_sha:
        remote_check_error = "Remote repository is configured, but the app could not derive a public commit URL for online verification."

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
        "project_url": project_url,
        "commit_url": commit_url,
        "remote_verified": remote_verified,
        "remote_check_error": remote_check_error,
    }


def get_git_file_metadata(file_path: Path) -> dict[str, Any]:
    file_path = Path(file_path)
    metadata: dict[str, Any] = {
        "path": str(file_path),
        "exists": file_path.exists(),
        "relative_path": None,
        "sha256": _sha256_file(file_path),
        "committed": False,
        "commit_sha": None,
        "commit_short": None,
        "local_blob_oid": None,
        "git_blob_oid": None,
        "matches_git": False,
        "project_url": None,
        "view_url": None,
        "raw_url": None,
    }

    repo_check = _run_git("rev-parse", "--show-toplevel")
    if not repo_check or repo_check.returncode != 0:
        return metadata

    repo_root = Path(repo_check.stdout.strip())
    try:
        relative_path = file_path.relative_to(repo_root)
    except ValueError:
        return metadata

    relative_str = relative_path.as_posix()
    metadata["relative_path"] = relative_str

    remote_info = _run_git("remote", "get-url", "origin")
    project_url = None
    if remote_info and remote_info.returncode == 0:
        project_url = _normalize_remote_web_url(remote_info.stdout.strip() or None)
    metadata["project_url"] = project_url

    commit_info = _run_git("log", "-1", "--format=%H%n%h", "--", relative_str)
    if commit_info and commit_info.returncode == 0 and commit_info.stdout.strip():
        lines = commit_info.stdout.strip().splitlines()
        if len(lines) >= 2:
            metadata["commit_sha"], metadata["commit_short"] = lines[:2]
            metadata["committed"] = True

    if metadata["exists"]:
        local_blob = _run_git("hash-object", str(file_path))
        if local_blob and local_blob.returncode == 0:
            metadata["local_blob_oid"] = local_blob.stdout.strip() or None

    if metadata["committed"] and metadata["relative_path"]:
        git_blob = _run_git("rev-parse", f"{metadata['commit_sha']}:{metadata['relative_path']}")
        if git_blob and git_blob.returncode == 0:
            metadata["git_blob_oid"] = git_blob.stdout.strip() or None

    metadata["matches_git"] = bool(
        metadata["local_blob_oid"]
        and metadata["git_blob_oid"]
        and metadata["local_blob_oid"] == metadata["git_blob_oid"]
    )
    metadata["view_url"] = _build_git_file_url(project_url, metadata["commit_sha"], metadata["relative_path"])
    metadata["raw_url"] = _build_git_raw_file_url(project_url, metadata["commit_sha"], metadata["relative_path"])
    return metadata


def get_genesis_anchor_report() -> dict[str, Any]:
    manifest_path = get_anchor_manifest_path()
    ots_path = get_anchor_ots_path(manifest_path)
    report: dict[str, Any] = {
        "status": STATUS_ANCHOR_MISSING,
        "manifest_path": str(manifest_path),
        "anchor_exists": manifest_path.exists(),
        "mismatches": [],
        "live": None,
        "anchor": None,
        "db_matches_anchor": False,
        "git": get_git_anchor_metadata(manifest_path),
        "bitcoin_proof": get_bitcoin_anchor_report(manifest_path),
        "files": {
            "manifest": get_git_file_metadata(manifest_path),
            "ots": get_git_file_metadata(ots_path),
        },
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
        return "Live genesis matches the committed manifest, and the anchor commit was verified against the remote repository online."
    if status == STATUS_REMOTE_UNVERIFIED:
        return "Live genesis matches the local Git anchor, but the app could not confirm that anchor commit exists on the remote repository online."
    if status == STATUS_GIT_UNAVAILABLE:
        return "Live genesis matches the manifest, but Git metadata is unavailable on this server."
    if status == STATUS_ANCHOR_MISSING:
        return "No genesis anchor manifest has been committed yet, so users can only trust the live database."
    return "Live genesis does not match the anchored manifest. Treat the chain as tampered until the mismatch is explained."
