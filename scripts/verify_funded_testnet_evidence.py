#!/usr/bin/env python3
"""Verify a sanitized funded Base Sepolia lifecycle evidence record.

This script is read-only. It never loads keys, calls Accessura, signs a
transaction, or submits a payment.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


BASE_SEPOLIA_USDC = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
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
TX_HASH = re.compile(r"^0x[0-9a-fA-F]{64}$")
SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")


class EvidenceError(ValueError):
    """The sanitized lifecycle evidence does not prove a release assertion."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise EvidenceError(message)


def require_nonnegative_integer(value: Any, label: str) -> int:
    text = str(value)
    require(text.isdigit(), f"{label} must be a non-negative integer string")
    return int(text)


def validate_evidence(evidence: dict[str, Any]) -> list[str]:
    require(evidence.get("network") == "eip155:84532", "network must be Base Sepolia")
    claim_id = evidence.get("claim_id")
    require(isinstance(claim_id, str) and claim_id, "claim_id is required")
    buyer = str(evidence.get("buyer_address") or "")
    seller = str(evidence.get("seller_payout_address") or "")
    require(bool(ADDRESS.fullmatch(buyer)), "buyer_address must be an EVM address")
    require(bool(ADDRESS.fullmatch(seller)), "seller_payout_address must be an EVM address")
    require(buyer.lower() != seller.lower(), "Buyer and Seller addresses must differ")

    passed: list[str] = []

    bid = evidence.get("bid") or {}
    require(
        require_nonnegative_integer(bid.get("buyer_usdc_before"), "bid buyer_usdc_before")
        == require_nonnegative_integer(bid.get("buyer_usdc_after"), "bid buyer_usdc_after"),
        "bid changed the Buyer USDC balance",
    )
    require(bid.get("transfer_count") == 0, "bid produced a token transfer")
    passed.append("bid_does_not_move_funds")

    award = evidence.get("award") or {}
    require(award.get("state") == "award_pending_delivery", "award must begin unpaid")
    require(not award.get("payment_tx_hash"), "unpaid award already has a payment tx")
    passed.append("award_is_unpaid")

    pre_delivery = evidence.get("pre_delivery_payment") or {}
    delivery_ready = evidence.get("delivery_ready_payment") or {}
    require(pre_delivery.get("http_status") == 202, "pre-delivery payment read must be 202")
    require(delivery_ready.get("http_status") == 402, "delivery-ready payment read must be 402")
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
    require(preview.get("confirm_real_payment") is False, "preview must use false confirmation")
    require(preview.get("payment_performed") is False, "preview reported a payment")
    require(preview.get("transfer_count") == 0, "preview produced a token transfer")
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
    require(payment.get("confirm_real_payment") is True, "funded payment must use true confirmation")
    transfers = payment.get("transfers")
    require(isinstance(transfers, list) and len(transfers) == 1, "payment must produce exactly one transfer")
    transfer = transfers[0]
    tx_hash = str(transfer.get("tx_hash") or "")
    require(bool(TX_HASH.fullmatch(tx_hash)), "payment tx_hash is invalid")
    require(str(transfer.get("from") or "").lower() == buyer.lower(), "transfer sender is not Buyer")
    require(str(transfer.get("to") or "").lower() == seller.lower(), "transfer recipient is not Seller")
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
        str(address).lower() for address in evidence.get("platform_addresses", [])
    }
    require(seller.lower() not in platform_addresses, "Seller was listed as a platform address")
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
    require(retry.get("new_transfer_count") == 0, "payment retry created another transfer")
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

    require(tuple(passed) == REQUIRED_ASSERTIONS, "funded assertion set is incomplete")
    return passed


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate sanitized Accessura funded Testnet lifecycle evidence"
    )
    parser.add_argument("evidence", type=Path, help="Path to the sanitized JSON evidence file")
    args = parser.parse_args()
    payload = json.loads(args.evidence.read_text(encoding="utf-8"))
    passed = validate_evidence(payload)
    print(json.dumps({
        "verified": True,
        "network": payload["network"],
        "claim_id": payload["claim_id"],
        "assertions": passed,
        "real_payment_performed_by_this_script": False,
    }, indent=2))


if __name__ == "__main__":
    main()
