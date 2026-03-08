"""Producer service — streams synthetic transactions to Redpanda."""

import json
import logging
import os
import sys
import time

from confluent_kafka import KafkaError, Producer
from prometheus_client import Counter, start_http_server

from .transaction import generate_transaction

# ---------------------------------------------------------------------------
# Structured JSON logging
# ---------------------------------------------------------------------------


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        log_obj = {
            "service": "producer",
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "message": record.getMessage(),
        }
        if hasattr(record, "extra_fields"):
            log_obj.update(record.extra_fields)
        return json.dumps(log_obj)


logger = logging.getLogger("producer")
logger.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(JSONFormatter())
logger.addHandler(handler)

# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------

MESSAGES_PRODUCED = Counter(
    "producer_messages_produced_total",
    "Total messages produced to Kafka",
    ["topic"],
)
PRODUCE_ERRORS = Counter(
    "producer_errors_total",
    "Total produce errors",
)

# ---------------------------------------------------------------------------
# Kafka helpers
# ---------------------------------------------------------------------------


def _delivery_callback(err, msg):
    """Called once per message to indicate delivery result."""
    if err is not None:
        PRODUCE_ERRORS.inc()
        logger.error(f"Delivery failed: {err}")
    else:
        MESSAGES_PRODUCED.labels(topic=msg.topic()).inc()


def create_producer(brokers: str) -> Producer:
    """Create a Kafka producer with retry logic for broker connection."""
    conf = {
        "bootstrap.servers": brokers,
        "client.id": "fraud-producer",
        "acks": "all",
        "retries": 5,
        "retry.backoff.ms": 500,
    }

    max_retries = 30
    for attempt in range(1, max_retries + 1):
        try:
            producer = Producer(conf)
            # Test connectivity by requesting metadata
            metadata = producer.list_topics(timeout=5)
            logger.info(f"Connected to Kafka broker(s): {list(metadata.brokers.values())}")
            return producer
        except KafkaError as e:
            wait = min(2**attempt, 30)
            logger.warning(
                f"Kafka not ready (attempt {attempt}/{max_retries}): {e}. Retrying in {wait}s..."
            )
            time.sleep(wait)

    logger.error("Failed to connect to Kafka after all retries. Exiting.")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def run():
    """Main producer loop."""
    brokers = os.getenv("KAFKA_BROKERS", "redpanda:9092")
    topic = os.getenv("KAFKA_TOPIC_RAW", "transactions_raw")
    rate = int(os.getenv("PRODUCER_RATE_PER_SEC", "20"))
    drift_mode = os.getenv("DRIFT_MODE", "0") == "1"
    interval = 1.0 / rate

    # Start Prometheus metrics server on a background thread
    metrics_port = int(os.getenv("PRODUCER_METRICS_PORT", "8002"))
    start_http_server(metrics_port)
    logger.info(f"Prometheus metrics exposed on :{metrics_port}/metrics")

    producer = create_producer(brokers)

    logger.info(f"Producer started — topic={topic}, rate={rate}/s, drift_mode={drift_mode}")

    try:
        while True:
            txn = generate_transaction(drift_mode=drift_mode)

            producer.produce(
                topic=topic,
                key=txn["user_id"],
                value=json.dumps(txn).encode("utf-8"),
                callback=_delivery_callback,
            )

            # Poll to trigger delivery callbacks
            producer.poll(0)

            logger.debug(
                "Produced: %s amount=%.2f fraud=%s",
                txn["transaction_id"],
                txn["amount"],
                txn["is_fraud"],
            )

            time.sleep(interval)

    except KeyboardInterrupt:
        logger.info("Shutting down producer...")
    finally:
        producer.flush(timeout=10)
        logger.info("Producer flushed and stopped.")


if __name__ == "__main__":
    run()
