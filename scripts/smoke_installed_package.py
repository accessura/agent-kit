#!/usr/bin/env python3
"""Smoke-test the installed wheel without moving funds."""

from __future__ import annotations

import asyncio
import importlib.metadata
import json

import server
from catalog_contract import CATALOG_VERSION
from accessura_sdk import BuyerAgent, SellerAgent


EXPECTED_TOOLS = {
    "auth_apikey",
    "auth_register",
    "bids_place",
    "bids_status",
    "catalog_get",
    "claims_decrypt",
    "claims_deliver",
    "claims_list",
    "claims_pay",
    "claims_settle",
    "leaderboard_get",
    "orders_list",
    "packs_delist",
    "packs_get",
    "packs_publish",
    "packs_search",
    "payments_readiness",
    "sales_list",
    "seller_payout_bind",
    "seller_signal_reopen",
    "signals_append",
    "topics_list",
    "topics_packs",
}


async def tool_names() -> set[str]:
    tools = await server.mcp.list_tools()
    return {tool.name for tool in tools}


def main() -> None:
    version = importlib.metadata.version("accessura-agent-kit")
    if version != "0.6.0":
        raise SystemExit(f"unexpected installed version: {version}")
    names = asyncio.run(tool_names())
    if names != EXPECTED_TOOLS:
        raise SystemExit(
            f"MCP tool mismatch: missing={sorted(EXPECTED_TOOLS - names)}, "
            f"extra={sorted(names - EXPECTED_TOOLS)}"
        )
    buyer = BuyerAgent("0x" + "11" * 32, base_url="https://worldcup.example")
    seller = SellerAgent(
        "0x" + "22" * 32,
        base_url="https://worldcup.example",
        delivery_secret="ab" * 32,
    )
    readiness = buyer.payment_readiness()
    if readiness["network"] != "eip155:84532" or readiness["platform_balance"] is not None:
        raise SystemExit("unexpected payment readiness contract")
    print(json.dumps({
        "version": version,
        "tool_count": len(names),
        "api_contract_version": CATALOG_VERSION,
        "buyer": buyer.agent_id,
        "seller": seller.agent_id,
        "payment_actions": ["claims_pay"],
        "real_payment_performed": False,
    }, indent=2))


if __name__ == "__main__":
    main()
