# Feature Store — Design & Architecture

## Overview

The feature store is the bridge between raw transactions and ML predictions. It answers the question: **"Is this transaction unusual for this user?"**

A raw transaction alone (amount, card type, merchant) tells you very little. The model needs **behavioral context** — how does this transaction compare to what this user normally does? The feature store provides that context by maintaining **per-user rolling aggregates** in Redis.

This module (`services/predictor/app/features.py`) is the **single source of truth** for feature definitions. The same code runs at training time (notebook) and inference time (predictor service). This prevents **train/serve skew** — the #1 cause of silent ML production failures.

---

## Why Not Just Use Raw Transaction Fields?

Consider two $5,000 transactions:

| User | Typical spend | $5,000 transaction | Suspicious? |
|---|---|---|---|
| user_0001 | $15/day (college student) | 333x their average | Very |
| user_0002 | $4,800/day (corporate card) | 1.04x their average | No |

The raw amount ($5,000) is identical. But the **context** — how it compares to the user's history — is completely different. That's what rolling aggregates provide.

---

## Features Computed

Four features are computed for every incoming transaction:

| Feature | Name constant | Description | Why it matters |
|---|---|---|---|
| `txn_count_1h` | `FEAT_TXN_COUNT_1H` | Number of transactions by this user in the last 1 hour | Rapid-fire transactions (card testing attacks) produce high counts |
| `txn_count_24h` | `FEAT_TXN_COUNT_24H` | Number of transactions by this user in the last 24 hours | Sustained unusual activity over a day |
| `avg_amount_24h` | `FEAT_AVG_AMOUNT_24H` | Average transaction amount for this user over 24 hours | Establishes the user's baseline spending level |
| `amount_vs_avg_ratio` | `FEAT_AMOUNT_VS_AVG_RATIO` | Current amount / avg_amount_24h | The "spike detector" — a ratio of 10 means 10x the user's typical spend |

### Feature Name Constants

All feature names are defined as Python constants and exported in the `FEATURE_NAMES` list:

```python
from app.features import FEATURE_NAMES
# ['txn_count_1h', 'txn_count_24h', 'avg_amount_24h', 'amount_vs_avg_ratio']
```

This ordered list is used everywhere:
- **Training notebook**: to build the feature matrix columns in the correct order
- **Predictor service**: to construct the input vector for model inference
- **MLflow**: logged as a schema artifact for auditing

Using constants (not raw strings) means a typo causes an `ImportError` at startup — not a silent mismatch between training and serving that produces garbage predictions for weeks.

---

## Redis Data Model

### Why Redis?

The predictor has a **50ms latency budget** for the entire inference pipeline. Feature computation must be fast:

| Option | Read latency | Problem |
|---|---|---|
| PostgreSQL | 5–20ms | Eats the latency budget, leaves nothing for inference |
| Kafka replay | 100ms+ | Scanning a topic is sequential, not indexed by user |
| In-memory dict | ~0.01ms | Lost on restart, can't share across predictor replicas |
| **Redis** | **~0.2ms** | In-memory, persistent, shared, built-in sorted sets |

### Sorted Sets (ZSET)

Redis sorted sets are the perfect data structure for time-windowed aggregates. Each member has a **score** (we use the Unix timestamp), and Redis keeps them sorted. This enables efficient range queries by time.

```
Redis Key:    user:user_0042:txns
Type:         Sorted Set (ZSET)

┌─────────────────────────────────────────────────────────────┐
│  Score (timestamp)    │  Member (JSON string)               │
├───────────────────────┼─────────────────────────────────────┤
│  1709474521.0         │  {"txn_id":"abc-1","amount":12.50}  │
│  1709474893.0         │  {"txn_id":"abc-2","amount":67.23}  │
│  1709475210.0         │  {"txn_id":"abc-3","amount":5.00}   │
│  1709478121.0         │  {"txn_id":"abc-4","amount":245.00} │
└───────────────────────┴─────────────────────────────────────┘
          ▲                           ▲
     Sorted by this            Data payload
```

### Key Naming Convention

```
user:{user_id}:txns
```

Example: `user:user_0042:txns`

Each user gets their own sorted set. This provides natural isolation — user A's data never mixes with user B's.

### Redis Commands Used

| Command | Purpose | Complexity |
|---|---|---|
| `ZADD key {member: score}` | Add a transaction | O(log N) |
| `ZRANGEBYSCORE key min max WITHSCORES` | Get all transactions in a time window | O(log N + M) where M = results |
| `ZREMRANGEBYSCORE key -inf cutoff` | Delete expired entries | O(log N + M) where M = deleted |
| `EXPIRE key ttl` | Set key TTL for inactive user cleanup | O(1) |

---

## Two Paths: Write and Read

### Write Path — `update_user_features()`

Called every time a new transaction arrives. Three operations in sequence:

```
New transaction arrives
       │
       ▼
ZADD user:user_0042:txns {json_payload: unix_timestamp}
       │  Add the transaction to the sorted set
       │
       ▼
ZREMRANGEBYSCORE user:user_0042:txns -inf (now - 86400)
       │  Remove any entries older than 24 hours
       │  This bounds memory usage — each user stores at most 24h of data
       │
       ▼
EXPIRE user:user_0042:txns 172800
       │  Reset TTL to 48 hours
       │  If a user goes completely inactive, Redis auto-deletes the key
       │
       Done
```

**Why clean up on write?** Every write is an opportunity to evict stale data. This is cheaper than running a separate background cleanup job, and it keeps memory bounded proportionally to active users.

**Function signature:**

```python
def update_user_features(
    redis_client: Redis,
    user_id: str,          # "user_0042"
    transaction_id: str,   # "abc-123" (used as part of the JSON member)
    amount: float,         # 67.23
    timestamp: float | None = None,  # Unix timestamp, defaults to time.time()
) -> None:
```

### Read Path — `get_user_features()`

Called to build the feature vector before model inference. One Redis call, then pure math:

```
Need features for user_0042 (current txn = $500)
       │
       ▼
ZRANGEBYSCORE user:user_0042:txns (now - 86400) +inf WITHSCORES
       │  Fetch all transactions in the 24h window
       │  Returns: [(json_str, timestamp), (json_str, timestamp), ...]
       │
       ▼
Parse JSON, compute aggregates:
       │
       ├── txn_count_24h = len(results)                    → 8
       ├── txn_count_1h  = count where score >= (now-3600) → 3
       ├── avg_amount_24h = sum(amounts) / count           → $62.50
       └── amount_vs_avg_ratio = 500 / 62.50               → 8.0
       │
       ▼
Return: {
    "txn_count_1h": 3.0,
    "txn_count_24h": 8.0,
    "avg_amount_24h": 62.5,
    "amount_vs_avg_ratio": 8.0
}
```

**Why one Redis call?** We fetch the full 24h window in a single `ZRANGEBYSCORE` and derive the 1h count by filtering in Python. This avoids a second round-trip to Redis. At 20 transactions/second, saving one round-trip per transaction adds up.

**Function signature:**

```python
def get_user_features(
    redis_client: Redis,
    user_id: str,           # "user_0042"
    current_amount: float,  # 500.0 (the transaction being scored)
    timestamp: float | None = None,
) -> dict[str, float]:      # {"txn_count_1h": 3.0, ...}
```

---

## Edge Cases and How They're Handled

### First Transaction for a User

When a user has no history in Redis, `ZRANGEBYSCORE` returns an empty list.

```
txn_count_1h    = 0.0
txn_count_24h   = 0.0
avg_amount_24h  = 0.0
amount_vs_avg_ratio = 1.0  ← NOT current_amount / 0 (division by zero)
```

The ratio defaults to **1.0** (neutral). This is a deliberate design choice: we don't want the model to flag every new user's first transaction as anomalous. The model should learn from other features (amount, card type, hour of day) for first-time users.

### Zero-Dollar Transactions

If a user has only $0 transactions in their history, `avg_amount_24h = 0.0`, which would cause division by zero in the ratio. We guard against this:

```python
if avg_amount_24h > 0:
    amount_vs_avg_ratio = current_amount / avg_amount_24h
else:
    amount_vs_avg_ratio = 1.0  # neutral fallback
```

### Memory Bounding

Without cleanup, a user making 20 transactions/second would accumulate 1.7M entries/day. Two mechanisms prevent unbounded growth:

1. **`ZREMRANGEBYSCORE` on every write** — removes entries older than 24h. A user's sorted set holds at most ~24h worth of transactions.
2. **`EXPIRE` with 48h TTL** — if a user goes completely inactive (no new transactions), Redis auto-deletes the entire key after 48 hours.

At 20 txns/sec with 1000 users, worst case is ~1.7M entries across all users at any time. Each entry is ~80 bytes of JSON + 8 bytes score = ~150MB total. Comfortably fits in Redis.

### User Isolation

Each user gets their own Redis key (`user:{user_id}:txns`). There is no shared state between users. This means:
- User A's transactions never affect User B's features
- A bug in one user's data can't corrupt another user
- You can delete a single user's data with `DEL user:user_0042:txns`

---

## How It Fits in the Pipeline

```
                    Phase 2                  Phase 3              Phase 4
                  ┌──────────┐          ┌──────────────┐     ┌──────────────┐
                  │ Producer │          │    Redis     │     │  Predictor   │
                  │          │          │ Feature Store│     │              │
                  └────┬─────┘          └──────┬───────┘     └──────┬───────┘
                       │                       │                    │
  1. Generate txn      │                       │                    │
  2. Publish ─────────>│ Kafka topic           │                    │
                       │ (transactions_raw)    │                    │
                       │                       │    3. Consume ────>│
                       │                       │                    │
                       │                       │<── 4. WRITE ──────│
                       │                       │    (update_user_   │
                       │                       │     features)      │
                       │                       │                    │
                       │                       │─── 5. READ ──────>│
                       │                       │    (get_user_      │
                       │                       │     features)      │
                       │                       │                    │
                       │                       │              6. Model inference
                       │                       │              7. Publish result
                       │ Kafka topic           │<──────────────────│
                       │ (transactions_scored) │                    │
```

The predictor (Phase 4) will call both functions for every transaction:
1. `update_user_features()` — record this transaction in Redis
2. `get_user_features()` — read back the rolling aggregates
3. Pass the feature dict to the model for inference

**Important ordering**: we write first, then read. This means the current transaction is included in the aggregates. The model is trained with this same convention.

---

## Dependency Injection — Why `redis_client` Is a Parameter

Both functions take `redis_client` as their first argument instead of creating a global Redis connection:

```python
def update_user_features(redis_client: Redis, user_id: str, ...) -> None:
def get_user_features(redis_client: Redis, user_id: str, ...) -> dict[str, float]:
```

This is deliberate. It enables:

1. **Testing with fakeredis** — tests pass a `fakeredis.FakeRedis()` instance. No real Redis needed, no Docker, no network. Tests run in <1 second.

2. **Reuse across contexts** — the training notebook can pass a different Redis client (or a fakeredis populated with synthetic history). The predictor passes its production Redis connection.

3. **No hidden global state** — the functions are pure (given the same Redis state, they return the same result). This makes debugging straightforward.

---

## Testing Strategy

Tests live in `services/predictor/tests/test_features.py` and use **fakeredis** — an in-memory Python implementation of Redis that behaves identically to real Redis but requires no running server.

### Test Categories

#### Basic Feature Computation (3 tests)

| Test | Scenario | Validates |
|---|---|---|
| `test_no_history_returns_defaults` | Brand new user, no Redis data | All features return safe defaults (0 counts, 1.0 ratio) |
| `test_single_transaction` | One prior transaction | count=1, correct avg, correct ratio (200/100 = 2.0) |
| `test_multiple_transactions_average` | Three transactions: $100, $200, $300 | avg = $200, ratio = 150/200 = 0.75 |

#### Time Window Correctness (2 tests)

| Test | Scenario | Validates |
|---|---|---|
| `test_1h_window_excludes_old` | Txn from 2h ago + txn from 30min ago | 1h count = 1, 24h count = 2 |
| `test_24h_window_excludes_expired` | Txn from 25h ago + txn from 1min ago | 25h-old txn is cleaned up, only fresh txn counted |

#### Edge Cases (4 tests)

| Test | Scenario | Validates |
|---|---|---|
| `test_zero_amount_transaction` | User's only transaction was $0 | avg=0, ratio falls back to 1.0 (no division by zero) |
| `test_different_users_are_isolated` | User A has $500 avg, User B has $10 avg | Features are completely independent per user |
| `test_feature_names_all_present` | Any call to `get_user_features` | Returns all 4 feature keys, no missing/extra |
| `test_large_spike_ratio` | User averages $10, current txn is $5,000 | ratio = 500.0 (correctly captures the anomaly) |

### Running Tests

```bash
# From project root
uv run pytest services/predictor/tests/test_features.py -v

# Expected output:
# 9 passed in ~0.7s
```

### Why fakeredis Over Mocking?

We could mock the Redis client with `unittest.mock`, but that tests our **mocking code**, not our **feature logic**. fakeredis actually executes `ZADD`, `ZRANGEBYSCORE`, and `ZREMRANGEBYSCORE` with correct semantics. If we wrote a query wrong (e.g., off-by-one in the time range), fakeredis would catch it. A mock would silently return whatever we told it to.

---

## Design Decisions

### Why sorted sets instead of separate keys per time bucket?

**Alternative**: Store counts in keys like `user:0042:count:2026-03-04T14` (one key per hour).

**Problem**: Time buckets create boundary issues. A transaction at 14:59:59 and a query at 15:00:01 would miss it if they're in different buckets. Sorted sets with timestamp scores give exact sub-second precision for any window.

### Why clean up on write instead of a background job?

**Alternative**: Run a cron job every hour to `ZREMRANGEBYSCORE` across all users.

**Problem**: Requires maintaining a separate process, tracking all active user keys, and the cleanup job itself takes time proportional to total users. Cleaning on write is O(1) amortized (each write cleans only its own key) and requires no coordination.

### Why 24h as the max window?

Fraud patterns in the real world are mostly **short-lived** — a stolen card gets used rapidly before it's blocked. A 24h window captures:
- Card testing attacks (many small transactions in minutes)
- Spending sprees (large purchases over hours)
- Geographic anomalies (purchases in two countries within hours)

Longer windows (7-day, 30-day) add little signal but dramatically increase memory usage and computation cost.

### Why not store the full transaction in Redis?

We only store `{"txn_id": "...", "amount": ...}` — not the full transaction with card type, merchant, timestamp fields, etc. The aggregates we compute only need the amount. Keeping the member payload small means:
- Less memory per user
- Faster serialization/deserialization
- Less data transferred per `ZRANGEBYSCORE` call

If we later need aggregates over other fields (e.g., unique merchant count), we can add them to the member JSON without changing the data model.

---

## File Reference

```
services/predictor/
├── app/
│   ├── __init__.py
│   ├── features.py          ← THIS MODULE — feature definitions + Redis logic
│   └── main.py              ← Predictor entry point (Phase 4)
├── tests/
│   ├── __init__.py
│   └── test_features.py     ← 9 tests covering computation, windows, edge cases
├── pyproject.toml            ← Dependencies: redis>=5.0
└── Dockerfile                ← Container image (Phase 4)

Root pyproject.toml:
  dev dependencies: fakeredis>=2.21 (for testing)
```
