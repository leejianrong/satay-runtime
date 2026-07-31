"""The ``satay dev`` Typer command surface (U1) — **satay[studio] only**.

Per ADR-0016 ``satay dev`` is a **Typer** command living in the ``satay[studio]`` extra,
not in the stdlib argparse core. The core ``satay`` CLI dispatches the ``dev`` verb here
when the extra is installed (:func:`run_dev_cli`), and prints an install hint when it is
not (the import of this module fails without Typer/FastAPI/uvicorn).

``--app`` is the option that makes the command run *your* code: it names the modules
whose ``@satay.workflow`` / ``@satay.task`` decorators must execute before the worker
starts (KAN-448). Without it the stack still boots — it just has an empty registry, and
says so.
"""

from __future__ import annotations

from typing import Annotated

import typer

from satay.devstack.orchestrator import DEFAULT_PORT, run_dev

app = typer.Typer(
    add_completion=False,
    help=(
        "Boot the full local Satay dev stack: worker + SQLite store + control/read API "
        "+ Studio, in one process. Pass --app to import your workflow modules so the "
        "worker can actually run them."
    ),
)

_APP_HELP = (
    "Import this module before starting, registering its @satay.workflow / @satay.task "
    "definitions. Repeatable. Without it the registry is empty: the stack serves Studio "
    "and the journal but cannot start a run or wake one parked on a timer or event. "
    "Defaults to the tool.satay app key in pyproject.toml."
)


@app.callback(invoke_without_command=True)
def dev(
    port: Annotated[
        int, typer.Option("--port", "-p", help="Loopback port (0 = ephemeral).")
    ] = DEFAULT_PORT,
    data_dir: Annotated[
        str | None,
        typer.Option("--data-dir", help="Data directory (default: ./.satay or $SATAY_DATA_DIR)."),
    ] = None,
    host: Annotated[str, typer.Option("--host", help="Loopback host to bind.")] = "127.0.0.1",
    app_modules: Annotated[
        list[str] | None, typer.Option("--app", "-A", metavar="MODULE", help=_APP_HELP)
    ] = None,
) -> None:
    """Run the worker, SQLite store, control/read API, and Studio in one process.

    The stack can only run workflows this process has imported. Name them with
    ``--app mypkg.workflows`` (repeatable), or list them once under ``[tool.satay]
    app`` in ``pyproject.toml``; the boot then prints how many workflows and tasks were
    registered. With neither, the registry is empty and nothing of yours will fire.
    """
    raise typer.Exit(
        code=run_dev(
            data_dir=data_dir,
            host=host,
            port=port,
            app_modules=list(app_modules or []),
        )
    )


def run_dev_cli(argv: list[str]) -> int:
    """Parse the ``dev`` options from ``argv`` and run the stack; return an exit code.

    Under ``standalone_mode=False`` click *returns* a ``typer.Exit``'s code instead of
    raising it, so the return value — not just the ``except`` arm — has to be honoured;
    ignoring it made every ``satay dev`` failure (a locked data dir, and now a bad
    ``--app``) exit 0.
    """
    try:
        code = app(args=list(argv), prog_name="satay dev", standalone_mode=False)
    except SystemExit as exc:  # pragma: no cover - argparse-style exits (e.g. --help)
        return int(exc.code or 0)
    except typer.Exit as exc:  # pragma: no cover - click returns these, see the docstring
        return int(exc.exit_code)
    return int(code) if isinstance(code, int) else 0


__all__ = ["app", "run_dev_cli"]
