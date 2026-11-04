"""Outbound (Phase 6) test fixtures.

Single source of truth for the stubbed ``httpx.AsyncClient`` used by
the meta_whatsapp tests. Each route is keyed by ``(method,
url_suffix)`` so a test can assert "the marketing endpoint was hit"
without caring about the full URL or auth headers.

Two fixture entrypoints:

  ``meta_route_map`` — yields a mutable dict ``{(method, suffix):
      MetaResponse}``; tests populate it with the responses they
      expect to fire.

  ``stub_httpx_client`` — monkeypatches ``httpx.AsyncClient`` inside
      the meta_whatsapp module so any code path that constructs one
      receives the stub. Returns a ``StubClient`` instance with a
      ``calls`` list of ``StubCall`` records for assertion.

The stub matches by suffix-after-graph-version: e.g. a real URL
``https://graph.facebook.com/v21.0/123/messages`` matches a route
keyed on ``("POST", "123/messages")``. This keeps the test routes
readable without hardcoding the base URL.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
import pytest


@dataclass
class MetaResponse:
    """One programmed response from the stub."""

    status_code: int
    json_body: dict[str, Any] | None = None
    text_body: str | None = None  # for 5xx that returns HTML


@dataclass
class StubCall:
    """One captured request the stub received."""

    method: str
    url: str
    json: dict[str, Any] | None
    headers: dict[str, str]
    suffix: str  # url portion AFTER the graph version


class StubClient:
    """Async-context-manager stub matching ``httpx.AsyncClient``'s surface.

    Holds a reference to the test's ``meta_route_map`` so a test can
    populate routes after the client has already been constructed
    (matters when the production code constructs the client lazily
    inside the call).
    """

    def __init__(self, route_map: dict[tuple[str, str], MetaResponse]) -> None:
        self._routes = route_map
        self.calls: list[StubCall] = []

    async def __aenter__(self) -> StubClient:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        return None

    def _suffix_for(self, url: str) -> str:
        """Slice the URL down to the part after the graph version.

        ``https://graph.facebook.com/v21.0/PHONE/messages`` ->
        ``PHONE/messages``. Falls back to the full URL if the version
        segment isn't present (defensive — never happens with the real
        client, but a misconfigured test fixture shouldn't crash with
        IndexError).
        """
        marker = "/v"
        idx = url.find(marker)
        if idx == -1:
            return url
        after = url[idx + 1 :]  # "v21.0/PHONE/messages"
        try:
            _, rest = after.split("/", 1)
        except ValueError:
            return url
        return rest

    async def _dispatch(
        self,
        method: str,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        suffix = self._suffix_for(url)
        # Strip query string for the route key so tests can assert by
        # path only.
        suffix_no_query = suffix.split("?", 1)[0]
        self.calls.append(
            StubCall(
                method=method,
                url=url,
                json=json,
                headers=headers or {},
                suffix=suffix,
            )
        )
        route = self._routes.get((method, suffix_no_query))
        if route is None:
            raise AssertionError(
                f"stub: no route registered for ({method}, {suffix_no_query}). "
                f"Test populated routes: {sorted(self._routes.keys())}"
            )
        if route.json_body is not None:
            return httpx.Response(route.status_code, json=route.json_body)
        return httpx.Response(route.status_code, text=route.text_body or "")

    async def post(
        self,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,  # noqa: ARG002, ASYNC109 — B6: shared-
        # client call sites always pass an explicit per-call timeout; the
        # stub doesn't enforce it.
    ) -> httpx.Response:
        return await self._dispatch("POST", url, json=json, headers=headers)

    async def delete(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,  # noqa: ARG002, ASYNC109 — see post() above
    ) -> httpx.Response:
        return await self._dispatch("DELETE", url, headers=headers)


@pytest.fixture
def meta_route_map() -> dict[tuple[str, str], MetaResponse]:
    return {}


@pytest.fixture
def stub_httpx_client(
    monkeypatch: pytest.MonkeyPatch,
    meta_route_map: dict[tuple[str, str], MetaResponse],
) -> StubClient:
    """Patch httpx.AsyncClient inside the meta_whatsapp module.

    The production code does ``async with httpx.AsyncClient(...) as
    client``; replacing the symbol inside the module means the with-
    block resolves to our StubClient. ``timeout=`` kwarg is ignored
    (StubClient doesn't care).
    """

    captured = StubClient(meta_route_map)

    def _factory(*_args: Any, **_kwargs: Any) -> StubClient:
        return captured

    from polaris_api.services import meta_whatsapp

    monkeypatch.setattr(meta_whatsapp, "httpx", _Httpx(_factory))
    return captured


class _Httpx:
    """Thin shim so ``meta_whatsapp.httpx.AsyncClient(...)`` returns
    our stub but ``meta_whatsapp.httpx.Response(...)`` still works.

    The module's call sites use ``httpx.AsyncClient`` and re-export
    ``httpx.Response`` indirectly through the dispatch return.
    """

    def __init__(self, async_client_factory: Any) -> None:
        self.AsyncClient = async_client_factory
        self.Response = httpx.Response


@pytest.fixture
def meta_client():  # type: ignore[no-untyped-def]
    """A MetaWhatsAppClient pinned to test-friendly defaults.

    Demo phone_number_id + token are non-placeholder so ``_resolve_token``
    accepts demo calls. base_url is the real Graph URL so tests
    asserting URL shape see realistic input; the stub intercepts before
    network ever happens.
    """
    from polaris_api.services.meta_whatsapp import MetaWhatsAppClient

    return MetaWhatsAppClient(
        base_url="https://graph.facebook.com",
        graph_api_version="v21.0",
    )
