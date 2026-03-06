# Notebooks

## train_model.ipynb — Baseline Model Training

This notebook trains the fraud detection model and logs everything to MLflow.

### What it does

1. **Generates a synthetic dataset** (20,000 transactions) using the same `generate_transaction()` from the producer and `features.py` from the predictor. A fakeredis instance simulates the Redis feature store so users accumulate realistic rolling history.

2. **Computes 7 features** (single source of truth from `features.py`):

   | Feature | Source | Description |
   |---------|--------|-------------|
   | `txn_count_1h` | Redis | Transactions by this user in the last hour |
   | `txn_count_24h` | Redis | Transactions in the last 24 hours |
   | `avg_amount_24h` | Redis | Average transaction amount over 24h |
   | `amount_vs_avg_ratio` | Redis | Current amount / 24h average (spike detector) |
   | `amount` | Transaction | Raw transaction amount |
   | `hour_of_day` | Transaction | Hour when the transaction occurred |
   | `day_of_week` | Transaction | Day of the week (0=Monday) |

3. **Trains a LogisticRegression** with `class_weight="balanced"` to handle the ~1.2% fraud rate imbalance.

4. **Logs to MLflow** (http://localhost:5001):
   - Parameters: model type, solver, max_iter, n_samples, n_features, test_size
   - Metrics: accuracy, precision, recall, F1, ROC AUC
   - Artifacts: sklearn model, feature schema JSON, exported joblib
   - Registers model as `fraud_detection_baseline` v1 in Model Registry

5. **Exports `model.joblib`** to `services/predictor/model/` for the predictor container.

### Why feature consistency matters

The notebook imports `features.py` directly from the predictor service. This means:
- Training features are computed by the **exact same code** as inference features
- Feature names and ordering come from `FEATURE_NAMES` (a constant list)
- No risk of train/serve skew from mismatched feature logic or typos

### How to run

```bash
# From the project root — make sure infrastructure is up
docker compose up -d

# Install dependencies (includes jupyter, mlflow, scikit-learn, etc.)
uv sync

# Execute the notebook
cd notebooks
uv run jupyter nbconvert --to notebook --execute train_model.ipynb --output train_model.ipynb

# Or open interactively
uv run jupyter notebook train_model.ipynb
```

### After training

Rebuild the predictor to bake in the new model:

```bash
docker compose up --build predictor -d
```

Verify the model loaded (no fallback warning):

```bash
docker logs predictor 2>&1 | head -10
# Should show: "Model loaded from /app/model/model.joblib"
```

### Model performance note

The baseline model has near-random performance (AUC ~0.47) because our synthetic data generates fraud labels **independently** of the features — there's no real signal. This is expected and fine for validating the pipeline. With real data or engineered correlations, the model would learn meaningful patterns.

### Output files

| File | Gitignored | Description |
|------|------------|-------------|
| `services/predictor/model/model.joblib` | Yes | Trained model artifact loaded by the predictor |
| `services/predictor/model/feature_schema.json` | No | Feature names and types for auditing train/serve consistency |
