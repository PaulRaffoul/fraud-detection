# Predictor Service

The predictor is the core inference pipeline. It consumes raw transactions from Kafka, enriches them with per-user features from Redis, runs model inference, and produces scored results back to Kafka.

---

## Architecture

```
                         ┌─────────────────────────────────────────────┐
                         │              Predictor Service              │
                         │                                             │
  transactions_raw ─────>│  Kafka Consumer (background thread)         │
  (Redpanda topic)       │       │                                     │
                         │       v                                     │
                         │  1. Parse JSON message                      │
                         │       │                                     │
                         │       v                                     │
                         │  2. update_user_features() ──> Redis ZADD   │
                         │       │                                     │
                         │       v                                     │
                         │  3. get_user_features()    <── Redis ZRANGE │
                         │       │                                     │
                         │       v                                     │
                         │  4. model.predict()  (or fallback 0.5)      │
                         │       │                                     │
                         │       v                                     │
                         │  5. Kafka Producer ──────────────────────────┼──> transactions_scored
                         │                                             │    (Redpanda topic)
                         │  FastAPI (main thread)                      │
                         │    GET /health                              │
                         │    GET /metrics  (Prometheus)               │
                         └─────────────────────────────────────────────┘
                                  │              │
                              Redis           model.joblib
                          (sorted sets)     (scikit-learn)
```

## Files

| File | Purpose |
|------|---------|
| `app/main.py` | FastAPI app, Kafka consumer loop, message processing pipeline |
| `app/features.py` | Single source of truth for feature definitions. Pure functions for Redis read/write. Shared with the training notebook to prevent train/serve skew |
| `app/model.py` | Model loading from joblib with fallback. If no model file exists, returns neutral score (0.5) |

## Message Processing Flow

Each message from `transactions_raw` goes through 5 steps inside `_process_message()`:

### 1. Parse
Deserialize the JSON payload. If malformed or missing required fields (`transaction_id`, `user_id`, `amount`), the message is **skipped and logged** — never crashes.

### 2. Update Redis (write path)
Calls `update_user_features()` from `features.py`:
- Stores the transaction in a Redis **sorted set** keyed by `user:{user_id}:txns`
- Score = Unix timestamp, Member = `{"txn_id": "...", "amount": 67.23}`
- Cleans up entries older than 24h to bound memory
- Refreshes a 48h TTL on the key

### 3. Get Features (read path)
Calls `get_user_features()` from `features.py`. Computes 4 rolling aggregates from Redis. Then merges 3 raw transaction fields. The full 7-feature vector:

| Feature | Source | Description |
|---------|--------|-------------|
| `txn_count_1h` | Redis | Number of transactions by this user in the last hour |
| `txn_count_24h` | Redis | Number of transactions in the last 24 hours |
| `avg_amount_24h` | Redis | Average transaction amount over 24 hours |
| `amount_vs_avg_ratio` | Redis | Current amount / 24h average (spike detector). Defaults to 1.0 for first-time users |
| `amount` | Transaction | Raw transaction amount |
| `hour_of_day` | Transaction | Hour when the transaction occurred (0-23) |
| `day_of_week` | Transaction | Day of the week (0=Monday, 6=Sunday) |

### 4. Predict
Passes the 7-feature vector (in a fixed order defined by `FEATURE_NAMES`) to the model via `predict()`. The model returns a fraud probability between 0.0 and 1.0. If score >= 0.5, the label is `fraud`; otherwise `legit`.

### 5. Produce Scored Result
Emits the original transaction enriched with `fraud_score`, `fraud_label`, `features`, and `inference_latency_ms` to the `transactions_scored` topic.

Example scored output:
```json
{
  "transaction_id": "817a2887-...",
  "user_id": "user_0106",
  "amount": 732.47,
  "card_type": "Mastercard",
  "merchant_category": "travel",
  "is_fraud": false,
  "fraud_score": 0.740889,
  "fraud_label": "fraud",
  "features": {
    "txn_count_1h": 79.0,
    "txn_count_24h": 440.0,
    "avg_amount_24h": 70.08,
    "amount_vs_avg_ratio": 10.45,
    "amount": 732.47,
    "hour_of_day": 20.0,
    "day_of_week": 4.0
  },
  "inference_latency_ms": 2.56
}
```

## Threading Model

- **Main thread**: FastAPI serves `/health` and `/metrics` via uvicorn
- **Background thread**: Kafka consumer loop runs in a daemon thread started during FastAPI's `lifespan` startup
- A `threading.Event` (`_STOP_EVENT`) coordinates clean shutdown

## Graceful Degradation

The predictor is designed to **never hard-crash** due to a missing dependency:

| Dependency | If unavailable |
|------------|----------------|
| Kafka | Retries with exponential backoff up to 30 attempts, then exits |
| Redis | Logs warning, uses default features (all zeros) for every transaction |
| Model file | Logs warning, returns neutral score 0.5 for every transaction |
| Malformed message | Skips + increments `predictor_parse_errors_total` counter |

## Kafka Semantics

- **Auto-offset reset**: `latest` — only processes new messages, not historical backlog
- **Auto-commit disabled**: Offsets are committed manually **after** processing (at-least-once delivery)
- Consumer group: `predictor-group` (configurable via `KAFKA_CONSUMER_GROUP`)

## Prometheus Metrics

Exposed at `GET /metrics` (port 8000), scraped by Prometheus every 15s.

| Metric | Type | Description |
|--------|------|-------------|
| `predictor_messages_consumed_total` | Counter | Total messages consumed |
| `predictor_parse_errors_total` | Counter | Messages that failed to parse |
| `predictor_inference_latency_seconds` | Histogram | End-to-end latency for feature enrichment + inference |
| `predictor_predictions_total{label=fraud\|legit}` | Counter | Prediction counts by label |

Latency histogram buckets: 5ms, 10ms, 25ms, 50ms, 100ms, 250ms, 500ms. Target is sub-50ms p99.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `KAFKA_BROKERS` | `redpanda:9092` | Kafka bootstrap servers |
| `KAFKA_TOPIC_RAW` | `transactions_raw` | Input topic |
| `KAFKA_TOPIC_SCORED` | `transactions_scored` | Output topic |
| `KAFKA_CONSUMER_GROUP` | `predictor-group` | Consumer group ID |
| `REDIS_HOST` | `redis` | Redis hostname |
| `REDIS_PORT` | `6379` | Redis port |
| `MODEL_PATH` | `/app/model/model.joblib` | Path to the trained model file |
| `LOG_LEVEL` | `INFO` | Logging level |

## Why Feature Consistency Matters

`features.py` is imported by both the predictor (inference) and the training notebook (Phase 5). This means:
- The same feature computation code runs at train time and serve time
- Feature names are defined as constants (`FEAT_TXN_COUNT_1H`, etc.) — no string typos
- `FEATURE_NAMES` defines the exact order the model expects its input vector

This eliminates **train/serve skew**, which is the #1 silent failure mode in production ML systems.
