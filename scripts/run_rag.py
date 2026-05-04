"""
End-to-end: parse PDF via mineru.net cloud → insert into RAGAnything → query.

All config is read from .env. Run:
    uv run python scripts/run_rag.py samples/sample.pdf

Required .env values:
    MINERU_CLOUD_TOKEN, LLM_BINDING_HOST, LLM_BINDING_API_KEY,
    LLM_MODEL, EMBEDDING_MODEL, EMBEDDING_DIM
Optional:
    VISION_MODEL (if blank → image processing disabled),
    WORKING_DIR, SUMMARY_LANGUAGE, ENABLE_TABLE_PROCESSING, ENABLE_EQUATION_PROCESSING
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from functools import partial
from pathlib import Path

# Make scripts/ importable when run as a module
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv(dotenv_path=".env", override=False)

from lightrag.llm.openai import openai_complete_if_cache, openai_embed
from lightrag.utils import EmbeddingFunc
from raganything import RAGAnything, RAGAnythingConfig

from scripts.mineru_cloud_parse import parse_via_cloud, _cfg


def _qwen3_extra_body() -> dict:
    """Force Qwen3 thinking off — endpoint returns content=null otherwise."""
    return {
        "chat_template_kwargs": {"enable_thinking": False},
    }


def _build_llm_func(model: str, api_key: str, base_url: str):
    extra_body = _qwen3_extra_body()

    def llm_model_func(prompt, system_prompt=None, history_messages=None, **kwargs):
        merged_extra = {**extra_body, **kwargs.pop("extra_body", {})}
        return openai_complete_if_cache(
            model,
            prompt,
            system_prompt=system_prompt,
            history_messages=history_messages or [],
            api_key=api_key,
            base_url=base_url,
            extra_body=merged_extra,
            **kwargs,
        )

    return llm_model_func


def _build_vision_func(vision_model: str, api_key: str, base_url: str, fallback_llm):
    extra_body = _qwen3_extra_body()

    def vision_model_func(
        prompt,
        system_prompt=None,
        history_messages=None,
        image_data=None,
        messages=None,
        **kwargs,
    ):
        merged_extra = {**extra_body, **kwargs.pop("extra_body", {})}
        if messages:
            return openai_complete_if_cache(
                vision_model,
                "",
                system_prompt=None,
                history_messages=[],
                messages=messages,
                api_key=api_key,
                base_url=base_url,
                extra_body=merged_extra,
                **kwargs,
            )
        if image_data:
            user_msg = {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{image_data}"},
                    },
                ],
            }
            msgs = []
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
                extra_body=merged_extra,
                **kwargs,
            )
        return fallback_llm(prompt, system_prompt, history_messages or [], **kwargs)

    return vision_model_func


async def run(pdf_path: str, queries: list[str]) -> None:
    base_url = _cfg("LLM_BINDING_HOST")
    api_key = _cfg("LLM_BINDING_API_KEY")
    llm_model = _cfg("LLM_MODEL")
    embed_base_url = os.getenv("EMBEDDING_BINDING_HOST", base_url)
    embed_api_key = os.getenv("EMBEDDING_BINDING_API_KEY", api_key)
    embed_model = _cfg("EMBEDDING_MODEL")
    embed_dim = int(_cfg("EMBEDDING_DIM"))
    vision_model = os.getenv("VISION_MODEL", "").strip()
    working_dir = os.getenv("WORKING_DIR", "./rag_storage")

    enable_image = bool(vision_model)
    if not enable_image:
        print(
            "[run_rag] VISION_MODEL is empty → image processing disabled",
            file=sys.stderr,
        )

    config = RAGAnythingConfig(
        working_dir=working_dir,
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

    # 1. parse via cloud
    content_list, _image_dir = parse_via_cloud(pdf_path)

    # 2. insert into RAGAnything
    print("[run_rag] inserting content_list into RAGAnything...", flush=True)
    await rag.insert_content_list(
        content_list=content_list,
        file_path=Path(pdf_path).name,
        display_stats=True,
    )

    # 3. query
    for q in queries:
        print(f"\n[run_rag] query: {q}", flush=True)
        answer = await rag.aquery(q, mode="hybrid")
        print(f"[run_rag] answer:\n{answer}", flush=True)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("pdf_path", help="path to PDF (or any mineru-supported doc)")
    p.add_argument(
        "--query",
        "-q",
        action="append",
        default=[],
        help="query to run after ingestion (can be passed multiple times)",
    )
    args = p.parse_args()

    queries = args.query or [
        "这篇文档的主要内容是什么？请用三到五点概括。",
        "Summarize the document in 3-5 bullet points.",
    ]

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    asyncio.run(run(args.pdf_path, queries))


if __name__ == "__main__":
    main()
