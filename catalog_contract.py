"""Pinned public-catalog contract for the MCP publish surface."""

from __future__ import annotations

from typing import Any

CATALOG_VERSION = "2026-07-17.pack-signal-read-contract"
INFO_TYPES = ("structured", "text", "figure", "video", "audio")
SIGNAL_TYPES = ("structured-data", "narrative-intel")
DELIVERY_FORMATS = {"structured": "json", "text": "markdown", "figure": "image", "video": "video", "audio": "audio"}
REQUIRED_FIELDS = {
    "structured": ("schema_version",),
    "text": ("word_count", "language", "source_url"),
    "figure": ("source_hash", "resolution", "capture_time", "media_type", "file_name", "file_size_bytes", "preview_description", "verification_notes"),
    "video": ("duration", "resolution", "source_hash", "media_type", "file_name", "file_size_bytes", "preview_description", "verification_notes"),
    "audio": ("duration", "format", "source_hash", "media_type", "file_name", "file_size_bytes", "preview_description", "verification_notes"),
}
SIGNAL_CONTRACT = {
    "requiredForBiddablePacks": True,
    "schemaField": "signal_schema",
    "typeField": "signal_type",
}


def assert_catalog_parity(catalog: dict[str, Any]) -> None:
    errors = []
    if catalog.get("version") != CATALOG_VERSION:
        errors.append(f"version {catalog.get('version')!r} != {CATALOG_VERSION!r}")
    enums = catalog.get("enums", {})
    if tuple(enums.get("infoType", [])) != INFO_TYPES:
        errors.append("infoType enum drift")
    if tuple(enums.get("signalType", [])) != SIGNAL_TYPES:
        errors.append("signalType enum drift")
    schemas = catalog.get("publishSchemas", {})
    for info_type in INFO_TYPES:
        actual = tuple(schemas.get(info_type, {}).get("requiredFields", []))
        if actual != REQUIRED_FIELDS[info_type]:
            errors.append(f"{info_type}.requiredFields drift: {actual!r}")
        if schemas.get(info_type, {}).get("deliveryFormat") != DELIVERY_FORMATS[info_type]:
            errors.append(f"{info_type}.deliveryFormat drift")
    signal_contract = catalog.get("signalContract", {})
    for key, expected in SIGNAL_CONTRACT.items():
        if signal_contract.get(key) != expected:
            errors.append(f"signalContract.{key} drift: {signal_contract.get(key)!r}")
    if errors:
        raise RuntimeError("public catalog drift detected: " + "; ".join(errors))
