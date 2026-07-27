import base64
import json
from pathlib import Path

import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data

from accessura_sdk.client import (
    BASE_SEPOLIA_USDC,
    BID_AUTHORIZATION_TYPES,
    DEFAULT_X402_NETWORK,
    PROTOCOL_DOMAIN,
    BuyerAgent,
    HumanBuyer,
    SellerAgent,
    _canonical_json,
    _enforce_payment_controls,
    _binding_bid_sla_risk_warnings,
    _js_number_string,
    _summarize_payment_controls,
    _sign_bid_authorization,
    _sign_bid_payment_authorization,
    _sign_x402_payment,
)


PRIVATE_KEY = "0x" + "11" * 32
SELLER = Account.from_key("0x" + "22" * 32).address
ASSET = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


def test_bid_authorization_recovers_buyer_and_matches_js_strings():
    buyer = BuyerAgent(PRIVATE_KEY, base_url="https://market.example")
    authorization = _sign_bid_authorization(
        buyer._account,
        buyer._enc_pub,
        "pack-1",
        "signal-1",
        0.000001,
        {"round": {"round_id": "round-7", "closes_at": "2099-01-01T00:00:00.000Z"}},
        "sha256:" + "ab" * 32,
    )
    message = {
        **{key: authorization[key] for key in (
            "bid_id", "pack_id", "signal_id", "buyer_payment_address",
            "buyer_signing_key", "buyer_encryption_pubkey", "delegation_id",
            "window_id", "nonce", "expiry",
            "payment_authorization_fingerprint",
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


def test_binding_bid_signs_exact_payment_terms_and_fingerprint(monkeypatch):
    monkeypatch.setattr("time.time", lambda: 1_700_000_000)
    buyer = Account.from_key(PRIVATE_KEY)
    terms = {
        "scheme": "exact",
        "network": "eip155:84532",
        "asset": BASE_SEPOLIA_USDC,
        "pay_to": SELLER,
        "token_domain": {"name": "USDC", "version": "2"},
        "authorization_valid_before_min": "1700000900",
        "authorization_valid_before_max": "1700001200",
        "payment_trigger": "seller_delivery_ready",
        "settlement_rule": "top_n_pay_as_bid",
        "seller_delivery_sla_seconds": 900,
    }
    compact, fingerprint = _sign_bid_payment_authorization(
        buyer, terms, 150000)
    assert compact["authorization"]["from"].lower() == buyer.address.lower()
    assert compact["authorization"]["to"].lower() == SELLER.lower()
    assert compact["authorization"]["value"] == "150000"
    assert compact["authorization"]["validBefore"] == "1700000900"
    assert len(bytes.fromhex(compact["signature"][2:])) == 65
    assert fingerprint.startswith("sha256:")
    assert len(fingerprint) == len("sha256:") + 64


def test_buyer_bid_submits_compact_payment_authorization_bound_to_bid(monkeypatch):
    import accessura_sdk.client as client

    calls = []
    status = {
        "round": {
            "round_id": "round-binding-1",
            "closes_at": "2099-01-01T00:00:00.000Z",
        },
        "payment_terms": {
            "scheme": "exact",
            "network": "eip155:84532",
            "asset": BASE_SEPOLIA_USDC,
            "pay_to": SELLER,
            "token_domain": {"name": "USDC", "version": "2"},
            "authorization_valid_before_min": "4070908800",
            "authorization_valid_before_max": "4070909100",
            "payment_trigger": "seller_delivery_ready",
            "settlement_rule": "top_n_pay_as_bid",
            "seller_delivery_sla_seconds": 86_400,
        },
    }

    def fake_request(method, url, headers, json_body=None):
        calls.append((method, url, json_body))
        if method == "GET":
            return status
        return {"ok": True, "received": json_body}

    monkeypatch.setattr(client, "_request", fake_request)
    buyer = BuyerAgent(
        PRIVATE_KEY,
        base_url="https://market.example",
        api_key="acc_test",
    )
    result = buyer.bid("pack-1", "signal-1", 0.15)
    submitted = result["received"]
    compact = submitted["payment_authorization"]
    authorization = submitted["authorization"]
    expected_fingerprint = "sha256:" + __import__("hashlib").sha256(
        _canonical_json(compact).encode("utf-8")
    ).hexdigest()
    assert authorization["payment_authorization_fingerprint"] == expected_fingerprint
    assert compact["authorization"]["value"] == "150000"
    assert compact["authorization"]["to"].lower() == SELLER.lower()
    assert result["payment_risk_warnings"] == [{
        "code": "LONG_SELLER_DELIVERY_SLA",
        "seller_delivery_sla_seconds": 86_400,
        "warning_threshold_seconds": 3_600,
        "message": result["payment_risk_warnings"][0]["message"],
    }]
    assert calls[-1][0] == "POST"
    status_result = buyer.get_bid_status("pack-1", "signal-1")
    assert status_result["payment_risk_warnings"][0]["code"] == (
        "LONG_SELLER_DELIVERY_SLA")


def test_binding_bid_long_sla_warning_is_informational_and_validated():
    short_terms = {"seller_delivery_sla_seconds": 3_600}
    long_terms = {"seller_delivery_sla_seconds": 3_601}
    assert _binding_bid_sla_risk_warnings(short_terms) == []
    warning = _binding_bid_sla_risk_warnings(long_terms)
    assert warning[0]["code"] == "LONG_SELLER_DELIVERY_SLA"
    assert warning[0]["seller_delivery_sla_seconds"] == 3_601
    with pytest.raises(RuntimeError, match="30 to 86400"):
        _binding_bid_sla_risk_warnings(
            {"seller_delivery_sla_seconds": 86_401})


def test_x402_header_is_exact_base_usdc_transfer_authorization(monkeypatch):
    buyer = Account.from_key(PRIVATE_KEY)
    required = {
        "x402Version": 2,
        "resource": {"url": "https://market.example/api/v1/claims/claim-1/pay"},
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
    # A fully-valid Base mainnet offer is still refused until promotion gates pass.
    monkeypatch.delenv("ACCESSURA_ALLOW_MAINNET", raising=False)
    with pytest.raises(RuntimeError, match="mainnet .* is closed"):
        _sign_x402_payment(buyer, required)
    monkeypatch.setenv("ACCESSURA_ALLOW_MAINNET", "1")
    monkeypatch.setenv("ACCESSURA_MAX_PAY_USDC", "1")
    monkeypatch.setenv("ACCESSURA_BUDGET_USDC", "10")
    monkeypatch.setenv("ACCESSURA_BUDGET_START_AT", "2000-01-01T00:00:00Z")
    monkeypatch.setenv("ACCESSURA_BUDGET_EXPIRES_AT", "2099-01-01T00:00:00Z")
    controls = {
        "budget_status": "ready",
        "budget_limit_base_units": "10000000",
        "budget_start_at": "2000-01-01T00:00:00Z",
        "budget_expires_at": "2099-01-01T00:00:00Z",
        "remaining_base_units": "10000000",
        "_active_claim_amounts": {},
    }
    payload, header = _sign_x402_payment(
        buyer, required, payment_controls=controls)
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
        "resource": {"url": "https://market.example/api/v1/claims/claim-sepolia/pay"},
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
        "resource": {"url": "https://market.example/api/v1/claims/claim-1/pay"},
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
    buyer = BuyerAgent(PRIVATE_KEY, base_url="https://market.example")
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


def test_buyer_reads_filtered_public_clearing_transcripts(monkeypatch):
    calls = []

    def fake_request(method, url, headers, body=None):
        calls.append((method, url, headers, body))
        return {
            "transcripts": [{"transcript_id": "tr-1"}],
            "round_summaries": [{"lowest_winning_price": 1.1}],
        }

    import accessura_sdk.client as client
    monkeypatch.setattr(client, "_request", fake_request)
    buyer = BuyerAgent(PRIVATE_KEY, base_url="https://market.example")
    result = buyer.get_clearing_transcripts(
        "pack/one", signal_id="signal one", round_index=2, limit=5
    )
    assert result["round_summaries"][0]["lowest_winning_price"] == 1.1
    assert calls == [(
        "GET",
        "https://market.example/api/v1/clearing/transcripts"
        "?pack_id=pack%2Fone&limit=5&signal_id=signal%20one&round_index=2",
        {},
        None,
    )]
    with pytest.raises(RuntimeError, match="non-negative"):
        buyer.get_clearing_transcripts("pack-1", round_index=-1)
    with pytest.raises(RuntimeError, match="non-negative"):
        buyer.get_clearing_transcripts("pack-1", round_index=True)
    with pytest.raises(RuntimeError, match="1 to 100"):
        buyer.get_clearing_transcripts("pack-1", limit=101)
    with pytest.raises(RuntimeError, match="1 to 100"):
        buyer.get_clearing_transcripts("pack-1", limit=True)


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

    buyer = BuyerAgent(PRIVATE_KEY, base_url="https://market.example")
    buyer._api_key = "acc_buyer"
    buyer._token = "jwt_buyer"
    assert buyer.get_claims() == []

    seller = SellerAgent(
        "0x" + "22" * 32,
        base_url="https://market.example",
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


def test_seller_readiness_uses_bearer_and_validates_updates(monkeypatch):
    calls = []

    def fake_request(method, url, headers, body=None):
        calls.append((method, url, headers, body))
        return {"ok": True, "readiness": {"delivery": {"status": "active"}}}

    import accessura_sdk.client as client
    monkeypatch.setattr(client, "_request", fake_request)
    seller = SellerAgent(
        "0x" + "22" * 32,
        base_url="https://market.example",
        delivery_secret="ab" * 32,
        api_key="acc_seller",
        token="jwt_seller",
    )

    seller.get_readiness()
    seller.update_readiness(status="active", sla_seconds=900)

    assert calls == [
        (
            "GET",
            "https://market.example/api/v1/sellers/readiness",
            {"Authorization": "Bearer jwt_seller"},
            None,
        ),
        (
            "POST",
            "https://market.example/api/v1/sellers/readiness",
            {"Authorization": "Bearer jwt_seller"},
            {"status": "active", "sla_seconds": 900},
        ),
    ]
    with pytest.raises(ValueError, match="status or sla_seconds"):
        seller.update_readiness()
    with pytest.raises(ValueError, match="active or paused"):
        seller.update_readiness(status="operator_override")
    with pytest.raises(ValueError, match="30 to 86400"):
        seller.update_readiness(sla_seconds=86_401)


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
        base_url="https://market.example",
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
    assert readiness["payment_controls"] == {
        "mode": "per_payment_only",
        "enforcement": "official_kit_path_only",
        "per_payment_limit_base_units": "100000000",
        "budget_limit_base_units": None,
        "budget_start_at": None,
        "budget_expires_at": None,
        "spent_base_units": None,
        "active_exposure_base_units": None,
        "remaining_base_units": None,
        "budget_status": "unconfigured",
        "as_of": None,
        "history_complete_from": None,
        "unknown_reason": None,
    }


def test_signing_domain_and_payment_network_are_independent_constants():
    assert PROTOCOL_DOMAIN == {
        "name": "WorldcupProtocol",
        "version": "1",
        "chainId": 8453,
        "verifyingContract": "0x0000000000000000000000000000000000000000",
    }
    assert DEFAULT_X402_NETWORK == "eip155:84532"


def test_seller_managed_encryption_requires_a_separate_delivery_secret():
    missing = SellerAgent(PRIVATE_KEY)
    with pytest.raises(RuntimeError, match="dedicated 32-byte"):
        missing._require_delivery_secret()
    reused = SellerAgent(PRIVATE_KEY, delivery_secret=PRIVATE_KEY)
    with pytest.raises(RuntimeError, match="must not equal"):
        reused._require_delivery_secret()
    distinct = SellerAgent(PRIVATE_KEY, delivery_secret="ab" * 32)
    assert distinct._require_delivery_secret() == bytes.fromhex("ab" * 32)


def test_mcp_public_surface_places_payment_authority_at_binding_bid():
    root = Path(__file__).parent.parent
    source = (root / "server.py").read_text()
    wrapper = (root / "client_wrapper.py").read_text()
    sdk = (root / "accessura_sdk" / "client.py").read_text()
    assert '@safe("claims.pay")' in source
    assert '@safe("bids.place")' in source
    assert '@safe("claims.receipt")' in source
    assert '@safe("auth.token")' in source
    assert '@safe("payments.readiness")' in source
    assert '@safe("seller.signal_reopen")' in source
    assert '@safe("seller.readiness_get")' in source
    assert '@safe("seller.readiness_update")' in source
    assert "confirm_real_payment" in source
    assert "_sign_bid_payment_authorization" in wrapper
    assert '"payment_authorization": payment_authorization' in wrapper
    assert "payment_authorization_fingerprint" in sdk
    assert '@safe("wallet.balance")' not in source
    assert '@safe("wallet.deposit")' not in source
    assert '@safe("wallet.withdraw")' not in source
    assert '@safe("claims.receipt_ack")' not in source
    assert '@safe("packs.relist")' not in source
    assert '@safe("orders.list")' not in source
    assert '@safe("sales.list")' not in source
    assert "async def wallet_deposit" not in wrapper
    assert "async def wallet_withdraw" not in wrapper
    assert "async def get_balance" not in wrapper
    assert "async def relist_pack" not in wrapper
    assert "async def list_orders" not in wrapper
    assert "async def list_sales" not in wrapper
    assert "async def get_transaction_receipt" in wrapper
    assert "async def get_session_token" in wrapper
    assert "def wallet_deposit" not in sdk
    assert "def wallet_withdraw" not in sdk
    assert "def get_balance" not in sdk
    assert "def relist_pack" not in sdk
    assert "def list_orders" not in sdk
    assert "def list_sales" not in sdk
    assert '"/orders' not in wrapper
    assert '"/sales' not in wrapper
    assert '"/orders' not in sdk
    assert '"/sales' not in sdk
    assert hasattr(BuyerAgent, "get_transaction_receipt")
    assert hasattr(SellerAgent, "get_transaction_receipt")
    assert hasattr(SellerAgent, "login")
    assert hasattr(SellerAgent, "get_readiness")
    assert hasattr(SellerAgent, "update_readiness")


def test_sdk_constructors_accept_saved_credentials():
    buyer = BuyerAgent(
        "0x" + "11" * 32,
        api_key="acc_saved",
        token="jwt_saved",
    )
    seller = SellerAgent(
        "0x" + "22" * 32,
        delivery_secret="ab" * 32,
        api_key="acc_saved",
        token="jwt_saved",
    )

    assert buyer._auth() == {"Authorization": "ApiKey acc_saved"}
    assert buyer._bearer_auth() == {"Authorization": "Bearer jwt_saved"}
    assert seller._auth() == {"Authorization": "ApiKey acc_saved"}
    assert seller._bearer_auth() == {"Authorization": "Bearer jwt_saved"}


# ── Signing-safety guards (blind-sign, ceiling, preview-binding, mainnet) ──

def _valid_auth_challenge(agent_address: str) -> dict:
    return {
        "domain": PROTOCOL_DOMAIN,
        "primaryType": "AuthChallenge",
        "types": {"AuthChallenge": [
            {"name": "challenge_id", "type": "string"},
            {"name": "agent_id", "type": "string"},
            {"name": "nonce", "type": "string"},
            {"name": "expires_at", "type": "string"},
        ]},
        "message": {"challenge_id": "c1", "agent_id": agent_address,
                    "nonce": "n", "expires_at": "2099-01-01T00:00:00Z"},
    }


def test_sign_auth_challenge_signs_authchallenge_and_refuses_disguised_transfer():
    from accessura_sdk.client import _sign_auth_challenge, _assert_safe_auth_challenge

    acct = Account.from_key(PRIVATE_KEY)
    payload = _valid_auth_challenge(acct.address)
    sig = _sign_auth_challenge(acct, payload)
    typed = encode_typed_data(
        payload["domain"], {"AuthChallenge": payload["types"]["AuthChallenge"]},
        payload["message"])
    assert Account.recover_message(typed, signature=sig).lower() == acct.address.lower()

    # An EIP-3009 USDC transfer disguised as an auth challenge is refused: it
    # carries the USD Coin token domain, not the null-contract protocol domain.
    transfer = {
        "domain": {"name": "USD Coin", "version": "2", "chainId": 84532,
                   "verifyingContract": BASE_SEPOLIA_USDC},
        "primaryType": "TransferWithAuthorization",
        "types": {"TransferWithAuthorization": [{"name": "from", "type": "address"}]},
        "message": {"from": acct.address},
    }
    with pytest.raises(RuntimeError, match="domain name"):
        _sign_auth_challenge(acct, transfer)

    # Right domain, wrong primaryType -> refused.
    with pytest.raises(RuntimeError, match="primaryType"):
        _assert_safe_auth_challenge({"domain": PROTOCOL_DOMAIN,
                                     "primaryType": "SomethingElse",
                                     "types": {}, "message": {}})

    # Right domain name but non-null verifyingContract -> refused.
    with pytest.raises(RuntimeError, match="verifyingContract"):
        _assert_safe_auth_challenge({
            "domain": {**PROTOCOL_DOMAIN, "verifyingContract": BASE_SEPOLIA_USDC},
            "primaryType": "AuthChallenge", "types": {}, "message": {}})


def test_x402_signer_enforces_ceiling_binding_and_mainnet_gate(monkeypatch):
    from accessura_sdk.client import _sign_x402_payment, _payment_readiness

    buyer = Account.from_key(PRIVATE_KEY)

    def offer(amount="150000", pay_to=SELLER, network="eip155:84532",
              asset=BASE_SEPOLIA_USDC, name="USDC"):
        return {
            "x402Version": 2,
            "resource": {"url": "https://api.example/x"},
            "accepts": [{
                "scheme": "exact", "network": network, "asset": asset,
                "amount": amount, "payTo": pay_to, "maxTimeoutSeconds": 60,
                "extra": {"name": name, "version": "2"},
            }],
        }

    monkeypatch.delenv("ACCESSURA_MAX_PAY_USDC", raising=False)  # default 100 USDC
    with pytest.raises(RuntimeError, match="exceeds the ACCESSURA_MAX_PAY_USDC"):
        _sign_x402_payment(buyer, offer(amount=str(200 * 10 ** 6)))

    with pytest.raises(RuntimeError, match="amount changed since preview"):
        _sign_x402_payment(buyer, offer(amount="150000"), expected_amount="140000")

    other = Account.from_key("0x" + "33" * 32).address
    with pytest.raises(RuntimeError, match="payTo changed since preview"):
        _sign_x402_payment(buyer, offer(pay_to=SELLER), expected_pay_to=other)

    payload, _ = _sign_x402_payment(
        buyer, offer(amount="150000", pay_to=SELLER),
        expected_amount="150000", expected_pay_to=SELLER)
    assert payload["payload"]["authorization"]["value"] == "150000"

    monkeypatch.delenv("ACCESSURA_ALLOW_MAINNET", raising=False)
    with pytest.raises(RuntimeError, match="mainnet .* is closed"):
        _payment_readiness(buyer, "eip155:8453")
    monkeypatch.setenv("ACCESSURA_ALLOW_MAINNET", "1")
    mainnet = _payment_readiness(buyer, "eip155:8453")
    assert mainnet["network"] == "eip155:8453"
    assert mainnet["signing_ready"] is False
    assert "ACCESSURA_MAX_PAY_USDC" in (
        mainnet["payment_controls"]["unknown_reason"])

    monkeypatch.delenv("ACCESSURA_BUDGET_USDC", raising=False)
    monkeypatch.delenv("ACCESSURA_BUDGET_START_AT", raising=False)
    monkeypatch.delenv("ACCESSURA_BUDGET_EXPIRES_AT", raising=False)
    monkeypatch.setenv("ACCESSURA_MAX_PAY_USDC", "")  # ceiling disabled
    big, _ = _sign_x402_payment(buyer, offer(amount=str(500 * 10 ** 6)))
    assert big["payload"]["authorization"]["value"] == str(500 * 10 ** 6)


def test_budget_snapshot_sums_complete_history_and_deduplicates_exposure(monkeypatch):
    monkeypatch.setenv("ACCESSURA_BUDGET_USDC", "10")
    monkeypatch.setenv("ACCESSURA_BUDGET_START_AT", "2000-01-01T00:00:00Z")
    monkeypatch.setenv("ACCESSURA_BUDGET_EXPIRES_AT", "2099-01-01T00:00:00Z")
    payment_pages = [{
        "view": "payments",
        "items": [{
            "intent_id": "intent-paid",
            "amount_base_units": "2000000",
            "chain_fact_status": "confirmed",
        }],
        "next_cursor": None,
        "has_more": False,
        "as_of": "2026-07-25T20:00:00Z",
        "history_complete_from": "2000-01-01T00:00:00Z",
        "history_complete": True,
    }]
    exposure_pages = [{
        "view": "active_exposure",
        "items": [
            {
                "exposure_id": "bid:bid-awarded",
                "kind": "pending_settlement",
                "amount_base_units": "3000000",
                "bid_id": "bid-awarded",
                "claim_id": None,
                "intent_id": None,
            },
            {
                "exposure_id": "intent:intent-awarded",
                "kind": "awarded_unpaid",
                "amount_base_units": "3000000",
                "bid_id": "bid-awarded",
                "claim_id": "claim-awarded",
                "intent_id": "intent-awarded",
            },
            {
                "exposure_id": "intent:intent-paid",
                "kind": "reconciliation_uncertain",
                "amount_base_units": "2000000",
                "bid_id": "bid-paid",
                "claim_id": "claim-paid",
                "intent_id": "intent-paid",
            },
            {
                "exposure_id": "bid:bid-standby",
                "kind": "ranked_waiting",
                "amount_base_units": "1000000",
                "bid_id": "bid-standby",
                "claim_id": None,
                "intent_id": None,
            },
        ],
        "next_cursor": None,
        "has_more": False,
        "as_of": "2026-07-25T20:00:01Z",
        "history_complete_from": "2000-01-01T00:00:00Z",
        "snapshot_kind": "current_platform_state",
    }]

    controls = _summarize_payment_controls(
        DEFAULT_X402_NETWORK, payment_pages, exposure_pages)

    assert controls["spent_base_units"] == "2000000"
    assert controls["active_exposure_base_units"] == "4000000"
    assert controls["remaining_base_units"] == "4000000"
    assert controls["budget_status"] == "ready"
    with pytest.raises(RuntimeError, match="remaining cumulative"):
        _enforce_payment_controls(
            amount_base_units=4_000_001,
            network=DEFAULT_X402_NETWORK,
            controls=controls,
            action="bid",
        )
    _enforce_payment_controls(
        amount_base_units=3_000_000,
        network=DEFAULT_X402_NETWORK,
        controls=controls,
        action="x402 payment",
        claim_id="claim-awarded",
    )
    monkeypatch.setenv("ACCESSURA_BUDGET_EXPIRES_AT", "2001-01-01T00:00:00Z")
    with pytest.raises(RuntimeError, match="budget_status is 'expired'"):
        _enforce_payment_controls(
            amount_base_units=1,
            network=DEFAULT_X402_NETWORK,
            controls=controls,
            action="bid",
        )


def test_budget_history_incomplete_is_unknown_and_signing_fails_closed(monkeypatch):
    monkeypatch.setenv("ACCESSURA_BUDGET_USDC", "10")
    monkeypatch.setenv("ACCESSURA_BUDGET_START_AT", "1999-01-01T00:00:00Z")
    monkeypatch.setenv("ACCESSURA_BUDGET_EXPIRES_AT", "2099-01-01T00:00:00Z")
    controls = _summarize_payment_controls(
        DEFAULT_X402_NETWORK,
        [{
            "view": "payments",
            "items": [],
            "next_cursor": None,
            "has_more": False,
            "as_of": "2026-07-25T20:00:00Z",
            "history_complete_from": "2000-01-01T00:00:00Z",
            "history_complete": False,
        }],
        [{
            "view": "active_exposure",
            "items": [],
            "next_cursor": None,
            "has_more": False,
            "as_of": "2026-07-25T20:00:00Z",
            "history_complete_from": "2000-01-01T00:00:00Z",
            "snapshot_kind": "current_platform_state",
        }],
    )

    assert controls["budget_status"] == "unknown"
    assert controls["remaining_base_units"] is None
    with pytest.raises(RuntimeError, match="budget_status is 'unknown'"):
        _enforce_payment_controls(
            amount_base_units=1,
            network=DEFAULT_X402_NETWORK,
            controls=controls,
            action="bid",
        )


def test_mainnet_requires_explicit_limits_and_finite_budget_snapshot(monkeypatch):
    from accessura_sdk.client import _payment_readiness

    buyer = Account.from_key(PRIVATE_KEY)
    monkeypatch.setenv("ACCESSURA_ALLOW_MAINNET", "1")
    monkeypatch.delenv("ACCESSURA_MAX_PAY_USDC", raising=False)
    monkeypatch.delenv("ACCESSURA_BUDGET_USDC", raising=False)
    readiness = _payment_readiness(buyer, "eip155:8453")
    assert readiness["signing_ready"] is False
    assert readiness["payment_controls"]["budget_status"] == "unknown"

    monkeypatch.setenv("ACCESSURA_MAX_PAY_USDC", "1")
    readiness = _payment_readiness(buyer, "eip155:8453")
    assert "ACCESSURA_BUDGET_USDC" in (
        readiness["payment_controls"]["unknown_reason"])

    monkeypatch.setenv("ACCESSURA_BUDGET_USDC", "10")
    monkeypatch.setenv("ACCESSURA_BUDGET_START_AT", "2000-01-01T00:00:00Z")
    monkeypatch.setenv("ACCESSURA_BUDGET_EXPIRES_AT", "2099-01-01T00:00:00Z")
    readiness = _payment_readiness(buyer, "eip155:8453")
    assert readiness["payment_controls"]["budget_status"] == "unknown"
    assert "not been loaded" in readiness["payment_controls"]["unknown_reason"]


def test_readiness_gracefully_downgrades_when_financial_api_is_unavailable(monkeypatch):
    import accessura_sdk.client as client

    monkeypatch.setenv("ACCESSURA_BUDGET_USDC", "10")
    monkeypatch.setenv("ACCESSURA_BUDGET_START_AT", "2000-01-01T00:00:00Z")
    monkeypatch.setenv("ACCESSURA_BUDGET_EXPIRES_AT", "2099-01-01T00:00:00Z")

    class Response:
        status_code = 404
        headers = {}
        text = json.dumps({"error": "not found"})

    class Httpx:
        def request(self, method, url, headers, content, timeout):
            return Response()

    monkeypatch.setattr(client, "_HTTPX", Httpx())
    readiness = BuyerAgent(
        PRIVATE_KEY, base_url="https://api.example", api_key="acc_test"
    ).payment_readiness()

    assert readiness["signing_ready"] is False
    assert readiness["payment_controls"]["budget_status"] == "unknown"
    assert "financial facts API unavailable" in (
        readiness["payment_controls"]["unknown_reason"])


def test_readiness_follows_every_financial_fact_cursor(monkeypatch):
    import urllib.parse
    import accessura_sdk.client as client

    monkeypatch.setenv("ACCESSURA_BUDGET_USDC", "10")
    monkeypatch.setenv("ACCESSURA_BUDGET_START_AT", "2000-01-01T00:00:00Z")
    monkeypatch.setenv("ACCESSURA_BUDGET_EXPIRES_AT", "2099-01-01T00:00:00Z")
    calls = []

    class Response:
        def __init__(self, body):
            self.status_code = 200
            self.headers = {}
            self.text = json.dumps(body)

    class Httpx:
        def request(self, method, url, headers, content, timeout):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            view = query["view"][0]
            cursor = query.get("cursor", [None])[0]
            calls.append((view, cursor))
            common = {
                "view": view,
                "as_of": "2026-07-25T20:00:00Z",
                "history_complete_from": "2000-01-01T00:00:00Z",
            }
            if view == "payments" and cursor is None:
                return Response({
                    **common,
                    "items": [{
                        "intent_id": "paid-1",
                        "amount_base_units": "1000000",
                        "chain_fact_status": "confirmed",
                    }],
                    "has_more": True,
                    "next_cursor": "page-2",
                    "history_complete": True,
                })
            if view == "payments":
                return Response({
                    **common,
                    "items": [{
                        "intent_id": "paid-2",
                        "amount_base_units": "2000000",
                        "chain_fact_status": "confirmed",
                    }],
                    "has_more": False,
                    "next_cursor": None,
                    "history_complete": True,
                })
            return Response({
                **common,
                "items": [{
                    "exposure_id": "bid:active",
                    "kind": "pending_settlement",
                    "amount_base_units": "3000000",
                    "bid_id": "active",
                    "claim_id": None,
                    "intent_id": None,
                }],
                "has_more": False,
                "next_cursor": None,
                "snapshot_kind": "current_platform_state",
            })

    monkeypatch.setattr(client, "_HTTPX", Httpx())
    controls = BuyerAgent(
        PRIVATE_KEY, base_url="https://api.example", api_key="acc_test"
    ).payment_readiness()["payment_controls"]

    assert calls == [
        ("payments", None),
        ("payments", "page-2"),
        ("active_exposure", None),
    ]
    assert controls["spent_base_units"] == "3000000"
    assert controls["active_exposure_base_units"] == "3000000"
    assert controls["remaining_base_units"] == "4000000"
    assert controls["budget_status"] == "ready"


def test_register_signs_identity_under_the_protocol_domain(monkeypatch):
    import accessura_sdk.client as client

    posted = {}

    class Response:
        def __init__(self, status, body):
            self.status_code = status
            self.headers = {}
            self.text = json.dumps(body)

    class Httpx:
        def request(self, method, url, headers, content, timeout):
            if method == "GET":
                return Response(200, {})  # not yet registered
            posted["body"] = json.loads(content.decode())
            return Response(200, {"ok": True})

    monkeypatch.setattr(client, "_HTTPX", Httpx())
    buyer = BuyerAgent(PRIVATE_KEY, base_url="https://api.example")
    assert buyer.register(name="A", role="buyer") is True

    typed = encode_typed_data(
        PROTOCOL_DOMAIN,
        {"IdentityRegistration": [
            {"name": "agent_id", "type": "string"},
            {"name": "payment_address", "type": "string"},
            {"name": "encryption_pubkey", "type": "string"},
        ]},
        {"agent_id": buyer.agent_id, "payment_address": buyer.agent_id,
         "encryption_pubkey": buyer._enc_pub})
    recovered = Account.recover_message(typed, signature=posted["body"]["signature"])
    assert recovered.lower() == buyer.agent_id.lower()


def test_get_api_key_caches_immediate_bearer_when_exchange_omits_token(monkeypatch):
    import accessura_sdk.client as client

    class Response:
        def __init__(self, body):
            self.status_code = 200
            self.headers = {}
            self.text = json.dumps(body)

    buyer = BuyerAgent(PRIVATE_KEY, base_url="https://api.example")

    class Httpx:
        def request(self, method, url, headers, content, timeout):
            body = json.loads(content.decode()) if content else {}
            if body.get("action") == "challenge":
                return Response({"challenge": {
                    "challenge_id": "c1",
                    "sign_payload": _valid_auth_challenge(buyer.agent_id)}})
            return Response({"api_key": "acc_x"})  # exchange returns no token

    monkeypatch.setattr(client, "_HTTPX", Httpx())

    calls = {"login": 0}

    def fake_login():
        calls["login"] += 1
        buyer._token = "jwt_from_login"

    monkeypatch.setattr(buyer, "login", fake_login)
    assert buyer.get_api_key() == "acc_x"
    assert calls["login"] == 1
    assert buyer._token == "jwt_from_login"


def test_transaction_receipt_is_passthrough_without_local_secret_injection(monkeypatch):
    import accessura_sdk.client as client

    receipt = {
        "claim_id": "c1",
        "award": {"pack_id": "pack-1", "signal_id": "sig-1"},
        "payment": {"amount": "150000", "payTo": SELLER, "network": "eip155:84532",
                    "tx_hash": "0xabc"},
        "delivery": {"opaque": True},
    }

    class Response:
        status_code = 200
        headers = {}
        text = json.dumps(receipt)

    class Httpx:
        def request(self, method, url, headers, content, timeout):
            assert "/transactions/" in url and url.endswith("/receipt")
            return Response()

    monkeypatch.setattr(client, "_HTTPX", Httpx())
    buyer = BuyerAgent(PRIVATE_KEY, base_url="https://api.example", token="jwt")
    out = buyer.get_transaction_receipt("c1")
    assert out == receipt
    blob = json.dumps(out).lower()
    assert PRIVATE_KEY.lower() not in blob
    assert "11" * 32 not in blob
    for leaked in ("private_key", "delivery_secret", "\"dek\"", "plaintext",
                   "payment-signature"):
        assert leaked not in blob
