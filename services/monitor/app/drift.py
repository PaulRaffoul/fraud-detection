"""Drift detection — pure functions for PSI and categorical distribution shift.

PSI (Population Stability Index) measures how much a numeric distribution
has shifted from a reference baseline. Industry-standard thresholds:
  PSI < 0.1  — no significant drift
  PSI 0.1–0.2 — moderate drift
  PSI > 0.2  — significant drift (alert)

These functions are stateless and testable — they take lists of values
and return drift scores.
"""

import math
from collections import Counter


def compute_psi(
    reference: list[float],
    current: list[float],
    n_bins: int = 10,
) -> float:
    """Compute Population Stability Index between two distributions.

    Args:
        reference: Baseline distribution values.
        current: Recent distribution values.
        n_bins: Number of bins for the histogram.

    Returns:
        PSI score. 0.0 if either distribution is empty.
    """
    if len(reference) < n_bins or len(current) < n_bins:
        return 0.0

    # Build bin edges from the reference distribution
    sorted_ref = sorted(reference)
    bin_edges = []
    for i in range(1, n_bins):
        idx = int(len(sorted_ref) * i / n_bins)
        bin_edges.append(sorted_ref[idx])

    # Count proportions in each bin
    def _bin_proportions(values: list[float]) -> list[float]:
        counts = [0] * n_bins
        for v in values:
            placed = False
            for j, edge in enumerate(bin_edges):
                if v <= edge:
                    counts[j] += 1
                    placed = True
                    break
            if not placed:
                counts[-1] += 1
        total = len(values)
        # Avoid zero proportions with small epsilon
        eps = 1e-6
        return [(c / total) + eps for c in counts]

    ref_props = _bin_proportions(reference)
    cur_props = _bin_proportions(current)

    # PSI = sum((cur - ref) * ln(cur / ref))
    psi = 0.0
    for r, c in zip(ref_props, cur_props):
        psi += (c - r) * math.log(c / r)

    return round(psi, 6)


def compute_categorical_drift(
    reference: list[str],
    current: list[str],
) -> dict[str, float]:
    """Compute per-category proportion shift between two distributions.

    Args:
        reference: Baseline category values.
        current: Recent category values.

    Returns:
        Dict with:
          - per-category absolute proportion differences
          - "max_drift": the largest single-category shift
          - "total_drift": sum of all absolute differences (L1 distance)
    """
    if not reference or not current:
        return {"max_drift": 0.0, "total_drift": 0.0}

    ref_counts = Counter(reference)
    cur_counts = Counter(current)
    ref_total = len(reference)
    cur_total = len(current)

    all_categories = set(ref_counts.keys()) | set(cur_counts.keys())

    result: dict[str, float] = {}
    total_drift = 0.0

    for cat in all_categories:
        ref_prop = ref_counts.get(cat, 0) / ref_total
        cur_prop = cur_counts.get(cat, 0) / cur_total
        diff = abs(cur_prop - ref_prop)
        result[cat] = round(diff, 6)
        total_drift += diff

    result["max_drift"] = round(max(result.values()), 6)
    result["total_drift"] = round(total_drift, 6)

    return result
