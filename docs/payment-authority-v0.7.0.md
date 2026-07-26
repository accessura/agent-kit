# Agent Kit v0.7.0 payment authority

Status: implementation candidate; requires ownership review. This document does
not publish a release, deploy code, or create a tag.

## Responsibility boundary

- The platform supplies clearing, payment, delivery, and active-exposure facts.
  It does not store or enforce a Buyer principal's budget.
- The Buyer principal chooses a dedicated wallet, funds it, and configures the
  per-payment ceiling, absolute grant, and finite validity interval.
- The agent chooses bids and purchases inside that standing grant.
- The kit helps execute the grant on its official path. Its checks defend
  against prompt injection and operator error; they are not theft protection.

Under Accessura's current EOA signing path, the dedicated wallet's balance is
the loss ceiling that survives compromise of the signing key. This is scoped to
the current implementation. Base USDC's token contract does not prohibit
smart-account signatures.

## Configuration

| Variable | Semantics |
|---|---|
| `ACCESSURA_MAX_PAY_USDC` | Positive per-bid/per-payment ceiling. Base Sepolia defaults to 100; Base mainnet requires an explicit value. |
| `ACCESSURA_BUDGET_USDC` | Positive absolute authorization over confirmed spend plus active exposure. |
| `ACCESSURA_BUDGET_START_AT` | Inclusive RFC3339 history boundary. Required with a budget. |
| `ACCESSURA_BUDGET_EXPIRES_AT` | Exclusive RFC3339 expiry. Required with a budget. |
| `ACCESSURA_ALLOW_MAINNET` | Enables Base mainnet selection only when equal to a recognized true value. Mainnet also requires both explicit limits and the finite interval. |

There is no daily or weekly reset. An operator creates a new grant by replacing
the amount and timestamps deliberately.

## Platform facts and degradation

The kit consumes every page of:

```text
GET /api/v1/transactions?view=payments&from=<start>&limit=200
GET /api/v1/transactions?view=active_exposure&limit=200
```

Payment rows are counted by `payment_tx_hash IS NOT NULL` on the platform side.
The kit requires `history_complete=true`, a compatible
`history_complete_from`, confirmed chain facts, valid base-unit amounts, and a
current-platform-state exposure snapshot. It deduplicates:

- the predecessor bid when an intent for the same `bid_id` is active;
- reconciliation exposure for an intent already present in payment history;
- repeated payment intents or exposure IDs.

If the API is not deployed, cannot be read, cannot paginate consistently, or
cannot declare completeness, `payments_readiness` returns
`payment_controls.budget_status="unknown"`. The tool itself remains usable, but
a configured cumulative budget refuses both bid and payment signing.

## Signing checkpoints

- Before `BidAuthorization`, the kit checks the bid against the per-payment
  ceiling and remaining cumulative authority.
- Before EIP-3009 `TransferWithAuthorization`, the kit repeats both checks.
  When the current claim is already represented by equal active exposure,
  payment realizes that commitment and does not count it twice.
- Preview binding, network/asset/domain validation, and the mainnet gate remain
  independent checks.

The SDK and MCP wrapper serialize fact-read, signature, and submission inside
one process. The kit is intentionally stateless: two processes sharing the
same wallet can read the same snapshot and race past a software budget.
Dedicated-wallet funding remains the hard boundary.

## Review checks

- Exact MCP tool count remains 25.
- `payments_readiness` adds the versioned nested `payment_controls` output
  schema; this is intentionally a v0.7.0 contract change.
- No local state file or platform budget ledger is introduced.
- No x402 signature shape, facilitator contract, or payment model is changed.
- PR 2 absence produces `unknown`; it does not crash readiness or silently
  assume zero spend.
- Base mainnet cannot sign with default or partial limit configuration.
