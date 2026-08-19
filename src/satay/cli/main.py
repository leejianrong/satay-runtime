"""Entry point for the core ``satay`` CLI (argparse, stdlib only).

``satay runs show <id>`` prints a run's timeline as text. It is deliberately **frozen at
the V1 event subset** (ADR-0016 Q50): every event gets a ``seq/type`` line, but only the
workflow/task events get their payloads summarised. Timer, event-wait, cancellation, and
fork events render as bare type lines — Studio covers the rest, and widening this renderer
is out of MVP scope. The one post-V1 event inside the summarised set is ``TaskFailed``,
which is the terminal twin of ``TaskCompleted`` rather than a new kind of durable call, so
leaving it bare stranded a verdict in the middle of a family the renderer already covers
(ADR-0016 refinement, KAN-957). ``satay dev`` is not part of the core CLI: it lives in the
``satay[studio]`` extra, so the core surfaces a clear message pointing at the install.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence

import satay

_STUDIO_HINT = (
    "`satay dev` is provided by the studio extra. Install it with:\n    pip install 'satay[studio]'"
)


def _load_dev_cli() -> Callable[[list[str]], int]:
    """Import the studio ``satay dev`` runner, raising ``ImportError`` if not installed."""
    from satay.devstack.cli import run_dev_cli

    return run_dev_cli


def build_parser() -> argparse.ArgumentParser:
    """Build the core argparse parser."""
    parser = argparse.ArgumentParser(
        prog="satay",
        description="Satay Runtime — local-first durable execution (core CLI).",
    )
    # Read the *derived* version (satay.__init__._detect_version), never a literal: this
    # flag exists to give that value a consumer, because having none is how the hard-coded
    # `0.0.0` in 0.1.0a1 went unnoticed (KAN-447/KAN-459). argparse handles `--version`
    # while scanning optionals, so it prints and exits 0 before the required-subcommand
    # check fires — `satay --version` works with no subcommand, which is how it is typed.
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {satay.__version__}",
        help="Print the installed Satay version and exit.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    runs = subcommands.add_parser("runs", help="Inspect durable runs.")
    runs_sub = runs.add_subparsers(dest="runs_command", required=True)
    show = runs_sub.add_parser("show", help="Print a run's timeline as text.")
    show.add_argument("run_id", help="The run id to display.")
    show.add_argument(
        "--data-dir",
        default=None,
        help="Override the data directory (default: ./.satay).",
    )

    # `dev` is declared so it shows in `satay --help`; its options are owned by the Typer
    # command in the studio extra (ADR-0016), so `main` intercepts the verb before argparse
    # parses it and forwards the remaining args to that command.
    subcommands.add_parser(
        "dev",
        help="(studio extra) Boot the local dev stack; --app MODULE imports your workflows.",
        add_help=False,
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the core CLI. Returns a process exit code."""
    args_list = list(sys.argv[1:] if argv is None else argv)

    # `satay dev` is a Typer command in the studio extra (ADR-0016). Intercept it before
    # argparse so the extra owns its --data-dir/--port options; fall back to a clear
    # install hint when the extra is not present.
    if args_list and args_list[0] == "dev":
        return _dispatch_dev(args_list[1:])

    parser = build_parser()
    args = parser.parse_args(args_list)

    if args.command == "runs" and args.runs_command == "show":
        return _runs_show(args.run_id, args.data_dir)

    parser.error(f"unknown command: {args.command}")
    return 2  # pragma: no cover - parser.error raises SystemExit


def _dispatch_dev(dev_argv: list[str]) -> int:
    """Dispatch ``satay dev`` to the studio Typer command, or print the install hint."""
    try:
        run_dev_cli = _load_dev_cli()
    except ImportError:
        print(_STUDIO_HINT, file=sys.stderr)
        return 2
    return run_dev_cli(dev_argv)


def _runs_show(run_id: str, data_dir: str | None) -> int:
    """Open the store read-only and print a run's text timeline (U1)."""
    import asyncio

    from satay.config import db_path, resolve_data_dir
    from satay.journal.store import SQLiteStore
    from satay.journal.timeline import render_timeline

    path = db_path(resolve_data_dir(data_dir))
    if not path.exists():
        print(f"no satay database at {path}", file=sys.stderr)
        return 1

    async def _load() -> int:
        store = SQLiteStore.open(path)
        try:
            record = await store.get_run(run_id)
            if record is None:
                print(f"run {run_id!r} not found", file=sys.stderr)
                return 1
            events = list(await store.read_events(run_id))
        finally:
            store.close()
        print(render_timeline(events, run_id=run_id))
        return 0

    return asyncio.run(_load())


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
