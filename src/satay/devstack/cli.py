"""The ``satay dev`` Typer command surface (U1) — **satay[studio] only**.

Per ADR-0016 ``satay dev`` is a **Typer** command living in the ``satay[studio]`` extra,
not in the stdlib argparse core. The core ``satay`` CLI dispatches the ``dev`` verb here
when the extra is installed (:func:`run_dev_cli`), and prints an install hint when it is
not (the import of this module fails without Typer/FastAPI/uvicorn).
"""

from __future__ import annotations

import typer

from satay.devstack.orchestrator import DEFAULT_PORT, run_dev

app = typer.Typer(add_completion=False, help="Boot the full local Satay dev stack.")


@app.callback(invoke_without_command=True)
def dev(
    port: int = typer.Option(DEFAULT_PORT, "--port", "-p", help="Loopback port (0 = ephemeral)."),
    data_dir: str | None = typer.Option(
        None, "--data-dir", help="Data directory (default: ./.satay or $SATAY_DATA_DIR)."
    ),
    host: str = typer.Option("127.0.0.1", "--host", help="Loopback host to bind."),
) -> None:
    """Run the worker, SQLite store, control/read API, and Studio in one process."""
    raise typer.Exit(code=run_dev(data_dir=data_dir, host=host, port=port))


def run_dev_cli(argv: list[str]) -> int:
    """Parse the ``dev`` options from ``argv`` and run the stack; return an exit code."""
    try:
        app(args=list(argv), standalone_mode=False)
    except SystemExit as exc:  # pragma: no cover - argparse-style exits (e.g. --help)
        return int(exc.code or 0)
    except typer.Exit as exc:
        return int(exc.exit_code)
    return 0


__all__ = ["app", "run_dev_cli"]
