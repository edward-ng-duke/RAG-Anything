"""
MinerU.net cloud API bridge.

Uploads a local document to mineru.net for parsing and returns a content_list
in MinerU's standard schema (the same shape RAGAnything.insert_content_list
expects). Image references in content_list are rewritten to absolute paths
pointing at the extracted image directory.

Usage (CLI):
    uv run python scripts/mineru_cloud_parse.py samples/sample.pdf

Usage (import):
    from scripts.mineru_cloud_parse import parse_via_cloud
    content_list, image_dir = parse_via_cloud("samples/sample.pdf")
"""

from __future__ import annotations

import json
import os
import sys
import time
import zipfile
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv(dotenv_path=".env", override=False)


def _cfg(name: str, default: str | None = None) -> str:
    val = os.getenv(name, default)
    if val is None or val == "" or val.startswith("PLACEHOLDER_"):
        raise RuntimeError(
            f"Missing or placeholder value for {name}. Edit .env and set a real value."
        )
    return val


def _request_upload_urls(
    file_path: Path,
    token: str,
    base_url: str,
) -> tuple[str, str]:
    """POST /file-urls/batch — get a batch_id and one signed PUT URL."""
    body = {
        "files": [
            {
                "name": file_path.name,
                "data_id": file_path.stem,
                "is_ocr": False,
            }
        ],
        "model_version": os.getenv("MINERU_MODEL_VERSION", "vlm"),
        "enable_formula": os.getenv("MINERU_ENABLE_FORMULA", "true").lower() == "true",
        "enable_table": os.getenv("MINERU_ENABLE_TABLE", "true").lower() == "true",
        "language": os.getenv("MINERU_LANGUAGE", "ch"),
    }
    resp = requests.post(
        f"{base_url}/file-urls/batch",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("code") != 0:
        raise RuntimeError(f"file-urls/batch failed: {payload}")
    data = payload["data"]
    return data["batch_id"], data["file_urls"][0]


def _put_file(file_path: Path, upload_url: str) -> None:
    with file_path.open("rb") as f:
        resp = requests.put(upload_url, data=f, timeout=300)
    resp.raise_for_status()


def _poll_until_done(
    batch_id: str,
    token: str,
    base_url: str,
    file_name: str,
    interval: int,
    timeout: int,
) -> str:
    """Poll until the file's state is `done`. Returns the full_zip_url.

    Tolerates transient network errors on individual polls — a single timeout
    shouldn't abort the whole pipeline given parsing can take minutes.
    """
    deadline = time.monotonic() + timeout
    last_state = ""
    while time.monotonic() < deadline:
        try:
            resp = requests.get(
                f"{base_url}/extract-results/batch/{batch_id}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
            )
            resp.raise_for_status()
            payload = resp.json()
        except (requests.ConnectionError, requests.Timeout) as e:
            print(f"[mineru.net] poll transient error ({e!r}), retrying...", flush=True)
            time.sleep(interval)
            continue

        if payload.get("code") != 0:
            raise RuntimeError(f"extract-results poll failed: {payload}")
        for entry in payload["data"]["extract_result"]:
            if entry["file_name"] != file_name:
                continue
            state = entry["state"]
            if state != last_state:
                print(f"[mineru.net] state={state}", flush=True)
                last_state = state
            if state == "done":
                return entry["full_zip_url"]
            if state == "failed":
                raise RuntimeError(
                    f"mineru.net parsing failed: {entry.get('err_msg', '<no message>')}"
                )
        time.sleep(interval)
    raise TimeoutError(f"Polling exceeded {timeout}s for batch {batch_id}")


def _download_and_extract(zip_url: str, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    zip_path = dest_dir / "result.zip"
    with requests.get(zip_url, stream=True, timeout=300) as r:
        r.raise_for_status()
        with zip_path.open("wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 16):
                f.write(chunk)
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(dest_dir)
    zip_path.unlink()
    return dest_dir


def _load_content_list(extract_dir: Path) -> tuple[list[dict[str, Any]], Path]:
    """Locate *_content_list.json and the images dir, rewrite img_path to absolute."""
    candidates = list(extract_dir.glob("**/*content_list.json"))
    if not candidates:
        raise FileNotFoundError(f"No *_content_list.json found under {extract_dir}")
    content_list_path = candidates[0]
    content_list = json.loads(content_list_path.read_text(encoding="utf-8"))

    images_dir = content_list_path.parent / "images"
    if not images_dir.is_dir():
        # fall back to anywhere under extract_dir
        found = list(extract_dir.glob("**/images"))
        images_dir = found[0] if found else content_list_path.parent

    base = content_list_path.parent
    for item in content_list:
        if not isinstance(item, dict):
            continue
        rel = item.get("img_path")
        if isinstance(rel, str) and rel:
            abs_path = (base / rel).resolve()
            item["img_path"] = str(abs_path)

    return content_list, images_dir.resolve()


def parse_via_cloud(
    pdf_path: str | Path,
    work_dir: str | Path = "./output/mineru_cloud",
) -> tuple[list[dict[str, Any]], Path]:
    """End-to-end: upload → poll → download → return (content_list, image_dir).

    If a previous successful parse for this stem already exists under work_dir,
    reuse it instead of re-uploading. Delete the stem's directory to force re-parse.
    """
    pdf_path = Path(pdf_path).resolve()
    if not pdf_path.exists():
        raise FileNotFoundError(pdf_path)

    cache_dir = Path(work_dir).resolve() / pdf_path.stem
    if list(cache_dir.glob("**/*content_list.json")):
        print(f"[mineru.net] using cached parse at {cache_dir}", flush=True)
        return _load_content_list(cache_dir)

    token = _cfg("MINERU_CLOUD_TOKEN")
    base_url = _cfg("MINERU_CLOUD_BASE_URL", "https://mineru.net/api/v4")
    interval = int(os.getenv("MINERU_POLL_INTERVAL_SEC", "5"))
    timeout = int(os.getenv("MINERU_POLL_TIMEOUT_SEC", "900"))

    print(f"[mineru.net] requesting upload URL for {pdf_path.name}", flush=True)
    batch_id, upload_url = _request_upload_urls(pdf_path, token, base_url)
    print(f"[mineru.net] batch_id={batch_id}", flush=True)

    print("[mineru.net] uploading file...", flush=True)
    _put_file(pdf_path, upload_url)

    print("[mineru.net] polling for completion...", flush=True)
    zip_url = _poll_until_done(
        batch_id, token, base_url, pdf_path.name, interval, timeout
    )

    extract_dir = Path(work_dir).resolve() / pdf_path.stem
    print(f"[mineru.net] downloading result ZIP to {extract_dir}", flush=True)
    _download_and_extract(zip_url, extract_dir)

    content_list, image_dir = _load_content_list(extract_dir)
    print(
        f"[mineru.net] parsed: {len(content_list)} blocks, images at {image_dir}",
        flush=True,
    )
    return content_list, image_dir


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: mineru_cloud_parse.py <pdf_path>", file=sys.stderr)
        sys.exit(1)
    cl, img = parse_via_cloud(sys.argv[1])
    types: dict[str, int] = {}
    for it in cl:
        if isinstance(it, dict):
            t = it.get("type", "unknown")
            types[t] = types.get(t, 0) + 1
    print(json.dumps({"blocks": len(cl), "types": types, "image_dir": str(img)}, indent=2))
