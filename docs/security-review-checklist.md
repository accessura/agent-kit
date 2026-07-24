# Agent Kit v0.6 Security Review Checklist

Review target:

- Agent Kit PR #18 candidate `d1eae9b41c25bed41980cda25b38a782a25d731f`.
- Product source-parity PR #364 candidate
  `8f2b0e47ce5d20cdf6d2af3b91df9d2b46c6cab3`.
- Base Sepolia only. Base mainnet is excluded.

This checklist is a reviewer map, not a new design decision. It records the
current implementation and the tests that must stay green. Review both
candidates together where a product-mirror location is listed.

## 1. Claims are Bearer-only

- [ ] **Status: implemented, fail-closed.** `client_wrapper.py:74-81`
  constructs a Bearer-only header and refuses a missing JWT;
  `client_wrapper.py:261-264` uses it for `/claims` even when an API key is
  also present.
- [ ] **SDK Buyer/Seller match.** `accessura_sdk/client.py:464-468`,
  `accessura_sdk/client.py:659-662`, `accessura_sdk/client.py:768-772`, and
  `accessura_sdk/client.py:1044-1048` require the cached token for claim
  polling.
- [ ] **MCP does not weaken the route.** `server.py:563-579` delegates
  `claims_list` to the Bearer-only wrapper. API-key exchange caches an
  immediate JWT and restart recovery uses `auth_token`; see
  `client_wrapper.py:398-450`.
- [ ] **Regression tests.** `tests/test_direct_sdk.py:215`,
  `tests/test_direct_sdk.py:249`, `tests/test_direct_sdk.py:521`,
  `tests/test_catalog_contract.py:254`, `tests/test_catalog_contract.py:271`,
  and `tests/test_catalog_contract.py:278`.
- [ ] **Product mirror.**
  `01_项目与Demo/worldcup/scripts/agent-ecosystem/client_wrapper.py:74-81`,
  `:261-264`;
  `.../accessura_sdk/client.py:464-468`, `:660-663`, `:769-773`,
  `:1021-1025`; `.../server.py:562-578`.

Reviewer check: with both API key and JWT present, inspect the actual request
header for `/claims`; it must be `Authorization: Bearer ...`. With no JWT it
must return an actionable error, never fall back to `ApiKey`.

## 2. x402 preview-to-execute boundary

- [ ] **Status: preview is read-only.** `server.py:643-678` calls the live
  claim-pay GET when `confirm_real_payment=false`, returns the 402 terms, and
  does not sign or POST.
- [ ] **Explicit preview binding.** The same handler requires callers to pass
  previewed `expected_amount` and `expected_pay_to` on confirmation.
  `client_wrapper.py:279-299` forwards both values to the signer.
- [ ] **Signer verifies the full offer.**
  `accessura_sdk/client.py:304-366` checks network, asset, USDC domain,
  recipient, amount, preview bindings, claim resource, and timeout before
  producing the EIP-3009 authorization.
- [ ] **Amount ceiling.** `accessura_sdk/client.py:219-236` converts
  `ACCESSURA_MAX_PAY_USDC` to base units (default `100` USDC);
  `accessura_sdk/client.py:353-358` refuses offers above it.
- [ ] **One public money action.** The exact manifest contains only
  `claims_pay` as a money-moving MCP tool. The no-money paths are bid,
  settlement, claim list, receipt, delivery, and decrypt.
- [ ] **Regression tests.** `tests/test_catalog_contract.py:335`,
  `tests/test_direct_sdk.py:57`, `tests/test_direct_sdk.py:113`,
  `tests/test_direct_sdk.py:156`, and `tests/test_direct_sdk.py:442`.
- [ ] **Product mirror.**
  `.../server.py:639-674`, `.../client_wrapper.py:279-299`,
  `.../accessura_sdk/client.py:219-236`, `:304-366`, and
  `.../test_direct_sdk.py:439`.

Reviewer check: mutate `amount` and `payTo` after preview; confirmation must
fail before signing. Set an amount above the default ceiling; it must also
fail before signing. Verify `claims_pay(false)` cannot reach the payment POST.

## 3. Auth blind-sign whitelist

- [ ] **Status: implemented at every auth signing site.**
  `accessura_sdk/client.py:158-202` only accepts the null-contract
  `WorldcupProtocol` domain and the whitelisted `AuthChallenge`,
  `IdentityRegistration`, or `SellerPayoutBinding` primary type.
- [ ] **Wrapper uses the same guard.** `client_wrapper.py:372-396` invokes
  `_assert_safe_auth_challenge` before wallet signing.
- [ ] **Payment authorization cannot masquerade as auth.**
  `TransferWithAuthorization`, the USDC token domain, and a non-null verifying
  contract are rejected before `account.sign_message`.
- [ ] **Regression tests.** `tests/test_direct_sdk.py:406` covers a valid auth
  challenge plus a disguised value-transfer payload;
  `tests/test_direct_sdk.py:486` verifies identity registration uses the
  protocol domain.
- [ ] **Product mirror.**
  `.../accessura_sdk/client.py:158-202`,
  `.../client_wrapper.py:372-396`, and
  `.../test_direct_sdk.py:403`, `:483`.

Reviewer check: search every backend-supplied EIP-712 signing site. Each auth
site must pass through the whitelist; x402 signing must remain isolated in the
dedicated payment signer.

## 4. Credentials are environment-only and not logged or persisted

- [ ] **Status: environment-only MCP inputs.** `client_wrapper.py:45-48`
  loads API key, JWT, wallet key, and delivery secret from environment
  variables. `server.py:28-32` states the boundary; no MCP tool schema accepts
  a private key, JWT, delivery secret, raw DEK, or payment authorization.
- [ ] **Local configuration is excluded.** `.gitignore` excludes `.mcp.json`;
  `tests/test_catalog_contract.py:622` asserts the ignore/documentation
  boundary.
- [ ] **Funded runner has no credential CLI.**
  `scripts/verify_funded_testnet_evidence.py` only exposes `--execute` and
  `--validate`; execute mode reads all credentials from environment, keeps
  them in memory, emits only sanitized JSON, and redacts known secret values
  from handled errors. `tests/test_catalog_contract.py:743` pins that parser
  surface.
- [ ] **Receipt remains secret-free.**
  `tests/test_direct_sdk.py:555` verifies participant receipt pass-through
  does not inject process credentials or plaintext.
- [ ] **Product mirror.**
  `.../client_wrapper.py:45-48`, `.../server.py:28-32`, and
  `.../test_direct_sdk.py:552`.

Reviewer check: inspect generated MCP input schemas, error paths, README
examples, workflows, and the funded JSON shape. No secret may appear in a tool
argument, repository file, test snapshot, stdout evidence, or log statement.

## 5. DEK and plaintext custody

- [ ] **Status: participant-local custody.**
  `client_wrapper.py:97-108` requires a dedicated delivery secret and rejects
  reuse of the wallet key. `client_wrapper.py:455-463` derives/encrypts only in
  the Seller process.
- [ ] **Platform receives opaque material only.**
  `client_wrapper.py:324-329` submits the Buyer-specific `platform_broker`
  envelope and optional ciphertext URL, not the raw DEK or plaintext.
- [ ] **SDK managed encryption matches.**
  `accessura_sdk/client.py:774-784`, `:995-1041`, and `:1057-1094` keep the
  delivery secret and DEK local, upload ciphertext, and wrap the derived DEK
  to the awarded Buyer.
- [ ] **Buyer decrypts locally.**
  `accessura_sdk/client.py:702-729` fetches opaque ciphertext without leaking
  credentials to an external Seller origin and decrypts with the local Buyer
  key. `server.py:584-638` marks Seller content untrusted.
- [ ] **Regression tests.** `tests/test_direct_sdk.py:184`,
  `tests/test_direct_sdk.py:261`, `tests/test_direct_sdk.py:318`,
  `tests/test_direct_sdk.py:555`, and executable
  `tests/test_sdk_decrypt.py` cover origin isolation, managed ciphertext,
  secret separation, receipt redaction, ECIES unwrap/decrypt, and AAD tamper
  rejection.
- [ ] **Product mirror.**
  `.../client_wrapper.py:97-108`, `:324-329`, `:457-465`;
  `.../accessura_sdk/client.py:775-785`, `:972-1018`, `:1034-1071`;
  `.../server.py:581-634`.

Reviewer check: trace Seller plaintext to ciphertext and DEK to wrapped
envelope, then trace Buyer ciphertext/envelope to local plaintext. There must
be no server-side plaintext or reconstructible raw-DEK path.

## 6. Base mainnet exclusion

- [ ] **Status: closed by default.**
  `accessura_sdk/client.py:205-216` refuses `eip155:8453` unless
  `ACCESSURA_ALLOW_MAINNET=1`; Base Sepolia `eip155:84532` remains the default
  and is unaffected.
- [ ] **Gate applies to readiness and payment signing.**
  `accessura_sdk/client.py:304-366` invokes the network gate before signing;
  payment readiness uses the same allowed-network check.
- [ ] **Signing domain is not a payment-network signal.** The fixed
  `WorldcupProtocol` EIP-712 domain `chainId: 8453` remains independent from
  the x402 payment network `eip155:84532`.
- [ ] **Funded runner is stricter.**
  `scripts/verify_funded_testnet_evidence.py` refuses execute mode whenever
  `ACCESSURA_ALLOW_MAINNET` is present and validates the exact Base Sepolia
  network/USDC pair. `tests/test_catalog_contract.py:759` covers the
  fail-closed environment check.
- [ ] **Regression tests.** `tests/test_direct_sdk.py:72`,
  `tests/test_direct_sdk.py:308`, and `tests/test_direct_sdk.py:442`.
- [ ] **Product mirror.**
  `.../accessura_sdk/client.py:205-216`, `:304-366`;
  `.../test_direct_sdk.py:72`, `:305`, `:439`.

Reviewer check: with no override, both mainnet readiness and a fully valid
mainnet x402 offer must fail. Do not set the override during this release
review. The presence of signing-domain `chainId: 8453` must not be described as
mainnet enablement.

## Review disposition

Stable remains blocked until both rows are complete:

| Gate | Current status | Required evidence |
| --- | --- | --- |
| Funded Base Sepolia lifecycle | `not_authorized` | All nine assertions plus one reconciled Buyer→Seller USDC transaction. |
| Security review | `pending` | FactNN approval, or JC's explicit one-time Base-Sepolia-only waiver plus another named reviewer and a recorded FactNN follow-up. |

Any waiver is limited to this Base Sepolia Testnet consolidation. It does not
extend to mainnet, fees, custody, or a new payment design.
