import { describe, it, expect, vi } from "vitest";
import {
  resolveTiered,
  AllTiersFailedError,
  type TierFailure,
} from "../src/tieredSource.js";

const boom = (msg = "upstream down") => () => Promise.reject(new Error(msg));

describe("resolveTiered", () => {
  it("returns the live value with a live badge and no failures", async () => {
    const r = await resolveTiered({ live: async () => 42 });
    expect(r).toMatchObject({
      value: 42,
      tier: "live",
      degraded: false,
      stale: false,
      badge: "live",
    });
    expect(r.failures).toEqual([]);
  });

  it("never consults the cache when live succeeds", async () => {
    const cache = vi.fn();
    await resolveTiered({ live: async () => 1, cache });
    expect(cache).not.toHaveBeenCalled();
  });

  it("falls back to cache and marks the result degraded", async () => {
    const r = await resolveTiered({
      live: boom(),
      cache: async () => ({ value: 7, cachedAt: Date.now() }),
    });
    expect(r.value).toBe(7);
    expect(r.tier).toBe("cache");
    expect(r.degraded).toBe(true);
    expect(r.stale).toBe(false);
    expect(r.failures.map((f: TierFailure) => f.tier)).toEqual(["live"]);
  });

  it("flags a cache hit older than maxCacheAge as stale but still returns it", async () => {
    const r = await resolveTiered({
      live: boom(),
      cache: async () => ({ value: 7, cachedAt: Date.now() - 60_000 }),
      maxCacheAge: 1_000,
    });
    expect(r.value).toBe(7);
    expect(r.stale).toBe(true);
    expect(r.badge).toContain("stale");
  });

  it("accepts a bare cached value with no timestamp", async () => {
    const r = await resolveTiered({ live: boom(), cache: async () => 7 });
    expect(r.value).toBe(7);
    expect(r.tier).toBe("cache");
    expect(r.ageMs).toBeUndefined();
    expect(r.stale).toBe(false);
    expect(r.badge).toBe("cache");
  });

  it("treats null/undefined from cache as a miss, not a value", async () => {
    for (const empty of [null, undefined]) {
      const r = await resolveTiered({
        live: boom(),
        cache: async () => empty,
        mock: () => -1,
      });
      expect(r.tier).toBe("mock");
      expect(r.failures.map((f) => f.tier)).toEqual(["live", "cache"]);
    }
  });

  it("does NOT treat falsy-but-real values as a miss", async () => {
    // 0 and "" are legitimate values; only null/undefined mean "no entry".
    const zero = await resolveTiered({ live: boom(), cache: async () => 0 });
    expect(zero.tier).toBe("cache");
    expect(zero.value).toBe(0);

    const empty = await resolveTiered({ live: boom(), cache: async () => "" });
    expect(empty.tier).toBe("cache");
    expect(empty.value).toBe("");
  });

  it("skips a cache hit beyond maxCacheAgeHard and falls through to mock", async () => {
    const r = await resolveTiered({
      live: boom(),
      cache: async () => ({ value: 7, cachedAt: Date.now() - 60_000 }),
      maxCacheAgeHard: 1_000,
      mock: () => -1,
    });
    expect(r.tier).toBe("mock");
    expect(r.value).toBe(-1);
    expect(r.badge).toBe("mock data — not real");
  });

  it("throws rather than fabricating data when no mock is configured", async () => {
    await expect(
      resolveTiered({ live: boom("live gone"), cache: boom("cache gone") }),
    ).rejects.toBeInstanceOf(AllTiersFailedError);
  });

  it("reports every tier failure on the thrown error", async () => {
    const err = await resolveTiered({
      live: boom("live gone"),
      cache: boom("cache gone"),
    }).catch((e) => e as AllTiersFailedError);
    expect(err.failures.map((f) => f.tier)).toEqual(["live", "cache"]);
    expect(err.message).toContain("live gone");
    expect(err.message).toContain("cache gone");
  });

  it("invokes onFailure once per failed tier, in attempt order", async () => {
    const seen: string[] = [];
    await resolveTiered({
      live: boom(),
      cache: boom(),
      mock: () => 0,
      onFailure: (f) => seen.push(f.tier),
    });
    expect(seen).toEqual(["live", "cache"]);
  });

  it("wraps a non-Error throw so callers always get an Error", async () => {
    const err = await resolveTiered({
      // eslint-disable-next-line @typescript-eslint/no-throw-literal
      live: async () => {
        throw "just a string";
      },
    }).catch((e) => e as AllTiersFailedError);
    expect(err.failures[0].error).toBeInstanceOf(Error);
    expect(err.failures[0].error.message).toBe("just a string");
  });

  it("falls through to mock when the cache itself throws", async () => {
    const r = await resolveTiered({
      live: boom(),
      cache: boom("cache exploded"),
      mock: () => 5,
    });
    expect(r.tier).toBe("mock");
    expect(r.failures.map((f) => f.error.message)).toContain("cache exploded");
  });

  it("accepts a synchronous mock", async () => {
    const r = await resolveTiered({ live: boom(), mock: () => 3 });
    expect(r.value).toBe(3);
    expect(r.tier).toBe("mock");
  });

  it("always labels the tier — there is no unlabelled path", async () => {
    const results = await Promise.all([
      resolveTiered({ live: async () => 1 }),
      resolveTiered({ live: boom(), cache: async () => 2 }),
      resolveTiered({ live: boom(), cache: boom(), mock: () => 3 }),
    ]);
    expect(results.map((r) => r.tier)).toEqual(["live", "cache", "mock"]);
    for (const r of results) expect(r.badge).toBeTruthy();
  });
});
