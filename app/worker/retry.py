"""Backoff exponencial con jitter ("equal jitter"): entre la mitad y el total de la espera calculada."""

from __future__ import annotations

import random


def backoff_seconds(attempt: int, base: int, cap: int, rng: random.Random | None = None) -> float:
    if attempt < 1:
        raise ValueError("attempt must be >= 1")
    generator = rng or random.Random()
    ceiling = min(float(cap), float(base) * (2 ** (attempt - 1)))
    return ceiling / 2 + generator.uniform(0, ceiling / 2)
