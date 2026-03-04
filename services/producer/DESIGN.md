# Producer Service — Design & Architecture

## Overview

The producer is a Python service that continuously generates synthetic credit card transactions and publishes them to Redpanda (Kafka-compatible broker) on the `transactions_raw` topic. It supports two modes: **normal** and **drift**.

---

## File Structure

```
services/producer/
├── app/
│   ├── __init__.py
│   ├── main.py           # Runtime loop: connect, generate, publish, log
│   └── transaction.py    # Pure function: generates one synthetic transaction
├── pyproject.toml        # Dependencies (confluent-kafka, prometheus-client)
├── Dockerfile            # Container image built with uv
└── DESIGN.md             # This file
```

---

## transaction.py — The Data Factory

A pure function with zero side effects. Generates one fake transaction at a time.

### Amount Generation — Log-Normal Distribution

```
amount = e^(Normal(mu, sigma))
```

Why log-normal? Real transaction amounts are skewed right — most people buy coffee ($5) and groceries ($40), few buy TVs ($2000). A normal distribution would give negative amounts and equal spread. Log-normal naturally produces:
- A cluster of small values (majority of transactions)
- A long right tail (occasional big purchases)

```
Normal mode:  mu=3.8, sigma=1.0  →  median ~$45, most under $200
Drift mode:   mu=4.8, sigma=1.3  →  median ~$120, heavier tail, more big purchases
```

### Card Type — Weighted Random Choice

```
Normal: Visa 40%, Mastercard 30%, Amex 20%, Discover 10%
Drift:  Visa 20%, Mastercard 20%, Amex 45%, Discover 15%
```

In drift mode, Amex dominates — simulating a scenario where a specific card network is being targeted.

### Fraud Label

```
Normal: 1.2% fraud rate  (realistic — real-world is ~0.1-2%)
Drift:  8.0% fraud rate  (attack scenario)
```

### Other Fields

- `transaction_id`: UUID — globally unique identifier
- `user_id`: Random from a pool of 1000 users (`user_0001` to `user_1000`) — same users repeat, which makes Redis rolling aggregates meaningful later
- `merchant_category`: Uniform random from 5 categories
- `hour_of_day` / `day_of_week`: From current UTC time
- `timestamp`: ISO 8601 format

### Example Output

```json
{
  "transaction_id": "a1b2c3d4-...",
  "user_id": "user_0042",
  "amount": 67.23,
  "card_type": "Visa",
  "merchant_category": "grocery",
  "timestamp": "2026-03-03T14:22:01.123456+00:00",
  "hour_of_day": 14,
  "day_of_week": 1,
  "is_fraud": false
}
```

---

## main.py — The Runtime Engine

Handles 4 responsibilities:

### A) Structured JSON Logging

Every log line is machine-parseable JSON:

```json
{"service": "producer", "timestamp": "2026-03-03 14:22:01", "level": "INFO", "message": "Producer started"}
```

In production with dozens of services, logs are piped to Elasticsearch/Loki. Structured logs allow filtering (`service=producer AND level=ERROR`) without regex parsing.

### B) Kafka Producer with Retry

```python
def create_producer(brokers):
    for attempt in range(1, 31):
        try:
            producer = Producer(conf)
            producer.list_topics(timeout=5)  # test connectivity
            return producer
        except KafkaError:
            wait = min(2**attempt, 30)  # 2s, 4s, 8s, ... 30s cap
            time.sleep(wait)
```

This is the **retry with exponential backoff** pattern. Docker Compose starts all containers roughly in parallel. Even with `depends_on: condition: service_healthy`, there can be race conditions. The producer must never crash because the broker took an extra few seconds.

### C) The Main Loop

```python
while True:
    txn = generate_transaction(drift_mode=drift_mode)      # 1. Generate
    producer.produce(topic, key=user_id, value=json_bytes,  # 2. Publish
                     callback=_delivery_callback)
    producer.poll(0)                                        # 3. Fire callbacks
    time.sleep(interval)                                    # 4. Rate limit
```

Key details:

- **`key=user_id`**: Kafka partitions messages by key. All transactions for the same user go to the same partition — this guarantees ordering per user, which matters when the predictor computes rolling aggregates.
- **`producer.poll(0)`**: confluent-kafka buffers messages internally. `poll(0)` triggers pending delivery callbacks without blocking. This is how we know if a message was actually acknowledged by the broker.
- **`_delivery_callback`**: Fires for every message. On success, increments the Prometheus counter. On failure, increments the error counter and logs.
- **`time.sleep(1/rate)`**: At `PRODUCER_RATE_PER_SEC=20`, this sleeps 50ms between messages.

### D) Prometheus Metrics

```python
start_http_server(8002)  # Spawns a background thread serving /metrics
```

Exposes an HTTP endpoint that Prometheus scrapes every 15 seconds. Two metrics:

| Metric | Type | Description |
|---|---|---|
| `producer_messages_produced_total` | Counter | Total messages successfully delivered |
| `producer_errors_total` | Counter | Total failed deliveries |

---

## How the Pipeline Runs in Practice

### Startup Sequence

```
docker compose up --build -d
```

```
Step 1: Docker builds the producer image
        ┌─────────────────────────────────┐
        │ FROM python:3.11-slim           │
        │ COPY --from=uv image → /bin/uv  │  ← uv binary copied from official image
        │ COPY pyproject.toml             │
        │ RUN uv pip install --system     │  ← installs confluent-kafka, prometheus-client
        │ COPY app/ ./app/                │
        │ CMD python -m app.main          │
        └─────────────────────────────────┘

Step 2: Docker starts infrastructure containers
        redpanda → starts, runs health check (rpk cluster health)
        redis    → starts, runs health check (redis-cli ping)
        mlflow   → starts, runs health check (HTTP /health)

Step 3: Redpanda becomes healthy → Docker starts producer
        (because of depends_on: redpanda: condition: service_healthy)

Step 4: Producer starts
        → Starts Prometheus HTTP server on :8002
        → Connects to redpanda:9092 (retries if needed)
        → Enters main loop
```

### Steady-State Operation

Once running, this happens every 50ms (20 times per second):

```
┌──────────┐  generate_transaction()   ┌──────────────────┐
│ Producer │ ────────────────────────> │ transaction.py    │
│ main.py  │ <──────────────────────── │ returns dict      │
│          │                           └──────────────────┘
│          │  produce(topic, key, val)
│          │ ──────────────────────────────────> ┌──────────┐
│          │                                     │ Redpanda │
│          │  delivery callback (ack)            │ topic:   │
│          │ <────────────────────────────────── │ txns_raw │
│          │                                     └──────────┘
│          │  inc(counter)
│          │ ──────────> Prometheus counter updated in memory
└──────────┘

Every 15 seconds:
┌────────────┐  GET /metrics   ┌──────────┐
│ Prometheus │ ──────────────> │ Producer │
│            │ <────────────── │ :8002    │
│            │  counter values └──────────┘
└────────────┘
```

### Data Flow Through Redpanda

Redpanda stores messages in the `transactions_raw` topic. Each message has:

- **Key**: `user_0042` (used for partitioning)
- **Value**: The full JSON transaction
- **Offset**: Auto-incrementing number per partition

Messages persist in the topic until the retention period expires. Consumers (the predictor, in Phase 4) read from a specific offset and can replay messages if needed.

At 20 messages/second:

- 1,200/minute
- 72,000/hour
- ~1.7 million/day

### Toggling Drift Mode

```bash
# Edit .env
DRIFT_MODE=1

# Restart only the producer (infrastructure stays up)
docker compose up -d producer
```

The producer restarts, reads `DRIFT_MODE=1`, and immediately starts generating from the shifted distributions. Redpanda doesn't care — it's just bytes. But downstream, the monitor service will detect the statistical shift.

### Stopping

```bash
docker compose down           # stops everything
docker compose stop producer  # stops just the producer
```

On `KeyboardInterrupt` or container stop, the `finally` block runs `producer.flush(timeout=10)` — this ensures any buffered messages are delivered before the process exits. Without this, the last few messages could be lost.
