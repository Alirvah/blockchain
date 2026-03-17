from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import REDIRECT_FIELD_NAME, get_user_model, login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.db import models, transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils import timezone
from django.urls import reverse
from django.views.decorators.http import require_POST

from .forms import (
    CustomerTransferForm,
    InviteCreateForm,
    InviteRegistrationForm,
    TransferForm,
    UserCreateForm,
    WalletCreateForm,
)
from .genesis_anchor import get_anchor_status_message
from .health import (
    ensure_background_validation,
    get_cached_anchor_report,
    get_cached_chain_validation,
    invalidate_chain_health_cache,
)
from .models import Block, InviteLink, Transfer, Wallet

User = get_user_model()
INVITE_BONUS_AMOUNT = Decimal("10.00")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def login_view(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    form = AuthenticationForm(request, data=request.POST or None)
    if request.method == "POST" and form.is_valid():
        login(request, form.get_user())
        ensure_background_validation()
        redirect_to = request.POST.get(REDIRECT_FIELD_NAME) or request.GET.get(REDIRECT_FIELD_NAME)
        if redirect_to and url_has_allowed_host_and_scheme(
            redirect_to,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        ):
            return redirect(redirect_to)
        return redirect("dashboard")
    return render(
        request,
        "registration/login.html",
        {
            "form": form,
            "next": request.GET.get(REDIRECT_FIELD_NAME, ""),
        },
    )


def register_view(request):
    """Public self-registration — no invite required."""
    if request.user.is_authenticated:
        return redirect("dashboard")

    form = InviteRegistrationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            user = User.objects.create_user(
                username=form.cleaned_data["username"],
                email=form.cleaned_data.get("email", ""),
                password=form.cleaned_data["password"],
            )
            Wallet.objects.create(
                label=f"{user.username}'s Wallet",
                wallet_type=Wallet.CUSTOMER,
                owner=user,
            )
        login(request, user)
        ensure_background_validation()
        messages.success(request, "Welcome to PatCoin! Your wallet has been created.")
        return redirect("dashboard")

    return render(request, "registration/register.html", {"form": form})


def invite_registration_url(request, invite):
    return request.build_absolute_uri(
        reverse("invite_register", kwargs={"token": invite.token})
    )


def patcoin_share_url(request, wallet_or_address):
    address = wallet_or_address.address if hasattr(wallet_or_address, "address") else str(wallet_or_address)
    return request.build_absolute_uri(
        reverse("patcoin_pay", kwargs={"address": address})
    )


def issue_signup_bonus(wallet, invite):
    treasury = Wallet.objects.filter(wallet_type=Wallet.TREASURY).first()
    if not treasury:
        raise ValueError("Treasury wallet is missing.")
    if treasury.balance < invite.bonus_amount:
        raise ValueError("Treasury has insufficient balance for the invite bonus.")

    tip = Block.get_chain_tip()
    new_index = (tip.index + 1) if tip else 1

    block = Block.objects.create(
        index=new_index,
        status=Block.PENDING,
        previous_hash=tip.block_hash if tip else "0" * 64,
    )
    transfer = Transfer.objects.create(
        sender=treasury,
        recipient=wallet,
        amount=invite.bonus_amount,
        memo=f"Invite bonus via {invite.token}",
        status=Transfer.PENDING,
        block=block,
        created_by=invite.created_by,
    )
    block.seal(user=invite.created_by)
    return transfer, block


def get_invite_funding_status():
    treasury = Wallet.objects.filter(wallet_type=Wallet.TREASURY).first()
    treasury_balance = treasury.balance if treasury else Decimal("0")
    open_invites = InviteLink.objects.filter(is_active=True, used_at__isnull=True)
    outstanding_liability = (
        open_invites.aggregate(total=models.Sum("bonus_amount"))["total"]
        or Decimal("0")
    )
    available_after_liability = treasury_balance - outstanding_liability
    shortfall = max(Decimal("0"), outstanding_liability - treasury_balance)

    return {
        "treasury": treasury,
        "treasury_balance": treasury_balance,
        "open_invite_count": open_invites.count(),
        "outstanding_liability": outstanding_liability,
        "available_after_liability": available_after_liability,
        "shortfall": shortfall,
        "can_create_invite": treasury_balance >= (outstanding_liability + INVITE_BONUS_AMOUNT),
        "existing_invites_funded": treasury_balance >= outstanding_liability,
        "next_invite_bonus": INVITE_BONUS_AMOUNT,
    }


# ---------------------------------------------------------------------------
# Dashboard (role-based)
# ---------------------------------------------------------------------------

@login_required
def dashboard(request):
    if request.user.is_staff:
        return admin_dashboard(request)
    return customer_dashboard(request)


def admin_dashboard(request):
    total_supply = Decimal(str(settings.PATCOIN_TOTAL_SUPPLY))
    treasury = Wallet.objects.filter(wallet_type=Wallet.TREASURY).first()
    treasury_balance = treasury.balance if treasury else Decimal("0")
    circulating = total_supply - treasury_balance
    wallet_count = Wallet.objects.count()
    block_count = Block.objects.filter(status__in=[Block.SEALED, Block.GENESIS]).count()
    pending_transfers = Transfer.objects.filter(status=Transfer.PENDING).count()
    recent_transfers = Transfer.objects.select_related("sender", "recipient")[:10]
    recent_blocks = Block.objects.all()[:5]
    invite_funding = get_invite_funding_status()

    return render(request, "ledger/admin_dashboard.html", {
        "total_supply": total_supply,
        "treasury_balance": treasury_balance,
        "circulating": circulating,
        "wallet_count": wallet_count,
        "block_count": block_count,
        "pending_transfers": pending_transfers,
        "recent_transfers": recent_transfers,
        "recent_blocks": recent_blocks,
        "invite_funding": invite_funding,
    })


def customer_dashboard(request):
    wallet = Wallet.objects.filter(owner=request.user).first()
    recent_transfers = []
    if wallet:
        recent_transfers = Transfer.objects.filter(
            models.Q(sender=wallet) | models.Q(recipient=wallet)
        ).select_related("sender", "recipient")[:10]

    return render(request, "ledger/customer_dashboard.html", {
        "wallet": wallet,
        "recent_transfers": recent_transfers,
        "receive_url": patcoin_share_url(request, wallet) if wallet else "",
    })


# ---------------------------------------------------------------------------
# Admin: Wallet management
# ---------------------------------------------------------------------------

@login_required
def wallet_list(request):
    if not request.user.is_staff:
        return redirect("dashboard")
    wallets = Wallet.objects.select_related("owner").all()
    return render(request, "ledger/wallet_list.html", {"wallets": wallets})


@login_required
def wallet_create(request):
    if not request.user.is_staff:
        return redirect("dashboard")
    form = WalletCreateForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        wallet = form.save()
        messages.success(request, f"Wallet '{wallet.label}' created.")
        return redirect("wallet_list")
    return render(request, "ledger/wallet_create.html", {"form": form})


@login_required
def wallet_detail(request, wallet_id):
    wallet = get_object_or_404(Wallet, id=wallet_id)

    # Customers can only view their own wallet
    if not request.user.is_staff and wallet.owner != request.user:
        return redirect("dashboard")

    transfers = Transfer.objects.filter(
        models.Q(sender=wallet) | models.Q(recipient=wallet)
    ).select_related("sender", "recipient", "block").order_by("-created_at")

    return render(request, "ledger/wallet_detail.html", {
        "wallet": wallet,
        "transfers": transfers,
        "receive_url": patcoin_share_url(request, wallet),
    })


# ---------------------------------------------------------------------------
# Admin: User management
# ---------------------------------------------------------------------------

@login_required
def user_list(request):
    if not request.user.is_staff:
        return redirect("dashboard")
    users = User.objects.prefetch_related("wallets").all()
    return render(request, "ledger/user_list.html", {"users": users})


@login_required
def user_create(request):
    if not request.user.is_staff:
        return redirect("dashboard")
    form = UserCreateForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user, wallet = form.save()
        msg = f"User '{user.username}' created."
        if wallet:
            msg += f" Wallet: {wallet.address}"
        messages.success(request, msg)
        return redirect("user_list")
    return render(request, "ledger/user_create.html", {"form": form})


# ---------------------------------------------------------------------------
# Admin: Invite management
# ---------------------------------------------------------------------------

@login_required
def invite_list(request):
    if not request.user.is_staff:
        return redirect("dashboard")

    invites = InviteLink.objects.select_related("created_by", "used_by").all()
    invite_rows = [
        {
            "invite": invite,
            "registration_url": invite_registration_url(request, invite),
        }
        for invite in invites
    ]
    return render(
        request,
        "ledger/invite_list.html",
        {
            "invite_rows": invite_rows,
            "invite_funding": get_invite_funding_status(),
        },
    )


@login_required
def invite_create(request):
    if not request.user.is_staff:
        return redirect("dashboard")

    invite_funding = get_invite_funding_status()
    form = InviteCreateForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        if not invite_funding["can_create_invite"]:
            form.add_error(
                None,
                "Treasury cannot safely cover another invite bonus. Fund treasury or use existing open invites first.",
            )
        else:
            invite = InviteLink.objects.create(
                note=form.cleaned_data.get("note", ""),
                bonus_amount=INVITE_BONUS_AMOUNT,
                created_by=request.user,
            )
            messages.success(
                request,
                f"Invite link created. Bonus: {invite.bonus_amount:,.2f} PAT.",
            )
            return redirect("invite_list")

    return render(
        request,
        "ledger/invite_create.html",
        {"form": form, "invite_funding": invite_funding},
    )


def invite_register(request, token):
    if request.user.is_authenticated:
        return redirect("dashboard")

    invite = get_object_or_404(InviteLink, token=token)
    if not invite.is_available:
        return render(
            request,
            "registration/register_invite.html",
            {
                "form": None,
                "invite": invite,
                "invite_invalid": True,
                "bonus_amount": invite.bonus_amount,
            },
            status=410,
        )

    form = InviteRegistrationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                user = User.objects.create_user(
                    username=form.cleaned_data["username"],
                    email=form.cleaned_data.get("email", ""),
                    password=form.cleaned_data["password"],
                )
                wallet = Wallet.objects.create(
                    label=f"{user.username}'s Wallet",
                    wallet_type=Wallet.CUSTOMER,
                    owner=user,
                )
                issue_signup_bonus(wallet, invite)
                invite.used_by = user
                invite.used_at = timezone.now()
                invite.save(update_fields=["used_by", "used_at"])
        except ValueError as exc:
            form.add_error(None, str(exc))
        else:
            transaction.on_commit(invalidate_chain_health_cache)
            login(request, user)
            ensure_background_validation()
            messages.success(
                request,
                f"Welcome to PatCoin. Your wallet was funded with {invite.bonus_amount:,.2f} PAT.",
            )
            return redirect("dashboard")

    return render(
        request,
        "registration/register_invite.html",
        {
            "form": form,
            "invite": invite,
            "invite_invalid": False,
            "bonus_amount": invite.bonus_amount,
        },
    )


# ---------------------------------------------------------------------------
# Admin: Transfers
# ---------------------------------------------------------------------------

@login_required
def transfer_list(request):
    if not request.user.is_staff:
        return redirect("dashboard")
    status_filter = request.GET.get("status", "")
    transfers = Transfer.objects.select_related("sender", "recipient", "block")
    if status_filter:
        transfers = transfers.filter(status=status_filter)
    return render(request, "ledger/transfer_list.html", {
        "transfers": transfers[:100],
        "status_filter": status_filter,
    })


@login_required
def transfer_create(request):
    if not request.user.is_staff:
        return redirect("dashboard")
    form = TransferForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            transfer = Transfer.objects.create(
                sender=form.cleaned_data["sender"],
                recipient=form.cleaned_data["recipient"],
                amount=form.cleaned_data["amount"],
                memo=form.cleaned_data.get("memo", ""),
                status=Transfer.PENDING,
                created_by=request.user,
            )
        messages.success(request, f"Transfer of {transfer.amount:,.2f} PAT created (pending).")
        return redirect("transfer_list")
    return render(request, "ledger/transfer_create.html", {"form": form})


# ---------------------------------------------------------------------------
# Customer: Send transfer
# ---------------------------------------------------------------------------

@login_required
def customer_send(request):
    if request.user.is_staff:
        return redirect("transfer_create")
    wallet = Wallet.objects.filter(owner=request.user).first()
    if not wallet:
        messages.error(request, "You don't have a wallet yet.")
        return redirect("dashboard")

    prefill_address = request.GET.get("to", "").strip()
    if request.method == "POST":
        form = CustomerTransferForm(request.POST, sender_wallet=wallet)
    else:
        form = CustomerTransferForm(
            initial={"recipient_address": prefill_address} if prefill_address else None,
            sender_wallet=wallet,
        )
    if request.method == "POST" and form.is_valid():
        recipient = form.cleaned_data["recipient_address"]
        with transaction.atomic():
            Transfer.objects.create(
                sender=wallet,
                recipient=recipient,
                amount=form.cleaned_data["amount"],
                memo=form.cleaned_data.get("memo", ""),
                status=Transfer.PENDING,
                created_by=request.user,
            )
        messages.success(request, f"Transfer of {form.cleaned_data['amount']:,.2f} PAT submitted.")
        return redirect("dashboard")
    return render(
        request,
        "ledger/customer_send.html",
        {
            "form": form,
            "wallet": wallet,
            "prefill_address": prefill_address,
        },
    )


@login_required
def patcoin_pay(request, address):
    wallet = get_object_or_404(Wallet, address=address)
    if request.user.is_staff:
        messages.info(
            request,
            f"Share link opened for {wallet.address}. Use the admin transfer screen to send manually.",
        )
        return redirect("transfer_create")
    return redirect(f"{reverse('customer_send')}?to={wallet.address}")


# ---------------------------------------------------------------------------
# Admin: Block management
# ---------------------------------------------------------------------------

@login_required
def block_list(request):
    blocks = Block.objects.all()
    if not request.user.is_staff:
        blocks = blocks.filter(status__in=[Block.SEALED, Block.GENESIS])
    return render(request, "ledger/block_list.html", {"blocks": blocks})


@login_required
def block_detail(request, block_id):
    block = get_object_or_404(Block.objects.select_related("sealed_by"), id=block_id)
    if not request.user.is_staff and block.status == Block.PENDING:
        return redirect("block_list")
    transfers = block.transfers.select_related("sender", "recipient").order_by("created_at")
    return render(request, "ledger/block_detail.html", {"blk": block, "transfers": transfers})


@login_required
def pending_queue(request):
    if not request.user.is_staff:
        return redirect("dashboard")
    pending = Transfer.objects.filter(status=Transfer.PENDING).select_related(
        "sender", "recipient"
    )
    return render(request, "ledger/pending_queue.html", {"pending_transfers": pending})


@login_required
@require_POST
def seal_block(request):
    if not request.user.is_staff:
        return redirect("dashboard")

    pending = Transfer.objects.filter(status=Transfer.PENDING)
    if not pending.exists():
        messages.warning(request, "No pending transfers to seal.")
        return redirect("pending_queue")

    with transaction.atomic():
        tip = Block.get_chain_tip()
        new_index = (tip.index + 1) if tip else 1

        block = Block.objects.create(
            index=new_index,
            status=Block.PENDING,
            previous_hash=tip.block_hash if tip else "0" * 64,
        )

        pending.update(block=block)
        block.seal(user=request.user)
        transaction.on_commit(invalidate_chain_health_cache)

    messages.success(
        request,
        f"Block #{block.index} sealed with {pending.count()} transfer(s). "
        f"Hash: {block.block_hash[:16]}..."
    )
    return redirect("block_detail", block_id=block.id)


# ---------------------------------------------------------------------------
# Explorer
# ---------------------------------------------------------------------------

@login_required
def explorer(request):
    force_refresh = request.GET.get("refresh") == "1"
    total_supply = Decimal(str(settings.PATCOIN_TOTAL_SUPPLY))
    treasury = Wallet.objects.filter(wallet_type=Wallet.TREASURY).first()
    treasury_balance = treasury.balance if treasury else Decimal("0")
    circulating = total_supply - treasury_balance

    chain_tip = Block.get_chain_tip()
    recent_blocks = Block.objects.filter(
        status__in=[Block.SEALED, Block.GENESIS]
    )[:10]
    recent_transfers = Transfer.objects.filter(
        status=Transfer.CONFIRMED
    ).select_related("sender", "recipient")[:10]
    active_wallets = Wallet.objects.filter(wallet_type=Wallet.CUSTOMER).count()
    pending_count = Transfer.objects.filter(status=Transfer.PENDING).count()

    chain_validation = get_cached_chain_validation(force_refresh=force_refresh)
    anchor_report = get_cached_anchor_report(force_refresh=force_refresh)

    return render(request, "ledger/explorer.html", {
        "total_supply": total_supply,
        "treasury_balance": treasury_balance,
        "circulating": circulating,
        "chain_tip": chain_tip,
        "recent_blocks": recent_blocks,
        "recent_transfers": recent_transfers,
        "active_wallets": active_wallets,
        "pending_count": pending_count,
        "chain_valid": chain_validation["is_valid"],
        "chain_errors": chain_validation["errors"],
        "anchor_report": anchor_report,
        "anchor_status_message": get_anchor_status_message(anchor_report),
        "force_refresh": force_refresh,
    })


# ---------------------------------------------------------------------------
# How It Works (educational)
# ---------------------------------------------------------------------------

@login_required
def how_it_works(request):
    """Educational view explaining the blockchain with real data."""
    force_refresh = request.GET.get("refresh") == "1"
    total_supply = Decimal(str(settings.PATCOIN_TOTAL_SUPPLY))
    treasury = Wallet.objects.filter(wallet_type=Wallet.TREASURY).first()

    # Genesis block — the root of everything
    genesis = Block.objects.filter(status=Block.GENESIS).first()
    genesis_tx = None
    if genesis:
        genesis_tx = genesis.transfers.select_related("sender", "recipient").first()

    # Full chain for the walkthrough
    chain = list(
        Block.objects.filter(status__in=[Block.SEALED, Block.GENESIS])
        .order_by("index")
        .prefetch_related("transfers")
    )

    # Pick a sample sealed block (latest) to demonstrate hashing
    sample_block = None
    sample_transfers = []
    for b in reversed(chain):
        if b.status == Block.SEALED:
            sample_block = b
            sample_transfers = list(
                b.transfers.select_related("sender", "recipient").order_by("created_at")
            )
            break

    # Show a pair of adjacent blocks to demonstrate linkage
    link_pair = None
    for i in range(1, len(chain)):
        if chain[i].status == Block.SEALED:
            link_pair = (chain[i - 1], chain[i])

    # Chain validation — live
    chain_validation = get_cached_chain_validation(force_refresh=force_refresh)

    # Anchor data — live
    anchor_report = get_cached_anchor_report(force_refresh=force_refresh)

    # A recent confirmed transfer to use as example
    example_tx = (
        Transfer.objects.filter(status=Transfer.CONFIRMED)
        .exclude(sender__isnull=True)
        .select_related("sender", "recipient", "block")
        .order_by("-created_at")
        .first()
    )

    # All confirmed transfers for the full ledger view
    all_transfers = list(
        Transfer.objects.filter(status=Transfer.CONFIRMED)
        .select_related("sender", "recipient", "block")
        .order_by("created_at")
    )

    ledger_preview_size = 3
    if len(all_transfers) > ledger_preview_size * 2:
        ledger_preview = all_transfers[:ledger_preview_size] + all_transfers[-ledger_preview_size:]
        omitted_transfer_count = len(all_transfers) - len(ledger_preview)
    else:
        ledger_preview = all_transfers
        omitted_transfer_count = 0

    return render(request, "ledger/how_it_works.html", {
        "total_supply": total_supply,
        "treasury": treasury,
        "genesis": genesis,
        "genesis_tx": genesis_tx,
        "chain": chain,
        "chain_length": len(chain),
        "sample_block": sample_block,
        "sample_transfers": sample_transfers,
        "link_pair": link_pair,
        "chain_valid": chain_validation["is_valid"],
        "chain_errors": chain_validation["errors"],
        "anchor_report": anchor_report,
        "example_tx": example_tx,
        "all_transfers": ledger_preview,
        "omitted_transfer_count": omitted_transfer_count,
        "force_refresh": force_refresh,
    })


# ---------------------------------------------------------------------------
# Chain validation
# ---------------------------------------------------------------------------

@login_required
def chain_validate(request):
    force_refresh = request.GET.get("refresh") == "1"
    chain_validation = get_cached_chain_validation(force_refresh=force_refresh)
    blocks = Block.objects.filter(
        status__in=[Block.SEALED, Block.GENESIS]
    ).order_by("index")
    return render(request, "ledger/chain_validate.html", {
        "is_valid": chain_validation["is_valid"],
        "errors": chain_validation["errors"],
        "blocks": blocks,
        "force_refresh": force_refresh,
    })


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------

@login_required
def provenance(request, wallet_id):
    force_refresh = request.GET.get("refresh") == "1"
    wallet = get_object_or_404(Wallet, id=wallet_id)

    # Any logged-in user can view provenance (it's a transparency/verification page)

    # Trace the lineage: walk incoming transfers back to genesis
    lineage = []
    visited = set()
    queue = list(
        wallet.incoming_transfers.filter(status=Transfer.CONFIRMED)
        .select_related("sender", "recipient", "block")
        .order_by("-created_at")
    )

    while queue:
        tx = queue.pop(0)
        if tx.id in visited:
            continue
        visited.add(tx.id)
        lineage.append(tx)

        if tx.sender:
            parent_txs = tx.sender.incoming_transfers.filter(
                status=Transfer.CONFIRMED
            ).select_related("sender", "recipient", "block")
            for ptx in parent_txs:
                if ptx.id not in visited:
                    queue.append(ptx)

    genesis_block = Block.objects.filter(status=Block.GENESIS).first()
    treasury = Wallet.objects.filter(wallet_type=Wallet.TREASURY).first()

    reaches_genesis = any(tx.sender is None for tx in lineage)

    # Chain validation
    chain_validation = get_cached_chain_validation(force_refresh=force_refresh)

    # Anchor verification
    anchor_report = get_cached_anchor_report(force_refresh=force_refresh)

    # Collect the unique blocks that carry this wallet's lineage
    lineage_block_ids = {tx.block_id for tx in lineage if tx.block_id}
    lineage_blocks = list(
        Block.objects.filter(id__in=lineage_block_ids)
        .order_by("index")
    )

    # All wallet transactions (not just lineage), most recent first
    all_transfers = Transfer.objects.filter(
        models.Q(sender=wallet) | models.Q(recipient=wallet)
    ).select_related("sender", "recipient", "block").order_by("-created_at")

    # Build verification steps
    checks = []
    checks.append({
        "label": "Genesis block exists",
        "ok": genesis_block is not None,
        "detail": f"Block #0 hash {genesis_block.block_hash[:16]}..." if genesis_block else "No genesis block found",
    })
    checks.append({
        "label": "Chain hashes are valid",
        "ok": chain_validation["is_valid"],
        "detail": (
            "All block hashes verified"
            if chain_validation["is_valid"]
            else f"{len(chain_validation['errors'])} error(s) found"
        ),
    })
    checks.append({
        "label": "Genesis anchored to file",
        "ok": anchor_report.get("anchor_exists", False),
        "detail": anchor_report["manifest_path"],
    })
    checks.append({
        "label": "Anchor matches live database",
        "ok": anchor_report.get("db_matches_anchor", False),
        "detail": "Hashes match" if anchor_report.get("db_matches_anchor") else "Mismatch or anchor missing",
    })
    checks.append({
        "label": "Anchor commit on remote",
        "ok": anchor_report["git"].get("remote_verified", False),
        "detail": (
            f"Verified on {anchor_report['git'].get('remote_name', 'origin')}"
            if anchor_report["git"].get("remote_verified")
            else "Not yet verified remotely"
        ),
    })
    checks.append({
        "label": "Funds trace to genesis mint",
        "ok": reaches_genesis,
        "detail": (
            f"Verified through {len(lineage)} transfer(s)"
            if reaches_genesis
            else "No confirmed path to genesis"
        ),
    })

    all_ok = all(c["ok"] for c in checks)

    return render(request, "ledger/provenance.html", {
        "wallet": wallet,
        "lineage": lineage,
        "genesis_block": genesis_block,
        "treasury": treasury,
        "reaches_genesis": reaches_genesis,
        "chain_valid": chain_validation["is_valid"],
        "anchor_report": anchor_report,
        "anchor_status_message": get_anchor_status_message(anchor_report),
        "lineage_blocks": lineage_blocks,
        "all_transfers": all_transfers,
        "checks": checks,
        "all_checks_pass": all_ok,
        "force_refresh": force_refresh,
    })


# ---------------------------------------------------------------------------
# API endpoints for charts
# ---------------------------------------------------------------------------

@login_required
def api_supply_data(request):
    total_supply = Decimal(str(settings.PATCOIN_TOTAL_SUPPLY))
    treasury = Wallet.objects.filter(wallet_type=Wallet.TREASURY).first()
    treasury_balance = treasury.balance if treasury else Decimal("0")
    circulating = total_supply - treasury_balance

    return JsonResponse({
        "total_supply": float(total_supply),
        "treasury": float(treasury_balance),
        "circulating": float(circulating),
    })


@login_required
def api_block_timeline(request):
    blocks = Block.objects.filter(
        status__in=[Block.SEALED, Block.GENESIS]
    ).order_by("index")[:50]

    data = []
    for b in blocks:
        data.append({
            "index": b.index,
            "hash": b.block_hash[:12],
            "tx_count": b.transfers.count(),
            "sealed_at": b.sealed_at.isoformat() if b.sealed_at else None,
            "status": b.status,
        })

    return JsonResponse({"blocks": data})


@login_required
def api_chain_graph(request):
    """Return chain data for visualization."""
    blocks = Block.objects.filter(
        status__in=[Block.SEALED, Block.GENESIS]
    ).order_by("index").prefetch_related("transfers")

    nodes = []
    edges = []
    for b in blocks:
        nodes.append({
            "id": str(b.id),
            "index": b.index,
            "hash": b.block_hash[:12],
            "status": b.status,
            "tx_count": b.transfers.count(),
        })

    block_list = list(blocks)
    for i in range(1, len(block_list)):
        edges.append({
            "from": str(block_list[i - 1].id),
            "to": str(block_list[i].id),
        })

    return JsonResponse({"nodes": nodes, "edges": edges})
