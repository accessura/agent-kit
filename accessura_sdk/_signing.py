"""EIP-712 signing helpers, protocol constants, and x402 payment signing.

All signing functions are pure (no env IO except _mainnet_allowed / _assert_network_allowed)
so they can be shared between the sync SDK and async MCP wrapper.
"""

import hashlib
import json
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

from .errors import (
    AccessuraAuthError,
    AccessuraErrorCode,
    AccessuraPaymentError,
)

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
LONG_SELLER_DELIVERY_SLA_WARNING_SECONDS = 3_600

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
        {"name": "payment_authorization_fingerprint", "type": "string"},
    ],
}


# ── Signing-safety guards ─────────────────────────────────────────────────

AUTH_CHALLENGE_PRIMARY_TYPES = frozenset({
    "AuthChallenge",
    "IdentityRegistration",
    "SellerPayoutBinding",
})


def _assert_safe_auth_challenge(payload: Any) -> None:
    """Refuse to blind-sign a backend-supplied EIP-712 payload unless it is a
    known Accessura auth challenge under the null-contract WorldcupProtocol
    domain.

    Auth signing and payment signing use the same wallet key. Without this
    guard a compromised/spoofed backend, a MITM, or a hostile ACCESSURA_BASE_URL
    could return an EIP-3009 USDC ``TransferWithAuthorization`` (USD Coin token
    domain) in place of an auth challenge and coax the wallet into signing a real
    money movement outside the dedicated payment-signing paths. Only the
    binding-bid signer (or legacy ``claims_pay`` compatibility path) may
    authorize a transfer.
    """
    if not isinstance(payload, dict):
        raise AccessuraAuthError(
            "auth challenge payload must be an object",
            code=AccessuraErrorCode.BLIND_SIGNING_REFUSED)
    domain = payload.get("domain")
    if not isinstance(domain, dict):
        raise AccessuraAuthError(
            "auth challenge payload missing an EIP-712 domain",
            code=AccessuraErrorCode.BLIND_SIGNING_REFUSED)
    if domain.get("name") != PROTOCOL_DOMAIN["name"]:
        raise AccessuraAuthError(
            "refusing to sign auth challenge: unexpected EIP-712 domain name "
            f"{domain.get('name')!r} (expected {PROTOCOL_DOMAIN['name']!r})",
            code=AccessuraErrorCode.BLIND_SIGNING_REFUSED)
    if str(domain.get("verifyingContract", "")).lower() != PROTOCOL_DOMAIN["verifyingContract"]:
        raise AccessuraAuthError(
            "refusing to sign auth challenge: non-null verifyingContract "
            f"{domain.get('verifyingContract')!r} (possible value-transfer "
            "authorization disguised as an auth challenge)",
            code=AccessuraErrorCode.BLIND_SIGNING_REFUSED)
    primary = payload.get("primaryType")
    if primary not in AUTH_CHALLENGE_PRIMARY_TYPES:
        raise AccessuraAuthError(
            f"refusing to sign auth challenge: primaryType {primary!r} is not an "
            f"Accessura auth type {sorted(AUTH_CHALLENGE_PRIMARY_TYPES)}",
            code=AccessuraErrorCode.BLIND_SIGNING_REFUSED)


def _sign_auth_challenge(account, payload: dict) -> str:
    """Validate a backend auth challenge is safe, then EIP-712 sign it."""
    from eth_account.messages import encode_typed_data

    _assert_safe_auth_challenge(payload)
    primary = payload["primaryType"]
    types = {primary: [
        {"name": f["name"], "type": f["type"]}
        for f in payload["types"][primary]
    ]}
    typed = encode_typed_data(payload["domain"], types, payload["message"])
    return _sig_hex(account.sign_message(typed).signature)


def _mainnet_allowed() -> bool:
    return os.getenv("ACCESSURA_ALLOW_MAINNET", "").strip().lower() in (
        "1", "true", "yes", "on")


def _active_payment_network() -> str:
    """Select the deployment payment network for pre-bid control checks."""
    return BASE_MAINNET_CAIP2 if _mainnet_allowed() else DEFAULT_X402_NETWORK


def _assert_network_allowed(network: str) -> None:
    """Base mainnet stays closed until the promotion gates pass (plan §4.12)."""
    if network == BASE_MAINNET_CAIP2 and not _mainnet_allowed():
        raise AccessuraPaymentError(
            "Base mainnet (eip155:8453) is closed for this release; the active "
            "target is Base Sepolia (eip155:84532). Set ACCESSURA_ALLOW_MAINNET=1 "
            "only after the deployment promotion gates pass.",
            code=AccessuraErrorCode.MAINNET_GATED)

def _usdc_price_base_units(price: float) -> int:
    return _parse_usdc_limit(str(price), "bid_price")


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
                            round_status: dict,
                            payment_authorization_fingerprint: str) -> dict:
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
        "payment_authorization_fingerprint":
            payment_authorization_fingerprint,
        "domain": PROTOCOL_DOMAIN,
        "signature": "",
    }
    message = {
        **{k: authorization[k] for k in (
            "bid_id", "pack_id", "signal_id", "buyer_payment_address",
            "buyer_signing_key", "buyer_encryption_pubkey", "delegation_id",
            "window_id", "nonce", "expiry",
            "payment_authorization_fingerprint",
        )},
        "signal_scope": _canonical_json(signal_scope),
        "price": _js_number_string(price),
    }
    typed = encode_typed_data(PROTOCOL_DOMAIN, BID_AUTHORIZATION_TYPES, message)
    authorization["signature"] = _sig_hex(account.sign_message(typed).signature)
    return authorization


def _sign_bid_payment_authorization(
        account, payment_terms: dict, amount_base_units: int, *,
        payment_controls: Optional[dict] = None) -> tuple[dict, str]:
    """Pre-sign the compact EIP-3009 authorization carried by a binding bid."""
    from eth_account.messages import encode_typed_data

    if not isinstance(payment_terms, dict):
        raise AccessuraPaymentError(
            "bid status did not return payment_terms",
            code=AccessuraErrorCode.PAYMENT_TERMS_MISSING)
    _binding_bid_sla_risk_warnings(payment_terms)
    network = str(payment_terms.get("network", ""))
    profile = X402_CHAIN_PROFILES.get(network)
    if profile is None:
        raise AccessuraPaymentError(
            f"unsupported binding-bid payment network: {network}",
            code=AccessuraErrorCode.PAYMENT_NETWORK_UNSUPPORTED)
    _assert_network_allowed(network)
    if payment_terms.get("scheme") != "exact":
        raise AccessuraPaymentError(
            "binding-bid payment terms must use exact settlement",
            code=AccessuraErrorCode.BID_TERMS_INVALID)
    asset = str(payment_terms.get("asset", ""))
    if asset.lower() != str(profile["asset"]).lower():
        raise AccessuraPaymentError(
            f"binding-bid terms do not use configured {profile['label']} USDC",
            code=AccessuraErrorCode.PAYMENT_ASSET_MISMATCH)
    token_domain = payment_terms.get("token_domain")
    expected_domain = {"name": profile["domain_name"], "version": "2"}
    if token_domain != expected_domain:
        raise AccessuraPaymentError(
            "binding-bid terms have an unexpected USDC EIP-712 domain",
            code=AccessuraErrorCode.PAYMENT_DOMAIN_MISMATCH)
    pay_to = payment_terms.get("pay_to")
    if not isinstance(pay_to, str) or not pay_to:
        raise AccessuraPaymentError(
            "binding-bid payment terms are missing pay_to",
            code=AccessuraErrorCode.BID_TERMS_INVALID)
    if (
        payment_terms.get("payment_trigger") != "seller_delivery_ready" or
        payment_terms.get("settlement_rule") != "top_n_pay_as_bid"
    ):
        raise AccessuraPaymentError(
            "binding-bid payment trigger or settlement rule is invalid",
            code=AccessuraErrorCode.BID_TERMS_INVALID)
    minimum = str(payment_terms.get("authorization_valid_before_min", ""))
    maximum = str(payment_terms.get("authorization_valid_before_max", ""))
    if (
        not minimum.isdigit() or not maximum.isdigit() or
        int(minimum) <= int(time.time()) or int(maximum) < int(minimum)
    ):
        raise AccessuraPaymentError(
            "binding-bid authorization validity window is invalid",
            code=AccessuraErrorCode.BID_TERMS_INVALID)
    _enforce_payment_controls(
        amount_base_units=amount_base_units,
        network=network,
        controls=payment_controls,
        action="binding bid",
    )
    authorization = {
        "from": account.address,
        "to": pay_to,
        "value": str(amount_base_units),
        "validAfter": "0",
        "validBefore": minimum,
        "nonce": "0x" + secrets.token_hex(32),
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
    domain = {
        "name": token_domain["name"],
        "version": token_domain["version"],
        "chainId": int(network.split(":", 1)[1]),
        "verifyingContract": asset,
    }
    typed = encode_typed_data(domain, types, {
        **authorization,
        "value": int(authorization["value"]),
        "validAfter": 0,
        "validBefore": int(authorization["validBefore"]),
    })
    compact = {
        "signature": _sig_hex(account.sign_message(typed).signature),
        "authorization": authorization,
    }
    fingerprint = "sha256:" + hashlib.sha256(
        _canonical_json(compact).encode("utf-8")
    ).hexdigest()
    return compact, fingerprint


def _binding_bid_sla_risk_warnings(payment_terms: dict) -> list[dict]:
    """Validate the frozen Seller SLA and surface long commitment risk."""
    if not isinstance(payment_terms, dict):
        raise AccessuraPaymentError(
            "bid status did not return payment_terms",
            code=AccessuraErrorCode.PAYMENT_TERMS_MISSING)
    raw_sla = payment_terms.get("seller_delivery_sla_seconds")
    if isinstance(raw_sla, bool):
        raise AccessuraPaymentError(
            "binding-bid Seller delivery SLA must be an integer",
            code=AccessuraErrorCode.SELLER_SLA_INVALID)
    try:
        sla_seconds = int(raw_sla)
    except (TypeError, ValueError) as exc:
        raise AccessuraPaymentError(
            "binding-bid payment terms are missing seller_delivery_sla_seconds",
            code=AccessuraErrorCode.SELLER_SLA_INVALID,
        ) from exc
    if str(sla_seconds) != str(raw_sla) or not 30 <= sla_seconds <= 86_400:
        raise AccessuraPaymentError(
            "binding-bid Seller delivery SLA must be an integer from 30 to 86400 seconds",
            code=AccessuraErrorCode.SELLER_SLA_INVALID,
        )
    if sla_seconds <= LONG_SELLER_DELIVERY_SLA_WARNING_SECONDS:
        return []
    return [{
        "code": "LONG_SELLER_DELIVERY_SLA",
        "seller_delivery_sla_seconds": sla_seconds,
        "warning_threshold_seconds":
            LONG_SELLER_DELIVERY_SLA_WARNING_SECONDS,
        "message": (
            f"Seller delivery SLA is {sla_seconds} seconds. If this bid wins, "
            "the bid amount may remain committed in active exposure until "
            "Seller delivery or the deadline. This SLA was exposed in "
            "payment_terms before signing; proceed only if the duration is "
            "acceptable."
        ),
    }]


def _attach_payment_risk_warnings(
        response: dict, risk_warnings: list[dict]) -> dict:
    if not risk_warnings:
        return response
    attached = dict(response)
    existing = attached.get("payment_risk_warnings")
    attached["payment_risk_warnings"] = (
        [*existing, *risk_warnings]
        if isinstance(existing, list)
        else risk_warnings
    )
    return attached


def _sign_x402_payment(account, payment_required: dict, *,
                       expected_amount: Optional[str] = None,
                       expected_pay_to: Optional[str] = None,
                       payment_controls: Optional[dict] = None,
                       claim_id: Optional[str] = None) -> tuple[dict, str]:
    """Build x402 v2 exact EVM payload and PAYMENT-SIGNATURE header.

    Base USDC implements EIP-3009 TransferWithAuthorization. The buyer signs
    locally; only the payload is returned to the HTTP layer.

    ``expected_amount`` / ``expected_pay_to`` (when provided by the caller from a
    prior read-only preview) bind this signature to the previewed terms: the
    signer refuses if the live offer's amount or recipient changed. A hard
    ceiling (ACCESSURA_MAX_PAY_USDC), finite cumulative grant, and the mainnet
    gate apply unconditionally before the irreversible signature.
    """
    from eth_account.messages import encode_typed_data

    if payment_required.get("x402Version") != 2:
        raise AccessuraPaymentError(
            "PAYMENT-REQUIRED must use x402Version 2",
            code=AccessuraErrorCode.PAYMENT_REQUIRED_MALFORMED)
    accepts = payment_required.get("accepts")
    if not isinstance(accepts, list) or len(accepts) != 1:
        raise AccessuraPaymentError(
            "PAYMENT-REQUIRED must contain exactly one payment offer",
            code=AccessuraErrorCode.PAYMENT_REQUIRED_MALFORMED)
    accepted = accepts[0]
    resource = payment_required.get("resource")
    if not isinstance(accepted, dict) or not isinstance(resource, dict):
        raise AccessuraPaymentError(
            "PAYMENT-REQUIRED did not include an exact EVM offer",
            code=AccessuraErrorCode.PAYMENT_REQUIRED_MALFORMED)
    network = str(accepted.get("network", ""))
    if accepted.get("scheme") != "exact":
        raise AccessuraPaymentError(
            f"unsupported x402 scheme: {accepted.get('scheme')}",
            code=AccessuraErrorCode.PAYMENT_SCHEME_UNSUPPORTED)
    profile = X402_CHAIN_PROFILES.get(network)
    if profile is None:
        raise AccessuraPaymentError(
            f"unsupported x402 network: {network}",
            code=AccessuraErrorCode.PAYMENT_NETWORK_UNSUPPORTED)
    if str(accepted.get("asset", "")).lower() != str(profile["asset"]).lower():
        raise AccessuraPaymentError(
            f"x402 offer does not use configured {profile['label']} USDC",
            code=AccessuraErrorCode.PAYMENT_ASSET_MISMATCH)
    extra = accepted.get("extra")
    expected_extra = {"name": profile["domain_name"], "version": "2"}
    if extra != expected_extra:
        raise AccessuraPaymentError(
            f"x402 offer has an unexpected {profile['label']} USDC EIP-712 domain",
            code=AccessuraErrorCode.PAYMENT_DOMAIN_MISMATCH)
    amount = str(accepted.get("amount", ""))
    if not amount.isdigit() or int(amount) <= 0:
        raise AccessuraPaymentError(
            "x402 amount must be a positive integer in USDC base units",
            code=AccessuraErrorCode.PAYMENT_REQUIRED_MALFORMED)
    _assert_network_allowed(network)
    pay_to = accepted.get("payTo")
    if expected_pay_to is not None and str(pay_to).lower() != str(expected_pay_to).lower():
        raise AccessuraPaymentError(
            "x402 payTo changed since preview; refusing to pay a different "
            f"recipient (previewed {expected_pay_to}, offered {pay_to})",
            code=AccessuraErrorCode.PAYMENT_PAYTO_MISMATCH)
    if expected_amount is not None and str(amount) != str(expected_amount):
        raise AccessuraPaymentError(
            "x402 amount changed since preview; refusing to pay a different "
            f"amount (previewed {expected_amount}, offered {amount})",
            code=AccessuraErrorCode.PAYMENT_AMOUNT_MISMATCH)
    _enforce_payment_controls(
        amount_base_units=int(amount),
        network=network,
        controls=payment_controls,
        action="x402 payment",
        claim_id=claim_id,
    )
    max_timeout = int(accepted.get("maxTimeoutSeconds", 60))
    if max_timeout <= 0:
        raise AccessuraPaymentError(
            "x402 maxTimeoutSeconds must be positive",
            code=AccessuraErrorCode.MAX_TIMEOUT_INVALID)
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

