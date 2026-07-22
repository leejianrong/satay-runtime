"""Root test configuration.

Loads the first-class testing-seam fixtures (ADR-0011) as a pytest plugin so every
tier can inject the manual clock, seeded RNG, fault injector, and temp-store paths.
"""

pytest_plugins = ["satay.testing.fixtures"]
