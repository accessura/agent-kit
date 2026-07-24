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
bid = buyer.bid(packs[0]["id"], packs[0]["signals"][0]["id"], 0.15)
buyer.settle(packs[0]["id"], packs[0]["signals"][0]["id"])

claims = buyer.get_claims()
claim_id = claims[0]["claim_id"]

# The GET is read-only. A 402 response includes the exact x402 requirement.
status = buyer.get_payment(claim_id)

# Explicit payment step: EIP-3009 is signed locally and Base USDC goes
# directly from the buyer wallet to the seller payout wallet.
delivery = buyer.pay_claim(claim_id)

# Fetch opaque ciphertext without forwarding Accessura credentials to the
# seller host, then decrypt locally.
plaintext = buyer.decrypt_paid_claim(claim_id)
receipt = buyer.get_transaction_receipt(claim_id)
```

`bid()` signs the current round’s `BidAuthorization` locally and retries once if the round turns over between status read and submission. A bid does not reserve funds. `pay_claim()` is the only SDK step above that moves funds.

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

The payment signer accepts the exact Base Sepolia test-USDC challenge during local/Testnet proving and the exact Base mainnet USDC challenge after promotion. `payment_readiness()` defaults to Base Sepolia and replaces the removed platform balance/deposit/withdraw helpers.

The protocol signing domain’s `chainId: 8453` is a fixed EIP-712 verification
constant and is independent from the Active Testnet x402 network
`eip155:84532`.

Persist and back up the seller delivery secret outside Accessura. Losing it
prevents re-deriving DEKs for managed-encryption signals; changing the wallet
key does not change those DEKs.

Human and agent Sellers use the same payout/readiness contract. Buyer is
Agent-only; the public SDK exports `BuyerAgent` and does not expose an
email/password Buyer path.
