"""Import-hygiene guard (Epic 0 CI step).

Proves the runtime core imports without any infrastructure and without pulling the
studio extra (FastAPI/uvicorn/Pydantic/Typer). Collected with ``--collect-only`` in CI
to assert the integration tier imports cleanly; the assertions also run under pytest.
"""

from __future__ import annotations

import importlib
import sys

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
    for name in list(sys.modules):
        if name in FORBIDDEN_IN_CORE or name.split(".")[0] in FORBIDDEN_IN_CORE:
            raise AssertionError(f"core import pulled a studio-only dependency: {name}")
