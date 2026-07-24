# Funded Base Sepolia Release Validation

Status: authorized by JC for the `v0.6.0` stable release gate; not yet executed.

This runbook records the one isolated funded lifecycle required before the
stable tag. It does not authorize Base mainnet, production deployment, a second
payment, or moving the tag. Do not create `v0.6.0` until all nine assertions
below pass and JC accepts the resulting evidence.

## Required operator inputs

- One dedicated Buyer private key whose address has enough Base Sepolia test
  ETH and test USDC for exactly one purchase.
- One distinct Seller identity with a verified Base Sepolia payout wallet.
- A fresh Pack, Signal, round, bid, and claim used only for this validation.
- The current candidate commit and Active Testnet release SHA.

JC provides the funded Buyer key and Seller payout identity through the normal
secret handoff. Never place a private key, API key, JWT, raw DEK, plaintext, or
payment authorization in Git, terminal transcripts, screenshots, PR comments,
or the sanitized evidence JSON.

## Execution boundary

The authorized operator runs the lifecycle with the candidate MCP/SDK and
captures read-only state before and after each action. This repository's
`scripts/verify_funded_testnet_evidence.py` only validates the sanitized record;
it performs no network request, signature, key release, or payment.

Stop immediately if the payment requirement names the wrong network, asset,
amount, or Seller payout address; if any platform address is a recipient; or if
the first payment result is uncertain. Do not retry an uncertain transaction
until the chain and participant receipt resolve its state.

## Nine required assertions

1. **Bid does not move funds.** Record Buyer test-USDC balance before and after
   the signed bid and prove zero token transfers.
2. **Award is unpaid.** After settlement, record
   `award_pending_delivery` with no payment transaction hash.
3. **Delivery readiness gates 402.** Payment read is `202` before Seller
   delivery and `402` only after the buyer-specific delivery is ready.
4. **False preview does not pay.** Run the MCP preview with
   `confirm_real_payment=false`; prove unchanged Buyer balance and zero token
   transfers.
5. **True confirmation produces one Buyer→Seller USDC transfer.** With JC's
   funded authorization, execute exactly one confirmed payment and record the
   Base Sepolia USDC transfer, amount, Buyer, Seller, and transaction hash.
6. **No platform recipient.** Prove the transfer recipient is the verified
   Seller payout wallet and is not any Accessura/platform address.
7. **Buyer decrypts locally.** Decrypt after paid delivery in the Buyer process;
   record only a SHA-256 digest of the plaintext.
8. **Retry does not duplicate payment.** Repeat the paid-result retrieval path;
   prove it resolves to the original transaction and creates zero new
   transfers.
9. **Receipt matches the transaction.** Read the participant receipt and prove
   its claim ID and payment transaction hash match the on-chain transfer.

## Sanitized evidence and verifier

Create a local JSON record matching the fields consumed by the verifier. It
contains public addresses, integer balance/amount strings, state names,
transaction hashes, counts, and a plaintext digest—never secrets or plaintext.

```bash
python scripts/verify_funded_testnet_evidence.py /absolute/path/to/evidence.json
```

Success prints `verified: true` and all nine assertion names. Preserve the
evidence outside the company canonical file and attach only the sanitized
result to the release review. A failed or incomplete assertion blocks the
stable tag; it must not be re-described as passed or downgraded to an RC without
a new JC decision.
