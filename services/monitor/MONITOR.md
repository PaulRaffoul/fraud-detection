# Monitor Service — Drift Detection

The monitor service detects distribution drift in live transactions by comparing a **reference baseline** against a **recent rolling window** of scored transactions.

## Architecture

```
transactions_scored (Kafka)
        │
        ▼
  ┌───────────┐
  │  Monitor   │
  │            │
  │ Reference  │  First 2000 messages → frozen baseline
  │ Window     │
  │            │
  │ Recent     │  Rolling 2000 messages → compared to reference
  │ Window     │
  │            │
  │ Drift      │  PSI (amount) + L1 (card_type) every 30s
  │ Compute    │
  └─────┬──────┘
        │
        ▼
  Prometheus Gauges → Grafana Dashboard + Alert Rules
```

## How It Works

1. **Reference phase**: The first 2000 scored messages fill the reference window (frozen baseline).
2. **Tracking phase**: Subsequent messages fill a rolling recent window (max 2000).
3. **Drift computation**: Every 30 seconds, the service computes:
   - **PSI (Population Stability Index)** on transaction `amount` — measures how much the numeric distribution has shifted.
   - **Categorical drift** on `card_type` — L1 distance between reference and recent proportions.
   - **Fraud rate** — proportion of `fraud` labels in the recent window.

### PSI Thresholds (Industry Standard)

| PSI Value | Interpretation |
|-----------|----------------|
| < 0.1     | No significant drift |
| 0.1 – 0.2 | Moderate drift |
| > 0.2     | Significant drift (alert fires) |

## Prometheus Metrics

| Metric | Type | Description |
|--------|------|-------------|
| `monitor_messages_consumed_total` | Counter | Total messages consumed |
| `monitor_parse_errors_total` | Counter | Failed message parses |
| `monitor_drift_psi_amount` | Gauge | PSI score for amount distribution |
| `monitor_drift_card_type_max` | Gauge | Largest single-category proportion shift |
| `monitor_drift_card_type_total` | Gauge | Total L1 distance across all card types |
| `monitor_drift_card_type_category{card_type}` | Gauge | Per-category proportion shift |
| `monitor_drift_fraud_rate` | Gauge | Fraud rate in recent window |
| `monitor_prediction_fraud_rate` | Gauge | Fraud prediction rate in recent window |

## Alert Rules

Configured in `infrastructure/prometheus/alert_rules.yml`:

- **AmountDistributionDrift**: Fires when `monitor_drift_psi_amount > 0.2` for 2 minutes.
- **HighFraudRate**: Fires when `monitor_drift_fraud_rate > 0.05` for 2 minutes.

## Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /health` | Health check (`{"status": "ok"}`) |
| `GET /metrics/` | Prometheus metrics |

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `KAFKA_BROKERS` | `redpanda:9092` | Kafka broker address |
| `KAFKA_TOPIC_SCORED` | `transactions_scored` | Input topic |
| `KAFKA_CONSUMER_GROUP_MONITOR` | `monitor-group` | Consumer group |
| `DRIFT_WINDOW_MINUTES` | `30` | Window size (informational) |
| `DRIFT_PSI_THRESHOLD` | `0.2` | PSI alert threshold |
| `DRIFT_REFERENCE_SIZE` | `2000` | Reference window size |
| `DRIFT_RECENT_SIZE` | `2000` | Recent window size |
| `DRIFT_COMPUTE_INTERVAL_SEC` | `30` | Drift recomputation interval |
| `LOG_LEVEL` | `INFO` | Logging level |

## Testing Drift

Toggle the producer's drift mode to shift distributions:

```bash
# Enable drift mode (higher amounts, more Amex, higher fraud rate)
DRIFT_MODE=1 docker compose up -d producer

# Disable drift mode (return to normal)
DRIFT_MODE=0 docker compose up -d producer
```

In drift mode:
- Amount distribution shifts from log-normal(4.0, 1.0) to log-normal(5.0, 1.2)
- Amex proportion increases from ~20% to ~45%
- Base fraud rate increases from 0.3% to 2%

Expected PSI jumps from ~0.01 to >0.5 within 2 minutes.

## File Structure

```
services/monitor/
├── app/
│   ├── __init__.py
│   ├── main.py          # FastAPI + Kafka consumer + drift loop
│   └── drift.py         # Pure functions: compute_psi, compute_categorical_drift
├── tests/
│   ├── __init__.py
│   └── test_drift.py    # 15 tests for PSI + categorical drift
├── Dockerfile
└── pyproject.toml
```
