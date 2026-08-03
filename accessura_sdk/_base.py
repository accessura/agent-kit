"""Shared base class for BuyerAgent and SellerAgent.

Provides the common keypair setup, authentication (register/login/get_api_key),
and discovery methods (search, list_topics, get_pack, etc.) that are identical
between buyer and seller agents.
"""

import urllib.parse
from typing import Any, Optional

from .errors import (
    AccessuraAuthError,
    AccessuraErrorCode,
)
from ._http import _request
from ._signing import (
    PROTOCOL_DOMAIN,
    _sig_hex,
    _sign_auth_challenge,
)


class SharedBaseAgent:
    """Common keypair setup, auth, and discovery methods."""

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
            raise AccessuraAuthError(
                "Bearer token required; run login() or get_api_key() first",
                code=AccessuraErrorCode.TOKEN_REQUIRED)
        return {"Authorization": f"Bearer {self._token}"}

    def register(self, name: str, role: str) -> bool:
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
            raise AccessuraAuthError(
                f"register_identity failed: {post_error}",
                code=AccessuraErrorCode.REGISTER_FAILED) from post_error
        raise AccessuraAuthError(
            f"register_identity failed: {r.get('error') or r}",
            code=AccessuraErrorCode.REGISTER_FAILED)

    def login(self):
        from eth_account.messages import encode_typed_data

        r = _request("POST", f"{self.api}/auth/token", {},
                     {"agent_id": self.agent_id, "action": "challenge"})
        ch = r.get("challenge")
        if not ch:
            raise AccessuraAuthError(
                f"auth challenge failed (did register() succeed?): {r.get('error') or r}",
                code=AccessuraErrorCode.AUTH_CHALLENGE_FAILED)
        sig = _sign_auth_challenge(self._account, ch["sign_payload"])
        r2 = _request("POST", f"{self.api}/auth/token", {},
                      {"agent_id": self.agent_id,
                       "challenge_id": ch["challenge_id"],
                       "signature": sig})
        if not r2.get("token"):
            raise AccessuraAuthError(
                f"token exchange failed: {r2.get('error') or r2}",
                code=AccessuraErrorCode.TOKEN_EXCHANGE_FAILED)
        self._token = r2["token"]

    def get_api_key(self) -> str:
        """Challenge -> sign -> exchange. Returns 'acc_...' and stores it."""
        from eth_account.messages import encode_typed_data

        r = _request("POST", f"{self.api}/auth/apikey", {},
                     {"agent_id": self.agent_id, "action": "challenge"})
        ch = r.get("challenge")
        if not ch:
            raise AccessuraAuthError(
                f"apikey challenge failed: {r.get('error') or r}",
                code=AccessuraErrorCode.AUTH_CHALLENGE_FAILED)
        sig = _sign_auth_challenge(self._account, ch["sign_payload"])
        out = _request("POST", f"{self.api}/auth/apikey", {},
                       {"agent_id": self.agent_id,
                        "challenge_id": ch["challenge_id"],
                        "signature": sig, "action": "exchange"})
        api_key = out.get("api_key")
        if not api_key:
            raise AccessuraAuthError(
                f"apikey exchange failed: {out.get('error') or out}",
                code=AccessuraErrorCode.APIKEY_EXCHANGE_FAILED)
        self._api_key = api_key
        if out.get("token"):
            self._token = out["token"]
        elif not self._token:
            # Cache an immediate Bearer too: /claims is Bearer-only.
            try:
                self.login()
            except Exception:
                pass
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

    def get_transaction_receipt(self, claim_id: str) -> dict:
        """Read secret-free direct transaction evidence as a participant."""
        return _request(
            "GET",
            f"{self.api}/transactions/{urllib.parse.quote(claim_id)}/receipt",
            self._auth())
