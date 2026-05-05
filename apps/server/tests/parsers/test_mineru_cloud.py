"""Tests for ``rag_service.parsers.mineru_cloud``.

Uses :class:`httpx.MockTransport` to intercept all outbound HTTP traffic;
no real network. We monkey-patch :class:`httpx.AsyncClient` inside the
module under test so the parser's internal ``async with httpx.AsyncClient``
block uses our transport.

The mocked endpoints reproduce the mineru.net API contract exactly as
``scripts/mineru_cloud_parse.py`` documents it:

- ``POST {base}/file-urls/batch`` → ``{"code": 0, "data": {"batch_id", "file_urls": [<signed_url>]}}``
- ``PUT <signed_url>`` → empty 200
- ``GET {base}/extract-results/batch/{batch_id}`` → ``{"code": 0, "data": {"extract_result": [{"file_name", "state", "full_zip_url", "err_msg"}]}}``
- ``GET <full_zip_url>`` → raw zip bytes containing ``*_content_list.json`` and ``images/``
"""

from __future__ import annotations

# Set required env vars before any rag_service import so the lazy
# ``settings`` singleton can be constructed without a real .env file.
import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://x@x/x")
os.environ.setdefault("REDIS_URL", "redis://x")
os.environ.setdefault("INTERNAL_TOKEN", "x" * 64)
os.environ.setdefault("LLM_BASE_URL", "x")
os.environ.setdefault("LLM_API_KEY", "x")
os.environ.setdefault("LLM_MODEL", "x")
os.environ.setdefault("EMBEDDING_BASE_URL", "x")
os.environ.setdefault("EMBEDDING_API_KEY", "x")
os.environ.setdefault("EMBEDDING_MODEL", "x")

import io  # noqa: E402
import json  # noqa: E402
import zipfile  # noqa: E402
from pathlib import Path  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402

from rag_service.parsers import mineru_cloud as mod  # noqa: E402
from rag_service.parsers.mineru_cloud import (  # noqa: E402
    DEFAULT_BASE_URL,
    MineruCloudError,
    MineruCloudParser,
    MineruCloudTimeoutError,
    get_default_parser,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


BASE_URL = "https://mineru.net/api/v4"
SIGNED_PUT_URL = "https://upload.example.com/signed/put?sig=abc"
ZIP_URL = "https://download.example.com/results/foo.zip"


def _patch_transport(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    """Route all ``httpx.AsyncClient`` traffic through ``MockTransport``."""
    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient

    def factory(**kw):
        return real(transport=transport, **kw)

    monkeypatch.setattr(mod.httpx, "AsyncClient", factory)


def _make_zip_bytes(
    content_list: list[dict],
    *,
    stem: str = "sample",
    include_images_dir: bool = True,
) -> bytes:
    """Build an in-memory ZIP that matches mineru.net's result layout:
    ``<stem>/<stem>_content_list.json`` plus an optional ``images/``
    folder."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(
            f"{stem}/{stem}_content_list.json",
            json.dumps(content_list),
        )
        if include_images_dir:
            # Empty image to ensure the dir exists in the archive
            z.writestr(f"{stem}/images/figure_1.png", b"\x89PNGfake")
    return buf.getvalue()


def _make_pdf(tmp_path: Path, name: str = "sample.pdf") -> Path:
    p = tmp_path / name
    p.write_bytes(b"%PDF-1.4\n%fake\n")
    return p


def _build_handler(
    *,
    file_name: str,
    poll_states: list[str],
    zip_bytes: bytes,
    upload_status: int = 200,
    poll_payload_override=None,
):
    """Construct a mock handler whose state machine walks ``poll_states``.

    Each call to ``GET /extract-results/batch/...`` consumes the next
    state from ``poll_states``; once exhausted, the last state repeats.
    The state ``"done"`` switches the response to include ``full_zip_url``.
    """
    state_iter = iter(poll_states)
    last_state = {"value": poll_states[0] if poll_states else "pending"}
    captured = {
        "upload_request_body": None,
        "upload_request_headers": None,
        "put_count": 0,
        "poll_count": 0,
        "download_count": 0,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        # --- Step 1: request signed upload URL --------------------------
        if request.method == "POST" and url == f"{BASE_URL}/file-urls/batch":
            captured["upload_request_body"] = json.loads(request.read())
            captured["upload_request_headers"] = dict(request.headers)
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "batch_id": "batch-123",
                        "file_urls": [SIGNED_PUT_URL],
                    },
                },
            )
        # --- Step 2: PUT to signed URL ----------------------------------
        if request.method == "PUT" and url == SIGNED_PUT_URL:
            captured["put_count"] += 1
            return httpx.Response(upload_status)
        # --- Step 3: poll -----------------------------------------------
        if (
            request.method == "GET"
            and url == f"{BASE_URL}/extract-results/batch/batch-123"
        ):
            captured["poll_count"] += 1
            if poll_payload_override is not None:
                return poll_payload_override(request)
            try:
                state = next(state_iter)
                last_state["value"] = state
            except StopIteration:
                state = last_state["value"]
            entry: dict = {"file_name": file_name, "state": state}
            if state == "done":
                entry["full_zip_url"] = ZIP_URL
            elif state == "failed":
                entry["err_msg"] = "synthetic failure"
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {"extract_result": [entry]},
                },
            )
        # --- Step 4: download zip ---------------------------------------
        if request.method == "GET" and url == ZIP_URL:
            captured["download_count"] += 1
            return httpx.Response(200, content=zip_bytes)

        return httpx.Response(404, json={"error": f"unexpected {request.method} {url}"})

    return handler, captured


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``asyncio.sleep`` a no-op inside the parser so polling is
    instant. ``time()`` keeps advancing via the real loop, but actual wall
    clock waits are skipped."""

    async def _instant_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(mod.asyncio, "sleep", _instant_sleep)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parse_happy_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Upload + one running poll + one done poll + download → content list."""
    pdf = _make_pdf(tmp_path)
    out = tmp_path / "out"

    raw_content = [
        {"type": "text", "text": "hello world", "page_idx": 0},
        {
            "type": "image",
            "img_path": "images/figure_1.png",
            "page_idx": 0,
        },
        {
            "type": "table",
            "text": "<table>...</table>",
            "page_idx": 1,
        },
    ]
    zip_bytes = _make_zip_bytes(raw_content, stem="sample")

    handler, captured = _build_handler(
        file_name=pdf.name,
        poll_states=["pending", "running", "done"],
        zip_bytes=zip_bytes,
    )
    _patch_transport(monkeypatch, handler)

    parser = MineruCloudParser(
        api_key="sk-mineru-test",
        base_url=BASE_URL,
        poll_interval=0.0,
        poll_timeout=10.0,
    )
    # Test the async impl directly: the public ``parse_document`` is a
    # sync wrapper that runs this in a worker thread (so RAGAnything's
    # sync Parser interface is honoured). The wire/parser logic lives
    # entirely in ``_parse_document_async``.
    result = await parser._parse_document_async(pdf, out)

    # Returned shape — same length, ordering preserved
    assert isinstance(result, list)
    assert len(result) == 3
    assert result[0]["type"] == "text"
    assert result[0]["text"] == "hello world"
    assert result[1]["type"] == "image"
    # img_path was rewritten to an absolute path under output_dir
    img_path = Path(result[1]["img_path"])
    assert img_path.is_absolute()
    assert img_path.name == "figure_1.png"
    # The zip extracted into output_dir/sample/, so the rewritten path
    # should live under output_dir.
    assert out.resolve() in img_path.parents

    # Sanity: traffic actually went through our mocks
    assert captured["put_count"] == 1
    assert captured["poll_count"] >= 3  # pending, running, done
    assert captured["download_count"] == 1

    # Auth header carried through to the JSON endpoints
    assert (
        captured["upload_request_headers"]["authorization"]
        == "Bearer sk-mineru-test"
    )
    body = captured["upload_request_body"]
    assert body["files"][0]["name"] == pdf.name
    assert body["files"][0]["data_id"] == pdf.stem
    assert body["model_version"] == "vlm"  # default
    assert body["language"] == "ch"  # default


@pytest.mark.asyncio
async def test_parse_returns_correct_shape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Returned items conform to the MinerU content_list schema (dicts
    with ``type`` plus type-appropriate fields)."""
    pdf = _make_pdf(tmp_path)
    out = tmp_path / "out"

    raw_content = [
        {"type": "text", "text": "para one", "page_idx": 0},
        {"type": "text", "text": "para two", "page_idx": 1},
        {
            "type": "image",
            "img_path": "images/figure_1.png",
            "img_caption": ["fig 1"],
            "page_idx": 2,
        },
    ]
    zip_bytes = _make_zip_bytes(raw_content)

    handler, _ = _build_handler(
        file_name=pdf.name,
        poll_states=["done"],
        zip_bytes=zip_bytes,
    )
    _patch_transport(monkeypatch, handler)

    parser = MineruCloudParser(
        api_key="sk", base_url=BASE_URL, poll_interval=0.0
    )
    # Test the async impl directly: the public ``parse_document`` is a
    # sync wrapper that runs this in a worker thread (so RAGAnything's
    # sync Parser interface is honoured). The wire/parser logic lives
    # entirely in ``_parse_document_async``.
    result = await parser._parse_document_async(pdf, out)

    assert all(isinstance(item, dict) for item in result)
    for item in result:
        assert "type" in item
        assert "page_idx" in item
        if item["type"] == "text":
            assert isinstance(item["text"], str)
        elif item["type"] == "image":
            assert "img_path" in item
            assert Path(item["img_path"]).is_absolute()


@pytest.mark.asyncio
async def test_parse_timeout_during_poll(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Polling that never reaches ``done`` should raise
    :class:`MineruCloudTimeoutError`. We force ``time.monotonic`` to jump
    past the deadline after the first call so the loop bails on the
    second poll iteration."""
    pdf = _make_pdf(tmp_path)
    out = tmp_path / "out"

    handler, _ = _build_handler(
        file_name=pdf.name,
        poll_states=["pending"],  # never reaches done
        zip_bytes=b"unused",
    )
    _patch_transport(monkeypatch, handler)

    counter = {"n": 0}

    def fake_now() -> float:
        counter["n"] += 1
        # First call sets deadline = 0 + poll_timeout = 1.0; subsequent
        # calls are well past it, so the deadline check trips.
        if counter["n"] == 1:
            return 0.0
        return 999.0

    # The parser uses a module-level ``_now`` indirection over
    # ``time.monotonic`` precisely so we can stub it here without
    # destabilising httpx / asyncio internals (which also call
    # ``time.monotonic``).
    monkeypatch.setattr(mod, "_now", fake_now)

    parser = MineruCloudParser(
        api_key="sk",
        base_url=BASE_URL,
        poll_interval=0.0,
        poll_timeout=1.0,
    )
    with pytest.raises(MineruCloudTimeoutError):
        await parser.parse_document(pdf, out)


@pytest.mark.asyncio
async def test_parse_upload_4xx_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A 401 from ``file-urls/batch`` surfaces as
    :class:`httpx.HTTPStatusError`."""
    pdf = _make_pdf(tmp_path)
    out = tmp_path / "out"

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == f"{BASE_URL}/file-urls/batch":
            return httpx.Response(401, json={"error": "unauthorized"})
        return httpx.Response(500, json={"error": "should not reach"})

    _patch_transport(monkeypatch, handler)

    parser = MineruCloudParser(
        api_key="sk-bad", base_url=BASE_URL, poll_interval=0.0
    )
    with pytest.raises(httpx.HTTPStatusError):
        await parser.parse_document(pdf, out)


@pytest.mark.asyncio
async def test_parse_failed_state_raises_clean_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When the cloud reports ``state == "failed"``, raise a clear
    :class:`MineruCloudError` carrying the err_msg."""
    pdf = _make_pdf(tmp_path)
    out = tmp_path / "out"

    handler, _ = _build_handler(
        file_name=pdf.name,
        poll_states=["running", "failed"],
        zip_bytes=b"unused",
    )
    _patch_transport(monkeypatch, handler)

    parser = MineruCloudParser(
        api_key="sk", base_url=BASE_URL, poll_interval=0.0
    )
    with pytest.raises(MineruCloudError) as ei:
        await parser.parse_document(pdf, out)
    assert "synthetic failure" in str(ei.value)


@pytest.mark.asyncio
async def test_parse_business_error_code_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A 200 response with non-zero ``code`` from the upload endpoint is
    a business-layer error and should raise :class:`MineruCloudError`."""
    pdf = _make_pdf(tmp_path)
    out = tmp_path / "out"

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == f"{BASE_URL}/file-urls/batch":
            return httpx.Response(
                200,
                json={"code": -1, "msg": "quota exceeded", "data": None},
            )
        return httpx.Response(500)

    _patch_transport(monkeypatch, handler)

    parser = MineruCloudParser(
        api_key="sk", base_url=BASE_URL, poll_interval=0.0
    )
    with pytest.raises(MineruCloudError):
        await parser.parse_document(pdf, out)


# ---------------------------------------------------------------------------
# get_default_parser
# ---------------------------------------------------------------------------


def test_get_default_parser_uses_api_key_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MINERU_CLOUD_API_KEY", "key-from-env")
    monkeypatch.delenv("MINERU_CLOUD_TOKEN", raising=False)
    monkeypatch.delenv("MINERU_CLOUD_BASE_URL", raising=False)

    p = get_default_parser()
    assert isinstance(p, MineruCloudParser)
    assert p.api_key == "key-from-env"
    assert p.base_url == DEFAULT_BASE_URL


def test_get_default_parser_falls_back_to_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MINERU_CLOUD_API_KEY", raising=False)
    monkeypatch.setenv("MINERU_CLOUD_TOKEN", "legacy-token")

    p = get_default_parser()
    assert p.api_key == "legacy-token"


def test_get_default_parser_raises_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MINERU_CLOUD_API_KEY", raising=False)
    monkeypatch.delenv("MINERU_CLOUD_TOKEN", raising=False)
    with pytest.raises(RuntimeError):
        get_default_parser()
