"""Tests for the /v1/onyx/* OpenAPI export script."""
from __future__ import annotations

import sys
from pathlib import Path

# Make the apps/server/scripts/ dir importable as the ``scripts`` package.
_SCRIPTS_PARENT = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_PARENT) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_PARENT))


def _run_export(tmp_path: Path):
    # Import directly; the script's env defaults already cover required vars
    from scripts.export_onyx_openapi import export_onyx_openapi
    output = tmp_path / "openapi.yaml"
    spec = export_onyx_openapi(output)
    return spec, output


def test_export_writes_yaml_file(tmp_path):
    spec, output = _run_export(tmp_path)
    assert output.exists()
    text = output.read_text()
    assert text.startswith("openapi:") or text.startswith("info:") or text.startswith("paths:")


def test_export_contains_only_onyx_paths(tmp_path):
    spec, _ = _run_export(tmp_path)
    paths = spec.get("paths", {})
    assert paths, "expected at least one onyx path"
    for p in paths:
        assert p.startswith("/v1/onyx/"), f"non-onyx path leaked: {p}"


def test_export_contains_kb_endpoints(tmp_path):
    spec, _ = _run_export(tmp_path)
    paths = spec.get("paths", {})
    assert "/v1/onyx/kb" in paths
    # kb_id-bearing endpoints
    assert any(p.startswith("/v1/onyx/kb/") and p != "/v1/onyx/kb" for p in paths)


def test_export_contains_query_endpoints(tmp_path):
    spec, _ = _run_export(tmp_path)
    paths = spec.get("paths", {})
    assert "/v1/onyx/query" in paths
    assert "/v1/onyx/query/sync" in paths


def test_export_contains_kg_endpoints(tmp_path):
    spec, _ = _run_export(tmp_path)
    paths = spec.get("paths", {})
    assert "/v1/onyx/kg/entities" in paths
    assert "/v1/onyx/kg/stats" in paths


def test_export_no_alpha_paths(tmp_path):
    spec, _ = _run_export(tmp_path)
    paths = spec.get("paths", {})
    # No alpha path should appear
    for p in paths:
        assert not p.startswith("/v1/auth"), p
        assert not p.startswith("/v1/documents") or p.startswith("/v1/onyx/documents"), p
        assert not p.startswith("/v1/conversations"), p


def test_export_no_token_value_leakage(tmp_path):
    spec, output = _run_export(tmp_path)
    text = output.read_text()
    # The script's INTERNAL_TOKEN env default is "x"*96; ensure that
    # specific value never lands in the schema.
    assert "x" * 96 not in text


def test_export_info_block_is_meaningful(tmp_path):
    spec, _ = _run_export(tmp_path)
    info = spec.get("info", {})
    assert info["version"] == "v1"
    assert "ONYX" in info["title"] or "onyx" in info["title"]
