"""Payment control system: budget enforcement and financial fact collection.

All functions are pure or self-contained. They validate env-configured budget
limits and load platform payment/exposure history to build a fail-closed
budget snapshot before any money movement.
"""

import os
import urllib.parse
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from .errors import (
    AccessuraErrorCode,
    AccessuraPaymentError,
)
from ._signing import (
    BASE_MAINNET_CAIP2,
    DEFAULT_X402_NETWORK,
    X402_CHAIN_PROFILES,
    _assert_network_allowed,
)
from ._http import _request_response

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
        raise AccessuraPaymentError(
            f"{action} amount {amount_base_units} base units exceeds the "
            f"ACCESSURA_MAX_PAY_USDC ceiling of {ceiling}; raise the limit "
            "deliberately to authorize more",
            code=AccessuraErrorCode.BUDGET_EXCEEDED)
    if not config["budget_configured"]:
        return
    if config["configured_status"] != "requires_history":
        raise AccessuraPaymentError(
            f"{action} refused: budget_status is "
            f"{config['configured_status']!r}",
            code=AccessuraErrorCode.BUDGET_UNKNOWN)
    if controls is None:
        raise AccessuraPaymentError(
            f"{action} refused: cumulative budget facts were not loaded",
            code=AccessuraErrorCode.BUDGET_UNKNOWN)
    status = controls.get("budget_status")
    if status in ("ready", "exhausted") and (
        controls.get("budget_limit_base_units")
        != str(config["budget_limit_base_units"])
        or controls.get("budget_start_at") != config["budget_start_at"]
        or controls.get("budget_expires_at") != config["budget_expires_at"]
    ):
        raise AccessuraPaymentError(
            f"{action} refused: payment-control configuration changed after "
            "the budget snapshot was loaded",
            code=AccessuraErrorCode.BUDGET_UNKNOWN)
    committed_for_claim = 0
    if claim_id:
        committed_for_claim = int(
            (controls.get("_active_claim_amounts") or {}).get(claim_id, 0))
    if status == "exhausted" and committed_for_claim == amount_base_units:
        return
    if status != "ready":
        reason = controls.get("unknown_reason")
        detail = f": {reason}" if reason else ""
        raise AccessuraPaymentError(
            f"{action} refused: budget_status is {status!r}{detail}",
            code=AccessuraErrorCode.BUDGET_UNKNOWN)
    remaining = int(controls["remaining_base_units"])
    incremental = 0 if committed_for_claim == amount_base_units else amount_base_units
    if incremental > remaining:
        raise AccessuraPaymentError(
            f"{action} amount {amount_base_units} base units exceeds the "
            f"remaining cumulative authorization of {remaining} base units",
            code=AccessuraErrorCode.BUDGET_EXCEEDED)


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


def _payment_readiness(account, network: str = DEFAULT_X402_NETWORK) -> dict:
    """Describe local binding/config; callers may attach platform fact history."""
    profile = X402_CHAIN_PROFILES.get(network)
    if profile is None:
        raise AccessuraPaymentError(
            f"unsupported payment network: {network}",
            code=AccessuraErrorCode.PAYMENT_NETWORK_UNSUPPORTED)
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


