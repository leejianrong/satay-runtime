"""E2E: every URL an example *prints* must be a URL the server actually serves.

``tests/e2e/test_examples.py`` proves the examples run and leave a coherent journal. It
cannot prove the walkthrough they print is followable, and that gap shipped a real bug:
``studio_walkthrough.py`` advertised ``/runs/<id>/compare?other_run_id=<id>`` while the
route takes a **required** ``?to=``, so the reader's own copy-pasted URL came back 422
(KAN-490). Nothing was red, because no test ever issued the request the example suggests.

So this module closes the loop end to end: run the example against a temp data dir, scrape
the URLs out of its stdout, then replay every one of them through the real FastAPI app
mounted over **the journal that example just wrote**. A printed URL that 404s, 422s or
names a route that does not exist fails here.

Why a module of its own rather than more cases in ``test_examples.py``: this needs the
``satay[studio]`` extra (FastAPI + the read API), and that module is deliberately
core-only so it runs under the plain dev env. Keeping the studio dependency quarantined
here leaves it that way.

Discovery is static and generic — any example whose source mentions an HTTP URL or a
``/runs`` path is picked up automatically — so a future example that prints an endpoint is
covered the day it lands, without editing this file.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote, urlsplit

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from satay.config import DATA_DIR_ENV_VAR, db_path
from satay.control.commands import CommandQueue
from satay.control.security import TOKEN_HEADER, SecurityPolicy
from satay.control.server import create_app
from satay.journal.store import SQLiteStore

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLES_DIR = REPO_ROOT / "examples"
TOKEN = "test-session-token"
EXAMPLE_TIMEOUT_SECONDS = 120

#: An example worth scanning: its source mentions an HTTP URL or a ``/runs`` path.
_MENTIONS_A_URL = re.compile(r"https?://|/runs\b")

#: Absolute URLs are unambiguous; take them whole and let :func:`urlsplit` do the work.
_ABSOLUTE_URL = re.compile(r"""https?://[^\s'"`,)\]]+""")

#: Bare paths. The lookbehind keeps this off the tail of a longer token, and only paths
#: whose first segment is a real top-level route (below) survive the filter — otherwise
#: every filesystem path in the output ("/tmp/...", the data dir) would be mistaken for an
#: endpoint. The blind spot is a printed path with a bogus FIRST segment; every deeper
#: segment and every query parameter — where this bug lived — is covered.
_BARE_PATH = re.compile(r"""(?<![\w.~/])(/[^\s'"`,)\]]*)""")

#: Punctuation that ends an English sentence, not a URL.
_TRAILING_PROSE = ".,;:!?)]}"

EXAMPLES_WITH_URLS = sorted(
    path.name for path in EXAMPLES_DIR.glob("*.py") if _MENTIONS_A_URL.search(path.read_text())
)


def test_at_least_one_example_advertises_the_read_api() -> None:
    """Guards the discovery above: a bad filter must not make this module vacuous."""
    assert EXAMPLES_WITH_URLS, "no example mentions a URL — the discovery filter is broken"


def _route_roots(app: object) -> set[str]:
    """The first path segment of every route the app declares (e.g. ``{"runs"}``)."""
    roots = set()
    for route in app.routes:  # type: ignore[attr-defined]
        segment = str(getattr(route, "path", "")).strip("/").split("/")[0]
        if segment and not segment.startswith("{"):
            roots.add(segment)
    return roots


def printed_urls(text: str, *, route_roots: set[str]) -> list[tuple[str, str]]:
    """Every ``(path, query)`` pair that looks like a request to the local server."""
    found: list[tuple[str, str]] = []
    for match in _ABSOLUTE_URL.finditer(text):
        parts = urlsplit(match.group(0).rstrip(_TRAILING_PROSE))
        found.append((parts.path or "/", parts.query))
    for match in _BARE_PATH.finditer(text):
        token = match.group(1).rstrip(_TRAILING_PROSE)
        if token.startswith("//"):
            continue  # scheme-relative; already taken by _ABSOLUTE_URL
        parts = urlsplit(token)
        if parts.path.strip("/").split("/")[0] not in route_roots:
            continue
        found.append((parts.path, parts.query))
    return sorted(set(found))


def resolve_placeholders(text: str, *, run_id: str, identity: str) -> str:
    """Substitute the ``<angle>`` / ``{brace}`` holes an example prints into a real value.

    An example prints a template for the reader to fill in; the test has to fill it in the
    way the reader would, from the journal the example just wrote.
    """

    def substitute(match: re.Match[str]) -> str:
        name = match.group(1).lower()
        if "identity" in name:
            return quote(identity, safe="")
        if "token" in name:
            return TOKEN
        return run_id

    return re.sub(r"[<{]([^<>{}]*)[>}]", substitute, text)


def run_example(name: str, data_dir: Path) -> str:
    """Run one example against ``data_dir`` and return its stdout, asserting a clean exit."""
    proc = subprocess.run(
        [sys.executable, str(EXAMPLES_DIR / name)],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONUNBUFFERED": "1", DATA_DIR_ENV_VAR: str(data_dir)},
        cwd=REPO_ROOT,
        timeout=EXAMPLE_TIMEOUT_SECONDS,
        check=False,
    )
    assert proc.returncode == 0, f"{name} exited {proc.returncode}\n{proc.stdout[-2000:]}"
    return proc.stdout


def _addressable_run(client: TestClient) -> tuple[str, str]:
    """A ``(run_id, identity)`` pair from the journal that every read endpoint resolves.

    Read back over HTTP rather than out of the store, so the ids the templates get filled
    with are ids the API itself just handed out.
    """
    for run in client.get("/runs").json()["runs"]:
        run_id = run["run_id"]
        for node in client.get(f"/runs/{run_id}/tree").json()["nodes"]:
            if node.get("kind") == "task":
                return run_id, str(node["identity"])
    raise AssertionError("no run in the journal has a task to name in a /tasks/ URL")


@pytest.mark.parametrize("name", EXAMPLES_WITH_URLS)
def test_every_printed_url_is_a_route_the_server_serves(name: str, tmp_path: Path) -> None:
    """Replay the example's own printed URLs against the app, over its own journal."""
    data_dir = tmp_path / "data"
    stdout = run_example(name, data_dir)

    store = SQLiteStore.open(db_path(data_dir))
    try:
        app = create_app(
            store=store, command_queue=CommandQueue(), security=SecurityPolicy(token=TOKEN)
        )
        urls = printed_urls(stdout, route_roots=_route_roots(app))
        if not urls:
            pytest.skip(f"{name} mentions a URL in its source but prints none")

        headers = {TOKEN_HEADER: TOKEN}
        with TestClient(app, base_url="http://localhost", headers=headers) as client:
            # Fill the templates from the journal the example just wrote, so a real run id
            # and a real task identity go over the wire — a 404 then means a wrong URL,
            # not a made-up id.
            run_id, identity = _addressable_run(client)
            for path, query in urls:
                url = resolve_placeholders(
                    path + (f"?{query}" if query else ""), run_id=run_id, identity=identity
                )
                response = client.get(url)
                assert response.status_code == 200, (
                    f"{name} prints {path}{'?' + query if query else ''}, which the server "
                    f"answers with {response.status_code} for {url}: {response.text[:300]}"
                )
    finally:
        store.close()


def test_the_walkthrough_advertises_the_whole_read_api(tmp_path: Path) -> None:
    """Non-vacuity: the walkthrough names all four read endpoints, compare with ``?to=``.

    Without this, deleting the endpoint list from the example would make the guard above
    pass by having nothing left to check — and ``?to=`` is the exact spelling KAN-490 got
    wrong, so it is pinned by name rather than only by a 200.
    """
    data_dir = tmp_path / "data"
    stdout = run_example("studio_walkthrough.py", data_dir)

    store = SQLiteStore.open(db_path(data_dir))
    try:
        app = create_app(
            store=store, command_queue=CommandQueue(), security=SecurityPolicy(token=TOKEN)
        )
        urls = printed_urls(stdout, route_roots=_route_roots(app))
    finally:
        store.close()

    paths = {path for path, _ in urls}
    assert "/runs" in paths
    assert any(path.endswith("/timeline") for path in paths)
    assert any(path.endswith("/tree") for path in paths)
    assert any("/tasks/" in path for path in paths)

    compare = [(path, query) for path, query in urls if path.endswith("/compare")]
    assert compare, "the walkthrough no longer shows the compare endpoint"
    for _, query in compare:
        assert query.startswith("to="), (
            f"compare is advertised as ?{query} — the route's query parameter is `to`, "
            "and it is required, so anything else is a 422 (KAN-490)"
        )
