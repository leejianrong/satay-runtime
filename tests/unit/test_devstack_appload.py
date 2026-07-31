"""Unit tests for ``satay dev --app`` module loading (KAN-448).

The dev stack's registry is only ever populated by ``@satay.workflow`` / ``@satay.task``
decorators running at **import time**, so a ``satay dev`` that imports nothing can never
wake a parked run. These tests cover the loader that closes that gap: what it imports,
where it looks when ``--app`` is absent, that every failure is loud and names the module,
and that making the project importable cannot shadow a stdlib module.

Pure stdlib (``importlib`` + ``tomllib``) — no studio extra needed.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from satay.api.registry import REGISTRY
from satay.devstack.appload import (
    AppImportError,
    AppLoadReport,
    load_app,
    resolve_app_modules,
)

_MODULE_SOURCE = """
import satay


@satay.task()
async def {prefix}_step(value: str) -> str:
    return value


@satay.workflow
async def {prefix}_flow(value: str) -> str:
    return await {prefix}_step(value)
"""


@pytest.fixture
def project(tmp_path: Path) -> Iterator[Path]:
    """A throwaway project directory, with ``sys.path``/``sys.modules`` restored after."""
    modules_before = set(sys.modules)
    path_before = list(sys.path)
    yield tmp_path
    for name in set(sys.modules) - modules_before:
        del sys.modules[name]
    sys.path[:] = path_before


def _write_module(project: Path, name: str) -> None:
    (project / f"{name}.py").write_text(_MODULE_SOURCE.format(prefix=name), encoding="utf-8")


# -- the happy path --------------------------------------------------------------


def test_app_module_import_registers_its_workflows_and_tasks(project: Path) -> None:
    """The whole point: after loading, the registry can resolve the user's workflow."""
    _write_module(project, "kan448_ok")

    report = load_app(["kan448_ok"], project_dir=project)

    assert REGISTRY.get_workflow("kan448_ok_flow") is not None
    assert REGISTRY.get_task("kan448_ok_step") is not None
    assert report.modules == ("kan448_ok",)
    assert report.source == "--app"
    assert report.can_run_workflows
    assert "kan448_ok_flow" in report.workflows
    assert "kan448_ok_step" in report.tasks


def test_report_names_the_counts_and_the_registered_names(project: Path) -> None:
    """A user who typos --app should read the count, not hunt a stack trace later."""
    _write_module(project, "kan448_report")

    lines = load_app(["kan448_report"], project_dir=project).describe()

    assert lines[0] == "app modules (--app): kan448_report"
    assert "kan448_report_flow" in lines[1]
    assert lines[1].startswith("registered: ")


def test_empty_registry_boot_says_zero_workflows_and_why() -> None:
    """The silent-empty-registry bug: the no-app boot must state the consequence."""
    report = AppLoadReport(modules=(), source="none", workflows=(), tasks=())

    lines = report.describe()

    assert not report.can_run_workflows
    assert lines[0].startswith("app modules: none")
    assert "0 workflows" in lines[1]
    assert any("cannot start a run or wake one" in line for line in lines)


# -- loud failures ---------------------------------------------------------------


def test_missing_module_raises_naming_the_module(project: Path) -> None:
    with pytest.raises(AppImportError) as excinfo:
        load_app(["kan448_absent"], project_dir=project)

    message = str(excinfo.value)
    assert "kan448_absent" in message
    assert "not found" in message


def test_module_raising_on_import_surfaces_the_underlying_cause(project: Path) -> None:
    (project / "kan448_boom.py").write_text(
        'raise RuntimeError("DATABASE_URL is not set")\n', encoding="utf-8"
    )

    with pytest.raises(AppImportError) as excinfo:
        load_app(["kan448_boom"], project_dir=project)

    message = str(excinfo.value)
    assert "kan448_boom" in message
    assert "RuntimeError" in message
    assert "DATABASE_URL is not set" in message
    assert isinstance(excinfo.value.__cause__, RuntimeError)


def test_module_with_a_missing_dependency_blames_the_dependency(project: Path) -> None:
    """A broken import *inside* the user's module must not read as 'module not found'."""
    (project / "kan448_needs_dep.py").write_text(
        "import kan448_no_such_package\n", encoding="utf-8"
    )

    with pytest.raises(AppImportError) as excinfo:
        load_app(["kan448_needs_dep"], project_dir=project)

    message = str(excinfo.value)
    assert "kan448_needs_dep" in message
    assert "kan448_no_such_package" in message
    assert "not installed" in message


def test_a_file_path_is_rejected_with_a_hint(project: Path) -> None:
    with pytest.raises(AppImportError) as excinfo:
        load_app(["src/userapp.py"], project_dir=project)

    assert "dotted module path" in str(excinfo.value)


# -- where the module list comes from --------------------------------------------


def test_pyproject_supplies_the_default_module_list(project: Path) -> None:
    _write_module(project, "kan448_cfg")
    (project / "pyproject.toml").write_text(
        '[tool.satay]\napp = ["kan448_cfg"]\n', encoding="utf-8"
    )

    report = load_app(None, project_dir=project)

    assert report.modules == ("kan448_cfg",)
    assert report.source.endswith("pyproject.toml")
    assert REGISTRY.get_workflow("kan448_cfg_flow") is not None


def test_a_bare_string_in_the_config_is_accepted(project: Path) -> None:
    (project / "pyproject.toml").write_text('[tool.satay]\napp = "one.module"\n', encoding="utf-8")

    modules, source = resolve_app_modules(None, project_dir=project)

    assert modules == ("one.module",)
    assert source.endswith("pyproject.toml")


def test_explicit_app_wins_over_the_config(project: Path) -> None:
    _write_module(project, "kan448_explicit")
    (project / "pyproject.toml").write_text(
        '[tool.satay]\napp = ["kan448_from_config"]\n', encoding="utf-8"
    )

    report = load_app(["kan448_explicit"], project_dir=project)

    assert report.modules == ("kan448_explicit",)
    assert report.source == "--app"


def test_a_pyproject_without_the_key_is_simply_no_modules(project: Path) -> None:
    (project / "pyproject.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")

    assert resolve_app_modules(None, project_dir=project) == ((), "none")


def test_a_malformed_config_value_is_an_error_not_a_shrug(project: Path) -> None:
    (project / "pyproject.toml").write_text("[tool.satay]\napp = 3\n", encoding="utf-8")

    with pytest.raises(AppImportError) as excinfo:
        resolve_app_modules(None, project_dir=project)

    assert "must be a module path or a list of module paths" in str(excinfo.value)


def test_unparseable_toml_names_the_file(project: Path) -> None:
    (project / "pyproject.toml").write_text("[tool.satay\n", encoding="utf-8")

    with pytest.raises(AppImportError) as excinfo:
        resolve_app_modules(None, project_dir=project)

    assert "pyproject.toml" in str(excinfo.value)


# -- module-shadowing safety -----------------------------------------------------


def test_project_dir_is_appended_to_sys_path_never_prepended(project: Path) -> None:
    """A stray ``queue.py`` in the project must not be able to shadow the stdlib.

    ``sys.path`` gains the project directory only at the **end**, so stdlib and
    site-packages resolve first. Prepending (what ``python -m`` does) is what would let a
    local file hijack an import the runtime itself depends on.
    """
    _write_module(project, "kan448_pathsafe")
    (project / "queue.py").write_text('raise AssertionError("stdlib queue was shadowed")\n')
    entry = str(project.resolve())
    assert entry not in sys.path

    load_app(["kan448_pathsafe"], project_dir=project)

    assert sys.path[-1] == entry
    assert sys.path.index(entry) == len(sys.path) - 1
    # The stdlib module of the same name still wins.
    import queue

    assert queue.__name__ == "queue"
    assert "site-packages" in str(queue.__file__) or "python3" in str(queue.__file__)


def test_no_modules_means_no_sys_path_change(project: Path) -> None:
    """Nothing to import, nothing to touch: the no-``--app`` boot leaves sys.path alone."""
    before = list(sys.path)

    load_app([], project_dir=project)

    assert sys.path == before
