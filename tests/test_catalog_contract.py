import asyncio
import copy

import pytest

import server
from catalog_contract import (
    CATALOG_VERSION,
    DELIVERY_FORMATS,
    INFO_TYPES,
    REQUIRED_FIELDS,
    SIGNAL_CONTRACT,
    SIGNAL_TYPES,
    assert_catalog_parity,
)


def pinned_catalog():
    return {
        "version": CATALOG_VERSION,
        "enums": {
            "infoType": list(INFO_TYPES),
            "signalType": list(SIGNAL_TYPES),
        },
        "publishSchemas": {
            info_type: {
                "deliveryFormat": DELIVERY_FORMATS[info_type],
                "requiredFields": list(REQUIRED_FIELDS[info_type]),
            }
            for info_type in INFO_TYPES
        },
        "signalContract": dict(SIGNAL_CONTRACT),
    }


def test_pinned_catalog_passes_parity():
    assert_catalog_parity(pinned_catalog())


@pytest.mark.parametrize("mutate,expected", [
    (lambda c: c.update(version="2026-07-12.operation-registry"), "version"),
    (lambda c: c["enums"].update(signalType=["structured-data"]), "signalType enum drift"),
    (lambda c: c["publishSchemas"]["text"].update(requiredFields=["word_count"]), "text.requiredFields drift"),
    (lambda c: c["publishSchemas"]["structured"].update(deliveryFormat="csv"), "structured.deliveryFormat drift"),
    (lambda c: c["signalContract"].update(schemaField="fields"), "signalContract.schemaField drift"),
])
def test_catalog_drift_is_detected(mutate, expected):
    catalog = copy.deepcopy(pinned_catalog())
    mutate(catalog)
    with pytest.raises(RuntimeError, match=expected):
        assert_catalog_parity(catalog)


def test_mcp_publish_schema_matches_signal_contract():
    publish = next(tool for tool in asyncio.run(server.mcp.list_tools()) if tool.name == "packs_publish")
    properties = publish.inputSchema["properties"]
    assert "topic_slugs" in properties and "topic_slug" not in properties
    assert "signal_schema" in properties and "fields_json" not in properties
    assert {"title", "info_type", "signal_type", "signal_schema"} <= set(publish.inputSchema["required"])
