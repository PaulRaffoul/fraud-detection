"""Feature store — per-user rolling aggregates backed by Redis sorted sets.

This module is the SINGLE SOURCE OF TRUTH for feature definitions.
The same functions are used at training time (notebook) and inference time (predictor).

Redis data model:
    Key:    user:{user_id}:txns
    Score:  Unix timestamp (float)
    Member: JSON string {"amount": 67.23, "txn_id": "abc-123"}

Rolling windows are computed via ZRANGEBYSCORE on the timestamp score.
"""

import json
import time
from typing import Any

from redis import Redis

# ---------------------------------------------------------------------------
# Feature name constants — use these everywhere to prevent train/serve skew
# ---------------------------------------------------------------------------

FEAT_TXN_COUNT_1H = "txn_count_1h"
FEAT_TXN_COUNT_24H = "txn_count_24h"
FEAT_AVG_AMOUNT_24H = "avg_amount_24h"
FEAT_AMOUNT_VS_AVG_RATIO = "amount_vs_avg_ratio"
FEAT_AMOUNT = "amount"
FEAT_HOUR_OF_DAY = "hour_of_day"
FEAT_DAY_OF_WEEK = "day_of_week"

# Rolling aggregate features computed from Redis
REDIS_FEATURE_NAMES = [
    FEAT_TXN_COUNT_1H,
    FEAT_TXN_COUNT_24H,
    FEAT_AVG_AMOUNT_24H,
    FEAT_AMOUNT_VS_AVG_RATIO,
]

# Full ordered list of feature names fed to the model
# Rolling aggregates (from Redis) + raw transaction fields
FEATURE_NAMES = REDIS_FEATURE_NAMES + [
    FEAT_AMOUNT,
    FEAT_HOUR_OF_DAY,
    FEAT_DAY_OF_WEEK,
]

# ---------------------------------------------------------------------------
# Time windows (seconds)
# ---------------------------------------------------------------------------

WINDOW_1H = 3600
WINDOW_24H = 86400

# TTL for Redis keys — auto-expire after 48h of inactivity
KEY_TTL_SECONDS = 48 * 3600


def _user_key(user_id: str) -> str:
    """Redis key for a user's transaction sorted set."""
    return f"user:{user_id}:txns"


# ---------------------------------------------------------------------------
# Write path — called when a new transaction arrives
# ---------------------------------------------------------------------------


def update_user_features(
    redis_client: Redis,
    user_id: str,
    transaction_id: str,
    amount: float,
    timestamp: float | None = None,
) -> None:
    """Record a transaction in the user's sorted set.

    Args:
        redis_client: Redis connection.
        user_id: The user identifier (e.g. "user_0042").
        transaction_id: Unique transaction ID.
        amount: Transaction amount.
        timestamp: Unix timestamp. Defaults to now.
    """
    ts = timestamp if timestamp is not None else time.time()
    key = _user_key(user_id)

    member = json.dumps({"txn_id": transaction_id, "amount": amount})
    redis_client.zadd(key, {member: ts})

    # Clean up entries older than 24h to bound memory usage
    cutoff = ts - WINDOW_24H
    redis_client.zremrangebyscore(key, "-inf", cutoff)

    # Refresh TTL so inactive users get cleaned up automatically
    redis_client.expire(key, KEY_TTL_SECONDS)


# ---------------------------------------------------------------------------
# Read path — called to build the feature vector for a transaction
# ---------------------------------------------------------------------------


def get_user_features(
    redis_client: Redis,
    user_id: str,
    current_amount: float,
    timestamp: float | None = None,
) -> dict[str, float]:
    """Compute rolling aggregate features for a user.

    Args:
        redis_client: Redis connection.
        user_id: The user identifier.
        current_amount: Amount of the current transaction (for ratio calc).
        timestamp: Unix timestamp. Defaults to now.

    Returns:
        Dict mapping feature names to values.
    """
    ts = timestamp if timestamp is not None else time.time()
    key = _user_key(user_id)

    # Fetch all transactions in the 24h window
    cutoff_24h = ts - WINDOW_24H
    raw_members: list[Any] = redis_client.zrangebyscore(key, cutoff_24h, "+inf", withscores=True)

    # Parse members: list of (json_string, score)
    txns_24h: list[dict[str, Any]] = []
    for member, _score in raw_members:
        if isinstance(member, bytes):
            member = member.decode("utf-8")
        txns_24h.append(json.loads(member))

    # 24h aggregates
    txn_count_24h = len(txns_24h)
    amounts_24h = [t["amount"] for t in txns_24h]
    avg_amount_24h = sum(amounts_24h) / txn_count_24h if txn_count_24h > 0 else 0.0

    # 1h window — filter from the 24h set
    cutoff_1h = ts - WINDOW_1H
    txn_count_1h = sum(1 for _, score in raw_members if score >= cutoff_1h)

    # Ratio: how unusual is this amount compared to user's recent average?
    # First transaction for a user → ratio = 1.0 (neutral)
    if avg_amount_24h > 0:
        amount_vs_avg_ratio = current_amount / avg_amount_24h
    else:
        amount_vs_avg_ratio = 1.0

    return {
        FEAT_TXN_COUNT_1H: float(txn_count_1h),
        FEAT_TXN_COUNT_24H: float(txn_count_24h),
        FEAT_AVG_AMOUNT_24H: round(avg_amount_24h, 4),
        FEAT_AMOUNT_VS_AVG_RATIO: round(amount_vs_avg_ratio, 4),
    }
