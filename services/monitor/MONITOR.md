# Monitor Service — Drift Detection

The monitor service detects distribution drift in live transactions by comparing a **reference baseline** against a **recent rolling window** of scored transactions. It runs as a standalone FastAPI service that consumes from Kafka, computes drift metrics, and exposes them to Prometheus for alerting and Grafana dashboards.

---

## Architecture

```
transactions_scored (Kafka topic)
        │
        ▼
  ┌──────────────────────────────────────────────────────┐
  │  Monitor Service (FastAPI on port 8001)               │
  │                                                       │
  │  ┌─────────────────────┐  ┌────────────────────────┐ │
  │  │  Main Thread         │  │  Background Thread      │ │
  │  │                      │  │  (Kafka Consumer Loop)   │ │
  │  │  GET /health         │  │                          │ │
  │  │  GET /metrics        │  │  1. Poll one msg at a    │ │
  │  │  (Prometheus ASGI)   │  │     time via poll(1.0)   │ │
  │  │                      │  │  2. Parse JSON payload   │ │
  │  └─────────────────────┘  │  3. Fill reference window │ │
  │                            │     (first 2000 msgs)    │ │
  │                            │  4. Then fill rolling     │ │
  │                            │     recent window (2000)  │ │
  │                            │  5. Every 30s, compute:   │ │
  │                            │     - PSI on amount       │ │
  │                            │     - L1 on card_type     │ │
  │                            │     - Fraud rate          │ │
  │                            │  6. Update Prometheus     │ │
  │                            │     gauges                │ │
  │                            └────────────────────────┘ │
  └──────────────────────┬────────────────────────────────┘
                         │
                         ▼
              Prometheus Gauges
                         │
              ┌──────────┴──────────┐
              ▼                     ▼
        Alert Rules           Grafana Dashboard
   (PSI > 0.2 for 2m)      (System / ML / Drift panels)
   (Fraud > 5% for 2m)
```

---

## How It Works

### Phase 1: Reference Collection

When the service starts, the Kafka consumer begins polling `transactions_scored` one message at a time using `consumer.poll(timeout=1.0)` from `confluent-kafka`. Each message is a JSON object containing fields like `amount`, `card_type`, and `fraud_label`.

The first **2,000 messages** are collected into the **reference window** — a plain Python list that acts as the frozen baseline. During this phase, no drift is computed because there is nothing to compare against yet.

Once 2,000 messages are collected, the reference window is frozen and never modified again. A log line confirms:
```
Reference window filled (2000 samples). Now tracking drift in recent window.
```

### Phase 2: Recent Window Tracking

After the reference window is full, all subsequent messages are appended to the **recent window** — a `collections.deque` with `maxlen=2000`. This means it automatically evicts the oldest entry when a new one arrives, keeping a rolling snapshot of the latest 2,000 transactions.

Three separate deques track:
- `recent_amounts` — `float` values for PSI computation
- `recent_card_types` — `str` values for categorical drift
- `recent_fraud_labels` — `str` values for fraud rate calculation

### Phase 3: Periodic Drift Computation

Every **30 seconds** (configurable via `DRIFT_COMPUTE_INTERVAL_SEC`), the service calls `_compute_and_expose_drift()` which:

1. Skips computation if the recent window has fewer than 100 samples (not enough data).
2. Calls `compute_psi(reference_amounts, recent_amounts)` to measure numeric distribution shift on transaction amounts.
3. Calls `compute_categorical_drift(reference_card_types, recent_card_types)` to measure L1 distance on card type proportions.
4. Calculates the fraud rate as the proportion of `"fraud"` labels in the recent window.
5. Updates all Prometheus gauges with the computed values.
6. Logs the drift status (e.g., `Drift check: PSI=0.0023 (no drift), card_type_max_shift=0.0051, recent_samples=2000`).

Drift is also recomputed during idle periods (when `poll()` returns `None`) to ensure metrics stay fresh even if message flow is intermittent.

---

## Kafka Consumer Details

The consumer loop in `consumer_loop()` (line 137 of `main.py`) uses `confluent-kafka`'s `Consumer` class:

- **Polling model**: Single-message polling via `consumer.poll(timeout=1.0)`. This returns one message or `None` if no message arrives within 1 second. The `confluent-kafka` library handles internal prefetching and batching at the librdkafka level, so `poll()` typically returns instantly from an internal buffer when messages are flowing.

- **Offset management**: `enable.auto.commit=True` — offsets are committed automatically at regular intervals. This is at-least-once semantics, which is acceptable for a monitoring service where reprocessing a few messages after a restart has no adverse effect.

- **Starting offset**: `auto.offset.reset=latest` — on first startup, the consumer begins reading from the latest offset (it doesn't replay historical data). This means the reference window fills with live data only.

- **Connection retry**: `create_consumer()` retries up to 30 times with exponential backoff (2^attempt seconds, capped at 30s). It validates connectivity by calling `consumer.list_topics(timeout=5)` before returning.

- **Error handling**:
  - `KafkaError._PARTITION_EOF` — silently skipped (normal when caught up).
  - `json.JSONDecodeError` / `UnicodeDecodeError` — increments `monitor_parse_errors_total`, logs the error, continues.
  - Missing `amount` or `card_type` fields — increments parse error counter, skips the message.
  - The consumer never crashes on bad input.

- **Graceful shutdown**: A `threading.Event` (`_STOP_EVENT`) is set during FastAPI's lifespan shutdown. The consumer loop checks it every iteration and exits cleanly when set.

---

## Drift Algorithms (`drift.py`)

### PSI (Population Stability Index) — Numeric Drift

`compute_psi(reference, current, n_bins=10)` measures how much a numeric distribution has shifted.

**Algorithm step-by-step:**

1. **Guard**: If either list has fewer samples than `n_bins`, return `0.0` (not enough data to form meaningful bins).
2. **Build bin edges** from the reference distribution using quantile-based splitting. Sort the reference values, then pick `n_bins - 1` evenly spaced percentile boundaries. This ensures each bin has roughly equal counts in the reference.
3. **Bin both distributions**: For each value, find which bin it falls into by comparing against the edges. Values above the last edge go into the final bin.
4. **Compute proportions**: Divide each bin count by the total, then add epsilon (`1e-6`) to every proportion. This prevents division by zero or `log(0)` when a bin has zero count in one distribution.
5. **Calculate PSI**: Sum over all bins: `(current_prop - ref_prop) * ln(current_prop / ref_prop)`.
6. **Round** to 6 decimal places and return.

**Why quantile-based bins?** Fixed-width bins can be dominated by outliers. Quantile bins ensure the reference distribution is evenly spread across bins, making PSI more sensitive to shifts in the bulk of the distribution.

**Interpretation:**

| PSI Value | Meaning | Action |
|-----------|---------|--------|
| < 0.1     | No significant drift | No action needed |
| 0.1 - 0.2 | Moderate drift | Investigate; may indicate seasonal change |
| > 0.2     | Significant drift | Alert fires; model retraining likely needed |

### Categorical Drift — L1 Distance

`compute_categorical_drift(reference, current)` measures how category proportions have shifted.

**Algorithm step-by-step:**

1. **Guard**: If either list is empty, return `{"max_drift": 0.0, "total_drift": 0.0}`.
2. **Count** occurrences of each category in both lists using `collections.Counter`.
3. **Union** all categories from both distributions (handles new/missing categories).
4. **For each category**: Compute `|current_proportion - reference_proportion|`.
5. **Aggregate**: `max_drift` is the largest single-category shift; `total_drift` is the sum of all absolute differences (L1 distance).
6. **Return** a dict with per-category values plus the two aggregates.

**Example**: If reference has 20% Amex and current has 45% Amex, the Amex drift is 0.25. If Visa drops from 40% to 25%, the Visa drift is 0.15. `total_drift` would be the sum of all such differences.

---

## Prometheus Metrics

All metrics are exposed at `GET /metrics` via `prometheus_client`'s ASGI app mounted on the FastAPI server.

### Counters

| Metric | Description |
|--------|-------------|
| `monitor_messages_consumed_total` | Total messages successfully parsed and processed. Incremented after extracting `amount` and `card_type` from a message. |
| `monitor_parse_errors_total` | Messages that failed JSON parsing or were missing required fields (`amount`, `card_type`). Useful for detecting upstream schema changes or producer bugs. |

### Gauges (updated every 30 seconds)

| Metric | Description |
|--------|-------------|
| `monitor_drift_psi_amount` | PSI score comparing recent vs. reference `amount` distributions. 0.0 means no drift; >0.2 triggers an alert. |
| `monitor_drift_card_type_max` | The largest single-category proportion shift across all card types. Useful for spotting which category moved the most. |
| `monitor_drift_card_type_total` | Sum of all per-category absolute proportion differences (L1 distance). Captures overall categorical distribution shift. |
| `monitor_drift_card_type_category{card_type}` | Per-category proportion shift. Labels include card types like `Visa`, `Mastercard`, `Amex`, `Discover`. Enables per-category drill-down in Grafana. |
| `monitor_drift_fraud_rate` | Fraction of messages in the recent window with `fraud_label == "fraud"`. Normal baseline is ~0.3%; drift mode pushes it to ~2-8%. |
| `monitor_prediction_fraud_rate` | Mirrors `monitor_drift_fraud_rate`. Exists as a separate gauge so that both the ML panel (prediction distribution) and the drift panel can reference it independently in Grafana. |

---

## Alert Rules

Configured in `infrastructure/prometheus/alert_rules.yml` and loaded by Prometheus.

### AmountDistributionDrift

```yaml
alert: AmountDistributionDrift
expr: monitor_drift_psi_amount > 0.2
for: 2m
labels:
  severity: warning
annotations:
  summary: "Amount distribution drift detected"
  description: "PSI score {{ $value }} exceeds threshold 0.2"
```

**Why 2 minutes?** Prevents flapping — transient PSI spikes from small sample fluctuations don't trigger the alert. The condition must be sustained for 2 full minutes before firing.

### HighFraudRate

```yaml
alert: HighFraudRate
expr: monitor_drift_fraud_rate > 0.05
for: 2m
labels:
  severity: critical
annotations:
  summary: "Fraud rate spike detected"
  description: "Fraud rate {{ $value | printf \"%.2%%\" }} exceeds 5% threshold"
```

**Why critical?** A sustained 5%+ fraud rate indicates either a genuine attack or a severe distribution shift. Both warrant immediate investigation.

---

## HTTP Endpoints

| Endpoint | Method | Response | Purpose |
|----------|--------|----------|---------|
| `/health` | GET | `{"status": "ok", "service": "monitor"}` | Liveness check for Docker health checks or load balancers |
| `/metrics` | GET | Prometheus text format | All counters and gauges listed above, scraped by Prometheus every 15 seconds |

Note: The `/metrics` endpoint is mounted as a sub-application using `prometheus_client.make_asgi_app()`, so the trailing slash (`/metrics/`) also works.

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `KAFKA_BROKERS` | `redpanda:9092` | Kafka/Redpanda broker address. Used by `confluent-kafka` to connect. |
| `KAFKA_TOPIC_SCORED` | `transactions_scored` | The Kafka topic to consume from. Should contain scored transactions from the predictor service. |
| `KAFKA_CONSUMER_GROUP_MONITOR` | `monitor-group` | Consumer group ID. Separate from the predictor's group so both services can independently consume the same topic. |
| `DRIFT_PSI_THRESHOLD` | `0.2` | PSI value above which a "DRIFT DETECTED" log is emitted. Also used in Prometheus alert rules. |
| `DRIFT_REFERENCE_SIZE` | `2000` | Number of messages to collect before freezing the reference window. Larger values give a more stable baseline but take longer to fill. |
| `DRIFT_RECENT_SIZE` | `2000` | Maximum size of the rolling recent window (deque maxlen). Larger values smooth out short-term fluctuations. |
| `DRIFT_COMPUTE_INTERVAL_SEC` | `30` | How often (in seconds) drift is recomputed. Lower values give faster detection but higher CPU usage. |
| `DRIFT_WINDOW_MINUTES` | `30` | Informational — documents the intended monitoring window. Not directly used in code (window size is controlled by `DRIFT_RECENT_SIZE` and message rate). |
| `LOG_LEVEL` | `INFO` | Python logging level. Set to `DEBUG` for verbose Kafka consumer logs. |

---

## Testing Drift

### Step 1: Start the System Normally

```bash
docker compose up --build
```

Wait until the monitor logs:
```
Reference window filled (2000 samples). Now tracking drift in recent window.
```

At 20 messages/second (default producer rate), this takes ~100 seconds for the reference and another ~100 seconds for the recent window to fill.

### Step 2: Verify No Drift (Baseline)

Check the monitor logs — you should see:
```
Drift check: PSI=0.0023 (no drift), card_type_max_shift=0.0051, recent_samples=2000
```

Or query Prometheus directly:
```bash
curl http://localhost:8001/metrics | grep monitor_drift_psi_amount
# monitor_drift_psi_amount 0.002345
```

### Step 3: Enable Drift Mode

Toggle the producer's drift mode to shift distributions:

```bash
DRIFT_MODE=1 docker compose up -d producer
```

**What changes in drift mode:**
- **Amount distribution**: Shifts from `log-normal(mean=4.0, sigma=1.0)` to `log-normal(mean=5.0, sigma=1.2)` — transactions become ~2.7x larger on average with wider spread.
- **Card type proportions**: Amex increases from ~20% to ~45%, other types decrease proportionally.
- **Fraud rate**: Increases from ~0.3% base rate to ~2% base rate.

### Step 4: Observe Drift Detection

Within ~2 minutes (time for the recent window to fill with drifted data), the monitor logs should show:
```
Drift check: PSI=0.5231 (DRIFT DETECTED), card_type_max_shift=0.2500, recent_samples=2000
```

The Prometheus alert `AmountDistributionDrift` fires after the PSI stays above 0.2 for 2 sustained minutes.

### Step 5: Disable Drift Mode

```bash
DRIFT_MODE=0 docker compose up -d producer
```

PSI will gradually drop back below 0.1 as the recent window fills with normal data again.

---

## Grafana Dashboard

The pre-provisioned dashboard at `infrastructure/grafana/dashboards/fraud_detection.json` has three sections relevant to the monitor:

### System Metrics Panel
- **Messages Produced** — `rate(producer_messages_produced_total[1m])`
- **Messages Consumed (Predictor)** — `rate(predictor_messages_consumed_total[1m])`
- **Inference Latency (p50/p99)** — histogram quantiles from the predictor

### ML Metrics Panel
- **Prediction Distribution** — fraud vs. legit prediction rates over time
- **Fraud Rate Gauge** — `monitor_drift_fraud_rate` with color thresholds (green <=3%, yellow 3-8%, red >=8%)
- **Parse Errors** — total errors from predictor and monitor

### Drift Detection Panel
- **Amount PSI (timeseries)** — `monitor_drift_psi_amount` with horizontal threshold lines at 0.1 (yellow) and 0.2 (red)
- **PSI Gauge** — visual gauge with green/yellow/red color bands
- **Card Type Drift (timeseries)** — `monitor_drift_card_type_category` broken out per card type, showing exactly which category shifted

Dashboard auto-refreshes every 10 seconds with a default 30-minute time range.

---

## Test Suite

Located in `services/monitor/tests/test_drift.py` — 15 tests covering both drift functions.

### PSI Tests (8 tests)

| Test | What It Verifies |
|------|-----------------|
| `test_identical_distributions_near_zero` | Same data passed as both reference and current produces PSI < 0.01 |
| `test_similar_distributions_low_psi` | Two independent samples from the same Gaussian have PSI < 0.1 |
| `test_shifted_distribution_high_psi` | Shifting the mean from 100 to 200 and doubling sigma produces PSI > 0.2 |
| `test_empty_reference_returns_zero` | Empty reference list returns 0.0 (no crash) |
| `test_empty_current_returns_zero` | Empty current list returns 0.0 (no crash) |
| `test_too_few_samples_returns_zero` | Fewer samples than bins returns 0.0 (can't form meaningful histogram) |
| `test_psi_is_non_negative` | PSI is always >= 0.0 (mathematical property of the formula) |
| `test_custom_bins` | PSI works correctly with `n_bins=5` instead of the default 10 |

### Categorical Drift Tests (7 tests)

| Test | What It Verifies |
|------|-----------------|
| `test_identical_distributions` | Same category list produces max_drift=0.0 and total_drift=0.0 |
| `test_shifted_distribution` | Changing Amex from 20% to 40% produces Amex drift of ~0.2 |
| `test_new_category_in_current` | A category not in reference appears in results with correct drift |
| `test_missing_category_in_current` | A category that disappears shows its full reference proportion as drift |
| `test_empty_reference` | Empty reference returns `{"max_drift": 0.0, "total_drift": 0.0}` |
| `test_empty_current` | Empty current returns `{"max_drift": 0.0, "total_drift": 0.0}` |
| `test_per_category_values_are_absolute` | Drift values are absolute differences, not signed |

Run tests with:
```bash
cd services/monitor
uv run pytest tests/ -v
```

---

## Design Decisions

### Why a Separate Service?
The monitor is decoupled from the predictor. This means:
- It can be scaled independently (e.g., run multiple monitors on different topics).
- It doesn't add latency to the prediction path.
- It only depends on Kafka — no Redis or model file needed.
- It can be stopped/restarted without affecting inference.

### Why Two Fixed-Size Windows Instead of Time-Based?
Time-based windows (`DRIFT_WINDOW_MINUTES`) are harder to reason about when message rates vary. With fixed-size windows:
- The reference always has exactly 2,000 samples (stable statistics).
- The recent window always has up to 2,000 samples (consistent comparison).
- PSI is not affected by message rate fluctuations.

### Why Freeze the Reference?
If the reference window were rolling, it would slowly absorb drift and "forget" the baseline. Freezing it means drift is always measured against the original production distribution. To reset the baseline, restart the service.

### Why Single-Message Polling?
`consumer.poll(timeout=1.0)` returns one message at a time. This is simpler than batch consumption (`consumer.consume(N)`) and sufficient for a monitoring service that:
- Doesn't need per-message low latency.
- Accumulates data into windows for periodic batch computation.
- Relies on `confluent-kafka`'s internal librdkafka prefetch buffer for throughput.

### Why Pure Drift Functions?
`compute_psi()` and `compute_categorical_drift()` are stateless — they take lists in and return numbers. This makes them:
- Testable with no mocking or fixtures.
- Reusable outside the monitor (e.g., in notebooks for offline analysis).
- Easy to reason about in isolation.

---

## File Structure

```
services/monitor/
├── app/
│   ├── __init__.py          # Package marker with version
│   ├── main.py              # FastAPI app + Kafka consumer loop + drift scheduling
│   └── drift.py             # Pure functions: compute_psi, compute_categorical_drift
├── tests/
│   ├── conftest.py          # Test fixtures
│   ├── __init__.py
│   └── test_drift.py        # 15 tests for PSI + categorical drift
├── Dockerfile               # python:3.11-slim + uv-based install
├── pyproject.toml           # confluent-kafka, fastapi, uvicorn, prometheus-client
└── MONITOR.md               # This file
```

### Related Infrastructure Files

```
infrastructure/
├── prometheus/
│   ├── prometheus.yml       # Scrape config (monitor on port 8001)
│   └── alert_rules.yml      # AmountDistributionDrift + HighFraudRate alerts
└── grafana/
    ├── datasource.yml       # Prometheus datasource (UID: PBFA97CFB590B2093)
    ├── dashboard_provider.yml  # File-based dashboard provisioning
    └── dashboards/
        └── fraud_detection.json  # 3-section dashboard (System / ML / Drift)
```
