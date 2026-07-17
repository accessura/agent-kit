# Round-trip self-test for accessura_sdk decrypt_delivery.
# Seller side replicated byte-for-byte from src/lib/crypto/ecies.ts
# (sellerWrapDek = framed iv||body||tag artifact; sellerEncrypt = legacy
# body-only artifact). Buyer side = the SDK module under test.
# Usage: python3 test_sdk_decrypt.py  (needs only `cryptography`, no server)
import base64
import hashlib
import importlib.util
import json
import os
import sys

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

ECIES_ALG = b"x402-envelope/secp256k1+hkdf-sha256+aes-256-gcm/v1"

_root = os.path.dirname(os.path.abspath(__file__))
if len(sys.argv) > 1:
    spec = importlib.util.spec_from_file_location("sdk_client", sys.argv[1])
    sdk = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sdk)
else:
    sys.path.insert(0, _root)
    from accessura_sdk import client as sdk

sha256 = lambda b: hashlib.sha256(b).digest()
b64u = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def seller_wrap(buyer_pub_hex, dek, hash_over, ct_iv, ct_tag, aad, wrap_aad):
    """Mirror ecies.ts kekFromEcdh + gcmEnc wrap layer + platform_broker shape."""
    eph_priv = ec.generate_private_key(ec.SECP256K1())
    buyer_pub = ec.EllipticCurvePublicKey.from_encoded_point(
        ec.SECP256K1(), bytes.fromhex(buyer_pub_hex[2:])
    )
    shared = eph_priv.exchange(ec.ECDH(), buyer_pub)
    kek = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=sha256(ECIES_ALG), info=b"x402-kek"
    ).derive(shared)
    wiv = os.urandom(12)
    wrapped_and_tag = AESGCM(kek).encrypt(wiv, dek, wrap_aad.encode() if wrap_aad else None)
    eph_pub_hex = "0x" + eph_priv.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    ).hex()
    # ct iv/tag broker fields: framed artifact carries iv||body||tag; broker
    # duplicates them (sellerWrapDek slices raw[0:12] / raw[-16:]).
    return {
        "alg": ECIES_ALG.decode(),
        "ciphertext_hash": "sha256:" + sha256(hash_over).hex(),
        "ct_iv_b64u": b64u(ct_iv),
        "ct_tag_b64u": b64u(ct_tag),
        "aad": aad,
        "wrap_aad": wrap_aad,
        "wrapped_key": {
            "eph_pub_hex": eph_pub_hex,
            "wrap_iv_b64u": b64u(wiv),
            "wrapped_dek_b64u": b64u(wrapped_and_tag[:-16]),
            "wrap_tag_b64u": b64u(wrapped_and_tag[-16:]),
        },
    }


buyer_priv = ec.generate_private_key(ec.SECP256K1())
buyer_priv_hex = format(buyer_priv.private_numbers().private_value, "064x")
buyer_pub_hex = "0x" + buyer_priv.public_key().public_bytes(
    serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
).hex()

results = []


def check(name, fn):
    try:
        fn()
        results.append((name, "PASS", ""))
    except Exception as e:
        results.append((name, "FAIL", f"{type(e).__name__}: {e}"))


PLAINTEXT = json.dumps({"signal": "verify-payload", "n": 42}).encode()

# Case 1: v1 keyless (sellerWrapDek): framed iv||body||tag, aad + wrap_aad, hash over full raw.
dek = os.urandom(32)
iv = os.urandom(12)
aad = "claim-123:0xBuyer"
body_and_tag = AESGCM(dek).encrypt(iv, PLAINTEXT, aad.encode())
raw = iv + body_and_tag
broker1 = seller_wrap(buyer_pub_hex, dek, raw, raw[:12], raw[-16:], aad, aad)


def case1():
    out = sdk.decrypt_delivery(broker1, base64.b64encode(raw).decode(), buyer_priv_hex)
    assert out == PLAINTEXT, f"plaintext mismatch: {out!r}"


check("framed artifact + aad + wrap_aad (sellerWrapDek path)", case1)

# Case 2: legacy sellerEncrypt: body-only artifact, hash over body, iv/tag only in broker, no aads.
dek2 = os.urandom(32)
ct_iv2 = os.urandom(12)
out2 = AESGCM(dek2).encrypt(ct_iv2, PLAINTEXT, None)
body2, ct_tag2 = out2[:-16], out2[-16:]
broker2 = seller_wrap(buyer_pub_hex, dek2, body2, ct_iv2, ct_tag2, None, None)


def case2():
    out = sdk.decrypt_delivery(broker2, base64.b64encode(body2).decode(), buyer_priv_hex)
    assert out == PLAINTEXT, f"plaintext mismatch: {out!r}"


check("legacy body-only artifact, broker-carried iv/tag (sellerEncrypt path)", case2)


# Case 3: tampered ciphertext must be rejected (hash or GCM failure, either is fine).
def case3():
    bad = bytearray(raw)
    bad[14] ^= 0xFF
    try:
        sdk.decrypt_delivery(broker1, base64.b64encode(bytes(bad)).decode(), buyer_priv_hex)
    except Exception:
        return
    raise AssertionError("tampered artifact decrypted without error")


check("tampered artifact rejected", case3)


# Case 4: bare wrapped_key dict (old caller shape) still decrypts a framed
# artifact — only possible when the seller used no AADs, since wrap_aad/aad
# live on the broker and a bare wrapped_key has no broker context.
dek4 = os.urandom(32)
iv4 = os.urandom(12)
raw4 = iv4 + AESGCM(dek4).encrypt(iv4, PLAINTEXT, None)
broker4 = seller_wrap(buyer_pub_hex, dek4, raw4, raw4[:12], raw4[-16:], None, None)


def case4():
    out = sdk.decrypt_delivery(dict(broker4["wrapped_key"]), base64.b64encode(raw4).decode(), buyer_priv_hex)
    assert out == PLAINTEXT


check("bare wrapped_key compat (no broker context)", case4)

# Case 5 (post-fix only): key-release response parsing + route resolution helpers.
if hasattr(sdk, "parse_key_release"):
    def case5():
        release_body = {
            "claim_id": "claim-123",
            "payment_status": "paid",
            # the backend's ECIES release value (delivery-x402/service.ts)
            "key_algorithm": "ECIES-secp256k1",
            "wrapped_key": base64.urlsafe_b64encode(json.dumps(broker1).encode()).rstrip(b"=").decode(),
            "delivery_artifact_b64": base64.b64encode(raw).decode(),
            "ciphertext_hash": broker1["ciphertext_hash"],
        }
        parsed = sdk.parse_key_release(release_body, "claim-123")
        assert parsed["broker"] == broker1, "broker decode mismatch"
        out = sdk.decrypt_delivery(parsed["broker"], parsed["ciphertext_b64"], buyer_priv_hex)
        assert out == PLAINTEXT

    check("key-release wrapped_key base64url(JSON) decode", case5)

    def case5b():
        # mock hold/legacy-x402 releases: opaque wrapped_key must pass through
        # untouched instead of raising at the base64url/JSON decode
        mock = sdk.parse_key_release({
            "claim_id": "claim-mock",
            "payment_status": "settled",
            "key_algorithm": "mock-X25519+A256GCMKW",
            "wrapped_key": "mock-token-not-json",
        }, "claim-mock")
        assert mock["broker"] is None, mock
        assert mock["wrapped_key"] == "mock-token-not-json", mock
        assert "error" not in mock, mock
        # an ECIES release with a corrupt wrapped_key surfaces a structured
        # error instead of raising
        bad = sdk.parse_key_release({
            "key_algorithm": "ECIES-secp256k1",
            "wrapped_key": "%%%not-decodable%%%",
        }, "claim-bad")
        assert bad["broker"] is None and "error" in bad, bad

    check("key-release non-ECIES passthrough + corrupt-key structured error", case5b)

    def case6():
        hdr = lambda claim: base64.urlsafe_b64encode(json.dumps(claim).encode()).rstrip(b"=").decode()
        ext = sdk.resolve_key_release_url(hdr({"seller_endpoint": "https://seller.example/api/v1"}), "c 1")
        assert ext == "https://seller.example/api/v1/claims/c%201/key-release", ext
        mock = sdk.resolve_key_release_url(hdr({"seller_endpoint": "/api/v1/mock-seller"}), "c1")
        assert mock == "/api/v1/mock-seller/claims/c1/key-release", mock
        garbage = sdk.resolve_key_release_url("!!!not-b64!!!", "c1")
        assert garbage == "/api/v1/mock-seller/claims/c1/key-release", garbage

    check("key-release route resolution (external / mock-seller / malformed)", case6)


# ── client_wrapper seller-side round-trip (MCP delivery path) ────────────
# Proves client_wrapper.seller_wrap_pre_encrypted_dek emits the canonical PlatformBroker
# (same shape/keys/encodings as ecies.ts sellerWrapDek) and that the SDK
# buyer decrypt round-trips it byte-identically. Skipped when httpx is
# missing (client_wrapper imports it at module level).
try:
    os.environ["ACCESSURA_PRIVATE_KEY"] = "0x" + buyer_priv_hex
    os.environ["ACCESSURA_DELIVERY_SECRET"] = "55" * 32
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import client_wrapper as cw
    cw.PRIVATE_KEY = "0x" + buyer_priv_hex  # in case the module was cached pre-env
    cw.DELIVERY_SECRET = "55" * 32
except ImportError as e:
    print(f"[SKIP] client_wrapper cases — {e}")
    cw = None

if cw is not None:
    BROKER_KEYS = {"alg", "ciphertext_hash", "ct_iv_b64u", "ct_tag_b64u", "aad", "wrap_aad", "wrapped_key"}
    WRAPPED_KEYS = {"eph_pub_hex", "wrap_iv_b64u", "wrapped_dek_b64u", "wrap_tag_b64u"}

    def case7():
        dek7 = cw.derive_signal_dek("pack-rt", "sig-rt")
        assert dek7 == cw.derive_signal_dek("pack-rt", "sig-rt"), "DEK derivation not deterministic"
        assert dek7 != cw.derive_signal_dek("pack-rt", "sig-other"), "DEK must differ per signal"
        assert dek7 == sdk.derive_signal_dek("55" * 32, "pack-rt", "sig-rt")
        original_private_key = cw.PRIVATE_KEY
        cw.PRIVATE_KEY = "0x" + "66" * 32
        assert dek7 == cw.derive_signal_dek("pack-rt", "sig-rt"), "wallet-key rotation changed seller DEK"
        cw.PRIVATE_KEY = original_private_key
        assert sdk.derive_signal_dek("77" * 32, "pack-rt", "sig-rt") != dek7
        content_b64 = cw.encrypt_signal_content(PLAINTEXT, "pack-rt", "sig-rt")
        wrap_aad7 = "claim-rt:0xBuyerRt"
        broker = cw.seller_wrap_pre_encrypted_dek(
            dek7, buyer_pub_hex, content_b64, "claim-rt", "0xBuyerRt")
        # canonical shape: exact key sets, alg constant, 0x04 eph pubkey,
        # unpadded base64url, hash over the raw framed artifact
        assert set(broker) == BROKER_KEYS, f"broker keys: {sorted(broker)}"
        assert set(broker["wrapped_key"]) == WRAPPED_KEYS, f"wrapped_key keys: {sorted(broker['wrapped_key'])}"
        assert broker["alg"] == sdk.ECIES_ALG
        assert broker["wrapped_key"]["eph_pub_hex"].startswith("0x04")
        for k in ("ct_iv_b64u", "ct_tag_b64u"):
            assert "=" not in broker[k], f"{k} must be unpadded base64url"
        for k in WRAPPED_KEYS - {"eph_pub_hex"}:
            assert "=" not in broker["wrapped_key"][k], f"{k} must be unpadded base64url"
        raw7 = base64.b64decode(content_b64)
        assert broker["ciphertext_hash"] == "sha256:" + sha256(raw7).hex()
        assert broker["wrap_aad"] == wrap_aad7 and broker["aad"] is None
        # SDK buyer decrypt (the exact consumer) round-trips
        assert sdk.decrypt_delivery(broker, content_b64, buyer_priv_hex) == PLAINTEXT
        # client_wrapper's own env-keyed buyer path too
        assert cw.buyer_decrypt(broker, content_b64) == PLAINTEXT
        # The low-level escape hatch still supports claim-time encryption, but
        # its local preflight must catch the historical aad/wrap_aad mix-up and
        # a wrong DEK before any envelope can be submitted.
        try:
            cw.seller_wrap_dek(
                dek7, buyer_pub_hex, content_b64,
                aad=wrap_aad7, wrap_aad=None)
        except ValueError as exc:
            assert "preflight failed" in str(exc)
        else:
            raise AssertionError("aad/wrap_aad confusion passed Seller preflight")
        try:
            cw.seller_wrap_pre_encrypted_dek(
                os.urandom(32), buyer_pub_hex, content_b64,
                "claim-rt", "0xBuyerRt")
        except ValueError as exc:
            assert "preflight failed" in str(exc)
        else:
            raise AssertionError("wrong DEK passed Seller preflight")
        # tampered wrap_aad must fail (binding is real)
        bad = json.loads(json.dumps(broker))
        bad["wrap_aad"] = "claim-rt:0xEvil"
        try:
            sdk.decrypt_delivery(bad, content_b64, buyer_priv_hex)
        except Exception:
            return
        raise AssertionError("wrap_aad tamper decrypted without error")

    check("client_wrapper canonical pre-encrypted wrap -> SDK decrypt + broker shape", case7)

    def case8():
        raw8 = bytes.fromhex(buyer_pub_hex[2:])
        for variant in (buyer_pub_hex, buyer_pub_hex[2:], buyer_pub_hex[4:]):
            got = cw.normalize_encryption_pubkey(variant)
            assert got == raw8, f"normalization mismatch for {variant[:8]}..."
        for bad in ("05" + buyer_pub_hex[4:], "04abcd", "0x1234"):
            try:
                cw.normalize_encryption_pubkey(bad)
            except ValueError:
                continue
            raise AssertionError(f"accepted malformed pubkey {bad[:10]}...")
        # not-on-curve bare X||Y passes structural normalization but must be
        # rejected by the curve check inside seller_wrap_dek
        try:
            cw.seller_wrap_dek(os.urandom(32), "0x04" + "ab" * 64,
                               base64.b64encode(os.urandom(64)).decode())
        except Exception:
            return
        raise AssertionError("seller_wrap_dek accepted a non-curve pubkey")

    check("pubkey normalization (0x04|X|Y / 04|X|Y / bare X|Y, no double-04)", case8)

fails = 0
for name, status, detail in results:
    print(f"[{status}] {name}" + (f" — {detail}" if detail else ""))
    fails += status == "FAIL"
print(f"==== {len(results) - fails}/{len(results)} passed ====")
sys.exit(1 if fails else 0)
