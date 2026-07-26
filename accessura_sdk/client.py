"""Accessura SDK — full-marketplace Python client for buyer and seller agents.

    pip install httpx cryptography eth-account

    from accessura_sdk import BuyerAgent, SellerAgent

    # Buyer
    agent = BuyerAgent(private_key="0x...")
    agent.register("My Agent")
    agent.get_api_key()
    packs = agent.search("election")
    agent.bid(pack_id, signal_id, 0.15)

    # Seller
    seller = SellerAgent(private_key="0x...")
    seller.register("My Seller", role="seller")
    seller.publish_pack(title="Hook title", info_type="text", ...)
    seller.append_signal(pack_id, "Signal label", "HOOK summary", ...)
"""

import base64
import hashlib
import json
import os
import secrets
import threading
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
         "User-Agent": "Accessura-SDK/0.7", **headers}
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
        raise RuntimeError("auth challenge payload must be an object")
    domain = payload.get("domain")
    if not isinstance(domain, dict):
        raise RuntimeError("auth challenge payload missing an EIP-712 domain")
    if domain.get("name") != PROTOCOL_DOMAIN["name"]:
        raise RuntimeError(
            "refusing to sign auth challenge: unexpected EIP-712 domain name "
            f"{domain.get('name')!r} (expected {PROTOCOL_DOMAIN['name']!r})")
    if str(domain.get("verifyingContract", "")).lower() != PROTOCOL_DOMAIN["verifyingContract"]:
        raise RuntimeError(
            "refusing to sign auth challenge: non-null verifyingContract "
            f"{domain.get('verifyingContract')!r} (possible value-transfer "
            "authorization disguised as an auth challenge)")
    primary = payload.get("primaryType")
    if primary not in AUTH_CHALLENGE_PRIMARY_TYPES:
        raise RuntimeError(
            f"refusing to sign auth challenge: primaryType {primary!r} is not an "
            f"Accessura auth type {sorted(AUTH_CHALLENGE_PRIMARY_TYPES)}")


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
        raise RuntimeError(
            "Base mainnet (eip155:8453) is closed for this release; the active "
            "target is Base Sepolia (eip155:84532). Set ACCESSURA_ALLOW_MAINNET=1 "
            "only after the deployment promotion gates pass.")


def _parse_usdc_limit(raw: str, variable: str) -> int:
    try:
        whole = Decimal(raw)
    except Exception as exc:
        raise RuntimeError(f"{variable} must be a positive USDC amount, got {raw!r}") from exc
    scaled = whole * 1_000_000
    if whole <= 0 or scaled != scaled.to_integral_value():
        raise RuntimeError(
            f"{variable} must be positive with at most 6 decimal places, got {raw!r}")
    return int(scaled)


def _max_pay_base_units(network: str = DEFAULT_X402_NETWORK) -> Optional[int]:
    """Optional hard ceiling for a single x402 payment, in USDC base units.

    Base Sepolia defaults to 100 USDC and permits an explicit empty value to
    disable the convenience guard. Base mainnet has no default: the operator
    must set a positive ACCESSURA_MAX_PAY_USDC deliberately.
    """
    configured = os.getenv("ACCESSURA_MAX_PAY_USDC")
    if network == BASE_MAINNET_CAIP2 and (
        configured is None or not configured.strip()
    ):
        raise RuntimeError(
            "Base mainnet requires an explicit positive ACCESSURA_MAX_PAY_USDC")
    raw = "100" if configured is None else configured.strip()
    if not raw:
        return None
    return _parse_usdc_limit(raw, "ACCESSURA_MAX_PAY_USDC")


def _parse_budget_time(raw: str, variable: str) -> datetime:
    normalized = raw.strip()
    if not normalized:
        raise RuntimeError(f"{variable} is required when ACCESSURA_BUDGET_USDC is set")
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError(f"{variable} must be an RFC3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise RuntimeError(f"{variable} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _payment_control_config(
    network: str = DEFAULT_X402_NETWORK,
    *,
    now: Optional[datetime] = None,
) -> dict:
    """Read the principal's finite absolute grant from environment variables."""
    per_payment_limit = _max_pay_base_units(network)
    budget_raw = os.getenv("ACCESSURA_BUDGET_USDC")
    start_raw = os.getenv("ACCESSURA_BUDGET_START_AT", "")
    expires_raw = os.getenv("ACCESSURA_BUDGET_EXPIRES_AT", "")
    budget_configured = budget_raw is not None and bool(budget_raw.strip())
    if not budget_configured:
        if start_raw.strip() or expires_raw.strip():
            raise RuntimeError(
                "ACCESSURA_BUDGET_START_AT/EXPIRES_AT require ACCESSURA_BUDGET_USDC")
        if network == BASE_MAINNET_CAIP2:
            raise RuntimeError(
                "Base mainnet requires an explicit positive ACCESSURA_BUDGET_USDC")
        return {
            "mode": "per_payment_only",
            "per_payment_limit_base_units": per_payment_limit,
            "budget_limit_base_units": None,
            "budget_start_at": None,
            "budget_expires_at": None,
            "budget_configured": False,
            "configured_status": "unconfigured",
        }

    budget_limit = _parse_usdc_limit(
        budget_raw.strip(), "ACCESSURA_BUDGET_USDC")
    starts_at = _parse_budget_time(start_raw, "ACCESSURA_BUDGET_START_AT")
    expires_at = _parse_budget_time(
        expires_raw, "ACCESSURA_BUDGET_EXPIRES_AT")
    if expires_at <= starts_at:
        raise RuntimeError(
            "ACCESSURA_BUDGET_EXPIRES_AT must be after ACCESSURA_BUDGET_START_AT")
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if current < starts_at:
        configured_status = "not_started"
    elif current >= expires_at:
        configured_status = "expired"
    else:
        configured_status = "requires_history"
    return {
        "mode": "finite_absolute_budget",
        "per_payment_limit_base_units": per_payment_limit,
        "budget_limit_base_units": budget_limit,
        "budget_start_at": _iso_utc(starts_at),
        "budget_expires_at": _iso_utc(expires_at),
        "budget_configured": True,
        "configured_status": configured_status,
    }


def _base_payment_controls(config: dict) -> dict:
    return {
        "mode": config["mode"],
        "enforcement": "official_kit_path_only",
        "per_payment_limit_base_units": (
            str(config["per_payment_limit_base_units"])
            if config["per_payment_limit_base_units"] is not None else None
        ),
        "budget_limit_base_units": (
            str(config["budget_limit_base_units"])
            if config["budget_limit_base_units"] is not None else None
        ),
        "budget_start_at": config["budget_start_at"],
        "budget_expires_at": config["budget_expires_at"],
        "spent_base_units": None,
        "active_exposure_base_units": None,
        "remaining_base_units": None,
        "budget_status": config["configured_status"],
        "as_of": None,
        "history_complete_from": None,
        "unknown_reason": None,
    }


def _unknown_payment_controls(config: dict, reason: str) -> dict:
    controls = _base_payment_controls(config)
    controls["budget_status"] = "unknown"
    controls["unknown_reason"] = reason
    return controls


def _local_payment_controls(network: str) -> dict:
    """Return config-visible controls before platform history is loaded."""
    try:
        config = _payment_control_config(network)
    except RuntimeError as exc:
        return {
            "mode": "invalid",
            "enforcement": "official_kit_path_only",
            "per_payment_limit_base_units": None,
            "budget_limit_base_units": None,
            "budget_start_at": None,
            "budget_expires_at": None,
            "spent_base_units": None,
            "active_exposure_base_units": None,
            "remaining_base_units": None,
            "budget_status": "unknown",
            "as_of": None,
            "history_complete_from": None,
            "unknown_reason": str(exc),
        }
    controls = _base_payment_controls(config)
    if not config["budget_configured"]:
        return controls
    if config["configured_status"] in ("not_started", "expired"):
        return controls
    return _unknown_payment_controls(
        config, "payment history and active exposure have not been loaded")


def _validate_fact_page(page: dict, expected_view: str) -> list[dict]:
    if not isinstance(page, dict) or page.get("view") != expected_view:
        raise RuntimeError(f"{expected_view} response has an unexpected shape")
    items = page.get("items")
    if not isinstance(items, list):
        raise RuntimeError(f"{expected_view} response is missing items")
    if not isinstance(page.get("has_more"), bool):
        raise RuntimeError(f"{expected_view} response is missing has_more")
    if page["has_more"] and not isinstance(page.get("next_cursor"), str):
        raise RuntimeError(f"{expected_view} response is missing next_cursor")
    return items


def _summarize_payment_controls(
    network: str,
    payment_pages: list[dict],
    exposure_pages: list[dict],
) -> dict:
    """Build a fail-closed budget snapshot from the platform's fact projection."""
    config = _payment_control_config(network)
    controls = _base_payment_controls(config)
    if not config["budget_configured"]:
        return controls
    if config["configured_status"] in ("not_started", "expired"):
        return controls
    if not payment_pages or not exposure_pages:
        return _unknown_payment_controls(config, "financial fact pages are missing")

    payment_items: list[dict] = []
    exposure_items: list[dict] = []
    history_complete_from: Optional[str] = None
    as_of_values: list[str] = []
    for page in payment_pages:
        payment_items.extend(_validate_fact_page(page, "payments"))
        if page.get("history_complete") is not True:
            return _unknown_payment_controls(
                config, "platform payment history is not declared complete")
        boundary = page.get("history_complete_from")
        if not isinstance(boundary, str):
            return _unknown_payment_controls(
                config, "platform payment history completeness boundary is missing")
        history_complete_from = (
            boundary if history_complete_from is None
            else max(history_complete_from, boundary)
        )
        if isinstance(page.get("as_of"), str):
            as_of_values.append(page["as_of"])
    for page in exposure_pages:
        exposure_items.extend(_validate_fact_page(page, "active_exposure"))
        if page.get("snapshot_kind") != "current_platform_state":
            return _unknown_payment_controls(
                config, "active exposure is not a current platform snapshot")
        if isinstance(page.get("as_of"), str):
            as_of_values.append(page["as_of"])

    if history_complete_from is None:
        return _unknown_payment_controls(
            config, "platform completeness boundary is unavailable")
    try:
        history_boundary = _parse_budget_time(
            history_complete_from, "history_complete_from")
        budget_start = _parse_budget_time(
            config["budget_start_at"], "ACCESSURA_BUDGET_START_AT")
    except RuntimeError as exc:
        return _unknown_payment_controls(config, str(exc))
    if budget_start < history_boundary:
        return _unknown_payment_controls(
            config, "budget starts before the platform completeness boundary")

    paid_intents: set[str] = set()
    spent = 0
    for item in payment_items:
        amount = str(item.get("amount_base_units", ""))
        if not amount.isdigit() or int(amount) <= 0:
            return _unknown_payment_controls(
                config, "payment history contains an invalid amount")
        if item.get("chain_fact_status") != "confirmed":
            return _unknown_payment_controls(
                config, "payment history contains an unconfirmed chain fact")
        intent_id = item.get("intent_id")
        if not isinstance(intent_id, str) or not intent_id:
            return _unknown_payment_controls(
                config, "payment history contains an invalid intent_id")
        if intent_id in paid_intents:
            continue
        paid_intents.add(intent_id)
        spent += int(amount)

    # Prefer an intent over its predecessor bid, and do not count a paid intent
    # again if reconciliation state still appears in the active projection.
    intent_bid_ids = {
        str(item.get("bid_id"))
        for item in exposure_items
        if item.get("intent_id") and item.get("bid_id")
    }
    active_seen: set[str] = set()
    active_claim_amounts: dict[str, int] = {}
    active = 0
    for item in exposure_items:
        exposure_id = item.get("exposure_id")
        amount = str(item.get("amount_base_units", ""))
        if not isinstance(exposure_id, str) or not exposure_id:
            return _unknown_payment_controls(
                config, "active exposure contains an invalid exposure_id")
        if not amount.isdigit() or int(amount) <= 0:
            return _unknown_payment_controls(
                config, "active exposure contains an invalid amount")
        if exposure_id in active_seen:
            continue
        active_seen.add(exposure_id)
        intent_id = item.get("intent_id")
        if isinstance(intent_id, str) and intent_id in paid_intents:
            continue
        if (
            not intent_id
            and item.get("kind") in ("pending_settlement", "ranked_waiting")
            and str(item.get("bid_id")) in intent_bid_ids
        ):
            continue
        units = int(amount)
        active += units
        claim_id = item.get("claim_id")
        if isinstance(claim_id, str) and claim_id:
            active_claim_amounts[claim_id] = max(
                units, active_claim_amounts.get(claim_id, 0))

    remaining = max(0, config["budget_limit_base_units"] - spent - active)
    controls.update({
        "spent_base_units": str(spent),
        "active_exposure_base_units": str(active),
        "remaining_base_units": str(remaining),
        "budget_status": "ready" if remaining > 0 else "exhausted",
        "as_of": max(as_of_values) if as_of_values else None,
        "history_complete_from": history_complete_from,
        "_active_claim_amounts": active_claim_amounts,
    })
    return controls


def _public_payment_controls(controls: dict) -> dict:
    return {key: value for key, value in controls.items() if not key.startswith("_")}


def _enforce_payment_controls(
    *,
    amount_base_units: int,
    network: str,
    controls: Optional[dict],
    action: str,
    claim_id: Optional[str] = None,
) -> None:
    """Refuse an over-limit bid/payment before its authorization is signed."""
    _assert_network_allowed(network)
    config = _payment_control_config(network)
    ceiling = config["per_payment_limit_base_units"]
    if ceiling is not None and amount_base_units > ceiling:
        raise RuntimeError(
            f"{action} amount {amount_base_units} base units exceeds the "
            f"ACCESSURA_MAX_PAY_USDC ceiling of {ceiling}; raise the limit "
            "deliberately to authorize more")
    if not config["budget_configured"]:
        return
    if config["configured_status"] != "requires_history":
        raise RuntimeError(
            f"{action} refused: budget_status is "
            f"{config['configured_status']!r}")
    if controls is None:
        raise RuntimeError(
            f"{action} refused: cumulative budget facts were not loaded")
    status = controls.get("budget_status")
    if status in ("ready", "exhausted") and (
        controls.get("budget_limit_base_units")
        != str(config["budget_limit_base_units"])
        or controls.get("budget_start_at") != config["budget_start_at"]
        or controls.get("budget_expires_at") != config["budget_expires_at"]
    ):
        raise RuntimeError(
            f"{action} refused: payment-control configuration changed after "
            "the budget snapshot was loaded")
    committed_for_claim = 0
    if claim_id:
        committed_for_claim = int(
            (controls.get("_active_claim_amounts") or {}).get(claim_id, 0))
    if status == "exhausted" and committed_for_claim == amount_base_units:
        return
    if status != "ready":
        reason = controls.get("unknown_reason")
        detail = f": {reason}" if reason else ""
        raise RuntimeError(
            f"{action} refused: budget_status is {status!r}{detail}")
    remaining = int(controls["remaining_base_units"])
    incremental = 0 if committed_for_claim == amount_base_units else amount_base_units
    if incremental > remaining:
        raise RuntimeError(
            f"{action} amount {amount_base_units} base units exceeds the "
            f"remaining cumulative authorization of {remaining} base units")


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
        raise RuntimeError("bid status did not return payment_terms")
    _binding_bid_sla_risk_warnings(payment_terms)
    network = str(payment_terms.get("network", ""))
    profile = X402_CHAIN_PROFILES.get(network)
    if profile is None:
        raise RuntimeError(f"unsupported binding-bid payment network: {network}")
    _assert_network_allowed(network)
    if payment_terms.get("scheme") != "exact":
        raise RuntimeError("binding-bid payment terms must use exact settlement")
    asset = str(payment_terms.get("asset", ""))
    if asset.lower() != str(profile["asset"]).lower():
        raise RuntimeError(
            f"binding-bid terms do not use configured {profile['label']} USDC")
    token_domain = payment_terms.get("token_domain")
    expected_domain = {"name": profile["domain_name"], "version": "2"}
    if token_domain != expected_domain:
        raise RuntimeError(
            "binding-bid terms have an unexpected USDC EIP-712 domain")
    pay_to = payment_terms.get("pay_to")
    if not isinstance(pay_to, str) or not pay_to:
        raise RuntimeError("binding-bid payment terms are missing pay_to")
    if (
        payment_terms.get("payment_trigger") != "seller_delivery_ready" or
        payment_terms.get("settlement_rule") != "top_n_pay_as_bid"
    ):
        raise RuntimeError("binding-bid payment trigger or settlement rule is invalid")
    minimum = str(payment_terms.get("authorization_valid_before_min", ""))
    maximum = str(payment_terms.get("authorization_valid_before_max", ""))
    if (
        not minimum.isdigit() or not maximum.isdigit() or
        int(minimum) <= int(time.time()) or int(maximum) < int(minimum)
    ):
        raise RuntimeError("binding-bid authorization validity window is invalid")
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
        raise RuntimeError("bid status did not return payment_terms")
    raw_sla = payment_terms.get("seller_delivery_sla_seconds")
    if isinstance(raw_sla, bool):
        raise RuntimeError("binding-bid Seller delivery SLA must be an integer")
    try:
        sla_seconds = int(raw_sla)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            "binding-bid payment terms are missing seller_delivery_sla_seconds"
        ) from exc
    if str(sla_seconds) != str(raw_sla) or not 30 <= sla_seconds <= 86_400:
        raise RuntimeError(
            "binding-bid Seller delivery SLA must be an integer from 30 to 86400 seconds"
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
    _assert_network_allowed(network)
    pay_to = accepted.get("payTo")
    if expected_pay_to is not None and str(pay_to).lower() != str(expected_pay_to).lower():
        raise RuntimeError(
            "x402 payTo changed since preview; refusing to pay a different "
            f"recipient (previewed {expected_pay_to}, offered {pay_to})")
    if expected_amount is not None and str(amount) != str(expected_amount):
        raise RuntimeError(
            "x402 amount changed since preview; refusing to pay a different "
            f"amount (previewed {expected_amount}, offered {amount})")
    _enforce_payment_controls(
        amount_base_units=int(amount),
        network=network,
        controls=payment_controls,
        action="x402 payment",
        claim_id=claim_id,
    )
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
    """Describe local binding/config; callers may attach platform fact history."""
    profile = X402_CHAIN_PROFILES.get(network)
    if profile is None:
        raise ValueError(f"unsupported payment network: {network}")
    _assert_network_allowed(network)
    payment_controls = _local_payment_controls(network)
    return {
        "signing_ready": payment_controls["budget_status"] in (
            "unconfigured", "ready"),
        "payment_ready": None,
        "balance_status": "not_checked",
        "payment_address": account.address,
        "network": network,
        "chain": profile["label"],
        "usdc_contract": profile["asset"],
        "testnet": profile["testnet"],
        "custody": "self_custody",
        "platform_balance": None,
        "payment_controls": payment_controls,
    }


def _collect_financial_pages_sync(
    *,
    api: str,
    headers: dict,
    view: str,
    budget_start_at: Optional[str] = None,
) -> list[dict]:
    pages: list[dict] = []
    cursor: Optional[str] = None
    seen_cursors: set[str] = set()
    for _ in range(1_000):
        params = {"view": view, "limit": "200"}
        if budget_start_at:
            params["from"] = budget_start_at
        if cursor:
            params["cursor"] = cursor
        query = urllib.parse.urlencode(params)
        status, _, page = _request_response(
            "GET", f"{api}/transactions?{query}", headers)
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


def _load_payment_controls_sync(
    *,
    api: str,
    headers: dict,
    network: str,
) -> dict:
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
        payments = _collect_financial_pages_sync(
            api=api,
            headers=headers,
            view="payments",
            budget_start_at=config["budget_start_at"],
        )
        exposure = _collect_financial_pages_sync(
            api=api,
            headers=headers,
            view="active_exposure",
        )
        return _summarize_payment_controls(network, payments, exposure)
    except Exception as exc:
        return _unknown_payment_controls(config, str(exc))


# ═══════════════════════════════════════════════════════════════════════════
# BuyerAgent (EIP-712 self-custody)
# ═══════════════════════════════════════════════════════════════════════════

class BuyerAgent:
    """Secp256k1-keyed buyer agent. EIP-712 auth, full trading lifecycle."""

    def __init__(self, private_key: str,
                 base_url: str = "https://testnet.accessura.io",
                 api_key: Optional[str] = None,
                 token: Optional[str] = None):
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
        self._token = token
        self._api_key = api_key
        self._payment_authority_lock = threading.RLock()

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

        domain = PROTOCOL_DOMAIN
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
        sig = _sign_auth_challenge(self._account, ch["sign_payload"])
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
        sig = _sign_auth_challenge(self._account, ch["sign_payload"])
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
        elif not self._token:
            # Cache an immediate Bearer too: /claims is Bearer-only, so an API
            # key alone leaves claim polling unusable until auth_token runs.
            try:
                self.login()
            except Exception:
                pass  # api key still valid; claims_list raises an actionable error
        return api_key

    # ── discovery ──────────────────────────────────────────────────────

    def list_topics(self, category: str = "", state: str = "active") -> dict:
        params = [f"state={urllib.parse.quote(state)}"]
        if category:
            params.append(f"category={urllib.parse.quote(category)}")
        return _request("GET", f"{self.api}/topics?{'&'.join(params)}", {})

    def list_topic_packs(self, slug: str, state: str = "all") -> dict:
        return _request(
            "GET",
            f"{self.api}/topics/{urllib.parse.quote(slug)}/packs?state={urllib.parse.quote(state)}",
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
        """Sign a binding bid and its on-delivery EIP-3009 authorization."""
        bid_amount = _usdc_price_base_units(price)
        with self._payment_authority_lock:
            for attempt in range(2):
                status = self.get_bid_status(pack_id, signal_id)
                payment_terms = status.get("payment_terms")
                network = str((payment_terms or {}).get("network", ""))
                risk_warnings = _binding_bid_sla_risk_warnings(payment_terms)
                controls = _load_payment_controls_sync(
                    api=self.api, headers=self._auth(), network=network)
                payment_authorization, payment_fingerprint = (
                    _sign_bid_payment_authorization(
                        self._account,
                        payment_terms,
                        bid_amount,
                        payment_controls=controls,
                    )
                )
                authorization = _sign_bid_authorization(
                    self._account,
                    self._enc_pub,
                    pack_id,
                    signal_id,
                    price,
                    status,
                    payment_fingerprint,
                )
                response = _request(
                    "POST", f"{self.api}/packs/{pack_id}/bid", self._auth(),
                    {"bid_price": price, "signal_id": signal_id,
                     "authorization": authorization,
                     "payment_authorization": payment_authorization})
                response = _attach_payment_risk_warnings(
                    response, risk_warnings)
                if (
                    response.get("error_code") != "BID_AUTHORIZATION_MISMATCH"
                    or attempt == 1
                ):
                    return response
        raise RuntimeError("unreachable bid retry state")

    def get_bid_status(self, pack_id: str,
                       signal_id: str = "") -> dict:
        params = ""
        if signal_id:
            params = f"?signal_id={urllib.parse.quote(signal_id)}"
        status = _request(
            "GET", f"{self.api}/packs/{pack_id}/bid{params}",
            self._auth())
        payment_terms = status.get("payment_terms")
        if not isinstance(payment_terms, dict):
            return status
        return _attach_payment_risk_warnings(
            status, _binding_bid_sla_risk_warnings(payment_terms))

    def settle(self, pack_id: str, signal_id: str = "") -> dict:
        return _request(
            "POST", f"{self.api}/packs/{pack_id}/settle",
            self._auth(), {"signal_id": signal_id})

    def payment_readiness(self, network: str = DEFAULT_X402_NETWORK) -> dict:
        """Return signing configuration plus platform payment/exposure facts."""
        readiness = _payment_readiness(self._account, network)
        controls = _load_payment_controls_sync(
            api=self.api, headers=self._auth(), network=network)
        readiness["payment_controls"] = _public_payment_controls(controls)
        readiness["signing_ready"] = controls["budget_status"] in (
            "unconfigured", "ready")
        return readiness

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

    def get_transaction_receipt(self, claim_id: str) -> dict:
        """Read secret-free direct transaction evidence as a participant."""
        return _request(
            "GET",
            f"{self.api}/transactions/{urllib.parse.quote(claim_id)}/receipt",
            self._auth())

    def pay_claim(self, claim_id: str, expected_amount: Optional[str] = None,
                  expected_pay_to: Optional[str] = None) -> dict:
        """Sign x402 locally and pay USDC directly to the claim seller.

        Pass ``expected_amount`` / ``expected_pay_to`` (read from a prior
        read-only preview) to bind the signature to the previewed terms.
        """
        with self._payment_authority_lock:
            status, _, required = _request_response(
                "GET", f"{self.api}/claims/{urllib.parse.quote(claim_id)}/pay",
                self._auth())
            if status == 200 or status == 202:
                return {**required, "_http_status": status}
            if status != 402:
                return {**required, "_http_status": status}
            accepts = required.get("accepts")
            network = (
                str(accepts[0].get("network", ""))
                if isinstance(accepts, list) and accepts
                and isinstance(accepts[0], dict)
                else ""
            )
            controls = _load_payment_controls_sync(
                api=self.api, headers=self._auth(), network=network)
            _, payment_header = _sign_x402_payment(
                self._account, required,
                expected_amount=expected_amount,
                expected_pay_to=expected_pay_to,
                payment_controls=controls,
                claim_id=claim_id,
            )
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

# ═══════════════════════════════════════════════════════════════════════════
# SellerAgent (EIP-712 self-custody seller)
# ═══════════════════════════════════════════════════════════════════════════

class SellerAgent:
    """Secp256k1-keyed seller agent. EIP-712 auth, publish + deliver."""

    def __init__(self, private_key: str,
                 base_url: str = "https://testnet.accessura.io",
                 delivery_secret: Optional[Union[str, bytes]] = None,
                 api_key: Optional[str] = None,
                 token: Optional[str] = None):
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
        self._token = token
        self._api_key = api_key

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

        domain = PROTOCOL_DOMAIN
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
        sig = _sign_auth_challenge(self._account, ch["sign_payload"])
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
        elif not self._token:
            # Cache an immediate Bearer too: /claims is Bearer-only.
            try:
                self.login()
            except Exception:
                pass  # api key still valid; list_claims raises an actionable error
        return api_key

    def login(self) -> None:
        """Create a fresh wallet-signature Bearer session for claim polling."""
        from eth_account.messages import encode_typed_data

        result = _request(
            "POST", f"{self.api}/auth/token", {},
            {"agent_id": self.agent_id, "action": "challenge"})
        challenge = result.get("challenge") or {}
        payload = challenge.get("sign_payload")
        if not payload:
            raise RuntimeError(
                f"auth challenge failed (did register() succeed?): "
                f"{result.get('error') or result}")
        signature = _sign_auth_challenge(self._account, payload)
        out = _request(
            "POST", f"{self.api}/auth/token", {},
            {"agent_id": self.agent_id,
             "challenge_id": challenge.get("challenge_id"),
             "signature": signature})
        if not out.get("token"):
            raise RuntimeError(f"token exchange failed: {out.get('error') or out}")
        self._token = out["token"]

    # ── discovery (shared with buyer — public endpoints) ─────────────

    def list_topics(self, category: str = "", state: str = "active") -> dict:
        params = [f"state={urllib.parse.quote(state)}"]
        if category:
            params.append(f"category={urllib.parse.quote(category)}")
        return _request("GET", f"{self.api}/topics?{'&'.join(params)}", {})

    def list_topic_packs(self, slug: str, state: str = "all") -> dict:
        return _request(
            "GET",
            f"{self.api}/topics/{urllib.parse.quote(slug)}/packs?state={urllib.parse.quote(state)}",
            {})

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
        signature = _sign_auth_challenge(self._account, payload)
        return _request(
            "POST", f"{self.api}/sellers/payout-wallet/verify", self._auth(),
            {"challenge_id": challenge["challenge_id"], "signature": signature})

    def payment_readiness(self, network: str = DEFAULT_X402_NETWORK) -> dict:
        """Return local payout chain/USDC readiness; no platform balance exists."""
        return _payment_readiness(self._account, network)

    def publish_pack(self, title: str, info_type: str, **kwargs) -> dict:
        """Publish a new data pack. DO NOT include signals — append separately."""
        from catalog_contract import (
            normalize_signal_schema,
            normalize_topic_slugs,
            validate_publish_contract,
        )

        topic_slugs = normalize_topic_slugs(kwargs.get("topic_slugs"))
        signal_type = kwargs.get("signal_type")
        signal_schema = normalize_signal_schema(kwargs.get("signal_schema"))
        fields = kwargs.get("fields")
        if not isinstance(fields, dict):
            raise RuntimeError("fields must be a JSON object")
        delivery_format = validate_publish_contract(
            info_type=info_type,
            topic_slugs=topic_slugs,
            signal_type=signal_type,
            signal_schema=signal_schema,
            fields=fields,
        )
        body = {
            "title": title,
            "info_type": info_type,
            **kwargs,
            "topic": topic_slugs[0],
            "topic_slugs": topic_slugs,
            "signal_type": signal_type,
            "signal_schema": signal_schema,
        }
        body.setdefault("delivery_format", delivery_format)
        return _request("POST", f"{self.api}/packs", self._auth(), body)

    def delist_pack(self, pack_id: str) -> dict:
        return _request(
            "POST", f"{self.api}/packs/{pack_id}/delist", self._auth())

    def reopen_signal_settlement(self, pack_id: str, signal_id: str) -> dict:
        """Explicitly reopen one signal after restoring seller readiness."""
        return _request(
            "POST",
            f"{self.api}/packs/{urllib.parse.quote(pack_id)}/signals/"
            f"{urllib.parse.quote(signal_id)}/settlement-readiness",
            self._auth(), {})

    def get_readiness(self) -> dict:
        """Read Seller-owned payout and delivery readiness state."""
        return _request(
            "GET",
            f"{self.api}/sellers/readiness",
            self._bearer_auth())

    def update_readiness(
        self,
        status: str = "",
        sla_seconds: Optional[int] = None,
    ) -> dict:
        """Pause/resume Seller delivery or update its listing-visible SLA."""
        normalized_status = status.strip().lower()
        if normalized_status and normalized_status not in {"active", "paused"}:
            raise ValueError("status must be active or paused")
        if sla_seconds is not None and (
            isinstance(sla_seconds, bool)
            or not isinstance(sla_seconds, int)
            or not 30 <= sla_seconds <= 86_400
        ):
            raise ValueError("sla_seconds must be an integer from 30 to 86400")
        if not normalized_status and sla_seconds is None:
            raise ValueError("status or sla_seconds required")
        body: dict[str, Any] = {}
        if normalized_status:
            body["status"] = normalized_status
        if sla_seconds is not None:
            body["sla_seconds"] = sla_seconds
        return _request(
            "POST",
            f"{self.api}/sellers/readiness",
            self._bearer_auth(),
            body)

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

    def get_transaction_receipt(self, claim_id: str) -> dict:
        """Read secret-free direct transaction evidence as a participant."""
        return _request(
            "GET",
            f"{self.api}/transactions/{urllib.parse.quote(claim_id)}/receipt",
            self._auth())

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

# ═══════════════════════════════════════════════════════════════════════════
# HumanBuyer (email/password)
# ═══════════════════════════════════════════════════════════════════════════

class HumanBuyer:
    """Removed compatibility shell; launch Buyers must use signed BuyerAgent."""

    def __init__(self, agent_id: str, email: str, password: str,
                 base_url: str = "https://testnet.accessura.io"):
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

    def list_topics(self, category: str = "", state: str = "active") -> dict:
        params = [f"state={urllib.parse.quote(state)}"]
        if category:
            params.append(f"category={urllib.parse.quote(category)}")
        return _request("GET", f"{self.api}/topics?{'&'.join(params)}", {})

    def get_catalog(self) -> dict:
        return _request("GET", f"{self.api}/catalog", {})
