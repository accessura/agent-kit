import asyncio
import json

import pytest

import server
from catalog_contract import parse_fields, validate_publish_contract


@pytest.mark.parametrize("info_type,fields", [
    ("structured", {"schema_version": "1.0", "columns": ["player", "status"]}),
    ("structured", {"schema_version": "1.0", "json_schema": {"odds": "number"}}),
    ("structured", {"schema_version": "1.0", "tables": [{"name": "x", "columns": ["id"]}]}),
    ("text", {"word_count": 10, "language": "en", "source_url": "seller note"}),
    ("figure", {"source_hash": "sha256:x", "resolution": "1x1", "capture_time": "2026-01-01T00:00:00Z", "media_type": "image/png", "file_name": "x.png", "file_size_bytes": 1, "preview_description": "preview", "verification_notes": "hash"}),
    ("video", {"duration": "00:01", "resolution": "1x1", "source_hash": "sha256:x", "media_type": "video/mp4", "file_name": "x.mp4", "file_size_bytes": 1, "preview_description": "preview", "verification_notes": "hash"}),
    ("audio", {"duration": "00:01", "format": "audio/mpeg", "source_hash": "sha256:x", "media_type": "audio/mpeg", "file_name": "x.mp3", "file_size_bytes": 1, "preview_description": "preview", "verification_notes": "hash"}),
])
def test_all_catalog_publish_shapes(info_type, fields):
    assert validate_publish_contract(info_type=info_type, topic_slug="world-cup-winner", signal_type="structured-data", fields=fields)


def test_multiple_topics_are_rejected_and_stale_fields_are_not_required():
    fields = {"word_count": 10, "language": "en", "source_url": "source"}
    with pytest.raises(RuntimeError, match="one active concrete"):
        validate_publish_contract(info_type="text", topic_slug="one,two", signal_type="narrative-intel", fields=fields)
    assert "cadence" not in fields and "freshness_seconds" not in fields


def test_mcp_publish_schema_exposes_single_slug_and_generic_fields():
    publish = next(tool for tool in asyncio.run(server.mcp.list_tools()) if tool.name == "packs_publish")
    properties = publish.inputSchema["properties"]
    assert "topic_slug" in properties and "topic_slugs" not in properties
    assert "fields_json" in properties
    assert {"title", "info_type", "topic_slug", "fields_json"} <= set(publish.inputSchema["required"])


def test_fields_json_must_be_an_object():
    with pytest.raises(RuntimeError, match="JSON object"):
        parse_fields(json.dumps(["not", "an", "object"]))
