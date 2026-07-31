"""The ``satay dev`` command surface for ``--app`` (KAN-448).

Covers the option reaching the orchestrator, the exit code actually escaping the Typer
layer, and ``--help`` telling the truth about what the command can run. Needs the studio
extra (Typer); skips cleanly without it.
"""

from __future__ import annotations

import pytest

pytest.importorskip("typer")

from satay.cli.main import main
from satay.devstack import cli as devstack_cli


@pytest.fixture
def captured_run_dev(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Replace the orchestrator entry point, recording the kwargs the command passes."""
    captured: dict[str, object] = {}

    def _fake_run_dev(**kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(devstack_cli, "run_dev", _fake_run_dev)
    return captured


def test_repeated_app_options_reach_the_orchestrator(captured_run_dev: dict[str, object]) -> None:
    code = main(["dev", "--port", "0", "--app", "mypkg.workflows", "--app", "mypkg.tasks"])

    assert code == 0
    assert captured_run_dev["app_modules"] == ["mypkg.workflows", "mypkg.tasks"]


def test_the_short_flag_works_too(captured_run_dev: dict[str, object]) -> None:
    assert main(["dev", "--port", "0", "-A", "mypkg.workflows"]) == 0
    assert captured_run_dev["app_modules"] == ["mypkg.workflows"]


def test_no_app_option_passes_an_empty_list(captured_run_dev: dict[str, object]) -> None:
    """No ``--app`` still boots — the orchestrator then falls back to the config file."""
    assert main(["dev", "--port", "0"]) == 0
    assert captured_run_dev["app_modules"] == []


def test_a_failing_boot_propagates_its_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """click *returns* a typer.Exit code under standalone_mode=False; honour it.

    Ignoring that return value made every ``satay dev`` failure — a locked data dir, and
    now an unimportable ``--app`` — exit 0 despite printing an error.
    """
    monkeypatch.setattr(devstack_cli, "run_dev", lambda **_: 2)

    assert main(["dev", "--app", "nope"]) == 2


def test_help_documents_app_and_the_empty_registry() -> None:
    """``--help`` must stop implying the stack runs your code without ``--app``.

    Asserts against the declared command rather than the rendered page: Typer draws help
    with rich, whose ANSI styling and column wrapping depend on the terminal it thinks it
    has, so scraping the rendered text passes locally and fails in CI.
    """
    import typer.main

    command = typer.main.get_command(devstack_cli.app)
    option = next(p for p in command.params if "--app" in p.opts)

    assert option.opts == ["--app", "-A"]
    assert option.multiple
    help_text = str(getattr(option, "help", ""))
    assert "the registry is empty" in help_text
    assert "cannot start a run or wake one parked" in help_text
    assert "pyproject.toml" in help_text
    # And the command's own summary leads with what --app is for.
    assert "--app" in str(devstack_cli.app.info.help)


def test_asking_for_help_exits_zero_without_booting(capsys: pytest.CaptureFixture[str]) -> None:
    assert devstack_cli.run_dev_cli(["--help"]) == 0
    assert "satay dev" in capsys.readouterr().out
