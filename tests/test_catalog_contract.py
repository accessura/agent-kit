import asyncio
import json

import pytest

import client_wrapper
import server
from catalog_contract import (
    MAX_TOPIC_SLUGS,
    normalize_signal_schema,
    normalize_topic_slugs,
    parse_fields,
    validate_publish_contract,
)


VALID_SCHEMA = {"status": "string", "observed_at": "datetime"}


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
        topic_slugs=["world-cup-winner"],
        signal_type="structured-data",
        signal_schema=VALID_SCHEMA,
        fields=fields,
    )


@pytest.mark.parametrize("count", [1, MAX_TOPIC_SLUGS])
def test_topic_slug_boundaries_are_accepted(count):
    slugs = [f"world-cup-market-{index}" for index in range(count)]
    assert normalize_topic_slugs(slugs) == slugs


@pytest.mark.parametrize("slugs", [[], [f"world-cup-market-{index}" for index in range(MAX_TOPIC_SLUGS + 1)]])
def test_topic_slug_boundaries_are_rejected(slugs):
    with pytest.raises(RuntimeError, match="1-20"):
        normalize_topic_slugs(slugs)


@pytest.mark.parametrize("slugs,match", [
    (["world-cup-winner", "world-cup-winner"], "duplicate"),
    ([""], "non-empty"),
    (["one,two"], "one non-empty"),
    (["world-cup-winner", 7], "must be a string"),
])
def test_topic_slug_entries_are_unambiguous(slugs, match):
    with pytest.raises(RuntimeError, match=match):
        normalize_topic_slugs(slugs)


def test_multi_topic_publish_contract_and_stale_fields_are_not_required():
    fields = {"word_count": 10, "language": "en", "source_url": "source"}
    assert validate_publish_contract(
        info_type="text",
        topic_slugs=["world-cup-winner", "world-cup-golden-boot-winner"],
        signal_type="narrative-intel",
        signal_schema=VALID_SCHEMA,
        fields=fields,
    ) == "markdown"
    assert "cadence" not in fields and "freshness_seconds" not in fields


def test_mcp_publish_schema_exposes_multi_topic_and_independent_signal_contract():
    publish = next(tool for tool in asyncio.run(server.mcp.list_tools()) if tool.name == "packs_publish")
    properties = publish.inputSchema["properties"]
    assert "topic_slugs" in properties and "topic_slug" not in properties
    assert properties["topic_slugs"]["type"] == "array"
    assert properties["signal_schema"]["type"] == "object"
    assert "fields_json" in properties
    assert {
        "title", "info_type", "topic_slugs", "fields_json", "signal_type", "signal_schema",
    } <= set(publish.inputSchema["required"])


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
    fields = {"word_count": 10, "language": "en", "source_url": "source", "status": "string"}
    with pytest.raises(RuntimeError, match="non-empty"):
        validate_publish_contract(
            info_type="text",
            topic_slugs=["world-cup-winner"],
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
            topic_slugs=[" world-cup-winner ", "world-cup-golden-boot-winner"],
            fields_json=json.dumps({"word_count": 10, "language": "en", "source_url": "source"}),
            signal_type="narrative-intel",
            signal_schema={" status ": " string ", "observed_at": "datetime"},
        )
    )

    assert json.loads(result)["pack_id"] == "pack-test"
    assert fake.body["topic"] == "world-cup-winner"
    assert fake.body["topic_slugs"] == ["world-cup-winner", "world-cup-golden-boot-winner"]
    assert fake.body["fields"] == {"word_count": 10, "language": "en", "source_url": "source"}
    assert fake.body["signal_schema"] == {"status": "string", "observed_at": "datetime"}
    assert fake.body["signal_type"] == "narrative-intel"


def test_fields_json_must_be_an_object():
    with pytest.raises(RuntimeError, match="JSON object"):
        parse_fields(json.dumps(["not", "an", "object"]))


def test_mcp_claim_list_forces_bearer_when_api_key_is_also_present(monkeypatch):
    calls = []

    async def fake_req(method, path, *, params=None, body=None, extra_headers=None):
        calls.append((method, path, params, extra_headers))
        return {"claims": []}

    monkeypatch.setattr(client_wrapper, "API_KEY", "acc_saved")
    monkeypatch.setattr(client_wrapper, "TOKEN", "jwt_session")
    monkeypatch.setattr(client_wrapper, "_req", fake_req)

    assert asyncio.run(client_wrapper.list_claims()) == {"claims": []}
    assert calls == [("GET", "/claims", None, {"Authorization": "Bearer jwt_session"})]


def test_mcp_claim_list_fails_closed_without_bearer(monkeypatch):
    monkeypatch.setattr(client_wrapper, "API_KEY", "acc_saved")
    monkeypatch.setattr(client_wrapper, "TOKEN", "")
    with pytest.raises(RuntimeError, match="Run auth.token"):
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
    monkeypatch.setattr(client_wrapper, "_sign_typed_payload", lambda payload: "0xsigned")
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
        server.claims_pay.__wrapped__("claim-1", confirm_real_payment=False)))

    assert result["payment_performed"] is False
    assert result["confirmation_required"] is True
    assert result["payment_preview"]["accepts"][0]["payTo"] == "0xSeller"
    assert fake.paid is False


def test_claims_receipt_uses_direct_transaction_receipt(monkeypatch):
    class FakeClient:
        async def get_transaction_receipt(self, claim_id):
            return {
                "receipt": {
                    "claim": {"claim_id": claim_id, "state": "paid_delivered"},
                    "payment": {"transaction_hash": "0xtx"},
                }
            }

    monkeypatch.setattr(server, "_require_auth", lambda: None)
    monkeypatch.setattr(server, "_get_client", lambda: FakeClient())
    result = json.loads(asyncio.run(server.claims_receipt.__wrapped__("claim-1")))
    assert result["receipt"]["claim"]["state"] == "paid_delivered"
    assert result["receipt"]["payment"]["transaction_hash"] == "0xtx"


def test_mcp_surface_replaces_legacy_custody_history_tools():
    names = {tool.name for tool in asyncio.run(server.mcp.list_tools())}
    assert len(names) == 24
    assert {"auth_token", "claims_receipt"} <= names
    assert "orders_list" not in names
    assert "sales_list" not in names
