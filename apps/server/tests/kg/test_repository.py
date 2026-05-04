"""Unit tests for ``rag_service.kg.repository``.

We can't run the production schema verbatim (LightRAG's ``lightrag.*``
schema-qualified tables, ``VECTOR(...)`` columns, ``ARRAY`` columns) on
SQLite, so the tests:

1. Monkeypatch ``repository.TABLE_PREFIX`` to ``""`` so the helpers issue
   unqualified table names.
2. Stand up minimal SQLite-compatible tables that share the column names
   the repository references — that's enough surface to exercise
   filtering, pagination, workspace isolation, and the
   missing-table-graceful-stats path.

This keeps the tests hermetic (no Postgres, no LightRAG bootstrap) while
still verifying the SQL the repository actually emits.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from rag_service.kg import repository


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def _no_schema_prefix(monkeypatch):
    """SQLite has no ``lightrag.`` schema; tell the repository to skip it.

    Yielded as a fixture (not autouse) so any future tests that *do* want a
    qualified table can opt out.
    """
    monkeypatch.setattr(repository, "TABLE_PREFIX", "")


@pytest.fixture
async def session(_no_schema_prefix):
    """Per-test async SQLite session with the three KG tables created."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        # Mirrors LightRAG's column names; types are loose because SQLite
        # doesn't have VECTOR / VARCHAR[] and the repo never reads them.
        await conn.execute(
            text(
                """
                CREATE TABLE lightrag_vdb_entity (
                    id TEXT,
                    workspace TEXT,
                    entity_name TEXT,
                    content TEXT,
                    content_vector TEXT,
                    file_path TEXT,
                    PRIMARY KEY (workspace, id)
                )
                """
            )
        )
        await conn.execute(
            text(
                """
                CREATE TABLE lightrag_vdb_relation (
                    id TEXT,
                    workspace TEXT,
                    source_id TEXT,
                    target_id TEXT,
                    content TEXT,
                    file_path TEXT,
                    PRIMARY KEY (workspace, id)
                )
                """
            )
        )
        await conn.execute(
            text(
                """
                CREATE TABLE lightrag_doc_chunks (
                    id TEXT,
                    workspace TEXT,
                    full_doc_id TEXT,
                    chunk_order_index INTEGER,
                    tokens INTEGER,
                    content TEXT,
                    file_path TEXT,
                    PRIMARY KEY (workspace, id)
                )
                """
            )
        )

    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        yield s
    await engine.dispose()


async def _seed_entity(
    session,
    *,
    id: str,
    workspace: str = "tenant-a",
    entity_name: str = "Alice",
    content: str = "",
    file_path: str | None = None,
) -> None:
    await session.execute(
        text(
            "INSERT INTO lightrag_vdb_entity "
            "(id, workspace, entity_name, content, file_path) "
            "VALUES (:id, :ws, :name, :content, :fp)"
        ),
        {"id": id, "ws": workspace, "name": entity_name, "content": content, "fp": file_path},
    )
    await session.commit()


async def _seed_relation(
    session,
    *,
    id: str,
    workspace: str = "tenant-a",
    source_id: str,
    target_id: str,
    content: str = "",
    file_path: str | None = None,
) -> None:
    await session.execute(
        text(
            "INSERT INTO lightrag_vdb_relation "
            "(id, workspace, source_id, target_id, content, file_path) "
            "VALUES (:id, :ws, :src, :tgt, :content, :fp)"
        ),
        {
            "id": id,
            "ws": workspace,
            "src": source_id,
            "tgt": target_id,
            "content": content,
            "fp": file_path,
        },
    )
    await session.commit()


async def _seed_chunk(
    session,
    *,
    id: str,
    workspace: str = "tenant-a",
    full_doc_id: str = "doc-1",
    chunk_order_index: int = 0,
    tokens: int = 10,
    content: str = "",
    file_path: str | None = None,
) -> None:
    await session.execute(
        text(
            "INSERT INTO lightrag_doc_chunks "
            "(id, workspace, full_doc_id, chunk_order_index, tokens, content, file_path) "
            "VALUES (:id, :ws, :fdi, :ord, :tok, :content, :fp)"
        ),
        {
            "id": id,
            "ws": workspace,
            "fdi": full_doc_id,
            "ord": chunk_order_index,
            "tok": tokens,
            "content": content,
            "fp": file_path,
        },
    )
    await session.commit()


# ---------------------------------------------------------------------------
# list_entities
# ---------------------------------------------------------------------------


async def test_list_entities_basic(session) -> None:
    for i, name in enumerate(["alice", "bob", "carol"]):
        await _seed_entity(session, id=f"e{i}", entity_name=name)

    out = await repository.list_entities(session, "tenant-a")
    assert [r["entity_name"] for r in out["items"]] == ["alice", "bob", "carol"]
    assert out["next_cursor"] is None


async def test_list_entities_filter_search(session) -> None:
    """``lower(col) LIKE lower(:p)`` should match case-insensitively."""
    await _seed_entity(session, id="e1", entity_name="Alice Wong")
    await _seed_entity(session, id="e2", entity_name="Bob Smith")
    await _seed_entity(session, id="e3", entity_name="Alicia Keys")

    out = await repository.list_entities(session, "tenant-a", search="alic")
    names = sorted(r["entity_name"] for r in out["items"])
    assert names == ["Alice Wong", "Alicia Keys"]


async def test_list_entities_pagination(session) -> None:
    for i in range(5):
        # zero-padded ids so lexical ordering matches insertion order
        await _seed_entity(session, id=f"e{i:02d}", entity_name=f"n{i}")

    page1 = await repository.list_entities(session, "tenant-a", limit=2)
    assert [r["id"] for r in page1["items"]] == ["e00", "e01"]
    assert page1["next_cursor"] is not None

    page2 = await repository.list_entities(
        session, "tenant-a", limit=2, cursor=page1["next_cursor"]
    )
    assert [r["id"] for r in page2["items"]] == ["e02", "e03"]
    assert page2["next_cursor"] is not None

    page3 = await repository.list_entities(
        session, "tenant-a", limit=2, cursor=page2["next_cursor"]
    )
    assert [r["id"] for r in page3["items"]] == ["e04"]
    # Last page: we got fewer than limit+1 rows back, so no further cursor.
    assert page3["next_cursor"] is None


async def test_list_entities_workspace_isolation(session) -> None:
    await _seed_entity(session, id="e1", workspace="tenant-a", entity_name="mine")
    await _seed_entity(session, id="e1", workspace="tenant-b", entity_name="theirs")

    out = await repository.list_entities(session, "tenant-a")
    assert [r["entity_name"] for r in out["items"]] == ["mine"]


async def test_list_entities_bad_cursor_falls_back_to_first_page(session) -> None:
    """Malformed cursor degrades to "no cursor" instead of erroring."""
    await _seed_entity(session, id="e1", entity_name="alice")

    out = await repository.list_entities(session, "tenant-a", cursor="not-base64!!")
    assert [r["id"] for r in out["items"]] == ["e1"]


# ---------------------------------------------------------------------------
# get_entity
# ---------------------------------------------------------------------------


async def test_get_entity_returns_dict_or_none(session) -> None:
    await _seed_entity(session, id="e1", entity_name="alice", content="bio")

    hit = await repository.get_entity(session, "tenant-a", "e1")
    assert hit is not None
    assert hit["entity_name"] == "alice"
    assert hit["content"] == "bio"

    miss_id = await repository.get_entity(session, "tenant-a", "does-not-exist")
    assert miss_id is None

    # Cross-tenant lookup is indistinguishable from a miss.
    miss_tenant = await repository.get_entity(session, "tenant-b", "e1")
    assert miss_tenant is None


# ---------------------------------------------------------------------------
# list_relations
# ---------------------------------------------------------------------------


async def test_list_relations_basic(session) -> None:
    await _seed_relation(session, id="r1", source_id="alice", target_id="bob")
    await _seed_relation(session, id="r2", source_id="alice", target_id="carol")
    await _seed_relation(session, id="r3", source_id="bob", target_id="carol")

    all_out = await repository.list_relations(session, "tenant-a")
    assert {r["id"] for r in all_out["items"]} == {"r1", "r2", "r3"}

    src_out = await repository.list_relations(session, "tenant-a", source="alice")
    assert {r["id"] for r in src_out["items"]} == {"r1", "r2"}

    tgt_out = await repository.list_relations(session, "tenant-a", target="carol")
    assert {r["id"] for r in tgt_out["items"]} == {"r2", "r3"}

    pair_out = await repository.list_relations(
        session, "tenant-a", source="alice", target="carol"
    )
    assert [r["id"] for r in pair_out["items"]] == ["r2"]


async def test_list_relations_workspace_isolation(session) -> None:
    await _seed_relation(
        session, id="r1", workspace="tenant-a", source_id="a", target_id="b"
    )
    await _seed_relation(
        session, id="r1", workspace="tenant-b", source_id="x", target_id="y"
    )

    out = await repository.list_relations(session, "tenant-a")
    assert [r["source_id"] for r in out["items"]] == ["a"]


# ---------------------------------------------------------------------------
# get_chunk
# ---------------------------------------------------------------------------


async def test_get_chunk_returns_dict_or_none(session) -> None:
    await _seed_chunk(
        session,
        id="c1",
        full_doc_id="doc-7",
        chunk_order_index=3,
        tokens=42,
        content="hello",
    )

    hit = await repository.get_chunk(session, "tenant-a", "c1")
    assert hit is not None
    assert hit["full_doc_id"] == "doc-7"
    assert hit["chunk_order_index"] == 3
    assert hit["tokens"] == 42
    assert hit["content"] == "hello"

    assert await repository.get_chunk(session, "tenant-a", "missing") is None
    assert await repository.get_chunk(session, "tenant-b", "c1") is None


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------


async def test_stats_counts(session) -> None:
    await _seed_entity(session, id="e1", entity_name="a")
    await _seed_entity(session, id="e2", entity_name="b")
    await _seed_relation(session, id="r1", source_id="a", target_id="b")
    await _seed_chunk(session, id="c1")
    await _seed_chunk(session, id="c2")
    await _seed_chunk(session, id="c3")

    # Other-tenant rows must not bleed into our counts.
    await _seed_entity(session, id="e9", workspace="tenant-b", entity_name="x")

    out = await repository.stats(session, "tenant-a")
    assert out == {"entities": 2, "relations": 1, "chunks": 3}


async def test_stats_handles_missing_table_gracefully(monkeypatch) -> None:
    """A tenant that has never ingested will have no LightRAG tables yet.

    Stats should collapse missing tables to ``0`` rather than 500ing.
    """
    monkeypatch.setattr(repository, "TABLE_PREFIX", "")
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sm = async_sessionmaker(engine, expire_on_commit=False)
    try:
        # Create only the entities table; relations + chunks are absent.
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    CREATE TABLE lightrag_vdb_entity (
                        id TEXT,
                        workspace TEXT,
                        entity_name TEXT,
                        content TEXT,
                        content_vector TEXT,
                        file_path TEXT,
                        PRIMARY KEY (workspace, id)
                    )
                    """
                )
            )
        async with sm() as s:
            await s.execute(
                text(
                    "INSERT INTO lightrag_vdb_entity "
                    "(id, workspace, entity_name) VALUES ('e1', 'tenant-a', 'a')"
                )
            )
            await s.commit()
            out = await repository.stats(s, "tenant-a")
        assert out == {"entities": 1, "relations": 0, "chunks": 0}
    finally:
        await engine.dispose()
