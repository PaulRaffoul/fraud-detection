# Real-Time Fraud Detection System

A production-grade, event-driven machine learning system for real-time credit card fraud detection. Built with streaming infrastructure, sub-50ms inference, per-user behavioral features, and live drift monitoring.

## Table of Contents

- [Architecture](#architecture)
- [Tech Stack](#tech-stack)
- [Project Structure](#project-structure)
- [Getting Started](#getting-started)
  - [Prerequisites](#prerequisites)
  - [Quick Start](#quick-start)
  - [Training the Model](#training-the-model)
- [Services](#services)
  - [Producer](#producer)
  - [Predictor](#predictor)
  - [Monitor](#monitor)
- [Feature Engineering](#feature-engineering)
- [Drift Detection](#drift-detection)
- [Observability](#observability)
  - [Dashboards](#dashboards)
  - [Metrics](#metrics)
  - [Alerting](#alerting)
- [Testing](#testing)
- [Configuration](#configuration)
- [Design Decisions](#design-decisions)
- [Documentation](#documentation)

---

## Architecture

```
                                     ┌─────────────────────────────────────────────────────────┐
                                     │                    Observability                        │
                                     │    Prometheus (:9090)  ──>  Grafana (:3000)             │
                                     │         ^      ^      ^                                 │
                                     │         │      │      │     MLflow (:5001)              │
                                     └─────────┼──────┼──────┼─────────────────────────────────┘
                                               │      │      │
┌──────────┐    ┌───────────────┐    ┌─────────┴──┐   │   ┌──┴────────┐    ┌───────────────┐
│ Producer │───>│   Redpanda    │───>│  Predictor │   │   │  Monitor  │    │   Redpanda    │
│          │    │               │    │            │   │   │           │    │               │
│ Synthetic│    │ transactions  │    │ Feature    │   │   │ PSI drift │    │ transactions  │
│ txn gen  │    │ _raw          │    │ enrichment │   │   │ detection │    │ _scored       │
│          │    │               │    │ + inference│───┼──>│           │<───│               │
│ :8002    │    │               │    │ :8000      │   │   │ :8001     │    │               │
└──────────┘    └───────────────┘    └─────┬──────┘   │   └───────────┘    └───────────────┘
                                           │          │
                                      ┌────▼─────┐   │
                                      │  Redis   │   │
                                      │ Per-user │   │
                                      │ features │   │
                                      │ :6379    │   │
                                      └──────────┘   │
```

**Data flow:**
1. **Producer** generates synthetic transactions at 20/sec and publishes to `transactions_raw`
2. **Predictor** consumes each transaction, computes per-user behavioral features from Redis, runs model inference, and publishes scored results to `transactions_scored`
3. **Monitor** consumes scored transactions and computes distribution drift (PSI on amount, L1 distance on card type) against a reference baseline
4. **Prometheus** scrapes metrics from all services; **Grafana** visualizes them in a pre-provisioned dashboard

---

## Tech Stack

| Component | Technology | Purpose |
|-----------|-----------|---------|
| Message broker | [Redpanda](https://redpanda.com/) v24.1 | Kafka-compatible streaming (no ZooKeeper) |
| Feature store | [Redis](https://redis.io/) 7 | Per-user rolling aggregates via sorted sets |
| ML framework | [scikit-learn](https://scikit-learn.org/) 1.5 | LogisticRegression baseline model |
| API framework | [FastAPI](https://fastapi.tiangolo.com/) | Health + metrics endpoints for predictor and monitor |
| Experiment tracking | [MLflow](https://mlflow.org/) 2.15 | Model registry, parameter/metric logging |
| Metrics | [Prometheus](https://prometheus.io/) 2.53 | Time-series metrics collection |
| Dashboards | [Grafana](https://grafana.com/) 11.1 | Pre-provisioned visualization |
| Kafka client | [confluent-kafka](https://github.com/confluentinc/confluent-kafka-python) | Producer and consumer for Python |
| Package manager | [uv](https://docs.astral.sh/uv/) | Workspace-based dependency management |
| Linting | [Ruff](https://docs.astral.sh/ruff/) | Linting and formatting |
| Type checking | [mypy](https://mypy-lang.org/) | Static type analysis (predictor service) |
| Orchestration | Docker Compose | Local multi-service orchestration |

---

## Project Structure

```
fraud_detection/
├── docker-compose.yml              # All 8 services defined here
├── .env.example                    # Environment variable template
├── pyproject.toml                  # uv workspace root + dev dependencies
├── uv.lock                        # Locked dependencies
├── .python-version                 # Python 3.11
│
├── services/
│   ├── producer/                   # Synthetic transaction generator
│   │   ├── app/
│   │   │   ├── main.py             # Kafka producer loop with retry
│   │   │   └── transaction.py      # Pure function: generates one transaction
│   │   ├── Dockerfile
│   │   └── pyproject.toml
│   │
│   ├── predictor/                  # ML inference pipeline
│   │   ├── app/
│   │   │   ├── main.py             # Kafka consumer + FastAPI
│   │   │   ├── features.py         # Feature definitions (single source of truth)
│   │   │   └── model.py            # Model loading + inference
│   │   ├── model/                  # Trained model artifacts
│   │   │   ├── model.joblib        # Trained model (gitignored, built by notebook)
│   │   │   └── feature_schema.json # Feature names + types for auditing
│   │   ├── tests/
│   │   │   └── test_features.py    # 9 tests (fakeredis)
│   │   ├── Dockerfile
│   │   └── pyproject.toml
│   │
│   └── monitor/                    # Drift detection service
│       ├── app/
│       │   ├── main.py             # Kafka consumer + FastAPI + drift loop
│       │   └── drift.py            # Pure functions: PSI + categorical drift
│       ├── tests/
│       │   └── test_drift.py       # 15 tests
│       ├── Dockerfile
│       └── pyproject.toml
│
├── notebooks/
│   └── train_model.ipynb           # Model training + MLflow logging
│
└── infrastructure/
    ├── prometheus/
    │   ├── prometheus.yml          # Scrape config for all services
    │   └── alert_rules.yml         # Drift + fraud rate alerts
    └── grafana/
        ├── datasource.yml          # Prometheus datasource (auto-provisioned)
        ├── dashboard_provider.yml  # Dashboard provisioning config
        └── dashboards/
            └── fraud_detection.json # Pre-built dashboard (3 sections, 10 panels)
```

---

## Getting Started

### Prerequisites

- [Docker](https://docs.docker.com/get-docker/) and Docker Compose
- [uv](https://docs.astral.sh/uv/getting-started/installation/) (for local development and testing)
- Python 3.11+ (managed by uv)

### Quick Start

```bash
# 1. Clone the repository
git clone https://github.com/PaulRaffoul/fraud-detection.git
cd fraud-detection

# 2. Copy environment variables
cp .env.example .env

# 3. Start all services
docker compose up -d

# 4. Verify all 8 containers are running
docker compose ps
```

All services will be available within ~30 seconds:

| Service | URL | Purpose |
|---------|-----|---------|
| Grafana | http://localhost:3000 | Dashboards (login: `admin` / `admin`) |
| Prometheus | http://localhost:9090 | Metrics & alerts |
| MLflow | http://localhost:5001 | Experiment tracking & model registry |
| Predictor | http://localhost:8000/health | Inference service health check |
| Monitor | http://localhost:8001/health | Drift monitor health check |
| Redpanda HTTP | http://localhost:18082 | Kafka HTTP proxy |

The system starts generating, scoring, and monitoring transactions immediately. Open Grafana and navigate to the **"Fraud Detection System"** dashboard to see live data.

### Training the Model

The predictor ships with a fallback (returns score 0.5) if no model file is present. To train and deploy a real model:

```bash
# Install dev dependencies (jupyter, scikit-learn, mlflow, etc.)
uv sync

# Run the training notebook
cd notebooks
uv run jupyter nbconvert --to notebook --execute train_model.ipynb --output train_model.ipynb

# Or open it interactively
uv run jupyter notebook train_model.ipynb
```

The notebook generates 20,000 synthetic transactions, trains a LogisticRegression, logs everything to MLflow, and exports `model.joblib` to `services/predictor/model/`.

After training, rebuild the predictor to bake in the new model:

```bash
cd ..
docker compose up --build predictor -d

# Verify the model loaded
docker logs predictor 2>&1 | head -5
# Should show: "Model loaded from /app/model/model.joblib"
```

### Stopping the System

```bash
docker compose down      # Stop all services (data preserved in volumes)
docker compose down -v   # Stop and delete all data (clean slate)
```

---

## Services

### Producer

Generates synthetic credit card transactions at a configurable rate (default: 20/sec) and publishes them to the `transactions_raw` Kafka topic.

**Transaction schema:**
```json
{
  "transaction_id": "a1b2c3d4-...",
  "user_id": "user_0042",
  "amount": 67.23,
  "card_type": "Visa",
  "merchant_category": "grocery",
  "timestamp": "2026-03-07T14:22:01.123456+00:00",
  "hour_of_day": 14,
  "day_of_week": 1,
  "is_fraud": false
}
```

**Fraud generation:** Fraud is feature-correlated, not random. The `_fraud_probability()` function applies stacking risk multipliers on a low base rate (~0.3%):

| Risk Factor | Multiplier | Rationale |
|-------------|------------|-----------|
| Amount > $500 | 6x | Fraudsters drain accounts with large purchases |
| Amount > $200 | 3x | Moderately suspicious |
| Hour 0-5 AM | 4x | Stolen cards used when victim is asleep |
| Electronics or travel | 3x | High-value, easily resold goods |

Multipliers stack: a $600 electronics purchase at 3 AM = 0.3% x 6 x 3 x 4 = **21.6% fraud probability**.

See [services/producer/DESIGN.md](services/producer/DESIGN.md) for detailed architecture.

### Predictor

The core inference pipeline. Consumes raw transactions, enriches with per-user features from Redis, runs model inference, and publishes scored results.

**Processing pipeline (per message):**
1. Parse JSON (skip malformed messages)
2. Write transaction to Redis sorted set (`ZADD`)
3. Read rolling aggregates from Redis (`ZRANGEBYSCORE`)
4. Add raw transaction features (amount, hour, day)
5. Run model inference (LogisticRegression)
6. Publish scored result to `transactions_scored`

**Graceful degradation:**
| Dependency | If unavailable |
|------------|----------------|
| Kafka | Retries with exponential backoff (up to 30 attempts) |
| Redis | Uses default features (all zeros) |
| Model file | Returns neutral score (0.5) |
| Malformed message | Skips + logs + increments error counter |

See [services/predictor/PREDICTOR.md](services/predictor/PREDICTOR.md) for detailed architecture.

### Monitor

Detects distribution drift by comparing a frozen reference baseline (first 2,000 messages) against a rolling recent window (latest 2,000 messages).

**Drift metrics computed every 30 seconds:**
- **PSI (Population Stability Index)** on transaction amounts
- **L1 distance** on card type proportions
- **Fraud rate** in the recent window

See [services/monitor/MONITOR.md](services/monitor/MONITOR.md) for detailed architecture.

---

## Feature Engineering

The feature store (`services/predictor/app/features.py`) is the **single source of truth** for feature definitions — imported by both the predictor service and the training notebook. This eliminates train/serve skew.

| Feature | Source | Description |
|---------|--------|-------------|
| `txn_count_1h` | Redis | User's transactions in the last hour |
| `txn_count_24h` | Redis | User's transactions in the last 24 hours |
| `avg_amount_24h` | Redis | User's average transaction amount over 24h |
| `amount_vs_avg_ratio` | Redis | Current amount / 24h average (spike detector) |
| `amount` | Transaction | Raw transaction amount |
| `hour_of_day` | Transaction | Hour of transaction (0-23) |
| `day_of_week` | Transaction | Day of week (0=Mon, 6=Sun) |

Redis sorted sets (ZADD/ZRANGEBYSCORE) enable sub-millisecond feature lookups with timestamp-based windowing. See [services/predictor/FEATURES.md](services/predictor/FEATURES.md) for the full design.

---

## Drift Detection

Toggle the producer's drift mode to simulate a distribution shift:

```bash
# Enable drift mode
DRIFT_MODE=1 docker compose up -d producer

# Disable drift mode
DRIFT_MODE=0 docker compose up -d producer
```

**What changes in drift mode:**

| Parameter | Normal | Drift |
|-----------|--------|-------|
| Amount distribution | log-normal(3.8, 1.0), median ~$45 | log-normal(4.8, 1.3), median ~$120 |
| Amex proportion | 20% | 45% |
| Fraud base rate | 0.3% | 2% |

**Expected effect:** PSI jumps from ~0.01 to >0.5 within 2 minutes. The `AmountDistributionDrift` Prometheus alert fires when PSI exceeds 0.2.

---

## Observability

### Dashboards

Grafana is pre-provisioned with a **Fraud Detection System** dashboard at http://localhost:3000 (login: `admin` / `admin`). It contains three sections:

**System Metrics**
- Messages produced rate (per second)
- Messages consumed by predictor (per second)
- Inference latency (p50 / p99)

**ML Metrics**
- Prediction distribution (fraud vs legit over time)
- Fraud rate gauge (recent window)
- Parse errors (predictor + monitor)

**Drift Detection**
- Amount PSI time series (with threshold lines at 0.1 and 0.2)
- PSI gauge
- Card type drift per category

### Metrics

Each service exposes a `/metrics` endpoint scraped by Prometheus every 15 seconds.

| Metric | Service | Type | Description |
|--------|---------|------|-------------|
| `producer_messages_produced_total` | Producer | Counter | Messages published |
| `producer_errors_total` | Producer | Counter | Failed deliveries |
| `predictor_messages_consumed_total` | Predictor | Counter | Messages consumed |
| `predictor_inference_latency_seconds` | Predictor | Histogram | Feature enrichment + inference time |
| `predictor_predictions_total{label}` | Predictor | Counter | Predictions by label (fraud/legit) |
| `predictor_parse_errors_total` | Predictor | Counter | Malformed messages |
| `monitor_drift_psi_amount` | Monitor | Gauge | PSI score for amount distribution |
| `monitor_drift_card_type_max` | Monitor | Gauge | Max single-category proportion shift |
| `monitor_drift_fraud_rate` | Monitor | Gauge | Fraud rate in recent window |
| `monitor_messages_consumed_total` | Monitor | Counter | Messages consumed by monitor |

### Alerting

Prometheus alert rules are defined in `infrastructure/prometheus/alert_rules.yml`:

| Alert | Condition | Severity | Description |
|-------|-----------|----------|-------------|
| `AmountDistributionDrift` | `monitor_drift_psi_amount > 0.2` for 2m | Warning | Transaction amount distribution has shifted |
| `HighFraudRate` | `monitor_drift_fraud_rate > 0.05` for 2m | Critical | Fraud rate exceeds 5% in recent window |

Check active alerts at http://localhost:9090/alerts.

---

## Testing

```bash
# Install dev dependencies
uv sync

# Run predictor tests (feature engineering — 9 tests)
uv run pytest services/predictor/tests -v

# Run monitor tests (drift detection — 15 tests)
uv run pytest services/monitor/tests -v

# Run linting
uv run ruff check services/

# Run type checking (predictor)
uv run mypy services/predictor/
```

Tests use [fakeredis](https://github.com/cunla/fakeredis-py) for Redis — no running infrastructure needed. All tests execute in under 1 second.

| Test Suite | Count | What it covers |
|-----------|-------|----------------|
| `test_features.py` | 9 | Feature computation, time windows, edge cases (zero amounts, first-time users, user isolation) |
| `test_drift.py` | 15 | PSI calculation, categorical drift, empty inputs, shifted vs identical distributions |

---

## Configuration

All configuration is via environment variables. Copy `.env.example` to `.env` and adjust as needed.

| Variable | Default | Description |
|----------|---------|-------------|
| `KAFKA_BROKERS` | `redpanda:9092` | Kafka bootstrap servers |
| `KAFKA_TOPIC_RAW` | `transactions_raw` | Raw transaction topic |
| `KAFKA_TOPIC_SCORED` | `transactions_scored` | Scored output topic |
| `KAFKA_CONSUMER_GROUP` | `predictor-group` | Predictor consumer group |
| `PRODUCER_RATE_PER_SEC` | `20` | Transactions generated per second |
| `DRIFT_MODE` | `0` | `0` = normal, `1` = shifted distributions |
| `REDIS_HOST` | `redis` | Redis hostname |
| `REDIS_PORT` | `6379` | Redis port |
| `MODEL_PATH` | `/app/model/model.joblib` | Path to trained model inside container |
| `LOG_LEVEL` | `INFO` | Logging level for all services |
| `MLFLOW_TRACKING_URI` | `http://mlflow:5000` | MLflow server URL |
| `DRIFT_PSI_THRESHOLD` | `0.2` | PSI threshold for drift alert |
| `DRIFT_WINDOW_MINUTES` | `30` | Drift window size |

---

## Design Decisions

| Decision | Rationale |
|----------|-----------|
| **Redpanda over Kafka** | No ZooKeeper, single binary, faster startup, lower memory. Fully Kafka API-compatible. |
| **Redis sorted sets for features** | Sub-millisecond reads, timestamp-based windowing via scores, natural per-user isolation. Keeps us under the 50ms latency target. |
| **Feature code shared between training and inference** | `features.py` is the single source of truth — eliminates train/serve skew, the #1 silent failure in production ML. |
| **Feature-correlated fraud generation** | Fraud probability depends on amount, hour, and merchant category, giving the model real signal (AUC ~0.74). Random labels would produce a useless model. |
| **Separate monitor service** | Drift detection is decoupled from inference — can scale independently, doesn't add latency to the prediction path. |
| **Graceful degradation everywhere** | No service hard-crashes if a dependency is down. Predictor works without Redis (default features) or model (score 0.5). All services retry with exponential backoff. |
| **At-least-once Kafka semantics** | Predictor commits offsets after processing, not before. Duplicates are acceptable; missed fraud is not. |
| **Grafana dashboards as code** | JSON dashboard + YAML datasource are version-controlled and auto-provisioned — no manual UI setup after `docker compose up`. |
| **MLflow port 5001 on host** | Avoids conflict with macOS AirPlay receiver (port 5000). Internal Docker network still uses 5000. |
| **uv workspace monorepo** | Each service has its own `pyproject.toml` for isolation, but shares a single lockfile and dev toolchain. |

---

## Documentation

Detailed documentation for each component:

| Document | Description |
|----------|-------------|
| [services/producer/DESIGN.md](services/producer/DESIGN.md) | Producer architecture, data generation, Kafka patterns |
| [services/predictor/PREDICTOR.md](services/predictor/PREDICTOR.md) | Predictor pipeline, threading model, graceful degradation |
| [services/predictor/FEATURES.md](services/predictor/FEATURES.md) | Feature store design, Redis data model, testing strategy |
| [services/monitor/MONITOR.md](services/monitor/MONITOR.md) | Drift detection, PSI thresholds, alert configuration |
| [notebooks/README.md](notebooks/README.md) | Model training, MLflow logging, performance metrics |
