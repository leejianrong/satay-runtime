"""Entry point for the core ``satay`` CLI (argparse, stdlib only).

``satay runs show <id>`` prints a run's timeline as text (behaviour lands in V1;
frozen at the V1 event subset per ADR-0016 Q50). ``satay dev`` is not part of the
core CLI: it lives in the ``satay[studio]`` extra, so the core surfaces a clear
message pointing at the install.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

_STUDIO_HINT = (
    "`satay dev` is provided by the studio extra. Install it with:\n    pip install 'satay[studio]'"
)


def build_parser() -> argparse.ArgumentParser:
    """Build the core argparse parser."""
    parser = argparse.ArgumentParser(
        prog="satay",
        description="Satay Runtime — local-first durable execution (core CLI).",
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

    # `dev` is declared only so the core can emit a helpful error; the real command
    # lives in the studio extra (ADR-0016).
    subcommands.add_parser("dev", help="(studio extra) Boot the full local dev stack.")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the core CLI. Returns a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "dev":
        print(_STUDIO_HINT, file=sys.stderr)
        return 2

    if args.command == "runs" and args.runs_command == "show":
        # Rendering the timeline requires the journal, which lands in V1.
        raise NotImplementedError("`satay runs show` lands in V1")

    parser.error(f"unknown command: {args.command}")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
