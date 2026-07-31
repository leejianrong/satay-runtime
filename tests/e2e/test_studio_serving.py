"""The V5 server serves the built Studio SPA alongside the JSON API (V6, ADR-0013).

Studio is a pure consumer of the read API; V6 adds no runtime behaviour, only static
serving of the prebuilt bundle from the same process. These tests assert the bundle is
served and, crucially, that mounting it does **not** weaken the ADR-0014 auth guard on
the API. They require the ``satay[studio]`` extra (``uv sync --extra studio``) and a
built bundle in ``src/satay/_studio_assets/``.

**A missing bundle fails by default (KAN-408).** The bundle is committed to the repo, so
the realistic way it goes missing is a deletion or a packaging/build change that drops
it — and ``satay[studio]`` would then install and serve nothing. Skipping on absence
made that regression invisible to CI, so absence is now an error unless a checkout
explicitly opts out via ``SATAY_ALLOW_MISSING_STUDIO_BUNDLE=1``, which downgrades it
back to a skip so the rest of the suite still runs (e.g. mid-``pnpm build`` work in
``studio/``). The failure names both fixes.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from satay.control import server
from satay.control.commands import CommandQueue
from satay.control.security import TOKEN_HEADER, SecurityPolicy
from satay.control.server import create_app, studio_index
from satay.journal.store import SQLiteStore

TOKEN = "studio-serve-token"

#: Opt out of the strict bundle gate: absence becomes a skip instead of a failure.
#: Strict is the default so no CI configuration is needed for the gate to hold — a
#: runner that forgot to export a flag must not silently lose the check.
ALLOW_MISSING_BUNDLE_ENV_VAR = "SATAY_ALLOW_MISSING_STUDIO_BUNDLE"

#: Values that opt out. Anything else — including a typo and the unset case — is strict.
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _missing_bundle_is_allowed() -> bool:
    """Whether this checkout has opted out of the strict missing-bundle gate."""
    return os.environ.get(ALLOW_MISSING_BUNDLE_ENV_VAR, "").strip().lower() in _TRUTHY


def _missing_bundle_message() -> str:
    """Actionable diagnosis for an absent bundle, used for both the failure and the skip."""
    return (
        f"Studio bundle missing: {studio_index()} does not exist, so `satay[studio]` "
        "would install a debugger that serves nothing.\n"
        "The bundle is committed to the repo, so it was most likely deleted or dropped "
        "by a packaging/build change. Fix it with either:\n"
        "  - rebuild it:  cd studio && pnpm install && pnpm build   "
        "(Vite emits into src/satay/_studio_assets/)\n"
        "  - restore it:  git restore src/satay/_studio_assets\n"
        "If you are deliberately working without a bundle, set "
        f"{ALLOW_MISSING_BUNDLE_ENV_VAR}=1 to skip the serving tests instead of failing "
        "them. Do not set it in CI: a missing bundle must stay red there (KAN-408)."
    )


@pytest.fixture
def _bundle_served() -> None:
    """Guard the serving tests: a missing bundle fails unless explicitly allowed.

    Attaching the diagnosis here means a dropped bundle reports the fix instead of a bare
    404 from the assertions below.
    """
    if studio_index().is_file():
        return
    if _missing_bundle_is_allowed():
        pytest.skip(_missing_bundle_message())
    pytest.fail(_missing_bundle_message(), pytrace=False)


def _client(*, with_token: bool = True) -> TestClient:
    store = SQLiteStore.open(":memory:")
    app = create_app(
        store=store, command_queue=CommandQueue(), security=SecurityPolicy(token=TOKEN)
    )
    headers = {TOKEN_HEADER: TOKEN} if with_token else {}
    return TestClient(app, base_url="http://localhost", headers=headers)  # type: ignore[arg-type]


@pytest.mark.usefixtures("_bundle_served")
def test_serves_studio_index_at_root() -> None:
    with _client() as client:
        resp = client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        body = resp.text
        assert '<div id="app">' in body
        assert "Satay Studio" in body


@pytest.mark.usefixtures("_bundle_served")
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


@pytest.mark.usefixtures("_bundle_served")
def test_static_mount_does_not_bypass_api_auth() -> None:
    # The SPA is served unauthenticated, but the API guard (ADR-0014) is untouched:
    # a tokenless API read is still rejected even though a static mount now exists.
    with _client(with_token=False) as client:
        assert client.get("/").status_code == 200  # static SPA: no token required
        assert client.get("/runs").status_code == 401  # API: token still required


def test_missing_bundle_still_serves_the_api(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # API-only mode is a supported configuration (sdist / pre-build): create_app must not
    # raise and the JSON API must keep working, only the SPA mount goes away. Point the
    # asset dir at an empty location so this holds regardless of the real tree state —
    # the strict gate above is what keeps the real tree honest.
    monkeypatch.setattr(server, "STUDIO_ASSETS_DIR", tmp_path / "no-bundle")
    assert not studio_index().is_file()
    with _client() as client:
        assert client.get("/").status_code == 404  # no SPA mounted
        assert client.get("/runs").status_code == 200  # API unaffected
