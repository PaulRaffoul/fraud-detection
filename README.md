# Real-Time Fraud Detection System

Production-grade, event-driven ML system for real-time fraud detection.

## Architecture

```
┌──────────┐    ┌───────────┐    ┌──────────┐    ┌───────────┐
│ Producer │───>│  Redpanda │───>│Predictor │───>│  Redpanda │
│(synthetic│    │  (Kafka)  │    │(ML infer)│    │ (scored)  │
│  txns)   │    │           │    │          │    │           │
└──────────┘    └───────────┘    └────┬─────┘    └───────────┘
                                      │
                                 ┌────▼─────┐
                                 │  Redis   │
                                 │(features)│
                                 └──────────┘

Observability: Prometheus + Grafana | Experiment tracking: MLflow
Drift detection: Monitor service
```

## How to Run

```bash
# 1. Copy env file (if not already done)
cp .env.example .env

# 2. Start all infrastructure services
docker compose up -d

# 3. Check that everything is healthy
docker compose ps
```

## How to Toggle Drift Mode

Set `DRIFT_MODE=1` in `.env` and restart the producer:

```bash
# Edit .env: DRIFT_MODE=1
docker compose up --build producer -d
```

Drift mode shifts distributions: higher amounts (median ~$120 vs ~$45), more Amex cards (45% vs 20%), and higher fraud base rate (2% vs 0.3%). This triggers detectable drift in the monitor service.

## How to View Dashboards

| Service    | URL                        | Credentials   |
|------------|----------------------------|---------------|
| Grafana    | http://localhost:3000      | admin / admin |
| Prometheus | http://localhost:9090      | —             |
| MLflow     | http://localhost:5001      | —             |
| Redpanda   | http://localhost:18082     | — (HTTP Proxy)|

## How to Run Tests

```bash
# Install dev dependencies
uv sync

# Run feature engineering tests
uv run pytest services/predictor/tests/ -v
```

## How to Train the Model

```bash
# Make sure infrastructure is running
docker compose up -d

# Install notebook dependencies
uv sync

# Execute the training notebook
cd notebooks
uv run jupyter nbconvert --to notebook --execute train_model.ipynb --output train_model.ipynb

# Rebuild predictor with the new model
docker compose up --build predictor -d
```

See [notebooks/README.md](notebooks/README.md) for details.

## Design Decisions

- **Redpanda over Kafka**: No ZooKeeper, single binary, faster startup, lower memory. Fully Kafka API-compatible.
- **Grafana provisioning via YAML**: Datasources auto-configured on startup — no manual UI setup needed.
- **MLflow port 5001 on host**: Avoids conflict with macOS AirPlay (port 5000). Internal network still uses 5000.
- **Feature-correlated fraud**: Synthetic fraud probability depends on amount, hour, and merchant category — giving the model real signal to learn (AUC ~0.74). See `services/producer/app/transaction.py`.
- **Feature consistency**: `features.py` is the single source of truth, imported by both the predictor and the training notebook — eliminates train/serve skew.
