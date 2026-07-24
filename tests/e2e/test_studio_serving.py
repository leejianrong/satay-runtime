"""The V5 server serves the built Studio SPA alongside the JSON API (V6, ADR-0013).

Studio is a pure consumer of the read API; V6 adds no runtime behaviour, only static
serving of the prebuilt bundle from the same process. These tests assert the bundle is
served and, crucially, that mounting it does **not** weaken the ADR-0014 auth guard on
the API. They require the ``satay[studio]`` extra and a built bundle in
``src/satay/_studio_assets/`` (produced by the frontend CI job / ``pnpm build``); when
the bundle is absent the serving tests skip rather than fail, so a bundle-less checkout
still runs the suite.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from satay.control.commands import CommandQueue
from satay.control.security import TOKEN_HEADER, SecurityPolicy
from satay.control.server import create_app, studio_index
from satay.journal.store import SQLiteStore

TOKEN = "studio-serve-token"

_needs_bundle = pytest.mark.skipif(
    not studio_index().is_file(),
    reason="Studio bundle not built (run `pnpm build` in studio/ or the frontend CI job)",
)


def _client(*, with_token: bool = True) -> TestClient:
    store = SQLiteStore.open(":memory:")
    app = create_app(
        store=store, command_queue=CommandQueue(), security=SecurityPolicy(token=TOKEN)
    )
    headers = {TOKEN_HEADER: TOKEN} if with_token else {}
    return TestClient(app, base_url="http://localhost", headers=headers)  # type: ignore[arg-type]


@_needs_bundle
def test_serves_studio_index_at_root() -> None:
    with _client() as client:
        resp = client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        body = resp.text
        assert '<div id="app">' in body
        assert "Satay Studio" in body


@_needs_bundle
def test_serves_studio_static_assets() -> None:
    # Parse the hashed asset path out of index.html and fetch it (proves /assets serving).
    with _client() as client:
        html = client.get("/").text
        import re

        match = re.search(r'src="\.?/?(assets/[^"]+\.js)"', html)
        assert match, f"no bundled JS asset referenced in index.html: {html!r}"
        asset = client.get("/" + match.group(1))
        assert asset.status_code == 200
        assert "javascript" in asset.headers["content-type"]


@_needs_bundle
def test_static_mount_does_not_bypass_api_auth() -> None:
    # The SPA is served unauthenticated, but the API guard (ADR-0014) is untouched:
    # a tokenless API read is still rejected even though a static mount now exists.
    with _client(with_token=False) as client:
        assert client.get("/").status_code == 200  # static SPA: no token required
        assert client.get("/runs").status_code == 401  # API: token still required


def test_serving_is_skipped_cleanly_without_a_bundle() -> None:
    # create_app must succeed whether or not the bundle exists (sdist / pre-build).
    store = SQLiteStore.open(":memory:")
    app = create_app(
        store=store, command_queue=CommandQueue(), security=SecurityPolicy(token=TOKEN)
    )
    assert app is not None
    store.close()
