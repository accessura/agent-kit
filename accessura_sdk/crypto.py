"""
Canonical ECIES crypto — extracts from `client.py` and `client_wrapper.py`.

All functions are pure (no env, no IO) so they can be reused by the sync SDK
(`BuyerAgent` / `SellerAgent`), the async MCP wrapper, and test suites.
Reference implementation: `src/lib/crypto/ecies.ts` (backend).
"""

import base64
import hashlib
import json
import secrets
import urllib.parse
from typing import Optional, Union

# Must match src/lib/crypto/ecies-shared.ts ECIES_ALG — it is the HKDF salt
# preimage.  The raw string should NOT be used as a salt.
ECIES_ALG = "x402-envelope/secp256k1+hkdf-sha256+aes-256-gcm/v1"


# ── tiny helpers ──────────────────────────────────────────────────────────

def _sha256(b: bytes) -> bytes:
    return hashlib.sha256(b).digest()


def _b64url(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "==")


def _b64u_encode(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


# ── key normalisation ─────────────────────────────────────────────────────

def normalize_encryption_pubkey(pubkey_hex: str) -> bytes:
    """Normalize an uncompressed secp256k1 pubkey to raw 65-byte 04|X|Y.

    Accepts '0x04|X|Y' (canonical), '04|X|Y', and bare 'X|Y' (128 hex chars).
    Never double-prepends the 04 point tag."""
    h = pubkey_hex[2:] if pubkey_hex.lower().startswith("0x") else pubkey_hex
    if len(h) == 128:
        h = "04" + h
    if len(h) != 130 or not h.startswith("04"):
        raise ValueError(
            "encryption pubkey must be an uncompressed secp256k1 point "
            "(0x04|X|Y, 65 bytes) — got %d hex chars" % len(h)
        )
    return bytes.fromhex(h)


# ── KEK derivation (canonical) ────────────────────────────────────────────

def _kek_from_shared(shared: bytes) -> bytes:
    """Canonical KEK: HKDF-SHA256(shared, salt=sha256(ECIES_ALG), info='x402-kek').

    The salt is the SHA-256 of the UTF-8 alg string — NOT the raw string
    (a previous wrapper used the raw string and nothing could decrypt)."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    return HKDF(
        algorithm=hashes.SHA256(), length=32,
        salt=_sha256(ECIES_ALG.encode("utf-8")), info=b"x402-kek",
    ).derive(shared)


# ── buyer decrypt (ECIES unwrap) ──────────────────────────────────────────

def decrypt_delivery(broker: dict, ciphertext_b64: str, private_key_hex: str) -> bytes:
    """ECIES decrypt — unwrap DEK via ECDH, then AES-256-GCM decrypt the content.

    `broker` is the decoded platform_broker dict.  Mirrors backend buyerDecrypt
    (src/lib/crypto/ecies.ts): verify ciphertext_hash, unwrap the DEK
    (ECDH -> HKDF -> AES-GCM with UTF-8 wrap_aad), then AES-GCM decrypt."""
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    if "eph_pub_hex" in broker:
        broker = {"wrapped_key": broker}

    ct = base64.b64decode(ciphertext_b64)
    expected = broker.get("ciphertext_hash")
    if expected and ("sha256:" + _sha256(ct).hex()) != expected:
        raise ValueError("ciphertext_hash mismatch (tampered or wrong artifact)")

    wk = broker["wrapped_key"]
    eph_hex = wk["eph_pub_hex"]
    if eph_hex.startswith("0x"):
        eph_hex = eph_hex[2:]

    buyer_priv = ec.derive_private_key(int(private_key_hex, 16), ec.SECP256K1())
    eph_pub = ec.EllipticCurvePublicKey.from_encoded_point(
        ec.SECP256K1(), bytes.fromhex(eph_hex))
    shared = buyer_priv.exchange(ec.ECDH(), eph_pub)

    kek = _kek_from_shared(shared)

    wrap_aad = broker["wrap_aad"].encode("utf-8") if broker.get("wrap_aad") else None
    dek = AESGCM(kek).decrypt(
        _b64url(wk["wrap_iv_b64u"]),
        _b64url(wk["wrapped_dek_b64u"]) + _b64url(wk["wrap_tag_b64u"]),
        wrap_aad,
    )

    ct_iv = _b64url(broker["ct_iv_b64u"]) if broker.get("ct_iv_b64u") else ct[:12]
    ct_tag = _b64url(broker["ct_tag_b64u"]) if broker.get("ct_tag_b64u") else ct[-16:]

    if (
        len(ct) >= len(ct_iv) + len(ct_tag)
        and ct[: len(ct_iv)] == ct_iv
        and ct[len(ct) - len(ct_tag):] == ct_tag
    ):
        body = ct[len(ct_iv): len(ct) - len(ct_tag)]
    else:
        body = ct

    aad = broker["aad"].encode("utf-8") if broker.get("aad") else None
    return AESGCM(dek).decrypt(ct_iv, body + ct_tag, aad)


# ── key-release helpers ───────────────────────────────────────────────────

def resolve_key_release_url(claim_header: str, claim_id: str) -> str:
    """Read seller_endpoint from the base64url claim header.

    Falls back to the platform mock-seller route. The buyer-side release is the
    SELLER's route, not GET /claims/{id}/key-release (that is seller-upload
    POST only)."""
    try:
        claim = json.loads(_b64url(claim_header).decode("utf-8"))
        endpoint = claim.get("seller_endpoint")
        if endpoint and endpoint != "/api/v1/mock-seller":
            return f"{endpoint}/claims/{urllib.parse.quote(claim_id)}/key-release"
    except Exception:
        pass
    return f"/api/v1/mock-seller/claims/{urllib.parse.quote(claim_id)}/key-release"


def parse_key_release(data: dict, claim_id: str) -> dict:
    """Normalize a key-release response.

    Returns { claim_id, payment_status, key_algorithm, broker, wrapped_key,
    ciphertext_b64, ciphertext_hash }."""
    wrapped = data.get("wrapped_key")
    algorithm = data.get("key_algorithm")
    broker = None
    error = None
    if isinstance(wrapped, dict):
        broker = wrapped
    elif isinstance(wrapped, str) and wrapped and algorithm in (None, "ECIES-secp256k1"):
        try:
            broker = json.loads(_b64url(wrapped).decode("utf-8"))
        except ValueError as e:
            error = f"wrapped_key decode failed: {e}"
    out = {
        "claim_id": data.get("claim_id", claim_id),
        "payment_status": data.get("payment_status"),
        "key_algorithm": algorithm,
        "broker": broker,
        "wrapped_key": wrapped,
        "ciphertext_b64": data.get("delivery_artifact_b64"),
        "ciphertext_hash": data.get("ciphertext_hash"),
    }
    if error:
        out["error"] = error
    return out


# ── seller-side crypto ────────────────────────────────────────────────────

def normalize_delivery_secret(delivery_secret: Union[str, bytes]) -> bytes:
    """Return a dedicated 32-byte seller delivery secret.

    String secrets are 64 hexadecimal characters with an optional ``0x``
    prefix. The wallet/identity private key must never be used as this value.
    """
    if isinstance(delivery_secret, bytes):
        raw = delivery_secret
    elif isinstance(delivery_secret, str):
        encoded = delivery_secret.strip()
        if encoded.lower().startswith("0x"):
            encoded = encoded[2:]
        if len(encoded) != 64:
            raise ValueError("delivery secret must be exactly 32 bytes (64 hex characters)")
        try:
            raw = bytes.fromhex(encoded)
        except ValueError as error:
            raise ValueError("delivery secret must be hexadecimal") from error
    else:
        raise TypeError("delivery secret must be bytes or a hexadecimal string")
    if len(raw) != 32:
        raise ValueError("delivery secret must be exactly 32 bytes")
    return raw


def derive_signal_dek(delivery_secret: Union[str, bytes], pack_id: str, signal_id: str) -> bytes:
    """Deterministic per-signal DEK for managed encryption.

    HKDF-SHA256(ikm=dedicated delivery-secret bytes, salt=sha256(ECIES_ALG),
    info='accessura-dek/{pack_id}/{signal_id}').

    Deterministic so claims.deliver can re-derive the same DEK at delivery
    time without storing anything. Payment/identity wallet keys are a separate
    security domain and are never an input to this derivation."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    ikm = normalize_delivery_secret(delivery_secret)
    return HKDF(
        algorithm=hashes.SHA256(), length=32,
        salt=_sha256(ECIES_ALG.encode("utf-8")),
        info=f"accessura-dek/{pack_id}/{signal_id}".encode("utf-8"),
    ).derive(ikm)


def encrypt_signal_content(plaintext: bytes, delivery_secret: Union[str, bytes],
                           pack_id: str, signal_id: str) -> str:
    """AES-256-GCM encrypt signal content with derived per-signal DEK.

    Uses v1 keyless framing iv(12) || body || tag(16).  Returns base64
    content_b64 ready for POST /packs/:id/signals."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    dek = derive_signal_dek(delivery_secret, pack_id, signal_id)
    iv = secrets.token_bytes(12)
    body_and_tag = AESGCM(dek).encrypt(iv, plaintext, None)
    return base64.b64encode(iv + body_and_tag).decode()


def seller_wrap_dek(dek: bytes, buyer_pubkey_hex: str, ciphertext_b64: str,
                    aad: Optional[str] = None,
                    wrap_aad: Optional[str] = None) -> dict:
    """Wrap a per-signal DEK to a buyer's encryption pubkey.

    Mirrors src/lib/crypto/ecies.ts sellerWrapDek exactly and returns the
    canonical PlatformBroker dict for POST /claims/:id/key-release."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if len(dek) != 32:
        raise ValueError("delivery DEK must be exactly 32 bytes")
    raw = base64.b64decode(ciphertext_b64)
    if len(raw) < 28:
        raise ValueError("ciphertext too short for iv(12)||body||tag(16) framing")
    ct_iv, ct_tag = raw[:12], raw[-16:]
    try:
        # Local-only preflight: validate the DEK and content AAD against the
        # exact framed artifact before the Buyer can be asked to pay.
        AESGCM(dek).decrypt(
            ct_iv, raw[12:-16] + ct_tag,
            aad.encode("utf-8") if aad else None)
    except Exception as exc:
        raise ValueError(
            "delivery preflight failed: DEK or content aad does not open ciphertext") from exc

    buyer_pub = ec.EllipticCurvePublicKey.from_encoded_point(
        ec.SECP256K1(), normalize_encryption_pubkey(buyer_pubkey_hex))
    eph_priv = ec.generate_private_key(ec.SECP256K1())
    shared = eph_priv.exchange(ec.ECDH(), buyer_pub)
    kek = _kek_from_shared(shared)

    wrap_iv_bytes = secrets.token_bytes(12)
    wrapped_and_tag = AESGCM(kek).encrypt(
        wrap_iv_bytes, dek,
        wrap_aad.encode("utf-8") if wrap_aad else None)
    eph_pub_hex = "0x" + eph_priv.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint).hex()

    return {
        "alg": ECIES_ALG,
        "ciphertext_hash": "sha256:" + _sha256(raw).hex(),
        "ct_iv_b64u": _b64u_encode(ct_iv),
        "ct_tag_b64u": _b64u_encode(ct_tag),
        "aad": aad,
        "wrap_aad": wrap_aad,
        "wrapped_key": {
            "eph_pub_hex": eph_pub_hex,
            "wrap_iv_b64u": _b64u_encode(wrap_iv_bytes),
            "wrapped_dek_b64u": _b64u_encode(wrapped_and_tag[:-16]),
            "wrap_tag_b64u": _b64u_encode(wrapped_and_tag[-16:]),
        },
    }


def seller_wrap_pre_encrypted_dek(
        dek: bytes, buyer_pubkey_hex: str, ciphertext_b64: str,
        claim_id: str, buyer_agent_id: str) -> dict:
    """Canonical mode for content encrypted before a claim winner existed.

    The content has no claim-specific AAD. The helper derives the only binding
    callers should supply — wrap_aad = claim_id:buyer_agent_id — internally so
    the similar-looking aad and wrap_aad parameters cannot be swapped.
    """
    if not claim_id or not buyer_agent_id:
        raise ValueError("claim_id and buyer_agent_id are required")
    return seller_wrap_dek(
        dek, buyer_pubkey_hex, ciphertext_b64,
        aad=None, wrap_aad=f"{claim_id}:{buyer_agent_id}")
