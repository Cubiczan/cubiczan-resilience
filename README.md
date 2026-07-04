# cubiczan-resilience

Shared resilience primitives for the portfolio — built once, adopted everywhere.
Closes the two most common defects found in the architecture audit: external calls
with no timeout/retry/backoff, and money/state operations with no idempotency.

| Package | Language | Provides |
|---|---|---|
| `typescript/` | TypeScript | `safeFetch()` (timeout + retry/backoff + SSRF allowlist), `requireAuth()` (fail-closed bearer + in-memory rate limit) |
| `python/`     | Python (pip: `cubiczan-resilience`) | `@resilient` (timeout + backoff-with-jitter + circuit breaker), idempotency key store, atomic file write, FastAPI auth dependency + CORS allowlist factory |
| `rust/`       | Rust crate `resilient-call` | timeout, backoff+jitter, CockroachDB serializable-retry (SQLSTATE 40001), idempotency ledger check |
| `onchain/`    | TS + Python | bounded retry + gas/fee bump, nonce management, tx-success assertion, off-chain circuit breaker |

Lifted and generalized from: `cfo-resilience-matrix`, `strata-aws-native`, `hermes-pi-factory-guardian`, `cross-harness-scaffolder`, `valiron-advisory-ai`, `agent-conductor`, `critmin-oracle`.

## Audit Ledger

A signed, append-only **JSONL audit ledger** shipped in all three languages
(`typescript/`, `python/`, `rust/`). Generalized from the HMAC-SHA256 audit
ledgers in `cleanmandate`, `swarmfi-executor`, `glacier-edge-arm`, and
`compliance-as-code-agent` (`*-core/src/audit.rs`), which each write one JSON
line per decision/event. This adds **signature chaining** so the ledger is
tamper-evident: an interior line cannot be edited, reordered, or deleted without
breaking every signature that follows it.

### Scheme

Each record is one JSON line with shape:

```json
{ "ts", "event", "actor", "inputs", "sources", "confidence?", "rationale?", "prev_sig", "sig" }
```

The signature is computed over a **canonical JSON** encoding (object keys sorted,
no insignificant whitespace) of the content fields plus the *previous* record's
signature — never over `sig` itself:

```text
canonical = canonical_json({ ts, event, actor, inputs, sources, confidence?, rationale?, prev_sig })
sig       = hex( HMAC-SHA256(key, canonical) )
```

- The genesis (first) record uses `prev_sig = ""`.
- Because each `sig` folds in the prior `sig`, the lines form a hash chain:
  `verify` re-walks the file, recomputes every signature, and returns the
  zero-based index of the first line whose signature — or whose `prev_sig` chain
  link — is broken.
- Optional fields (`confidence`, `rationale`) are omitted from the canonical
  payload when absent, so the signed bytes are identical across languages. The
  three ports assert a shared golden signature vector so they can never drift.
- The signing key resolves from the `key` argument, then the `AUDIT_LEDGER_KEY`
  environment variable, then a documented insecure default
  (`cubiczan-resilience-insecure-default-key`) that is intended for tests/dev
  only — set `AUDIT_LEDGER_KEY` in production.

### TypeScript

```ts
import { AuditLedger, verifyLedger } from "@cubiczan/resilience";

const ledger = new AuditLedger({ path: ".state/audit.jsonl" }); // key from AUDIT_LEDGER_KEY
const sig = ledger.append({
  event: "approve_payout",
  actor: "cfo-agent",
  inputs: { amount: 1000, vendor: "acme" },
  sources: ["invoice:inv-42"],
  confidence: 0.97,
  rationale: "within policy limit",
});

const result = ledger.verify(); // or verifyLedger(path, key)
if (!result.ok) console.error("tampered at line", result.tamperedIndex);
```

### Python

```python
from cubiczan_resilience import AuditLedger, verify_ledger

ledger = AuditLedger(".state/audit.jsonl")  # key from AUDIT_LEDGER_KEY
sig = ledger.append(
    "approve_payout",
    "cfo-agent",
    inputs={"amount": 1000, "vendor": "acme"},
    sources=["invoice:inv-42"],
    confidence=0.97,
    rationale="within policy limit",
)

result = ledger.verify()  # or verify_ledger(path, key=...)
if not result.ok:
    print("tampered at line", result.tampered_index)
```

### Rust

```rust
use resilient_call::{AuditLedger, AuditRecordInput, verify_ledger};

let ledger = AuditLedger::open(".state/audit.jsonl", None)?; // key from AUDIT_LEDGER_KEY
let sig = ledger.append(AuditRecordInput {
    event: "approve_payout".into(),
    actor: "cfo-agent".into(),
    inputs: Some(serde_json::json!({ "amount": 1000, "vendor": "acme" })),
    sources: Some(serde_json::json!(["invoice:inv-42"])),
    confidence: Some(0.97),
    rationale: Some("within policy limit".into()),
    ..Default::default()
})?;

let result = ledger.verify()?; // or verify_ledger(path, Some(key))
if !result.is_ok() {
    eprintln!("tampered at line {:?}", result.tampered_index());
}
```
