"""Model loader — loads a joblib model with fallback if missing.

If the model file doesn't exist, the predictor returns a neutral score (0.5)
rather than crashing. This allows the service to start and process messages
while waiting for a trained model to be deployed.
"""

import logging
import os
from typing import Any

import joblib
import numpy as np

logger = logging.getLogger("predictor")

# Sentinel used when no model is available
_NO_MODEL = None


def load_model(model_path: str | None = None) -> Any:
    """Load a scikit-learn model from disk.

    Args:
        model_path: Path to the joblib file.
                    Defaults to MODEL_PATH env var or /app/model/model.joblib.

    Returns:
        The loaded model, or None if the file doesn't exist.
    """
    path = model_path or os.getenv("MODEL_PATH", "/app/model/model.joblib")

    if not os.path.exists(path):
        logger.warning(f"Model file not found at {path}. Using fallback score 0.5.")
        return _NO_MODEL

    try:
        model = joblib.load(path)
        logger.info(f"Model loaded from {path}")
        return model
    except Exception as e:
        logger.error(f"Failed to load model from {path}: {e}. Using fallback score 0.5.")
        return _NO_MODEL


def predict(model: Any, features: dict[str, float], feature_names: list[str]) -> float:
    """Run inference on a single transaction.

    Args:
        model: A loaded sklearn model (with predict_proba), or None.
        features: Dict of feature name → value.
        feature_names: Ordered list of feature names the model expects.

    Returns:
        Fraud probability (0.0–1.0). Returns 0.5 if no model is loaded.
    """
    if model is _NO_MODEL:
        return 0.5

    # Build feature vector in the exact order the model was trained on
    X = np.array([[features.get(name, 0.0) for name in feature_names]])

    try:
        # predict_proba returns [[p_legit, p_fraud]]
        proba = model.predict_proba(X)
        return float(proba[0][1])
    except Exception as e:
        logger.error(f"Inference failed: {e}. Returning fallback score 0.5.")
        return 0.5
