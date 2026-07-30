"""Three-tier data resolution: live → cache → mock, with an honest provenance badge.

Generalised from the independently-written three-tier fallbacks in
``market-radar`` (*Three-Tier Data Resolution*), ``courtvision-ai``,
``finance-cockpit``, ``chainsight-ai``, ``deltafin``, ``decision-brief``,
``medpsy-clinical-trial-agent``, and ``metal-tokenization-traceability``
(*Three-Tier Data Fallback with Honest Badges*) — nine repos that each
reimplemented the same ladder.

The pattern exists because a dashboard that silently shows mock data is worse
than one that shows nothing: the reader cannot tell a real number from a
placeholder. So every result carries the tier it came from, and the caller is
expected to render that badge.

Design rules, all taken from the donor implementations:

* **The badge is not optional.** :func:`resolve_tiered` cannot return a value
  without a ``tier``; there is no code path yielding an unlabelled number.
* **Mock is opt-in.** With no ``mock`` supplied, exhausting live and cache
  raises :class:`AllTiersFailed`. Fabricating data is never the default.
* **A cache hit is still degraded.** ``stale`` is set when the cached value is
  older than ``max_cache_age``, so "cached a second ago" is distinguishable
  from "cached last Tuesday".
* **Errors are collected, not swallowed.** Every tier failure is reported, so a
  silent fallback still leaves a diagnosable trail.

Example::

    result = resolve_tiered(
        live=lambda: fetch_spot_price("LME-CU"),
        cache=lambda: read_cache("LME-CU"),
        max_cache_age=60.0,
    )
    render(result.value, badge=result.badge, muted=result.degraded)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Generic, Optional, Sequence, TypeVar, Union

T = TypeVar("T")

#: Tier names, in attempt order.
LIVE = "live"
CACHE = "cache"
MOCK = "mock"

DEFAULT_MAX_CACHE_AGE = 300.0  # seconds

#: Sentinel meaning "the cache had no entry". Distinct from a cached ``None``,
#: which is a legitimate value the caller may have stored on purpose.
MISS = object()


@dataclass(frozen=True)
class TierFailure:
    """One tier's failure."""

    tier: str
    error: BaseException

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.tier}: {self.error}"


@dataclass(frozen=True)
class Cached(Generic[T]):
    """A cached value together with when it was recorded.

    ``cached_at`` is epoch seconds. Return a bare value instead if you have no
    timestamp — staleness simply cannot be computed then.
    """

    value: T
    cached_at: float


@dataclass(frozen=True)
class TieredResult(Generic[T]):
    """A resolved value plus honest provenance."""

    value: T
    #: Which tier produced :attr:`value`. Render this.
    tier: str
    #: True for any cache or mock hit, or a cache hit older than ``max_cache_age``.
    degraded: bool
    #: True only for a cache hit older than ``max_cache_age``.
    stale: bool = False
    #: Age of the cached value in seconds, when the cache reported one.
    age: Optional[float] = None
    #: Why each attempted tier failed, in attempt order. Empty on a live hit.
    failures: Sequence[TierFailure] = field(default_factory=tuple)
    #: Short human-readable provenance, e.g. ``"cache (stale, 3h)"``.
    badge: str = ""


class AllTiersFailed(RuntimeError):
    """Every configured tier failed and no ``mock`` was supplied."""

    def __init__(self, failures: Sequence[TierFailure]) -> None:
        detail = "; ".join(str(f) for f in failures) or "no tiers configured"
        super().__init__(f"all data tiers failed ({detail})")
        self.failures = tuple(failures)


def _human_age(seconds: float) -> str:
    if seconds < 1:
        return f"{int(seconds * 1000)}ms"
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def _badge(tier: str, stale: bool, age: Optional[float]) -> str:
    if tier == LIVE:
        return "live"
    if tier == MOCK:
        return "mock data — not real"
    parts = []
    if stale:
        parts.append("stale")
    if age is not None:
        parts.append(_human_age(age))
    return f"cache ({', '.join(parts)})" if parts else "cache"


def resolve_tiered(
    *,
    live: Callable[[], T],
    cache: Optional[Callable[[], Union[Cached[T], T, None]]] = None,
    mock: Optional[Callable[[], T]] = None,
    max_cache_age: float = DEFAULT_MAX_CACHE_AGE,
    max_cache_age_hard: Optional[float] = None,
    on_failure: Optional[Callable[[TierFailure], None]] = None,
    miss_sentinel: Any = None,
) -> TieredResult[T]:
    """Resolve a value through live → cache → mock, reporting provenance.

    Args:
        live: The authoritative source. Tried first.
        cache: Cached fallback. Return ``miss_sentinel`` (``None`` by default)
            for a miss. Raising is also treated as a miss and recorded.
        mock: Last-resort placeholder. Omit to make exhaustion an error — the
            right choice for anything a person reads as a real figure.
        max_cache_age: A cache hit older than this (seconds) is flagged
            ``stale``. Still returned; staleness is reported, not fatal.
        max_cache_age_hard: Reject a cache hit older than this outright and fall
            through to ``mock``. Off by default.
        on_failure: Called once per tier failure. Wire to your logger.
        miss_sentinel: What ``cache`` returns to mean "no entry". Defaults to
            ``None``. Pass :data:`MISS` when a cached ``None`` is a real value
            you need to distinguish from a miss.

    Returns:
        A :class:`TieredResult` whose ``tier`` and ``badge`` are always set.

    Raises:
        AllTiersFailed: when every configured tier failed.
    """
    failures: list[TierFailure] = []

    def fail(tier: str, error: BaseException) -> None:
        failure = TierFailure(tier=tier, error=error)
        failures.append(failure)
        if on_failure is not None:
            on_failure(failure)

    try:
        return TieredResult(
            value=live(),
            tier=LIVE,
            degraded=False,
            stale=False,
            failures=(),
            badge=_badge(LIVE, False, None),
        )
    except Exception as exc:  # noqa: BLE001 - any live failure falls through
        fail(LIVE, exc)

    if cache is not None:
        try:
            hit = cache()
        except Exception as exc:  # noqa: BLE001 - a raising cache is a miss
            fail(CACHE, exc)
        else:
            if hit is miss_sentinel:
                fail(CACHE, LookupError("cache miss"))
            else:
                if isinstance(hit, Cached):
                    value, age = hit.value, max(0.0, time.time() - hit.cached_at)
                else:
                    value, age = hit, None

                if (
                    max_cache_age_hard is not None
                    and age is not None
                    and age > max_cache_age_hard
                ):
                    fail(
                        CACHE,
                        ValueError(
                            f"cached value age {age:.1f}s exceeds hard limit "
                            f"{max_cache_age_hard:.1f}s"
                        ),
                    )
                else:
                    stale = age is not None and age > max_cache_age
                    return TieredResult(
                        value=value,
                        tier=CACHE,
                        degraded=True,
                        stale=stale,
                        age=age,
                        failures=tuple(failures),
                        badge=_badge(CACHE, stale, age),
                    )

    if mock is not None:
        try:
            return TieredResult(
                value=mock(),
                tier=MOCK,
                degraded=True,
                stale=False,
                failures=tuple(failures),
                badge=_badge(MOCK, False, None),
            )
        except Exception as exc:  # noqa: BLE001
            fail(MOCK, exc)

    raise AllTiersFailed(failures)
