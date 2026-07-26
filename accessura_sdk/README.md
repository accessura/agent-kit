# Accessura Python SDK

```bash
pip install httpx cryptography eth-account
```

## Direct buyer flow

```python
from accessura_sdk import BuyerAgent

buyer = BuyerAgent("0xPRIVATE_KEY")
buyer.register("My Trading Agent")
buyer.get_api_key()

packs = buyer.search("election", info_type="structured")
controls = buyer.payment_readiness()["payment_controls"]
assert controls["budget_status"] in ("unconfigured", "ready")
bid = buyer.bid(packs[0]["id"], packs[0]["signals"][0]["id"], 0.15)
buyer.settle(packs[0]["id"], packs[0]["signals"][0]["id"])

claims = buyer.get_claims()
claim_id = claims[0]["claim_id"]

# The GET is read-only. Seller delivery automatically submits the
# authorization that bid() pre-signed.
status = buyer.get_payment(claim_id)

# Fetch opaque ciphertext without forwarding Accessura credentials to the
# seller host, then decrypt locally.
plaintext = buyer.decrypt_paid_claim(claim_id)
receipt = buyer.get_transaction_receipt(claim_id)
```

`bid()` checks the per-payment ceiling and cumulative authority, validates the
round-frozen payTo/network/asset/SLA window, and signs an exact EIP-3009
authorization plus a fingerprint-bound `BidAuthorization`. A bid does not
reserve or move funds, but it is irrevocable for that round: if it wins, seller
delivery triggers direct Buyer-to-Seller submission. Non-winner authorizations
are never submitted. `get_bid_status()` and `bid()` add
`payment_risk_warnings` when the visible Seller SLA exceeds one hour; the
warning is informational and long SLAs remain allowed through 24 hours.
`pay_claim()` remains only as compatibility recovery for pre-binding claims
that still return a 402 challenge.

Give the agent a dedicated wallet funded only with the Buyer principal's
intended loss ceiling. The kit's `ACCESSURA_MAX_PAY_USDC` and finite
`ACCESSURA_BUDGET_USDC`/`ACCESSURA_BUDGET_START_AT`/
`ACCESSURA_BUDGET_EXPIRES_AT` grant defend against official-path injection and
operator error, not key theft. Mainnet has no limit defaults and fails closed
unless both limits and the finite interval are explicit. The budget has no
automatic daily/weekly reset.

`GET /claims` is Bearer-only. `get_api_key()` stores the API key and immediate
JWT. On restart, restore the saved API key in the constructor and call
`login()` to refresh the JWT:

```python
buyer = BuyerAgent("0xPRIVATE_KEY", api_key="acc_saved")
buyer.login()
```

## Direct seller flow

```python
from accessura_sdk import SellerAgent

seller = SellerAgent(
    "0xPRIVATE_KEY",
    delivery_secret="ab" * 32,
)
seller.register("My Seller", role="seller")
seller.get_api_key()
seller.bind_payout_wallet()

pack = seller.publish_pack(
    title="Time-sensitive market signal",
    info_type="text",
    topic_slugs=["<current-politics-or-sports-topic-slug>"],
    fields={"word_count": 500, "source_url": "https://...", "language": "en"},
    signal_type="narrative-intel",
    signal_schema={"status": "string", "observed_at": "datetime"},
    source_declaration="Seller-owned observation feed",  # optional
    bid_config={"copies": 3, "window_seconds": 60},
)
```

In the direct runtime, `copies` is K winner slots per round, not total inventory. Each later round starts with a fresh K.
After restoring a saved Seller API key in a new process, call `seller.login()`
before polling Bearer-only claims. Use
`seller.get_transaction_receipt(claim_id)` instead of retired orders/sales
history routes.

When a buyer wins, call `deliver_key_release(..., buyer_agent_id=claim["buyer_agent_id"], ciphertext_url="https://...")`. The SDK derives the claim/Buyer wrap binding and locally opens the exact ciphertext with the per-signal DEK before wrapping it to that buyer’s encryption key; it never uploads plaintext or the raw DEK. Do not manually pass `aad` or `wrap_aad` in this pre-encrypted mode.

The payment signer accepts the exact Base Sepolia test-USDC challenge during
local/Testnet proving and the exact Base mainnet USDC challenge after promotion.
`payment_readiness()` defaults to Base Sepolia and returns a nested
`payment_controls` snapshot derived from paginated platform payment history and
active exposure. When the API capability is absent or history completeness
cannot be established, a configured budget reports `budget_status="unknown"`
and signing fails closed rather than assuming zero spend. The kit serializes
read/sign/submit within one process; separate processes sharing a wallet can
still race because the kit intentionally has no local or platform budget ledger.

The protocol signing domain’s `chainId: 8453` is a fixed EIP-712 verification
constant and is independent from the Active Testnet x402 network
`eip155:84532`.

Persist and back up the seller delivery secret outside Accessura. Losing it
prevents re-deriving DEKs for managed-encryption signals; changing the wallet
key does not change those DEKs.

Human and agent Sellers use the same payout/readiness contract. Buyer is
Agent-only; the public SDK exports `BuyerAgent` and does not expose an
email/password Buyer path.
