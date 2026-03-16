from django.contrib import admin

from .models import Block, InviteLink, Transfer, Wallet


@admin.register(Block)
class BlockAdmin(admin.ModelAdmin):
    list_display = ["index", "status", "block_hash_short", "sealed_at", "created_at"]
    list_filter = ["status"]
    readonly_fields = ["id", "block_hash", "previous_hash", "created_at", "sealed_at"]

    def block_hash_short(self, obj):
        return obj.block_hash[:16] + "..." if obj.block_hash else "—"

    block_hash_short.short_description = "Hash"


@admin.register(Wallet)
class WalletAdmin(admin.ModelAdmin):
    list_display = ["label", "wallet_type", "address_short", "owner", "created_at"]
    list_filter = ["wallet_type"]
    readonly_fields = ["id", "address", "created_at"]

    def address_short(self, obj):
        return obj.address[:14] + "..."

    address_short.short_description = "Address"


@admin.register(Transfer)
class TransferAdmin(admin.ModelAdmin):
    list_display = ["tx_hash_short", "sender", "recipient", "amount", "status", "created_at"]
    list_filter = ["status"]
    readonly_fields = ["id", "tx_hash", "created_at"]

    def tx_hash_short(self, obj):
        return obj.tx_hash[:16] + "..."

    tx_hash_short.short_description = "TX Hash"


@admin.register(InviteLink)
class InviteLinkAdmin(admin.ModelAdmin):
    list_display = [
        "token",
        "note",
        "bonus_amount",
        "is_active",
        "used_at",
        "created_by",
        "created_at",
    ]
    list_filter = ["is_active", "used_at"]
    readonly_fields = ["id", "token", "created_at", "used_at", "used_by"]
