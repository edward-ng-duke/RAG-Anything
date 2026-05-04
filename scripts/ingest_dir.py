"""
Batch-ingest every PDF under a directory into a RAGAnything working dir.

Mirrors scripts/run_rag.py initialization (Qwen3 thinking-off LLM/vision funcs,
OpenAI-compatible embedding) but iterates over a directory and skips queries.

Usage:
    uv run python scripts/ingest_dir.py data/不错的简历 \
        --working-dir ./rag_storage_resumes
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from functools import partial
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv(dotenv_path=".env", override=False)

from lightrag.llm.openai import openai_complete_if_cache, openai_embed
from lightrag.utils import EmbeddingFunc
from raganything import RAGAnything, RAGAnythingConfig

from scripts.mineru_cloud_parse import parse_via_cloud, _cfg
from scripts.run_rag import _build_llm_func, _build_vision_func


async def run_dir(
    input_dir: Path, working_dir: Path, glob_pattern: str
) -> None:
    base_url = _cfg("LLM_BINDING_HOST")
    api_key = _cfg("LLM_BINDING_API_KEY")
    llm_model = _cfg("LLM_MODEL")
    embed_base_url = os.getenv("EMBEDDING_BINDING_HOST", base_url)
    embed_api_key = os.getenv("EMBEDDING_BINDING_API_KEY", api_key)
    embed_model = _cfg("EMBEDDING_MODEL")
    embed_dim = int(_cfg("EMBEDDING_DIM"))
    vision_model = os.getenv("VISION_MODEL", "").strip()

    enable_image = bool(vision_model)
    if not enable_image:
        print(
            "[ingest_dir] VISION_MODEL is empty → image processing disabled",
            file=sys.stderr,
        )

    config = RAGAnythingConfig(
        working_dir=str(working_dir),
        enable_image_processing=enable_image,
        enable_table_processing=os.getenv("ENABLE_TABLE_PROCESSING", "true").lower()
        == "true",
        enable_equation_processing=os.getenv(
            "ENABLE_EQUATION_PROCESSING", "true"
        ).lower()
        == "true",
        display_content_stats=True,
    )

    llm_func = _build_llm_func(llm_model, api_key, base_url)
    vision_func = (
        _build_vision_func(vision_model, api_key, base_url, llm_func)
        if enable_image
        else llm_func
    )

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
    )

    pdfs = sorted(input_dir.glob(glob_pattern))
    if not pdfs:
        raise SystemExit(
            f"[ingest_dir] no files match {glob_pattern!r} under {input_dir}"
        )

    print(
        f"[ingest_dir] found {len(pdfs)} file(s) under {input_dir} → working_dir={working_dir}",
        flush=True,
    )

    for i, pdf in enumerate(pdfs, start=1):
        print(f"\n[ingest_dir] ({i}/{len(pdfs)}) parsing {pdf.name}", flush=True)
        content_list, _image_dir = parse_via_cloud(pdf)

        print(
            f"[ingest_dir] ({i}/{len(pdfs)}) inserting {pdf.name} "
            f"({len(content_list)} blocks)",
            flush=True,
        )
        await rag.insert_content_list(
            content_list=content_list,
            file_path=pdf.name,
            display_stats=True,
        )

    print(
        f"\n[ingest_dir] done. {len(pdfs)} file(s) ingested into {working_dir}",
        flush=True,
    )


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("input_dir", help="directory containing PDFs to ingest")
    p.add_argument(
        "--working-dir",
        default="./rag_storage_resumes",
        help="RAGAnything working dir (default: ./rag_storage_resumes)",
    )
    p.add_argument(
        "--glob",
        default="**/*.pdf",
        help="glob pattern, relative to input_dir (default: **/*.pdf)",
    )
    args = p.parse_args()

    input_dir = Path(args.input_dir).resolve()
    if not input_dir.is_dir():
        raise SystemExit(f"[ingest_dir] not a directory: {input_dir}")
    working_dir = Path(args.working_dir).resolve()
    working_dir.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    asyncio.run(run_dir(input_dir, working_dir, args.glob))


if __name__ == "__main__":
    main()
