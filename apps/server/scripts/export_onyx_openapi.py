"""Export the /v1/onyx/* OpenAPI surface as a stable yaml file for ONYX team's SDK generation.

Usage:
    cd apps/server && uv run python scripts/export_onyx_openapi.py
        [--output ../../docs/onyx-integration/openapi.yaml]

The script:
  1. Calls create_app() to build the full FastAPI app with all routers mounted.
  2. Reads the OpenAPI dict via app.openapi().
  3. Filters paths down to those starting with /v1/onyx/.
  4. Strips Authorization-related Bearer example values so secrets never
     ship in the spec.
  5. Writes pretty-printed YAML to the output path.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Set required env vars BEFORE create_app import
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://export:export@localhost/export")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("INTERNAL_TOKEN", "x" * 96)
os.environ.setdefault("LLM_BASE_URL", "http://export-llm/v1")
os.environ.setdefault("LLM_API_KEY", "export-key")
os.environ.setdefault("LLM_MODEL", "export-model")
os.environ.setdefault("EMBEDDING_BASE_URL", "http://export-emb/v1")
os.environ.setdefault("EMBEDDING_API_KEY", "export-key")
os.environ.setdefault("EMBEDDING_MODEL", "export-embed")
os.environ.setdefault("JWT_SECRET_KEY", "z" * 64)


def _filter_onyx_paths(spec: dict) -> dict:
    """Keep only /v1/onyx/* paths; trim everything else."""
    paths = spec.get("paths", {})
    onyx_paths = {p: v for p, v in paths.items() if p.startswith("/v1/onyx/")}
    spec["paths"] = onyx_paths
    return spec


def _strip_secret_examples(spec: dict) -> dict:
    """Remove any example value of an Authorization-related schema."""
    components = spec.get("components", {})
    schemas = components.get("schemas", {})
    for schema_name, schema_def in schemas.items():
        if "Authorization" in schema_name or "Token" in schema_name:
            schema_def.pop("example", None)
    # Walk securitySchemes too — strip example tokens.
    sec = components.get("securitySchemes", {})
    for s in sec.values():
        s.pop("example", None)
    return spec


def _trim_unused_schemas(spec: dict) -> dict:
    """Remove schemas no longer referenced after path filtering.

    A schema is referenced if any reachable $ref points to it. We compute
    the closure transitively. This keeps the export concise.
    """
    import json
    used: set[str] = set()
    spec_str = json.dumps(spec.get("paths", {}))
    while True:
        before = len(used)
        for name in list(spec.get("components", {}).get("schemas", {}).keys()):
            if f"#/components/schemas/{name}" in spec_str:
                used.add(name)
        if not used:
            break
        # Add transitively-referenced schemas
        spec_str = json.dumps(spec.get("paths", {})) + json.dumps(
            {k: v for k, v in spec.get("components", {}).get("schemas", {}).items() if k in used}
        )
        if len(used) == before:
            break
    components = spec.get("components", {})
    schemas = components.get("schemas", {})
    components["schemas"] = {k: v for k, v in schemas.items() if k in used}
    return spec


def export_onyx_openapi(output_path: Path) -> dict:
    """Build, filter, and write the onyx OpenAPI to ``output_path``. Returns the dict."""
    # Late import after env is set
    from rag_service.api.app import create_app
    import yaml

    app = create_app()
    spec = app.openapi()
    spec = _filter_onyx_paths(spec)
    spec = _strip_secret_examples(spec)
    spec = _trim_unused_schemas(spec)

    # Tweak metadata for the public spec
    spec["info"] = {
        "title": "RAG-Anything ↔ ONYX integration API",
        "version": "v1",
        "description": (
            "Service-to-service surface used by the ONYX backend to drive "
            "RAG-Anything's KB / documents / query / KG features. See "
            "ONYX_INTEGRATION_PLAN.md for the full integration guide."
        ),
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(spec, f, sort_keys=False, default_flow_style=False, allow_unicode=True)
    return spec


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).parent.parent.parent.parent / "docs" / "onyx-integration" / "openapi.yaml",
    )
    args = parser.parse_args()
    spec = export_onyx_openapi(args.output)
    print(f"wrote {args.output} with {len(spec.get('paths', {}))} paths")
    return 0


if __name__ == "__main__":
    sys.exit(main())
