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

``satay runs delete`` and ``satay gc`` (ADR-0037/0039) are the runtime's first
**destructive** verbs, deliberately CLI-only with no importable Python equivalent
(ADR-0039 Decision 2). ``gc`` is dry-run by default, printing what it would reclaim;
``--apply`` is required to actually delete blob files.
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
    delete = runs_sub.add_parser(
        "delete", help="Delete one terminal run's journal rows (ADR-0037/0039)."
    )
    delete.add_argument("run_id", help="The run id to delete.")
    delete.add_argument(
        "--data-dir",
        default=None,
        help="Override the data directory (default: ./.satay).",
    )

    gc = subcommands.add_parser(
        "gc", help="Sweep blob files no run's journal references (ADR-0037/0039)."
    )
    gc.add_argument(
        "--data-dir",
        default=None,
        help="Override the data directory (default: ./.satay).",
    )
    gc.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete reclaimable blobs (default: dry run, reports only).",
    )
    gc.add_argument(
        "--grace-period-seconds",
        type=float,
        default=None,
        help="Protect blobs younger than this many seconds (default: 300).",
    )

    # `satay eval BASELINE CANDIDATE` gates one run against another on output *and* cost
    # (ADR-0041). Read-only and stdlib-only by design: it compares two runs that already
    # exist, so it never needs the workflows imported — producing the candidate (a fork and
    # replay under a change) is `satay.replay_eval`'s job, run from the caller's own harness
    # where the code under test is in scope. Exits non-zero when the candidate regresses.
    ev = subcommands.add_parser(
        "eval",
        help="Gate a candidate run against a baseline on output and cost (ADR-0041).",
    )
    ev.add_argument("baseline_run_id", help="The recorded baseline run id.")
    ev.add_argument("candidate_run_id", help="The candidate run id to gate against it.")
    ev.add_argument(
        "--data-dir",
        default=None,
        help="Override the data directory (default: ./.satay).",
    )
    ev.add_argument(
        "--expect-output",
        choices=("unchanged", "changed", "any"),
        default="unchanged",
        help=(
            "unchanged (default): a changed or unconfirmable output is a regression. "
            "changed: an unchanged output is a regression. any: do not gate output."
        ),
    )
    ev.add_argument(
        "--max-cost-increase",
        type=float,
        default=None,
        metavar="N",
        help="Fail if a usage metric rises by more than N (default: cost is not gated).",
    )
    ev.add_argument(
        "--cost-key",
        default=None,
        metavar="KEY",
        help=(
            "Restrict --max-cost-increase to one usage key (e.g. usd, output_tokens); "
            "without it the cap applies to every key."
        ),
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
    if args.command == "runs" and args.runs_command == "delete":
        return _runs_delete(args.run_id, args.data_dir)
    if args.command == "gc":
        return _gc(args.data_dir, apply=args.apply, grace_period_seconds=args.grace_period_seconds)
    if args.command == "eval":
        return _eval(
            args.baseline_run_id,
            args.candidate_run_id,
            args.data_dir,
            expect_output=args.expect_output,
            max_cost_increase=args.max_cost_increase,
            cost_key=args.cost_key,
        )

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


def _runs_delete(run_id: str, data_dir: str | None) -> int:
    """Delete one terminal run's rows; does not touch blobs (ADR-0037/0039)."""
    import asyncio

    from satay.config import db_path, resolve_data_dir
    from satay.journal.store import SQLiteStore

    path = db_path(resolve_data_dir(data_dir))
    if not path.exists():
        print(f"no satay database at {path}", file=sys.stderr)
        return 1

    async def _delete() -> int:
        store = SQLiteStore.open(path)
        try:
            await store.delete_run(run_id)
        except LookupError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        finally:
            store.close()
        print(f"deleted run {run_id!r}")
        return 0

    return asyncio.run(_delete())


def _gc(data_dir: str | None, *, apply: bool, grace_period_seconds: float | None) -> int:
    """Dry-run (default) or apply a blob-GC mark-and-sweep pass (ADR-0037/0039)."""
    import asyncio

    from satay.blobs import BlobStore
    from satay.blobs.gc import DEFAULT_GRACE_PERIOD_SECONDS, collect_garbage
    from satay.config import blob_dir, db_path, resolve_data_dir
    from satay.journal.store import SQLiteStore

    resolved_data_dir = resolve_data_dir(data_dir)
    path = db_path(resolved_data_dir)
    if not path.exists():
        print(f"no satay database at {path}", file=sys.stderr)
        return 1
    grace = DEFAULT_GRACE_PERIOD_SECONDS if grace_period_seconds is None else grace_period_seconds

    async def _run() -> int:
        store = SQLiteStore.open(path)
        try:
            blobs = BlobStore(blob_dir(resolved_data_dir))
            report = await collect_garbage(store, blobs, apply=apply, grace_period_seconds=grace)
        finally:
            store.close()
        reclaimed_mb = report.reclaimable_bytes / (1024 * 1024)
        kept_mb = report.kept_bytes / (1024 * 1024)
        kept_count = report.referenced_count + len(report.protected_ids)
        if apply:
            print(f"reclaimed {len(report.reclaimable_ids)} blobs, {reclaimed_mb:.1f} MB")
        else:
            print(
                f"would reclaim {len(report.reclaimable_ids)} blobs, {reclaimed_mb:.1f} MB "
                f"({kept_count} blobs, {kept_mb:.1f} MB still referenced or in the grace period)"
            )
            print("re-run with --apply to delete them")
        return 0

    return asyncio.run(_run())


def _eval(
    baseline_run_id: str,
    candidate_run_id: str,
    data_dir: str | None,
    *,
    expect_output: str,
    max_cost_increase: float | None,
    cost_key: str | None,
) -> int:
    """Compare two recorded runs and gate; exit non-zero on a regression (ADR-0041)."""
    import asyncio

    from satay import compare_runs, gate
    from satay.config import db_path, resolve_data_dir
    from satay.journal.store import SQLiteStore

    path = db_path(resolve_data_dir(data_dir))
    if not path.exists():
        print(f"no satay database at {path}", file=sys.stderr)
        return 1

    # A --cost-key scopes the cap to that one metric; otherwise the scalar cap applies to
    # every key. `None` (the default) leaves cost ungated entirely.
    if max_cost_increase is None:
        cap: float | dict[str, float] | None = None
    elif cost_key is not None:
        cap = {cost_key: max_cost_increase}
    else:
        cap = max_cost_increase

    async def _run() -> int:
        store = SQLiteStore.open(path)
        try:
            report = await compare_runs(baseline_run_id, candidate_run_id, store=store)
        except LookupError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        finally:
            store.close()

        print(f"baseline  {report.baseline_run_id} ({report.baseline_status})")
        print(f"candidate {report.candidate_run_id} ({report.candidate_status})")
        print()
        if report.output.changed:
            where = ", ".join(report.output.paths) or "."
            print(f"output: changed at {where}")
        elif report.output.redacted:
            print("output: equality unknown (redacted in the journal)")
        else:
            print("output: unchanged")

        if report.usage_delta:
            print("usage delta:")
            width = max(len(key) for key in report.usage_delta)
            for key, delta in sorted(report.usage_delta.items()):
                print(f"  {key.ljust(width)}  {_signed(delta)}")
        else:
            print("usage delta: (none recorded)")

        verdict = gate(report, expect_output=expect_output, max_usage_increase=cap)
        print()
        if verdict.passed:
            print("GATE: PASS")
            return 0
        print("GATE: FAIL")
        for reason in verdict.reasons:
            print(f"  - {reason}")
        return 1

    return asyncio.run(_run())


def _signed(delta: float) -> str:
    """Format a usage delta with an explicit sign, ints as ints and floats compactly."""
    if isinstance(delta, int):
        return f"{delta:+d}"
    return f"{delta:+g}"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
