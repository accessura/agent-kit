# Funded Base Sepolia Release Validation

Status: `not_authorized`. Prepared for the `v0.6.0` stable gate; no funded run
has been executed.

This runbook covers one isolated first-party Base Sepolia lifecycle. It does
not authorize Base mainnet, production deployment, a second payment, merge,
or tag creation. Until the funded assertions and review gate both pass, the
highest permitted release is `v0.6.0-rc.1`.

## Safety boundary

`scripts/verify_funded_testnet_evidence.py` has two explicit modes:

- `--validate EVIDENCE_JSON` is offline and never loads credentials.
- `--execute` creates a fresh lifecycle and performs exactly one confirmed
  Buyer-to-Seller Base Sepolia USDC payment.

The parser intentionally has no private-key, API-key, JWT, payout, or
delivery-secret arguments. Execute mode reads credentials from the process
environment, holds them only in memory, never writes them to a file, and
prints only sanitized evidence JSON. Do not enable shell tracing (`set -x`),
paste secrets into a command line, or capture the environment in CI logs.

`ACCESSURA_ALLOW_MAINNET` must be absent. The script refuses to run if that
variable exists, including when its value is `0`. It fixes the payment network
to `eip155:84532` and the asset to Base Sepolia USDC
`0x036CbD53842c5426634e7929541eC2318f3dCF7e`.

Stop immediately if the requirement names the wrong network, asset, amount,
or Seller payout address; if any platform address is a recipient; or if the
first payment result is uncertain. Resolve an uncertain transaction from the
chain and participant receipt before retrying.

## Required environment

JC supplies these values through a private secret handoff and injects them
into the operator shell without putting them in shell history:

| Variable | Requirement |
| --- | --- |
| `ACCESSURA_FUNDED_BUYER_PRIVATE_KEY` | Dedicated Buyer key with enough Base Sepolia test USDC for one purchase. |
| `ACCESSURA_FUNDED_SELLER_PRIVATE_KEY` | Dedicated Seller identity/payout proof key. |
| `ACCESSURA_FUNDED_SELLER_PAYOUT_ADDRESS` | Seller payout address; must equal the address controlled by the Seller key. |
| `ACCESSURA_DELIVERY_SECRET` | Dedicated 32-byte Seller managed-encryption secret; must differ from the Seller wallet key. |
| `ACCESSURA_BASE_SEPOLIA_RPC_URL` | Base Sepolia JSON-RPC endpoint used only for balance, log, and receipt evidence. |

Optional non-secret controls:

| Variable | Default | Purpose |
| --- | --- | --- |
| `ACCESSURA_FUNDED_BASE_URL` | `https://testnet.accessura.io` | Candidate API origin. |
| `ACCESSURA_FUNDED_TOPIC_SLUG` | first current active Topic | Explicit fresh Pack Topic. |
| `ACCESSURA_FUNDED_BID_USDC` | `0.01` | One-buyer bid/payment amount. |
| `ACCESSURA_FUNDED_WINDOW_SECONDS` | `60` | Fresh one-slot round window. |
| `ACCESSURA_FUNDED_SETTLE_TIMEOUT_SECONDS` | window + 180 | Award, delivery, receipt, and chain timeout. |
| `ACCESSURA_FUNDED_POLL_SECONDS` | `5` | Poll interval. |
| `ACCESSURA_PLATFORM_ADDRESSES` | empty | Comma-separated known platform addresses to exclude as recipients. |
| `ACCESSURA_MAX_PAY_USDC` | SDK default `100` | Local signing ceiling; lower it for this canary if desired. |

The fresh Pack, Signal, round, claim, ciphertext, and plaintext exist only for
this validation. The script creates them after authorization; do not pre-create
or repurpose customer inventory.

## Authorized one-command execution

Only after JC explicitly authorizes the funded run and the required variables
are present:

```bash
umask 077
env -u ACCESSURA_ALLOW_MAINNET \
  python scripts/verify_funded_testnet_evidence.py --execute \
  > /absolute/private/path/funded-base-sepolia-evidence.json
```

This is the only funded command. Do not add credentials as command arguments.
The output file contains public addresses, identifiers, balances, amount,
transaction hash, plaintext SHA-256, and assertion results—never a key, API
credential, JWT, raw DEK, plaintext, or payment authorization.

The execute path calls the actual MCP `claims_pay` handler twice around the
payment boundary:

1. `confirm_real_payment=false` reads the live 402 preview and proves no
   transfer.
2. `confirm_real_payment=true` passes `expected_amount` and
   `expected_pay_to` copied from that preview. A changed offer is refused.

After the first confirmed payment is reconciled on-chain and in the participant
receipt, the script calls the paid-result path again and proves there is no new
USDC transfer.

## Nine required assertions

1. **Bid does not move funds.** Buyer test-USDC balance is unchanged and the
   Base Sepolia USDC contract emits no outgoing Buyer transfer during the bid.
2. **Award is unpaid.** Settlement produces `award_pending_delivery` with no
   payment transaction hash.
3. **Delivery readiness gates 402.** Payment read is `202` before the
   Buyer-specific Seller envelope and `402` only afterward.
4. **False preview does not pay.** MCP preview uses
   `confirm_real_payment=false`, preserves the Buyer balance, emits no
   transfer, and yields the exact amount/payTo bindings.
5. **True confirmation produces one Buyer→Seller USDC transfer.** The
   confirmed MCP call uses both `expected_*` bindings and the successful
   transaction receipt contains exactly one Buyer-to-Seller Base Sepolia USDC
   `Transfer` for the previewed amount.
6. **No platform recipient.** The sole recipient is the proof-bound Seller
   payout wallet and is not in the supplied platform-address set.
7. **Buyer decrypts locally.** The Buyer process decrypts the paid ciphertext;
   evidence records only the plaintext SHA-256 digest.
8. **Retry does not duplicate payment.** Paid-result retry resolves to the
   original transaction hash and emits zero new Buyer USDC transfers.
9. **Receipt matches the transaction.** Participant receipt claim ID and
   transaction hash match the on-chain transfer.

## Offline verification and evidence-record fields

Re-check the sanitized result without any credential environment:

```bash
env -u ACCESSURA_FUNDED_BUYER_PRIVATE_KEY \
    -u ACCESSURA_FUNDED_SELLER_PRIVATE_KEY \
    -u ACCESSURA_DELIVERY_SECRET \
  python scripts/verify_funded_testnet_evidence.py \
    --validate /absolute/private/path/funded-base-sepolia-evidence.json
```

Success prints `verified: true`, `funded_testnet_tx`,
`funded_testnet_amount_base_units`, and all nine boolean assertion results.
Copy those sanitized fields into `docs/release-evidence-v0.6.md`:

```text
funded_testnet_result: passed
funded_testnet_tx: <0x transaction hash>
funded_testnet_amount_base_units: <integer micro-USDC>
```

Keep detailed transaction evidence in the project release record, not the
company canonical file. A failed or incomplete assertion blocks stable; it
must not be described as passed.
