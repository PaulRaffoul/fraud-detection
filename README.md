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

> Coming in Phase 2

## How to View Dashboards

| Service    | URL                        | Credentials   |
|------------|----------------------------|---------------|
| Grafana    | http://localhost:3000      | admin / admin |
| Prometheus | http://localhost:9090      | —             |
| MLflow     | http://localhost:5001      | —             |
| Redpanda   | http://localhost:18082     | — (HTTP Proxy)|

## How to Run Tests

> Coming in Phase 3

## Design Decisions

- **Redpanda over Kafka**: No ZooKeeper, single binary, faster startup, lower memory. Fully Kafka API-compatible.
- **Grafana provisioning via YAML**: Datasources auto-configured on startup — no manual UI setup needed.
- **MLflow port 5001 on host**: Avoids conflict with macOS AirPlay (port 5000). Internal network still uses 5000.
