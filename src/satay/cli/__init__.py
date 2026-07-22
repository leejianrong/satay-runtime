"""Core CLI (U1).

The core ships a **minimal, stdlib-only ``argparse`` CLI** for the read-only
``satay runs show`` (ADR-0016). ``satay dev`` and the Typer command surface live in
the ``satay[studio]`` extra; invoking ``satay dev`` from the core fails with a clear
message naming the install to run.
"""

from __future__ import annotations

from satay.cli.main import main

__all__ = ["main"]
