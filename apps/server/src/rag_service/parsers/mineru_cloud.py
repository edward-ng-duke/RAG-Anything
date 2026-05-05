"""MinerU.net cloud API parser.

An async wrapper around the mineru.net public extraction API. The flow
mirrors :mod:`scripts.mineru_cloud_parse` (the ground-truth reference)
beat-for-beat:

1. ``POST {base}/file-urls/batch`` — request a ``batch_id`` and one signed
   PUT URL per file, passing parsing options (model_version, language,
   formula/table flags).
2. ``PUT <signed_url>`` — upload the file bytes directly to object storage.
3. ``GET {base}/extract-results/batch/{batch_id}`` — poll until the entry
   for our file reports ``state == "done"`` (or ``"failed"``), then read
   ``full_zip_url`` from that entry.
4. ``GET <full_zip_url>`` — download the result archive, unzip into
   ``output_dir``, locate ``*_content_list.json`` and ``images/``, and
   rewrite ``img_path`` fields to absolute paths.

The returned ``content_list`` matches MinerU's standard schema (the same
shape :class:`raganything.parser.MineruParser` returns), so downstream
RAGAnything ingestion code can consume it without modification.

Interface compatibility
-----------------------
RAGAnything's upstream :class:`raganything.parser.Parser` is **sync**
(``parse_document`` returns a list, ``check_installation`` returns a
bool). Subclassing it lets ``RAGAnything.doc_parser = MineruCloudParser(...)``
be a drop-in replacement for the local ``MineruParser``. The async
implementation lives in :meth:`_parse_document_async`; the public sync
:meth:`parse_document` runs it in a dedicated thread so we never collide
with an outer running event loop.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import threading
import time
import zipfile
from pathlib import Path
from typing import Any

import httpx
from raganything.parser import Parser

DEFAULT_BASE_URL = "https://mineru.net/api/v4"


def _now() -> float:
    """Indirection over ``time.monotonic`` so tests can patch the wall
    clock used for poll-deadline tracking without affecting httpx /
    asyncio internals."""
    return time.monotonic()


class MineruCloudError(RuntimeError):
    """Base error raised by :class:`MineruCloudParser`."""


class MineruCloudTimeoutError(MineruCloudError):
    """Raised when polling for a finished job exceeds ``poll_timeout``."""


class MineruCloudParseFailed(MineruCloudError):
    """Raised when the cloud reports ``state == "failed"`` for our file."""


class MineruCloudParser(Parser):
    """Cloud parser that delegates document parsing to mineru.net.

    Subclasses :class:`raganything.parser.Parser` so it can be assigned to
    :attr:`raganything.RAGAnything.doc_parser` directly. The upstream
    interface is sync (``parse_document`` returns a list, no ``await``);
    we wrap the async implementation in a worker thread to avoid colliding
    with any outer running event loop (e.g. arq).

    Parameters
    ----------
    api_key:
        Bearer token for the mineru.net API. Sent as
        ``Authorization: Bearer <api_key>`` on every authenticated call.
    base_url:
        API root, defaults to :data:`DEFAULT_BASE_URL`. Trailing slashes
        are stripped.
    poll_interval:
        Seconds between successive ``extract-results`` polls. Defaults
        to 5s, matching the reference script.
    poll_timeout:
        Total seconds to wait for ``state == "done"`` before raising
        :class:`MineruCloudTimeoutError`. Defaults to 900s (15 min).
    request_timeout:
        Per-request timeout for the JSON control-plane calls
        (``file-urls/batch`` and ``extract-results``). Defaults to 30s.
    upload_timeout:
        Per-request timeout for the PUT upload and the ZIP download.
        Defaults to 300s — these can be slow for large PDFs.
    model_version, language, enable_formula, enable_table:
        Parsing options forwarded to ``file-urls/batch``. Defaults match
        the reference script's environment-driven defaults.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        *,
        poll_interval: float = 5.0,
        poll_timeout: float = 900.0,
        request_timeout: float = 30.0,
        upload_timeout: float = 300.0,
        model_version: str = "vlm",
        language: str = "ch",
        enable_formula: bool = True,
        enable_table: bool = True,
    ) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        self.api_key = api_key
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.poll_interval = poll_interval
        self.poll_timeout = poll_timeout
        self.request_timeout = request_timeout
        self.upload_timeout = upload_timeout
        self.model_version = model_version
        self.language = language
        self.enable_formula = enable_formula
        self.enable_table = enable_table

    # ------------------------------------------------------------------
    # Parser interface (sync — required by raganything.parser.Parser)
    # ------------------------------------------------------------------

    def check_installation(self) -> bool:
        """Always available — the cloud API is the only "install" needed,
        and the constructor already rejected an empty token."""
        return True

    # The upstream RAGAnything processor dispatches PDFs to ``parse_pdf``
    # and images to ``parse_image`` directly (not via ``parse_document``),
    # so we surface both as thin wrappers over the cloud entrypoint.

    def parse_pdf(
        self,
        pdf_path: str | Path,
        output_dir: str | None = None,
        method: str = "auto",
        lang: str | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        return self.parse_document(
            pdf_path,
            method=method,
            output_dir=output_dir,
            lang=lang,
            **kwargs,
        )

    def parse_image(
        self,
        image_path: str | Path,
        output_dir: str | None = None,
        lang: str | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        return self.parse_document(
            image_path, output_dir=output_dir, lang=lang, **kwargs
        )

    def parse_office_doc(
        self,
        doc_path: str | Path,
        output_dir: str | None = None,
        lang: str | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        return self.parse_document(
            doc_path, output_dir=output_dir, lang=lang, **kwargs
        )

    def parse_document(
        self,
        file_path: str | Path,
        method: str = "auto",
        output_dir: str | None = None,
        lang: str | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Sync entrypoint expected by upstream RAGAnything ingestion.

        Runs :meth:`_parse_document_async` in a dedicated worker thread so
        we never call ``asyncio.run`` from inside an already-running loop
        (which arq workers always have).
        """
        result: dict[str, Any] = {}

        def _worker() -> None:
            try:
                result["value"] = asyncio.run(
                    self._parse_document_async(
                        file_path,
                        output_dir or "./output",
                        lang=lang,
                        **kwargs,
                    )
                )
            except BaseException as exc:  # noqa: BLE001
                result["error"] = exc

        t = threading.Thread(
            target=_worker, name="mineru-cloud-parse", daemon=True
        )
        t.start()
        t.join()
        if "error" in result:
            raise result["error"]
        return result["value"]  # type: ignore[no-any-return]

    # ------------------------------------------------------------------
    # Async implementation
    # ------------------------------------------------------------------

    async def _parse_document_async(
        self,
        file_path: str | Path,
        output_dir: str | Path,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Parse ``file_path`` via mineru.net, write results into
        ``output_dir``, and return the MinerU-style content list.

        ``kwargs`` is accepted for compatibility with the
        :class:`raganything.parser.Parser` interface (``method``, ``lang``,
        etc.), but only the parser-construction options apply for the
        cloud flow. ``lang`` overrides ``self.language`` for this call.
        """
        file_path = Path(file_path).resolve()
        if not file_path.exists():
            raise FileNotFoundError(file_path)
        output_dir = Path(output_dir).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)

        lang = kwargs.get("lang") or self.language

        async with httpx.AsyncClient() as client:
            batch_id, upload_url = await self._request_upload_url(
                client, file_path, lang=lang
            )
            await self._put_file(client, file_path, upload_url)
            zip_url = await self._poll_until_done(
                client, batch_id, file_path.name
            )
            zip_bytes = await self._download_zip(client, zip_url)

        self._extract_zip(zip_bytes, output_dir)
        return self._load_content_list(output_dir)

    # ------------------------------------------------------------------
    # Step 1 — request signed upload URL
    # ------------------------------------------------------------------

    async def _request_upload_url(
        self,
        client: httpx.AsyncClient,
        file_path: Path,
        *,
        lang: str,
    ) -> tuple[str, str]:
        body = {
            "files": [
                {
                    "name": file_path.name,
                    "data_id": file_path.stem,
                    "is_ocr": False,
                }
            ],
            "model_version": self.model_version,
            "enable_formula": self.enable_formula,
            "enable_table": self.enable_table,
            "language": lang,
        }
        resp = await client.post(
            f"{self.base_url}/file-urls/batch",
            json=body,
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=self.request_timeout,
        )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("code") != 0:
            raise MineruCloudError(f"file-urls/batch failed: {payload}")
        data = payload["data"]
        return data["batch_id"], data["file_urls"][0]

    # ------------------------------------------------------------------
    # Step 2 — PUT file to signed URL
    # ------------------------------------------------------------------

    async def _put_file(
        self,
        client: httpx.AsyncClient,
        file_path: Path,
        upload_url: str,
    ) -> None:
        # Read bytes once; mineru.net's signed URLs expect a single PUT
        # with the full body, no chunked-streaming required.
        data = await asyncio.to_thread(file_path.read_bytes)
        resp = await client.put(
            upload_url, content=data, timeout=self.upload_timeout
        )
        resp.raise_for_status()

    # ------------------------------------------------------------------
    # Step 3 — poll
    # ------------------------------------------------------------------

    async def _poll_until_done(
        self,
        client: httpx.AsyncClient,
        batch_id: str,
        file_name: str,
    ) -> str:
        deadline = _now() + self.poll_timeout
        while True:
            try:
                resp = await client.get(
                    f"{self.base_url}/extract-results/batch/{batch_id}",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    timeout=self.request_timeout,
                )
                resp.raise_for_status()
                payload = resp.json()
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout):
                # Transient errors are tolerated — parsing can take minutes
                # and a single hiccup shouldn't kill the whole pipeline.
                if _now() >= deadline:
                    raise MineruCloudTimeoutError(
                        f"Polling exceeded {self.poll_timeout}s for batch {batch_id}"
                    )
                await asyncio.sleep(self.poll_interval)
                continue

            if payload.get("code") != 0:
                raise MineruCloudError(f"extract-results poll failed: {payload}")

            for entry in payload["data"]["extract_result"]:
                if entry["file_name"] != file_name:
                    continue
                state = entry["state"]
                if state == "done":
                    return entry["full_zip_url"]
                if state == "failed":
                    raise MineruCloudParseFailed(
                        f"mineru.net parsing failed: "
                        f"{entry.get('err_msg', '<no message>')}"
                    )
                # else: pending / running / etc. — keep polling

            if _now() >= deadline:
                raise MineruCloudTimeoutError(
                    f"Polling exceeded {self.poll_timeout}s for batch {batch_id}"
                )
            await asyncio.sleep(self.poll_interval)

    # ------------------------------------------------------------------
    # Step 4 — download + extract + load
    # ------------------------------------------------------------------

    async def _download_zip(
        self, client: httpx.AsyncClient, zip_url: str
    ) -> bytes:
        resp = await client.get(zip_url, timeout=self.upload_timeout)
        resp.raise_for_status()
        return resp.content

    @staticmethod
    def _extract_zip(zip_bytes: bytes, dest_dir: Path) -> None:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
            z.extractall(dest_dir)

    @staticmethod
    def _load_content_list(extract_dir: Path) -> list[dict[str, Any]]:
        """Locate ``*_content_list.json`` under ``extract_dir`` and rewrite
        any relative ``img_path`` values to absolute paths.

        Mirrors :func:`scripts.mineru_cloud_parse._load_content_list` but
        only returns the content list (the image dir is implied by the
        rewritten absolute paths).
        """
        candidates = list(extract_dir.glob("**/*content_list.json"))
        if not candidates:
            raise FileNotFoundError(
                f"No *_content_list.json found under {extract_dir}"
            )
        content_list_path = candidates[0]
        content_list = json.loads(
            content_list_path.read_text(encoding="utf-8")
        )

        base = content_list_path.parent
        for item in content_list:
            if not isinstance(item, dict):
                continue
            rel = item.get("img_path")
            if isinstance(rel, str) and rel:
                item["img_path"] = str((base / rel).resolve())

        return content_list


def get_default_parser() -> MineruCloudParser:
    """Read ``MINERU_CLOUD_API_KEY`` from the environment and return a
    parser configured with sensible defaults.

    Also honours ``MINERU_CLOUD_BASE_URL`` (defaulting to
    :data:`DEFAULT_BASE_URL`) for parity with the reference script's env
    layout. Raises :class:`RuntimeError` if the API key is unset or
    blank.

    Note: the reference script uses ``MINERU_CLOUD_TOKEN`` as its env var
    name; this module standardises on ``MINERU_CLOUD_API_KEY`` (matching
    the task spec) and falls back to ``MINERU_CLOUD_TOKEN`` so existing
    ``.env`` files keep working.
    """
    api_key = (
        os.getenv("MINERU_CLOUD_API_KEY")
        or os.getenv("MINERU_CLOUD_TOKEN")
        or ""
    )
    if not api_key:
        raise RuntimeError(
            "MINERU_CLOUD_API_KEY is not set; cannot construct "
            "MineruCloudParser default instance"
        )
    return MineruCloudParser(
        api_key=api_key,
        base_url=os.getenv("MINERU_CLOUD_BASE_URL"),
    )
