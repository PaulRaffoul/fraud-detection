"""Predictor service — consumes transactions, enriches with features, runs inference.

Architecture:
    - FastAPI serves health endpoint + Prometheus metrics on main thread
    - Kafka consumer runs in a background thread
    - Each message: parse → update Redis → get features → predict → produce scored result
"""

import json
import logging
import os
import sys
import threading
import time
from contextlib import asynccontextmanager

import redis
import uvicorn
from confluent_kafka import Consumer, KafkaError, KafkaException, Producer
from fastapi import FastAPI
from prometheus_client import Counter, Histogram, make_asgi_app

from .features import FEATURE_NAMES, get_user_features, update_user_features
from .model import load_model, predict

# ---------------------------------------------------------------------------
# Structured JSON logging
# ---------------------------------------------------------------------------


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        log_obj = {
            "service": "predictor",
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "message": record.getMessage(),
        }
        if hasattr(record, "extra_fields"):
            log_obj.update(record.extra_fields)
        return json.dumps(log_obj)


logger = logging.getLogger("predictor")
logger.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(JSONFormatter())
logger.addHandler(handler)

# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------

MESSAGES_CONSUMED = Counter(
    "predictor_messages_consumed_total",
    "Total messages consumed from Kafka",
)
PARSE_ERRORS = Counter(
    "predictor_parse_errors_total",
    "Total messages that failed to parse",
)
INFERENCE_LATENCY = Histogram(
    "predictor_inference_latency_seconds",
    "End-to-end latency for feature enrichment + inference",
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5],
)
PREDICTIONS = Counter(
    "predictor_predictions_total",
    "Total predictions by label",
    ["label"],
)

# ---------------------------------------------------------------------------
# Connection helpers with retry
# ---------------------------------------------------------------------------

_STOP_EVENT = threading.Event()


def connect_redis(max_retries: int = 30) -> redis.Redis | None:
    """Connect to Redis with exponential backoff. Returns None on failure."""
    host = os.getenv("REDIS_HOST", "redis")
    port = int(os.getenv("REDIS_PORT", "6379"))

    for attempt in range(1, max_retries + 1):
        try:
            client = redis.Redis(host=host, port=port, decode_responses=False)
            client.ping()
            logger.info(f"Connected to Redis at {host}:{port}")
            return client
        except redis.ConnectionError as e:
            wait = min(2**attempt, 30)
            logger.warning(
                f"Redis not ready (attempt {attempt}/{max_retries}): {e}. Retrying in {wait}s..."
            )
            time.sleep(wait)

    logger.error("Failed to connect to Redis. Feature enrichment will use defaults.")
    return None


def create_consumer(max_retries: int = 30) -> Consumer:
    """Create a Kafka consumer with retry logic."""
    brokers = os.getenv("KAFKA_BROKERS", "redpanda:9092")
    group_id = os.getenv("KAFKA_CONSUMER_GROUP", "predictor-group")

    conf = {
        "bootstrap.servers": brokers,
        "group.id": group_id,
        "auto.offset.reset": "latest",
        "enable.auto.commit": False,
    }

    for attempt in range(1, max_retries + 1):
        try:
            consumer = Consumer(conf)
            # Test connectivity
            consumer.list_topics(timeout=5)
            logger.info(f"Kafka consumer connected to {brokers}, group={group_id}")
            return consumer
        except KafkaException as e:
            wait = min(2**attempt, 30)
            logger.warning(
                f"Kafka not ready (attempt {attempt}/{max_retries}): {e}. Retrying in {wait}s..."
            )
            time.sleep(wait)

    logger.error("Failed to connect to Kafka after all retries. Exiting.")
    sys.exit(1)


def create_producer(max_retries: int = 30) -> Producer:
    """Create a Kafka producer for scored output."""
    brokers = os.getenv("KAFKA_BROKERS", "redpanda:9092")

    conf = {
        "bootstrap.servers": brokers,
        "client.id": "fraud-predictor",
        "acks": "all",
    }

    for attempt in range(1, max_retries + 1):
        try:
            producer = Producer(conf)
            producer.list_topics(timeout=5)
            logger.info(f"Kafka producer connected to {brokers}")
            return producer
        except KafkaException as e:
            wait = min(2**attempt, 30)
            logger.warning(
                f"Kafka producer not ready (attempt {attempt}/{max_retries}): {e}. "
                f"Retrying in {wait}s..."
            )
            time.sleep(wait)

    logger.error("Failed to create Kafka producer. Exiting.")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Consumer loop (runs in background thread)
# ---------------------------------------------------------------------------


def _process_message(
    raw_value: bytes,
    redis_client: redis.Redis | None,
    model: object,
    kafka_producer: Producer,
    output_topic: str,
) -> None:
    """Parse, enrich, predict, and produce a single message."""
    # 1. Parse
    try:
        txn = json.loads(raw_value)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        PARSE_ERRORS.inc()
        logger.error(f"Failed to parse message: {e}")
        return

    # Validate required fields
    required = ["transaction_id", "user_id", "amount"]
    if not all(k in txn for k in required):
        PARSE_ERRORS.inc()
        logger.error(f"Missing required fields in message: {txn.keys()}")
        return

    txn_id = txn["transaction_id"]
    user_id = txn["user_id"]
    amount = float(txn["amount"])

    # 2. Enrich with Redis features (graceful degradation)
    start = time.perf_counter()

    if redis_client is not None:
        try:
            update_user_features(redis_client, user_id, txn_id, amount)
            features = get_user_features(redis_client, user_id, amount)
        except redis.ConnectionError:
            logger.warning(f"Redis unavailable for txn {txn_id}. Using default features.")
            features = {name: 0.0 for name in FEATURE_NAMES}
    else:
        features = {name: 0.0 for name in FEATURE_NAMES}

    # 3. Predict
    fraud_score = predict(model, features, FEATURE_NAMES)
    label = "fraud" if fraud_score >= 0.5 else "legit"

    elapsed = time.perf_counter() - start
    INFERENCE_LATENCY.observe(elapsed)
    PREDICTIONS.labels(label=label).inc()
    MESSAGES_CONSUMED.inc()

    # 4. Build scored output
    scored = {
        **txn,
        "fraud_score": round(fraud_score, 6),
        "fraud_label": label,
        "features": features,
        "inference_latency_ms": round(elapsed * 1000, 2),
    }

    # 5. Produce to output topic
    try:
        kafka_producer.produce(
            topic=output_topic,
            key=user_id,
            value=json.dumps(scored).encode("utf-8"),
        )
        kafka_producer.poll(0)
    except Exception as e:
        logger.error(f"Failed to produce scored message for {txn_id}: {e}")

    logger.debug(
        f"Scored txn {txn_id}: score={fraud_score:.4f} label={label} latency={elapsed * 1000:.2f}ms"
    )


def consumer_loop(
    redis_client: redis.Redis | None,
    model: object,
) -> None:
    """Background thread: consume → enrich → predict → produce."""
    input_topic = os.getenv("KAFKA_TOPIC_RAW", "transactions_raw")
    output_topic = os.getenv("KAFKA_TOPIC_SCORED", "transactions_scored")

    consumer = create_consumer()
    kafka_producer = create_producer()
    consumer.subscribe([input_topic])

    logger.info(f"Consuming from '{input_topic}', producing to '{output_topic}'")

    try:
        while not _STOP_EVENT.is_set():
            msg = consumer.poll(timeout=1.0)
            if msg is None:
                continue

            if msg.error():
                if msg.error().code() == KafkaError._PARTITION_EOF:
                    continue
                logger.error(f"Consumer error: {msg.error()}")
                continue

            _process_message(
                raw_value=msg.value(),
                redis_client=redis_client,
                model=model,
                kafka_producer=kafka_producer,
                output_topic=output_topic,
            )

            # At-least-once: commit AFTER processing
            consumer.commit(asynchronous=False)

    except Exception as e:
        logger.error(f"Consumer loop crashed: {e}")
    finally:
        kafka_producer.flush(timeout=10)
        consumer.close()
        logger.info("Consumer stopped.")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start consumer thread on startup, stop on shutdown."""
    # Load model
    model = load_model()

    # Connect to Redis
    redis_client = connect_redis()

    # Start consumer in background thread
    _STOP_EVENT.clear()
    thread = threading.Thread(
        target=consumer_loop,
        args=(redis_client, model),
        daemon=True,
        name="kafka-consumer",
    )
    thread.start()
    logger.info("Predictor service started.")

    yield

    # Shutdown
    logger.info("Shutting down predictor...")
    _STOP_EVENT.set()
    thread.join(timeout=10)
    if redis_client:
        redis_client.close()
    logger.info("Predictor stopped.")


app = FastAPI(title="Fraud Predictor", lifespan=lifespan)

# Mount Prometheus metrics at /metrics
metrics_app = make_asgi_app()
app.mount("/metrics", metrics_app)


@app.get("/health")
def health():
    """Health check endpoint."""
    return {"status": "ok", "service": "predictor"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
