# resilient-call

Small, dependency-light **resilience primitives** for the cubiczan portfolio.

Closes the two most common defects from the architecture audit:

1. external calls with no **timeout / retry / backoff**, and
2. money/state operations with no **idempotency** guard.

Lifted and generalized from proven patterns in `cross-harness-scaffolder`
(CockroachDB serializable-retry) and `swarmfi-executor` / `cleanmandate`
(idempotency ledger). The CRDB retry in the scaffolder hard-coded its delay
schedule and had **no jitter**; this crate adds full jitter and a backoff cap.

## What you get

| API | Purpose |
|---|---|
| `retry(op, &policy, classify)` | async generic retry: exponential backoff + **full jitter**, max attempts, classifier closure deciding retryable vs terminal |
| `with_timeout(fut, dur)` | tokio timeout wrapper → typed `ResilienceError::Timeout` |
| `crdb_retry(op)` | CockroachDB serializable retry; retries **only** on SQLSTATE `40001`, capped backoff + jitter |
| `IdempotencyLedger` / `FileLedger` | JSONL-backed `contains(key)` / `record(key)` guard for money/state ops |
| `AuditLedger` / `verify_ledger` | signed, append-only JSONL audit ledger; HMAC-SHA256 over canonical JSON **+ the prior signature**, chaining lines so tampering is detectable |

Minimal deps: `tokio`, `rand`, `thiserror`, `serde`/`serde_json`, plus
`hmac`/`sha2`/`hex` for the audit ledger. No network deps.

## Add it

```toml
[dependencies]
resilient-call = { path = "../cubiczan-resilience/rust" }
```

## Examples

### Retry with backoff + jitter and a classifier

```rust
use resilient_call::{retry, RetryPolicy, ResilienceError};

let policy = RetryPolicy::with_max_attempts(5); // base 50ms, cap 2s, full jitter

let result: Result<Bytes, ResilienceError<HttpError>> = retry(
    || async { http_client.get(url).await },     // re-runnable per attempt
    &policy,
    |e: &HttpError| e.is_transient(),             // 5xx/timeout retryable, 4xx terminal
).await;

match result {
    Ok(body) => { /* ... */ }
    Err(ResilienceError::Exhausted { attempts, source }) => { /* gave up */ }
    Err(ResilienceError::Terminal(e)) => { /* non-retryable, surfaced at once */ }
    Err(ResilienceError::Timeout(_)) => unreachable!(),
}
```

### Timeout wrapper

```rust
use resilient_call::{with_timeout, ResilienceError};
use std::time::Duration;

match with_timeout(slow_call(), Duration::from_secs(2)).await {
    Ok(v) => { /* ... */ }
    Err(ResilienceError::Timeout(d)) => eprintln!("timed out after {d:?}"),
    Err(other) => { /* underlying error */ }
}
```

### CockroachDB serializable retry (SQLSTATE 40001 only)

```rust
use resilient_call::{crdb_retry, SqlError};

let row = crdb_retry(|| async {
    // open + run + commit a SERIALIZABLE transaction.
    // Map driver errors into SqlError { sqlstate, message }.
    run_txn().await.map_err(|e| SqlError::new(e.sqlstate(), e.to_string()))
}).await?;
// Retried on 40001 (serialization_failure) with capped backoff + jitter.
// Any other SQLSTATE (e.g. 23505 unique_violation) is terminal immediately.
```

### Idempotency ledger (guard money/state ops)

```rust
use resilient_call::{FileLedger, IdempotencyLedger};

let ledger = FileLedger::open(".state/idempotency.jsonl")?;
let key = format!("payment:{}", idempotency_key);

if ledger.contains(&key)? {
    return Ok(prior_result()); // idempotent replay: do NOT re-charge
}
charge_card(amount)?;          // the side effect
ledger.record(&key)?;          // mark done so retries are blocked
```

### Audit ledger (signed, append-only, tamper-evident)

```rust
use resilient_call::{AuditLedger, AuditRecordInput, verify_ledger};

let ledger = AuditLedger::open(".state/audit.jsonl", None)?; // key from AUDIT_LEDGER_KEY
let _sig = ledger.append(AuditRecordInput {
    event: "approve_payout".into(),
    actor: "cfo-agent".into(),
    inputs: Some(serde_json::json!({ "amount": 1000 })),
    sources: Some(serde_json::json!(["invoice:inv-42"])),
    confidence: Some(0.97),
    rationale: Some("within policy limit".into()),
    ..Default::default()
})?;

// Each record's sig = HMAC-SHA256(key, canonical_json(record + prev_sig)),
// so lines form a hash chain. verify() re-walks and flags the first bad line.
match ledger.verify()? {
    r if r.is_ok() => { /* intact */ }
    r => eprintln!("tampered at line {:?}", r.tampered_index()),
}
// Or without an instance: verify_ledger(path, Some(key))?.
```

The signing key resolves from the `open(_, key)` argument, then the
`AUDIT_LEDGER_KEY` env var, then a documented insecure default meant for
tests/dev only. See the [top-level README](../README.md#audit-ledger) for the
full scheme shared across the TS / Python / Rust ports.

## Tests

```sh
cargo check
cargo test
```

Covered: retry succeeds after N transient failures; gives up after max
attempts; terminal errors short-circuit; timeout fires (and passes fast
successes through); `crdb_retry` retries on `40001` but not other SQLSTATEs;
idempotency ledger blocks duplicate keys and persists across reopen; the audit
ledger appends N records and verifies intact, chains each line to the prior
signature, resumes the chain across reopen, and detects an edited payload, a
deleted interior line, and a wrong key — each at the correct line index.

## License

MIT OR Apache-2.0
