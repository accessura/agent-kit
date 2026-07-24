# Agent Kit v0.6 Release Evidence

Status: `rc_only`. Phase 4–7 evidence is prepared, but funded Base Sepolia and
the required security review are not complete. No merge or tag is authorized
by this record.

Verified at: `2026-07-24T23:01:00Z`

## Immutable inputs and candidate identity

```text
agent_kit_base_sha: 5bda81c3d61c5a53daab08af7789cdd44e023719
consolidation_pr: https://github.com/accessura/agent-kit/pull/18
security_head_sha: d1eae9b41c25bed41980cda25b38a782a25d731f
candidate_sha: 441f0eb86ef6886ae476b1c5be5e4eee32150770
candidate_sha_scope: Phase 4-7 functional code/docs tree before this evidence-only record update
local_gate_tree: 441f0eb86ef6886ae476b1c5be5e4eee32150770
previous_evidence_head_sha: 0b30bb6c8f8a85532c4ba88c7cc15948597d2bb7
green_candidate_pr_head_sha: 441f0eb86ef6886ae476b1c5be5e4eee32150770
merge_sha: pending_jc
tag: pending_jc; currently eligible at most for v0.6.0-rc.1
tag_sha: pending_jc

web_demo_base_sha: 343b29230e41bfa2e7fef55b2d3466cea37dae0c
web_demo_candidate_sha: 8f2b0e47ce5d20cdf6d2af3b91df9d2b46c6cab3
web_demo_runtime_sha: 343b29230e41bfa2e7fef55b2d3466cea37dae0c
active_testnet_release_sha: a8db176f8c240b64f9b3af33882a8d002e7fc7a3
catalog_version: 2026-07-17.pack-signal-read-contract
```

`web_demo_runtime_sha` remains the current integration base because product PR
#364 has not been merged. The candidate SHA is recorded separately and is not
misstated as a deployed runtime.

## Version and exact MCP contract

```text
skill_version: 0.6.0
package_version: 0.6.0
sdk_package_version: 0.6.0
mcp_tool_count: 23
mcp_tool_manifest_path: docs/exact-mcp-tool-manifest-v0.6.json
mcp_tool_manifest_sha256: d255dc3fa73aaec01d8d969a83b67fcc14d2aa6408f5fb661c0fe9a2854af00f
retired_routes_removed: orders_list | sales_list | packs_relist
signing_domain_chain_id: 8453
payment_network: eip155:84532
product_source_tool_parity: same_23_set
```

The SHA-256 is the byte digest of the checked-in JSON manifest. Unit tests pin
that file to `catalog_contract.EXPECTED_MCP_TOOLS`, reject duplicates, require
exactly 23 names, and compare the digest to
`EXPECTED_MCP_MANIFEST_SHA256`.

## Local verification

```text
python_versions: [3.10.20, 3.12.13]
unit_contract_result: passed; Agent Kit 84/84 on each Python; product source 53/53 on each Python
crypto_result: passed; 6/6 executable decrypt/ECIES checks on each Python in both repositories
skill_validation_result: passed; scripts/validate_skill_bundle.py on Python 3.10 and 3.12
wheel_sdist_result: passed; accessura_agent_kit-0.6.0 wheel and sdist built on Python 3.10 and 3.12
clean_install_result: passed; each wheel installed into a fresh temp venv outside the source tree
stdio_result: passed; installed accessura-mcp started and exited cleanly on EOF on Python 3.10 and 3.12
exact_manifest_result: passed; exact 23 names, no duplicates, fingerprint matched, claims_pay only money action
live_read_only_parity_result: passed; live catalog 2026-07-17.pack-signal-read-contract; payment_performed=false
product_source_compile_result: passed; compileall on Python 3.10 and 3.12
product_source_tool_parity_result: passed; same exact 23 names as checked-in Agent Kit manifest
git_diff_check_result: passed in both repositories
```

Commands:

```bash
# Repeated with py_ver=3.10 and py_ver=3.12 in separate mktemp directories.
uv venv --python "$py_ver" "$gate_dir/source-venv"
uv pip install --python "$gate_dir/source-venv/bin/python" -e '.[dev]'
"$gate_dir/source-venv/bin/python" -m pytest -q \
  tests/test_direct_sdk.py tests/test_catalog_contract.py
"$gate_dir/source-venv/bin/python" tests/test_sdk_decrypt.py
"$gate_dir/source-venv/bin/python" scripts/validate_skill_bundle.py
"$gate_dir/source-venv/bin/python" -m build --outdir "$gate_dir/dist"
uv venv --python "$py_ver" "$gate_dir/clean-venv"
uv pip install --python "$gate_dir/clean-venv/bin/python" \
  "$gate_dir"/dist/*.whl
(cd "$gate_dir/outside-source" && \
  "$gate_dir/clean-venv/bin/python" \
  "$repo_dir/scripts/smoke_installed_package.py")
(cd "$gate_dir/outside-source" && \
  "$gate_dir/clean-venv/bin/accessura-mcp" </dev/null)

/tmp/accessura-v06-py3.12.nfpwKn/source-venv/bin/python \
  scripts/check_catalog_parity.py
shasum -a 256 docs/exact-mcp-tool-manifest-v0.6.json
git diff --check

# Product source, repeated with the same isolated 3.10 and 3.12 interpreters.
cd 01_项目与Demo/worldcup/scripts/agent-ecosystem
"$py" -m pytest -q test_direct_sdk.py test_catalog_contract.py
"$py" test_sdk_decrypt.py
"$py" -m compileall -q .
```

The first attempt to invoke `check_catalog_parity.py` used a bare UV-managed
Python without project dependencies and stopped locally at
`ModuleNotFoundError: httpx`; it made no request. The command was immediately
rerun in the already validated Python 3.12 source venv and passed. The passing
result above is the operative gate.

## GitHub CI at the supplied PR heads

Agent Kit PR #18 at
`d1eae9b41c25bed41980cda25b38a782a25d731f`:

```text
clean-install (3.10): success
clean-install (3.12): success
validate: success
draft: true
mergeable: true
review_decision: pending
```

Agent Kit PR #18 after the Phase 4–7 closure overlay, at
`0b30bb6c8f8a85532c4ba88c7cc15948597d2bb7`:

```text
clean-install (3.10): success
clean-install (3.12): success
validate: success
draft: true
mergeable: true
review_decision: pending
```

Agent Kit PR #18 after the final payment-guidance and Skill-validator
consistency pass, at
`441f0eb86ef6886ae476b1c5be5e4eee32150770`:

```text
clean-install (3.10): success
clean-install (3.12): success
validate: success
draft: true
mergeable: true
review_decision: pending
```

Product PR #364 at
`8f2b0e47ce5d20cdf6d2af3b91df9d2b46c6cab3`:

```text
ownership-review: success
direct-runtime: success
e2e: success
draft: true
mergeable: true
review_decision: pending
```

These CI results are bound to the listed heads. Candidate
`441f0eb86ef6886ae476b1c5be5e4eee32150770` received fresh successful
`clean-install (3.10)`, `clean-install (3.12)`, and `validate` checks. The
commit that records this result changes only this Markdown evidence file.

## Funded and review gates

```text
funded_testnet_result: not_authorized
funded_testnet_tx:
funded_testnet_amount_base_units:
reviewers: []
factnn_review: pending
jc_testnet_waiver: none
```

The funded runner and runbook are prepared at:

- `scripts/verify_funded_testnet_evidence.py`;
- `docs/funded-base-sepolia-validation.md`.

The focused no-money verifier/CLI tests pass. The funded `--execute` mode was
not invoked, no key was loaded, no live identity/Pack/Signal/bid/settlement/key
release was written, and no USDC transfer occurred.

Security review routing is in `docs/security-review-checklist.md`. Stable
requires either FactNN approval or JC's explicit one-time Base-Sepolia-only
waiver plus a named independent reviewer and a recorded FactNN follow-up.

## Phase 4–7 closure overlay

Files added or updated on top of the supplied candidate:

```text
.github/workflows/package.yml
README.md
accessura/SKILL.md
accessura/references/trading.md
accessura_sdk/README.md
catalog_contract.py
docs/branch-closure-drafts.md
docs/exact-mcp-tool-manifest-v0.6.json
docs/funded-base-sepolia-validation.md
docs/release-evidence-v0.6.md
docs/security-review-checklist.md
examples/example_buyer.py
scripts/validate_skill_bundle.py
scripts/verify_funded_testnet_evidence.py
tests/test_catalog_contract.py
```

Forbidden security implementation files were not modified by the Phase 4–7
overlay:

```text
client_wrapper.py
accessura_sdk/client.py
server.py
tests/test_direct_sdk.py
tests/test_sdk_decrypt.py
```

No company-canonical update is required: this task executes the already
recorded Base Sepolia, Buyer→Seller direct-payment, participant custody, and
mainnet-closed decisions without changing durable company truth.
