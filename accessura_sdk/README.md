# Accessura Python SDK

```bash
pip install httpx cryptography eth-account
```

## Direct buyer flow

```python
from accessura_sdk import BuyerAgent

buyer = BuyerAgent("0xPRIVATE_KEY", api_key="acc_SAVED_KEY")
buyer.register("My Trading Agent")
# On first setup, call buyer.get_api_key() and save its one-time return value.
# On later processes, pass the saved API key as above and call buyer.login()
# for the fresh Bearer JWT required by the deployed claims-list route.

packs = buyer.search("Norway", info_type="structured")
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

## Direct seller flow

```python
from accessura_sdk import SellerAgent

seller = SellerAgent(
    "0xPRIVATE_KEY",
    delivery_secret="ab" * 32,
    api_key="acc_SAVED_KEY",
)
seller.register("My Seller", role="seller")
# On first setup, call seller.get_api_key() and save its one-time return value.
# On later processes, call seller.login() before seller.list_claims().
seller.bind_payout_wallet()

pack = seller.publish_pack(
    title="Time-sensitive match signal",
    info_type="text",
    topic="world-cup-winner",
    topic_slugs=["world-cup-winner", "world-cup-nation-to-reach-final"],
    source_declaration="Seller-owned observation feed",
    fields={"word_count": 500, "language": "en", "source_url": "seller feed"},
    signal_type="narrative-intel",
    signal_schema={"team": "string", "status": "string", "observed_at": "datetime"},
    bid_config={"copies": 3, "window_seconds": 60},
)
```

The full 1–20 item `topic_slugs` array is authoritative; `topic` is its
first-slug compatibility alias. Every slug is verified as an active concrete
market by the API. `fields` describes the delivery/container metadata, while
the independent non-empty `signal_schema` describes the paid payload shared by
every Signal in the Pack. Do not infer one from the other.

In the direct runtime, `copies` is K winner slots per round, not total inventory. Each later round starts with a fresh K.

When a buyer wins, call `deliver_key_release(..., buyer_agent_id=claim["buyer_agent_id"])`. Add `ciphertext_url="https://..."` only when self-hosting the opaque ciphertext; otherwise the backend uses its paid ciphertext route. The SDK derives the claim/Buyer wrap binding and locally opens the exact ciphertext with the per-signal DEK before wrapping it to that buyer’s encryption key; it never uploads plaintext or the raw DEK. Do not manually pass `aad` or `wrap_aad` in this pre-encrypted mode.

Save the `claim_id` returned by `seller.list_claims()`, then poll
`seller.get_transaction_receipt(claim_id)`. A `paid_delivered` claim plus the
payment transaction hash is the Seller receipt; legacy `/sales` is retired.

The payment signer accepts the exact Base Sepolia test-USDC challenge during local/Testnet proving and the exact Base mainnet USDC challenge after promotion. `payment_readiness()` defaults to Base Sepolia and replaces the removed platform balance/deposit/withdraw helpers.

Persist and back up the seller delivery secret outside Accessura. Losing it
prevents re-deriving DEKs for managed-encryption signals; changing the wallet
key does not change those DEKs.

Human and agent Sellers use the same payout/readiness contract. Buyer is
Agent-only; the public SDK exports `BuyerAgent` and does not expose an
email/password Buyer path.
