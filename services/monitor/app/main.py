"""Monitor service — detects distribution drift in live transactions.

Architecture:
    - FastAPI serves health endpoint + Prometheus metrics on main thread
    - Kafka consumer runs in a background thread
    - Maintains two sliding windows: reference (baseline) and recent
    - Periodically computes PSI on amount + categorical drift on card_type
    - Exposes drift scores as Prometheus gauges for Grafana alerting
"""

import json
import logging
import os
import sys
import threading
import time
from collections import deque
from contextlib import asynccontextmanager

import uvicorn
from confluent_kafka import Consumer, KafkaError, KafkaException
from fastapi import FastAPI
from prometheus_client import Counter, Gauge, make_asgi_app

from .drift import compute_categorical_drift, compute_psi

# ---------------------------------------------------------------------------
# Structured JSON logging
# ---------------------------------------------------------------------------


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        log_obj = {
            "service": "monitor",
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "message": record.getMessage(),
        }
        if hasattr(record, "extra_fields"):
            log_obj.update(record.extra_fields)
        return json.dumps(log_obj)


logger = logging.getLogger("monitor")
logger.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(JSONFormatter())
logger.addHandler(handler)

# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------

MESSAGES_CONSUMED = Counter(
    "monitor_messages_consumed_total",
    "Total messages consumed by the monitor",
)
PARSE_ERRORS = Counter(
    "monitor_parse_errors_total",
    "Messages that failed to parse",
)

# Drift gauges
DRIFT_PSI_AMOUNT = Gauge(
    "monitor_drift_psi_amount",
    "PSI score for transaction amount distribution",
)
DRIFT_CARD_TYPE_MAX = Gauge(
    "monitor_drift_card_type_max",
    "Max single-category proportion shift for card_type",
)
DRIFT_CARD_TYPE_TOTAL = Gauge(
    "monitor_drift_card_type_total",
    "Total L1 distance for card_type distribution",
)
DRIFT_FRAUD_RATE = Gauge(
    "monitor_drift_fraud_rate",
    "Fraud rate in the recent window",
)

# Per-category card type gauges
DRIFT_CARD_TYPE_CATEGORY = Gauge(
    "monitor_drift_card_type_category",
    "Proportion shift per card type category",
    ["card_type"],
)

# Prediction distribution in recent window
PREDICTION_FRAUD_RATE = Gauge(
    "monitor_prediction_fraud_rate",
    "Proportion of fraud predictions in recent window",
)

_STOP_EVENT = threading.Event()

# ---------------------------------------------------------------------------
# Kafka consumer with retry
# ---------------------------------------------------------------------------


def create_consumer(max_retries: int = 30) -> Consumer:
    """Create a Kafka consumer with retry logic."""
    brokers = os.getenv("KAFKA_BROKERS", "redpanda:9092")
    group_id = os.getenv("KAFKA_CONSUMER_GROUP_MONITOR", "monitor-group")

    conf = {
        "bootstrap.servers": brokers,
        "group.id": group_id,
        "auto.offset.reset": "latest",
        "enable.auto.commit": True,
    }

    for attempt in range(1, max_retries + 1):
        try:
            consumer = Consumer(conf)
            consumer.list_topics(timeout=5)
            logger.info(f"Kafka consumer connected to {brokers}, group={group_id}")
            return consumer
        except KafkaException as e:
            wait = min(2**attempt, 30)
            logger.warning(
                f"Kafka not ready (attempt {attempt}/{max_retries}): {e}. "
                f"Retrying in {wait}s..."
            )
            time.sleep(wait)

    logger.error("Failed to connect to Kafka after all retries. Exiting.")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Drift computation loop
# ---------------------------------------------------------------------------


def consumer_loop() -> None:
    """Background thread: consume scored transactions and compute drift."""
    input_topic = os.getenv("KAFKA_TOPIC_SCORED", "transactions_scored")
    psi_threshold = float(os.getenv("DRIFT_PSI_THRESHOLD", "0.2"))
    compute_interval = int(os.getenv("DRIFT_COMPUTE_INTERVAL_SEC", "30"))

    # Sliding windows: reference (first N messages) and recent
    # Reference fills up first, then stays fixed. Recent is rolling.
    reference_size = int(os.getenv("DRIFT_REFERENCE_SIZE", "2000"))
    recent_max_size = int(os.getenv("DRIFT_RECENT_SIZE", "2000"))

    reference_amounts: list[float] = []
    reference_card_types: list[str] = []
    recent_amounts: deque[float] = deque(maxlen=recent_max_size)
    recent_card_types: deque[str] = deque(maxlen=recent_max_size)
    recent_fraud_labels: deque[str] = deque(maxlen=recent_max_size)

    reference_full = False
    last_compute_time = time.time()

    consumer = create_consumer()
    consumer.subscribe([input_topic])

    logger.info(
        f"Monitor consuming from '{input_topic}', "
        f"reference_size={reference_size}, recent_size={recent_max_size}, "
        f"compute_interval={compute_interval}s, psi_threshold={psi_threshold}"
    )

    try:
        while not _STOP_EVENT.is_set():
            msg = consumer.poll(timeout=1.0)
            if msg is None:
                # Still check if it's time to recompute drift
                _maybe_compute_drift(
                    last_compute_time, compute_interval, psi_threshold,
                    reference_amounts, recent_amounts,
                    reference_card_types, recent_card_types,
                    recent_fraud_labels,
                )
                if time.time() - last_compute_time >= compute_interval:
                    last_compute_time = time.time()
                continue

            if msg.error():
                if msg.error().code() == KafkaError._PARTITION_EOF:
                    continue
                logger.error(f"Consumer error: {msg.error()}")
                continue

            # Parse message
            try:
                txn = json.loads(msg.value())
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                PARSE_ERRORS.inc()
                logger.error(f"Failed to parse message: {e}")
                continue

            amount = txn.get("amount")
            card_type = txn.get("card_type")
            fraud_label = txn.get("fraud_label", "legit")

            if amount is None or card_type is None:
                PARSE_ERRORS.inc()
                continue

            MESSAGES_CONSUMED.inc()

            # Fill reference window first, then switch to recent
            if not reference_full:
                reference_amounts.append(float(amount))
                reference_card_types.append(str(card_type))
                if len(reference_amounts) >= reference_size:
                    reference_full = True
                    logger.info(
                        f"Reference window filled ({reference_size} samples). "
                        f"Now tracking drift in recent window."
                    )
            else:
                recent_amounts.append(float(amount))
                recent_card_types.append(str(card_type))
                recent_fraud_labels.append(str(fraud_label))

            # Periodically compute drift
            now = time.time()
            if now - last_compute_time >= compute_interval and reference_full:
                _compute_and_expose_drift(
                    psi_threshold,
                    reference_amounts, list(recent_amounts),
                    reference_card_types, list(recent_card_types),
                    list(recent_fraud_labels),
                )
                last_compute_time = now

    except Exception as e:
        logger.error(f"Monitor consumer loop crashed: {e}")
    finally:
        consumer.close()
        logger.info("Monitor consumer stopped.")


def _maybe_compute_drift(
    last_compute_time: float,
    compute_interval: int,
    psi_threshold: float,
    reference_amounts: list[float],
    recent_amounts: deque[float],
    reference_card_types: list[str],
    recent_card_types: deque[str],
    recent_fraud_labels: deque[str],
) -> None:
    """Check if it's time to recompute and do so if needed."""
    if time.time() - last_compute_time < compute_interval:
        return
    if not reference_amounts or not recent_amounts:
        return
    _compute_and_expose_drift(
        psi_threshold,
        reference_amounts, list(recent_amounts),
        reference_card_types, list(recent_card_types),
        list(recent_fraud_labels),
    )


def _compute_and_expose_drift(
    psi_threshold: float,
    reference_amounts: list[float],
    recent_amounts: list[float],
    reference_card_types: list[str],
    recent_card_types: list[str],
    recent_fraud_labels: list[str],
) -> None:
    """Compute drift metrics and expose to Prometheus."""
    if len(recent_amounts) < 100:
        return  # Not enough data yet

    # PSI on amount
    psi = compute_psi(reference_amounts, recent_amounts)
    DRIFT_PSI_AMOUNT.set(psi)

    # Categorical drift on card_type
    cat_drift = compute_categorical_drift(reference_card_types, recent_card_types)
    DRIFT_CARD_TYPE_MAX.set(cat_drift.get("max_drift", 0.0))
    DRIFT_CARD_TYPE_TOTAL.set(cat_drift.get("total_drift", 0.0))

    # Per-category gauges
    for key, value in cat_drift.items():
        if key not in ("max_drift", "total_drift"):
            DRIFT_CARD_TYPE_CATEGORY.labels(card_type=key).set(value)

    # Fraud rate in recent window
    if recent_fraud_labels:
        fraud_count = sum(1 for label in recent_fraud_labels if label == "fraud")
        fraud_rate = fraud_count / len(recent_fraud_labels)
        DRIFT_FRAUD_RATE.set(round(fraud_rate, 4))
        PREDICTION_FRAUD_RATE.set(round(fraud_rate, 4))

    # Log drift status
    drift_status = "DRIFT DETECTED" if psi > psi_threshold else "no drift"
    logger.info(
        f"Drift check: PSI={psi:.4f} ({drift_status}), "
        f"card_type_max_shift={cat_drift.get('max_drift', 0):.4f}, "
        f"recent_samples={len(recent_amounts)}"
    )


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start consumer thread on startup, stop on shutdown."""
    _STOP_EVENT.clear()
    thread = threading.Thread(
        target=consumer_loop,
        daemon=True,
        name="monitor-consumer",
    )
    thread.start()
    logger.info("Monitor service started.")

    yield

    logger.info("Shutting down monitor...")
    _STOP_EVENT.set()
    thread.join(timeout=10)
    logger.info("Monitor stopped.")


app = FastAPI(title="Drift Monitor", lifespan=lifespan)

# Mount Prometheus metrics at /metrics
metrics_app = make_asgi_app()
app.mount("/metrics", metrics_app)


@app.get("/health")
def health():
    """Health check endpoint."""
    return {"status": "ok", "service": "monitor"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8001)
