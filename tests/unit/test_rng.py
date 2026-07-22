"""Unit tests for the injectable RNG determinism (ADR-0011, Q46)."""

from __future__ import annotations

from satay.testing.rng import Rng, SeededRng, SystemRng


def test_implementations_satisfy_protocol() -> None:
    assert isinstance(SystemRng(), Rng)
    assert isinstance(SeededRng(0), Rng)


def test_same_seed_produces_identical_sequences() -> None:
    a = SeededRng(42)
    b = SeededRng(42)
    seq_a = [a.random() for _ in range(20)]
    seq_b = [b.random() for _ in range(20)]
    assert seq_a == seq_b


def test_different_seeds_differ() -> None:
    a = SeededRng(1)
    b = SeededRng(2)
    seq_a = [a.random() for _ in range(20)]
    seq_b = [b.random() for _ in range(20)]
    assert seq_a != seq_b


def test_uniform_is_reproducible_and_in_range() -> None:
    a = SeededRng(7)
    b = SeededRng(7)
    for _ in range(50):
        x = a.uniform(0.5, 1.5)
        y = b.uniform(0.5, 1.5)
        assert x == y
        assert 0.5 <= x <= 1.5


def test_reset_restores_the_sequence() -> None:
    rng = SeededRng(99)
    first = [rng.random() for _ in range(10)]
    rng.reset()
    second = [rng.random() for _ in range(10)]
    assert first == second
