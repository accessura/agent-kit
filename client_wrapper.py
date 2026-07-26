"""
Unified API client for the Accessura MCP server (server.py).

HTTP layer: httpx (async) against ACCESSURA_BASE_URL.
Crypto layer: reuses the canonical accessura_sdk implementation
(decrypt_delivery / parse_key_release / resolve_key_release_url) and mirrors
src/lib/crypto/ecies.ts sellerWrapDek byte-for-byte on the seller side.

Credentials come from environment variables only — never from tool arguments:
    ACCESSURA_BASE_URL     API host (default: https://testnet.accessura.io)
    ACCESSURA_API_KEY      "acc_..." API key  -> Authorization: ApiKey ...
    ACCESSURA_TOKEN        JWT               -> Authorization: Bearer ...
    ACCESSURA_PRIVATE_KEY  secp256k1 private key hex. Used in-process for
                           EIP-712 signing and buyer-side ECIES decrypt.
                           It is never sent anywhere and never logged.
    ACCESSURA_DELIVERY_SECRET  dedicated 32-byte hex secret used only for
                           seller-side per-signal DEK derivation.
"""

import asyncio
import json
import os
import weakref
from typing import Any, Optional
from urllib.parse import quote

import httpx

from accessura_sdk.crypto import (
    ECIES_ALG,
    decrypt_delivery,
    derive_signal_dek,
    encrypt_signal_content,
    normalize_delivery_secret,
    normalize_encryption_pubkey,
    seller_wrap_dek,
)
from accessura_sdk.client import (
    DEFAULT_X402_NETWORK,
    _assert_safe_auth_challenge,
    _base_payment_controls,
    _enforce_payment_controls,
    _local_payment_controls,
    _payment_control_config,
    _payment_readiness,
    _public_payment_controls,
    _sign_bid_authorization,
    _sign_bid_payment_authorization,
    _sign_x402_payment,
    _summarize_payment_controls,
    _unknown_payment_controls,
    _usdc_price_base_units,
    _validate_fact_page,
)

BASE_URL = os.getenv("ACCESSURA_BASE_URL", "https://testnet.accessura.io").rstrip("/")
API_KEY = os.getenv("ACCESSURA_API_KEY", "")
TOKEN = os.getenv("ACCESSURA_TOKEN", "")
PRIVATE_KEY = os.getenv("ACCESSURA_PRIVATE_KEY", "")
DELIVERY_SECRET = os.getenv("ACCESSURA_DELIVERY_SECRET", "")
_PAYMENT_AUTHORITY_LOCKS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _payment_authority_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _PAYMENT_AUTHORITY_LOCKS.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _PAYMENT_AUTHORITY_LOCKS[loop] = lock
    return lock


# ── Credentials ───────────────────────────────────────────────────────────

def set_credentials(api_key: str = "", token: str = "") -> None:
    """Update in-process credentials (e.g. right after /auth/apikey exchange),
    so follow-up calls in the same server process use the new key without a
    restart."""
    global API_KEY, TOKEN
    if api_key:
        API_KEY = api_key
    if token:
        TOKEN = token


def _auth_headers() -> dict[str, str]:
    """Authorization header from in-process credentials (ApiKey wins)."""
    h: dict[str, str] = {}
    if API_KEY:
        h["Authorization"] = f"ApiKey {API_KEY}"
    elif TOKEN:
        h["Authorization"] = f"Bearer {TOKEN}"
    return h


def _bearer_auth_headers() -> dict[str, str]:
    """Bearer auth for the deployed claim-list route, which is JWT-only."""
    if not TOKEN:
        raise RuntimeError(
            "Bearer token required for claims_list. Run auth_token to create a "
            "fresh in-process session token (or set ACCESSURA_TOKEN)."
        )
    return {"Authorization": f"Bearer {TOKEN}"}


def _has_auth() -> bool:
    return bool(API_KEY or TOKEN)


def _require_private_key() -> str:
    if not PRIVATE_KEY:
        raise RuntimeError(
            "ACCESSURA_PRIVATE_KEY env var required for this operation. "
            "Set it in the MCP server environment (never pass keys as tool arguments)."
        )
    return PRIVATE_KEY


def _require_delivery_secret() -> bytes:
    if not DELIVERY_SECRET:
        raise RuntimeError(
            "ACCESSURA_DELIVERY_SECRET is required for managed seller encryption. "
            "Set a dedicated 32-byte hex secret; never reuse ACCESSURA_PRIVATE_KEY."
        )
    secret = normalize_delivery_secret(DELIVERY_SECRET)
    if PRIVATE_KEY:
        wallet_hex = PRIVATE_KEY[2:] if PRIVATE_KEY.lower().startswith("0x") else PRIVATE_KEY
        if secret == bytes.fromhex(wallet_hex):
            raise RuntimeError("ACCESSURA_DELIVERY_SECRET must not equal ACCESSURA_PRIVATE_KEY")
    return secret


def _account():
    """eth_account Account for the env private key (EIP-712 signing)."""
    _require_private_key()
    try:
        from eth_account.account import Account
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("eth-account not installed — pip install eth-account") from e
    return Account.from_key(PRIVATE_KEY)


# ── HTTP core ─────────────────────────────────────────────────────────────

async def _req(method: str, path: str, *, params: Optional[dict] = None,
               body: Optional[dict] = None, extra_headers: Optional[dict] = None) -> dict[str, Any]:
    status, _, data = await _req_response(
        method, path, params=params, body=body, extra_headers=extra_headers)
    if status >= 400:
        raise RuntimeError(f"HTTP {status} {method} {path}: {json.dumps(data)[:300]}")
    return data


async def _req_response(method: str, path: str, *, params: Optional[dict] = None,
                        body: Optional[dict] = None,
                        extra_headers: Optional[dict] = None) -> tuple[int, dict, dict[str, Any]]:
    """Protocol-aware HTTP response, retaining x402 402 status and headers."""
    url = f"{BASE_URL}/api/v1{path}"
    headers = {"User-Agent": "Accessura-MCP/0.7", **_auth_headers(), **(extra_headers or {})}
    if body is not None:
        headers["Content-Type"] = "application/json"
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.request(method, url, headers=headers, params=params,
                                 json=body if body is not None else None)
    try:
        data = r.json()
    except ValueError:
        data = {"error": "non-JSON response", "message": r.text[:300]}
    return r.status_code, dict(r.headers), data


async def _get(path: str, params: Optional[dict] = None) -> dict[str, Any]:
    return await _req("GET", path, params=params)


async def _post(path: str, body: Optional[dict] = None) -> dict[str, Any]:
    return await _req("POST", path, body=body or {})


def _quote(s: str) -> str:
    return quote(s, safe="")


# ── Discovery (no auth) ───────────────────────────────────────────────────

async def list_topics(category: str = "", state: str = "active") -> dict:
    params: dict[str, Any] = {"state": state}
    if category:
        params["category"] = category
    return await _get("/topics", params)


async def list_topic_packs(slug: str, state: str = "all") -> dict:
    return await _get(f"/topics/{_quote(slug)}/packs", {"state": state})


async def get_catalog() -> dict:
    return await _get("/catalog")


async def get_leaderboard(limit: int = 20) -> dict:
    return await _get("/leaderboard", {"limit": limit})


# ── Packs ─────────────────────────────────────────────────────────────────

async def search_packs(query: str = "", topic_slug: str = "", info_type: str = "",
                       sort: str = "recency", limit: int = 20, page: int = 1) -> dict:
    params: dict[str, Any] = {"limit": limit, "page": page, "sort": sort}
    if query:
        params["q"] = query
    if topic_slug:
        params["topic_slug"] = topic_slug
    if info_type:
        params["info_type"] = info_type
    return await _get("/packs", params)


async def get_pack(pack_id: str) -> dict:
    return await _get(f"/packs/{_quote(pack_id)}")


async def publish_pack(pack_data: dict) -> dict:
    return await _post("/packs", pack_data)


async def delist_pack(pack_id: str) -> dict:
    return await _post(f"/packs/{_quote(pack_id)}/delist")


async def reopen_signal_settlement(pack_id: str, signal_id: str) -> dict:
    return await _post(
        f"/packs/{_quote(pack_id)}/signals/{_quote(signal_id)}/settlement-readiness")


# ── Signals ───────────────────────────────────────────────────────────────

async def append_signal(pack_id: str, signal_data: dict) -> dict:
    return await _post(f"/packs/{_quote(pack_id)}/signals", signal_data)


# ── Bidding ───────────────────────────────────────────────────────────────

async def _collect_financial_pages(
    view: str,
    *,
    budget_start_at: Optional[str] = None,
) -> list[dict]:
    pages: list[dict] = []
    cursor: Optional[str] = None
    seen_cursors: set[str] = set()
    for _ in range(1_000):
        params: dict[str, Any] = {"view": view, "limit": 200}
        if budget_start_at:
            params["from"] = budget_start_at
        if cursor:
            params["cursor"] = cursor
        status, _, page = await _req_response(
            "GET", "/transactions", params=params)
        if status >= 400:
            raise RuntimeError(
                f"financial facts API unavailable: HTTP {status} {page}")
        _validate_fact_page(page, view)
        pages.append(page)
        if not page["has_more"]:
            return pages
        next_cursor = page["next_cursor"]
        if next_cursor in seen_cursors:
            raise RuntimeError("financial facts pagination repeated a cursor")
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    raise RuntimeError("financial facts pagination exceeded 1000 pages")


async def _load_payment_controls(network: str) -> dict:
    try:
        config = _payment_control_config(network)
    except RuntimeError:
        return _local_payment_controls(network)
    controls = _base_payment_controls(config)
    if not config["budget_configured"]:
        return controls
    if config["configured_status"] in ("not_started", "expired"):
        return controls
    try:
        payments = await _collect_financial_pages(
            "payments", budget_start_at=config["budget_start_at"])
        exposure = await _collect_financial_pages("active_exposure")
        return _summarize_payment_controls(network, payments, exposure)
    except Exception as exc:
        return _unknown_payment_controls(config, str(exc))


async def place_bid(pack_id: str, bid_data: dict) -> dict:
    signal_id = str(bid_data.get("signal_id") or "")
    price = float(bid_data.get("bid_price"))
    if not signal_id:
        raise RuntimeError("signal_id is required for a direct signed bid")
    bid_amount = _usdc_price_base_units(price)
    async with _payment_authority_lock():
        if bid_data.get("authorization"):
            if not bid_data.get("payment_authorization"):
                raise RuntimeError(
                    "a caller-supplied BidAuthorization must include its "
                    "payment_authorization")
            status = await get_bid_status(pack_id, signal_id)
            network = str((status.get("payment_terms") or {}).get("network", ""))
            controls = await _load_payment_controls(network)
            _enforce_payment_controls(
                amount_base_units=bid_amount,
                network=network,
                controls=controls,
                action="binding bid submission",
            )
            return await _post(f"/packs/{_quote(pack_id)}/bid", bid_data)
        for attempt in range(2):
            status = await get_bid_status(pack_id, signal_id)
            payment_terms = status.get("payment_terms")
            network = str((payment_terms or {}).get("network", ""))
            controls = await _load_payment_controls(network)
            _enforce_payment_controls(
                amount_base_units=bid_amount,
                network=network,
                controls=controls,
                action="binding bid",
            )
            from accessura_sdk.client import BuyerAgent
            agent = BuyerAgent(
                private_key=_require_private_key(),
                base_url=BASE_URL,
            )
            payment_authorization, payment_fingerprint = (
                _sign_bid_payment_authorization(
                    _account(),
                    payment_terms,
                    bid_amount,
                    payment_controls=controls,
                )
            )
            authorization = _sign_bid_authorization(
                _account(),
                agent._enc_pub,
                pack_id,
                signal_id,
                price,
                status,
                payment_fingerprint,
            )
            code, _, response = await _req_response(
                "POST", f"/packs/{_quote(pack_id)}/bid",
                body={
                    **bid_data,
                    "authorization": authorization,
                    "payment_authorization": payment_authorization,
                })
            if code < 400:
                return response
            if (
                response.get("error_code") != "BID_AUTHORIZATION_MISMATCH"
                or attempt == 1
            ):
                raise RuntimeError(
                    f"HTTP {code} POST /packs/{pack_id}/bid: {response}")
    raise RuntimeError("unreachable bid retry state")


async def get_bid_status(pack_id: str, signal_id: str = "") -> dict:
    params = {"signal_id": signal_id} if signal_id else None
    return await _get(f"/packs/{_quote(pack_id)}/bid", params)


async def payment_readiness(network: str = DEFAULT_X402_NETWORK) -> dict:
    """Signing configuration plus platform payment/exposure facts."""
    readiness = _payment_readiness(_account(), network)
    controls = await _load_payment_controls(network)
    readiness["payment_controls"] = _public_payment_controls(controls)
    readiness["signing_ready"] = controls["budget_status"] in (
        "unconfigured", "ready")
    return readiness


# ── Claims & Settlement ──────────────────────────────────────────────────

async def settle_auction(pack_id: str, signal_id: str) -> dict:
    return await _post(f"/packs/{_quote(pack_id)}/settle", {"signal_id": signal_id})


async def list_claims(role: str = "buyer") -> dict:
    params = {"role": "seller"} if role == "seller" else None
    return await _req(
        "GET", "/claims", params=params, extra_headers=_bearer_auth_headers())


async def get_claim_payment(claim_id: str) -> dict:
    status, headers, data = await _req_response(
        "GET", f"/claims/{_quote(claim_id)}/pay")
    return {**data, "_http_status": status,
            "_payment_required": headers.get("payment-required")}


async def get_transaction_receipt(claim_id: str) -> dict:
    """Read the participant-visible direct transaction receipt by claim ID."""
    return await _get(f"/transactions/{_quote(claim_id)}/receipt")


async def pay_claim(claim_id: str, expected_amount: Optional[str] = None,
                    expected_pay_to: Optional[str] = None) -> dict:
    """Sign x402 with ACCESSURA_PRIVATE_KEY and pay the seller directly.

    expected_amount / expected_pay_to (read from a prior read-only preview) bind
    the signature to the previewed terms; the signer refuses if they changed.
    """
    async with _payment_authority_lock():
        status, _, required = await _req_response(
            "GET", f"/claims/{_quote(claim_id)}/pay")
        if status in (200, 202):
            return {**required, "_http_status": status}
        if status != 402:
            raise RuntimeError(
                f"HTTP {status} GET /claims/{claim_id}/pay: {required}")
        accepts = required.get("accepts")
        network = (
            str(accepts[0].get("network", ""))
            if isinstance(accepts, list) and accepts
            and isinstance(accepts[0], dict)
            else ""
        )
        controls = await _load_payment_controls(network)
        _, payment_header = _sign_x402_payment(
            _account(), required,
            expected_amount=expected_amount,
            expected_pay_to=expected_pay_to,
            payment_controls=controls,
            claim_id=claim_id,
        )
        paid_status, _, paid = await _req_response(
            "POST", f"/claims/{_quote(claim_id)}/pay", body={},
            extra_headers={"PAYMENT-SIGNATURE": payment_header})
        if paid_status >= 400:
            raise RuntimeError(
                f"HTTP {paid_status} POST /claims/{claim_id}/pay: {paid}")
        return {**paid, "_http_status": paid_status}


async def fetch_paid_ciphertext(ciphertext_url: str) -> dict:
    from urllib.parse import urlsplit

    target = urlsplit(ciphertext_url)
    api_origin = urlsplit(BASE_URL)
    same_origin = (target.scheme, target.netloc) == (api_origin.scheme, api_origin.netloc)
    if not same_origin:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                ciphertext_url,
                # Seller hosts receive no Accessura API key/JWT.
                headers={"User-Agent": "Accessura-MCP/0.7"},
            )
        if response.status_code >= 400:
            raise RuntimeError(f"HTTP {response.status_code} ciphertext fetch: {response.text[:300]}")
        return response.json()
    prefix = f"{BASE_URL}/api/v1"
    path = ciphertext_url[len(prefix):] if ciphertext_url.startswith(prefix) else target.path
    return await _get(path)


async def deliver_key_release(claim_id: str, platform_broker: dict,
                              ciphertext_url: str = "") -> dict:
    body = {"platform_broker": platform_broker}
    if ciphertext_url:
        body["ciphertext_url"] = ciphertext_url
    return await _post(f"/claims/{_quote(claim_id)}/key-release", body)


# ── Wallet ────────────────────────────────────────────────────────────────

# ── Auth ──────────────────────────────────────────────────────────────────

async def bind_seller_payout_wallet(chain: str = DEFAULT_X402_NETWORK) -> dict:
    """Prove the env private key controls the seller's direct payout wallet."""
    account = _account()
    challenge_result = await _post("/sellers/payout-wallet/challenge", {
        "payout_address": account.address,
        "chain": chain,
    })
    challenge = challenge_result.get("challenge") or {}
    payload = challenge.get("sign_payload")
    if not payload:
        raise RuntimeError(f"seller payout challenge failed: {challenge_result}")
    signature = _sign_typed_payload(payload)
    return await _post("/sellers/payout-wallet/verify", {
        "challenge_id": challenge.get("challenge_id"),
        "signature": signature,
    })


async def get_seller_readiness() -> dict:
    """Read Seller-owned payout and delivery readiness state."""
    return await _req(
        "GET",
        "/sellers/readiness",
        extra_headers=_bearer_auth_headers(),
    )


async def update_seller_readiness(
    status: str = "",
    sla_seconds: Optional[int] = None,
) -> dict:
    """Pause/resume Seller delivery or update the listing-visible SLA."""
    normalized_status = status.strip().lower()
    if normalized_status and normalized_status not in {"active", "paused"}:
        raise RuntimeError("status must be active or paused")
    if sla_seconds is not None and (
        isinstance(sla_seconds, bool)
        or not isinstance(sla_seconds, int)
        or not 30 <= sla_seconds <= 86_400
    ):
        raise RuntimeError("sla_seconds must be an integer from 30 to 86400")
    if not normalized_status and sla_seconds is None:
        raise RuntimeError("status or sla_seconds required")
    body: dict[str, Any] = {}
    if normalized_status:
        body["status"] = normalized_status
    if sla_seconds is not None:
        body["sla_seconds"] = sla_seconds
    return await _req(
        "POST",
        "/sellers/readiness",
        body=body,
        extra_headers=_bearer_auth_headers(),
    )


def register_identity(agent_name: str, role: str = "buyer") -> dict:
    """Register the env-keyed identity (idempotent). Signs the EIP-712
    IdentityRegistration payload with ACCESSURA_PRIVATE_KEY — the backend
    rejects unsigned address-derived registrations (anti-squatting).
    Raises on failure (non-2xx is never success)."""
    _require_private_key()
    from accessura_sdk.client import BuyerAgent

    agent = BuyerAgent(private_key=PRIVATE_KEY, base_url=BASE_URL)
    agent.register(name=agent_name, role=role)  # raises RuntimeError on failure
    return {
        "ok": True,
        "agent_id": agent.agent_id,
        "role": role,
        "encryption_pubkey": agent._enc_pub,
    }


def _sign_typed_payload(payload: dict) -> str:
    """EIP-712 sign a backend auth challenge with the env private key.

    Guarded against blind-signing: a spoofed/compromised backend (or a hostile
    ACCESSURA_BASE_URL) must not coax the wallet into signing a disguised USDC
    TransferWithAuthorization through an auth path. Only the binding-bid signer
    (or legacy claims_pay compatibility path) may authorize payment.
    """
    from eth_account.messages import encode_typed_data

    _assert_safe_auth_challenge(payload)
    account = _account()
    typed = encode_typed_data(
        payload["domain"],
        {payload["primaryType"]: [
            {"name": f["name"], "type": f["type"]}
            for f in payload["types"][payload["primaryType"]]
        ]},
        payload["message"],
    )
    s = account.sign_message(typed).signature.hex()
    return s if s.startswith("0x") else "0x" + s


async def get_api_key() -> dict:
    """Full API-key flow: challenge -> EIP-712 sign in-process -> exchange.
    Updates in-process credentials so subsequent calls use the new key."""
    account = _account()
    agent_id = account.address
    data = await _post("/auth/apikey", {"agent_id": agent_id, "action": "challenge"})
    ch = data.get("challenge") or {}
    payload = ch.get("sign_payload")
    if not payload:
        raise RuntimeError(f"apikey challenge failed: {data.get('error') or data}")
    signature = _sign_typed_payload(payload)
    out = await _post("/auth/apikey", {
        "agent_id": agent_id,
        "challenge_id": ch.get("challenge_id"),
        "signature": signature,
        "action": "exchange",
    })
    if not out.get("api_key"):
        raise RuntimeError(f"apikey exchange failed: {out.get('error') or out}")
    set_credentials(api_key=out["api_key"], token=out.get("token", ""))
    if not TOKEN:
        # /claims is Bearer-only; cache an immediate session token so claim
        # polling works without a separate manual auth_token step.
        try:
            await get_session_token()
            out["_bearer"] = "ready"
        except Exception as e:  # api key still valid for non-claims routes
            out["_bearer"] = f"deferred: run auth_token before claims_list ({e})"
    return out


async def get_session_token() -> dict:
    """Refresh the JWT needed by claims.list without issuing another API key."""
    account = _account()
    agent_id = account.address
    data = await _post("/auth/token", {"agent_id": agent_id, "action": "challenge"})
    ch = data.get("challenge") or {}
    payload = ch.get("sign_payload")
    if not payload:
        raise RuntimeError(f"token challenge failed: {data.get('error') or data}")
    signature = _sign_typed_payload(payload)
    out = await _post("/auth/token", {
        "agent_id": agent_id,
        "challenge_id": ch.get("challenge_id"),
        "signature": signature,
    })
    if not out.get("token"):
        raise RuntimeError(f"token exchange failed: {out.get('error') or out}")
    set_credentials(token=out["token"])
    return out


# ── ECIES crypto (delegates to accessura_sdk.crypto) ──────────────────────

# Direct re-exports from SDK crypto
from accessura_sdk.crypto import (  # noqa: E402 — re-export
    seller_wrap_dek,
    seller_wrap_pre_encrypted_dek,
)

# Managed seller wrappers (use only ACCESSURA_DELIVERY_SECRET)
_encrypt_content = encrypt_signal_content  # the SDK function
_derive_dek = derive_signal_dek            # the SDK function

def encrypt_signal_content(plaintext: bytes, pack_id: str, signal_id: str) -> str:
    return _encrypt_content(plaintext, _require_delivery_secret(), pack_id, signal_id)

def derive_signal_dek(pack_id: str, signal_id: str) -> bytes:
    return _derive_dek(_require_delivery_secret(), pack_id, signal_id)

def buyer_decrypt(broker: dict, ciphertext_b64: str) -> bytes:
    return decrypt_delivery(broker, ciphertext_b64, _require_private_key())
