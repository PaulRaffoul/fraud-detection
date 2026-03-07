"""Synthetic transaction generator with normal and drift modes.

Fraud probability is feature-dependent — it increases with:
  - High transaction amounts (> $200)
  - Late-night hours (0–5 AM)
  - Risky merchant categories (electronics, travel)
This gives the model real signal to learn from while keeping the overall
fraud rate realistic (~1.2% normal, ~8% drift).
"""

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

# Base fraud rates (before risk multipliers)
FRAUD_RATE_BASE_NORMAL = 0.003
FRAUD_RATE_BASE_DRIFT = 0.02

USER_POOL_SIZE = 1000


def _weighted_choice(weights: dict[str, float]) -> str:
    """Pick a key from a {key: probability} dict."""
    keys = list(weights.keys())
    vals = list(weights.values())
    return random.choices(keys, weights=vals, k=1)[0]


def _fraud_probability(
    amount: float,
    hour: int,
    merchant_category: str,
    base_rate: float,
) -> float:
    """Compute fraud probability based on transaction risk factors.

    Risk multipliers stack multiplicatively on the base rate:
      - Amount > $500: 6x, $200-$500: 3x
      - Late night (0-5 AM): 4x
      - Risky merchants (electronics, travel): 3x

    The result is clamped to [0, 1].
    """
    risk = base_rate

    # High amounts are more suspicious
    if amount > 500:
        risk *= 6.0
    elif amount > 200:
        risk *= 3.0

    # Late-night transactions are riskier
    if 0 <= hour <= 5:
        risk *= 4.0

    # Certain merchant categories are riskier
    if merchant_category in ("electronics", "travel"):
        risk *= 3.0

    return min(risk, 1.0)


def generate_transaction(drift_mode: bool = False) -> dict:
    """Generate a single synthetic transaction.

    Fraud is correlated with amount, hour, and merchant category so
    that ML models can learn meaningful patterns.

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

    # Merchant category
    merchant_category = random.choice(MERCHANT_CATEGORIES)

    # Fraud label — probability depends on transaction risk factors
    base_rate = FRAUD_RATE_BASE_DRIFT if drift_mode else FRAUD_RATE_BASE_NORMAL
    fraud_prob = _fraud_probability(amount, now.hour, merchant_category, base_rate)
    is_fraud = random.random() < fraud_prob

    return {
        "transaction_id": str(uuid.uuid4()),
        "user_id": f"user_{random.randint(1, USER_POOL_SIZE):04d}",
        "amount": amount,
        "card_type": card_type,
        "merchant_category": merchant_category,
        "timestamp": now.isoformat(),
        "hour_of_day": now.hour,
        "day_of_week": now.weekday(),
        "is_fraud": is_fraud,
    }
