from django.contrib.auth.views import LogoutView
from django.urls import path

from . import views

urlpatterns = [
    # Auth
    path("login/", views.login_view, name="login"),
    path("logout/", LogoutView.as_view(), name="logout"),

    # Dashboard
    path("", views.dashboard, name="dashboard"),

    # Admin: Users
    path("users/", views.user_list, name="user_list"),
    path("users/create/", views.user_create, name="user_create"),

    # Wallets
    path("wallets/", views.wallet_list, name="wallet_list"),
    path("wallets/create/", views.wallet_create, name="wallet_create"),
    path("wallets/<uuid:wallet_id>/", views.wallet_detail, name="wallet_detail"),

    # Transfers
    path("transfers/", views.transfer_list, name="transfer_list"),
    path("transfers/create/", views.transfer_create, name="transfer_create"),
    path("send/", views.customer_send, name="customer_send"),

    # Blocks
    path("blocks/", views.block_list, name="block_list"),
    path("blocks/<uuid:block_id>/", views.block_detail, name="block_detail"),

    # Operations
    path("pending/", views.pending_queue, name="pending_queue"),
    path("seal/", views.seal_block, name="seal_block"),

    # Explorer
    path("explorer/", views.explorer, name="explorer"),
    path("chain/validate/", views.chain_validate, name="chain_validate"),

    # Provenance
    path("provenance/<uuid:wallet_id>/", views.provenance, name="provenance"),

    # API
    path("api/supply/", views.api_supply_data, name="api_supply"),
    path("api/blocks/", views.api_block_timeline, name="api_blocks"),
    path("api/chain-graph/", views.api_chain_graph, name="api_chain_graph"),
]
