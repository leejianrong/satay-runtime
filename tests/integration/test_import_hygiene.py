"""Import-hygiene guard (Epic 0 CI step).

Proves the runtime core imports without any infrastructure and without pulling the
studio extra (FastAPI/uvicorn/Pydantic/Typer). Collected with ``--collect-only`` in CI
to assert the integration tier imports cleanly; the assertions also run under pytest.

The no-studio-dependency assertion runs in a **fresh subprocess** (V5): the studio extra
is installed in the same CI job that runs the HTTP-surface tests, and some sibling test
modules import FastAPI, so scanning the *current* interpreter's ``sys.modules`` would be
contaminated by other tests. A clean child interpreter imports only the core modules and
proves none of the forbidden packages were pulled — a strictly stronger guard than the
in-process scan it replaces (the boundary is enforced, never weakened).
"""

from __future__ import annotations

import importlib
import subprocess
import sys
import textwrap

CORE_MODULES = [
    "satay",
    "satay.config",
    "satay.api",
    "satay.replay",
    "satay.journal",
    "satay.executor",
    "satay.timers",
    "satay.control",
    "satay.versioning",
    "satay.blobs",
    "satay.devstack",
    "satay.testing",
    "satay.cli",
]

FORBIDDEN_IN_CORE = ["fastapi", "uvicorn", "pydantic", "typer", "click"]


def test_core_modules_import_cleanly() -> None:
    for name in CORE_MODULES:
        importlib.import_module(name)


def test_core_import_pulls_no_studio_dependency() -> None:
    program = textwrap.dedent(
        f"""
        import importlib, sys
        for name in {CORE_MODULES!r}:
            importlib.import_module(name)
        forbidden = {FORBIDDEN_IN_CORE!r}
        pulled = sorted(
            n for n in sys.modules if n.split(".")[0] in forbidden
        )
        if pulled:
            sys.stdout.write("PULLED:" + ",".join(pulled))
            raise SystemExit(1)
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"core import pulled a studio-only dependency: {result.stdout} {result.stderr}"
    )
