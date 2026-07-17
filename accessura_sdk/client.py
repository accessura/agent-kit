"""Accessura SDK — full-marketplace Python client for buyer and seller agents.

    pip install httpx cryptography eth-account

    from accessura_sdk import BuyerAgent, SellerAgent

    # Buyer
    agent = BuyerAgent(private_key="0x...")
    agent.register("My Agent")
    agent.login()
    packs = agent.search("Norway")
    agent.bid(pack_id, signal_id, 0.15)

    # Seller
    seller = SellerAgent(private_key="0x...")
    seller.register("My Seller", role="seller")
    seller.publish_pack(title="Hook title", info_type="text", ...)
    seller.append_signal(pack_id, "Signal label", "HOOK summary", ...)
"""

import base64
import json
import os
import secrets
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional, Union

# ── Import canonical ECIES from crypto.py ─────────────────────────────────
from .crypto import (
    ECIES_ALG,
    decrypt_delivery,
    derive_signal_dek,
    encrypt_signal_content,
    normalize_delivery_secret,
    normalize_encryption_pubkey,
    seller_wrap_pre_encrypted_dek,
)


# ═══════════════════════════════════════════════════════════════════════════
# HTTP transport
# ═══════════════════════════════════════════════════════════════════════════

_HTTPX = None

def _get_httpx():
    global _HTTPX
    if _HTTPX is None:
        try:
            import httpx as _h
            _HTTPX = _h
        except ImportError:
            _HTTPX = False
    return _HTTPX


def _request(method: str, url: str, headers: dict,
             json_body: Any = None) -> dict:
    _, _, body = _request_response(method, url, headers, json_body)
    return body


def _request_response(method: str, url: str, headers: dict,
                      json_body: Any = None) -> tuple[int, dict, dict]:
    """Return status, lowercase response headers, and parsed JSON.

    x402 uses HTTP 402 plus PAYMENT-REQUIRED, so callers must retain protocol
    status/headers instead of treating every non-200 response as an exception.
    """
    h = {"Content-Type": "application/json",
         "User-Agent": "Accessura-SDK/0.5", **headers}
    body_bytes = json.dumps(json_body).encode() if json_body is not None else None
    if _get_httpx():
        r = _get_httpx().request(method, url, headers=h, content=body_bytes,
                                 timeout=30.0)
        return r.status_code, dict(r.headers), _maybe_json(r.text)
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, data=body_bytes, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, _maybe_json(raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, _maybe_json(raw)


def _maybe_json(text: str) -> dict:
    try:
        return json.loads(text)
    except Exception:
        return {"_error": text}


def _sig_hex(signature) -> str:
    """Normalize eth_account signature to 0x-prefixed hex."""
    s = signature.hex()
    return s if s.startswith("0x") else "0x" + s


PROTOCOL_DOMAIN = {
    "name": "WorldcupProtocol",
    "version": "1",
    "chainId": 8453,
    "verifyingContract": "0x0000000000000000000000000000000000000000",
}
BASE_MAINNET_CAIP2 = "eip155:8453"
BASE_MAINNET_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
BASE_SEPOLIA_CAIP2 = "eip155:84532"
BASE_SEPOLIA_USDC = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
DEFAULT_X402_NETWORK = BASE_SEPOLIA_CAIP2
X402_CHAIN_PROFILES = {
    BASE_SEPOLIA_CAIP2: {
        "label": "Base Sepolia",
        "asset": BASE_SEPOLIA_USDC,
        "domain_name": "USDC",
        "testnet": True,
    },
    BASE_MAINNET_CAIP2: {
        "label": "Base",
        "asset": BASE_MAINNET_USDC,
        "domain_name": "USD Coin",
        "testnet": False,
    },
}

BID_AUTHORIZATION_TYPES = {
    "BidAuthorization": [
        {"name": "bid_id", "type": "string"},
        {"name": "pack_id", "type": "string"},
        {"name": "signal_id", "type": "string"},
        {"name": "signal_scope", "type": "string"},
        {"name": "price", "type": "string"},
        {"name": "buyer_payment_address", "type": "address"},
        {"name": "buyer_signing_key", "type": "address"},
        {"name": "buyer_encryption_pubkey", "type": "string"},
        {"name": "delegation_id", "type": "string"},
        {"name": "window_id", "type": "string"},
        {"name": "nonce", "type": "string"},
        {"name": "expiry", "type": "string"},
    ],
}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _js_number_string(value: float) -> str:
    """Match JavaScript String(Number(value)) for finite auction prices."""
    number = float(value)
    if not (number == number and number not in (float("inf"), float("-inf"))):
        raise ValueError("bid price must be finite")
    if number == 0:
        return "0"
    raw = repr(number).lower()
    magnitude = abs(number)
    if 1e-6 <= magnitude < 1e21:
        fixed = format(Decimal(raw), "f")
        return fixed.rstrip("0").rstrip(".") if "." in fixed else fixed
    if "e" not in raw:
        raw = format(number, ".15e")
    mantissa, exponent = raw.split("e", 1)
    sign = "+" if not exponent.startswith("-") else "-"
    digits = exponent.lstrip("+-0") or "0"
    return f"{mantissa}e{sign}{digits}"


def _sign_bid_authorization(account, encryption_pubkey: str, pack_id: str,
                            signal_id: str, price: float,
                            round_status: dict) -> dict:
    from eth_account.messages import encode_typed_data

    round_info = round_status.get("round") or round_status.get("window") or {}
    round_id = round_info.get("round_id") or round_info.get("window_id")
    if not round_id:
        raise RuntimeError("bid status did not return a direct round_id")
    expiry = round_info.get("closes_at") or (
        datetime.now(timezone.utc) + timedelta(minutes=1)
    ).isoformat().replace("+00:00", "Z")
    signal_scope = {"mode": "single_signal", "signal_id": signal_id}
    authorization = {
        "bid_id": f"bid_{secrets.token_hex(16)}",
        "pack_id": pack_id,
        "signal_id": signal_id,
        "signal_scope": signal_scope,
        "price": price,
        "buyer_payment_address": account.address,
        "buyer_signing_key": account.address,
        "buyer_encryption_pubkey": encryption_pubkey,
        "delegation_id": "",
        "window_id": round_id,
        "nonce": secrets.token_hex(16),
        "expiry": expiry,
        "domain": PROTOCOL_DOMAIN,
        "signature": "",
    }
    message = {
        **{k: authorization[k] for k in (
            "bid_id", "pack_id", "signal_id", "buyer_payment_address",
            "buyer_signing_key", "buyer_encryption_pubkey", "delegation_id",
            "window_id", "nonce", "expiry",
        )},
        "signal_scope": _canonical_json(signal_scope),
        "price": _js_number_string(price),
    }
    typed = encode_typed_data(PROTOCOL_DOMAIN, BID_AUTHORIZATION_TYPES, message)
    authorization["signature"] = _sig_hex(account.sign_message(typed).signature)
    return authorization


def _sign_x402_payment(account, payment_required: dict) -> tuple[dict, str]:
    """Build x402 v2 exact EVM payload and PAYMENT-SIGNATURE header.

    Base USDC implements EIP-3009 TransferWithAuthorization. The buyer signs
    locally; only the payload is returned to the HTTP layer.
    """
    from eth_account.messages import encode_typed_data

    if payment_required.get("x402Version") != 2:
        raise RuntimeError("PAYMENT-REQUIRED must use x402Version 2")
    accepts = payment_required.get("accepts")
    if not isinstance(accepts, list) or len(accepts) != 1:
        raise RuntimeError("PAYMENT-REQUIRED must contain exactly one payment offer")
    accepted = accepts[0]
    resource = payment_required.get("resource")
    if not isinstance(accepted, dict) or not isinstance(resource, dict):
        raise RuntimeError("PAYMENT-REQUIRED did not include an exact EVM offer")
    network = str(accepted.get("network", ""))
    if accepted.get("scheme") != "exact":
        raise RuntimeError(f"unsupported x402 scheme: {accepted.get('scheme')}")
    profile = X402_CHAIN_PROFILES.get(network)
    if profile is None:
        raise RuntimeError(f"unsupported x402 network: {network}")
    if str(accepted.get("asset", "")).lower() != str(profile["asset"]).lower():
        raise RuntimeError(f"x402 offer does not use configured {profile['label']} USDC")
    extra = accepted.get("extra")
    expected_extra = {"name": profile["domain_name"], "version": "2"}
    if extra != expected_extra:
        raise RuntimeError(f"x402 offer has an unexpected {profile['label']} USDC EIP-712 domain")
    amount = str(accepted.get("amount", ""))
    if not amount.isdigit() or int(amount) <= 0:
        raise RuntimeError("x402 amount must be a positive integer in USDC base units")
    max_timeout = int(accepted.get("maxTimeoutSeconds", 60))
    if max_timeout <= 0:
        raise RuntimeError("x402 maxTimeoutSeconds must be positive")
    valid_before = int(time.time()) + max(1, min(max_timeout, 55))
    nonce = "0x" + secrets.token_hex(32)
    authorization = {
        "from": account.address,
        "to": accepted["payTo"],
        "value": amount,
        "validAfter": "0",
        "validBefore": str(valid_before),
        "nonce": nonce,
    }
    domain = {
        "name": extra["name"],
        "version": extra["version"],
        "chainId": int(network.split(":", 1)[1]),
        "verifyingContract": accepted["asset"],
    }
    types = {
        "TransferWithAuthorization": [
            {"name": "from", "type": "address"},
            {"name": "to", "type": "address"},
            {"name": "value", "type": "uint256"},
            {"name": "validAfter", "type": "uint256"},
            {"name": "validBefore", "type": "uint256"},
            {"name": "nonce", "type": "bytes32"},
        ],
    }
    typed_message = {
        **authorization,
        "value": int(authorization["value"]),
        "validAfter": 0,
        "validBefore": valid_before,
    }
    typed = encode_typed_data(domain, types, typed_message)
    signature = _sig_hex(account.sign_message(typed).signature)
    payload = {
        "x402Version": 2,
        "resource": resource,
        "accepted": accepted,
        "payload": {"signature": signature, "authorization": authorization},
    }
    header = base64.b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    return payload, header


def _payment_readiness(account, network: str = DEFAULT_X402_NETWORK) -> dict:
    """Describe the local self-custody payment binding without a platform balance."""
    profile = X402_CHAIN_PROFILES.get(network)
    if profile is None:
        raise ValueError(f"unsupported payment network: {network}")
    return {
        "signing_ready": True,
        "payment_ready": None,
        "balance_status": "not_checked",
        "payment_address": account.address,
        "network": network,
        "chain": profile["label"],
        "usdc_contract": profile["asset"],
        "testnet": profile["testnet"],
        "custody": "self_custody",
        "platform_balance": None,
    }


# ═══════════════════════════════════════════════════════════════════════════
# BuyerAgent (EIP-712 self-custody)
# ═══════════════════════════════════════════════════════════════════════════

class BuyerAgent:
    """Secp256k1-keyed buyer agent. EIP-712 auth, full trading lifecycle."""

    def __init__(self, private_key: str,
                 base_url: str = "https://worldcup-direct-testnet.accessuraportal.com"):
        self.base_url = base_url.rstrip("/")
        self.api = f"{self.base_url}/api/v1"
        self.private_key = private_key

        from eth_account.account import Account
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives import serialization

        self._account = Account.from_key(private_key)
        self.agent_id = self._account.address
        self._enc_priv = ec.derive_private_key(
            int(private_key, 16), ec.SECP256K1())
        self._enc_pub = "0x" + self._enc_priv.public_key().public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint).hex()
        self._token: Optional[str] = None
        self._api_key: Optional[str] = None

    def _auth(self) -> dict:
        h = {}
        if self._api_key:
            h["Authorization"] = f"ApiKey {self._api_key}"
        elif self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    def _bearer_auth(self) -> dict:
        """Auth for the claim-list route, which is intentionally JWT-only."""
        if not self._token:
            raise RuntimeError("Bearer token required; run login() or get_api_key() first")
        return {"Authorization": f"Bearer {self._token}"}

    # ── auth ──────────────────────────────────────────────────────────

    def register(self, name: str = "Accessura Agent",
                 role: str = "buyer") -> bool:
        from eth_account.messages import encode_typed_data

        try:
            existing = _request(
                "GET",
                f"{self.api}/agents/identity?agent_id={urllib.parse.quote(self.agent_id)}&keys=1",
                {})
        except Exception:
            existing = {}
        if (existing.get("identity") or {}).get("signing_key"):
            return True

        domain = {"name": "WorldcupProtocol", "version": "1", "chainId": 8453,
                  "verifyingContract": "0x0000000000000000000000000000000000000000"}
        msg = {"agent_id": self.agent_id, "payment_address": self.agent_id,
               "encryption_pubkey": self._enc_pub}
        typed = encode_typed_data(domain, {
            "IdentityRegistration": [
                {"name": "agent_id", "type": "string"},
                {"name": "payment_address", "type": "string"},
                {"name": "encryption_pubkey", "type": "string"},
            ]}, msg)
        sig = _sig_hex(self._account.sign_message(typed).signature)
        post_error = None
        try:
            r = _request("POST", f"{self.api}/agents/identity", {}, {
                "action": "register_identity", "agent_id": self.agent_id,
                "agent_name": name, "role": role,
                "payment_address": self.agent_id, "signing_key": self.agent_id,
                "encryption_pubkey": self._enc_pub, "signature": sig,
            })
        except Exception as exc:
            post_error = exc
            r = {}
        if r.get("ok"):
            return True
        try:
            retry = _request("GET",
                f"{self.api}/agents/identity?agent_id={urllib.parse.quote(self.agent_id)}&keys=1",
                {})
        except Exception:
            retry = {}
        signing_key = (retry.get("identity") or {}).get("signing_key", "")
        if signing_key.lower() == self.agent_id.lower():
            return True
        if post_error is not None:
            raise RuntimeError(
                f"register_identity failed: {post_error}") from post_error
        raise RuntimeError(
            f"register_identity failed: {r.get('error') or r}")

    def login(self):
        from eth_account.messages import encode_typed_data

        r = _request("POST", f"{self.api}/auth/token", {},
                     {"agent_id": self.agent_id, "action": "challenge"})
        ch = r.get("challenge")
        if not ch:
            raise RuntimeError(
                f"auth challenge failed (did register() succeed?): {r.get('error') or r}")
        payload = ch["sign_payload"]
        types = {payload["primaryType"]: [
            {"name": f["name"], "type": f["type"]}
            for f in payload["types"][payload["primaryType"]]]}
        typed = encode_typed_data(
            payload["domain"], types, payload["message"])
        sig = _sig_hex(self._account.sign_message(typed).signature)
        r2 = _request("POST", f"{self.api}/auth/token", {},
                      {"agent_id": self.agent_id,
                       "challenge_id": ch["challenge_id"],
                       "signature": sig})
        if not r2.get("token"):
            raise RuntimeError(
                f"token exchange failed: {r2.get('error') or r2}")
        self._token = r2["token"]

    def get_api_key(self) -> str:
        """Challenge → sign → exchange. Returns 'acc_...' and stores it."""
        from eth_account.messages import encode_typed_data

        r = _request("POST", f"{self.api}/auth/apikey", {},
                     {"agent_id": self.agent_id, "action": "challenge"})
        ch = r.get("challenge")
        if not ch:
            raise RuntimeError(
                f"apikey challenge failed: {r.get('error') or r}")
        payload = ch["sign_payload"]
        types = {payload["primaryType"]: [
            {"name": f["name"], "type": f["type"]}
            for f in payload["types"][payload["primaryType"]]]}
        typed = encode_typed_data(
            payload["domain"], types, payload["message"])
        sig = _sig_hex(self._account.sign_message(typed).signature)
        out = _request("POST", f"{self.api}/auth/apikey", {},
                       {"agent_id": self.agent_id,
                        "challenge_id": ch["challenge_id"],
                        "signature": sig, "action": "exchange"})
        api_key = out.get("api_key")
        if not api_key:
            raise RuntimeError(
                f"apikey exchange failed: {out.get('error') or out}")
        self._api_key = api_key
        if out.get("token"):
            self._token = out["token"]
        return api_key

    # ── discovery ──────────────────────────────────────────────────────

    def list_topics(self, bucket: str = "", query: str = "",
                    limit: int = 24, page: int = 1) -> dict:
        params = f"limit={limit}&page={page}"
        if bucket:
            params += f"&bucket={urllib.parse.quote(bucket)}"
        if query:
            params += f"&q={urllib.parse.quote(query)}"
        return _request("GET", f"{self.api}/worldcup/topics?{params}", {})

    def list_topic_packs(self, slug: str, limit: int = 20) -> dict:
        return _request(
            "GET",
            f"{self.api}/worldcup/topics/{urllib.parse.quote(slug)}/packs?limit={limit}",
            {})

    def get_catalog(self) -> dict:
        return _request("GET", f"{self.api}/catalog", {})

    def get_leaderboard(self, limit: int = 20) -> dict:
        return _request("GET",
                        f"{self.api}/leaderboard?limit={limit}", {})

    def search(self, query: str, limit: int = 20,
               topic_slug: str = "", info_type: str = "",
               sort: str = "recency") -> list[dict]:
        params = [f"limit={limit}", f"sort={sort}"]
        if query:
            params.append(f"q={urllib.parse.quote(query)}")
        if topic_slug:
            params.append(f"topic_slug={urllib.parse.quote(topic_slug)}")
        if info_type:
            params.append(f"info_type={info_type}")
        return _request(
            "GET", f"{self.api}/packs?{'&'.join(params)}",
            self._auth()).get("packs", [])

    def get_pack(self, pack_id: str) -> dict:
        r = _request("GET",
                     f"{self.api}/packs/{pack_id}", self._auth())
        return r.get("pack", r)

    def list_packs(self, topic_slug: str = "",
                   limit: int = 20) -> list[dict]:
        params = [f"limit={limit}"]
        if topic_slug:
            params.append(f"topic_slug={urllib.parse.quote(topic_slug)}")
        return _request(
            "GET", f"{self.api}/packs?{'&'.join(params)}",
            self._auth()).get("packs", [])

    # ── bidding + settlement ───────────────────────────────────────────

    def bid(self, pack_id: str, signal_id: str, price: float) -> dict:
        """Sign and submit one self-custodied bid for the current direct round."""
        for attempt in range(2):
            status = self.get_bid_status(pack_id, signal_id)
            authorization = _sign_bid_authorization(
                self._account, self._enc_pub, pack_id, signal_id, price, status)
            response = _request(
                "POST", f"{self.api}/packs/{pack_id}/bid", self._auth(),
                {"bid_price": price, "signal_id": signal_id,
                 "authorization": authorization})
            if response.get("error_code") != "BID_AUTHORIZATION_MISMATCH" or attempt == 1:
                return response
        raise RuntimeError("unreachable bid retry state")

    def get_bid_status(self, pack_id: str,
                       signal_id: str = "") -> dict:
        params = ""
        if signal_id:
            params = f"?signal_id={urllib.parse.quote(signal_id)}"
        return _request(
            "GET", f"{self.api}/packs/{pack_id}/bid{params}",
            self._auth())

    def settle(self, pack_id: str, signal_id: str = "") -> dict:
        return _request(
            "POST", f"{self.api}/packs/{pack_id}/settle",
            self._auth(), {"signal_id": signal_id})

    def payment_readiness(self, network: str = DEFAULT_X402_NETWORK) -> dict:
        """Return local chain/USDC signing readiness; no platform balance exists."""
        return _payment_readiness(self._account, network)

    # ── claims + delivery ──────────────────────────────────────────────

    def get_claims(self) -> list[dict]:
        return _request(
            "GET", f"{self.api}/claims", self._bearer_auth()).get("claims", [])

    def get_payment(self, claim_id: str) -> dict:
        """Read pending delivery, PAYMENT-REQUIRED, or paid delivery state."""
        status, headers, body = _request_response(
            "GET", f"{self.api}/claims/{urllib.parse.quote(claim_id)}/pay",
            self._auth())
        return {**body, "_http_status": status,
                "_payment_required": headers.get("payment-required")}

    def pay_claim(self, claim_id: str) -> dict:
        """Sign x402 locally and pay USDC directly to the claim seller."""
        status, _, required = _request_response(
            "GET", f"{self.api}/claims/{urllib.parse.quote(claim_id)}/pay",
            self._auth())
        if status == 200 or status == 202:
            return {**required, "_http_status": status}
        if status != 402:
            return {**required, "_http_status": status}
        _, payment_header = _sign_x402_payment(self._account, required)
        paid_status, _, paid = _request_response(
            "POST", f"{self.api}/claims/{urllib.parse.quote(claim_id)}/pay",
            {**self._auth(), "PAYMENT-SIGNATURE": payment_header}, {})
        if paid_status >= 400:
            raise RuntimeError(f"x402 payment failed: HTTP {paid_status} {paid}")
        return {**paid, "_http_status": paid_status}

    def fetch_paid_ciphertext(self, delivery: dict) -> str:
        """Fetch opaque ciphertext from a paid direct-delivery response."""
        url = delivery.get("ciphertext_url")
        if not isinstance(url, str) or not url:
            raise RuntimeError("paid delivery did not include ciphertext_url")
        target = urllib.parse.urlsplit(url)
        api_origin = urllib.parse.urlsplit(self.api)
        same_origin = (target.scheme, target.netloc) == (api_origin.scheme, api_origin.netloc)
        # Accessura credentials must never be forwarded to a seller host.
        status, _, body = _request_response("GET", url, self._auth() if same_origin else {})
        if status != 200 or not isinstance(body.get("ciphertext_b64"), str):
            raise RuntimeError(f"ciphertext fetch failed: HTTP {status} {body}")
        return body["ciphertext_b64"]

    def decrypt_paid_claim(self, claim_id: str) -> bytes:
        """Retrieve an already-paid delivery and decrypt it locally."""
        delivery = self.get_payment(claim_id)
        if delivery.get("_http_status") != 200:
            raise RuntimeError(f"claim is not paid_delivered: {delivery}")
        broker = delivery.get("platform_broker")
        if not isinstance(broker, dict):
            raise RuntimeError("paid delivery did not include platform_broker")
        return self.decrypt(broker, self.fetch_paid_ciphertext(delivery))

    def decrypt(self, broker: dict, ciphertext_b64: str) -> bytes:
        return decrypt_delivery(broker, ciphertext_b64, self.private_key)

    # ── wallet ────────────────────────────────────────────────────────

    # ── orders ────────────────────────────────────────────────────────

    def list_orders(self, limit: int = 20) -> dict:
        return _request(
            "GET", f"{self.api}/orders?limit={limit}", self._auth())


# ═══════════════════════════════════════════════════════════════════════════
# SellerAgent (EIP-712 self-custody seller)
# ═══════════════════════════════════════════════════════════════════════════

class SellerAgent:
    """Secp256k1-keyed seller agent. EIP-712 auth, publish + deliver."""

    def __init__(self, private_key: str,
                 base_url: str = "https://worldcup-direct-testnet.accessuraportal.com",
                 delivery_secret: Optional[Union[str, bytes]] = None):
        self.base_url = base_url.rstrip("/")
        self.api = f"{self.base_url}/api/v1"
        self.private_key = private_key
        self.delivery_secret = delivery_secret or os.getenv("ACCESSURA_DELIVERY_SECRET", "")

        from eth_account.account import Account
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives import serialization

        self._account = Account.from_key(private_key)
        self.agent_id = self._account.address
        self._enc_priv = ec.derive_private_key(
            int(private_key, 16), ec.SECP256K1())
        self._enc_pub = "0x" + self._enc_priv.public_key().public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint).hex()
        self._token: Optional[str] = None
        self._api_key: Optional[str] = None

    def _auth(self) -> dict:
        h = {}
        if self._api_key:
            h["Authorization"] = f"ApiKey {self._api_key}"
        elif self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    def _bearer_auth(self) -> dict:
        """Auth for the claim-list route, which is intentionally JWT-only."""
        if not self._token:
            raise RuntimeError("Bearer token required; run get_api_key() first")
        return {"Authorization": f"Bearer {self._token}"}

    def _require_delivery_secret(self) -> bytes:
        if not self.delivery_secret:
            raise RuntimeError(
                "managed seller encryption requires a dedicated 32-byte "
                "ACCESSURA_DELIVERY_SECRET; never reuse the wallet private key"
            )
        secret = normalize_delivery_secret(self.delivery_secret)
        wallet_hex = self.private_key[2:] if self.private_key.lower().startswith("0x") else self.private_key
        if secret == bytes.fromhex(wallet_hex):
            raise RuntimeError("ACCESSURA_DELIVERY_SECRET must not equal the wallet private key")
        return secret

    # ── auth ──────────────────────────────────────────────────────────

    def register(self, name: str = "Accessura Seller",
                 role: str = "seller") -> bool:
        """Register as seller (same EIP-712 flow as buyer)."""
        from eth_account.messages import encode_typed_data

        try:
            existing = _request("GET",
                f"{self.api}/agents/identity?agent_id={urllib.parse.quote(self.agent_id)}&keys=1",
                {})
        except Exception:
            existing = {}
        if (existing.get("identity") or {}).get("signing_key"):
            return True

        domain = {"name": "WorldcupProtocol", "version": "1", "chainId": 8453,
                  "verifyingContract": "0x0000000000000000000000000000000000000000"}
        msg = {"agent_id": self.agent_id,
               "payment_address": self.agent_id,
               "encryption_pubkey": self._enc_pub}
        typed = encode_typed_data(domain, {
            "IdentityRegistration": [
                {"name": "agent_id", "type": "string"},
                {"name": "payment_address", "type": "string"},
                {"name": "encryption_pubkey", "type": "string"},
            ]}, msg)
        sig = _sig_hex(self._account.sign_message(typed).signature)
        post_error = None
        try:
            r = _request("POST", f"{self.api}/agents/identity", {}, {
                "action": "register_identity", "agent_id": self.agent_id,
                "agent_name": name, "role": role,
                "payment_address": self.agent_id,
                "signing_key": self.agent_id,
                "encryption_pubkey": self._enc_pub, "signature": sig,
            })
        except Exception as exc:
            post_error = exc
            r = {}
        if r.get("ok"):
            return True
        try:
            retry = _request("GET",
                f"{self.api}/agents/identity?agent_id={urllib.parse.quote(self.agent_id)}&keys=1",
                {})
        except Exception:
            retry = {}
        signing_key = (retry.get("identity") or {}).get("signing_key", "")
        if signing_key.lower() == self.agent_id.lower():
            return True
        if post_error is not None:
            raise RuntimeError(
                f"register_identity failed: {post_error}") from post_error
        raise RuntimeError(
            f"register_identity failed: {r.get('error') or r}")

    def get_api_key(self) -> str:
        """Challenge → sign → exchange."""
        from eth_account.messages import encode_typed_data

        r = _request("POST", f"{self.api}/auth/apikey", {},
                     {"agent_id": self.agent_id, "action": "challenge"})
        ch = r.get("challenge")
        if not ch:
            raise RuntimeError(
                f"apikey challenge failed: {r.get('error') or r}")
        payload = ch["sign_payload"]
        types = {payload["primaryType"]: [
            {"name": f["name"], "type": f["type"]}
            for f in payload["types"][payload["primaryType"]]]}
        typed = encode_typed_data(
            payload["domain"], types, payload["message"])
        sig = _sig_hex(self._account.sign_message(typed).signature)
        out = _request("POST", f"{self.api}/auth/apikey", {},
                       {"agent_id": self.agent_id,
                        "challenge_id": ch["challenge_id"],
                        "signature": sig, "action": "exchange"})
        api_key = out.get("api_key")
        if not api_key:
            raise RuntimeError(
                f"apikey exchange failed: {out.get('error') or out}")
        self._api_key = api_key
        if out.get("token"):
            self._token = out["token"]
        return api_key

    # ── publishing ────────────────────────────────────────────────────

    def bind_payout_wallet(self, chain: str = DEFAULT_X402_NETWORK) -> dict:
        """Prove and bind this seller's self-custodied direct-payment wallet."""
        challenge_result = _request(
            "POST", f"{self.api}/sellers/payout-wallet/challenge", self._auth(),
            {"payout_address": self._account.address, "chain": chain})
        challenge = challenge_result.get("challenge") or {}
        payload = challenge.get("sign_payload")
        if not payload:
            raise RuntimeError(f"seller payout challenge failed: {challenge_result}")
        from eth_account.messages import encode_typed_data
        primary = payload["primaryType"]
        typed = encode_typed_data(
            payload["domain"], {primary: payload["types"][primary]}, payload["message"])
        signature = _sig_hex(self._account.sign_message(typed).signature)
        return _request(
            "POST", f"{self.api}/sellers/payout-wallet/verify", self._auth(),
            {"challenge_id": challenge["challenge_id"], "signature": signature})

    def payment_readiness(self, network: str = DEFAULT_X402_NETWORK) -> dict:
        """Return local payout chain/USDC readiness; no platform balance exists."""
        return _payment_readiness(self._account, network)

    def publish_pack(self, title: str, info_type: str, **kwargs) -> dict:
        """Publish a new data pack. DO NOT include signals — append separately."""
        body = {"title": title, "info_type": info_type, **kwargs}
        return _request("POST", f"{self.api}/packs", self._auth(), body)

    def delist_pack(self, pack_id: str) -> dict:
        return _request(
            "POST", f"{self.api}/packs/{pack_id}/delist", self._auth())

    def relist_pack(self, pack_id: str) -> dict:
        return _request(
            "POST", f"{self.api}/packs/{pack_id}/relist", self._auth())

    def reopen_signal_settlement(self, pack_id: str, signal_id: str) -> dict:
        """Explicitly reopen one signal after restoring seller readiness."""
        return _request(
            "POST",
            f"{self.api}/packs/{urllib.parse.quote(pack_id)}/signals/"
            f"{urllib.parse.quote(signal_id)}/settlement-readiness",
            self._auth(), {})

    # ── signals ───────────────────────────────────────────────────────

    def append_signal(self, pack_id: str, label: str, summary: str,
                      content_text: str = "",
                      content_b64: str = "",
                      source: str = "",
                      observed_at: str = "",
                      payload: Optional[dict] = None,
                      encrypt_with_managed: bool = False) -> dict:
        """Append a signal to a pack.

        If encrypt_with_managed=True and content_text is given, the content is
        AES-256-GCM encrypted in-process with a deterministic per-signal DEK
        (HKDF from the dedicated delivery secret). The same DEK can be re-derived later
        by deliver_key_release()."""
        signal: dict[str, Any] = {"label": label, "summary": summary}
        if payload is not None:
            signal["payload"] = payload
        if source:
            signal["source"] = source
        if observed_at:
            signal["observed_at"] = observed_at

        # Auto-generate a signal ID if none provided — MUST happen before
        # encrypt_with_managed so the DEK is derived with the real signal_id.
        import time, secrets
        signal.setdefault("id",
                          f"sig-{int(time.time() * 1000)}-{secrets.token_hex(4)}")

        if encrypt_with_managed and content_text:
            signal["content_b64"] = encrypt_signal_content(
                content_text.encode("utf-8"), self._require_delivery_secret(),
                pack_id, signal["id"])
        elif content_b64:
            signal["content_b64"] = content_b64

        result = _request(
            "POST", f"{self.api}/packs/{pack_id}/signals",
            self._auth(), signal)
        # The platform intentionally does not expose unpaid ciphertext through
        # claim reads. Return the ciphertext generated in this Seller process so
        # callers can persist it locally and bind the later key-release envelope
        # without attempting a platform readback.
        if signal.get("content_b64"):
            result = dict(result)
            result["_local_signal_id"] = signal["id"]
            result["_local_ciphertext_b64"] = signal["content_b64"]
        return result

    # ── claims + delivery ─────────────────────────────────────────────

    def list_claims(self) -> list[dict]:
        """Get pending deliveries (seller view)."""
        return _request(
            "GET", f"{self.api}/claims?role=seller",
            self._bearer_auth()).get("deliveries", [])

    def deliver_key_release(self, claim_id: str,
                            buyer_pubkey_hex: str,
                            ciphertext_b64: str,
                            pack_id: str = "",
                            signal_id: str = "",
                            **kwargs) -> dict:
        """Wrap DEK to buyer's pubkey and POST key-release envelope.

        Uses deterministic DEK derivation (HKDF from the delivery secret) if
        encrypt_with_managed was used during signal append."""
        dek = kwargs.get("dek")
        if dek is None and pack_id and signal_id:
            dek = derive_signal_dek(
                self._require_delivery_secret(), pack_id, signal_id)
        if dek is None:
            raise ValueError(
                "dek required (or provide pack_id+signal_id for managed DEK)")
        if isinstance(dek, str):
            dek = bytes.fromhex(dek[2:] if dek.startswith("0x") else dek)

        buyer_agent_id = kwargs.get("buyer_agent_id")
        if not buyer_agent_id:
            raise ValueError("buyer_agent_id required for claim-bound delivery")
        if kwargs.get("aad") is not None or kwargs.get("wrap_aad") is not None:
            raise ValueError(
                "deliver_key_release derives AAD binding internally; do not pass aad or wrap_aad")

        envelope = seller_wrap_pre_encrypted_dek(
            dek, buyer_pubkey_hex, ciphertext_b64,
            claim_id, buyer_agent_id)
        return _request(
            "POST", f"{self.api}/claims/{claim_id}/key-release",
            self._auth(), {
                "platform_broker": envelope,
                **({"ciphertext_url": kwargs["ciphertext_url"]}
                   if kwargs.get("ciphertext_url") else {}),
            })

    # ── sales ─────────────────────────────────────────────────────────

    def list_sales(self, limit: int = 20) -> dict:
        return _request(
            "GET", f"{self.api}/sales?limit={limit}", self._auth())

    # ── wallet ────────────────────────────────────────────────────────

# ═══════════════════════════════════════════════════════════════════════════
# HumanBuyer (email/password)
# ═══════════════════════════════════════════════════════════════════════════

class HumanBuyer:
    """Removed compatibility shell; launch Buyers must use signed BuyerAgent."""

    def __init__(self, agent_id: str, email: str, password: str,
                 base_url: str = "https://worldcup-direct-testnet.accessuraportal.com"):
        self.base_url = base_url.rstrip("/")
        self.api = f"{self.base_url}/api/v1"
        self.agent_id = agent_id
        self._email = email
        self._password = password
        self._token: Optional[str] = None

    def _auth(self) -> dict:
        h = {}
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    def register(self, name: str = "") -> bool:
        del name
        raise RuntimeError(
            "Buyer is Agent-only; migrate to a locally signed BuyerAgent")

    def login(self):
        raise RuntimeError(
            "Buyer is Agent-only; migrate to a locally signed BuyerAgent")

    def search(self, query: str, limit: int = 20,
               info_type: str = "") -> list[dict]:
        path = f"/packs?q={urllib.parse.quote(query)}&limit={limit}"
        if info_type:
            path += f"&info_type={info_type}"
        return _request(
            "GET", f"{self.api}{path}", self._auth()).get("packs", [])

    def get_pack(self, pack_id: str) -> dict:
        r = _request(
            "GET", f"{self.api}/packs/{pack_id}", self._auth())
        return r.get("pack", r)

    def list_packs(self, topic_slug: str = "",
                   limit: int = 20) -> list[dict]:
        params = [f"limit={limit}"]
        if topic_slug:
            params.append(f"topic_slug={urllib.parse.quote(topic_slug)}")
        return _request(
            "GET", f"{self.api}/packs?{'&'.join(params)}",
            self._auth()).get("packs", [])

    def bid(self, pack_id: str, signal_id: str,
            price: float) -> dict:
        raise RuntimeError(
            "direct mainnet bidding requires a locally signed BuyerAgent; "
            "HumanBuyer cannot place unsigned bids")

    def settle(self, pack_id: str, signal_id: str = "") -> dict:
        return _request(
            "POST", f"{self.api}/packs/{pack_id}/settle",
            self._auth(), {"signal_id": signal_id})

    def list_topics(self, bucket: str = "", query: str = "",
                    limit: int = 24) -> dict:
        params = f"limit={limit}"
        if bucket:
            params += f"&bucket={urllib.parse.quote(bucket)}"
        if query:
            params += f"&q={urllib.parse.quote(query)}"
        return _request(
            "GET", f"{self.api}/worldcup/topics?{params}", {})

    def get_catalog(self) -> dict:
        return _request("GET", f"{self.api}/catalog", {})
