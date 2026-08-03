"""Structured error taxonomy for the Accessura SDK.

Inspired by @beep-it/sdk-core's namespaced BeepErrorCode enum and typed
error hierarchy. Each error carries a machine-readable ``code`` so agent
code (and the MCP ``@safe`` decorator) can distinguish a payment failure
from an auth failure without regex-matching human-readable strings.

    try:
        agent.bid(pack_id, signal_id, price)
    except AccessuraPaymentError as e:
        print(e.code)   # PAY_3008 (BUDGET_EXCEEDED) vs PAY_3002 (BID_TERMS_INVALID)
        print(e.message)  # Human-readable message

Errors remain compatible with the existing ``RuntimeError``-based code:
every ``AccessuraError`` is an ``Exception``, so existing ``except Exception``
blocks continue to catch them. The ``@safe`` decorator in server.py reads the
``code`` attribute and includes it in the MCP JSON error response.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional


class AccessuraErrorCode(str, Enum):
    """Namespaced error codes for machine-readable error discrimination.

    Pattern: ``<DOMAIN>_<NUMBER>``. The numeric groups map to HTTP-style
    categories: 1xxx auth, 2xxx network, 3xxx payment, 4xxx validation,
    5xxx crypto, 9xxx unknown.
    """

    # ── 1xxx Authentication / Identity ──────────────────────────────────
    MISSING_API_KEY = "AUTH_1001"
    TOKEN_REQUIRED = "AUTH_1002"
    AUTH_CHALLENGE_FAILED = "AUTH_1003"
    BLIND_SIGNING_REFUSED = "AUTH_1004"
    REGISTER_FAILED = "AUTH_1005"
    APIKEY_EXCHANGE_FAILED = "AUTH_1006"
    TOKEN_EXCHANGE_FAILED = "AUTH_1007"
    PAYOUT_BIND_FAILED = "AUTH_1008"
    ETH_ACCOUNT_NOT_INSTALLED = "AUTH_1009"

    # ── 2xxx Network / HTTP ─────────────────────────────────────────────
    HTTP_ERROR = "NET_2001"
    CIPHERTEXT_FETCH_FAILED = "NET_2002"
    FACTS_UNAVAILABLE = "NET_2003"
    FACTS_PAGINATION = "NET_2004"

    # ── 3xxx Payment / Bidding / x402 ───────────────────────────────────
    PAYMENT_TERMS_MISSING = "PAY_3001"
    BID_TERMS_INVALID = "PAY_3002"
    BID_AUTHORIZATION_MISMATCH = "PAY_3003"
    PAYMENT_REQUIRED_MALFORMED = "PAY_3004"
    PAYMENT_AMOUNT_MISMATCH = "PAY_3005"
    PAYMENT_PAYTO_MISMATCH = "PAY_3006"
    PAYMENT_FAILED = "PAY_3007"
    BUDGET_EXCEEDED = "PAY_3008"
    BUDGET_UNKNOWN = "PAY_3009"
    MAINNET_GATED = "PAY_3010"
    SELLER_SLA_INVALID = "PAY_3011"
    PAYMENT_NETWORK_UNSUPPORTED = "PAY_3012"
    PAYMENT_SCHEME_UNSUPPORTED = "PAY_3013"
    PAYMENT_ASSET_MISMATCH = "PAY_3014"
    PAYMENT_DOMAIN_MISMATCH = "PAY_3015"
    MAX_TIMEOUT_INVALID = "PAY_3016"
    DELIVERY_NOT_PAID = "PAY_3017"

    # ── 4xxx Validation ─────────────────────────────────────────────────
    MISSING_PARAMETER = "VAL_4001"
    INVALID_PARAMETER = "VAL_4002"
    INVALID_STATE = "VAL_4003"

    # ── 5xxx Cryptography / Key Material ─────────────────────────────────
    KEY_MATERIAL_INVALID = "CRY_5001"
    CIPHERTEXT_TAMPERED = "CRY_5002"
    DECRYPT_FAILED = "CRY_5003"
    DELIVERY_PREFLIGHT_FAILED = "CRY_5004"

    # ── 9xxx Unknown ────────────────────────────────────────────────────
    UNKNOWN = "UNKNOWN_9999"


class AccessuraError(Exception):
    """Base error for all structured SDK errors.

    Every ``AccessuraError`` carries a machine-readable ``code`` so callers
    can branch on the error category without string-matching the message.
    """

    def __init__(
        self,
        message: str,
        *,
        code: AccessuraErrorCode = AccessuraErrorCode.UNKNOWN,
        status_code: Optional[int] = None,
        details: Optional[dict[str, Any]] = None,
        request_id: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.details = details or {}
        self.request_id = request_id

    @property
    def message(self) -> str:
        """Alias for str(self), used by the MCP @safe decorator."""
        return str(self)

    def get_user_message(self) -> str:
        """Return the human-readable error message."""
        return str(self)

    def to_dict(self) -> dict[str, Any]:
        """Serializable representation for MCP JSON responses."""
        result: dict[str, Any] = {
            "error": str(self),
            "code": self.code.value,
        }
        if self.status_code is not None:
            result["status_code"] = self.status_code
        if self.details:
            result["details"] = self.details
        if self.request_id:
            result["request_id"] = self.request_id
        return result


# ── Specialized error classes ──────────────────────────────────────────────


class AccessuraAuthError(AccessuraError):
    """Authentication or identity-related failure (1xxx codes)."""

    def __init__(
        self,
        message: str,
        *,
        code: AccessuraErrorCode = AccessuraErrorCode.AUTH_CHALLENGE_FAILED,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, code=code, **kwargs)


class AccessuraNetworkError(AccessuraError):
    """Network, HTTP, or API transport failure (2xxx codes)."""

    def __init__(
        self,
        message: str,
        *,
        code: AccessuraErrorCode = AccessuraErrorCode.HTTP_ERROR,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, code=code, **kwargs)


class AccessuraPaymentError(AccessuraError):
    """Payment, bidding, or x402 authorization failure (3xxx codes)."""

    def __init__(
        self,
        message: str,
        *,
        code: AccessuraErrorCode = AccessuraErrorCode.PAYMENT_FAILED,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, code=code, **kwargs)


class AccessuraValidationError(AccessuraError):
    """Input validation failure (4xxx codes)."""

    def __init__(
        self,
        message: str,
        *,
        code: AccessuraErrorCode = AccessuraErrorCode.INVALID_PARAMETER,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, code=code, **kwargs)


class AccessuraCryptoError(AccessuraError):
    """Cryptographic operation failure (5xxx codes)."""

    def __init__(
        self,
        message: str,
        *,
        code: AccessuraErrorCode = AccessuraErrorCode.DECRYPT_FAILED,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, code=code, **kwargs)


# ── Error class registry (for deserialization / factory use) ───────────────

_ERROR_CLASS_BY_PREFIX: dict[str, type[AccessuraError]] = {
    "AUTH_": AccessuraAuthError,
    "NET_": AccessuraNetworkError,
    "PAY_": AccessuraPaymentError,
    "VAL_": AccessuraValidationError,
    "CRY_": AccessuraCryptoError,
}


def error_class_for_code(code: AccessuraErrorCode) -> type[AccessuraError]:
    """Return the specialized error class for a given error code."""
    for prefix, cls in _ERROR_CLASS_BY_PREFIX.items():
        if code.value.startswith(prefix):
            return cls
    return AccessuraError
