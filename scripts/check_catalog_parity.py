#!/usr/bin/env python3
import asyncio
import json
import os

import httpx
from catalog_contract import CATALOG_VERSION, assert_catalog_parity


async def main():
    base = os.getenv("ACCESSURA_BASE_URL", "https://worldcup-direct-testnet.accessuraportal.com").rstrip("/")
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(f"{base}/api/v1/catalog")
        response.raise_for_status()
    assert_catalog_parity(response.json())
    print(json.dumps({"verified": True, "api_contract_version": CATALOG_VERSION, "payment_performed": False}))


if __name__ == "__main__":
    asyncio.run(main())
