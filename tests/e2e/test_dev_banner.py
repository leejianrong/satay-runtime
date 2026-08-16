"""End-to-end: ``satay dev`` prints its tokenized URL even when stdout is a pipe (KAN-491).

``satay dev`` prints its banner once and then blocks until Ctrl-C. Python only
line-buffers stdout when it is a tty, so redirected — ``satay dev > dev.log``, a CI step,
an agent capturing output — the banner sat in the buffer for the whole life of the
process and the tokenized Studio URL, the one thing you cannot reconstruct by hand, never
arrived. Three people hit it before it was written down.

The test has to be a real subprocess with a real pipe: that is the only place the bug
exists. ``PYTHONUNBUFFERED`` is explicitly cleared, because setting it is the workaround
this is meant to make unnecessary. Reads go through :mod:`selectors` on a non-blocking
pipe so that an unflushed banner fails on the deadline instead of hanging the suite —
which is precisely what it did before the fix.
"""

from __future__ import annotations

import os
import re
import selectors
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("uvicorn")
pytest.importorskip("typer")

#: Generous — the boot is well under a second — but bounded, because this is also how
#: long the *failing* case takes to report.
BANNER_TIMEOUT_SECONDS = 15.0

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="selectors cannot poll a pipe on Windows (ADR-0019)"
)


def _read_until_token(proc: subprocess.Popen[bytes], deadline: float) -> str:
    """Drain the child's stdout until the tokenized URL shows up, or the deadline does."""
    assert proc.stdout is not None
    os.set_blocking(proc.stdout.fileno(), False)
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    buffer = b""
    try:
        while time.monotonic() < deadline:
            if not selector.select(timeout=0.5):
                continue
            chunk = proc.stdout.read()
            if chunk:
                buffer += chunk
                if b"?token=" in buffer:
                    break
            elif proc.poll() is not None:  # pragma: no cover - the stack exited early
                break
    finally:
        selector.close()
    return buffer.decode(errors="replace")


def test_the_dev_banner_reaches_a_pipe_without_pythonunbuffered(tmp_path: Path) -> None:
    """The tokenized Studio URL arrives while the stack is still running."""
    env = {**os.environ}
    env.pop("PYTHONUNBUFFERED", None)
    env["PYTHONWARNINGS"] = "ignore"

    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; from satay.cli import main; sys.exit(main())",
            "dev",
            "--port",
            "0",
            "--data-dir",
            str(tmp_path / ".satay"),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    try:
        banner = _read_until_token(proc, time.monotonic() + BANNER_TIMEOUT_SECONDS)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            proc.kill()
            proc.wait(timeout=15)
        if proc.stdout is not None:
            proc.stdout.close()

    assert "Satay Studio:" in banner, f"banner never reached the pipe: {banner!r}"
    assert re.search(r"http://127\.0\.0\.1:\d+/\?token=\S+", banner), banner
    assert "press Ctrl-C to stop" in banner, banner
