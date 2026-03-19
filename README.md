# PatCoin Blockchain Ledger

A centralized blockchain ledger application for managing a cryptocurrency called **PAT**. Built with Django and PostgreSQL, it provides wallets, transfers, block sealing, chain validation, and a full web UI for both administrators and customers.

## How It Works

### Core Concepts

**PatCoin operates as a centralized blockchain.** All PAT tokens originate from a single genesis mint into a treasury wallet. Administrators distribute tokens to customer wallets via transfers. Every transfer starts as "pending" and gets grouped into a block that is sealed (hashed and linked to the previous block), forming an immutable chain.

```
Genesis Block (#0)           Sealed Block (#1)           Sealed Block (#2)
┌─────────────────┐         ┌─────────────────┐         ┌─────────────────┐
│ hash: a3f8...   │◄────────│ prev: a3f8...   │◄────────│ prev: 7c2d...   │
│ nonce: GENESIS  │         │ hash: 7c2d...   │         │ hash: e91b...   │
│                 │         │                 │         │                 │
│ MINT 1,000,000  │         │ Treasury → Alice│         │ Alice → Bob     │
│   → Treasury    │         │ Treasury → Bob  │         │ Bob → Carol     │
└─────────────────┘         └─────────────────┘         └─────────────────┘
```

### Lifecycle of a Transfer

1. **Created** -- A user (admin or customer) initiates a transfer. The system locks the sender's wallet row and checks that the balance (including already-pending outflows) covers the amount. If it does, a `PENDING` transfer is created.
2. **Sealed into a block** -- An admin clicks "Seal" (or the auto-seal worker runs every 5 minutes). All pending transfers are collected into a new block. The block's SHA-256 hash is computed over its index, the previous block's hash, timestamp, nonce, and all transfer hashes. The block becomes `SEALED` and every transfer in it becomes `CONFIRMED`.
3. **Verified** -- Anyone can validate the full chain, checking that every block's stored hash matches its recomputed hash, and that each block's `previous_hash` matches the prior block.

### Double-Spend Prevention

Transfers use PostgreSQL row-level locking (`SELECT ... FOR UPDATE`) on the sender wallet inside an atomic transaction. The balance check includes pending outflows, so two concurrent transfers against the same wallet cannot both succeed if funds are insufficient.

### Genesis Anchoring

The genesis block's state (block hash, treasury wallet ID, mint amount) is exported to a JSON manifest at `anchors/genesis.json` and committed to Git. The system continuously compares live database state against this anchor, and optionally verifies the anchor's Git commit exists on the remote. This provides tamper detection: if someone modifies the genesis block or treasury, the mismatch is flagged.

If you anchor that manifest to Bitcoin, record the proof metadata separately in `anchors/genesis-proof.json`. That companion file stores the OpenTimestamps metadata, Git/download links, and one or more Bitcoin attestations without changing the canonical genesis manifest.

The current proof path looks like this:

```text
anchors/genesis.json
  -> hash stamped into
anchors/genesis.json.ots
  -> upgraded into one or more Bitcoin attestations
Bitcoin transaction(s)
```

The web UI exposes this proof trail on the provenance, explorer, and chain validation pages. Users can:
- download `anchors/genesis.json` and `anchors/genesis.json.ots` directly from the app
- open the committed Git copies of those same files
- see whether the local files match the committed Git blobs byte-for-byte
- follow links from the OpenTimestamps proof to the recorded Bitcoin transaction(s)
- manually upload the file pair to `https://opentimestamps.org/` for independent checking

### Provenance Tracing

For any wallet, the system traces all incoming funds back through the transfer chain using breadth-first search. If every path leads back to the genesis mint, the funds are verified as legitimate. The provenance page runs six checks: genesis exists, chain hashes valid, anchor file exists, anchor matches live data, anchor commit verified on remote, and funds trace to genesis.

---

## Architecture

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│   Browser    │────▶│  Django/     │────▶│  PostgreSQL  │
│  (HTMX)     │◀────│  Gunicorn    │◀────│  16-Alpine   │
└──────────────┘     └──────────────┘     └──────────────┘
                           ▲
                     ┌─────┴──────┐
                     │ Auto-Seal  │  (runs every 5 min)
                     │  Worker    │
                     └────────────┘
```

| Component | Technology |
|-----------|-----------|
| Backend | Django 5.1, Python 3.12 |
| Database | PostgreSQL 16 |
| Server | Gunicorn (3 workers) |
| Frontend | Django templates, HTMX, custom CSS |
| Static files | Whitenoise |
| Containerization | Docker, Docker Compose |

---

## Data Model

### Block
UUID-identified blockchain blocks with status flow: `GENESIS` → `PENDING` → `SEALED`. Each stores its index, hash, previous block hash, nonce, and sealing metadata.

### Wallet
Two types: a single **treasury** wallet (receives the genesis mint) and **customer** wallets (owned by users). Each has an auto-generated `0x`-prefixed hex address. Balance is computed from confirmed transfers; pending balance includes unconfirmed outflows.

### Transfer
Moves PAT between wallets. Tracks sender, recipient, amount, memo, status (`PENDING`/`CONFIRMED`/`FAILED`), the block it belongs to, and a SHA-256 transaction hash. Genesis mints have a null sender.

### InviteLink
Single-use tokens for onboarding new users with a signup bonus funded from the treasury.

---

## User Roles

### Admin (staff users)
- View system-wide dashboard with supply metrics
- Create wallets and users
- Create and manage invite links
- Initiate transfers between any wallets
- Seal pending transfers into blocks
- View all wallets, users, and transfers

### Customer (regular users)
- View personal dashboard with wallet balance
- Send PAT to other wallets by address
- Share a receive link/address
- Browse the block explorer
- View chain validation and provenance

---

## Pages & Features

| Page | Path | Description |
|------|------|-------------|
| Dashboard | `/` | Admin: supply stats, pending queue, quick actions. Customer: wallet balance, recent transfers |
| Send PAT | `/send/` | Customer transfer form with address lookup |
| Pay Link | `/pay/<address>/` | Shareable link that prefills the send form |
| Wallets | `/wallets/` | Admin list of all wallets |
| Users | `/users/` | Admin user management |
| Invites | `/invites/` | Admin invite link management |
| Transfers | `/transfers/` | Admin transfer list with status filters |
| Pending Queue | `/pending/` | View and seal pending transfers |
| Explorer | `/explorer/` | Chain overview with supply distribution and recent activity |
| Blocks | `/blocks/` | List of sealed blocks |
| Block Detail | `/blocks/<id>/` | Block metadata and its transfers |
| How It Works | `/how-it-works/` | Educational guide using real blockchain data |
| Validate Chain | `/chain/validate/` | Chain integrity verification |
| Provenance | `/provenance/<wallet_id>/` | Fund lineage tracing back to genesis |

### JSON API Endpoints
- `GET /api/supply/` -- Total supply, treasury balance, circulating amount
- `GET /api/blocks/` -- Recent sealed blocks with transaction counts
- `GET /api/chain-graph/` -- Node/edge data for chain visualization

---

## Getting Started

### Prerequisites
- Docker and Docker Compose

### Setup

1. **Clone the repository and configure environment:**
   ```bash
   cp .env.example .env
   # Edit .env -- at minimum change passwords and DJANGO_SECRET_KEY
   ```

2. **Start the services:**
   ```bash
   docker compose up --build -d
   ```

   This will:
   - Start PostgreSQL and wait for it to be healthy
   - Run database migrations
   - Bootstrap the genesis block (creates admin user, treasury wallet, and genesis mint of 1,000,000 PAT)
   - Collect static files
   - Start Gunicorn on port 8000 (mapped to `PATCOIN_PORT`, default 8004)
   - Start the auto-seal worker (seals pending transfers every 5 minutes)

3. **Access the app:**
   ```
   http://localhost:8004
   ```
   Log in with the admin credentials from your `.env` file.

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `POSTGRES_DB` | `patcoin` | Database name |
| `POSTGRES_USER` | `patcoin` | Database user |
| `POSTGRES_PASSWORD` | `change-me` | Database password |
| `DATABASE_URL` | (composed from above) | Full database connection string |
| `DJANGO_SECRET_KEY` | (none) | Django secret key -- **must change in production** |
| `DJANGO_DEBUG` | `1` | Set to `0` for production |
| `DJANGO_ALLOWED_HOSTS` | `*` | Comma-separated allowed hostnames |
| `PATCOIN_PORT` | `8004` | Host port the app is exposed on |
| `PATCOIN_TOTAL_SUPPLY` | `1000000` | Total PAT minted in genesis |
| `GENESIS_ANCHOR_PATH` | `anchors/genesis.json` | Path to genesis anchor manifest |
| `BOOTSTRAP_ADMIN_USERNAME` | `admin` | Initial admin username |
| `BOOTSTRAP_ADMIN_EMAIL` | `admin@example.com` | Initial admin email |
| `BOOTSTRAP_ADMIN_PASSWORD` | (none) | Initial admin password -- **must change** |

---

## Management Commands

```bash
# Bootstrap genesis block and admin user (runs automatically on startup)
docker compose exec web python manage.py bootstrap_genesis

# Manually seal all pending transfers
docker compose exec web python manage.py auto_seal_blocks

# Export genesis state to anchor file
docker compose exec web python manage.py export_genesis_anchor

# Verify genesis anchor matches live database
docker compose exec web python manage.py verify_genesis_anchor

# Upgrade an OpenTimestamps proof once the calendars have anchored it
docker compose exec web ots upgrade /app/anchors/genesis.json.ots

# Inspect the OpenTimestamps proof structure and any recorded Bitcoin attestations
docker compose exec web ots info /app/anchors/genesis.json.ots
```

If `ots upgrade` completes successfully, the `.ots` file may contain multiple Bitcoin attestations from different calendars. In that case, `anchors/genesis-proof.json` should store them under `bitcoin_anchor.attestations`.

---

## Running Tests

```bash
docker compose exec web python manage.py test ledger
```

The test suite covers genesis bootstrapping, transfer creation, double-spend prevention, block sealing, chain validation, invite flows, authentication, provenance tracing, and genesis anchoring.

---

## Security Features

- **Row-level locking** on wallet balances to prevent double-spends
- **PostgreSQL advisory locks** to prevent concurrent block sealing
- **Rate limiting** on login, registration, and invite endpoints
- **CSRF protection** on all forms
- **Secure cookies** in production (when `DJANGO_DEBUG=0`)
- **No-cache headers** after logout to prevent back-button data leaks
- **Genesis anchoring** for tamper detection
- **Atomic transactions** for all balance-modifying operations
