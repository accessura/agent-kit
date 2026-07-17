import base64
import json
from pathlib import Path

import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data

from accessura_sdk.client import (
    BASE_SEPOLIA_USDC,
    BID_AUTHORIZATION_TYPES,
    PROTOCOL_DOMAIN,
    BuyerAgent,
    HumanBuyer,
    SellerAgent,
    _canonical_json,
    _js_number_string,
    _sign_bid_authorization,
    _sign_x402_payment,
)


PRIVATE_KEY = "0x" + "11" * 32
SELLER = Account.from_key("0x" + "22" * 32).address
ASSET = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


def test_bid_authorization_recovers_buyer_and_matches_js_strings():
    buyer = BuyerAgent(PRIVATE_KEY, base_url="https://worldcup.example")
    authorization = _sign_bid_authorization(
        buyer._account,
        buyer._enc_pub,
        "pack-1",
        "signal-1",
        0.000001,
        {"round": {"round_id": "round-7", "closes_at": "2099-01-01T00:00:00.000Z"}},
    )
    message = {
        **{key: authorization[key] for key in (
            "bid_id", "pack_id", "signal_id", "buyer_payment_address",
            "buyer_signing_key", "buyer_encryption_pubkey", "delegation_id",
            "window_id", "nonce", "expiry",
        )},
        "signal_scope": _canonical_json(authorization["signal_scope"]),
        "price": _js_number_string(authorization["price"]),
    }
    typed = encode_typed_data(PROTOCOL_DOMAIN, BID_AUTHORIZATION_TYPES, message)
    recovered = Account.recover_message(typed, signature=authorization["signature"])
    assert recovered.lower() == buyer.agent_id.lower()
    assert message["signal_scope"] == '{"mode":"single_signal","signal_id":"signal-1"}'
    assert message["price"] == "0.000001"
    assert _js_number_string(1e-7) == "1e-7"
    assert _js_number_string(1e21) == "1e+21"


def test_x402_header_is_exact_base_usdc_transfer_authorization():
    buyer = Account.from_key(PRIVATE_KEY)
    required = {
        "x402Version": 2,
        "resource": {"url": "https://worldcup.example/api/v1/claims/claim-1/pay"},
        "accepts": [{
            "scheme": "exact",
            "network": "eip155:8453",
            "asset": ASSET,
            "amount": "150000",
            "payTo": SELLER,
            "maxTimeoutSeconds": 60,
            "extra": {"name": "USD Coin", "version": "2"},
        }],
    }
    payload, header = _sign_x402_payment(buyer, required)
    assert json.loads(base64.b64decode(header)) == payload
    assert payload["x402Version"] == 2
    assert payload["resource"] == required["resource"]
    assert payload["accepted"] == required["accepts"][0]

    authorization = payload["payload"]["authorization"]
    assert authorization["from"].lower() == buyer.address.lower()
    assert authorization["to"].lower() == SELLER.lower()
    assert authorization["value"] == "150000"
    assert len(bytes.fromhex(payload["payload"]["signature"][2:])) == 65

    domain = {
        "name": "USD Coin",
        "version": "2",
        "chainId": 8453,
        "verifyingContract": ASSET,
    }
    types = {"TransferWithAuthorization": [
        {"name": "from", "type": "address"},
        {"name": "to", "type": "address"},
        {"name": "value", "type": "uint256"},
        {"name": "validAfter", "type": "uint256"},
        {"name": "validBefore", "type": "uint256"},
        {"name": "nonce", "type": "bytes32"},
    ]}
    typed = encode_typed_data(domain, types, {
        **authorization,
        "value": int(authorization["value"]),
        "validAfter": int(authorization["validAfter"]),
        "validBefore": int(authorization["validBefore"]),
    })
    recovered = Account.recover_message(typed, signature=payload["payload"]["signature"])
    assert recovered.lower() == buyer.address.lower()


def test_x402_header_accepts_exact_base_sepolia_test_usdc_challenge():
    buyer = Account.from_key(PRIVATE_KEY)
    required = {
        "x402Version": 2,
        "resource": {"url": "https://worldcup.example/api/v1/claims/claim-sepolia/pay"},
        "accepts": [{
            "scheme": "exact",
            "network": "eip155:84532",
            "asset": BASE_SEPOLIA_USDC,
            "amount": "150000",
            "payTo": SELLER,
            "maxTimeoutSeconds": 60,
            "extra": {"name": "USDC", "version": "2"},
        }],
    }
    payload, header = _sign_x402_payment(buyer, required)
    assert json.loads(base64.b64decode(header)) == payload
    assert payload["resource"] == required["resource"]
    assert payload["accepted"] == required["accepts"][0]

    authorization = payload["payload"]["authorization"]
    typed = encode_typed_data({
        "name": "USDC",
        "version": "2",
        "chainId": 84532,
        "verifyingContract": BASE_SEPOLIA_USDC,
    }, {"TransferWithAuthorization": [
        {"name": "from", "type": "address"},
        {"name": "to", "type": "address"},
        {"name": "value", "type": "uint256"},
        {"name": "validAfter", "type": "uint256"},
        {"name": "validBefore", "type": "uint256"},
        {"name": "nonce", "type": "bytes32"},
    ]}, {
        **authorization,
        "value": int(authorization["value"]),
        "validAfter": int(authorization["validAfter"]),
        "validBefore": int(authorization["validBefore"]),
    })
    recovered = Account.recover_message(typed, signature=payload["payload"]["signature"])
    assert recovered.lower() == buyer.address.lower()


def test_x402_signer_rejects_unsupported_network_asset_or_domain():
    buyer = Account.from_key(PRIVATE_KEY)
    required = {
        "x402Version": 2,
        "resource": {"url": "https://worldcup.example/api/v1/claims/claim-1/pay"},
        "accepts": [{
            "scheme": "exact",
            "network": "eip155:1",
            "asset": ASSET,
            "amount": "150000",
            "payTo": SELLER,
            "maxTimeoutSeconds": 60,
            "extra": {"name": "USDC", "version": "2"},
        }],
    }
    with pytest.raises(RuntimeError, match="unsupported x402 network"):
        _sign_x402_payment(buyer, required)
    required["accepts"][0]["network"] = "eip155:8453"
    required["accepts"][0]["asset"] = SELLER
    required["accepts"][0]["extra"] = {"name": "USD Coin", "version": "2"}
    with pytest.raises(RuntimeError, match="configured Base USDC"):
        _sign_x402_payment(buyer, required)
    required["accepts"][0]["asset"] = ASSET
    required["accepts"][0]["extra"] = {"name": "USDC", "version": "2"}
    with pytest.raises(RuntimeError, match="unexpected Base USDC EIP-712 domain"):
        _sign_x402_payment(buyer, required)


def test_external_ciphertext_fetch_never_forwards_accessura_auth(monkeypatch):
    buyer = BuyerAgent(PRIVATE_KEY, base_url="https://worldcup.example")
    buyer._api_key = "acc_secret"
    calls = []

    class Response:
        status_code = 200
        headers = {}
        text = json.dumps({"ciphertext_b64": "opaque"})

    class Httpx:
        def request(self, method, url, headers, content, timeout):
            calls.append((url, headers))
            return Response()

    import accessura_sdk.client as client
    monkeypatch.setattr(client, "_HTTPX", Httpx())
    assert buyer.fetch_paid_ciphertext({
        "ciphertext_url": "https://seller.example/claim-1.json",
    }) == "opaque"
    assert "Authorization" not in calls[0][1]


def test_human_buyer_cannot_submit_unsigned_direct_bid():
    buyer = HumanBuyer("human-1", "buyer@example.com", "unused")
    with pytest.raises(RuntimeError, match="Agent-only"):
        buyer.login()
    with pytest.raises(RuntimeError, match="locally signed BuyerAgent"):
        buyer.bid("pack-1", "signal-1", 1)


def test_claim_lists_use_bearer_when_api_key_is_also_present(monkeypatch):
    calls = []

    class Response:
        status_code = 200
        headers = {}
        text = json.dumps({"claims": [], "deliveries": []})

    class Httpx:
        def request(self, method, url, headers, content, timeout):
            calls.append((url, headers))
            return Response()

    import accessura_sdk.client as client
    monkeypatch.setattr(client, "_HTTPX", Httpx())

    buyer = BuyerAgent(PRIVATE_KEY, base_url="https://worldcup.example")
    buyer._api_key = "acc_buyer"
    buyer._token = "jwt_buyer"
    assert buyer.get_claims() == []

    seller = SellerAgent(
        "0x" + "22" * 32,
        base_url="https://worldcup.example",
        delivery_secret="ab" * 32,
    )
    seller._api_key = "acc_seller"
    seller._token = "jwt_seller"
    assert seller.list_claims() == []

    assert calls[0][1]["Authorization"] == "Bearer jwt_buyer"
    assert calls[1][1]["Authorization"] == "Bearer jwt_seller"


def test_claim_lists_fail_closed_without_bearer():
    buyer = BuyerAgent(PRIVATE_KEY)
    buyer._api_key = "acc_buyer"
    with pytest.raises(RuntimeError, match="Bearer token required"):
        buyer.get_claims()

    seller = SellerAgent("0x" + "22" * 32, delivery_secret="ab" * 32)
    seller._api_key = "acc_seller"
    with pytest.raises(RuntimeError, match="Bearer token required"):
        seller.list_claims()


def test_managed_seller_append_returns_only_its_local_ciphertext(monkeypatch):
    requests = []

    class Response:
        status_code = 201
        headers = {}
        text = json.dumps({"ok": True, "pack_id": "pack-1", "appended": 1})

    class Httpx:
        def request(self, method, url, headers, content, timeout):
            requests.append(json.loads(content.decode()))
            return Response()

    import accessura_sdk.client as client
    monkeypatch.setattr(client, "_HTTPX", Httpx())
    seller = SellerAgent(
        "0x" + "22" * 32,
        base_url="https://worldcup.example",
        delivery_secret="ab" * 32,
    )
    seller._token = "jwt_seller"

    result = seller.append_signal(
        "pack-1",
        "Encrypted update",
        "Public metadata only",
        content_text='{"private":"signal"}',
        encrypt_with_managed=True,
    )

    assert result["_local_signal_id"] == requests[0]["id"]
    assert result["_local_ciphertext_b64"] == requests[0]["content_b64"]
    assert "private" not in json.dumps(requests[0])
    assert "_local_ciphertext_b64" not in requests[0]


def test_payment_readiness_defaults_to_base_sepolia_without_platform_balance():
    readiness = BuyerAgent(PRIVATE_KEY).payment_readiness()
    assert readiness["network"] == "eip155:84532"
    assert readiness["usdc_contract"].lower() == BASE_SEPOLIA_USDC.lower()
    assert readiness["custody"] == "self_custody"
    assert readiness["signing_ready"] is True
    assert readiness["payment_ready"] is None
    assert readiness["balance_status"] == "not_checked"
    assert readiness["platform_balance"] is None


def test_seller_managed_encryption_requires_a_separate_delivery_secret():
    missing = SellerAgent(PRIVATE_KEY)
    with pytest.raises(RuntimeError, match="dedicated 32-byte"):
        missing._require_delivery_secret()
    reused = SellerAgent(PRIVATE_KEY, delivery_secret=PRIVATE_KEY)
    with pytest.raises(RuntimeError, match="must not equal"):
        reused._require_delivery_secret()
    distinct = SellerAgent(PRIVATE_KEY, delivery_secret="ab" * 32)
    assert distinct._require_delivery_secret() == bytes.fromhex("ab" * 32)


def test_mcp_public_surface_has_one_explicit_payment_action():
    root = Path(__file__).parent.parent
    source = (root / "server.py").read_text()
    wrapper = (root / "client_wrapper.py").read_text()
    sdk = (root / "accessura_sdk" / "client.py").read_text()
    assert '@safe("claims.pay")' in source
    assert '@safe("payments.readiness")' in source
    assert '@safe("seller.signal_reopen")' in source
    assert "confirm_real_payment" in source
    assert '@safe("wallet.balance")' not in source
    assert '@safe("wallet.deposit")' not in source
    assert '@safe("wallet.withdraw")' not in source
    assert '@safe("claims.receipt_ack")' not in source
    assert "async def wallet_deposit" not in wrapper
    assert "async def wallet_withdraw" not in wrapper
    assert "async def get_balance" not in wrapper
    assert "def wallet_deposit" not in sdk
    assert "def wallet_withdraw" not in sdk
    assert "def get_balance" not in sdk
