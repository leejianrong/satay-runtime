"""E2E-tier smoke test.

The primary seam (public API + temp SQLite store + injected clock/RNG/faults) is
exercised for real starting in V1. For Epic 0 this asserts only that the seam's
determinism controls are wired and injectable through the shared fixtures, so the tier
is green and ready to grow.
"""

from __future__ import annotations

from satay.testing import FaultInjector, ManualClock, SeededRng


def test_seam_controls_are_injectable(
    manual_clock: ManualClock,
    seeded_rng: SeededRng,
    fault_injector: FaultInjector,
    temp_db_path: object,
) -> None:
    assert manual_clock.monotonic() == 0.0
    assert isinstance(seeded_rng.random(), float)
    fault_injector.crash_after("TaskCompleted")
