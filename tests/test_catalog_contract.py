import asyncio
import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

import client_wrapper
import server
from catalog_contract import (
    CATALOG_VERSION,
    DELIVERY_FORMATS,
    EXPECTED_MCP_MANIFEST_SHA256,
    EXPECTED_MCP_TOOLS,
    INFO_TYPES,
    MAX_TOPIC_SLUGS,
    REQUIRED_FIELDS,
    SIGNAL_CONTRACT,
    SIGNAL_TYPES,
    assert_catalog_parity,
    normalize_signal_schema,
    normalize_topic_slugs,
    parse_fields,
    validate_publish_contract,
)

VALID_SCHEMA = {"status": "string", "observed_at": "datetime"}


def load_repo_script(name):
    script_path = Path(__file__).resolve().parents[1] / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_repo_script_{name}", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def pinned_catalog():
    return {
        "version": CATALOG_VERSION,
        "enums": {
            "infoType": list(INFO_TYPES),
            "signalType": list(SIGNAL_TYPES),
        },
        "publishSchemas": {
            info_type: {
                "deliveryFormat": DELIVERY_FORMATS[info_type],
                "requiredFields": list(REQUIRED_FIELDS[info_type]),
            }
            for info_type in INFO_TYPES
        },
        "signalContract": dict(SIGNAL_CONTRACT),
        "operations": [{
            "id": "publish.pack",
            "request": (
                "one or more active concrete topic_slugs plus required "
                "signal_type + signal_schema"
            ),
            "note": (
                "topic_slugs is authoritative; lifecycle uses the latest "
                "endDate across bound Topics"
            ),
        }],
    }


def test_pinned_catalog_passes_parity():
    assert_catalog_parity(pinned_catalog())


@pytest.mark.parametrize("mutate,expected", [
    (lambda c: c.update(version="2026-07-12.operation-registry"), "version"),
    (lambda c: c["enums"].update(signalType=["structured-data"]), "signalType enum drift"),
    (lambda c: c["publishSchemas"]["text"].update(requiredFields=["word_count"]), "text.requiredFields drift"),
    (lambda c: c["publishSchemas"]["structured"].update(deliveryFormat="csv"), "structured.deliveryFormat drift"),
    (lambda c: c["signalContract"].update(schemaField="fields"), "signalContract.schemaField drift"),
])
def test_catalog_drift_is_detected(mutate, expected):
    catalog = copy.deepcopy(pinned_catalog())
    mutate(catalog)
    with pytest.raises(RuntimeError, match=expected):
        assert_catalog_parity(catalog)


def test_mcp_publish_schema_matches_signal_contract():
    publish = next(tool for tool in asyncio.run(server.mcp.list_tools()) if tool.name == "packs_publish")
    properties = publish.inputSchema["properties"]
    assert "topic_slugs" in properties and "topic_slug" not in properties
    assert properties["topic_slugs"]["type"] == "array"
    assert properties["signal_schema"]["type"] == "object"
    assert "fields_json" in properties
    assert {
        "title", "info_type", "topic_slugs", "fields_json",
        "signal_type", "signal_schema",
    } <= set(publish.inputSchema["required"])


@pytest.mark.parametrize("info_type,fields", [
    ("structured", {"schema_version": "1.0", "columns": ["player", "status"]}),
    ("structured", {"schema_version": "1.0", "json_schema": {"odds": "number"}}),
    ("structured", {"schema_version": "1.0", "tables": [{"name": "x", "columns": ["id"]}]}),
    ("text", {"word_count": 10, "language": "en", "source_url": "seller note"}),
    ("figure", {"source_hash": "sha256:x", "resolution": "1x1", "capture_time": "2026-01-01T00:00:00Z", "media_type": "image/png", "file_name": "x.png", "file_size_bytes": 1, "preview_description": "preview", "verification_notes": "hash"}),
    ("video", {"duration": "00:01", "resolution": "1x1", "source_hash": "sha256:x", "media_type": "video/mp4", "file_name": "x.mp4", "file_size_bytes": 1, "preview_description": "preview", "verification_notes": "hash"}),
    ("audio", {"duration": "00:01", "format": "audio/mpeg", "source_hash": "sha256:x", "media_type": "audio/mpeg", "file_name": "x.mp3", "file_size_bytes": 1, "preview_description": "preview", "verification_notes": "hash"}),
])
def test_all_catalog_publish_shapes(info_type, fields):
    assert validate_publish_contract(
        info_type=info_type,
        topic_slugs=["market-winner"],
        signal_type="structured-data",
        signal_schema=VALID_SCHEMA,
        fields=fields,
    )


@pytest.mark.parametrize("count", [1, MAX_TOPIC_SLUGS])
def test_topic_slug_boundaries_are_accepted(count):
    slugs = [f"market-{index}" for index in range(count)]
    assert normalize_topic_slugs(slugs) == slugs


@pytest.mark.parametrize(
    "slugs",
    [[], [f"market-{index}" for index in range(MAX_TOPIC_SLUGS + 1)]],
)
def test_topic_slug_boundaries_are_rejected(slugs):
    with pytest.raises(RuntimeError, match="1-20"):
        normalize_topic_slugs(slugs)


@pytest.mark.parametrize("slugs,match", [
    (["market-winner", "market-winner"], "duplicate"),
    ([""], "non-empty"),
    (["one,two"], "one non-empty"),
    (["market-winner", 7], "must be a string"),
])
def test_topic_slug_entries_are_unambiguous(slugs, match):
    with pytest.raises(RuntimeError, match=match):
        normalize_topic_slugs(slugs)


def test_multi_topic_publish_contract_and_stale_fields_are_not_required():
    fields = {"word_count": 10, "language": "en", "source_url": "source"}
    assert validate_publish_contract(
        info_type="text",
        topic_slugs=["market-winner", "market-runner-up"],
        signal_type="narrative-intel",
        signal_schema=VALID_SCHEMA,
        fields=fields,
    ) == "markdown"
    assert "cadence" not in fields and "freshness_seconds" not in fields


@pytest.mark.parametrize("schema,match", [
    ({}, "non-empty"),
    ({"unsafe name": "string"}, "safe identifier"),
    ({"constructor": "string"}, "safe identifier"),
    ({"status": ""}, "non-empty type"),
    ({"status": 7}, "type-name string"),
])
def test_signal_schema_rejects_invalid_contracts(schema, match):
    with pytest.raises(RuntimeError, match=match):
        normalize_signal_schema(schema)


def test_fields_cannot_substitute_for_signal_schema():
    fields = {
        "word_count": 10,
        "language": "en",
        "source_url": "source",
        "status": "string",
    }
    with pytest.raises(RuntimeError, match="non-empty"):
        validate_publish_contract(
            info_type="text",
            topic_slugs=["market-winner"],
            signal_type="narrative-intel",
            signal_schema={},
            fields=fields,
        )


def test_mcp_publish_sends_canonical_multi_topic_body(monkeypatch):
    class FakeClient:
        def __init__(self):
            self.body = None

        async def publish_pack(self, body):
            self.body = body
            return {"pack_id": "pack-test", "pack": body}

    fake = FakeClient()
    monkeypatch.setattr(server, "_require_auth", lambda: None)
    monkeypatch.setattr(server, "_get_client", lambda: fake)
    result = asyncio.run(
        server.packs_publish.__wrapped__(
            title="Test Pack",
            info_type="text",
            topic_slugs=[" market-winner ", "market-runner-up"],
            fields_json=json.dumps({
                "word_count": 10,
                "language": "en",
                "source_url": "source",
            }),
            signal_type="narrative-intel",
            signal_schema={" status ": " string ", "observed_at": "datetime"},
        )
    )

    assert json.loads(result)["pack_id"] == "pack-test"
    assert fake.body["topic"] == "market-winner"
    assert fake.body["topic_slugs"] == ["market-winner", "market-runner-up"]
    assert fake.body["fields"] == {
        "word_count": 10,
        "language": "en",
        "source_url": "source",
    }
    assert fake.body["signal_schema"] == {
        "status": "string",
        "observed_at": "datetime",
    }
    assert fake.body["signal_type"] == "narrative-intel"


def test_fields_json_must_be_an_object():
    with pytest.raises(RuntimeError, match="JSON object"):
        parse_fields(json.dumps(["not", "an", "object"]))


def test_topics_list_omits_sector_and_projects_tag_slugs(monkeypatch):
    class FakeClient:
        async def list_topics(self, category="", state="active"):
            assert category == "sports"
            assert state == "active"
            return {
                "topics": [{
                    "slug": "topic-1",
                    "title": "Topic",
                    "category": "sports",
                    "tagSlugs": ["sports", "soccer"],
                    "sectorSlugs": ["must-not-leak"],
                }],
            }

    monkeypatch.setattr(server, "_get_client", lambda: FakeClient())
    result = json.loads(asyncio.run(
        server.topics_list.__wrapped__(category="sports", state="active")
    ))
    assert result["topics"][0]["tagSlugs"] == ["sports", "soccer"]
    assert "sectorSlugs" not in result["topics"][0]


def test_mcp_claim_list_forces_bearer_when_api_key_is_also_present(monkeypatch):
    calls = []

    async def fake_req(method, path, *, params=None, body=None, extra_headers=None):
        calls.append((method, path, params, extra_headers))
        return {"claims": []}

    monkeypatch.setattr(client_wrapper, "API_KEY", "acc_saved")
    monkeypatch.setattr(client_wrapper, "TOKEN", "jwt_session")
    monkeypatch.setattr(client_wrapper, "_req", fake_req)

    assert asyncio.run(client_wrapper.list_claims()) == {"claims": []}
    assert calls == [
        ("GET", "/claims", None, {"Authorization": "Bearer jwt_session"})
    ]


def test_mcp_claim_list_fails_closed_without_bearer(monkeypatch):
    monkeypatch.setattr(client_wrapper, "API_KEY", "acc_saved")
    monkeypatch.setattr(client_wrapper, "TOKEN", "")
    with pytest.raises(RuntimeError, match="Run auth_token"):
        asyncio.run(client_wrapper.list_claims())


def test_session_token_refreshes_bearer_without_creating_api_key(monkeypatch):
    calls = []

    class FakeAccount:
        address = "0xAgent"

    async def fake_post(path, body):
        calls.append((path, body))
        if body.get("action") == "challenge":
            return {
                "challenge": {
                    "challenge_id": "challenge-1",
                    "sign_payload": {"primaryType": "Authentication"},
                }
            }
        return {"token": "jwt_fresh", "token_type": "Bearer"}

    monkeypatch.setattr(client_wrapper, "API_KEY", "acc_saved")
    monkeypatch.setattr(client_wrapper, "TOKEN", "")
    monkeypatch.setattr(client_wrapper, "_account", lambda: FakeAccount())
    monkeypatch.setattr(
        client_wrapper, "_sign_typed_payload", lambda payload: "0xsigned"
    )
    monkeypatch.setattr(client_wrapper, "_post", fake_post)

    result = asyncio.run(client_wrapper.get_session_token())

    assert result["token"] == "jwt_fresh"
    assert client_wrapper.API_KEY == "acc_saved"
    assert client_wrapper.TOKEN == "jwt_fresh"
    assert calls == [
        ("/auth/token", {"agent_id": "0xAgent", "action": "challenge"}),
        ("/auth/token", {
            "agent_id": "0xAgent",
            "challenge_id": "challenge-1",
            "signature": "0xsigned",
        }),
    ]


def test_auth_token_activates_session_without_exposing_jwt(monkeypatch):
    class FakeClient:
        async def get_session_token(self):
            return {
                "token": "jwt_secret",
                "token_type": "Bearer",
                "auth_mode": "wallet_signature",
            }

    monkeypatch.setattr(server, "_get_client", lambda: FakeClient())
    result = json.loads(asyncio.run(server.auth_token.__wrapped__()))

    assert result["ok"] is True
    assert result["token_type"] == "Bearer"
    assert "token" not in result


def test_claims_pay_false_returns_live_preview_without_payment(monkeypatch):
    class FakeClient:
        paid = False

        async def get_claim_payment(self, claim_id):
            return {
                "_http_status": 402,
                "x402Version": 2,
                "accepts": [{
                    "network": "eip155:84532",
                    "asset": "0xUSDC",
                    "payTo": "0xSeller",
                    "amount": "150000",
                    "maxTimeoutSeconds": 60,
                }],
            }

        async def pay_claim(self, claim_id):
            self.paid = True
            return {"state": "paid_delivered"}

    fake = FakeClient()
    monkeypatch.setattr(server, "_require_auth", lambda: None)
    monkeypatch.setattr(server, "_get_client", lambda: fake)
    result = json.loads(asyncio.run(
        server.claims_pay.__wrapped__(
            "claim-1", confirm_real_payment=False
        )
    ))

    assert result["payment_performed"] is False
    assert result["confirmation_required"] is True
    assert result["payment_preview"]["accepts"][0]["payTo"] == "0xSeller"
    assert fake.paid is False


def test_claims_receipt_uses_direct_transaction_receipt(monkeypatch):
    class FakeClient:
        async def get_transaction_receipt(self, claim_id):
            return {
                "receipt": {
                    "claim": {
                        "claim_id": claim_id,
                        "state": "paid_delivered",
                    },
                    "payment": {"transaction_hash": "0xtx"},
                }
            }

    monkeypatch.setattr(server, "_require_auth", lambda: None)
    monkeypatch.setattr(server, "_get_client", lambda: FakeClient())
    result = json.loads(asyncio.run(
        server.claims_receipt.__wrapped__("claim-1")
    ))
    assert result["receipt"]["claim"]["state"] == "paid_delivered"
    assert result["receipt"]["payment"]["transaction_hash"] == "0xtx"


def test_mcp_surface_is_exact_23_tool_contract():
    names = {tool.name for tool in asyncio.run(server.mcp.list_tools())}
    assert len(names) == 23
    assert {"auth_token", "claims_receipt"} <= names
    assert {
        "packs_relist", "orders_list", "sales_list",
    }.isdisjoint(names)


def test_mcp_surface_equals_the_shared_exact_manifest():
    names = {tool.name for tool in asyncio.run(server.mcp.list_tools())}
    assert names == EXPECTED_MCP_TOOLS


def test_checked_in_exact_manifest_and_sha256_match_the_shared_contract():
    manifest_path = (
        Path(__file__).resolve().parents[1]
        / "docs"
        / "exact-mcp-tool-manifest-v0.6.json"
    )
    raw = manifest_path.read_bytes()
    manifest = json.loads(raw)

    assert manifest == sorted(EXPECTED_MCP_TOOLS)
    assert len(manifest) == len(set(manifest)) == 23
    assert hashlib.sha256(raw).hexdigest() == EXPECTED_MCP_MANIFEST_SHA256


def test_mcp_publish_schema_exposes_contract_bounds_and_enums():
    publish = next(
        tool for tool in asyncio.run(server.mcp.list_tools())
        if tool.name == "packs_publish"
    )
    properties = publish.inputSchema["properties"]
    assert properties["topic_slugs"]["minItems"] == 1
    assert properties["topic_slugs"]["maxItems"] == 20
    assert properties["info_type"]["enum"] == list(INFO_TYPES)
    assert properties["signal_type"]["enum"] == list(SIGNAL_TYPES)


@pytest.mark.parametrize(
    "topic_slugs,signal_schema,signal_type,error",
    [
        ([f"market-{index}" for index in range(21)], VALID_SCHEMA, "narrative-intel", "1-20"),
        (["market-one"], None, "narrative-intel", "signal_schema"),
        (["market-one,market-two"], VALID_SCHEMA, "narrative-intel", "one non-empty"),
        (["market-one"], VALID_SCHEMA, "unsupported-signal", "signal_type"),
    ],
)
def test_sdk_publish_rejects_invalid_contract_before_network(
    monkeypatch, topic_slugs, signal_schema, signal_type, error
):
    import accessura_sdk.client as sdk_client
    from accessura_sdk import SellerAgent

    monkeypatch.setattr(
        sdk_client,
        "_request",
        lambda *_args, **_kwargs: pytest.fail("invalid publish reached the network"),
    )
    seller = SellerAgent("0x" + "22" * 32, delivery_secret="ab" * 32)
    kwargs = {
        "topic_slugs": topic_slugs,
        "fields": {
            "word_count": 100,
            "language": "en",
            "source_url": "https://seller.example/source",
        },
        "signal_type": signal_type,
    }
    if signal_schema is not None:
        kwargs["signal_schema"] = signal_schema
    with pytest.raises(RuntimeError, match=error):
        seller.publish_pack("Election signal", "text", **kwargs)


def test_sdk_publish_normalizes_body_and_derives_topic_alias(monkeypatch):
    import accessura_sdk.client as sdk_client
    from accessura_sdk import SellerAgent

    captured = {}

    def fake_request(method, url, headers, body=None):
        captured.update(method=method, url=url, headers=headers, body=body)
        return {"ok": True, "pack_id": "pack-one"}

    monkeypatch.setattr(sdk_client, "_request", fake_request)
    seller = SellerAgent(
        "0x" + "22" * 32,
        delivery_secret="ab" * 32,
        api_key="acc_saved",
    )
    result = seller.publish_pack(
        "Election signal",
        "text",
        topic="caller-supplied-alias",
        topic_slugs=[" election-market ", "sports-market"],
        fields={
            "word_count": 100,
            "language": "en",
            "source_url": "https://seller.example/source",
        },
        signal_type="narrative-intel",
        signal_schema={" status ": " string "},
    )

    assert result["pack_id"] == "pack-one"
    assert captured["method"] == "POST"
    assert captured["body"]["topic"] == "election-market"
    assert captured["body"]["topic_slugs"] == [
        "election-market",
        "sports-market",
    ]
    assert captured["body"]["signal_schema"] == {"status": "string"}
    assert captured["body"]["delivery_format"] == "markdown"


def test_topic_queries_are_sector_free_in_wrapper_and_sdk(monkeypatch):
    import urllib.parse

    import accessura_sdk.client as sdk_client
    from accessura_sdk import BuyerAgent, SellerAgent

    wrapper_calls = []

    async def fake_get(path, params=None):
        wrapper_calls.append((path, params))
        return {"topics": []}

    monkeypatch.setattr(client_wrapper, "_get", fake_get)
    asyncio.run(client_wrapper.list_topics(category="sports", state="past"))
    assert wrapper_calls == [
        ("/topics", {"state": "past", "category": "sports"})
    ]

    sdk_urls = []

    def fake_request(method, url, headers, body=None):
        sdk_urls.append(url)
        return {"topics": []}

    monkeypatch.setattr(sdk_client, "_request", fake_request)
    BuyerAgent("0x" + "11" * 32).list_topics(
        category="politics", state="active"
    )
    SellerAgent(
        "0x" + "22" * 32,
        delivery_secret="ab" * 32,
    ).list_topics(category="politics", state="active")

    assert len(sdk_urls) == 2
    for url in sdk_urls:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        assert query == {"state": ["active"], "category": ["politics"]}
        assert "sector" not in query


def test_signal_schema_cannot_satisfy_pack_required_fields():
    with pytest.raises(RuntimeError, match="word_count, language, source_url"):
        validate_publish_contract(
            info_type="text",
            topic_slugs=["election-market"],
            signal_type="narrative-intel",
            signal_schema={
                "word_count": "integer",
                "language": "string",
                "source_url": "string",
            },
            fields={},
        )


def test_signal_contract_pins_scope_and_public_fields():
    assert SIGNAL_CONTRACT["scope"] == (
        "One Pack-level payload contract shared by every Signal in that Pack"
    )
    assert SIGNAL_CONTRACT["publicSignalFields"] == [
        "id",
        "label",
        "summary",
        "source",
        "observedAt",
        "confidence",
        "topicSlugs",
    ]


def test_skill_validator_checks_both_package_versions(tmp_path):
    validate_skill_bundle = load_repo_script("validate_skill_bundle")

    root_project = tmp_path / "pyproject.toml"
    sdk_project = tmp_path / "sdk-pyproject.toml"
    root_project.write_text('[project]\nversion = "0.6.0"\n')
    sdk_project.write_text('[project]\nversion = "0.6.1"\n')

    with pytest.raises(SystemExit):
        validate_skill_bundle.validate_project_versions(
            "0.6.0", (root_project, sdk_project)
        )


def test_skill_validator_scans_references_readme_and_examples(tmp_path):
    validate_skill_bundle = load_repo_script("validate_skill_bundle")

    paths = {
        path.relative_to(validate_skill_bundle.ROOT).as_posix()
        for path in validate_skill_bundle.forbidden_scan_files()
    }
    assert "README.md" in paths
    assert "accessura/references/authentication.md" in paths
    assert "accessura/references/market-data.md" in paths
    assert "accessura/references/trading.md" in paths
    assert "examples/example_buyer.py" in paths

    forbidden = tmp_path / "example.md"
    forbidden.write_text("orders_list")
    with pytest.raises(SystemExit):
        validate_skill_bundle.validate_forbidden_text((forbidden,))


def test_kit_keeps_all_five_stable_version_pins():
    from pathlib import Path

    root = Path(__file__).parent.parent
    assert 'version = "0.6.0"' in (root / "pyproject.toml").read_text()
    assert 'version = "0.6.0"' in (
        root / "accessura_sdk" / "pyproject.toml"
    ).read_text()
    assert (root / "accessura" / "VERSION").read_text().strip() == "0.6.0"
    assert "@v0.6.0" in (root / "README.md").read_text()
    assert "@v0.6.0" in (root / "server.py").read_text()


def test_broken_repo_external_javascript_examples_are_removed():
    from pathlib import Path

    root = Path(__file__).parent.parent
    assert not (root / "examples" / "agent-protocol-quickstart.mjs").exists()
    assert not (root / "examples" / "seller-readiness-dry-run.mjs").exists()


def test_local_mcp_config_is_ignored_and_documented_as_secret_bearing():
    from pathlib import Path

    root = Path(__file__).parent.parent
    assert ".mcp.json" in (root / ".gitignore").read_text().splitlines()
    assert "Do not commit" in (root / "README.md").read_text()
    assert "Do not commit" in (root / "server.py").read_text()


def test_trading_example_derives_topic_alias_from_first_slug():
    from pathlib import Path

    root = Path(__file__).parent.parent
    trading = (
        root / "accessura" / "references" / "trading.md"
    ).read_text()
    assert '"topic": "<current-politics-or-sports-topic-slug>"' in trading


def test_active_fixtures_use_neutral_market_language():
    from pathlib import Path

    root = Path(__file__).parent.parent
    legacy_host = "worldcup" + ".example"
    legacy_query = "Nor" + "way"
    files = [
        root / "accessura_sdk" / "__init__.py",
        root / "accessura_sdk" / "client.py",
        root / "accessura_sdk" / "README.md",
        root / "examples" / "example_buyer.py",
        root / "scripts" / "smoke_installed_package.py",
        root / "tests" / "test_direct_sdk.py",
    ]
    for path in files:
        text = path.read_text()
        assert legacy_host not in text
        assert legacy_query not in text


def _valid_funded_evidence():
    buyer = "0x" + "11" * 20
    seller = "0x" + "22" * 20
    tx_hash = "0x" + "ab" * 32
    return {
        "network": "eip155:84532",
        "claim_id": "claim-funded-one",
        "buyer_address": buyer,
        "seller_payout_address": seller,
        "platform_addresses": ["0x" + "33" * 20],
        "bid": {
            "buyer_usdc_before": "1000000",
            "buyer_usdc_after": "1000000",
            "transfer_count": 0,
        },
        "award": {
            "state": "award_pending_delivery",
            "payment_tx_hash": None,
        },
        "pre_delivery_payment": {"http_status": 202},
        "delivery_ready_payment": {
            "http_status": 402,
            "network": "eip155:84532",
            "asset": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
            "pay_to": seller,
            "amount": "150000",
        },
        "preview": {
            "confirm_real_payment": False,
            "payment_performed": False,
            "expected_amount": "150000",
            "expected_pay_to": seller,
            "buyer_usdc_before": "1000000",
            "buyer_usdc_after": "1000000",
            "transfer_count": 0,
        },
        "payment": {
            "confirm_real_payment": True,
            "expected_amount": "150000",
            "expected_pay_to": seller,
            "transfers": [{
                "tx_hash": tx_hash,
                "from": buyer,
                "to": seller,
                "asset": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
                "amount": "150000",
            }],
        },
        "decrypt": {
            "local": True,
            "plaintext_sha256": "cd" * 32,
        },
        "retry": {
            "payment_tx_hash": tx_hash,
            "new_transfer_count": 0,
        },
        "receipt": {
            "claim_id": "claim-funded-one",
            "payment_tx_hash": tx_hash,
        },
    }


def test_funded_evidence_verifier_covers_all_nine_release_assertions():
    verifier = load_repo_script("verify_funded_testnet_evidence")

    assert verifier.validate_evidence(_valid_funded_evidence()) == list(
        verifier.REQUIRED_ASSERTIONS
    )


def test_funded_evidence_verifier_rejects_duplicate_payment():
    verifier = load_repo_script("verify_funded_testnet_evidence")

    evidence = _valid_funded_evidence()
    evidence["payment"]["transfers"].append(
        dict(evidence["payment"]["transfers"][0])
    )
    with pytest.raises(verifier.EvidenceError, match="exactly one"):
        verifier.validate_evidence(evidence)


def test_funded_runner_cli_cannot_accept_credentials():
    verifier = load_repo_script("verify_funded_testnet_evidence")

    option_strings = {
        option
        for action in verifier.build_parser()._actions
        for option in action.option_strings
    }
    assert option_strings == {"-h", "--help", "--execute", "--validate"}
    assert not any(
        token in option.lower()
        for option in option_strings
        for token in ("key", "token", "secret", "payout")
    )


def test_funded_runner_refuses_any_mainnet_override(monkeypatch):
    verifier = load_repo_script("verify_funded_testnet_evidence")

    monkeypatch.setenv("ACCESSURA_ALLOW_MAINNET", "0")
    with pytest.raises(
        verifier.ExecutionError,
        match="ACCESSURA_ALLOW_MAINNET must be absent",
    ):
        verifier.FundedConfig()


def test_funded_validate_summary_is_release_record_ready():
    verifier = load_repo_script("verify_funded_testnet_evidence")

    summary = verifier._validated_summary(_valid_funded_evidence())
    assert summary["verified"] is True
    assert summary["funded_testnet_tx"] == "0x" + "ab" * 32
    assert summary["funded_testnet_amount_base_units"] == "150000"
    assert all(summary["assertion_results"].values())
    assert summary["real_payment_performed_by_this_script"] is False


def test_ci_paths_cover_expanded_skill_and_funded_gates():
    from pathlib import Path

    root = Path(__file__).parent.parent
    skill_workflow = (
        root / ".github" / "workflows" / "validate-skill.yml"
    ).read_text()
    package_workflow = (
        root / ".github" / "workflows" / "package.yml"
    ).read_text()
    assert '"accessura_sdk/pyproject.toml"' in skill_workflow
    assert '"examples/**"' in skill_workflow
    assert '"scripts/verify_funded_testnet_evidence.py"' in package_workflow
    assert '"docs/exact-mcp-tool-manifest-v0.6.json"' in package_workflow


def test_sdk_publish_rejects_unknown_info_type_before_network(monkeypatch):
    import accessura_sdk.client as sdk_client
    from accessura_sdk import SellerAgent

    monkeypatch.setattr(
        sdk_client,
        "_request",
        lambda *_args, **_kwargs: pytest.fail("invalid publish reached the network"),
    )
    seller = SellerAgent("0x" + "22" * 32, delivery_secret="ab" * 32)
    with pytest.raises(RuntimeError, match="info_type"):
        seller.publish_pack(
            "Election signal",
            "database",
            topic_slugs=["election-market"],
            fields={},
            signal_type="narrative-intel",
            signal_schema=VALID_SCHEMA,
        )
