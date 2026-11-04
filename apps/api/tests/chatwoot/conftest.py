"""Chatwoot integration test fixtures (Phase 7 PR-2).

Stubbed ``httpx.AsyncClient`` matching the Chatwoot REST surface. Same
spirit as ``tests/outbound/conftest.py`` for Meta, but the Chatwoot
client uses ``client.request(method, url, ...)`` generically (not
``.post`` / ``.delete`` directly) — so the stub dispatches via
``request()``.

Route keys are ``(method, path_after_base_url)``; e.g. a real call to
``https://chat.getpolaris.my/api/v1/accounts/1/conversations/42/toggle_status``
routes on ``("POST", "api/v1/accounts/1/conversations/42/toggle_status")``.

The stub also supports a per-route queue of responses so a single route
can program "fail 5xx twice then succeed" sequences for retry tests.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx
import pytest

if TYPE_CHECKING:
    from polaris_api.services.chatwoot.client import ChatwootClient

# Type alias for the cw_register fixture's returned helper. Lets test
# functions declare `cw_register: CWRegister` for mypy parity.
CWRegister = Callable[..., None]


@dataclass
class CWResponse:
    """One programmed response from the stub.

    Either ``json_body`` or ``text_body`` (mutually exclusive — set the
    one that matches what real Chatwoot returns for the case under test).
    ``raise_exc`` is for simulating network failures (timeout, connection-
    reset); when set, the stub raises this instead of returning a response.
    """

    status_code: int = 200
    json_body: dict[str, Any] | list[Any] | None = None
    text_body: str | None = None
    raise_exc: BaseException | None = None


@dataclass
class CWCall:
    """One captured request the stub received."""

    method: str
    url: str
    path: str  # url portion after base URL (route key)
    json: dict[str, Any] | None
    headers: dict[str, str]


@dataclass
class StubRouteQueue:
    """Sequence of responses returned in order for repeated calls to the same route.

    Set up by tests that need to model retry-then-success or
    label-not-present-then-created sequences.
    """

    queue: deque[CWResponse] = field(default_factory=deque)


_BASE_URL = "https://chat.getpolaris.my"


class StubChatwootClient:
    """Async-context-manager stub matching ``httpx.AsyncClient``."""

    def __init__(
        self,
        route_map: dict[tuple[str, str], StubRouteQueue],
    ) -> None:
        self._routes = route_map
        self.calls: list[CWCall] = []

    async def __aenter__(self) -> StubChatwootClient:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        return None

    @staticmethod
    def _path_for(url: str) -> str:
        """Slice the URL down to the path after the configured base URL."""
        if url.startswith(_BASE_URL + "/"):
            return url[len(_BASE_URL) + 1 :].split("?", 1)[0]
        # Fall back to anything after the host so tests catch URL drift.
        try:
            after_scheme = url.split("://", 1)[1]
            _, rest = after_scheme.split("/", 1)
            return rest.split("?", 1)[0]
        except (IndexError, ValueError):
            return url

    async def request(
        self,
        method: str,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,  # noqa: ARG002, ASYNC109 — B6: shared-
        # client call sites always pass an explicit per-call timeout; the
        # stub doesn't enforce it.
        follow_redirects: bool | None = None,  # noqa: ARG002 — see above
    ) -> httpx.Response:
        path = self._path_for(url)
        self.calls.append(
            CWCall(
                method=method,
                url=url,
                path=path,
                json=json,
                headers=headers or {},
            )
        )
        key = (method.upper(), path)
        bucket = self._routes.get(key)
        if bucket is None or not bucket.queue:
            raise AssertionError(
                f"stub: no route registered for ({method.upper()}, {path}). "
                f"Test populated routes: {sorted(self._routes.keys())}"
            )
        programmed = bucket.queue.popleft() if len(bucket.queue) > 1 else bucket.queue[0]
        # If there's only one response left, leave it for any repeat
        # calls (a sticky default). If there are more, consume one per
        # call (sequenced retry behaviour).
        if programmed.raise_exc is not None:
            raise programmed.raise_exc
        if programmed.json_body is not None:
            return httpx.Response(programmed.status_code, json=programmed.json_body)
        return httpx.Response(programmed.status_code, text=programmed.text_body or "")


@pytest.fixture
def cw_route_map() -> dict[tuple[str, str], StubRouteQueue]:
    return {}


def _register(
    route_map: dict[tuple[str, str], StubRouteQueue],
    method: str,
    path: str,
    *responses: CWResponse,
) -> None:
    bucket = route_map.setdefault((method.upper(), path), StubRouteQueue())
    for r in responses:
        bucket.queue.append(r)


@pytest.fixture
def cw_register(
    cw_route_map: dict[tuple[str, str], StubRouteQueue],
) -> CWRegister:
    """Helper to register one or more responses for a route.

    Multiple responses on the same route are consumed in order (used by
    retry-budget tests: 5xx, 5xx, 200 → final call succeeds on the third
    attempt).
    """

    def _add(method: str, path: str, *responses: CWResponse) -> None:
        _register(cw_route_map, method, path, *responses)

    return _add


class _Httpx:
    """Thin shim so ``chatwoot.client.httpx.AsyncClient(...)`` returns
    our stub but ``chatwoot.client.httpx.Response(...)`` /
    ``httpx.TimeoutException`` still resolve to the real httpx.

    Mirrors the Phase 6 pattern in ``tests/outbound/conftest.py``.
    """

    def __init__(self, async_client_factory: Any) -> None:
        self.AsyncClient = async_client_factory
        self.Response = httpx.Response
        self.TimeoutException = httpx.TimeoutException
        self.ConnectError = httpx.ConnectError


@pytest.fixture
def stub_chatwoot_httpx(
    monkeypatch: pytest.MonkeyPatch,
    cw_route_map: dict[tuple[str, str], StubRouteQueue],
) -> StubChatwootClient:
    """Patch httpx inside the chatwoot.client module via the _Httpx shim.

    Patching the whole ``httpx`` symbol (rather than just
    ``httpx.AsyncClient`` in-place) keeps mypy happy about the module
    attribute and matches the Phase 6 pattern.
    """
    captured = StubChatwootClient(cw_route_map)

    def _factory(*_args: Any, **_kwargs: Any) -> StubChatwootClient:
        return captured

    import polaris_api.services.chatwoot.client as cw_client_mod

    monkeypatch.setattr(cw_client_mod, "httpx", _Httpx(_factory))
    # Disable backoff sleeps for speed — retry tests don't need wall time.
    monkeypatch.setattr(
        cw_client_mod.ChatwootClient,
        "_backoff_seconds",
        staticmethod(lambda _attempt: 0.0),
    )
    return captured


@pytest.fixture
def chatwoot_client() -> ChatwootClient:
    """A ChatwootClient with non-placeholder config suitable for tests."""
    from polaris_api.services.chatwoot.client import ChatwootClient

    return ChatwootClient(
        base_url=_BASE_URL,
        bot_access_token="test-token-not-placeholder",
        # Label + contact verbs call _check_user_config(), which requires a
        # non-empty, non-placeholder agent_api_token (CW rejects bot tokens
        # for those endpoints). Supply one so the label-flow tests exercise
        # the GET->POST path instead of fail-closing with ChatwootConfigError.
        agent_api_token="test-user-token-not-placeholder",
        timeout_s=1.0,
        max_retries=2,
        label_auto_provision=True,
    )
