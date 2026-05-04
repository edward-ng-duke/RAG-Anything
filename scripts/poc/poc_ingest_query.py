"""Single-PDF end-to-end PoC: parse via MinerU Cloud → ingest into RAGAnything
backed by LightRAG's Postgres storage stack → run a query → verify entities
landed in the right workspace via raw SQL → finalize cleanly.

Usage:
    cp scripts/poc/.env.poc.example scripts/poc/.env.poc
    # edit scripts/poc/.env.poc to fill in real values
    python scripts/poc/poc_ingest_query.py

This script intentionally has no CLI flags — it is a one-shot smoke test of
the wiring (PG KV/vector/graph/docstatus, MinerU cloud, LLM, embeddings).
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
import traceback
from functools import partial
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, unquote

# Make repo root importable so we can reuse `scripts.mineru_cloud_parse`.
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# .env.poc loading (prefer python-dotenv, fall back to a small parser)
# ---------------------------------------------------------------------------
def _load_env_poc(env_path: Path) -> None:
    """Load KEY=VALUE lines from env_path into os.environ (no override)."""
    if not env_path.exists():
        return
    try:
        from dotenv import load_dotenv  # type: ignore

        load_dotenv(dotenv_path=str(env_path), override=False)
        return
    except Exception:
        pass
    # Stdlib fallback: skip blanks/comments, parse KEY=VALUE, strip quotes.
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if (val.startswith('"') and val.endswith('"')) or (
            val.startswith("'") and val.endswith("'")
        ):
            val = val[1:-1]
        if key and key not in os.environ:
            os.environ[key] = val


REQUIRED_ENV_VARS = (
    "LLM_BASE_URL",
    "LLM_API_KEY",
    "LLM_MODEL",
    "EMBEDDING_BASE_URL",
    "EMBEDDING_API_KEY",
    "EMBEDDING_MODEL",
    "MINERU_CLOUD_API_KEY",
    "POSTGRES_DSN",
)


def _check_required_env() -> None:
    missing = [k for k in REQUIRED_ENV_VARS if not os.environ.get(k, "").strip()]
    if missing:
        print(
            "missing env vars: " + ", ".join(missing) + "\n"
            "Copy scripts/poc/.env.poc.example to scripts/poc/.env.poc and fill in real values.",
            file=sys.stderr,
        )
        sys.exit(2)


def _split_postgres_dsn(dsn: str) -> Dict[str, str]:
    """Parse a postgresql://user:password@host:port/database DSN into
    discrete LightRAG-expected env vars."""
    parsed = urlparse(dsn)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ValueError(f"POSTGRES_DSN must use postgresql:// scheme, got: {dsn!r}")
    return {
        "POSTGRES_HOST": parsed.hostname or "localhost",
        "POSTGRES_PORT": str(parsed.port or 5432),
        "POSTGRES_USER": unquote(parsed.username or ""),
        "POSTGRES_PASSWORD": unquote(parsed.password or ""),
        "POSTGRES_DATABASE": (parsed.path or "/").lstrip("/") or "postgres",
    }


# ---------------------------------------------------------------------------
# PDF picker — smallest .pdf under data/, fall back to samples/
# ---------------------------------------------------------------------------
def _pick_pdf() -> Path:
    data_dir = REPO_ROOT / "data"
    samples_dir = REPO_ROOT / "samples"

    def _smallest(root: Path) -> Optional[Path]:
        if not root.exists():
            return None
        candidates = [p for p in root.rglob("*.pdf") if p.is_file()]
        if not candidates:
            return None
        candidates.sort(key=lambda p: p.stat().st_size)
        return candidates[0]

    pick = _smallest(data_dir) or _smallest(samples_dir)
    if pick is None:
        raise FileNotFoundError(
            f"No .pdf found under {data_dir} or {samples_dir}. "
            "Drop a PDF into one of those directories and retry."
        )
    return pick


# ---------------------------------------------------------------------------
# LLM / embedding / vision wiring
# (Mirrors scripts/run_rag.py. We keep the helpers minimal and inline here so
# this PoC stays single-file and self-contained.)
# ---------------------------------------------------------------------------
def _build_llm_func(model: str, api_key: str, base_url: str):
    from lightrag.llm.openai import openai_complete_if_cache

    def llm_model_func(prompt, system_prompt=None, history_messages=None, **kwargs):
        return openai_complete_if_cache(
            model,
            prompt,
            system_prompt=system_prompt,
            history_messages=history_messages or [],
            api_key=api_key,
            base_url=base_url,
            **kwargs,
        )

    return llm_model_func


def _build_vision_func(vision_model: str, api_key: str, base_url: str, fallback_llm):
    from lightrag.llm.openai import openai_complete_if_cache

    def vision_model_func(
        prompt,
        system_prompt=None,
        history_messages=None,
        image_data=None,
        messages=None,
        **kwargs,
    ):
        if messages:
            return openai_complete_if_cache(
                vision_model,
                "",
                system_prompt=None,
                history_messages=[],
                messages=messages,
                api_key=api_key,
                base_url=base_url,
                **kwargs,
            )
        if image_data:
            user_msg = {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{image_data}"
                        },
                    },
                ],
            }
            msgs: List[Dict[str, Any]] = []
            if system_prompt:
                msgs.append({"role": "system", "content": system_prompt})
            msgs.append(user_msg)
            return openai_complete_if_cache(
                vision_model,
                "",
                system_prompt=None,
                history_messages=[],
                messages=msgs,
                api_key=api_key,
                base_url=base_url,
                **kwargs,
            )
        return fallback_llm(prompt, system_prompt, history_messages or [], **kwargs)

    return vision_model_func


# ---------------------------------------------------------------------------
# MinerU Cloud parser registration
#
# Choice: option (a) — import scripts.mineru_cloud_parse and reuse its
# parse_via_cloud() helper inside a thin Parser subclass that we register
# under the name "mineru-cloud". This lets us drive the standard
# RAGAnything.process_document_complete() path (which calls
# parser.parse_pdf(...)) without forking the existing cloud helper.
# ---------------------------------------------------------------------------
def _register_mineru_cloud_parser() -> None:
    from raganything.parser import Parser, register_parser, list_parsers
    from scripts.mineru_cloud_parse import parse_via_cloud

    if "mineru-cloud" in list_parsers():
        return

    class MineruCloudParser(Parser):
        """Adapts scripts.mineru_cloud_parse.parse_via_cloud to the Parser ABC."""

        def check_installation(self) -> bool:
            # Cloud parser only needs the API token + `requests`. We treat
            # token presence as the install signal.
            return bool(os.environ.get("MINERU_CLOUD_TOKEN"))

        def parse_pdf(self, pdf_path, output_dir=None, method="auto", lang=None, **kwargs):
            work_dir = output_dir or "./output/mineru_cloud"
            content_list, _image_dir = parse_via_cloud(pdf_path, work_dir=work_dir)
            return content_list

        def parse_document(
            self, file_path, method="auto", output_dir=None, lang=None, **kwargs
        ):
            return self.parse_pdf(
                pdf_path=file_path,
                output_dir=output_dir,
                method=method,
                lang=lang,
                **kwargs,
            )

    register_parser("mineru-cloud", MineruCloudParser)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main() -> int:
    # 1. Load .env.poc from this script's directory.
    env_path = Path(__file__).resolve().parent / ".env.poc"
    _load_env_poc(env_path)

    # 2. Validate required env vars.
    _check_required_env()

    llm_base_url = os.environ["LLM_BASE_URL"]
    llm_api_key = os.environ["LLM_API_KEY"]
    llm_model = os.environ["LLM_MODEL"]
    embed_base_url = os.environ["EMBEDDING_BASE_URL"]
    embed_api_key = os.environ["EMBEDDING_API_KEY"]
    embed_model = os.environ["EMBEDDING_MODEL"]
    mineru_api_key = os.environ["MINERU_CLOUD_API_KEY"]
    postgres_dsn = os.environ["POSTGRES_DSN"]
    embed_dim = int(os.environ.get("EMBEDDING_DIM", "1536"))

    # mineru_cloud_parse.py reads MINERU_CLOUD_TOKEN; bridge from our spec name.
    os.environ.setdefault("MINERU_CLOUD_TOKEN", mineru_api_key)

    # Decompose POSTGRES_DSN into the discrete vars LightRAG's PG backend reads
    # off os.environ. We use setdefault so an explicitly-set discrete var wins.
    for k, v in _split_postgres_dsn(postgres_dsn).items():
        os.environ.setdefault(k, v)

    # 3. Pick a PDF.
    picked = _pick_pdf()
    size_mb = picked.stat().st_size / (1024 * 1024)
    print(f"Picked: {picked} ({size_mb:.2f} MB)")

    # 4. Working dir + parser registration.
    working_dir = tempfile.mkdtemp(prefix="poc_")
    print(f"Working dir: {working_dir}")
    _register_mineru_cloud_parser()

    # 5. Build RAGAnything (mirroring scripts/run_rag.py).
    from lightrag.llm.openai import openai_embed
    from lightrag.utils import EmbeddingFunc
    from raganything import RAGAnything, RAGAnythingConfig

    config = RAGAnythingConfig(
        working_dir=working_dir,
        parser="mineru-cloud",
        # Multimodal off by default for PoC speed; flip back on if needed.
        enable_image_processing=False,
        enable_table_processing=True,
        enable_equation_processing=True,
        display_content_stats=True,
    )

    llm_func = _build_llm_func(llm_model, llm_api_key, llm_base_url)
    vision_func = _build_vision_func(llm_model, llm_api_key, llm_base_url, llm_func)

    embedding_func = EmbeddingFunc(
        embedding_dim=embed_dim,
        max_token_size=8192,
        func=partial(
            openai_embed.func,
            model=embed_model,
            api_key=embed_api_key,
            base_url=embed_base_url,
        ),
    )

    rag = RAGAnything(
        config=config,
        llm_model_func=llm_func,
        vision_model_func=vision_func,
        embedding_func=embedding_func,
        lightrag_kwargs={
            "kv_storage": "PGKVStorage",
            "vector_storage": "PGVectorStorage",
            "graph_storage": "PGGraphStorage",
            "doc_status_storage": "PGDocStatusStorage",
            "workspace": "poc-tenant-1",
            "vector_db_storage_cls_kwargs": {
                "cosine_better_than_threshold": 0.5,
            },
        },
    )

    # 6. Ingest.
    mineru_out = Path(working_dir) / "mineru"
    t0 = time.monotonic()
    await rag.process_document_complete(
        file_path=str(picked),
        output_dir=str(mineru_out),
    )
    print(f"Ingestion complete in {time.monotonic() - t0:.1f}s")

    # 7. Query.
    result = await rag.aquery("What is this document about?", mode="hybrid")
    if isinstance(result, dict):
        answer = str(result.get("answer") or result.get("response") or result)
        sources = result.get("sources") or result.get("context") or []
    else:
        answer = str(result)
        sources = []
    print("\n--- Answer (truncated to 500 chars) ---")
    print(answer[:500])
    print(f"\nlen(sources) = {len(sources)}")
    if sources:
        print("--- sources[:3] ---")
        for s in sources[:3]:
            preview = str(s)
            print(preview[:200] + ("..." if len(preview) > 200 else ""))

    # 8. Raw SQL: confirm entities landed in this tenant's workspace.
    #    Note: spec says `lightrag.LIGHTRAG_VDB_ENTITY`. LightRAG's PG backend
    #    creates this table without an explicit schema (i.e. in the search_path
    #    default, typically `public`). If your DB does not have a `lightrag`
    #    schema with this view, set search_path or change to public.
    try:
        import asyncpg  # type: ignore

        conn = await asyncpg.connect(dsn=postgres_dsn)
        try:
            try:
                count = await conn.fetchval(
                    "SELECT count(*) FROM lightrag.LIGHTRAG_VDB_ENTITY "
                    "WHERE workspace=$1",
                    "poc-tenant-1",
                )
            except Exception as e_schema:
                # Fall back to the default-search-path table if the qualified
                # name does not resolve.
                print(
                    f"[poc] lightrag-schema lookup failed ({e_schema!r}); "
                    "falling back to unqualified LIGHTRAG_VDB_ENTITY"
                )
                count = await conn.fetchval(
                    "SELECT count(*) FROM LIGHTRAG_VDB_ENTITY WHERE workspace=$1",
                    "poc-tenant-1",
                )
            print(f"Entities in workspace: {count}")
        finally:
            await conn.close()
    except ImportError:
        print(
            "[poc] asyncpg not installed; skipping raw-SQL entity count. "
            "Install with: pip install asyncpg",
            file=sys.stderr,
        )

    # 9. Clean shutdown.
    await rag.finalize_storages()
    print("Finalized cleanly")
    return 0


if __name__ == "__main__":
    try:
        rc = asyncio.run(main())
        sys.exit(rc)
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        sys.exit(1)
