"""
Accessura SDK — Python client for the Accessura data marketplace.

    pip install httpx cryptography eth-account

    from accessura_sdk import BuyerAgent, SellerAgent

    # Buyer
    agent = BuyerAgent(private_key="0x...")
    agent.register("My Agent")
    agent.get_api_key()
    packs = agent.search("Norway")

    # Seller
    seller = SellerAgent(private_key="0x...")
    seller.register("My Seller", role="seller")
    seller.publish_pack(title="Hook Title", info_type="text", ...)

Crypto utilities (standalone, pure functions):
    from accessura_sdk.crypto import decrypt_delivery, seller_wrap_pre_encrypted_dek
"""

from .client import BuyerAgent, SellerAgent
from . import crypto

__all__ = ["BuyerAgent", "SellerAgent", "crypto"]
