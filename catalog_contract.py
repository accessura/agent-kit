"""Pinned public-catalog contract for the MCP publish surface."""

from __future__ import annotations

import json
from typing import Any

CATALOG_VERSION = "2026-07-12.operation-registry"
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


def parse_fields(fields_json: str) -> dict[str, Any]:
    try:
        fields = json.loads(fields_json)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"fields_json must be valid JSON: {exc.msg}") from exc
    if not isinstance(fields, dict):
        raise RuntimeError("fields_json must decode to a JSON object")
    return fields


def validate_publish_contract(*, info_type: str, topic_slug: str, signal_type: str, fields: dict[str, Any]) -> str:
    if info_type not in INFO_TYPES:
        raise RuntimeError(f"info_type must be one of: {', '.join(INFO_TYPES)}")
    if not topic_slug.strip() or "," in topic_slug:
        raise RuntimeError("topic_slug must be one active concrete market slug (not a list)")
    if signal_type not in SIGNAL_TYPES:
        raise RuntimeError(f"signal_type must be one of: {', '.join(SIGNAL_TYPES)}")
    missing = [name for name in REQUIRED_FIELDS[info_type] if fields.get(name) in (None, "")]
    if missing:
        raise RuntimeError(f"fields_json missing required {info_type} fields: {', '.join(missing)}")
    if info_type == "structured":
        descriptors = sum(key in fields for key in ("columns", "json_schema", "tables")) + int("request_schema" in fields or "response_schema" in fields)
        if descriptors != 1:
            raise RuntimeError("structured fields require exactly one shape: columns, json_schema, tables, or request_schema + response_schema")
        if ("request_schema" in fields) != ("response_schema" in fields):
            raise RuntimeError("endpoint shape requires both request_schema and response_schema")
    return DELIVERY_FORMATS[info_type]


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
    if errors:
        raise RuntimeError("public catalog drift detected: " + "; ".join(errors))
