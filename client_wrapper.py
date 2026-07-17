"""
Unified API client for the Accessura MCP server (server.py).

HTTP layer: httpx (async) against ACCESSURA_BASE_URL.
Crypto layer: reuses the canonical accessura_sdk implementation
(decrypt_delivery / parse_key_release / resolve_key_release_url) and mirrors
src/lib/crypto/ecies.ts sellerWrapDek byte-for-byte on the seller side.

Credentials come from environment variables only — never from tool arguments:
    ACCESSURA_BASE_URL     API host (default: https://worldcup-direct-testnet.accessuraportal.com)
    ACCESSURA_API_KEY      "acc_..." API key  -> Authorization: ApiKey ...
    ACCESSURA_TOKEN        JWT               -> Authorization: Bearer ...
    ACCESSURA_PRIVATE_KEY  secp256k1 private key hex. Used in-process for
                           EIP-712 signing and buyer-side ECIES decrypt.
                           It is never sent anywhere and never logged.
    ACCESSURA_DELIVERY_SECRET  dedicated 32-byte hex secret used only for
                           seller-side per-signal DEK derivation.
"""

import json
import os
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
    _payment_readiness,
    _sign_bid_authorization,
    _sign_x402_payment,
)

BASE_URL = os.getenv("ACCESSURA_BASE_URL", "https://worldcup-direct-testnet.accessuraportal.com").rstrip("/")
API_KEY = os.getenv("ACCESSURA_API_KEY", "")
TOKEN = os.getenv("ACCESSURA_TOKEN", "")
PRIVATE_KEY = os.getenv("ACCESSURA_PRIVATE_KEY", "")
DELIVERY_SECRET = os.getenv("ACCESSURA_DELIVERY_SECRET", "")


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
    headers = {"User-Agent": "Accessura-MCP/0.5", **_auth_headers(), **(extra_headers or {})}
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

async def list_topics(bucket: str = "", query: str = "", limit: int = 24, page: int = 1) -> dict:
    params: dict[str, Any] = {"limit": limit, "page": page}
    if bucket:
        params["bucket"] = bucket
    if query:
        params["q"] = query
    return await _get("/worldcup/topics", params)


async def list_topic_packs(slug: str, limit: int = 20) -> dict:
    return await _get(f"/worldcup/topics/{_quote(slug)}/packs", {"limit": limit})


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


async def relist_pack(pack_id: str) -> dict:
    return await _post(f"/packs/{_quote(pack_id)}/relist")


async def reopen_signal_settlement(pack_id: str, signal_id: str) -> dict:
    return await _post(
        f"/packs/{_quote(pack_id)}/signals/{_quote(signal_id)}/settlement-readiness")


# ── Signals ───────────────────────────────────────────────────────────────

async def append_signal(pack_id: str, signal_data: dict) -> dict:
    return await _post(f"/packs/{_quote(pack_id)}/signals", signal_data)


# ── Bidding ───────────────────────────────────────────────────────────────

async def place_bid(pack_id: str, bid_data: dict) -> dict:
    if bid_data.get("authorization"):
        return await _post(f"/packs/{_quote(pack_id)}/bid", bid_data)
    signal_id = str(bid_data.get("signal_id") or "")
    price = float(bid_data.get("bid_price"))
    if not signal_id:
        raise RuntimeError("signal_id is required for a direct signed bid")
    from accessura_sdk.client import BuyerAgent
    agent = BuyerAgent(private_key=_require_private_key(), base_url=BASE_URL)
    for attempt in range(2):
        status = await get_bid_status(pack_id, signal_id)
        authorization = _sign_bid_authorization(
            _account(), agent._enc_pub, pack_id, signal_id, price, status)
        code, _, response = await _req_response(
            "POST", f"/packs/{_quote(pack_id)}/bid",
            body={**bid_data, "authorization": authorization})
        if code < 400:
            return response
        if response.get("error_code") != "BID_AUTHORIZATION_MISMATCH" or attempt == 1:
            raise RuntimeError(f"HTTP {code} POST /packs/{pack_id}/bid: {response}")
    raise RuntimeError("unreachable bid retry state")


async def get_bid_status(pack_id: str, signal_id: str = "") -> dict:
    params = {"signal_id": signal_id} if signal_id else None
    return await _get(f"/packs/{_quote(pack_id)}/bid", params)


def payment_readiness(network: str = DEFAULT_X402_NETWORK) -> dict:
    """Local self-custody chain/USDC readiness; never a platform balance."""
    return _payment_readiness(_account(), network)


# ── Claims & Settlement ──────────────────────────────────────────────────

async def settle_auction(pack_id: str, signal_id: str) -> dict:
    return await _post(f"/packs/{_quote(pack_id)}/settle", {"signal_id": signal_id})


async def list_claims(role: str = "buyer") -> dict:
    params = {"role": "seller"} if role == "seller" else None
    return await _get("/claims", params)


async def get_claim_payment(claim_id: str) -> dict:
    status, headers, data = await _req_response(
        "GET", f"/claims/{_quote(claim_id)}/pay")
    return {**data, "_http_status": status,
            "_payment_required": headers.get("payment-required")}


async def pay_claim(claim_id: str) -> dict:
    """Sign x402 with ACCESSURA_PRIVATE_KEY and pay the seller directly."""
    status, _, required = await _req_response(
        "GET", f"/claims/{_quote(claim_id)}/pay")
    if status in (200, 202):
        return {**required, "_http_status": status}
    if status != 402:
        raise RuntimeError(f"HTTP {status} GET /claims/{claim_id}/pay: {required}")
    _, payment_header = _sign_x402_payment(_account(), required)
    paid_status, _, paid = await _req_response(
        "POST", f"/claims/{_quote(claim_id)}/pay", body={},
        extra_headers={"PAYMENT-SIGNATURE": payment_header})
    if paid_status >= 400:
        raise RuntimeError(f"HTTP {paid_status} POST /claims/{claim_id}/pay: {paid}")
    return {**paid, "_http_status": paid_status}


async def fetch_paid_ciphertext(ciphertext_url: str) -> dict:
    if not ciphertext_url.startswith(f"{BASE_URL}/api/v1/"):
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                ciphertext_url,
                # Seller hosts receive no Accessura API key/JWT.
                headers={"User-Agent": "Accessura-MCP/0.5"},
            )
        if response.status_code >= 400:
            raise RuntimeError(f"HTTP {response.status_code} ciphertext fetch: {response.text[:300]}")
        return response.json()
    path = ciphertext_url[len(f"{BASE_URL}/api/v1"):]
    return await _get(path)


async def deliver_key_release(claim_id: str, platform_broker: dict,
                              ciphertext_url: str = "") -> dict:
    body = {"platform_broker": platform_broker}
    if ciphertext_url:
        body["ciphertext_url"] = ciphertext_url
    return await _post(f"/claims/{_quote(claim_id)}/key-release", body)


# ── Wallet ────────────────────────────────────────────────────────────────

# ── Orders & Sales ────────────────────────────────────────────────────────

async def list_orders(limit: int = 20) -> dict:
    return await _get("/orders", {"limit": limit})


async def list_sales(limit: int = 20) -> dict:
    return await _get("/sales", {"limit": limit})


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
    """EIP-712 sign a backend sign_payload with the env private key."""
    from eth_account.messages import encode_typed_data

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
