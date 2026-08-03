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

import os
import secrets
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Optional, Union

# ── Re-export canonical public surface ────────────────────────────────────
from .errors import (
    AccessuraAuthError,
    AccessuraCryptoError,
    AccessuraError,
    AccessuraErrorCode,
    AccessuraNetworkError,
    AccessuraPaymentError,
    AccessuraValidationError,
)
from .crypto import (
    decrypt_delivery,
    derive_signal_dek,
    encrypt_signal_content,
    normalize_delivery_secret,
    seller_wrap_pre_encrypted_dek,
)

# ── Internal modules ──────────────────────────────────────────────────────
from ._http import _request, _request_response
from ._signing import (
    DEFAULT_X402_NETWORK,
    PROTOCOL_DOMAIN,
    _attach_payment_risk_warnings,
    _binding_bid_sla_risk_warnings,
    _sig_hex,
    _sign_auth_challenge,
    _sign_bid_authorization,
    _sign_bid_payment_authorization,
    _sign_x402_payment,
    _usdc_price_base_units,
)
from ._payment_controls import (
    _enforce_payment_controls,
    _load_payment_controls_sync,
    _payment_readiness,
    _public_payment_controls,
)
from ._base import SharedBaseAgent


# ═══════════════════════════════════════════════════════════════════════════
# Polling helpers (inspired by @beep-it/sdk-core waitForPaymentCompletion)
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class PollOptions:
    """Configuration for a polling loop.

    Inspired by beep-sdk's polling helpers with exponential backoff.
    """
    interval_ms: int = 15_000
    timeout_ms: int = 300_000
    on_update: Any = None   # callable(response_dict) -> None
    on_error: Any = None    # callable(error) -> None

    _BACKOFF_MULTIPLIER: float = field(default=1.5, repr=False, compare=False)
    _MAX_INTERVAL_MS: int = field(default=60_000, repr=False, compare=False)


def _is_transient_http_status(status: int) -> bool:
    """Return True for status codes that should trigger a backoff retry."""
    if status >= 500:
        return True
    if status == 429:
        return True
    return False


def _poll_sleep(interval_ms: int) -> None:
    time.sleep(interval_ms / 1000.0)


def _poll_backoff(current_ms: int, options: PollOptions) -> int:
    """Apply exponential backoff, capped at MAX_INTERVAL_MS."""
    return min(int(current_ms * options._BACKOFF_MULTIPLIER + 0.5),
               options._MAX_INTERVAL_MS)


# ═══════════════════════════════════════════════════════════════════════════
# BuyerAgent (EIP-712 self-custody)
# ═══════════════════════════════════════════════════════════════════════════

class BuyerAgent(SharedBaseAgent):
    """Secp256k1-keyed buyer agent. EIP-712 auth, full trading lifecycle."""

    def __init__(self, private_key: str,
                 base_url: str = "https://testnet.accessura.io",
                 api_key: Optional[str] = None,
                 token: Optional[str] = None):
        super().__init__(private_key, base_url=base_url,
                         api_key=api_key, token=token)
        self._payment_authority_lock = threading.RLock()

    # ── registration helpers ──────────────────────────────────────────

    def register(self, name: str = "Accessura Agent",
                 role: str = "buyer") -> bool:
        return super().register(name, role)

    # ── buyer-specific discovery ──────────────────────────────────────

    def get_catalog(self) -> dict:
        return _request("GET", f"{self.api}/catalog", {})

    def get_leaderboard(self, limit: int = 20) -> dict:
        return _request("GET",
                        f"{self.api}/leaderboard?limit={limit}", {})

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

    def wait_for_bid_settled(
        self,
        pack_id: str,
        signal_id: str = "",
        *,
        interval_ms: int = 15_000,
        timeout_ms: int = 300_000,
        on_update: Any = None,
        on_error: Any = None,
    ) -> dict:
        """Poll ``get_bid_status`` until the round settles or times out.

        Returns the final status dict. The caller should inspect
        ``status["status"]`` to determine the outcome.

        Uses exponential backoff: transient errors multiply the interval by
        1.5 (capped at 60 s); successful round-trips reset to the base
        interval.
        """
        options = PollOptions(
            interval_ms=interval_ms,
            timeout_ms=timeout_ms,
            on_update=on_update,
            on_error=on_error,
        )
        deadline = time.monotonic() + timeout_ms / 1000.0
        current_interval = options.interval_ms
        last: dict = {}

        while time.monotonic() < deadline:
            try:
                last = self.get_bid_status(pack_id, signal_id)
                status_val = last.get("status", "")
                if status_val in (
                    "won", "lost", "cleared", "settled",
                    "cancelled", "expired", "rejected",
                ):
                    return last
                if options.on_update:
                    options.on_update(last)
                current_interval = options.interval_ms
            except Exception as exc:
                if options.on_error:
                    options.on_error(exc)
                if isinstance(exc, (AccessuraError, RuntimeError)):
                    return last or {"status": "poll_error", "error": str(exc)}
                current_interval = _poll_backoff(current_interval, options)

            _poll_sleep(current_interval)

        return last or {"status": "poll_timeout",
                        "error": "bid status poll timed out"}

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
                raise AccessuraPaymentError(
                    f"x402 payment failed: HTTP {paid_status} {paid}",
                    code=AccessuraErrorCode.PAYMENT_FAILED,
                    status_code=paid_status)
            return {**paid, "_http_status": paid_status}

    def fetch_paid_ciphertext(self, delivery: dict) -> str:
        """Fetch opaque ciphertext from a paid direct-delivery response."""
        url = delivery.get("ciphertext_url")
        if not isinstance(url, str) or not url:
            raise AccessuraNetworkError(
                "paid delivery did not include ciphertext_url",
                code=AccessuraErrorCode.CIPHERTEXT_FETCH_FAILED)
        target = urllib.parse.urlsplit(url)
        api_origin = urllib.parse.urlsplit(self.api)
        same_origin = (target.scheme, target.netloc) == (api_origin.scheme, api_origin.netloc)
        status, _, body = _request_response("GET", url, self._auth() if same_origin else {})
        if status != 200 or not isinstance(body.get("ciphertext_b64"), str):
            raise AccessuraNetworkError(
                f"ciphertext fetch failed: HTTP {status} {body}",
                code=AccessuraErrorCode.CIPHERTEXT_FETCH_FAILED,
                status_code=status)
        return body["ciphertext_b64"]

    def decrypt_paid_claim(self, claim_id: str) -> bytes:
        """Retrieve an already-paid delivery and decrypt it locally."""
        delivery = self.get_payment(claim_id)
        if delivery.get("_http_status") != 200:
            raise AccessuraPaymentError(
                f"claim is not paid_delivered: {delivery}",
                code=AccessuraErrorCode.DELIVERY_NOT_PAID)
        broker = delivery.get("platform_broker")
        if not isinstance(broker, dict):
            raise AccessuraPaymentError(
                "paid delivery did not include platform_broker",
                code=AccessuraErrorCode.PAYMENT_REQUIRED_MALFORMED)
        return self.decrypt(broker, self.fetch_paid_ciphertext(delivery))

    def decrypt(self, broker: dict, ciphertext_b64: str) -> bytes:
        return decrypt_delivery(broker, ciphertext_b64, self.private_key)

    def wait_for_claim_paid(
        self,
        claim_id: str,
        *,
        expected_amount: Optional[str] = None,
        expected_pay_to: Optional[str] = None,
        interval_ms: int = 15_000,
        timeout_ms: int = 300_000,
        on_update: Any = None,
        on_error: Any = None,
    ) -> dict:
        """Poll ``get_payment`` until the claim is paid or times out.

        When the claim reaches a payable state (HTTP 402), this method
        automatically calls ``pay_claim`` with the bound
        ``expected_amount`` / ``expected_pay_to`` if provided.

        Returns the final payment dict with ``_http_status``.
        """
        options = PollOptions(
            interval_ms=interval_ms,
            timeout_ms=timeout_ms,
            on_update=on_update,
            on_error=on_error,
        )
        deadline = time.monotonic() + timeout_ms / 1000.0
        current_interval = options.interval_ms
        last: dict = {}

        while time.monotonic() < deadline:
            try:
                last = self.get_payment(claim_id)
                http_status = last.get("_http_status")

                if http_status in (200, 202):
                    return last

                if http_status == 402:
                    if expected_amount is not None or expected_pay_to is not None:
                        last = self.pay_claim(
                            claim_id,
                            expected_amount=expected_amount,
                            expected_pay_to=expected_pay_to,
                        )
                        if last.get("_http_status") in (200, 202):
                            return last
                        paid_status = last.get("_http_status", 0)
                        if _is_transient_http_status(paid_status):
                            if options.on_error:
                                options.on_error(
                                    AccessuraPaymentError(
                                        f"auto-pay returned HTTP {paid_status}; retrying",
                                        code=AccessuraErrorCode.PAYMENT_FAILED,
                                        status_code=paid_status,
                                    ))
                            current_interval = _poll_backoff(current_interval, options)
                            _poll_sleep(current_interval)
                            continue
                        return last
                    return last

                if options.on_update:
                    options.on_update(last)
                current_interval = options.interval_ms
            except Exception as exc:
                if options.on_error:
                    options.on_error(exc)
                if isinstance(exc, (AccessuraError, RuntimeError)):
                    return last or {"_http_status": 0, "error": str(exc)}
                current_interval = _poll_backoff(current_interval, options)

            _poll_sleep(current_interval)

        return last or {"_http_status": 0,
                        "error": "claim payment poll timed out"}


# ═══════════════════════════════════════════════════════════════════════════
# SellerAgent (EIP-712 self-custody seller)
# ═══════════════════════════════════════════════════════════════════════════

class SellerAgent(SharedBaseAgent):
    """Secp256k1-keyed seller agent. EIP-712 auth, publish + deliver."""

    def __init__(self, private_key: str,
                 base_url: str = "https://testnet.accessura.io",
                 delivery_secret: Optional[Union[str, bytes]] = None,
                 api_key: Optional[str] = None,
                 token: Optional[str] = None):
        super().__init__(private_key, base_url=base_url,
                         api_key=api_key, token=token)
        self.delivery_secret = delivery_secret or os.getenv("ACCESSURA_DELIVERY_SECRET", "")

    def _require_delivery_secret(self) -> bytes:
        if not self.delivery_secret:
            raise AccessuraCryptoError(
                "managed seller encryption requires a dedicated 32-byte "
                "ACCESSURA_DELIVERY_SECRET; never reuse the wallet private key",
                code=AccessuraErrorCode.KEY_MATERIAL_INVALID)
        secret = normalize_delivery_secret(self.delivery_secret)
        wallet_hex = self.private_key[2:] if self.private_key.lower().startswith("0x") else self.private_key
        if secret == bytes.fromhex(wallet_hex):
            raise AccessuraCryptoError(
                "ACCESSURA_DELIVERY_SECRET must not equal the wallet private key",
                code=AccessuraErrorCode.KEY_MATERIAL_INVALID)
        return secret

    # ── registration helpers ──────────────────────────────────────────

    def register(self, name: str = "Accessura Seller",
                 role: str = "seller") -> bool:
        return super().register(name, role)

    # ── publishing ────────────────────────────────────────────────────

    def bind_payout_wallet(self, chain: str = DEFAULT_X402_NETWORK) -> dict:
        """Prove and bind this seller's self-custodied direct-payment wallet."""
        challenge_result = _request(
            "POST", f"{self.api}/sellers/payout-wallet/challenge", self._auth(),
            {"payout_address": self._account.address, "chain": chain})
        challenge = challenge_result.get("challenge") or {}
        payload = challenge.get("sign_payload")
        if not payload:
            raise AccessuraAuthError(
                f"seller payout challenge failed: {challenge_result}",
                code=AccessuraErrorCode.PAYOUT_BIND_FAILED)
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
            raise AccessuraValidationError(
                "fields must be a JSON object",
                code=AccessuraErrorCode.INVALID_PARAMETER)
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
            raise AccessuraValidationError(
                "status must be active or paused",
                code=AccessuraErrorCode.INVALID_PARAMETER)
        if sla_seconds is not None and (
            isinstance(sla_seconds, bool)
            or not isinstance(sla_seconds, int)
            or not 30 <= sla_seconds <= 86_400
        ):
            raise AccessuraValidationError(
                "sla_seconds must be an integer from 30 to 86400",
                code=AccessuraErrorCode.INVALID_PARAMETER)
        if not normalized_status and sla_seconds is None:
            raise AccessuraValidationError(
                "status or sla_seconds required",
                code=AccessuraErrorCode.MISSING_PARAMETER)
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

        import time as _time
        signal.setdefault("id",
                          f"sig-{int(_time.time() * 1000)}-{secrets.token_hex(4)}")

        if encrypt_with_managed and content_text:
            signal["content_b64"] = encrypt_signal_content(
                content_text.encode("utf-8"), self._require_delivery_secret(),
                pack_id, signal["id"])
        elif content_b64:
            signal["content_b64"] = content_b64

        result = _request(
            "POST", f"{self.api}/packs/{pack_id}/signals",
            self._auth(), signal)
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
            raise AccessuraValidationError(
                "dek required (or provide pack_id+signal_id for managed DEK)",
                code=AccessuraErrorCode.MISSING_PARAMETER)
        if isinstance(dek, str):
            dek = bytes.fromhex(dek[2:] if dek.startswith("0x") else dek)

        buyer_agent_id = kwargs.get("buyer_agent_id")
        if not buyer_agent_id:
            raise AccessuraValidationError(
                "buyer_agent_id required for claim-bound delivery",
                code=AccessuraErrorCode.MISSING_PARAMETER)
        if kwargs.get("aad") is not None or kwargs.get("wrap_aad") is not None:
            raise AccessuraValidationError(
                "deliver_key_release derives AAD binding internally; do not pass aad or wrap_aad",
                code=AccessuraErrorCode.INVALID_PARAMETER)

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
