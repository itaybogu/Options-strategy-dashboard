#!/usr/bin/env python3
"""
test_cache_scope.py — provider isolation of cache keys.

Why this file exists
────────────────────
set_provider() used to flush the entire market cache on every switch. That was
correct but wasteful: it discarded still-valid entries (universe lists, hourly
fundamentals) and forced memoised ticker objects to be rebuilt.

The flush was replaced with provider-scoped cache keys. Correctness now rests
entirely on that scoping — nothing else stops a yfinance caller from reading an
entry that IBKR wrote. These tests pin that invariant down so a future edit to
the key format cannot silently reintroduce cross-provider leakage.

Offline: no network, no TWS. Fake tickers only.
"""

import os
import sys

os.environ.setdefault("IBKR_FALLBACK_TO_YFINANCE", "true")

import market_cache as mc          # noqa: E402
import data_provider as dp         # noqa: E402

_failures: list[str] = []


def chk(cond: bool, msg: str) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {msg}")
    if not cond:
        _failures.append(msg)


class FakeTicker:
    """Minimal stand-in that tags its data so we can trace which one was read."""

    def __init__(self, tag: str, counter: list | None = None):
        self.tag = tag
        if counter is not None:
            counter[0] += 1

    @property
    def fast_info(self):
        return {"lastPrice": self.tag}

    @property
    def options(self):
        return (self.tag,)


def test_cross_provider_isolation() -> None:
    print("\n1. An entry written under one provider is invisible to the other")
    mc.clear()
    ib = mc.CachedTicker("AAPL", lambda: FakeTicker("IBKR_PRICE"), scope="ibkr")
    yf = mc.CachedTicker("AAPL", lambda: FakeTicker("YF_PRICE"), scope="yfinance")

    chk(ib.fast_info["lastPrice"] == "IBKR_PRICE",
        "ibkr-scoped read returns ibkr data")
    # The critical assertion: yfinance must NOT inherit the ibkr entry that is
    # already warm for the same symbol and accessor.
    chk(yf.fast_info["lastPrice"] == "YF_PRICE",
        "yfinance-scoped read is not served the ibkr entry")

    scoped = sorted(k for k in mc._store if k.startswith(("ibkr:", "yfinance:")))
    chk(scoped == ["ibkr:quote:AAPL", "yfinance:quote:AAPL"],
        f"two disjoint keys written: {scoped}")


def test_same_scope_still_dedupes() -> None:
    print("\n2. Scoping does not defeat caching within one provider")
    mc.clear()
    built = [0]
    factory = lambda: FakeTicker("X", built)          # noqa: E731

    a = mc.CachedTicker("MSFT", factory, scope="yfinance")
    b = mc.CachedTicker("MSFT", factory, scope="yfinance")
    a.fast_info
    b.fast_info
    # Two wrappers, same scope+symbol+accessor: the second must hit the cache
    # and never invoke the factory. If this regresses the cache is worthless.
    chk(built[0] == 1, f"underlying built once across two wrappers (got {built[0]})")


def test_switch_preserves_valid_entries() -> None:
    print("\n3. Provider switch no longer flushes unrelated entries")
    mc.clear()
    mc.get_or_fetch("universe:sp500", 600, lambda: ["AAPL", "MSFT"])

    dp.set_provider("yfinance")
    dp.set_provider("ibkr")

    # Universe lists are provider-independent and expensive to rebuild; a
    # switch must not evict them.
    chk("universe:sp500" in mc._store,
        "provider-independent universe entry survived two switches")


def test_unscoped_default_unchanged() -> None:
    print("\n4. Omitting scope keeps the original key shape")
    mc.clear()
    mc.CachedTicker("IBM", lambda: FakeTicker("Z")).fast_info
    # Guards direct CachedTicker construction elsewhere in the codebase, which
    # must keep working without passing a scope.
    chk("quote:IBM" in mc._store,
        f"unscoped key left unprefixed: {sorted(mc._store)}")


def test_fallback_uses_yfinance_keyspace() -> None:
    print("\n5. Fallback reads and writes the yfinance keyspace")
    mc.clear()
    dp.set_provider("ibkr")
    tk = dp.get_ticker("AAPL")        # TWS absent in CI -> degrades

    chk(dp.provider_status()["fallback_active"],
        "fallback recorded eagerly, at get_ticker() time")
    # While degraded the bytes are yfinance's, so the scope must say so even
    # though the *configured* provider is still ibkr.
    chk(getattr(tk, "_scope", "") == "yfinance",
        f"wrapper scoped to effective provider (got {getattr(tk, '_scope', '?')!r})")


def main() -> int:
    print("=" * 62)
    print("CACHE SCOPE / PROVIDER ISOLATION")
    print("=" * 62)
    for fn in (test_cross_provider_isolation,
               test_same_scope_still_dedupes,
               test_switch_preserves_valid_entries,
               test_unscoped_default_unchanged,
               test_fallback_uses_yfinance_keyspace):
        fn()

    print("\n" + "=" * 62)
    if _failures:
        print(f"RESULT: {len(_failures)} FAILED")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("RESULT: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
