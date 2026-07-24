"""Pinned public-catalog contract for the MCP publish surface."""

from __future__ import annotations

import json
import re
from typing import Any

CATALOG_VERSION = "2026-07-17.pack-signal-read-contract"
INFO_TYPES = ("structured", "text", "figure", "video", "audio")
SIGNAL_TYPES = ("structured-data", "narrative-intel")
DELIVERY_FORMATS = {"structured": "json", "text": "markdown", "figure": "image", "video": "video", "audio": "audio"}
MIN_TOPIC_SLUGS = 1
MAX_TOPIC_SLUGS = 20
MAX_SIGNAL_SCHEMA_BYTES = 16_000
MAX_SIGNAL_SCHEMA_FIELDS = 128
MAX_SIGNAL_FIELD_NAME_LENGTH = 96
MAX_SIGNAL_FIELD_TYPE_LENGTH = 96
SIGNAL_FIELD_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
UNSAFE_SIGNAL_FIELD_NAMES = {"__proto__", "prototype", "constructor"}
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
    "separateFrom": "fields (Pack delivery/container metadata)",
}


def parse_fields(fields_json: str) -> dict[str, Any]:
    try:
        fields = json.loads(fields_json)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"fields_json must be valid JSON: {exc.msg}") from exc
    if not isinstance(fields, dict):
        raise RuntimeError("fields_json must decode to a JSON object")
    return fields


def normalize_topic_slugs(topic_slugs: list[str]) -> list[str]:
    if not isinstance(topic_slugs, list):
        raise RuntimeError("topic_slugs must be an array of active concrete market slugs")
    if not MIN_TOPIC_SLUGS <= len(topic_slugs) <= MAX_TOPIC_SLUGS:
        raise RuntimeError(f"topic_slugs must contain {MIN_TOPIC_SLUGS}-{MAX_TOPIC_SLUGS} entries")
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_slug in topic_slugs:
        if not isinstance(raw_slug, str):
            raise RuntimeError("every topic_slugs entry must be a string")
        slug = raw_slug.strip()
        if not slug or "," in slug:
            raise RuntimeError("every topic_slugs entry must be one non-empty concrete market slug")
        if slug in seen:
            raise RuntimeError(f"topic_slugs contains a duplicate slug: {slug}")
        seen.add(slug)
        normalized.append(slug)
    return normalized


def normalize_signal_schema(signal_schema: dict[str, str]) -> dict[str, str]:
    if not isinstance(signal_schema, dict) or not signal_schema:
        raise RuntimeError("signal_schema must be a non-empty object mapping field names to type names")
    if len(signal_schema) > MAX_SIGNAL_SCHEMA_FIELDS:
        raise RuntimeError(f"signal_schema may declare at most {MAX_SIGNAL_SCHEMA_FIELDS} fields")
    try:
        encoded = json.dumps(
            signal_schema, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise RuntimeError("signal_schema must be JSON serializable") from exc
    if len(encoded) > MAX_SIGNAL_SCHEMA_BYTES:
        raise RuntimeError(f"signal_schema must be <= {MAX_SIGNAL_SCHEMA_BYTES // 1000}KB")

    normalized: dict[str, str] = {}
    for raw_name, raw_type in signal_schema.items():
        if not isinstance(raw_name, str):
            raise RuntimeError("every signal_schema field name must be a string")
        name = raw_name.strip()
        if (
            not name
            or len(name) > MAX_SIGNAL_FIELD_NAME_LENGTH
            or not SIGNAL_FIELD_NAME.fullmatch(name)
            or name in UNSAFE_SIGNAL_FIELD_NAMES
        ):
            raise RuntimeError(
                f'signal_schema field "{raw_name}" must be a safe identifier '
                f"up to {MAX_SIGNAL_FIELD_NAME_LENGTH} characters"
            )
        if name in normalized:
            raise RuntimeError(f"signal_schema field is declared more than once after trimming: {name}")
        if not isinstance(raw_type, str):
            raise RuntimeError(f"signal_schema.{name} must be a type-name string")
        type_name = raw_type.strip()
        if not type_name or len(type_name) > MAX_SIGNAL_FIELD_TYPE_LENGTH:
            raise RuntimeError(
                f"signal_schema.{name} must be a non-empty type name "
                f"up to {MAX_SIGNAL_FIELD_TYPE_LENGTH} characters"
            )
        normalized[name] = type_name
    return normalized


def validate_publish_contract(
    *,
    info_type: str,
    topic_slugs: list[str],
    signal_type: str,
    signal_schema: dict[str, str],
    fields: dict[str, Any],
) -> str:
    if info_type not in INFO_TYPES:
        raise RuntimeError(f"info_type must be one of: {', '.join(INFO_TYPES)}")
    normalize_topic_slugs(topic_slugs)
    if signal_type not in SIGNAL_TYPES:
        raise RuntimeError(f"signal_type must be one of: {', '.join(SIGNAL_TYPES)}")
    normalize_signal_schema(signal_schema)
    missing = [name for name in REQUIRED_FIELDS[info_type] if fields.get(name) in (None, "")]
    if missing:
        raise RuntimeError(f"fields_json missing required {info_type} fields: {', '.join(missing)}")
    if info_type == "structured":
        descriptors = (
            sum(key in fields for key in ("columns", "json_schema", "tables"))
            + int("request_schema" in fields or "response_schema" in fields)
        )
        if descriptors != 1:
            raise RuntimeError(
                "structured fields require exactly one shape: columns, json_schema, "
                "tables, or request_schema + response_schema"
            )
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
    signal_contract = catalog.get("signalContract", {})
    for key, expected in SIGNAL_CONTRACT.items():
        if signal_contract.get(key) != expected:
            errors.append(f"signalContract.{key} drift: {signal_contract.get(key)!r}")
    operations = catalog.get("operations", [])
    publish = next(
        (operation for operation in operations if operation.get("id") == "publish.pack"),
        None,
    )
    if publish is None:
        errors.append("publish.pack operation missing")
    else:
        request = publish.get("request", "")
        note = publish.get("note", "")
        if "one or more active concrete topic_slugs" not in request:
            errors.append("publish.pack multi-topic request drift")
        if "required signal_type + signal_schema" not in request:
            errors.append("publish.pack required Signal contract drift")
        if "topic_slugs is authoritative" not in note:
            errors.append("publish.pack topic authority drift")
        if "latest endDate" not in note:
            errors.append("publish.pack topic lifecycle drift")
    if errors:
        raise RuntimeError("public catalog drift detected: " + "; ".join(errors))
