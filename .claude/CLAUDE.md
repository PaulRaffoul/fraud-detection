# CLAUDE_CODE.md — Production-Grade Real-Time Fraud Detection System
Owner: Pierre  
Goal: Build a full-stack, event-driven ML system for real-time fraud detection with low-latency inference, feature consistency, and strong observability.

---

## 0) Rules of Engagement (MANDATORY)
These rules override everything else.

### 1) Step-by-step only
- Never produce the whole project at once.
- Work in small, verifiable increments.
- Each step must be runnable/testable in **< 10 minutes**.

### 2) Teaching Mode (every step must include)
For every step, include:
- What we are building
- Why it matters
- What files we will create/modify
- How to run/verify
- What to expect
- Common pitfalls  
Explain decisions briefly but clearly (assume I’m technical but learning system design).

### 3) Output format per step (STRICT)
For each step, output **exactly** these sections in this order:
1. Goal
2. Concepts (what I learn)
3. Plan
4. Changes (files)
5. Code (only the files changed)
6. How to run
7. How to test
8. Stop point (tell me what to reply)

### 4) Don’t move on without confirmation
After every step, stop and ask me to reply with:
- **"continue"** to proceed  
OR
- a question / fix request

### 5) Keep changes minimal
- Prefer the smallest set of files.
- Avoid refactors until the system is working.
- Don’t introduce optional tools unless needed.

---

## 1) Non-Negotiables (Production Bar)
You are a coding agent implementing a production-grade project. Follow these rules strictly:

### Hard Constraints
- **Docker Compose is the source of truth** for local orchestration.
- **No service may hard-crash** because another service is down. Implement:
  - retries with backoff
  - health checks
  - graceful degradation (skip / buffer / retry later)
- **All configs/secrets via `.env`** (and `.env.example` must be complete).
- **Structured JSON logging** for all services.
- **pytest suite** for feature engineering logic.
- **GitHub Actions CI** that runs lint + tests.
- **Latency target**: Sub-50ms inference inside predictor service (excluding Kafka wait time).
- All deliverables must be **runnable locally** with:
  - `docker compose up --build`
  - clear README steps

### Definition of Done (Global)
Project is “done” when:
1. Producer emits synthetic transactions continuously to Kafka topic.
2. Predictor consumes, enriches with Redis features, performs inference, emits results to output topic (or MongoDB if added later).
3. MLflow logs at least one model run + registers a model artifact.
4. Prometheus scrapes metrics from services.
5. Grafana dashboard loads and shows system + ML metrics.
6. Drift mode changes distribution and triggers drift alert/flag.
7. CI passes on GitHub Actions.

---

## 2) Repository Structure (Must Match)
Create/maintain this structure exactly:

/project-root
├── pyproject.toml          # uv workspace root + dev dependencies
├── uv.lock                 # locked dependencies (committed)
├── .python-version         # pins Python 3.11
├── services
│   ├── producer
│   │   └── pyproject.toml  # producer dependencies
│   ├── predictor
│   │   └── pyproject.toml  # predictor dependencies
│   └── monitor
│       └── pyproject.toml  # monitor dependencies
├── notebooks
├── infrastructure
│   ├── prometheus
│   └── grafana
├── docker-compose.yml
├── .env.example
├── README.md
└── CLAUDE_CODE.md

---

## 3) Coding Standards (Strict)
### Python
- Python version: **3.11**
- **Package manager: `uv`** (mandatory for all dependency management)
  - Root `pyproject.toml` defines a uv workspace with all services as members.
  - Each service has its own `pyproject.toml` with service-specific dependencies.
  - Dev tools (ruff, mypy, pytest) live in the root `pyproject.toml` dev dependencies.
  - Use `uv sync` to install all dependencies. Use `uv run` to execute scripts/tools.
  - Dockerfiles must use `uv` for installing dependencies (copy from official image).
  - **Do not use pip, requirements.txt, or poetry.**
- Code style:
  - **ruff** for linting and formatting
- Types:
  - **mypy** minimum for predictor
- Structure:
  - Put shared logic into small modules (avoid giant files).
  - All external calls (Kafka, Redis, MLflow) must be wrapped with retry logic.

### Logging
- Structured JSON logs. Every log line includes:
  - `service`
  - `timestamp`
  - `level`
  - `message`
  - context fields (`transaction_id`, `user_id`, `latency_ms`, etc.)
- Do not log secrets.

### Reliability Patterns
- Every service must:
  - start even if dependencies are missing
  - retry connections with exponential backoff
  - expose a health endpoint if HTTP-based (predictor/monitor)
- Kafka consumer must:
  - never crash on malformed messages (skip + log)
  - commit offsets safely (at-least-once acceptable)

### Feature Consistency
- Single source of truth for feature definitions:
  - `services/predictor/app/features.py`
  - tested with pytest
- Training feature schema must match inference schema; store schema in MLflow.

### Metrics (Prometheus)
Expose `/metrics` for:
- throughput counters (messages consumed, produced, predicted)
- inference latency histogram
- prediction distribution counters (fraud vs non-fraud)
- drift score gauge (or drift flag)

---

## 4) Tech Choices (Default)
Use these defaults unless blocked:
- Kafka replacement: **Redpanda** (Kafka API compatible, lighter)
- Kafka client: `confluent-kafka`
- Feature store: **Redis**
- Model: start with **LogisticRegression** (simple baseline), joblib
- API: **FastAPI**
- Experiment tracking: **MLflow**
- Monitoring: **Prometheus + Grafana**
- Drift: PSI on `Amount` + distribution shift for `Card_Type`

---

## 5) Phased Plan (Implement in Order)
IMPORTANT: Implementation must follow the “Rules of Engagement” step-by-step format.
Do not skip ahead.

### Phase 0 (FIRST TASK) — Scaffolding ✅
Create the folder structure and placeholder files exactly as specified.
No functionality yet. Keep it minimal.

### Phase 1 — Compose Infrastructure First ✅
Bring up redpanda, redis, mlflow, prometheus, grafana with docker compose.

### Phase 2 — Producer with Drift Mode ✅
Stream synthetic transactions to `transactions_raw`.
- `transaction.py`: Pure function generating synthetic data with log-normal amounts, weighted card types, configurable fraud rates.
- `main.py`: Kafka producer loop with exponential backoff retry, structured JSON logging, Prometheus counters.
- Drift mode shifts distributions (higher amounts, more Amex, 8% fraud rate) via `DRIFT_MODE=1` env var.

### Phase 3 — Redis Feature Store + Tests
Build the feature engineering layer that computes per-user rolling aggregates in Redis.

**Files to create/modify:**
- `services/predictor/app/features.py` — Single source of truth for feature definitions. Pure functions that:
  - Update Redis with new transaction data (sorted sets keyed by user_id with timestamp scores)
  - Query rolling aggregates: `txn_count_1h`, `txn_count_24h`, `avg_amount_24h`
  - Compute derived features: `amount_vs_avg_ratio` (current amount / user's 24h average)
- `services/predictor/pyproject.toml` — Add `redis` dependency
- `tests/test_features.py` — pytest suite using `fakeredis` to verify:
  - Correct counts over time windows (1h, 24h)
  - Correct average computation
  - Ratio computation (including edge case: first transaction for a user)
  - Expired data is excluded from aggregates

**Why this matters:**
- Feature consistency: same code runs at training and inference time.
- Pure function tests catch feature bugs before they poison model predictions.
- Redis sorted sets with timestamp scores enable efficient window-based expiry.
- Sub-millisecond Redis reads keep us under the 50ms latency target.

**Key design decisions:**
- Use Redis sorted sets (ZADD/ZRANGEBYSCORE) — timestamps as scores allow efficient range queries and TTL-like cleanup.
- Feature functions are pure (take Redis client as argument) — testable with fakeredis, no mocking needed.
- All feature names defined as constants — prevents train/serve skew from typos.

### Phase 4 — Predictor Pipeline
Consume from `transactions_raw`, enrich with Redis features, run inference, emit to `transactions_scored`.

**Files to create/modify:**
- `services/predictor/app/main.py` — FastAPI app with:
  - Kafka consumer (confluent-kafka) in a background thread
  - Message parsing with error handling (skip malformed, log, don't crash)
  - Feature enrichment via `features.py` + Redis
  - Model inference (joblib-loaded LogisticRegression)
  - Produce scored results to `transactions_scored` topic
  - Health endpoint (`GET /health`)
- `services/predictor/app/model.py` — Model loading with retry, fallback if model file missing
- `services/predictor/Dockerfile` — uv-based, copies model artifact
- `docker-compose.yml` — Add predictor service, depends_on redis + redpanda
- `infrastructure/prometheus/prometheus.yml` — Add predictor scrape target

**Prometheus metrics exposed:**
- `predictor_messages_consumed_total` — throughput counter
- `predictor_inference_latency_seconds` — histogram (must be < 50ms p99)
- `predictor_predictions_total{label=fraud|legit}` — prediction distribution

**Key design decisions:**
- At-least-once consumption (commit after processing, not before)
- Graceful degradation: if Redis is down, skip enrichment and use default features
- If model file is missing, log error and return neutral score (0.5)

### Phase 5 — MLflow Training + Registry Logging
Train the baseline model, log it to MLflow, export artifact for the predictor.

**Files to create/modify:**
- `notebooks/train_model.ipynb` — Jupyter notebook that:
  - Generates a training dataset using `transaction.py` + `features.py`
  - Trains LogisticRegression (scikit-learn)
  - Logs parameters, metrics (accuracy, precision, recall, F1, AUC) to MLflow
  - Logs the feature schema (ordered feature names + types) as an MLflow artifact
  - Exports `model.joblib` to `services/predictor/model/`
  - Registers the model in MLflow Model Registry
- `services/predictor/model/model.joblib` — Trained model artifact (gitignored, built by notebook)

**Why this matters:**
- MLflow provides experiment lineage — which model is in production, what metrics it achieved, what features it used.
- Feature schema artifact ensures train/serve consistency is auditable.
- Notebook format makes it easy to iterate and visualize results.

### Phase 6 — Drift Monitor + Alerting + Dashboard
Detect distribution drift in incoming transactions and surface it through metrics and dashboards.

**Files to create/modify:**
- `services/monitor/app/main.py` — FastAPI service that:
  - Consumes from `transactions_scored` (or `transactions_raw`)
  - Maintains sliding windows of `amount` and `card_type` distributions
  - Computes PSI (Population Stability Index) for `amount` over `DRIFT_WINDOW_MINUTES`
  - Computes categorical distribution shift for `card_type`
  - Exposes drift metrics via `/metrics` for Prometheus
- `services/monitor/app/drift.py` — Pure functions for PSI calculation and distribution comparison
- `services/monitor/Dockerfile` — uv-based container
- `services/monitor/pyproject.toml` — Dependencies
- `infrastructure/grafana/dashboards/` — Pre-provisioned Grafana dashboard JSON:
  - System panel: message throughput, consumer lag, inference latency
  - ML panel: prediction distribution (fraud vs legit over time)
  - Drift panel: PSI gauge, card_type distribution bars, drift alert indicator
- `infrastructure/prometheus/alert_rules.yml` — Alert rule: fire when `drift_psi > DRIFT_PSI_THRESHOLD`
- `docker-compose.yml` — Add monitor service

**Key design decisions:**
- PSI > 0.2 = significant drift (industry standard threshold)
- Monitor is a separate service (not inside predictor) — separation of concerns, can scale independently
- Grafana dashboards are provisioned via JSON files (reproducible, version-controlled)

### Phase 7 — CI/CD Quality Gate
Automated quality checks on every push via GitHub Actions.

**Files to create/modify:**
- `.github/workflows/ci.yml` — GitHub Actions workflow that runs:
  - `ruff check` + `ruff format --check` — linting and formatting
  - `mypy services/predictor/` — type checking for the predictor service
  - `pytest tests/` — feature engineering tests + predictor message parsing tests
- Matrix: runs on Python 3.11
- Uses `uv` for dependency installation in CI

**What must pass:**
- Zero ruff errors
- Zero mypy errors in predictor
- All pytest tests green

---

## 6) Environment Variables (Standardize Names)
Use these keys consistently:

### Kafka / Redpanda
- KAFKA_BROKERS=redpanda:9092
- KAFKA_TOPIC_RAW=transactions_raw
- KAFKA_TOPIC_SCORED=transactions_scored
- KAFKA_CONSUMER_GROUP=predictor-group

### Producer
- PRODUCER_RATE_PER_SEC=20
- DRIFT_MODE=0

### Redis
- REDIS_HOST=redis
- REDIS_PORT=6379

### Predictor
- MODEL_PATH=/app/model/model.joblib
- LOG_LEVEL=INFO

### MLflow
- MLFLOW_TRACKING_URI=http://mlflow:5000
- MLFLOW_EXPERIMENT_NAME=fraud_detection

### Monitoring
- DRIFT_WINDOW_MINUTES=30
- DRIFT_PSI_THRESHOLD=0.2

---

## 7) Testing Requirements (Minimum)
pytest tests for:
- Feature computation correctness (pure functions)
- Redis feature update logic (fakeredis or abstraction)
- Predictor message parsing/validation (invalid JSON doesn’t crash)

---

## 8) Documentation Requirements
README must include:
- System diagram (ASCII is fine)
- How to run
- How to toggle drift mode
- How to view MLflow, Grafana, Prometheus
- How to run tests + CI explanation
- Design decisions section

---

## 9) Agent Workflow Rules (How You Should Work)
- Implement **one step at a time**, runnable in <10 minutes.
- Keep changes minimal.
- Prefer robust and simple.
- If a design choice is made, document briefly in README.

---
END.