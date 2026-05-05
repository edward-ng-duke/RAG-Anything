"""OpenAI-compatible LLM, embedding, and VLM factory functions.

Each factory returns a callable whose signature matches what LightRAG /
RAG-Anything expects to be passed as ``llm_model_func``,
``embedding_func``, and ``vision_model_func`` respectively.

The transport is plain ``httpx.AsyncClient`` against an OpenAI-compatible
``/chat/completions`` and ``/embeddings`` endpoint - no provider SDKs.
This keeps the wrappers tenant-agnostic and trivially mockable in tests
via ``httpx.MockTransport``.

NOTE: ``EmbeddingFunc`` is imported from ``lightrag.utils`` (verified at
LightRAG 1.4.x; dataclass fields are ``embedding_dim``, ``func``,
``max_token_size``).
"""

from __future__ import annotations

import base64
from typing import Any, Awaitable, Callable

import httpx
import numpy as np
from lightrag.utils import EmbeddingFunc

LLMFunc = Callable[..., Awaitable[str]]
VLMFunc = Callable[..., Awaitable[str]]


def make_llm_func(
    base_url: str,
    api_key: str,
    model: str,
    *,
    timeout: float = 60.0,
) -> LLMFunc:
    """Build an OpenAI-compatible chat completions wrapper.

    Returned callable shape (matches LightRAG's ``llm_model_func``)::

        async def fn(prompt: str,
                     system_prompt: str | None = None,
                     history_messages: list[dict] | None = None,
                     **kwargs) -> str
    """
    base = base_url.rstrip("/")

    async def _llm(
        prompt: str,
        system_prompt: str | None = None,
        history_messages: list[dict] | None = None,
        **kwargs: Any,
    ) -> str:
        messages: list[dict] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        if history_messages:
            messages.extend(history_messages)
        messages.append({"role": "user", "content": prompt})

        body: dict[str, Any] = {"model": model, "messages": messages}
        for k in ("temperature", "max_tokens", "top_p", "stream"):
            if k in kwargs and kwargs[k] is not None:
                body[k] = kwargs[k]
        body.setdefault("stream", False)

        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(
                f"{base}/chat/completions",
                json=body,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )
            r.raise_for_status()
            data = r.json()
            return data["choices"][0]["message"]["content"]

    return _llm


def make_embedding_func(
    base_url: str,
    api_key: str,
    model: str,
    *,
    embedding_dim: int = 1536,
    max_token_size: int = 8192,
    timeout: float = 60.0,
) -> EmbeddingFunc:
    """Build an OpenAI-compatible embeddings wrapper as ``EmbeddingFunc``."""
    base = base_url.rstrip("/")

    async def _embed(texts: list[str]) -> np.ndarray:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(
                f"{base}/embeddings",
                json={"model": model, "input": texts},
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )
            r.raise_for_status()
            data = r.json()
            # OpenAI returns {"data": [{"embedding": [...]}, ...]} preserving
            # input order; we trust that contract. LightRAG's adapter expects
            # a numpy array (it calls ``.size`` on the result), so coerce.
            return np.asarray(
                [item["embedding"] for item in data["data"]], dtype=np.float32
            )

    return EmbeddingFunc(
        embedding_dim=embedding_dim,
        max_token_size=max_token_size,
        func=_embed,
    )


def make_vlm_func(
    base_url: str,
    api_key: str,
    model: str | None,
    *,
    timeout: float = 60.0,
) -> VLMFunc | None:
    """Build an OpenAI-compatible VLM wrapper. Returns ``None`` if no model.

    Returned callable shape::

        async def fn(prompt: str,
                     image_data: bytes | str | list | None = None,
                     system_prompt: str | None = None,
                     history_messages: list[dict] | None = None,
                     **kwargs) -> str

    ``image_data`` may be raw bytes (base64-encoded into a data URI), a
    string (assumed to be a URL or data URI already), or a list of either.
    """
    if not model:
        return None

    base = base_url.rstrip("/")

    async def _vlm(
        prompt: str,
        image_data: Any = None,
        system_prompt: str | None = None,
        history_messages: list[dict] | None = None,
        **kwargs: Any,
    ) -> str:
        content: list[dict] = [{"type": "text", "text": prompt}]
        if image_data is not None:
            items = image_data if isinstance(image_data, list) else [image_data]
            for it in items:
                if isinstance(it, bytes):
                    b64 = base64.b64encode(it).decode("ascii")
                    content.append(
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                        }
                    )
                elif isinstance(it, str):
                    content.append({"type": "image_url", "image_url": {"url": it}})
                else:
                    raise TypeError(f"unsupported image_data item type: {type(it)}")

        messages: list[dict] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        if history_messages:
            messages.extend(history_messages)
        messages.append({"role": "user", "content": content})

        body: dict[str, Any] = {"model": model, "messages": messages}
        for k in ("temperature", "max_tokens"):
            if k in kwargs and kwargs[k] is not None:
                body[k] = kwargs[k]

        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(
                f"{base}/chat/completions",
                json=body,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )
            r.raise_for_status()
            data = r.json()
            return data["choices"][0]["message"]["content"]

    return _vlm
