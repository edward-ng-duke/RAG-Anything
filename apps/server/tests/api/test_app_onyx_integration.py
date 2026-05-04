"""Tests for ``rag_service.api.app.create_app`` — onyx router mount + middlewares.

Task 3.3 wires the five ``/v1/onyx/*`` routers into the real FastAPI app
and adds two new pieces of cross-cutting plumbing:

* ``OnyxIpAllowlistMiddleware`` short-circuits 403 for ``/v1/onyx/*``
  when ``settings.onyx_backend_allowed_cidrs`` is set and the caller IP
  (XFF first hop, fallback to ``request.client.host``) doesn't match any
  allowed CIDR. It runs *before* the rate-limit middleware so blocked
  IPs don't poison rate-limit buckets.
* The existing ``request_id_mw`` is augmented to bind the
  ``onyx_user_id`` ``contextvar`` from the ``X-Onyx-User-Id`` header for
  ``/v1/onyx/*`` requests so structlog records emitted while serving an
  onyx request carry the caller id.

Tests stand up the real ``create_app()`` (lifespan disabled by default
when no test client triggers startup) and exercise the routes via
``httpx.ASGITransport`` so the full middleware chain runs.
"""

from __future__ import annotations

# ``tests/conftest.py`` already populates the env vars that ``rag_service``
# requires at import-time. We override DATA_DIR locally to keep this
# test's data dir from colliding with sibling tests.
import os  # noqa: E402

os.environ.setdefault("DATA_DIR", "/tmp/rag_app_onyx_integration_test")

import pytest  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

from rag_service.api.app import create_app  # noqa: E402
from rag_service.observability.logging import onyx_user_id_var  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _client(app, **kwargs):
    """Wrap an ASGI app in an httpx ``AsyncClient`` for tests.

    Centralised so every test uses the same base URL + transport — keeps
    the test bodies focused on the request/response under inspection.
    """
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://t",
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Router-mount checks — 401 (route reached the auth dep) ≠ 404 (not mounted)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_onyx_routes_mounted() -> None:
    """``GET /v1/onyx/kb`` returns 401 (auth dep reached), not 404."""
    app = create_app()
    async with _client(app) as ac:
        r = await ac.get("/v1/onyx/kb")
    assert r.status_code == 401, r.text


@pytest.mark.asyncio
async def test_onyx_query_route_mounted() -> None:
    """``POST /v1/onyx/query`` returns 401 with no auth, not 404."""
    app = create_app()
    async with _client(app) as ac:
        r = await ac.post("/v1/onyx/query", json={"query": "x"})
    assert r.status_code == 401, r.text


@pytest.mark.asyncio
async def test_onyx_kg_route_mounted() -> None:
    """``GET /v1/onyx/kg/stats`` returns 401 with no auth, not 404."""
    app = create_app()
    async with _client(app) as ac:
        r = await ac.get("/v1/onyx/kg/stats")
    assert r.status_code == 401, r.text


@pytest.mark.asyncio
async def test_onyx_documents_route_mounted() -> None:
    """``GET /v1/onyx/documents`` returns 401 with no auth, not 404."""
    app = create_app()
    async with _client(app) as ac:
        r = await ac.get("/v1/onyx/documents")
    assert r.status_code == 401, r.text


@pytest.mark.asyncio
async def test_onyx_jobs_route_mounted() -> None:
    """``GET /v1/onyx/jobs/anything`` returns 401 with no auth, not 404."""
    app = create_app()
    async with _client(app) as ac:
        r = await ac.get("/v1/onyx/jobs/abc-123")
    # Unauthenticated → 401 from auth dep (404 would mean the router isn't
    # mounted at all).
    assert r.status_code == 401, r.text


@pytest.mark.asyncio
async def test_existing_alpha_routes_still_work() -> None:
    """Mounting onyx routers must not regress the alpha API surface.

    ``GET /v1/documents`` without auth must still hit the alpha auth
    dep and produce 401 (the existing tenant test suite covers the
    happy path).
    """
    app = create_app()
    async with _client(app) as ac:
        r = await ac.get("/v1/documents")
    assert r.status_code == 401, r.text


# ---------------------------------------------------------------------------
# onyx_user_id contextvar binding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_onyx_user_id_contextvar_set_during_request() -> None:
    """``X-Onyx-User-Id`` must be bound to ``onyx_user_id_var`` for the request.

    We mount a probe endpoint under ``/v1/onyx/__probe__`` *after* the
    middleware chain is installed, then send a request with the header
    and assert the handler observed the contextvar value.
    """
    app = create_app()
    captured: dict[str, str | None] = {}

    @app.get("/v1/onyx/__probe__")
    async def _probe() -> dict[str, str | None]:
        captured["onyx_user_id"] = onyx_user_id_var.get()
        return {"onyx_user_id": captured["onyx_user_id"]}

    async with _client(app) as ac:
        r = await ac.get(
            "/v1/onyx/__probe__",
            headers={"X-Onyx-User-Id": "u_alice"},
        )
    assert r.status_code == 200, r.text
    assert captured["onyx_user_id"] == "u_alice"
    # The contextvar resets after the response is sent → no bleed-over.
    assert onyx_user_id_var.get() is None


@pytest.mark.asyncio
async def test_onyx_user_id_not_bound_for_alpha_paths() -> None:
    """The contextvar binding only fires for ``/v1/onyx/*`` paths.

    A probe under ``/v1/__alpha_probe__`` with the header set must see
    ``None`` so the alpha API surface isn't accidentally tagged with an
    onyx caller id.
    """
    app = create_app()
    captured: dict[str, str | None] = {}

    @app.get("/v1/__alpha_probe__")
    async def _probe() -> dict[str, str | None]:
        captured["onyx_user_id"] = onyx_user_id_var.get()
        return {"onyx_user_id": captured["onyx_user_id"]}

    async with _client(app) as ac:
        r = await ac.get(
            "/v1/__alpha_probe__",
            headers={"X-Onyx-User-Id": "u_alice"},
        )
    assert r.status_code == 200, r.text
    assert captured["onyx_user_id"] is None


# ---------------------------------------------------------------------------
# IP allowlist middleware
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ip_allowlist_inactive_when_cidrs_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty CIDR list disables the global allowlist short-circuit.

    Requests pass straight through to the auth dep (which 401s without
    a bearer token) — the 403 path must NOT fire.
    """
    from rag_service import config as rag_config

    monkeypatch.setattr(
        rag_config.settings, "onyx_backend_allowed_cidrs", [], raising=False
    )
    app = create_app()
    async with _client(app) as ac:
        r = await ac.get(
            "/v1/onyx/kb",
            headers={"X-Forwarded-For": "8.8.8.8"},
        )
    assert r.status_code == 401, r.text


@pytest.mark.asyncio
async def test_ip_allowlist_active_blocks_disallowed_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller outside every configured CIDR is rejected with 403."""
    from rag_service import config as rag_config

    monkeypatch.setattr(
        rag_config.settings,
        "onyx_backend_allowed_cidrs",
        ["10.0.1.0/24"],
        raising=False,
    )
    app = create_app()
    async with _client(app) as ac:
        r = await ac.get(
            "/v1/onyx/kb",
            headers={"X-Forwarded-For": "8.8.8.8"},
        )
    assert r.status_code == 403, r.text
    assert r.json() == {"detail": "caller ip not allowed"}


@pytest.mark.asyncio
async def test_ip_allowlist_allows_in_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Caller inside an allowed CIDR passes the IP gate (still 401 from auth)."""
    from rag_service import config as rag_config

    monkeypatch.setattr(
        rag_config.settings,
        "onyx_backend_allowed_cidrs",
        ["10.0.1.0/24"],
        raising=False,
    )
    app = create_app()
    async with _client(app) as ac:
        r = await ac.get(
            "/v1/onyx/kb",
            headers={"X-Forwarded-For": "10.0.1.50"},
        )
    assert r.status_code == 401, r.text


@pytest.mark.asyncio
async def test_ip_allowlist_only_applies_to_onyx_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The allowlist must not affect the alpha API surface.

    A disallowed IP hitting ``/v1/documents`` (alpha) must reach its
    own auth dep — i.e. produce 401, not the IP-gate 403.
    """
    from rag_service import config as rag_config

    monkeypatch.setattr(
        rag_config.settings,
        "onyx_backend_allowed_cidrs",
        ["10.0.1.0/24"],
        raising=False,
    )
    app = create_app()
    async with _client(app) as ac:
        r = await ac.get(
            "/v1/documents",
            headers={"X-Forwarded-For": "8.8.8.8"},
        )
    assert r.status_code == 401, r.text


@pytest.mark.asyncio
async def test_ip_allowlist_uses_xff_first_hop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the first XFF hop is consulted (the hop closest to the client)."""
    from rag_service import config as rag_config

    monkeypatch.setattr(
        rag_config.settings,
        "onyx_backend_allowed_cidrs",
        ["10.0.1.0/24"],
        raising=False,
    )
    app = create_app()
    async with _client(app) as ac:
        # First hop in-range; later hops out-of-range. The first hop wins.
        r = await ac.get(
            "/v1/onyx/kb",
            headers={"X-Forwarded-For": "10.0.1.5, 192.168.1.1, 8.8.8.8"},
        )
    assert r.status_code == 401, r.text


@pytest.mark.asyncio
async def test_ip_allowlist_falls_back_to_client_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No XFF → fall back to ``request.client.host``.

    ASGITransport sets ``client=("testclient", 50000)`` by default, which
    fails ``ipaddress.ip_address(...)``. We rebuild the transport with
    a real-looking client tuple via the ``client`` kwarg.
    """
    from rag_service import config as rag_config

    monkeypatch.setattr(
        rag_config.settings,
        "onyx_backend_allowed_cidrs",
        ["10.0.1.0/24"],
        raising=False,
    )
    app = create_app()
    transport = ASGITransport(app=app, client=("10.0.1.5", 12345))
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get("/v1/onyx/kb")
    # In-range client.host → IP gate passes, auth dep returns 401.
    assert r.status_code == 401, r.text


@pytest.mark.asyncio
async def test_ip_allowlist_falls_back_to_client_host_blocks_out_of_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Out-of-range client.host with no XFF → 403."""
    from rag_service import config as rag_config

    monkeypatch.setattr(
        rag_config.settings,
        "onyx_backend_allowed_cidrs",
        ["10.0.1.0/24"],
        raising=False,
    )
    app = create_app()
    transport = ASGITransport(app=app, client=("8.8.8.8", 12345))
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get("/v1/onyx/kb")
    assert r.status_code == 403, r.text
    assert r.json() == {"detail": "caller ip not allowed"}


@pytest.mark.asyncio
async def test_ip_allowlist_v6_cidr_v4_caller_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A v4 caller cannot match a v6-only CIDR list (and vice versa).

    Mixing address families in :func:`ipaddress.ip_address.in_network`
    is a ``TypeError``; the middleware skips mismatched-family entries
    and ends up with no match → 403.
    """
    from rag_service import config as rag_config

    monkeypatch.setattr(
        rag_config.settings,
        "onyx_backend_allowed_cidrs",
        ["2001:db8::/32"],
        raising=False,
    )
    app = create_app()
    async with _client(app) as ac:
        r = await ac.get(
            "/v1/onyx/kb",
            headers={"X-Forwarded-For": "10.0.1.5"},
        )
    assert r.status_code == 403, r.text
    assert r.json() == {"detail": "caller ip not allowed"}


@pytest.mark.asyncio
async def test_ip_allowlist_malformed_xff_returns_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-IP-shaped XFF first hop must be rejected, not 500'd."""
    from rag_service import config as rag_config

    monkeypatch.setattr(
        rag_config.settings,
        "onyx_backend_allowed_cidrs",
        ["10.0.1.0/24"],
        raising=False,
    )
    app = create_app()
    async with _client(app) as ac:
        r = await ac.get(
            "/v1/onyx/kb",
            headers={"X-Forwarded-For": "not-an-ip"},
        )
    assert r.status_code == 403, r.text
    assert r.json() == {"detail": "caller ip not allowed"}
