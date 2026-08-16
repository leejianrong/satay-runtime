"""Root test configuration.

Loads the first-class testing-seam fixtures (ADR-0011) as a pytest plugin so every
tier can inject the manual clock, seeded RNG, fault injector, temp-store paths, and the
``drain`` helper that drives a run through backoff on the manual clock.

``drain`` used to be defined here. It is now :func:`satay.testing.settle`, shipped from
the plugin, because the ``examples/`` scripts need the same loop and a script cannot use
a pytest fixture (KAN-482).

It also carries the **missing-extra gate** (KAN-460): a full-suite run without
``satay[studio]`` fails instead of quietly skipping every module that gates on it. The
predicate lives in :mod:`_extra_guard` next door; only the hook is here.
"""

from __future__ import annotations

import warnings

import pytest

from _extra_guard import evaluate

pytest_plugins = ["satay.testing.fixtures"]


class MissingExtraWarning(UserWarning):
    """A narrowed run is missing an extra, so some modules skipped themselves."""


def pytest_collection_finish(session: pytest.Session) -> None:
    """Refuse to report success for a whole-suite run that is missing the studio extra.

    Runs at collection finish, before any test executes, so a dropped extra costs a
    couple of seconds rather than a full suite. See :mod:`_extra_guard` for why a
    full-suite run errors while a narrowed one only warns.
    """
    config = session.config
    verdict = evaluate(config.args, config.invocation_params.dir, config.option)
    if verdict is None:
        return
    if verdict.fatal:
        pytest.exit(verdict.message, returncode=pytest.ExitCode.USAGE_ERROR)
    warnings.warn(verdict.message, MissingExtraWarning, stacklevel=1)
