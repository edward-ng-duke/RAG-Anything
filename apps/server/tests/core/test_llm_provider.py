"""Tests for ``rag_service.core.llm_provider`` factory functions.

Uses ``httpx.MockTransport`` to intercept outbound requests; no real
network calls. We monkey-patch ``httpx.AsyncClient`` inside the module
under test so the factory's internal ``async with httpx.AsyncClient(...)``
block actually uses our transport.
"""

from __future__ import annotations

# Set required env vars before any rag_service import so the lazy
# ``settings`` singleton can be constructed without a real .env file.
import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://x@x/x")
os.environ.setdefault("REDIS_URL", "redis://x")
os.environ.setdefault("INTERNAL_TOKEN", "x")
os.environ.setdefault("LLM_BASE_URL", "x")
os.environ.setdefault("LLM_API_KEY", "x")
os.environ.setdefault("LLM_MODEL", "x")
os.environ.setdefault("EMBEDDING_BASE_URL", "x")
os.environ.setdefault("EMBEDDING_API_KEY", "x")
os.environ.setdefault("EMBEDDING_MODEL", "x")

import json  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402

from rag_service.core import llm_provider as mod  # noqa: E402
from rag_service.core.llm_provider import (  # noqa: E402
    make_embedding_func,
    make_llm_func,
    make_vlm_func,
)


def _patch_transport(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    """Replace ``httpx.AsyncClient`` inside the module under test with one
    that routes all requests through ``MockTransport(handler)``."""
    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient

    def factory(**kw):
        return real(transport=transport, **kw)

    monkeypatch.setattr(mod.httpx, "AsyncClient", factory)


# ---------------------------------------------------------------------------
# make_llm_func
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_func_basic(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.read()
        captured["headers"] = dict(request.headers)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "hello"}}]},
        )

    _patch_transport(monkeypatch, handler)

    fn = make_llm_func("http://test/v1", "sk-test", "gpt-test")
    result = await fn(
        "hi",
        system_prompt="be brief",
        history_messages=[
            {"role": "user", "content": "earlier"},
            {"role": "assistant", "content": "ok"},
        ],
    )

    assert result == "hello"
    assert captured["url"] == "http://test/v1/chat/completions"
    body = json.loads(captured["body"])
    assert body["model"] == "gpt-test"
    assert body["stream"] is False
    msgs = body["messages"]
    # system first, then history in order, then the new user prompt last
    assert msgs[0] == {"role": "system", "content": "be brief"}
    assert msgs[1] == {"role": "user", "content": "earlier"}
    assert msgs[2] == {"role": "assistant", "content": "ok"}
    assert msgs[-1] == {"role": "user", "content": "hi"}
    assert "Bearer sk-test" in captured["headers"]["authorization"]


@pytest.mark.asyncio
async def test_llm_func_no_system_no_history(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.read()
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "x"}}]}
        )

    _patch_transport(monkeypatch, handler)

    fn = make_llm_func("http://test/v1/", "sk-test", "gpt-test")  # trailing slash
    out = await fn("just-user")
    assert out == "x"

    body = json.loads(captured["body"])
    assert body["messages"] == [{"role": "user", "content": "just-user"}]


@pytest.mark.asyncio
async def test_llm_func_forwards_kwargs(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.read()
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "ok"}}]}
        )

    _patch_transport(monkeypatch, handler)

    fn = make_llm_func("http://test/v1", "sk-test", "gpt-test")
    await fn("hi", temperature=0.3, max_tokens=100, top_p=0.9, stream=True)

    body = json.loads(captured["body"])
    assert body["temperature"] == 0.3
    assert body["max_tokens"] == 100
    assert body["top_p"] == 0.9
    # explicit stream=True overrides the False default
    assert body["stream"] is True


@pytest.mark.asyncio
async def test_llm_func_drops_none_kwargs(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.read()
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "ok"}}]}
        )

    _patch_transport(monkeypatch, handler)

    fn = make_llm_func("http://test/v1", "sk-test", "gpt-test")
    await fn("hi", temperature=None, max_tokens=None)

    body = json.loads(captured["body"])
    assert "temperature" not in body
    assert "max_tokens" not in body


@pytest.mark.asyncio
async def test_llm_func_4xx_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    _patch_transport(monkeypatch, handler)

    fn = make_llm_func("http://test/v1", "sk-bad", "gpt-test")
    with pytest.raises(httpx.HTTPStatusError):
        await fn("hi")


@pytest.mark.asyncio
async def test_llm_func_5xx_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "unavailable"})

    _patch_transport(monkeypatch, handler)

    fn = make_llm_func("http://test/v1", "sk-test", "gpt-test")
    with pytest.raises(httpx.HTTPStatusError):
        await fn("hi")


# ---------------------------------------------------------------------------
# make_embedding_func
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_embedding_func_returns_vectors(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.read()
        captured["headers"] = dict(request.headers)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"embedding": [0.1, 0.2, 0.3]},
                    {"embedding": [0.4, 0.5, 0.6]},
                    {"embedding": [0.7, 0.8, 0.9]},
                ]
            },
        )

    _patch_transport(monkeypatch, handler)

    ef = make_embedding_func(
        "http://test/v1",
        "sk-test",
        "embed-test",
        embedding_dim=3,
        max_token_size=4096,
    )

    # EmbeddingFunc dataclass shape
    assert ef.embedding_dim == 3
    assert ef.max_token_size == 4096

    vectors = await ef.func(["a", "b", "c"])
    assert vectors == [
        [0.1, 0.2, 0.3],
        [0.4, 0.5, 0.6],
        [0.7, 0.8, 0.9],
    ]
    assert captured["url"] == "http://test/v1/embeddings"
    body = json.loads(captured["body"])
    assert body == {"model": "embed-test", "input": ["a", "b", "c"]}
    assert "Bearer sk-test" in captured["headers"]["authorization"]


@pytest.mark.asyncio
async def test_embedding_func_4xx_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    _patch_transport(monkeypatch, handler)

    ef = make_embedding_func("http://test/v1", "sk-bad", "embed-test")
    with pytest.raises(httpx.HTTPStatusError):
        await ef.func(["a"])


# ---------------------------------------------------------------------------
# make_vlm_func
# ---------------------------------------------------------------------------


def test_vlm_func_none_when_no_model() -> None:
    assert make_vlm_func("http://test/v1", "sk-test", None) is None
    assert make_vlm_func("http://test/v1", "sk-test", "") is None


@pytest.mark.asyncio
async def test_vlm_func_with_image_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.read()
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "saw image"}}]}
        )

    _patch_transport(monkeypatch, handler)

    vfn = make_vlm_func("http://test/v1", "sk-test", "vlm-test")
    assert vfn is not None
    out = await vfn("describe", image_data=b"\x89PNGfakebytes")
    assert out == "saw image"

    body = json.loads(captured["body"])
    msgs = body["messages"]
    assert len(msgs) == 1
    user = msgs[0]
    assert user["role"] == "user"
    parts = user["content"]
    assert parts[0] == {"type": "text", "text": "describe"}
    assert parts[1]["type"] == "image_url"
    url = parts[1]["image_url"]["url"]
    assert url.startswith("data:image/jpeg;base64,")
    # decode and round-trip check
    import base64

    decoded = base64.b64decode(url.split(",", 1)[1])
    assert decoded == b"\x89PNGfakebytes"


@pytest.mark.asyncio
async def test_vlm_func_with_image_url_string(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.read()
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "ok"}}]}
        )

    _patch_transport(monkeypatch, handler)

    vfn = make_vlm_func("http://test/v1", "sk-test", "vlm-test")
    assert vfn is not None
    await vfn("what is this?", image_data="https://example.com/foo.jpg")

    body = json.loads(captured["body"])
    parts = body["messages"][0]["content"]
    assert parts[1]["image_url"]["url"] == "https://example.com/foo.jpg"


@pytest.mark.asyncio
async def test_vlm_func_with_list_of_images(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.read()
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "ok"}}]}
        )

    _patch_transport(monkeypatch, handler)

    vfn = make_vlm_func("http://test/v1", "sk-test", "vlm-test")
    assert vfn is not None
    await vfn(
        "compare these",
        image_data=["https://example.com/a.jpg", b"raw-bytes"],
        system_prompt="be terse",
    )

    body = json.loads(captured["body"])
    msgs = body["messages"]
    assert msgs[0] == {"role": "system", "content": "be terse"}
    parts = msgs[1]["content"]
    assert parts[0]["type"] == "text"
    assert parts[1]["image_url"]["url"] == "https://example.com/a.jpg"
    assert parts[2]["image_url"]["url"].startswith("data:image/jpeg;base64,")


@pytest.mark.asyncio
async def test_vlm_func_unsupported_image_type_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "ok"}}]}
        )

    _patch_transport(monkeypatch, handler)

    vfn = make_vlm_func("http://test/v1", "sk-test", "vlm-test")
    assert vfn is not None
    with pytest.raises(TypeError):
        await vfn("hi", image_data=12345)  # type: ignore[arg-type]
