"""HTTP transport layer for the Accessura SDK.

Thin sync HTTP helpers with httpx-first, urllib-fallback strategy.
x402 uses HTTP 402 plus PAYMENT-REQUIRED, so ``_request_response`` retains
protocol status/headers instead of treating every non-200 as an exception.
"""

import json
from typing import Any

# Module-level httpx cache (lazy import)
_HTTPX = None


def _get_httpx():
    global _HTTPX
    if _HTTPX is None:
        try:
            import httpx as _h
            _HTTPX = _h
        except ImportError:
            _HTTPX = False
    return _HTTPX


def _request(method: str, url: str, headers: dict,
             json_body: Any = None) -> dict:
    _, _, body = _request_response(method, url, headers, json_body)
    return body


def _request_response(method: str, url: str, headers: dict,
                      json_body: Any = None) -> tuple[int, dict, dict]:
    """Return status, lowercase response headers, and parsed JSON.

    x402 uses HTTP 402 plus PAYMENT-REQUIRED, so callers must retain protocol
    status/headers instead of treating every non-200 response as an exception.
    """
    h = {"Content-Type": "application/json",
         "User-Agent": "Accessura-SDK/0.7", **headers}
    body_bytes = json.dumps(json_body).encode() if json_body is not None else None
    if _get_httpx():
        r = _get_httpx().request(method, url, headers=h, content=body_bytes,
                                 timeout=30.0)
        return r.status_code, dict(r.headers), _maybe_json(r.text)
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, data=body_bytes, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, _maybe_json(raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, _maybe_json(raw)


def _maybe_json(text: str) -> dict:
    try:
        return json.loads(text)
    except Exception:
        return {"_error": text}
