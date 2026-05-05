"""AGE cypher wrappers for graph traversal queries.

Notes
-----
- Apache AGE stores graphs in PG and is queried via the ``cypher()`` SQL
  function (``ag_catalog.cypher('graph_name', $$ ... $$) AS (...)``).
- Workspace isolation: each workspace gets its own AGE graph named
  ``{workspace_namespace}`` (LightRAG convention — see
  ``lightrag.kg.postgres_impl._get_workspace_graph_name``).
- Cypher injection: AGE's ``cypher()`` does accept agtype parameters, but
  they're awkward to thread through SQLAlchemy ``text()`` and the LightRAG
  write paths still embed entity IDs directly in the cypher source. We
  follow the same approach for these read paths and SANITIZE entity IDs
  via :func:`_normalize_node_id` before splicing.

agtype parsing
--------------
AGE returns ``agtype`` columns as strings shaped like:

* single vertex/edge: ``{"id": 123, "label": "base", "properties": {...}}::vertex``
* array (e.g. variable-length relationship paths):
  ``[{...}::edge, {...}::edge]`` — but in practice asyncpg/pg drivers
  return the whole array as a single string with a trailing ``::vertex``
  or ``::edge`` after the closing ``]``.

LightRAG's ``_record_to_dict`` (postgres_impl.py ~L4742) handles both
shapes by ``rfind("::")``. We mirror that. Best-effort — the SQLAlchemy
driver may already JSON-decode some shapes; the parsers accept dicts/lists
as well as strings.
"""

from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# LightRAG names every workspace's AGE graph as
# ``re.sub(r"[^a-zA-Z0-9_]", "_", workspace) + "_" + namespace``
# (see ``lightrag.kg.postgres_impl._get_workspace_graph_name``). The
# ``chunk_entity_relation`` namespace is the one used for the KG that
# upserts entities/relations from document chunks — i.e. exactly the
# graph our /kg/* routes traverse.
_GRAPH_NAMESPACE = "chunk_entity_relation"


# Allowed characters in a sanitized cypher-embedded entity_id. Keep this
# tight: alphanumerics, underscore, hyphen, and colon (LightRAG sometimes
# uses ``ns:name`` style identifiers). Everything else collapses to ``_``
# so the resulting string can never break out of the surrounding quotes.
_NORMALIZE_RE = re.compile(r"[^a-zA-Z0-9_\-:]")


def _normalize_node_id(entity_id: str) -> str:
    """Defeat cypher-string injection by squashing unsafe characters.

    Mirrors the *intent* (not the exact bytewise behaviour) of LightRAG's
    ``_normalize_node_id``: that one escapes quotes/backslashes; we go
    further and strip them entirely so the splice site cannot be
    weaponised even if an upstream layer forgets to quote.
    """
    if not isinstance(entity_id, str) or not entity_id:
        raise ValueError("entity_id must be non-empty string")
    safe = _NORMALIZE_RE.sub("_", entity_id)
    if not safe:
        raise ValueError(f"entity_id reduces to empty: {entity_id!r}")
    return safe


def _graph_name(tenant_id: str) -> str:
    """Map a tenant_id to its AGE graph name.

    Mirrors ``lightrag.kg.postgres_impl.PostgreSQLDB._get_workspace_graph_name``
    bytewise: PG identifier-safe substitution
    (``[^a-zA-Z0-9_] -> _``) followed by ``_<namespace>``. Without this the
    cypher queries target a graph that doesn't exist (e.g.
    ``onyx-abc-123`` instead of ``onyx_abc_123_chunk_entity_relation``)
    and AGE returns an empty result set silently.

    Reuses ``paths.validate_tenant_id`` so the graph name shares the same
    charset rules as on-disk tenant directories. The lazy import avoids
    a circular dependency at module load.
    """
    import re as _re
    from rag_service.core.paths import validate_tenant_id

    validate_tenant_id(tenant_id)
    safe_workspace = _re.sub(r"[^a-zA-Z0-9_]", "_", tenant_id.strip())
    safe_namespace = _re.sub(r"[^a-zA-Z0-9_]", "_", _GRAPH_NAMESPACE)
    return f"{safe_workspace}_{safe_namespace}"


# ---------------------------------------------------------------------------
# Public traversal helpers
# ---------------------------------------------------------------------------


async def neighbors(
    db: AsyncSession,
    tenant_id: str,
    entity_id: str,
    *,
    depth: int = 1,
) -> dict:
    """Return ``{nodes, edges}`` for ``entity_id`` and its k-hop neighbourhood.

    ``depth`` is constrained to ``{1, 2, 3}`` — wider traversals on a
    real-world KG explode quadratically and we'd rather force callers to
    paginate via repeated 1-hop calls than ship a footgun.
    """
    if depth not in (1, 2, 3):
        raise ValueError("depth must be 1, 2, or 3")

    safe_eid = _normalize_node_id(entity_id)
    g = _graph_name(tenant_id)

    cypher_q = (
        f"MATCH (n)-[r*1..{depth}]-(m) "
        f"WHERE n.entity_id = '{safe_eid}' "
        f"RETURN DISTINCT n, r, m"
    )
    sql = (
        f"SELECT * FROM ag_catalog.cypher('{g}', $$ {cypher_q} $$) "
        f"AS (n agtype, r agtype, m agtype)"
    )
    return await _run_traversal(db, sql)


async def subgraph(
    db: AsyncSession,
    tenant_id: str,
    entity_ids: list[str],
    *,
    depth: int = 2,
) -> dict:
    """Multi-source subgraph rooted at the given ``entity_ids``.

    Empty input short-circuits to ``{nodes: [], edges: []}`` rather than
    issuing a degenerate ``IN []`` query.
    """
    if depth not in (1, 2, 3):
        raise ValueError("depth must be 1, 2, or 3")
    if not entity_ids:
        return {"nodes": [], "edges": []}

    safe_ids = [_normalize_node_id(e) for e in entity_ids]
    g = _graph_name(tenant_id)
    id_list = ",".join(f"'{s}'" for s in safe_ids)

    # ``r*0..depth`` includes the root nodes themselves (a 0-length path)
    # so single-node lookups still surface in ``nodes``.
    cypher_q = (
        f"MATCH (n)-[r*0..{depth}]-(m) "
        f"WHERE n.entity_id IN [{id_list}] "
        f"RETURN DISTINCT n, r, m"
    )
    sql = (
        f"SELECT * FROM ag_catalog.cypher('{g}', $$ {cypher_q} $$) "
        f"AS (n agtype, r agtype, m agtype)"
    )
    return await _run_traversal(db, sql)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


async def _run_traversal(db: AsyncSession, sql: str) -> dict:
    """Execute a cypher traversal and assemble the node/edge dict.

    Any DB-side error (graph missing, AGE not installed, malformed result)
    degrades to an empty result rather than 500ing — KG endpoints are
    advisory and the caller cannot fix a missing graph by retrying.
    """
    try:
        rows = (await db.execute(text(sql))).all()
    except Exception:  # noqa: BLE001 — see docstring
        return {"nodes": [], "edges": []}

    nodes_dict: dict[str, dict] = {}
    edges: list[dict] = []

    for row in rows:
        # Defensive: row arity should be (n, r, m); short-rows are skipped.
        if len(row) < 3:
            continue
        n_raw, r_raw, m_raw = row[0], row[1], row[2]
        for raw in (n_raw, m_raw):
            node = _parse_agtype_node(raw)
            if node and node["id"] and node["id"] not in nodes_dict:
                nodes_dict[node["id"]] = node
        for e in _parse_agtype_path(r_raw):
            edges.append(e)

    # Dedupe edges by (source, target, type). AGE's MATCH on undirected
    # patterns yields each edge twice (once per direction).
    seen: set[tuple[Any, Any, Any]] = set()
    deduped: list[dict] = []
    for e in edges:
        k = (e.get("source"), e.get("target"), e.get("type"))
        if k not in seen:
            seen.add(k)
            deduped.append(e)

    return {"nodes": list(nodes_dict.values()), "edges": deduped}


def _strip_agtype_suffix(s: str) -> str:
    """Drop a trailing ``::vertex`` / ``::edge`` / ``::path`` etc.

    Mirrors LightRAG's ``parse_agtype_string`` which uses ``rfind("::")``.
    Keeps any internal ``::`` (e.g. inside a property string) intact.
    """
    if "::" not in s:
        return s
    last = s.rfind("::")
    return s[:last] if last > 0 else s


def _parse_agtype_node(raw: Any) -> dict | None:
    """Parse one agtype vertex into ``{id, label, properties}``.

    Accepts already-decoded dicts in addition to raw strings so callers can
    feed in pre-parsed rows from a future SQLAlchemy type adapter.
    """
    if raw is None:
        return None
    if isinstance(raw, dict):
        d = raw
    else:
        s = str(raw)
        s = _strip_agtype_suffix(s)
        try:
            d = json.loads(s)
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(d, dict):
            return None

    props = d.get("properties") or {}
    if not isinstance(props, dict):
        props = {}
    # ``entity_id`` is the LightRAG-canonical identifier; fall back to
    # AGE's internal numeric ``id`` so callers can still de-duplicate on
    # rows that didn't go through the LightRAG writer (e.g. legacy data).
    nid = props.get("entity_id") or d.get("id") or ""
    return {
        "id": str(nid) if nid != "" else "",
        "label": d.get("label") or props.get("entity_type") or "Entity",
        "properties": props,
    }


def _parse_agtype_path(raw: Any) -> list[dict]:
    """Parse a variable-length relationship binding into a list of edges.

    AGE returns ``[r*1..k]`` as either:

    * a JSON array string ``[{...}::edge, {...}::edge]`` (whole-array
      suffix), or
    * an already-decoded list (when SQLAlchemy / the agtype codec
      pre-parsed it).

    Each element should look like a vertex record but with ``::edge`` and
    ``start_id`` / ``end_id`` plus ``properties.source_id`` /
    ``properties.target_id`` from LightRAG.
    """
    if raw is None:
        return []
    arr: Any
    if isinstance(raw, list):
        arr = raw
    else:
        s = str(raw)
        s = _strip_agtype_suffix(s)
        try:
            arr = json.loads(s)
        except (json.JSONDecodeError, ValueError):
            return []
    if not isinstance(arr, list):
        return []

    edges: list[dict] = []
    for item in arr:
        if isinstance(item, str):
            stripped = _strip_agtype_suffix(item)
            try:
                item = json.loads(stripped)
            except (json.JSONDecodeError, ValueError):
                continue
        if not isinstance(item, dict):
            continue
        props = item.get("properties") or {}
        if not isinstance(props, dict):
            props = {}
        edges.append(
            {
                # LightRAG stores the entity-ID-shaped endpoints in
                # properties. AGE's own numeric ``start_id`` / ``end_id``
                # are kept as a fallback for non-LightRAG-written edges.
                "source": props.get("source_id")
                or props.get("source")
                or item.get("start_id"),
                "target": props.get("target_id")
                or props.get("target")
                or item.get("end_id"),
                "type": item.get("label") or props.get("relation") or "RELATED",
                "properties": props,
            }
        )
    return edges
