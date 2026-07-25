#!/usr/bin/env python3
"""Run or verify the funded Base Sepolia release evidence.

The default-safe interface has two explicit modes:

* ``--execute`` creates one fresh lifecycle and performs exactly one confirmed
  Buyer-to-Seller Base Sepolia USDC payment.
* ``--validate PATH`` validates an already-sanitized JSON record without
  loading credentials or making network requests.

Every credential used by ``--execute`` comes from the environment. The parser
intentionally has no private-key, API-key, token, or delivery-secret argument.
The script never persists credentials and prints only the sanitized evidence
JSON.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional


BASE_SEPOLIA_NETWORK = "eip155:84532"
BASE_SEPOLIA_USDC = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
TRANSFER_TOPIC = (
    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
)
REQUIRED_ASSERTIONS = (
    "bid_does_not_move_funds",
    "award_is_unpaid",
    "delivery_ready_precedes_402",
    "false_preview_does_not_pay",
    "one_buyer_to_seller_usdc_transfer",
    "no_platform_recipient",
    "buyer_decrypts_locally",
    "retry_does_not_duplicate_payment",
    "receipt_matches_transaction_hash",
)
ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
PRIVATE_KEY = re.compile(r"^(?:0x)?[0-9a-fA-F]{64}$")
TX_HASH = re.compile(r"^0x[0-9a-fA-F]{64}$")
SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")


class EvidenceError(ValueError):
    """The sanitized lifecycle evidence does not prove a release assertion."""


class ExecutionError(RuntimeError):
    """The funded lifecycle stopped before producing valid release evidence."""


class FundedConfig:
    """Environment-only funded runner configuration with a redacted repr."""

    __slots__ = (
        "base_url",
        "rpc_url",
        "buyer_private_key",
        "seller_private_key",
        "seller_payout_address",
        "delivery_secret",
        "topic_slug",
        "bid_price",
        "window_seconds",
        "settle_timeout_seconds",
        "poll_seconds",
        "platform_addresses",
    )

    def __init__(self) -> None:
        if "ACCESSURA_ALLOW_MAINNET" in os.environ:
            raise ExecutionError(
                "ACCESSURA_ALLOW_MAINNET must be absent for the funded Testnet gate"
            )
        self.base_url = os.getenv(
            "ACCESSURA_FUNDED_BASE_URL", "https://testnet.accessura.io"
        ).rstrip("/")
        self.rpc_url = _required_env("ACCESSURA_BASE_SEPOLIA_RPC_URL")
        self.buyer_private_key = _required_env(
            "ACCESSURA_FUNDED_BUYER_PRIVATE_KEY"
        )
        self.seller_private_key = _required_env(
            "ACCESSURA_FUNDED_SELLER_PRIVATE_KEY"
        )
        self.seller_payout_address = _required_env(
            "ACCESSURA_FUNDED_SELLER_PAYOUT_ADDRESS"
        )
        self.delivery_secret = _required_env("ACCESSURA_DELIVERY_SECRET")
        self.topic_slug = os.getenv("ACCESSURA_FUNDED_TOPIC_SLUG", "").strip()
        self.bid_price = _decimal_env("ACCESSURA_FUNDED_BID_USDC", "0.01")
        self.window_seconds = _positive_int_env(
            "ACCESSURA_FUNDED_WINDOW_SECONDS", 60
        )
        self.settle_timeout_seconds = _positive_int_env(
            "ACCESSURA_FUNDED_SETTLE_TIMEOUT_SECONDS",
            self.window_seconds + 180,
        )
        self.poll_seconds = _positive_int_env(
            "ACCESSURA_FUNDED_POLL_SECONDS", 5
        )
        raw_platforms = os.getenv("ACCESSURA_PLATFORM_ADDRESSES", "")
        self.platform_addresses = tuple(
            value.strip()
            for value in raw_platforms.split(",")
            if value.strip()
        )

        if not PRIVATE_KEY.fullmatch(self.buyer_private_key):
            raise ExecutionError(
                "ACCESSURA_FUNDED_BUYER_PRIVATE_KEY is not a 32-byte hex key"
            )
        if not PRIVATE_KEY.fullmatch(self.seller_private_key):
            raise ExecutionError(
                "ACCESSURA_FUNDED_SELLER_PRIVATE_KEY is not a 32-byte hex key"
            )
        if not ADDRESS.fullmatch(self.seller_payout_address):
            raise ExecutionError(
                "ACCESSURA_FUNDED_SELLER_PAYOUT_ADDRESS is not an EVM address"
            )
        for address in self.platform_addresses:
            if not ADDRESS.fullmatch(address):
                raise ExecutionError(
                    "ACCESSURA_PLATFORM_ADDRESSES contains a non-EVM address"
                )
        if self.bid_price <= 0:
            raise ExecutionError("ACCESSURA_FUNDED_BID_USDC must be positive")

    def __repr__(self) -> str:
        return "FundedConfig(<credentials redacted>)"


class RpcClient:
    """Minimal read-only JSON-RPC client used for on-chain evidence."""

    def __init__(self, url: str):
        self.url = url
        self._request_id = 0

    def call(self, method: str, params: list[Any]) -> Any:
        self._request_id += 1
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": self._request_id,
                "method": method,
                "params": params,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            self.url,
            data=body,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "Accessura-Agent-Kit-Funded-Evidence/0.6",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise ExecutionError(f"Base Sepolia RPC request failed: {exc}") from exc
        if payload.get("error"):
            raise ExecutionError(
                f"Base Sepolia RPC {method} failed: {payload['error']}"
            )
        return payload.get("result")

    def block_number(self) -> int:
        return int(self.call("eth_blockNumber", []), 16)

    def usdc_balance(self, address: str) -> int:
        data = "0x70a08231" + address[2:].lower().rjust(64, "0")
        value = self.call(
            "eth_call",
            [{"to": BASE_SEPOLIA_USDC, "data": data}, "latest"],
        )
        return int(value, 16)

    def outgoing_usdc_transfers(
        self, address: str, from_block: int, to_block: int
    ) -> list[dict[str, Any]]:
        if to_block < from_block:
            return []
        sender_topic = "0x" + address[2:].lower().rjust(64, "0")
        logs = self.call(
            "eth_getLogs",
            [{
                "address": BASE_SEPOLIA_USDC,
                "fromBlock": hex(from_block),
                "toBlock": hex(to_block),
                "topics": [TRANSFER_TOPIC, sender_topic],
            }],
        )
        return list(logs or [])

    def wait_for_receipt(
        self, tx_hash: str, timeout_seconds: int, poll_seconds: int
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            receipt = self.call("eth_getTransactionReceipt", [tx_hash])
            if receipt:
                if receipt.get("status") != "0x1":
                    raise ExecutionError("funded transaction receipt is not successful")
                return receipt
            time.sleep(poll_seconds)
        raise ExecutionError("timed out waiting for the funded transaction receipt")


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ExecutionError(f"{name} is required in the process environment")
    return value


def _positive_int_env(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ExecutionError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise ExecutionError(f"{name} must be a positive integer")
    return value


def _decimal_env(name: str, default: str) -> Decimal:
    raw = os.getenv(name, default).strip()
    try:
        return Decimal(raw)
    except InvalidOperation as exc:
        raise ExecutionError(f"{name} must be a decimal USDC amount") from exc


def require(condition: bool, message: str) -> None:
    if not condition:
        raise EvidenceError(message)


def require_nonnegative_integer(value: Any, label: str) -> int:
    text = str(value)
    require(text.isdigit(), f"{label} must be a non-negative integer string")
    return int(text)


def _first_dict_value(value: Any, keys: tuple[str, ...]) -> Any:
    if isinstance(value, dict):
        for key in keys:
            candidate = value.get(key)
            if candidate not in (None, ""):
                return candidate
        for candidate in value.values():
            found = _first_dict_value(candidate, keys)
            if found not in (None, ""):
                return found
    elif isinstance(value, list):
        for candidate in value:
            found = _first_dict_value(candidate, keys)
            if found not in (None, ""):
                return found
    return None


def _extract_id(payload: dict[str, Any], keys: tuple[str, ...], label: str) -> str:
    value = _first_dict_value(payload, keys)
    if not isinstance(value, str) or not value:
        raise ExecutionError(f"{label} was missing from the API response")
    return value


def _extract_payment_terms(payment: dict[str, Any]) -> dict[str, str]:
    accepts = payment.get("accepts")
    offer = accepts[0] if isinstance(accepts, list) and accepts else payment
    if not isinstance(offer, dict):
        raise ExecutionError("x402 preview did not contain a payment offer")
    terms = {
        "network": str(offer.get("network") or ""),
        "asset": str(offer.get("asset") or ""),
        "pay_to": str(offer.get("payTo") or offer.get("pay_to") or ""),
        "amount": str(
            offer.get("amount")
            or offer.get("maxAmountRequired")
            or offer.get("max_amount_required")
            or ""
        ),
        "resource": str(offer.get("resource") or payment.get("resource") or ""),
    }
    if not terms["amount"].isdigit():
        raise ExecutionError("x402 preview amount is not an integer base-unit string")
    return terms


def _claim_for(
    items: list[dict[str, Any]], pack_id: str, signal_id: str
) -> Optional[dict[str, Any]]:
    for item in items:
        if (
            str(item.get("pack_id") or item.get("packId") or "") == pack_id
            and str(item.get("signal_id") or item.get("signalId") or "")
            == signal_id
        ):
            return item
    return None


def _wait_for_award(
    buyer: Any,
    pack_id: str,
    signal_id: str,
    timeout_seconds: int,
    poll_seconds: int,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        buyer.settle(pack_id, signal_id)
        claim = _claim_for(buyer.get_claims(), pack_id, signal_id)
        if claim:
            return claim
        time.sleep(poll_seconds)
    raise ExecutionError("timed out waiting for the fresh awarded claim")


def _wait_for_seller_delivery(
    seller: Any,
    pack_id: str,
    signal_id: str,
    timeout_seconds: int,
    poll_seconds: int,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        delivery = _claim_for(seller.list_claims(), pack_id, signal_id)
        if delivery:
            return delivery
        time.sleep(poll_seconds)
    raise ExecutionError("timed out waiting for the Seller delivery entry")


def _wait_for_payment_status(
    buyer: Any,
    claim_id: str,
    status: int,
    timeout_seconds: int,
    poll_seconds: int,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = buyer.get_payment(claim_id)
        if last.get("_http_status") == status:
            return last
        time.sleep(poll_seconds)
    raise ExecutionError(
        f"timed out waiting for claim payment HTTP {status}; last state was "
        f"{last.get('state')!r}"
    )


def _wait_for_receipt_hash(
    buyer: Any,
    claim_id: str,
    timeout_seconds: int,
    poll_seconds: int,
) -> tuple[dict[str, Any], str]:
    deadline = time.monotonic() + timeout_seconds
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = buyer.get_transaction_receipt(claim_id)
        tx_hash = _first_dict_value(
            last, ("payment_tx_hash", "transaction_hash", "tx_hash")
        )
        if isinstance(tx_hash, str) and TX_HASH.fullmatch(tx_hash):
            return last, tx_hash
        time.sleep(poll_seconds)
    raise ExecutionError(
        "timed out waiting for the participant receipt transaction hash"
    )


def _parse_receipt_usdc_transfers(
    receipt: dict[str, Any], buyer: str
) -> list[dict[str, str]]:
    transfers: list[dict[str, str]] = []
    for log in receipt.get("logs") or []:
        topics = log.get("topics") or []
        if (
            str(log.get("address") or "").lower() != BASE_SEPOLIA_USDC.lower()
            or len(topics) < 3
            or str(topics[0]).lower() != TRANSFER_TOPIC.lower()
        ):
            continue
        sender = "0x" + str(topics[1])[-40:]
        if sender.lower() != buyer.lower():
            continue
        transfers.append({
            "tx_hash": str(log.get("transactionHash") or receipt.get("transactionHash") or ""),
            "from": sender,
            "to": "0x" + str(topics[2])[-40:],
            "asset": BASE_SEPOLIA_USDC,
            "amount": str(int(str(log.get("data") or "0x0"), 16)),
        })
    return transfers


def _mcp_claims_pay(
    buyer: Any,
    base_url: str,
    buyer_private_key: str,
    claim_id: str,
    confirm: bool,
    expected_amount: str = "",
    expected_pay_to: str = "",
) -> dict[str, Any]:
    """Call the real MCP handler with env-backed Buyer credentials."""
    os.environ["ACCESSURA_BASE_URL"] = base_url
    os.environ["ACCESSURA_PRIVATE_KEY"] = buyer_private_key
    os.environ["ACCESSURA_API_KEY"] = buyer._api_key or ""
    os.environ["ACCESSURA_TOKEN"] = buyer._token or ""

    import client_wrapper
    import server

    client_wrapper.BASE_URL = base_url
    client_wrapper.PRIVATE_KEY = buyer_private_key
    client_wrapper.API_KEY = buyer._api_key or ""
    client_wrapper.TOKEN = buyer._token or ""
    server._client = client_wrapper
    raw = asyncio.run(
        server.claims_pay.__wrapped__(
            claim_id,
            confirm_real_payment=confirm,
            expected_amount=expected_amount,
            expected_pay_to=expected_pay_to,
        )
    )
    payload = json.loads(raw)
    if payload.get("error"):
        raise ExecutionError(f"claims_pay failed: {payload['error']}")
    return payload


def validate_evidence(evidence: dict[str, Any]) -> list[str]:
    require(
        evidence.get("network") == BASE_SEPOLIA_NETWORK,
        "network must be Base Sepolia",
    )
    claim_id = evidence.get("claim_id")
    require(isinstance(claim_id, str) and claim_id, "claim_id is required")
    buyer = str(evidence.get("buyer_address") or "")
    seller = str(evidence.get("seller_payout_address") or "")
    require(bool(ADDRESS.fullmatch(buyer)), "buyer_address must be an EVM address")
    require(
        bool(ADDRESS.fullmatch(seller)),
        "seller_payout_address must be an EVM address",
    )
    require(
        buyer.lower() != seller.lower(), "Buyer and Seller addresses must differ"
    )

    passed: list[str] = []

    bid = evidence.get("bid") or {}
    require(
        require_nonnegative_integer(
            bid.get("buyer_usdc_before"), "bid buyer_usdc_before"
        )
        == require_nonnegative_integer(
            bid.get("buyer_usdc_after"), "bid buyer_usdc_after"
        ),
        "bid changed the Buyer USDC balance",
    )
    require(bid.get("transfer_count") == 0, "bid produced a token transfer")
    passed.append("bid_does_not_move_funds")

    award = evidence.get("award") or {}
    require(
        award.get("state") == "award_pending_delivery",
        "award must begin unpaid",
    )
    require(
        not award.get("payment_tx_hash"),
        "unpaid award already has a payment tx",
    )
    passed.append("award_is_unpaid")

    pre_delivery = evidence.get("pre_delivery_payment") or {}
    delivery_ready = evidence.get("delivery_ready_payment") or {}
    require(
        pre_delivery.get("http_status") == 202,
        "pre-delivery payment read must be 202",
    )
    require(
        delivery_ready.get("http_status") == 402,
        "delivery-ready payment read must be 402",
    )
    require(
        delivery_ready.get("network") == BASE_SEPOLIA_NETWORK,
        "402 network must be Base Sepolia",
    )
    require(
        str(delivery_ready.get("asset") or "").lower()
        == BASE_SEPOLIA_USDC.lower(),
        "402 asset must be Base Sepolia USDC",
    )
    require(
        str(delivery_ready.get("pay_to") or "").lower() == seller.lower(),
        "402 pay_to must equal the Seller payout address",
    )
    payment_amount = require_nonnegative_integer(
        delivery_ready.get("amount"), "delivery-ready amount"
    )
    require(payment_amount > 0, "delivery-ready amount must be positive")
    passed.append("delivery_ready_precedes_402")

    preview = evidence.get("preview") or {}
    require(
        preview.get("confirm_real_payment") is False,
        "preview must use false confirmation",
    )
    require(
        preview.get("payment_performed") is False,
        "preview reported a payment",
    )
    require(
        preview.get("transfer_count") == 0,
        "preview produced a token transfer",
    )
    require(
        str(preview.get("expected_amount") or "") == str(payment_amount),
        "preview amount binding is missing",
    )
    require(
        str(preview.get("expected_pay_to") or "").lower() == seller.lower(),
        "preview payTo binding is missing",
    )
    require(
        require_nonnegative_integer(
            preview.get("buyer_usdc_before"), "preview buyer_usdc_before"
        )
        == require_nonnegative_integer(
            preview.get("buyer_usdc_after"), "preview buyer_usdc_after"
        ),
        "preview changed the Buyer USDC balance",
    )
    passed.append("false_preview_does_not_pay")

    payment = evidence.get("payment") or {}
    require(
        payment.get("confirm_real_payment") is True,
        "funded payment must use true confirmation",
    )
    require(
        str(payment.get("expected_amount") or "") == str(payment_amount),
        "confirmed payment did not bind expected_amount",
    )
    require(
        str(payment.get("expected_pay_to") or "").lower() == seller.lower(),
        "confirmed payment did not bind expected_pay_to",
    )
    transfers = payment.get("transfers")
    require(
        isinstance(transfers, list) and len(transfers) == 1,
        "payment must produce exactly one transfer",
    )
    transfer = transfers[0]
    tx_hash = str(transfer.get("tx_hash") or "")
    require(bool(TX_HASH.fullmatch(tx_hash)), "payment tx_hash is invalid")
    require(
        str(transfer.get("from") or "").lower() == buyer.lower(),
        "transfer sender is not Buyer",
    )
    require(
        str(transfer.get("to") or "").lower() == seller.lower(),
        "transfer recipient is not Seller",
    )
    require(
        str(transfer.get("asset") or "").lower() == BASE_SEPOLIA_USDC.lower(),
        "transfer asset is not Base Sepolia USDC",
    )
    require(
        require_nonnegative_integer(transfer.get("amount"), "transfer amount")
        == payment_amount,
        "transfer amount does not match the 402 requirement",
    )
    passed.append("one_buyer_to_seller_usdc_transfer")

    platform_addresses = {
        str(address).lower()
        for address in evidence.get("platform_addresses", [])
    }
    require(
        seller.lower() not in platform_addresses,
        "Seller was listed as a platform address",
    )
    require(
        str(transfer.get("to") or "").lower() not in platform_addresses,
        "platform address received the payment",
    )
    passed.append("no_platform_recipient")

    decrypt = evidence.get("decrypt") or {}
    require(decrypt.get("local") is True, "Buyer decryption must be local")
    require(
        bool(SHA256.fullmatch(str(decrypt.get("plaintext_sha256") or ""))),
        "decrypted plaintext SHA-256 evidence is missing",
    )
    passed.append("buyer_decrypts_locally")

    retry = evidence.get("retry") or {}
    require(
        retry.get("new_transfer_count") == 0,
        "payment retry created another transfer",
    )
    require(
        str(retry.get("payment_tx_hash") or "").lower() == tx_hash.lower(),
        "payment retry did not resolve to the original transaction",
    )
    passed.append("retry_does_not_duplicate_payment")

    receipt = evidence.get("receipt") or {}
    require(receipt.get("claim_id") == claim_id, "receipt claim_id does not match")
    require(
        str(receipt.get("payment_tx_hash") or "").lower() == tx_hash.lower(),
        "receipt transaction hash does not match the on-chain transfer",
    )
    passed.append("receipt_matches_transaction_hash")

    require(
        tuple(passed) == REQUIRED_ASSERTIONS,
        "funded assertion set is incomplete",
    )
    return passed


def execute_funded_lifecycle(config: FundedConfig) -> dict[str, Any]:
    """Create one fresh Testnet lifecycle and return sanitized evidence only."""
    from accessura_sdk import BuyerAgent, SellerAgent

    rpc = RpcClient(config.rpc_url)
    buyer = BuyerAgent(
        private_key=config.buyer_private_key,
        base_url=config.base_url,
    )
    seller = SellerAgent(
        private_key=config.seller_private_key,
        base_url=config.base_url,
        delivery_secret=config.delivery_secret,
    )
    if seller.agent_id.lower() != config.seller_payout_address.lower():
        raise ExecutionError(
            "Seller private key does not control "
            "ACCESSURA_FUNDED_SELLER_PAYOUT_ADDRESS"
        )
    if buyer.agent_id.lower() == seller.agent_id.lower():
        raise ExecutionError("Buyer and Seller must use distinct identities")

    buyer.register("Agent Kit v0.6 funded validation Buyer", role="buyer")
    buyer.get_api_key()
    if not buyer._token:
        buyer.login()
    seller.register("Agent Kit v0.6 funded validation Seller", role="seller")
    seller.get_api_key()
    if not seller._token:
        seller.login()
    seller.bind_payout_wallet(BASE_SEPOLIA_NETWORK)

    topic_slug = config.topic_slug
    if not topic_slug:
        topics = seller.list_topics(state="active").get("topics") or []
        topic_slug = next(
            (
                str(topic.get("slug"))
                for topic in topics
                if topic.get("slug") and not topic.get("closed")
            ),
            "",
        )
    if not topic_slug:
        raise ExecutionError(
            "no active Topic was available; set ACCESSURA_FUNDED_TOPIC_SLUG"
        )

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    pack_response = seller.publish_pack(
        title=f"Agent Kit v0.6 funded validation {run_id}",
        info_type="text",
        topic_slugs=[topic_slug],
        fields={
            "word_count": 8,
            "source_url": "seller-local:first-party-release-validation",
            "language": "en",
        },
        signal_type="narrative-intel",
        signal_schema={"run_id": "string", "status": "string"},
        source_declaration="First-party Base Sepolia release validation",
        bid_config={
            "copies": 1,
            "window_seconds": config.window_seconds,
            "reserve_price": float(config.bid_price),
            "per_call_price": float(config.bid_price),
            "settlement_rule": "top_n_pay_as_bid",
        },
    )
    pack_id = _extract_id(
        pack_response, ("pack_id", "packId", "id"), "pack_id"
    )
    plaintext = json.dumps(
        {"run_id": run_id, "status": "funded-testnet-validation"},
        separators=(",", ":"),
    ).encode("utf-8")
    signal_response = seller.append_signal(
        pack_id=pack_id,
        label="Funded Testnet validation Signal",
        summary="First-party lifecycle evidence; no external traction claim.",
        content_text=plaintext.decode("utf-8"),
        source="seller-local",
        observed_at=datetime.now(timezone.utc).isoformat(),
        encrypt_with_managed=True,
    )
    if signal_response.get("error"):
        raise ExecutionError(
            f"fresh Signal append failed before bidding: "
            f"{signal_response['error']}"
        )
    signal_id = _extract_id(
        signal_response,
        ("_local_signal_id", "signal_id", "signalId", "id"),
        "signal_id",
    )
    ciphertext_b64 = str(
        signal_response.get("_local_ciphertext_b64") or ""
    )
    if not ciphertext_b64:
        raise ExecutionError("Seller-local ciphertext was not returned")

    bid_start_block = rpc.block_number()
    bid_balance_before = rpc.usdc_balance(buyer.agent_id)
    bid_response = buyer.bid(pack_id, signal_id, float(config.bid_price))
    if bid_response.get("error"):
        raise ExecutionError(f"bid failed: {bid_response['error']}")
    bid_balance_after = rpc.usdc_balance(buyer.agent_id)
    bid_end_block = rpc.block_number()
    bid_transfers = rpc.outgoing_usdc_transfers(
        buyer.agent_id, bid_start_block, bid_end_block
    )

    award = _wait_for_award(
        buyer,
        pack_id,
        signal_id,
        config.settle_timeout_seconds,
        config.poll_seconds,
    )
    claim_id = _extract_id(
        award, ("claim_id", "claimId", "id"), "claim_id"
    )
    award_state = str(award.get("state") or "")
    award_tx = _first_dict_value(
        award, ("payment_tx_hash", "transaction_hash", "tx_hash")
    )

    pre_delivery = buyer.get_payment(claim_id)
    seller_delivery = _wait_for_seller_delivery(
        seller,
        pack_id,
        signal_id,
        config.settle_timeout_seconds,
        config.poll_seconds,
    )
    seller.deliver_key_release(
        claim_id=claim_id,
        buyer_pubkey_hex=str(
            seller_delivery.get("buyer_encryption_pubkey")
            or seller_delivery.get("buyer_pubkey_hex")
            or ""
        ),
        ciphertext_b64=ciphertext_b64,
        pack_id=pack_id,
        signal_id=signal_id,
        buyer_agent_id=str(
            seller_delivery.get("buyer_agent_id")
            or seller_delivery.get("buyer_id")
            or buyer.agent_id
        ),
    )
    delivery_ready = _wait_for_payment_status(
        buyer,
        claim_id,
        402,
        config.settle_timeout_seconds,
        config.poll_seconds,
    )
    ready_terms = _extract_payment_terms(delivery_ready)

    preview_start_block = rpc.block_number()
    preview_balance_before = rpc.usdc_balance(buyer.agent_id)
    preview = _mcp_claims_pay(
        buyer,
        config.base_url,
        config.buyer_private_key,
        claim_id,
        confirm=False,
    )
    preview_payment = preview.get("payment_preview") or {}
    preview_terms = _extract_payment_terms(preview_payment)
    preview_balance_after = rpc.usdc_balance(buyer.agent_id)
    preview_end_block = rpc.block_number()
    preview_transfers = rpc.outgoing_usdc_transfers(
        buyer.agent_id, preview_start_block, preview_end_block
    )

    if preview_terms != ready_terms:
        raise ExecutionError(
            "claims_pay preview terms differ from the delivery-ready 402"
        )
    if preview_terms["network"] != BASE_SEPOLIA_NETWORK:
        raise ExecutionError("claims_pay preview is not Base Sepolia")
    if preview_terms["asset"].lower() != BASE_SEPOLIA_USDC.lower():
        raise ExecutionError("claims_pay preview is not Base Sepolia USDC")
    if preview_terms["pay_to"].lower() != seller.agent_id.lower():
        raise ExecutionError("claims_pay preview recipient is not the Seller")

    paid = _mcp_claims_pay(
        buyer,
        config.base_url,
        config.buyer_private_key,
        claim_id,
        confirm=True,
        expected_amount=preview_terms["amount"],
        expected_pay_to=preview_terms["pay_to"],
    )
    paid_tx = _first_dict_value(
        paid, ("payment_tx_hash", "transaction_hash", "tx_hash")
    )
    participant_receipt, receipt_tx = _wait_for_receipt_hash(
        buyer,
        claim_id,
        config.settle_timeout_seconds,
        config.poll_seconds,
    )
    tx_hash = str(paid_tx or receipt_tx)
    if not TX_HASH.fullmatch(tx_hash):
        raise ExecutionError("confirmed payment did not return a transaction hash")
    if tx_hash.lower() != receipt_tx.lower():
        raise ExecutionError(
            "confirmed payment and participant receipt transaction hashes differ"
        )

    chain_receipt = rpc.wait_for_receipt(
        tx_hash,
        config.settle_timeout_seconds,
        config.poll_seconds,
    )
    transfers = _parse_receipt_usdc_transfers(
        chain_receipt, buyer.agent_id
    )
    paid_delivery = _wait_for_payment_status(
        buyer,
        claim_id,
        200,
        config.settle_timeout_seconds,
        config.poll_seconds,
    )
    decrypted = buyer.decrypt_paid_claim(claim_id)
    if decrypted != plaintext:
        raise ExecutionError("Buyer-local decrypted plaintext did not match")

    retry_start_block = rpc.block_number()
    retry = _mcp_claims_pay(
        buyer,
        config.base_url,
        config.buyer_private_key,
        claim_id,
        confirm=True,
        expected_amount=preview_terms["amount"],
        expected_pay_to=preview_terms["pay_to"],
    )
    retry_tx = _first_dict_value(
        retry, ("payment_tx_hash", "transaction_hash", "tx_hash")
    ) or _first_dict_value(
        paid_delivery, ("payment_tx_hash", "transaction_hash", "tx_hash")
    ) or tx_hash
    retry_end_block = rpc.block_number()
    original_block = int(str(chain_receipt.get("blockNumber") or "0x0"), 16)
    retry_logs = rpc.outgoing_usdc_transfers(
        buyer.agent_id,
        min(original_block, retry_start_block),
        retry_end_block,
    )
    new_retry_logs = [
        log
        for log in retry_logs
        if str(log.get("transactionHash") or "").lower() != tx_hash.lower()
    ]

    evidence: dict[str, Any] = {
        "schema_version": "accessura-funded-base-sepolia-evidence/v1",
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "network": BASE_SEPOLIA_NETWORK,
        "claim_id": claim_id,
        "pack_id": pack_id,
        "signal_id": signal_id,
        "buyer_address": buyer.agent_id,
        "seller_payout_address": seller.agent_id,
        "platform_addresses": list(config.platform_addresses),
        "bid": {
            "buyer_usdc_before": str(bid_balance_before),
            "buyer_usdc_after": str(bid_balance_after),
            "transfer_count": len(bid_transfers),
        },
        "award": {
            "state": award_state,
            "payment_tx_hash": award_tx,
        },
        "pre_delivery_payment": {
            "http_status": pre_delivery.get("_http_status"),
        },
        "delivery_ready_payment": {
            "http_status": delivery_ready.get("_http_status"),
            **ready_terms,
        },
        "preview": {
            "confirm_real_payment": False,
            "payment_performed": preview.get("payment_performed"),
            "expected_amount": preview_terms["amount"],
            "expected_pay_to": preview_terms["pay_to"],
            "buyer_usdc_before": str(preview_balance_before),
            "buyer_usdc_after": str(preview_balance_after),
            "transfer_count": len(preview_transfers),
        },
        "payment": {
            "confirm_real_payment": True,
            "expected_amount": preview_terms["amount"],
            "expected_pay_to": preview_terms["pay_to"],
            "transfers": transfers,
        },
        "decrypt": {
            "local": True,
            "plaintext_sha256": hashlib.sha256(decrypted).hexdigest(),
        },
        "retry": {
            "payment_tx_hash": str(retry_tx),
            "new_transfer_count": len(new_retry_logs),
        },
        "receipt": {
            "claim_id": str(
                _first_dict_value(
                    participant_receipt, ("claim_id", "claimId")
                )
                or claim_id
            ),
            "payment_tx_hash": receipt_tx,
        },
    }
    passed = validate_evidence(evidence)
    evidence["funded_testnet_result"] = "passed"
    evidence["funded_testnet_tx"] = tx_hash
    evidence["funded_testnet_amount_base_units"] = preview_terms["amount"]
    evidence["assertion_results"] = {name: name in passed for name in REQUIRED_ASSERTIONS}
    evidence["real_payment_performed_by_this_script"] = True
    return evidence


def _validated_summary(payload: dict[str, Any]) -> dict[str, Any]:
    passed = validate_evidence(payload)
    transfer = (payload.get("payment") or {}).get("transfers", [{}])[0]
    return {
        "verified": True,
        "network": payload["network"],
        "claim_id": payload["claim_id"],
        "funded_testnet_tx": transfer.get("tx_hash"),
        "funded_testnet_amount_base_units": transfer.get("amount"),
        "assertion_results": {name: name in passed for name in REQUIRED_ASSERTIONS},
        "real_payment_performed_by_this_script": False,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run or validate Accessura funded Base Sepolia evidence"
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--execute",
        action="store_true",
        help=(
            "perform the explicitly authorized one-payment Base Sepolia "
            "lifecycle using environment-only credentials"
        ),
    )
    mode.add_argument(
        "--validate",
        type=Path,
        metavar="EVIDENCE_JSON",
        help="validate a sanitized evidence JSON file without loading credentials",
    )
    return parser


def _redact_error(message: str) -> str:
    redacted = message
    for name in (
        "ACCESSURA_FUNDED_BUYER_PRIVATE_KEY",
        "ACCESSURA_FUNDED_SELLER_PRIVATE_KEY",
        "ACCESSURA_DELIVERY_SECRET",
    ):
        value = os.getenv(name, "")
        if value:
            redacted = redacted.replace(value, "<redacted>")
    return redacted


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.execute:
            result = execute_funded_lifecycle(FundedConfig())
        else:
            payload = json.loads(args.validate.read_text(encoding="utf-8"))
            result = _validated_summary(payload)
    except Exception as exc:
        print(json.dumps({
            "verified": False,
            "error": _redact_error(str(exc)),
            "real_payment_performed_by_this_script": (
                "unknown; inspect chain and participant receipt before retry"
                if args.execute
                else False
            ),
        }, indent=2))
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
