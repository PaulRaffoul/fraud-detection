"""Tests for drift detection functions (PSI + categorical drift)."""

import random

import pytest
from app.drift import compute_categorical_drift, compute_psi

# ---------------------------------------------------------------------------
# PSI tests
# ---------------------------------------------------------------------------


class TestComputePSI:
    """Test Population Stability Index computation."""

    def test_identical_distributions_near_zero(self):
        """Same distribution should produce PSI close to 0."""
        data = [random.gauss(100, 20) for _ in range(1000)]
        psi = compute_psi(data, data)
        assert psi < 0.01

    def test_similar_distributions_low_psi(self):
        """Two samples from the same distribution should have low PSI."""
        random.seed(42)
        ref = [random.gauss(100, 20) for _ in range(2000)]
        cur = [random.gauss(100, 20) for _ in range(2000)]
        psi = compute_psi(ref, cur)
        assert psi < 0.1  # No significant drift

    def test_shifted_distribution_high_psi(self):
        """A shifted distribution should produce PSI > 0.2."""
        random.seed(42)
        ref = [random.gauss(100, 20) for _ in range(2000)]
        cur = [random.gauss(200, 40) for _ in range(2000)]  # Shifted mean + wider
        psi = compute_psi(ref, cur)
        assert psi > 0.2  # Significant drift

    def test_empty_reference_returns_zero(self):
        """Empty reference should return 0.0."""
        psi = compute_psi([], [1.0, 2.0, 3.0])
        assert psi == 0.0

    def test_empty_current_returns_zero(self):
        """Empty current should return 0.0."""
        psi = compute_psi([1.0, 2.0, 3.0], [])
        assert psi == 0.0

    def test_too_few_samples_returns_zero(self):
        """Fewer samples than bins should return 0.0."""
        psi = compute_psi([1.0] * 5, [2.0] * 5, n_bins=10)
        assert psi == 0.0

    def test_psi_is_non_negative(self):
        """PSI should always be non-negative."""
        random.seed(123)
        ref = [random.gauss(50, 10) for _ in range(500)]
        cur = [random.gauss(55, 15) for _ in range(500)]
        psi = compute_psi(ref, cur)
        assert psi >= 0.0

    def test_custom_bins(self):
        """PSI should work with custom bin count."""
        random.seed(42)
        ref = [random.gauss(100, 20) for _ in range(500)]
        cur = [random.gauss(100, 20) for _ in range(500)]
        psi = compute_psi(ref, cur, n_bins=5)
        assert psi < 0.1


# ---------------------------------------------------------------------------
# Categorical drift tests
# ---------------------------------------------------------------------------


class TestComputeCategoricalDrift:
    """Test categorical distribution shift detection."""

    def test_identical_distributions(self):
        """Same distribution should produce zero drift."""
        data = ["Visa"] * 40 + ["Mastercard"] * 30 + ["Amex"] * 20 + ["Discover"] * 10
        result = compute_categorical_drift(data, data)
        assert result["max_drift"] == 0.0
        assert result["total_drift"] == 0.0

    def test_shifted_distribution(self):
        """Changed proportions should produce non-zero drift."""
        ref = ["Visa"] * 40 + ["Mastercard"] * 30 + ["Amex"] * 20 + ["Discover"] * 10
        cur = ["Visa"] * 20 + ["Mastercard"] * 20 + ["Amex"] * 40 + ["Discover"] * 20
        result = compute_categorical_drift(ref, cur)
        assert result["max_drift"] > 0.0
        assert result["total_drift"] > 0.0
        # Amex shifted from 20% to 40%, so its drift should be ~0.2
        assert result["Amex"] == pytest.approx(0.2, abs=0.01)

    def test_new_category_in_current(self):
        """New category in current should appear in results."""
        ref = ["Visa"] * 50 + ["Mastercard"] * 50
        cur = ["Visa"] * 40 + ["Mastercard"] * 40 + ["Amex"] * 20
        result = compute_categorical_drift(ref, cur)
        assert "Amex" in result
        assert result["Amex"] == pytest.approx(0.2, abs=0.01)

    def test_missing_category_in_current(self):
        """Category missing from current should show its full ref proportion as drift."""
        ref = ["Visa"] * 50 + ["Mastercard"] * 50
        cur = ["Visa"] * 100
        result = compute_categorical_drift(ref, cur)
        assert result["Mastercard"] == pytest.approx(0.5, abs=0.01)

    def test_empty_reference(self):
        """Empty reference should return zero drift."""
        result = compute_categorical_drift([], ["Visa", "Mastercard"])
        assert result == {"max_drift": 0.0, "total_drift": 0.0}

    def test_empty_current(self):
        """Empty current should return zero drift."""
        result = compute_categorical_drift(["Visa", "Mastercard"], [])
        assert result == {"max_drift": 0.0, "total_drift": 0.0}

    def test_per_category_values_are_absolute(self):
        """Per-category drift values should be absolute differences."""
        ref = ["A"] * 70 + ["B"] * 30
        cur = ["A"] * 30 + ["B"] * 70
        result = compute_categorical_drift(ref, cur)
        assert result["A"] == pytest.approx(0.4, abs=0.01)
        assert result["B"] == pytest.approx(0.4, abs=0.01)
        assert result["total_drift"] == pytest.approx(0.8, abs=0.02)
