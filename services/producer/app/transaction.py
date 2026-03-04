"""Synthetic transaction generator with normal and drift modes."""

import math
import random
import uuid
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Distribution configs
# ---------------------------------------------------------------------------

CARD_TYPES_NORMAL = {"Visa": 0.40, "Mastercard": 0.30, "Amex": 0.20, "Discover": 0.10}
CARD_TYPES_DRIFT = {"Visa": 0.20, "Mastercard": 0.20, "Amex": 0.45, "Discover": 0.15}

MERCHANT_CATEGORIES = ["grocery", "electronics", "gas", "travel", "restaurant"]

# Log-normal parameters: amount = exp(Normal(mu, sigma))
# Normal mode: median ~$45, most under $200
AMOUNT_MU_NORMAL = 3.8
AMOUNT_SIGMA_NORMAL = 1.0

# Drift mode: median ~$120, heavier tail
AMOUNT_MU_DRIFT = 4.8
AMOUNT_SIGMA_DRIFT = 1.3

FRAUD_RATE_NORMAL = 0.012  # ~1.2%
FRAUD_RATE_DRIFT = 0.08  # ~8%

USER_POOL_SIZE = 1000


def _weighted_choice(weights: dict[str, float]) -> str:
    """Pick a key from a {key: probability} dict."""
    keys = list(weights.keys())
    vals = list(weights.values())
    return random.choices(keys, weights=vals, k=1)[0]


def generate_transaction(drift_mode: bool = False) -> dict:
    """Generate a single synthetic transaction.

    Args:
        drift_mode: If True, shift distributions to simulate data drift.

    Returns:
        Dictionary with transaction fields.
    """
    now = datetime.now(timezone.utc)

    # Amount
    if drift_mode:
        mu, sigma = AMOUNT_MU_DRIFT, AMOUNT_SIGMA_DRIFT
    else:
        mu, sigma = AMOUNT_MU_NORMAL, AMOUNT_SIGMA_NORMAL
    amount = round(math.exp(random.gauss(mu, sigma)), 2)
    # Cap at a reasonable max
    amount = min(amount, 15_000.0)

    # Card type
    card_weights = CARD_TYPES_DRIFT if drift_mode else CARD_TYPES_NORMAL
    card_type = _weighted_choice(card_weights)

    # Fraud label
    fraud_rate = FRAUD_RATE_DRIFT if drift_mode else FRAUD_RATE_NORMAL
    is_fraud = random.random() < fraud_rate

    return {
        "transaction_id": str(uuid.uuid4()),
        "user_id": f"user_{random.randint(1, USER_POOL_SIZE):04d}",
        "amount": amount,
        "card_type": card_type,
        "merchant_category": random.choice(MERCHANT_CATEGORIES),
        "timestamp": now.isoformat(),
        "hour_of_day": now.hour,
        "day_of_week": now.weekday(),
        "is_fraud": is_fraud,
    }
