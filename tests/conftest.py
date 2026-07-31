"""Root test configuration.

Loads the first-class testing-seam fixtures (ADR-0011) as a pytest plugin so every
tier can inject the manual clock, seeded RNG, fault injector, temp-store paths, and the
``drain`` helper that drives a run through backoff on the manual clock.

``drain`` used to be defined here. It is now :func:`satay.testing.settle`, shipped from
the plugin, because the ``examples/`` scripts need the same loop and a script cannot use
a pytest fixture (KAN-482).
"""

from __future__ import annotations

pytest_plugins = ["satay.testing.fixtures"]
