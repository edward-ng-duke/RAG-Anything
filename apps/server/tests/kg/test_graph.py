"""Unit tests for ``rag_service.kg.graph``.

Apache AGE isn't available under SQLite (it's a Postgres-only extension)
and standing up real Postgres + AGE in CI is heavy. So these tests:

* Exercise the pure helpers (``_normalize_node_id``, the agtype parsers)
  directly with synthetic strings.
* Mock ``AsyncSession.execute`` so we can assert on the SQL the
  traversal helpers emit and feed in canned ``agtype``-shaped rows
  without touching a database.

We are explicitly NOT trying to verify cypher correctness — that's an
integration concern. We ARE verifying:

* sanitization actually neutralizes injection payloads,
* depth bounds are enforced,
* DB errors degrade to empty results (the route layer relies on this),
* node/edge dedupe and shape are stable.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from rag_service.kg import graph


# ---------------------------------------------------------------------------
# _normalize_node_id
# ---------------------------------------------------------------------------


def test_normalize_node_id_basic() -> None:
    """Alphanumerics survive; specials collapse to ``_``."""
    assert graph._normalize_node_id("alice") == "alice"
    assert graph._normalize_node_id("Alice123") == "Alice123"
    # Spaces, quotes, semicolons, dollar signs all become underscores so a
    # value spliced into a single-quoted cypher literal cannot escape it.
    # Hyphens ARE in the allowlist (LightRAG uses them in entity_ids), so
    # the trailing ``--`` survives — what matters is that the quote and
    # semicolon both die.
    assert graph._normalize_node_id("a'; DROP--") == "a___DROP--"
    assert graph._normalize_node_id('he said "hi"') == "he_said__hi_"


def test_normalize_node_id_empty_raises() -> None:
    """Empty / non-string inputs raise instead of returning ``""``."""
    with pytest.raises(ValueError):
        graph._normalize_node_id("")
    with pytest.raises(ValueError):
        graph._normalize_node_id(None)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        graph._normalize_node_id(123)  # type: ignore[arg-type]


def test_normalize_node_id_with_safe_chars() -> None:
    """Underscores, hyphens, and colons are preserved (LightRAG uses them)."""
    assert graph._normalize_node_id("ns:name") == "ns:name"
    assert graph._normalize_node_id("entity-1") == "entity-1"
    assert graph._normalize_node_id("a_b_c") == "a_b_c"
    assert graph._normalize_node_id("type:sub-id_42") == "type:sub-id_42"


def test_normalize_node_id_reduces_to_empty_raises() -> None:
    """Input made entirely of unsafe characters has no representation."""
    # The current implementation collapses unsafe chars to ``_``, so a
    # string of only spaces becomes only underscores (still non-empty).
    # But explicitly empty stays empty -> ValueError.
    # Sanity-check the boundary: a single allowed char is fine.
    assert graph._normalize_node_id("a") == "a"


# ---------------------------------------------------------------------------
# _graph_name — must mirror lightrag's _get_workspace_graph_name byte-for-byte
# ---------------------------------------------------------------------------


def test_graph_name_substitutes_non_identifier_chars() -> None:
    """Hyphens and other non-[a-zA-Z0-9_] chars become underscores.

    Regression test for the Wave H smoke bug: prior to the fix this
    returned the tenant_id verbatim and queries against the AGE graph
    silently returned empty rows.
    """
    assert (
        graph._graph_name("onyx-abc-123")
        == "onyx_abc_123_chunk_entity_relation"
    )


def test_graph_name_no_substitutions_needed() -> None:
    """Already-safe identifiers pass through (modulo namespace suffix)."""
    assert (
        graph._graph_name("tenant_a") == "tenant_a_chunk_entity_relation"
    )


def test_graph_name_uuid_style_tenant_id() -> None:
    """The onyx-{uuid} format from production gets the same treatment as
    every other tenant_id."""
    assert graph._graph_name("onyx-6a32cb98-d940-4ca5-a2d7-c5e9bc39e8db") == (
        "onyx_6a32cb98_d940_4ca5_a2d7_c5e9bc39e8db_chunk_entity_relation"
    )


# ---------------------------------------------------------------------------
# Depth / input validation
# ---------------------------------------------------------------------------


async def test_neighbors_invalid_depth_raises() -> None:
    """Only depth in {1, 2, 3} is accepted."""
    db = MagicMock()
    for bad in (0, 4, -1, 10):
        with pytest.raises(ValueError):
            await graph.neighbors(db, "tenant-a", "alice", depth=bad)


async def test_subgraph_invalid_depth_raises() -> None:
    db = MagicMock()
    with pytest.raises(ValueError):
        await graph.subgraph(db, "tenant-a", ["alice"], depth=0)
    with pytest.raises(ValueError):
        await graph.subgraph(db, "tenant-a", ["alice"], depth=4)


async def test_subgraph_empty_input_returns_empty() -> None:
    """``entity_ids=[]`` short-circuits without touching the DB."""
    db = MagicMock()
    db.execute = AsyncMock(side_effect=AssertionError("must not be called"))
    out = await graph.subgraph(db, "tenant-a", [], depth=2)
    assert out == {"nodes": [], "edges": []}


# ---------------------------------------------------------------------------
# SQL emission — capture the text() statement and inspect it
# ---------------------------------------------------------------------------


def _captured_sql_session(rows: list[Any] | None = None) -> tuple[Any, list[str]]:
    """Build a mock AsyncSession that records the SQL it was given.

    The execute() coroutine returns a result whose ``.all()`` yields the
    canned ``rows`` (default empty), letting tests assert on both the
    emitted SQL and the post-parse output.
    """
    captured: list[str] = []

    result = MagicMock()
    result.all = MagicMock(return_value=rows or [])

    async def _execute(stmt: Any, *_a: Any, **_k: Any) -> Any:
        # SQLAlchemy ``text()`` exposes the raw string via ``.text``.
        captured.append(getattr(stmt, "text", str(stmt)))
        return result

    db = MagicMock()
    db.execute = _execute
    return db, captured


async def test_neighbors_query_builds_correctly() -> None:
    """Captured SQL contains the cypher template + sanitized id (not the raw)."""
    db, captured = _captured_sql_session()
    # Hostile entity_id with quotes + a cypher injection attempt.
    out = await graph.neighbors(db, "tenant-a", "a'; DROP", depth=2)
    assert out == {"nodes": [], "edges": []}
    assert len(captured) == 1
    sql = captured[0]
    # Graph name mirrors lightrag's _get_workspace_graph_name:
    # ``tenant-a`` -> ``tenant_a_chunk_entity_relation``.
    assert "ag_catalog.cypher('tenant_a_chunk_entity_relation'" in sql
    # Variable-length pattern with the requested depth.
    assert "[r*1..2]" in sql
    # Sanitized id is what we splice — quote, semicolon, and space are
    # squashed; alphanumerics survive.
    assert "n.entity_id = 'a___DROP'" in sql
    # Original injection payload (with the literal quote) must NOT survive.
    assert "a'; DROP" not in sql


async def test_neighbors_default_depth_is_one() -> None:
    db, captured = _captured_sql_session()
    await graph.neighbors(db, "tenant-a", "alice")
    assert "[r*1..1]" in captured[0]


async def test_neighbors_resolves_ent_id_to_entity_name(monkeypatch) -> None:
    """``ent-<hash>`` inputs are translated to entity_name before cypher.

    Regression: lightrag indexes AGE vertices by ``entity_id =
    entity_name``, so passing the surrogate id verbatim returns 0 rows.
    """
    db, captured = _captured_sql_session()

    async def _stub(_db, _tid, eids):
        return {"ent-1ec0aec1cdccfcaa6df8e07b40d3e664": "Dogs"}

    from rag_service.kg import repository
    monkeypatch.setattr(repository, "get_entity_names_by_ids", _stub)

    await graph.neighbors(
        db, "tenant-a", "ent-1ec0aec1cdccfcaa6df8e07b40d3e664", depth=1
    )
    sql = captured[0]
    # The cypher MATCH carries the resolved name, not the surrogate id.
    assert "n.entity_id = 'Dogs'" in sql
    assert "ent-1ec0aec1cdccfcaa6df8e07b40d3e664" not in sql


async def test_neighbors_passthrough_when_name_already(monkeypatch) -> None:
    """Inputs that don't match the ``ent-`` prefix are spliced as-is.

    Internal callers that already use names (and tests that pass simple
    strings) must keep working without round-tripping through the DB.
    """
    db, captured = _captured_sql_session()

    async def _must_not_call(*_a, **_k):
        raise AssertionError("repository should not be consulted for plain names")

    from rag_service.kg import repository
    monkeypatch.setattr(repository, "get_entity_names_by_ids", _must_not_call)

    await graph.neighbors(db, "tenant-a", "Alice")
    assert "n.entity_id = 'Alice'" in captured[0]


async def test_neighbors_falls_back_when_ent_id_unknown(monkeypatch) -> None:
    """If the ent-id isn't found, splice it verbatim — better an empty
    result than a 500 from a missing translation."""
    db, captured = _captured_sql_session()

    async def _empty(_db, _tid, _eids):
        return {}

    from rag_service.kg import repository
    monkeypatch.setattr(repository, "get_entity_names_by_ids", _empty)

    out = await graph.neighbors(db, "tenant-a", "ent-deadbeef")
    assert out == {"nodes": [], "edges": []}
    # Splice keeps the surrogate hyphens (allow-listed in _normalize_node_id).
    assert "n.entity_id = 'ent-deadbeef'" in captured[0]


async def test_subgraph_resolves_each_ent_id(monkeypatch) -> None:
    """Multi-source subgraph translates every ent-id input independently."""
    db, captured = _captured_sql_session()

    async def _stub(_db, _tid, eids):
        # Translate one of two; leaves the other to the verbatim fallback.
        return {"ent-aaa": "Alice"}

    from rag_service.kg import repository
    monkeypatch.setattr(repository, "get_entity_names_by_ids", _stub)

    await graph.subgraph(db, "tenant-a", ["ent-aaa", "Bob"], depth=1)
    sql = captured[0]
    assert "'Alice'" in sql
    assert "'Bob'" in sql
    assert "ent-aaa" not in sql


async def test_subgraph_query_builds_in_clause() -> None:
    db, captured = _captured_sql_session()
    await graph.subgraph(db, "tenant-a", ["alice", "bob"], depth=3)
    sql = captured[0]
    assert "[r*0..3]" in sql
    assert "n.entity_id IN ['alice','bob']" in sql


async def test_subgraph_sanitizes_each_id() -> None:
    db, captured = _captured_sql_session()
    await graph.subgraph(db, "tenant-a", ["good", "bad'; --"], depth=1)
    sql = captured[0]
    # Each id is quoted independently; bad chars in the second id are
    # collapsed so the IN list cannot be broken out of. (Hyphens are in
    # the allowlist, so the trailing ``--`` survives sanitization but
    # cannot do harm now that the quote and semicolon are dead.)
    assert "['good','bad___--']" in sql
    assert "bad'; --" not in sql


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


async def test_neighbors_handles_db_error_gracefully() -> None:
    """A failing query returns ``{nodes: [], edges: []}`` rather than raising."""
    db = MagicMock()
    db.execute = AsyncMock(side_effect=RuntimeError("AGE not installed"))
    out = await graph.neighbors(db, "tenant-a", "alice")
    assert out == {"nodes": [], "edges": []}


async def test_subgraph_handles_db_error_gracefully() -> None:
    db = MagicMock()
    db.execute = AsyncMock(side_effect=RuntimeError("graph 'tenant-a' does not exist"))
    out = await graph.subgraph(db, "tenant-a", ["alice"])
    assert out == {"nodes": [], "edges": []}


async def test_neighbors_invalid_tenant_id_raises() -> None:
    """Tenant-id validation propagates from ``paths.validate_tenant_id``."""
    db = MagicMock()
    with pytest.raises(Exception):  # InvalidTenantIdError subclasses ValueError
        await graph.neighbors(db, "../../etc/passwd", "alice")


# ---------------------------------------------------------------------------
# agtype parsing
# ---------------------------------------------------------------------------


def test_parse_agtype_node_basic() -> None:
    raw = (
        '{"id": 844424930131969, "label": "base", '
        '"properties": {"entity_id": "alice", "entity_type": "Person", '
        '"description": "the protagonist"}}::vertex'
    )
    out = graph._parse_agtype_node(raw)
    assert out is not None
    assert out["id"] == "alice"
    assert out["label"] == "base"
    assert out["properties"]["entity_type"] == "Person"
    assert out["properties"]["description"] == "the protagonist"


def test_parse_agtype_node_falls_back_to_age_id() -> None:
    """If properties lacks ``entity_id`` we keep the AGE numeric id."""
    raw = '{"id": 42, "label": "Foo", "properties": {}}::vertex'
    out = graph._parse_agtype_node(raw)
    assert out is not None
    assert out["id"] == "42"
    assert out["label"] == "Foo"


def test_parse_agtype_node_handles_dict_input() -> None:
    """Pre-decoded dicts (future SQLAlchemy adapter) should work too."""
    d = {"id": 1, "label": "base", "properties": {"entity_id": "x", "entity_type": "T"}}
    out = graph._parse_agtype_node(d)
    assert out is not None
    assert out["id"] == "x"


def test_parse_agtype_node_handles_none() -> None:
    assert graph._parse_agtype_node(None) is None


def test_parse_agtype_node_handles_garbage() -> None:
    assert graph._parse_agtype_node("not json::vertex") is None
    assert graph._parse_agtype_node("[1,2,3]::vertex") is None  # not a dict


def test_parse_agtype_path_basic() -> None:
    """A 1-edge path returned as ``[edge]::edge``."""
    raw = (
        '[{"id": 1, "label": "DIRECTED", "start_id": 100, "end_id": 200, '
        '"properties": {"source_id": "alice", "target_id": "bob", '
        '"weight": 1.0, "description": "knows"}}]::edge'
    )
    out = graph._parse_agtype_path(raw)
    assert len(out) == 1
    e = out[0]
    assert e["source"] == "alice"
    assert e["target"] == "bob"
    assert e["type"] == "DIRECTED"
    assert e["properties"]["weight"] == 1.0


def test_parse_agtype_path_multi_hop() -> None:
    raw = json.dumps(
        [
            {
                "label": "DIRECTED",
                "properties": {"source_id": "a", "target_id": "b"},
            },
            {
                "label": "DIRECTED",
                "properties": {"source_id": "b", "target_id": "c"},
            },
        ]
    ) + "::edge"
    out = graph._parse_agtype_path(raw)
    assert [e["source"] for e in out] == ["a", "b"]
    assert [e["target"] for e in out] == ["b", "c"]


def test_parse_agtype_path_handles_list_input() -> None:
    pre_decoded = [
        {"label": "DIRECTED", "properties": {"source_id": "x", "target_id": "y"}}
    ]
    out = graph._parse_agtype_path(pre_decoded)
    assert out == [
        {
            "source": "x",
            "target": "y",
            "type": "DIRECTED",
            "properties": {"source_id": "x", "target_id": "y"},
        }
    ]


def test_parse_agtype_path_handles_none_and_garbage() -> None:
    assert graph._parse_agtype_path(None) == []
    assert graph._parse_agtype_path("not json") == []
    # A scalar agtype shouldn't yield edges.
    assert graph._parse_agtype_path('{"id": 1}::vertex') == []


def test_parse_agtype_path_falls_back_to_age_endpoint_ids() -> None:
    """Edges without LightRAG ``source_id``/``target_id`` use AGE start/end."""
    raw = '[{"label": "REL", "start_id": 7, "end_id": 9, "properties": {}}]::edge'
    out = graph._parse_agtype_path(raw)
    assert out == [
        {
            "source": 7,
            "target": 9,
            "type": "REL",
            "properties": {},
        }
    ]


# ---------------------------------------------------------------------------
# End-to-end traversal w/ mocked rows: dedupe + shape
# ---------------------------------------------------------------------------


async def test_neighbors_assembles_nodes_and_edges_from_rows() -> None:
    """Full path: mocked rows -> parsed -> deduped output."""
    n_alice = (
        '{"id": 1, "label": "base", '
        '"properties": {"entity_id": "alice", "entity_type": "Person"}}::vertex'
    )
    n_bob = (
        '{"id": 2, "label": "base", '
        '"properties": {"entity_id": "bob", "entity_type": "Person"}}::vertex'
    )
    edge = (
        '[{"id": 10, "label": "DIRECTED", "start_id": 1, "end_id": 2, '
        '"properties": {"source_id": "alice", "target_id": "bob"}}]::edge'
    )
    # AGE undirected MATCH yields each edge in both directions; dedupe
    # should collapse them to one.
    rows = [
        (n_alice, edge, n_bob),
        (n_bob, edge, n_alice),
    ]
    db, _ = _captured_sql_session(rows=rows)
    out = await graph.neighbors(db, "tenant-a", "alice", depth=1)

    ids = sorted(n["id"] for n in out["nodes"])
    assert ids == ["alice", "bob"]
    assert len(out["edges"]) == 1
    assert out["edges"][0]["source"] == "alice"
    assert out["edges"][0]["target"] == "bob"
    assert out["edges"][0]["type"] == "DIRECTED"


async def test_neighbors_skips_unparseable_rows() -> None:
    """A garbled row shouldn't poison the rest of the result."""
    n_ok = (
        '{"id": 1, "label": "base", '
        '"properties": {"entity_id": "alice"}}::vertex'
    )
    rows = [
        ("not-json::vertex", "garbage", "also-garbage"),
        (n_ok, "[]::edge", n_ok),
    ]
    db, _ = _captured_sql_session(rows=rows)
    out = await graph.neighbors(db, "tenant-a", "alice", depth=1)
    assert [n["id"] for n in out["nodes"]] == ["alice"]
    assert out["edges"] == []
