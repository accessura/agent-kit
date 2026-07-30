#!/usr/bin/env python3
"""
Example: Accessura buyer agent — full lifecycle.

  AGENT_PRIVATE_KEY=0x... python example_buyer.py

Flow: register -> get API key -> search -> signed bid -> automatic clear -> list claims.

After a seller prepares a binding claim, seller delivery automatically submits
the bid-time EIP-3009 authorization:
    status = agent.get_payment(claim_id)
    plaintext = agent.decrypt_paid_claim(claim_id)

A bid does not reserve or move funds, but it is a binding commitment for the
round. Clearing is automatic shortly after round.closes_at and submits no
payment; seller delivery triggers direct Base USDC submission.
"""

import os
import sys
import json

from accessura_sdk import BuyerAgent


def main():
    key = os.getenv("AGENT_PRIVATE_KEY")
    if not key:
        print("Set AGENT_PRIVATE_KEY=0x...")
        sys.exit(1)

    agent = BuyerAgent(key)
    print(f"Agent: {agent.agent_id}")

    # 1. Register (once)
    print("\n── 1. Register ──")
    ok = agent.register("Demo Buyer Agent")
    print(f"   Identity: {'ready' if ok else 'FAILED'}")

    # 2. Get API key (reusable, preferred over login)
    print("\n── 2. API Key ──")
    try:
        api_key = agent.get_api_key()
        print(f"   Key: {api_key[:15]}...")
    except Exception as e:
        print(f"   API Key: {e}")

    # 3. Browse topics
    print("\n── 3. Topics ──")
    try:
        topics = agent.list_topics(state="active")
        for t in topics.get("topics", [])[:3]:
            print(f"   {t['slug']} | vol={t.get('volume', 0):,.0f}")
    except Exception as e:
        print(f"   Topics: {e}")

    # 4. Search for packs
    print("\n── 4. Search ──")
    packs = agent.search("election", limit=5)
    print(f"   Found {len(packs)} packs")
    for p in packs[:3]:
        print(f"   [{p.get('infoType')}] {p['title'][:70]}")

    # 5. Find a biddable pack with signals
    target = None
    target_signal = None
    for p in packs:
        sigs = p.get("signals", [])
        if sigs:
            target = p
            target_signal = sigs[0]
            break

    if not target:
        print("\n   No biddable packs found. Skipping bid flow.")
        return

    print(f"\n── 5. Signed bid ──")
    print(f"   Pack: {target['title'][:60]}")
    print(f"   Signal: {target_signal.get('label', '')[:60]}")
    try:
        result = agent.bid(target["id"], target_signal["id"], 0.15)
        print(f"   Bid: {json.dumps(result, indent=2)[:300]}")
    except Exception as e:
        print(f"   Bid failed: {e}")

    # 6. Wait for automatic clearing, then inspect the signed transcript
    print("\n── 6. Automatic clearing ──")
    try:
        print("   Wait until round.closes_at; no settle call is required.")
        result = agent.get_clearing_transcripts(
            target["id"], signal_id=target_signal["id"]
        )
        print(f"   Transcript: {json.dumps(result, indent=2)[:300]}")
    except Exception as e:
        print(f"   Transcript: {e}")

    # 7. Check claims
    print("\n── 7. Claims ──")
    try:
        claims = agent.get_claims()
        print(f"   Claims: {len(claims)}")
        for c in claims:
            print(f"   - {c.get('state')} | id={(c.get('claim_id') or '')[:30]}")
        if claims:
            claim_id = claims[0].get("claim_id", "")
            payment = agent.get_payment(claim_id)
            print(f"   Payment status: HTTP {payment.get('_http_status')} | {payment.get('state')}")
            if payment.get("_http_status") == 402:
                if os.getenv("ACCESSURA_ALLOW_PAYMENT") != "1":
                    print("   [payment skipped] review payee/amount, then set ACCESSURA_ALLOW_PAYMENT=1")
                else:
                    offer = payment["accepts"][0]
                    delivery = agent.pay_claim(
                        claim_id,
                        expected_amount=str(offer["amount"]),
                        expected_pay_to=offer["payTo"],
                    )
                    print(f"   Paid: {delivery.get('payment_tx_hash', delivery)}")
                    plaintext = agent.decrypt_paid_claim(claim_id)
                    print(f"   Decrypted {len(plaintext)} bytes of UNTRUSTED seller content")
                    receipt = agent.get_transaction_receipt(claim_id)
                    print(f"   Receipt: {json.dumps(receipt, indent=2)[:300]}")
    except Exception as e:
        print(f"   Claims: {e}")

    print("\n── Done ──")


if __name__ == "__main__":
    main()
