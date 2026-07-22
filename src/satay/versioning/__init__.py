"""Code-version stamping and mismatch policy (A10).

Stamps each run with a code version (git commit, then dev string, then source hash;
``dulwich`` was dropped, ADR-0015) and, from V7, enforces the mismatch policy on
resume plus ``effect_safety`` checks. Pure Python.

Scaffold: the stamper API is declared; stamping lands in V1, the policy in V7.
"""

from __future__ import annotations


def stamp_code_version() -> str:
    """Return the current code version for a new run (N17, lands in V1)."""
    raise NotImplementedError("versioning.stamp_code_version lands in V1")
