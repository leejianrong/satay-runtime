"""Missing-extra gate for the test session (KAN-460).

Several test modules open with ``pytest.importorskip("fastapi")`` and friends, so
without ``satay[studio]`` installed they remove **themselves** from the run. That is the
right behaviour for a deliberately narrowed run and the wrong behaviour for a full-suite
run: ``uv run pytest -q`` on a dev-only environment prints a green summary while a whole
tier — including the KAN-408 missing-bundle gate — never executed. Nothing goes red when
the coverage disappears, which is the same class of failure as KAN-408 itself and as
KAN-576 (``make ci`` ran 138 of 427 tests and exited 0).

This module holds the predicate; ``tests/conftest.py`` holds the one hook that applies
it. It is a plain module rather than a fixture because the decision has to be made once
per session, at collection time, over the session as a whole — a fixture only ever sees
the tests that survived collection, and the modules at issue are exactly the ones that
did not.

The shape deliberately mirrors KAN-408's missing-bundle gate in
``tests/e2e/test_studio_serving.py``: **strict by default**, with one explicit
environment-variable opt-out that downgrades the error to a non-fatal warning. A runner
that forgot to export a flag must not silently lose the check.

Full-suite versus narrowed
--------------------------
``make test`` (``pytest tests/unit``) is the documented fast inner loop and runs on a
dev-only environment, so it must stay green — a guard that breaks the inner loop is
worse than the bug it fixes. The session therefore reads its own *request*:

* the session asked for everything under ``tests/`` (``config.args`` covers the tests
  root) and applied no narrowing switch  ->  a missing extra is a **hard error**;
* the session asked for a subset (a tier, a file, a node id, ``-k``, ``-m``, ``--lf``)
  ->  a missing extra is a **warning**, naming the modules that will be skipped.

Asking for a subset is already an explicit statement that you are not running
everything; asking for everything and silently getting less is the bug. That split
needs no environment variable in the ``Makefile``, so every documented raw command
(``uv run pytest tests/unit -q``, ``uv run pytest tests/integration --collect-only -q``)
keeps working untouched on a dev-only environment, while ``uv run pytest -q`` — what CI
runs and what ``make test-all`` runs — goes red the moment the extra is dropped.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path
from typing import Any

#: The tests directory. This module sits next to ``conftest.py``, so a full-suite run is
#: one whose requested paths cover this directory (or an ancestor of it).
TESTS_ROOT = Path(__file__).resolve().parent

#: Import names the ``satay[studio]`` extra installs (see ``pyproject.toml``). These are
#: the names the ``importorskip`` calls across the suite gate on. ``httpx`` is not here:
#: it is a dev-group dependency, so ``uv sync`` alone already provides it.
STUDIO_EXTRA_MODULES: tuple[str, ...] = ("fastapi", "uvicorn", "pydantic", "typer")

#: Opt out of the strict gate: a missing extra becomes a warning instead of an error.
#: Named after KAN-408's ``SATAY_ALLOW_MISSING_STUDIO_BUNDLE`` so the two gates read the
#: same way. Do not set it in CI.
ALLOW_MISSING_EXTRA_ENV_VAR = "SATAY_ALLOW_MISSING_STUDIO_EXTRA"

#: Values that opt out. Anything else — including a typo and the unset case — is strict.
#: Same set as KAN-408's gate, deliberately.
_TRUTHY = frozenset({"1", "true", "yes", "on"})

#: ``config.option`` attributes whose truthiness means "the user narrowed this run".
#: ``config.args`` cannot see these — ``pytest -q -k clock`` still reports ``['tests']``
#: — so they are read separately, and any of them downgrades the error to a warning.
_NARROWING_OPTIONS: tuple[str, ...] = (
    "keyword",  # -k
    "markexpr",  # -m
    "deselect",  # --deselect
    "lf",  # --lf / --last-failed
    "failedfirst",  # --ff (reorders, but pairs with --lf in practice)
    "stepwise",  # --sw
)


def opted_out(
    env_var: str = ALLOW_MISSING_EXTRA_ENV_VAR, environ: Mapping[str, str] | None = None
) -> bool:
    """Whether this checkout has explicitly opted out of the strict gate."""
    source = os.environ if environ is None else environ
    return source.get(env_var, "").strip().lower() in _TRUTHY


def missing_modules(names: Iterable[str] = STUDIO_EXTRA_MODULES) -> tuple[str, ...]:
    """The subset of ``names`` that cannot be imported in this interpreter.

    Uses ``find_spec`` rather than a real import: the point is to detect an absent
    distribution, and importing FastAPI into every session — including the dev-only
    inner loop — to answer the question would be a needless cost.
    """
    missing = []
    for name in names:
        try:
            found = find_spec(name) is not None
        except (ImportError, ValueError):  # pragma: no cover - a broken/partial install
            found = False
        if not found:
            missing.append(name)
    return tuple(missing)


def covers_whole_suite(args: Sequence[str], invocation_dir: Path) -> bool:
    """Whether the requested paths cover the whole ``tests/`` tree.

    ``args`` is ``config.args`` — pytest has already substituted ``testpaths`` when no
    path was given on the command line, and has already separated options from paths, so
    ``pytest -q`` arrives here as ``['tests']`` and ``pytest tests/unit -q`` as
    ``['tests/unit']``. A node-id suffix (``file.py::test_x``) is stripped before the
    comparison.
    """
    for arg in args:
        raw = str(arg).split("::", 1)[0]
        path = Path(raw)
        if not path.is_absolute():
            path = invocation_dir / path
        try:
            path = path.resolve()
        except OSError:  # pragma: no cover - defensive
            continue
        if path == TESTS_ROOT or TESTS_ROOT.is_relative_to(path):
            return True
    return False


def is_narrowed(option: Any) -> bool:
    """Whether a narrowing switch (``-k``, ``-m``, ``--lf`` …) is in play."""
    return any(getattr(option, name, None) for name in _NARROWING_OPTIONS)


@dataclass(frozen=True)
class ExtraGuardVerdict:
    """What the session should do about a missing extra."""

    missing: tuple[str, ...]
    fatal: bool
    message: str


def _message(missing: Sequence[str], *, fatal: bool) -> str:
    names = ", ".join(missing)
    head = (
        f"The satay[studio] extra is not installed ({names} missing), so every test "
        "module that gates on it removed itself from this session."
    )
    fix = (
        "Install it and re-run:\n"
        "  make dev-studio                       # uv sync --extra studio --frozen\n"
        "  make test-all                         # the full suite, as CI runs it"
    )
    if not fatal:
        return (
            f"{head}\n"
            "This run was narrowed to a subset, so it is a warning rather than an error "
            "— but the skipped modules are real coverage you are not getting.\n"
            f"{fix}"
        )
    return (
        f"{head}\n"
        "This session asked for the WHOLE suite, so a green result would be a lie: a "
        "whole tier — including the KAN-408 missing-bundle gate — would report success "
        "without running. Skipping it silently is the bug this gate exists to stop "
        "(KAN-460).\n"
        f"{fix}\n"
        f"To run the subset anyway, narrow the run (e.g. `uv run pytest tests/unit -q`) "
        f"or set {ALLOW_MISSING_EXTRA_ENV_VAR}=1 to downgrade this to a warning. Do not "
        "set it in CI: a dropped extra must stay red there."
    )


def evaluate(
    args: Sequence[str],
    invocation_dir: Path,
    option: Any,
    *,
    modules: Iterable[str] = STUDIO_EXTRA_MODULES,
    environ: Mapping[str, str] | None = None,
) -> ExtraGuardVerdict | None:
    """Decide what a session should do about the studio extra, or ``None`` if all is well.

    Split out from the hook so the decision is testable without a pytest session; the
    child-interpreter test in ``tests/integration/test_extra_guard.py`` covers the wiring.
    """
    missing = missing_modules(modules)
    if not missing:
        return None
    fatal = (
        covers_whole_suite(args, invocation_dir)
        and not is_narrowed(option)
        and not opted_out(environ=environ)
    )
    return ExtraGuardVerdict(missing=missing, fatal=fatal, message=_message(missing, fatal=fatal))
