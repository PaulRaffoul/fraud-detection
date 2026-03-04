"""Tests for feature store — rolling aggregates backed by Redis sorted sets."""

import time

import fakeredis
import pytest
from app.features import (
    FEAT_AMOUNT_VS_AVG_RATIO,
    FEAT_AVG_AMOUNT_24H,
    FEAT_TXN_COUNT_1H,
    FEAT_TXN_COUNT_24H,
    FEATURE_NAMES,
    get_user_features,
    update_user_features,
)


@pytest.fixture()
def redis_client():
    """Fresh fakeredis instance per test."""
    return fakeredis.FakeRedis()


# ---------------------------------------------------------------------------
# Basic feature computation
# ---------------------------------------------------------------------------


class TestGetUserFeatures:
    """Test the read path — computing features from stored transactions."""

    def test_no_history_returns_defaults(self, redis_client):
        """First-ever transaction for a user should return sensible defaults."""
        features = get_user_features(redis_client, "user_0001", current_amount=50.0)

        assert features[FEAT_TXN_COUNT_1H] == 0.0
        assert features[FEAT_TXN_COUNT_24H] == 0.0
        assert features[FEAT_AVG_AMOUNT_24H] == 0.0
        assert features[FEAT_AMOUNT_VS_AVG_RATIO] == 1.0  # neutral

    def test_single_transaction(self, redis_client):
        """After recording one transaction, features should reflect it."""
        now = time.time()

        update_user_features(
            redis_client, "user_0001", "txn_1", amount=100.0, timestamp=now - 60
        )

        features = get_user_features(
            redis_client, "user_0001", current_amount=200.0, timestamp=now
        )

        assert features[FEAT_TXN_COUNT_1H] == 1.0
        assert features[FEAT_TXN_COUNT_24H] == 1.0
        assert features[FEAT_AVG_AMOUNT_24H] == 100.0
        assert features[FEAT_AMOUNT_VS_AVG_RATIO] == 2.0  # 200 / 100

    def test_multiple_transactions_average(self, redis_client):
        """Average should correctly aggregate multiple transactions."""
        now = time.time()

        update_user_features(
            redis_client, "user_0001", "txn_1", amount=100.0, timestamp=now - 120
        )
        update_user_features(
            redis_client, "user_0001", "txn_2", amount=200.0, timestamp=now - 60
        )
        update_user_features(
            redis_client, "user_0001", "txn_3", amount=300.0, timestamp=now - 30
        )

        features = get_user_features(
            redis_client, "user_0001", current_amount=150.0, timestamp=now
        )

        assert features[FEAT_TXN_COUNT_24H] == 3.0
        assert features[FEAT_AVG_AMOUNT_24H] == 200.0  # (100+200+300)/3
        assert features[FEAT_AMOUNT_VS_AVG_RATIO] == 0.75  # 150/200


# ---------------------------------------------------------------------------
# Time window correctness
# ---------------------------------------------------------------------------


class TestTimeWindows:
    """Test that 1h and 24h windows include/exclude correctly."""

    def test_1h_window_excludes_old_transactions(self, redis_client):
        """Transactions older than 1h should not count in txn_count_1h."""
        now = time.time()

        # 2 hours ago — outside 1h window, inside 24h window
        update_user_features(
            redis_client, "user_0001", "txn_old", amount=50.0, timestamp=now - 7200
        )
        # 30 minutes ago — inside 1h window
        update_user_features(
            redis_client, "user_0001", "txn_recent", amount=75.0, timestamp=now - 1800
        )

        features = get_user_features(
            redis_client, "user_0001", current_amount=60.0, timestamp=now
        )

        assert features[FEAT_TXN_COUNT_1H] == 1.0  # only txn_recent
        assert features[FEAT_TXN_COUNT_24H] == 2.0  # both

    def test_24h_window_excludes_expired_transactions(self, redis_client):
        """Transactions older than 24h should be cleaned up on write."""
        now = time.time()

        # 25 hours ago — outside 24h window
        update_user_features(
            redis_client, "user_0001", "txn_expired", amount=100.0, timestamp=now - 90000
        )
        # This write triggers cleanup of expired entries
        update_user_features(
            redis_client, "user_0001", "txn_fresh", amount=50.0, timestamp=now - 60
        )

        features = get_user_features(
            redis_client, "user_0001", current_amount=50.0, timestamp=now
        )

        assert features[FEAT_TXN_COUNT_24H] == 1.0  # only txn_fresh
        assert features[FEAT_AVG_AMOUNT_24H] == 50.0


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Test boundary conditions and unusual inputs."""

    def test_zero_amount_transaction(self, redis_client):
        """A $0 transaction should not break ratio calculation."""
        now = time.time()

        update_user_features(
            redis_client, "user_0001", "txn_1", amount=0.0, timestamp=now - 60
        )

        features = get_user_features(
            redis_client, "user_0001", current_amount=100.0, timestamp=now
        )

        # avg is 0.0, so ratio should fall back to 1.0 (neutral)
        assert features[FEAT_AVG_AMOUNT_24H] == 0.0
        assert features[FEAT_AMOUNT_VS_AVG_RATIO] == 1.0

    def test_different_users_are_isolated(self, redis_client):
        """Features for one user must not leak into another user."""
        now = time.time()

        update_user_features(
            redis_client, "user_0001", "txn_1", amount=500.0, timestamp=now - 60
        )
        update_user_features(
            redis_client, "user_0002", "txn_2", amount=10.0, timestamp=now - 60
        )

        features_1 = get_user_features(
            redis_client, "user_0001", current_amount=100.0, timestamp=now
        )
        features_2 = get_user_features(
            redis_client, "user_0002", current_amount=100.0, timestamp=now
        )

        assert features_1[FEAT_AVG_AMOUNT_24H] == 500.0
        assert features_2[FEAT_AVG_AMOUNT_24H] == 10.0

    def test_feature_names_all_present(self, redis_client):
        """Every call should return all feature names."""
        features = get_user_features(redis_client, "user_0001", current_amount=50.0)
        assert set(features.keys()) == set(FEATURE_NAMES)

    def test_large_spike_ratio(self, redis_client):
        """A massive spike should produce a high ratio."""
        now = time.time()

        # User normally spends ~$10
        for i in range(5):
            update_user_features(
                redis_client, "user_0001", f"txn_{i}", amount=10.0, timestamp=now - 300 + i
            )

        features = get_user_features(
            redis_client, "user_0001", current_amount=5000.0, timestamp=now
        )

        # 5000 / 10 = 500x spike
        assert features[FEAT_AMOUNT_VS_AVG_RATIO] == 500.0
