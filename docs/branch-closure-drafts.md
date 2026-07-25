# Agent Kit v0.6 Branch-Closure Drafts

Status: text only. No comment has been posted, no PR/issue has been closed, and
no branch has been deleted.

Replace `[FINAL_TAG]`, `[FINAL_TAG_SHA]`, `[AGENT_KIT_MERGE_SHA]`, and
`[PRODUCT_MERGE_SHA]` only after the corresponding immutable facts exist.
Use `v0.6.0` only if funded Testnet and review both pass; otherwise use
`v0.6.0-rc.1`.

## Agent Kit PR #8 supersession comment

> Superseded by #18 and released as `[FINAL_TAG]` (`[FINAL_TAG_SHA]`).
>
> The valid #8 contract work was preserved in the consolidation:
>
> - structured `topic_slugs` with 1–20 unique active concrete Topics;
> - explicit `signal_type` and independent non-empty `signal_schema`;
> - Pack `fields` kept separate from the paid Signal payload contract;
> - `topic = topic_slugs[0]` retained only as the compatibility alias;
> - live catalog parity, schema bounds, all five Pack delivery shapes, Skill,
>   SDK, packaging, and Python 3.10/3.12 regression coverage.
>
> #18 also incorporates the later Active Testnet origin/Sector/terminal-delist
> contract and security gates. It intentionally replaces #8's obsolete
> 24-tool conclusion with the exact 23-tool manifest and removes the retired
> orders, sales, and relist surfaces. Closing this PR as superseded; it was not
> merged independently.

## Agent Kit PR #11 supersession comment

> Superseded by #18 and released as `[FINAL_TAG]` (`[FINAL_TAG_SHA]`).
>
> The valid #11 lifecycle and onboarding work was semantically transplanted:
>
> - Bearer-only Buyer/Seller claim polling;
> - wallet-signed `auth_token` recovery after process restart;
> - API-key exchange caching an immediate Bearer session;
> - live read-only `claims_pay(false)` 402 preview;
> - participant-visible `claims_receipt`;
> - saved SDK API-key/JWT constructor inputs;
> - Buyer, Human Seller, and Agent Seller lifecycle documentation and tests.
>
> The consolidation does not carry #11's old origin, obsolete relist behavior,
> retired Topic paths, or 24-tool assertion. It additionally binds confirmed
> payment to previewed `expected_amount`/`expected_pay_to`, enforces
> `ACCESSURA_MAX_PAY_USDC`, guards auth blind-signing, and keeps mainnet closed
> by default. Closing this PR as superseded; it was not merged independently.

## Agent Kit PR #17 supersession comment

> Superseded by #18 and released as `[FINAL_TAG]` (`[FINAL_TAG_SHA]`).
>
> #17 supplied the consolidation baseline for:
>
> - the Active Testnet origin;
> - neutral Politics/Sports Topic discovery;
> - current multi-Topic/Signal publishing;
> - terminal Pack delisting;
> - the 0.6.0 package/Skill version line and installable package gates.
>
> #18 preserves those behaviors, then corrects the surface composition:
> `auth_token` and `claims_receipt` are present; `orders_list`, `sales_list`,
> and `packs_relist` are absent. It also adds the #11 lifecycle work, no-Sector
> Agent contract, exact-manifest fingerprint, security hardening, and funded
> release evidence gate. Closing #17 as superseded rather than merging two
> competing release trees.

## Agent Kit issue #7 closing comment

> Resolved by Agent Kit #18 at `[AGENT_KIT_MERGE_SHA]`, with product source
> parity in accessura/web-demo-design#364 at `[PRODUCT_MERGE_SHA]`, and released
> as `[FINAL_TAG]` (`[FINAL_TAG_SHA]`).
>
> The shipped contract accepts 1–20 normalized concrete `topic_slugs`, derives
> the compatibility `topic` alias from the first slug, requires explicit
> `signal_type` plus an independent non-empty `signal_schema`, and keeps Pack
> `fields` separate. The final MCP surface is the exact 23-tool direct
> lifecycle; the issue's historical 24-tool expectation is superseded because
> orders, sales, and relist are retired.
>
> No inventory bulk migration or republish was performed. Base mainnet remains
> closed. Closing the contract-sync issue after the merged commits and
> immutable tag are verified.

## web-demo-design PR #306 update

### Required document disposition before merge

Do not merge the current 314-line
`docs/architecture/agent-kit-contract-sync-plan.md` as an `active` plan. It
still treats the archive origin, 24 tools, PR #8, and the old release order as
current. Before making #306 ready:

1. change its `docs/CATALOG.md` entry from `active` to `superseded`;
2. replace the old plan body with a short historical pointer that names
   `agent-kit-v0-6-release-consolidation-plan.md` as the current execution
   authority;
3. preserve only the still-valid 1–20 Topic guardrail, no-bulk-migration
   rationale, and completed selective-republish history;
4. point current implementation/release facts to Agent Kit #18, product #364,
   and `[FINAL_TAG]`;
5. remove active claims for the old host, 24 tools, `#11 → #8`, and a stable
   tag until those facts are actually true.

If #306 is no longer useful after this compaction, close it as superseded by
#364 instead of merging a second active release plan. Do not merge its current
unmodified body.

### Replacement status section

> ## Current state
>
> - The release-consolidation plan supersedes the old `#11 → #8 → main`
>   sequence and the old 24-tool conclusion.
> - Agent Kit consolidation: accessura/agent-kit#18, candidate
>   `d1eae9b41c25bed41980cda25b38a782a25d731f` before closure-only follow-up.
> - Product source parity: accessura/web-demo-design#364, candidate
>   `8f2b0e47ce5d20cdf6d2af3b91df9d2b46c6cab3`.
> - Both candidates expose the same exact 23-tool set:
>   `auth_token`/`claims_receipt` included;
>   `orders_list`/`sales_list`/`packs_relist` removed.
> - Security hardening is mirrored in both candidates: auth blind-sign
>   whitelist, preview-bound `expected_amount`/`expected_pay_to`, default
>   `ACCESSURA_MAX_PAY_USDC=100`, and opt-in-only mainnet.
> - Package/no-money gates are green. Funded Base Sepolia and the required
>   security review remain pending; no stable availability claim is made.
> - No production write, funded payment, bulk migration, or second Pack
>   republish was performed by this documentation sync.

### PR #306 update comment

> Updated by the Agent Kit v0.6 consolidation wave:
>
> - execution moved to accessura/agent-kit#18 and product parity PR #364;
> - exact surface is 23 tools, not 24;
> - #8/#11/#17 are donor/superseded branches, not a merge sequence;
> - `claims_pay(true)` now requires previewed
>   `expected_amount`/`expected_pay_to`;
> - `ACCESSURA_MAX_PAY_USDC` defaults to 100 USDC;
> - `ACCESSURA_ALLOW_MAINNET=1` is required for mainnet, which remains out of
>   scope;
> - funded Base Sepolia evidence and review remain release gates.
>
> Final merged SHAs and `[FINAL_TAG]` will be inserted only after JC authorizes
> those actions. Until then this PR remains documentation of a release
> candidate, not an availability announcement.

## Branches eligible for deletion after closure

Delete only after the supersession comments are posted, #18/#364 and #306 have
their final disposition, the chosen tag is immutable, and remote branch tips
are rechecked:

- `feat/pack-signal-contract-v0-6` (#8);
- `codex/v0-6-release-integration` (#11);
- `sync/active-testnet-cutover` (#17);
- `codex/v0-6-release-consolidation` (#18), after merge/tag;
- `feat/agent-kit-v0-6-release-consolidation` (#364), after merge;
- `feat/wc-agent-kit-sync-plan` (#306), after merge or close.

Do not delete `feat/mvp-v2`, `dev/cjx`, `main`, or any public tag.
