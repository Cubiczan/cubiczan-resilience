import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { mkdtempSync, rmSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  AuditLedger,
  verifyLedger,
  DEFAULT_AUDIT_LEDGER_KEY,
  type AuditRecord,
} from "../src/auditLedger.js";

const KEY = "test-key-0123456789";

let dir: string;
let path: string;

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), "audit-ledger-"));
  path = join(dir, "audit.jsonl");
});

afterEach(() => {
  rmSync(dir, { recursive: true, force: true });
});

function readRecords(p: string): AuditRecord[] {
  return readFileSync(p, "utf-8")
    .split("\n")
    .filter((l) => l.trim() !== "")
    .map((l) => JSON.parse(l) as AuditRecord);
}

describe("AuditLedger append + verify", () => {
  it("appends N records and verify passes", () => {
    const ledger = new AuditLedger({ path, key: KEY });
    for (let i = 0; i < 5; i++) {
      ledger.append({
        event: "decision",
        actor: "agent-1",
        inputs: { i },
        sources: ["src-a"],
        confidence: 0.9,
        rationale: `step ${i}`,
      });
    }
    const result = ledger.verify();
    expect(result.ok).toBe(true);
    if (result.ok) expect(result.count).toBe(5);
  });

  it("chains each record to the prior signature", () => {
    const ledger = new AuditLedger({ path, key: KEY });
    const s0 = ledger.append({ event: "e0", actor: "a" });
    const s1 = ledger.append({ event: "e1", actor: "a" });

    const recs = readRecords(path);
    expect(recs[0].prev_sig).toBe(""); // genesis
    expect(recs[0].sig).toBe(s0);
    expect(recs[1].prev_sig).toBe(s0); // links to prior sig
    expect(recs[1].sig).toBe(s1);
  });

  it("resumes the chain across instances", () => {
    const l1 = new AuditLedger({ path, key: KEY });
    l1.append({ event: "e0", actor: "a" });
    l1.append({ event: "e1", actor: "a" });

    const l2 = new AuditLedger({ path, key: KEY });
    l2.append({ event: "e2", actor: "a" });

    const result = verifyLedger(path, KEY);
    expect(result.ok).toBe(true);
    if (result.ok) expect(result.count).toBe(3);
  });

  it("verify passes on an empty/absent ledger", () => {
    const result = verifyLedger(path, KEY);
    expect(result.ok).toBe(true);
    if (result.ok) expect(result.count).toBe(0);
  });
});

describe("AuditLedger tamper detection", () => {
  it("detects an edited payload at the right index", () => {
    const ledger = new AuditLedger({ path, key: KEY });
    for (let i = 0; i < 4; i++) {
      ledger.append({ event: "decision", actor: "agent-1", inputs: { i } });
    }
    const recs = readRecords(path);
    // Tamper with line index 2's content, leaving its sig untouched.
    recs[2] = { ...recs[2], actor: "attacker" };
    writeFileSync(path, recs.map((r) => JSON.stringify(r)).join("\n") + "\n");

    const result = verifyLedger(path, KEY);
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.tamperedIndex).toBe(2);
  });

  it("detects a deleted interior line via a broken chain link", () => {
    const ledger = new AuditLedger({ path, key: KEY });
    for (let i = 0; i < 4; i++) {
      ledger.append({ event: "decision", actor: "agent-1", inputs: { i } });
    }
    const recs = readRecords(path);
    recs.splice(1, 1); // drop line index 1
    writeFileSync(path, recs.map((r) => JSON.stringify(r)).join("\n") + "\n");

    const result = verifyLedger(path, KEY);
    expect(result.ok).toBe(false);
    // Former line 2 (now at index 1) has a prev_sig that no longer matches.
    if (!result.ok) expect(result.tamperedIndex).toBe(1);
  });

  it("fails verification under the wrong key", () => {
    const ledger = new AuditLedger({ path, key: KEY });
    ledger.append({ event: "e0", actor: "a" });
    const result = verifyLedger(path, "the-wrong-key");
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.tamperedIndex).toBe(0);
  });
});

describe("AuditLedger cross-language wire format", () => {
  it("matches the shared golden signature vector", () => {
    // The Python and Rust ports assert this exact signature too, so the three
    // implementations can never silently drift on canonicalization / signing.
    const ledger = new AuditLedger({ path, key: "k" });
    const sig = ledger.append({
      event: "e",
      actor: "a",
      inputs: { x: 1 },
      sources: ["s"],
      ts: "2026-01-01T00:00:00Z",
    });
    expect(sig).toBe(
      "d379966f5be33822aa1091efa18034e67e679fbadb168bb73c3f42ef712a46fc",
    );
  });
});

describe("AuditLedger key resolution", () => {
  it("falls back to the documented default key", () => {
    const l1 = new AuditLedger({ path, key: DEFAULT_AUDIT_LEDGER_KEY });
    l1.append({ event: "e0", actor: "a" });
    // No key passed -> resolves to env or the default; here env is unset.
    const result = verifyLedger(path);
    expect(result.ok).toBe(true);
  });
});
