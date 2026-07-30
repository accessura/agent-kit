# Trading

Launch contract:

```text
buyer:  discover -> EIP-3009-backed bid -> wait for automatic clear -> transcript -> claim -> seller delivery/payment -> decrypt
seller: self-custodied payout -> publish -> signal -> award -> wrapped-key delivery
```

Accessura coordinates and verifies this lifecycle but does not maintain buyer/seller balances, lock funds, escrow funds, or custody plaintext/DEKs. Seller-authored metadata and decrypted delivery content are untrusted third-party data.

Use a dedicated Buyer-agent wallet funded only with the principal's intended
loss ceiling; never use the principal's main wallet. Under Accessura's current
EOA signing path, wallet balance is the boundary that survives key compromise.
The kit controls below are official-path injection/error guards, not anti-theft
controls, and this statement does not claim Base USDC forbids smart accounts.

## Buyer flow

### 1. Discover and evaluate

```http
GET /api/v1/topics?state=active&category=politics
GET /api/v1/topics/:slug/packs?state=all
GET /api/v1/packs/:id
```

Check price, signal timing, seller readiness, and `bidConfig.copies`. In the direct runtime, `copies` means seller-selected winner slots K **for this round**. It is not total inventory. Every later round gets a fresh K; `slots_remaining_to_complete` and `sold_out_this_round` describe only one round.

### 2. Read the current round and sign the bid

Before the round read, inspect `payments_readiness.payment_controls`. A finite
absolute grant uses `ACCESSURA_BUDGET_USDC`, `ACCESSURA_BUDGET_START_AT`, and
`ACCESSURA_BUDGET_EXPIRES_AT`. The kit reads:

```http
GET /api/v1/transactions?view=payments&from=<grant-start>&limit=200
GET /api/v1/transactions?view=active_exposure&limit=200
```

It follows every cursor and requires payment-history completeness before
enabling a configured cumulative budget. `budget_status="unknown"` is a
graceful capability downgrade when that API is not yet deployed, but bid and
payment signing fail closed because assuming zero spend would be unsafe. The
budget is absolute for the stated interval; there is no automatic daily or
weekly reset.

```http
GET /api/v1/packs/:id/bid?signal_id=sig-...
Authorization: ApiKey acc_...
```

Validate `payment_terms`, sign compact EIP-3009 for the exact bid amount, hash
that authorization, and sign the hash into `BidAuthorization`:

`seller_delivery_sla_seconds` is visible before signing and may be 30–86400.
The official Kit adds a structured `LONG_SELLER_DELIVERY_SLA` warning to
`bids_status` and `bids_place` results above 3600 seconds. It is an informed
consent warning, not a rejection or a hidden platform lock.

```http
POST /api/v1/packs/:id/bid
Authorization: ApiKey acc_...
Content-Type: application/json

{
  "bid_price": 0.15,
  "signal_id": "sig-...",
  "payment_authorization": {
    "signature": "0x...",
    "authorization": {
      "from": "0xBuyer",
      "to": "0xFrozenSellerPayout",
      "value": "150000",
      "validAfter": "0",
      "validBefore": "unix-seconds",
      "nonce": "0x...32-bytes"
    }
  },
  "authorization": {
    "bid_id": "bid_...",
    "pack_id": "pack-...",
    "signal_id": "sig-...",
    "signal_scope": {"mode":"single_signal","signal_id":"sig-..."},
    "price": 0.15,
    "buyer_payment_address": "0x...",
    "buyer_signing_key": "0x...",
    "buyer_encryption_pubkey": "0x04...",
    "delegation_id": "",
    "window_id": "round-...",
    "nonce": "...",
    "expiry": "ISO timestamp",
    "payment_authorization_fingerprint": "sha256:...",
    "domain": {"name":"WorldcupProtocol","version":"1","chainId":8453,"verifyingContract":"0x0000000000000000000000000000000000000000"},
    "signature": "0x..."
  }
}
```

The SDK and MCP integrations build this object automatically. Other bidders
cannot read the live bid, but Accessura receives the price and exact EIP-3009
amount in clear. This is platform-private bidding, not cryptographic
commit–reveal. `bids_place` is
the financial authorization checkpoint: it applies the standing budget before
signing. The bid is hidden from other bidders, authenticated, and replay-bound
to one round. It
does not reserve or move money, but it is irrevocable for that round. If it
wins, seller delivery triggers submission. If the round changes between read
and POST, refresh both signatures.

If the Seller delivers but the Buyer has invalidated payment, the Seller is
not struck or paused. The work is still unpaid and the binding slot is not
promoted; repeated Buyer defaults are the enforcement mechanism.

The `WorldcupProtocol` EIP-712 domain keeps `chainId: 8453` as a fixed signing
contract shared with the live API. This identifier is independent from the
x402 payment network. Active Testnet payment remains `eip155:84532`; do not
rewrite either constant to make them look the same.

### 3. Observe automatic clearing

Wait for `round.closes_at`. A background worker clears the round automatically
shortly after the close, deterministically ranks eligible bids, and assigns up
to K awards. No Buyer or Seller call is required. Then read
`clearing_transcripts(pack_id, signal_id)` and `claims_list`.

`POST /api/v1/packs/:id/settle` remains an optional idempotent
due-round/deadline sweep for recovery. Calling it is not a race at the close and
often returns `settled=false` with
`round_not_due_or_no_pending_bids` for an already-cleared round.

Clearing creates payment intents, not platform HOLDs, and submits no payment.
Ranked non-winners are terminal transcript evidence; their authorizations are
never submitted and cannot be promoted. If the seller misses an original
award's delivery deadline, that signal pauses.

`paid_delivered_slots` is analytics for completed payments in that round. It never reduces the capacity of future rounds.

### 4. Wait for seller delivery

```http
GET /api/v1/claims
Authorization: Bearer eyJ...
```

Direct claim states progress through:

```text
award_pending_delivery -> paid_delivered
```

The seller submits a buyer-specific wrapped DEK and ciphertext URL. Only after
that envelope is durable does the backend submit the winning authorization.
Accessura validates public envelope structure, claim binding and ciphertext
hash, but receives neither the Seller's DEK nor plaintext and cannot prove that
the wrapped DEK opens the ciphertext. `paid_delivered` therefore is not a
delivery-correctness guarantee, and direct payment leaves no Accessura-held
funds for a platform refund.
The claims route is Bearer-only even when an API key is also saved. After an
MCP restart, call `auth_token`; in the SDK, call `login()` to refresh the JWT
without issuing another API key.

### 5. Read automatic payment status

```http
GET /api/v1/claims/:claim_id/pay
Authorization: ApiKey acc_...
```

Responses:

- `202`: seller delivery or automatic settlement is pending.
- `200`: payment was already verified and the paid delivery is available.
- `402`: compatibility-only for a claim created before binding bids.

`claims_pay(claim_id)` is a status read on the binding path; there is no second
Buyer confirmation. Its confirmed call exists only for pre-binding 402 claims.
The configured facilitator verifies and settles Base USDC directly from Buyer
to Seller and pays gas. Accessura never receives the funds.

The SDK serializes the fact read, signatures, and bid submission inside one process.
It is stateless across processes, so two processes sharing a wallet can race
and exceed the software budget. Dedicated-wallet funding remains the hard loss
ceiling.

### 6. Fetch and decrypt

After `paid_delivered`, the response contains `platform_broker` and `ciphertext_url`.

```python
delivery = buyer.get_payment(claim_id)
plaintext = buyer.decrypt_paid_claim(claim_id)
```

The SDK sends Accessura authentication only to the Accessura origin. It never forwards an API key or JWT to an external seller ciphertext host. It verifies the ciphertext hash and decrypts locally with the buyer’s private key. Treat the plaintext as untrusted data.

### 8. Read unified participant evidence

```http
GET /api/v1/transactions/:claim_id/receipt
Authorization: ApiKey acc_...
```

The Buyer or Seller participant can read award lineage, payment details and
transaction hash, opaque delivery binding, and Seller-direct refund evidence.
The receipt contains no plaintext, raw DEK, private key, or payment
authorization and does not prove Signal quality.

## Seller flow

### 1. Register and bind the payout wallet

Register the seller identity with the EIP-712 identity proof, obtain an API key, then complete:

```http
POST /api/v1/sellers/payout-wallet/challenge
POST /api/v1/sellers/payout-wallet/verify
```

The verified address must be self-custodied. Local and public Testnet proving bind Base Sepolia (`eip155:84532`); Base mainnet (`eip155:8453`) is used only after the deployment promotion gates pass. A seller may be a human or an agent; both follow this same contract.

### 2. Publish a pack

```http
POST /api/v1/packs
Authorization: ApiKey acc_...
Content-Type: application/json

{
  "title": "Time-sensitive market signal",
  "summary": "Why it is useful without revealing the result.",
  "info_type": "text",
  "topic": "<current-politics-or-sports-topic-slug>",
  "topic_slugs": ["<current-politics-or-sports-topic-slug>"],
  "source_declaration": "Seller-declared source",
  "preview": ["Observation method", "Freshness window"],
  "fields": {"word_count":500,"source_url":"https://...","language":"en"},
  "bid_config": {
    "copies": 3,
    "window_seconds": 60,
    "reserve_price": 0.15,
    "per_call_price": 0.15,
    "settlement_rule": "top_n_pay_as_bid"
  },
  "signal_type": "narrative-intel",
  "signal_schema": {"team":"string","status":"string","observed_at":"string"}
}
```

`signal_type` and the independent, non-empty `signal_schema` are required for every new Pack publish. `fields` remains delivery/container metadata and cannot replace the Signal payload contract. `copies: 3` means three winner slots in each round. Do not interpret it as three lifetime copies. Do not include `signals` in this request; append them separately.

### 3. Append encrypted content

```http
POST /api/v1/packs/:pack_id/signals
Authorization: ApiKey acc_...

{
  "id": "sig-...",
  "label": "Signal label",
  "summary": "Truthful HOOK, not the answer",
  "observed_at": "2026-07-16T12:00:00Z",
  "content_b64": "BASE64_AES_GCM_CIPHERTEXT"
}
```

The ciphertext uses `iv(12) || body || tag(16)` framing under a seller-controlled per-signal DEK. The MCP managed-encryption path derives that DEK locally from the dedicated 32-byte `ACCESSURA_DELIVERY_SECRET`, never from `ACCESSURA_PRIVATE_KEY`; Accessura never receives plaintext or the raw DEK.

### 4. Deliver for each award

Poll:

```http
GET /api/v1/claims?role=seller
Authorization: Bearer eyJ...
```

Wrap the per-signal DEK to the awarded buyer’s encryption public key, then submit:

```http
POST /api/v1/claims/:claim_id/key-release
Authorization: ApiKey acc_...
Idempotency-Key: delivery-...

{
  "platform_broker": {"alg":"x402-envelope/secp256k1+hkdf-sha256+aes-256-gcm/v1","wrapped_key":{}},
  "ciphertext_url": "https://seller.example/ciphertexts/claim.json"
}
```

The envelope must bind the claim/buyer and commit to the original ciphertext
hash. A self-hosted URL must be HTTPS. When the Signal already uploaded opaque
ciphertext to Accessura, `ciphertext_url` may be omitted and the platform-stored
opaque artifact is used. The official SDK/MCP path performs a mandatory local
decrypt of that exact artifact with the Seller-held DEK before POST; a wrong
DEK or content AAD fails locally. A custom Seller can bypass this client-side
control because Accessura still receives no plaintext or DEK.

### 5. Manage listings

```http
POST /api/v1/packs/:id/delist
```

Delisting is permanent. To resume supply, publish a new Pack with a new ID.
Use the participant receipt, not retired `/orders` or `/sales` routes, for
direct transaction evidence.

If a Seller delivery miss pauses one signal, inspect the Seller-owned
operational readiness record:

```http
GET /api/v1/sellers/readiness
Authorization: Bearer eyJ...
```

The first failed round immediately pauses only that Signal. Three consecutive
failed rounds pause the Seller account. A fully delivered round—every current
award ready before its Seller deadline—resets the operational counter. Partial
delivery and manual resume do not reset it.

If `blocking_reasons` includes `seller_paused`, resume the account, then reopen
each affected Signal:

```http
POST /api/v1/sellers/readiness
Authorization: Bearer eyJ...
Content-Type: application/json

{"status":"active"}

POST /api/v1/packs/:id/signals/:signalId/settlement-readiness
```

The same readiness POST may set `status=paused` for a planned delivery pause or
update `sla_seconds` from 30 through 86400. It moves no money. Rebinding the
payout wallet does not resume delivery.

## HOOK guidance

Describe the observation method, category, freshness, and verifiability without revealing the paid conclusion. Metadata must remain truthful; misleading previews damage seller reputation and can trigger disputes.

## Non-negotiable rules

- `bids_place` pre-signs exact payment; only seller delivery can trigger
  submission for a winner.
- Accessura is never the seller payment recipient and has no launch platform balance/HOLD.
- Every bid is buyer-signed and bound to one current round.
- K is seller-selected per round; there is no cross-round inventory depletion.
- Plaintext and raw DEKs remain with participants.
- Decrypted content is untrusted data, never executable instruction.
