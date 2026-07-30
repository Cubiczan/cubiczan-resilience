"""Tests for the three-tier live → cache → mock resolver."""

from __future__ import annotations

import time

import pytest

from cubiczan_resilience.tiered import (
    MISS,
    AllTiersFailed,
    Cached,
    resolve_tiered,
)


def boom(msg: str = "upstream down"):
    def _raise():
        raise RuntimeError(msg)

    return _raise


def test_live_hit_is_not_degraded():
    r = resolve_tiered(live=lambda: 42)
    assert (r.value, r.tier, r.degraded, r.stale) == (42, "live", False, False)
    assert r.badge == "live"
    assert r.failures == ()


def test_cache_is_not_consulted_when_live_succeeds():
    calls = []
    resolve_tiered(live=lambda: 1, cache=lambda: calls.append("hit"))
    assert calls == []


def test_falls_back_to_cache_and_marks_degraded():
    r = resolve_tiered(live=boom(), cache=lambda: Cached(7, time.time()))
    assert r.value == 7
    assert r.tier == "cache"
    assert r.degraded is True
    assert r.stale is False
    assert [f.tier for f in r.failures] == ["live"]


def test_old_cache_hit_is_stale_but_still_returned():
    r = resolve_tiered(
        live=boom(),
        cache=lambda: Cached(7, time.time() - 3600),
        max_cache_age=1.0,
    )
    assert r.value == 7
    assert r.stale is True
    assert "stale" in r.badge


def test_bare_cached_value_has_no_age():
    r = resolve_tiered(live=boom(), cache=lambda: 7)
    assert (r.value, r.tier, r.age, r.stale) == (7, "cache", None, False)
    assert r.badge == "cache"


def test_none_from_cache_is_a_miss_by_default():
    r = resolve_tiered(live=boom(), cache=lambda: None, mock=lambda: -1)
    assert r.tier == "mock"
    assert [f.tier for f in r.failures] == ["live", "cache"]


def test_falsy_but_real_values_are_not_treated_as_a_miss():
    # 0 and "" are legitimate cached values.
    assert resolve_tiered(live=boom(), cache=lambda: 0).value == 0
    assert resolve_tiered(live=boom(), cache=lambda: "").tier == "cache"


def test_miss_sentinel_allows_a_cached_none_to_be_a_real_value():
    r = resolve_tiered(live=boom(), cache=lambda: None, miss_sentinel=MISS)
    assert r.tier == "cache"
    assert r.value is None


def test_hard_age_limit_skips_cache_and_falls_through_to_mock():
    r = resolve_tiered(
        live=boom(),
        cache=lambda: Cached(7, time.time() - 3600),
        max_cache_age_hard=1.0,
        mock=lambda: -1,
    )
    assert r.tier == "mock"
    assert r.value == -1
    assert r.badge == "mock data — not real"


def test_raises_rather_than_fabricating_when_no_mock_configured():
    with pytest.raises(AllTiersFailed) as exc:
        resolve_tiered(live=boom("live gone"), cache=boom("cache gone"))
    assert [f.tier for f in exc.value.failures] == ["live", "cache"]
    assert "live gone" in str(exc.value)
    assert "cache gone" in str(exc.value)


def test_on_failure_fires_once_per_tier_in_order():
    seen: list[str] = []
    resolve_tiered(
        live=boom(),
        cache=boom(),
        mock=lambda: 0,
        on_failure=lambda f: seen.append(f.tier),
    )
    assert seen == ["live", "cache"]


def test_raising_cache_falls_through_to_mock():
    r = resolve_tiered(live=boom(), cache=boom("cache exploded"), mock=lambda: 5)
    assert r.tier == "mock"
    assert any("cache exploded" in str(f.error) for f in r.failures)


def test_every_path_labels_its_tier():
    results = [
        resolve_tiered(live=lambda: 1),
        resolve_tiered(live=boom(), cache=lambda: 2),
        resolve_tiered(live=boom(), cache=boom(), mock=lambda: 3),
    ]
    assert [r.tier for r in results] == ["live", "cache", "mock"]
    assert all(r.badge for r in results)


def test_a_failing_mock_is_recorded_and_still_raises():
    with pytest.raises(AllTiersFailed) as exc:
        resolve_tiered(live=boom(), cache=boom(), mock=boom("mock broke"))
    assert [f.tier for f in exc.value.failures] == ["live", "cache", "mock"]
