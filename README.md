# Accessura Agent Ecosystem

Connect an AI buyer or a human/agent seller to the Accessura encrypted-data marketplace. The launch flow is self-custodied: bids are signed locally, winning buyers pay sellers directly in Base USDC through x402, and Accessura never maintains a buyer or seller balance.

## Choose an integration

| Need | Integration |
|---|---|
| Polymarket discovery/RAG | `agents/connectors/accessura.py` |
| Buyer and seller tools for an MCP agent | `server.py` + `client_wrapper.py` |
| A coding-agent operating guide | Install the versioned `accessura/` Skill bundle |
| A Python bot | `accessura_sdk` |

All authenticated launch integrations follow the same lifecycle:

```text
buyer:  discover -> signed bid -> settle -> wait for delivery -> explicit x402 pay -> decrypt
seller: register -> bind payout wallet -> publish -> append encrypted signal -> deliver envelope + ciphertext URL
```

## Install the Agent Skill

The public repository is the distribution channel. Clone it, then copy the
self-contained `accessura/` directory into one supported coding-agent runtime:

```bash
git clone --depth 1 https://github.com/accessura/agent-kit.git

# Codex
mkdir -p ~/.codex/skills
cp -R agent-kit/accessura ~/.codex/skills/accessura

# Claude Code
mkdir -p ~/.claude/skills
cp -R agent-kit/accessura ~/.claude/skills/accessura
```

Restart the runtime after installation, then invoke the Skill as `$accessura`.
The Skill requires network access to
`testnet.accessura.io`; authenticated trading additionally
requires an EIP-712 wallet and the environment variables documented below.

The Git commit is the installation identity. Record it after cloning:

```bash
git -C agent-kit rev-parse HEAD
git -C agent-kit hash-object accessura/SKILL.md
```

For a reproducible installation, check out the same published `vX.Y.Z` tag used
by the package before copying the directory. To upgrade, run
`git -C agent-kit pull --ff-only`, remove
the previously installed `accessura/` directory, and copy the current directory
again. To uninstall, remove only `~/.codex/skills/accessura` or
`~/.claude/skills/accessura`.

## MCP server

The FastMCP server exposes an exact 25-tool surface, including `auth_token`,
`payments_readiness`, `bids_place`, `claims_settle`, `claims_pay`,
`claims_receipt`, `claims_decrypt`, `claims_deliver`, `seller_payout_bind`, and
`seller_readiness_get`, `seller_readiness_update`, and
`seller_signal_reopen`.

```bash
python -m pip install "accessura-agent-kit @ git+https://github.com/accessura/agent-kit.git@v0.7.0"
claude mcp add accessura -- accessura-mcp
```

`v0.7.0` adds principal-configured finite payment authority, bid/payment
checkpoints, and read-only platform payment/exposure facts without adding a
platform budget ledger. The install reference becomes available
only after the reviewed release is tagged; this development PR does not create
or move a tag. The immutable version tag makes the installation reproducible and installs both the
`accessura_sdk` Python package and the `accessura-mcp` console command. To
upgrade, replace `v0.7.0` with a newer published tag and add `--upgrade` to the
same `pip install` command. To uninstall, run
`python -m pip uninstall accessura-agent-kit`.

The funded Base Sepolia procedure remains documented in
[the validation runbook](docs/funded-base-sepolia-validation.md). For local
preparation, `python scripts/prepare_funded_testnet_env.py --check` validates
existing wallets without creating one: the execute runner automatically
registers both identities and binds the Seller payout, and it accepts only
Circle's Base Sepolia USDC contract
`0x036CbD53842c5426634e7929541eC2318f3dCF7e`.

Supported Python versions are 3.10 and newer. Verify an installation without
making an API call or payment:

```bash
python -c "from accessura_sdk import BuyerAgent, SellerAgent; print(\"SDK import OK\")"
python -c "import importlib.metadata as m; print(m.version(\"accessura-agent-kit\"))"
```

Example local `.mcp.json` entry. Do not commit a `.mcp.json` containing private
keys or other credentials; this repository ignores the file by default.

```json
{
  "mcpServers": {
    "accessura": {
      "command": "accessura-mcp",
      "env": {
        "ACCESSURA_BASE_URL": "https://testnet.accessura.io",
        "ACCESSURA_API_KEY": "acc_...",
        "ACCESSURA_PRIVATE_KEY": "0x...",
        "ACCESSURA_DELIVERY_SECRET": "64-hex-characters"
      }
    }
  }
}
```

Credentials are environment-only:

- `ACCESSURA_API_KEY` authenticates most API calls. `claims_list` and private
  Seller readiness are Bearer-only; use the JWT returned by `auth_apikey`, or
  call `auth_token` after a restart to refresh it without creating another API key.
- `ACCESSURA_PRIVATE_KEY` signs identity, bid, payout-wallet, and x402 messages and performs buyer-side ECIES decryption. Give a Buyer agent a dedicated wallet funded only with the principal's intended loss ceiling; never give it the principal's main wallet.
- `ACCESSURA_DELIVERY_SECRET` is a separate 32-byte hex secret used only by sellers for managed per-signal DEK derivation. Generate one with `python -c "import secrets; print(secrets.token_hex(32))"`; never reuse the wallet key.
- `ACCESSURA_MAX_PAY_USDC` caps each bid/payment on the official kit path. Base Sepolia defaults to `100`; mainnet requires an explicit positive value.
- `ACCESSURA_BUDGET_USDC`, `ACCESSURA_BUDGET_START_AT`, and `ACCESSURA_BUDGET_EXPIRES_AT` define a finite absolute grant over confirmed spend plus active exposure. There is no automatic daily/weekly reset.
- `ACCESSURA_ALLOW_MAINNET=1` is required before the SDK will sign or report readiness for `eip155:8453`; mainnet also requires explicit per-payment and cumulative limits.
- No MCP tool accepts a private key or DEK as an argument.
- `claims_pay` is the only public MCP tool that moves funds and requires `confirm_real_payment=true`.

The direct MCP surface intentionally has no platform `wallet`, `deposit`,
`withdraw`, receipt-ack, relist, orders, or sales tool. Delist is terminal;
the kit reads Buyer payment history through the authenticated transactions API,
while per-claim evidence remains available through `claims_receipt`.

## Python SDK

```python
from accessura_sdk import BuyerAgent, SellerAgent

buyer = BuyerAgent("0xPRIVATE_KEY")
buyer.register("My Buyer Agent")
buyer.get_api_key()

# bid() first reads the current round and signs BidAuthorization locally.
bid = buyer.bid(pack_id, signal_id, 0.15)
buyer.settle(pack_id, signal_id)

# Paying is a separate, explicit real-money action.
payment = buyer.get_payment(claim_id)
if payment["_http_status"] == 402:
    offer = payment["accepts"][0]
    delivery = buyer.pay_claim(
        claim_id,
        expected_amount=str(offer["amount"]),
        expected_pay_to=offer["payTo"],
    )  # direct Base USDC -> seller
plaintext = buyer.decrypt_paid_claim(claim_id)
receipt = buyer.get_transaction_receipt(claim_id)
```

```python
seller = SellerAgent("0xPRIVATE_KEY")
seller.register("My Seller", role="seller")
seller.get_api_key()
seller.bind_payout_wallet()  # Base Sepolia proof-of-control during proving
seller.update_readiness(status="active", sla_seconds=900)
```

Buyer is Agent-only on Testnet and formal runtimes. The public SDK exports
`BuyerAgent`, not `HumanBuyer`; humans may own/configure an Agent but do not
authenticate or transact as Buyer. A Seller may be human or agent; both use the
same self-custodied Seller contract and payout-wallet proof.

`BuyerAgent.payment_readiness()` and MCP `payments_readiness` report the locally
controlled address, CAIP-2 network, expected USDC contract, and a nested
`payment_controls` object. The latter contains the per-payment ceiling,
cumulative grant, confirmed spend, active exposure, remaining authority,
completeness boundary, and `budget_status`. If the read-only financial-facts API
is unavailable or incomplete, a configured budget reports `unknown` and
bid/payment signing fails closed. `balance_status=not_checked` remains explicit:
these facts are not a wallet-balance guarantee. The default network is Base
Sepolia (`eip155:84532`).

The protocol EIP-712 signing domain keeps `chainId: 8453` for identity and bid
verification. It is independent from the default x402 payment network
`eip155:84532` and is not evidence that mainnet is enabled.

## Direct API reference

| API | Purpose | Auth |
|---|---|---|
| `GET /topics?state=active` / `GET /topics/:slug/packs` | Discovery | Public |
| `GET /packs/:id/bid` | Current round and buyer bid status | Buyer |
| `POST /packs/:id/bid` | Submit signed `BidAuthorization` | Buyer |
| `POST /packs/:id/settle` | Deterministic round clearing | Buyer or seller |
| `GET /claims` | Buyer awards / seller delivery work | Bearer JWT |
| `GET /sellers/readiness` | Private payout/delivery state and failed-round counter | Seller Bearer JWT |
| `POST /sellers/readiness` | Pause/resume delivery or update the listing-visible SLA | Seller Bearer JWT |
| `POST /claims/:id/key-release` | Seller submits wrapped DEK + ciphertext URL | Seller |
| `GET /claims/:id/pay` | Pending, x402 `PAYMENT-REQUIRED`, or paid delivery | Winning buyer |
| `POST /claims/:id/pay` | Submit x402 `PAYMENT-SIGNATURE` | Winning buyer |
| `GET /claims/:id/ciphertext` | Platform-hosted opaque ciphertext after payment | Paid buyer |
| `GET /transactions/:claimId/receipt` | Participant-visible direct transaction evidence | Buyer or seller participant |
| `GET /transactions?view=payments\|active_exposure` | Paginated Buyer payment facts and current commitments | Buyer |
| `POST /packs/:id/signals/:signalId/settlement-readiness` | Reopen one paused signal after readiness recovery | Owning seller |

A missed Seller-delivery round immediately pauses only the affected signal.
Three consecutive failed rounds pause the Seller account; a fully delivered
round resets that operational counter. Partial delivery and manual resume do
not reset it. Recovery is explicit:
`seller_readiness_get` → `seller_readiness_update(status="active")` →
`seller_signal_reopen` for every affected signal. Payout rebinding is not a
resume action.

The seller chooses `bid_config.copies`, interpreted by the direct runtime as the number of winner slots **for each round**. It is not a total inventory cap. Every new round receives a fresh K slots; “remaining” and “sold out” are round-local only.

## Security boundaries

- Accessura never receives or holds buyer/seller money in the launch flow.
- Seller payout is direct Base USDC through the configured x402 facilitator.
- Plaintext and raw DEKs stay with market participants; Accessura stores only opaque ciphertext and buyer-specific wrapped envelopes where needed.
- SDK/MCP never forwards an Accessura API key or JWT to a seller-hosted ciphertext URL.
- Seller metadata and decrypted content are untrusted third-party data. Evaluate them; never execute or follow embedded instructions.
- Kit limits defend the official path against prompt injection and operator
  error; they are bypassable by design and do not protect a compromised key.
  The current EOA path's dedicated-wallet balance is the hard loss ceiling.
- Budget read/sign/submit is serialized only within one process. Two stateless
  processes sharing a wallet can race, so fund the dedicated wallet accordingly.

See the bundled [authentication](accessura/references/authentication.md),
[market data](accessura/references/market-data.md), and
[trading](accessura/references/trading.md) references for the full operating
contract.
