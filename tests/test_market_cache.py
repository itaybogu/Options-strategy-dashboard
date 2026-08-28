"""
test_market_cache.py — verifies the shared market data cache actually dedupes.

These tests use a counting fake instead of real network calls, so they are
deterministic and run offline. The point is not to test yfinance; it is to
prove that N identical requests produce 1 upstream fetch, that distinct
requests are not conflated, and that concurrent callers collapse into a
single fetch rather than a thundering herd.

Run:  python3 test_market_cache.py
"""

from __future__ import annotations

import sys
import time
import threading

import market_cache


passed = 0
failed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}" + (f"  ({detail})" if detail else ""))


def section(title: str) -> None:
    print(f"\n{title}\n" + "-" * len(title))


# ──────────────────────────────────────────────────────────────────────────
# 1. Basic dedup: same key, many calls, one fetch
# ──────────────────────────────────────────────────────────────────────────

def test_basic_dedup() -> None:
    section("1. Repeated identical requests collapse to one fetch")
    market_cache.clear()
    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        return "payload"

    results = [market_cache.get_or_fetch("t:basic", 60.0, fetch) for _ in range(20)]

    check("upstream called exactly once", calls["n"] == 1, f"called {calls['n']}x")
    check("all callers got the value", all(r == "payload" for r in results))


# ──────────────────────────────────────────────────────────────────────────
# 2. Distinct keys must NOT share a cache entry
# ──────────────────────────────────────────────────────────────────────────

def test_key_isolation() -> None:
    section("2. Distinct keys stay isolated")
    market_cache.clear()
    calls = {"n": 0}

    def make(val):
        def fetch():
            calls["n"] += 1
            return val
        return fetch

    a = market_cache.get_or_fetch("t:AAPL", 60.0, make("aapl"))
    b = market_cache.get_or_fetch("t:MSFT", 60.0, make("msft"))
    a2 = market_cache.get_or_fetch("t:AAPL", 60.0, make("WRONG"))

    check("two distinct keys -> two fetches", calls["n"] == 2, f"called {calls['n']}x")
    check("values not cross-contaminated", a == "aapl" and b == "msft")
    check("repeat hit returns original, not new fetch", a2 == "aapl")


# ──────────────────────────────────────────────────────────────────────────
# 3. Expiry: a stale entry must be refetched
# ──────────────────────────────────────────────────────────────────────────

def test_expiry() -> None:
    section("3. Entries expire after their TTL")
    market_cache.clear()
    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        return calls["n"]

    first = market_cache.get_or_fetch("t:exp", 0.15, fetch)
    cached = market_cache.get_or_fetch("t:exp", 0.15, fetch)
    time.sleep(0.25)
    after = market_cache.get_or_fetch("t:exp", 0.15, fetch)

    check("within TTL served from cache", first == 1 and cached == 1)
    check("after TTL refetched", after == 2, f"got {after}, calls={calls['n']}")


# ──────────────────────────────────────────────────────────────────────────
# 4. In-flight dedup: concurrent callers share one fetch
# ──────────────────────────────────────────────────────────────────────────

def test_inflight_dedup() -> None:
    section("4. Concurrent callers collapse into a single fetch")
    market_cache.clear()
    calls = {"n": 0}
    lock = threading.Lock()

    def slow_fetch():
        with lock:
            calls["n"] += 1
        time.sleep(0.3)          # simulate a slow network round-trip
        return "shared"

    results = []
    res_lock = threading.Lock()

    def worker():
        v = market_cache.get_or_fetch("t:inflight", 60.0, slow_fetch)
        with res_lock:
            results.append(v)

    threads = [threading.Thread(target=worker) for _ in range(12)]
    t0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.time() - t0

    check("12 concurrent callers -> 1 fetch", calls["n"] == 1, f"called {calls['n']}x")
    check("all 12 got the value", len(results) == 12 and all(r == "shared" for r in results))
    # If they had serialised, 12 * 0.3s = 3.6s. Sharing means ~0.3s.
    check("callers waited in parallel, not serially", elapsed < 1.5, f"{elapsed:.2f}s")


# ──────────────────────────────────────────────────────────────────────────
# 5. Errors are cached briefly, then retried
# ──────────────────────────────────────────────────────────────────────────

def test_error_caching() -> None:
    section("5. Failures are cached briefly to avoid hammering the source")
    market_cache.clear()
    calls = {"n": 0}

    def failing():
        calls["n"] += 1
        raise RuntimeError("upstream down")

    # Error TTL is min(ttl, 30s), so a short ttl gives a short error window.
    errors = 0
    for _ in range(5):
        try:
            market_cache.get_or_fetch("t:err", 0.2, failing)
        except RuntimeError:
            errors += 1

    check("caller still sees the error every time", errors == 5, f"{errors}/5 raised")
    check("but upstream only hit once", calls["n"] == 1, f"called {calls['n']}x")

    time.sleep(0.3)
    try:
        market_cache.get_or_fetch("t:err", 0.2, failing)
    except RuntimeError:
        pass
    check("retries after error TTL lapses", calls["n"] == 2, f"called {calls['n']}x")

    # cache_errors=False must not suppress repeat attempts.
    market_cache.clear()
    calls["n"] = 0
    for _ in range(3):
        try:
            market_cache.get_or_fetch("t:err2", 60.0, failing, cache_errors=False)
        except RuntimeError:
            pass
    check("cache_errors=False retries every call", calls["n"] == 3, f"called {calls['n']}x")


# ──────────────────────────────────────────────────────────────────────────
# 6. CachedTicker: the duck-type strategies actually consume
# ──────────────────────────────────────────────────────────────────────────

class FakeTicker:
    """Stands in for yf.Ticker, counting how often each endpoint is hit."""

    def __init__(self, symbol: str, counter: dict):
        self.symbol = symbol
        self._c = counter

    def history(self, period="1mo", interval="1d", auto_adjust=True, **kw):
        self._c["history"] = self._c.get("history", 0) + 1
        return f"hist:{self.symbol}:{period}:{interval}"

    def option_chain(self, date=None):
        self._c["chain"] = self._c.get("chain", 0) + 1
        return f"chain:{self.symbol}:{date}"

    @property
    def options(self):
        self._c["options"] = self._c.get("options", 0) + 1
        return ("2026-01-16", "2026-02-20")

    @property
    def info(self):
        self._c["info"] = self._c.get("info", 0) + 1
        return {"sector": "Tech"}

    def something_uncached(self):
        return "passthrough"


def test_cached_ticker() -> None:
    section("6. CachedTicker wraps the yf.Ticker duck-type transparently")
    market_cache.clear()
    counter: dict = {}
    builds = {"n": 0}

    def factory():
        builds["n"] += 1
        return FakeTicker("AAPL", counter)

    tk = market_cache.CachedTicker("AAPL", factory)

    check("construction is lazy (no fetch yet)", builds["n"] == 0)

    h1 = tk.history(period="1y")
    h2 = tk.history(period="1y")
    check("repeat history -> 1 upstream call", counter.get("history") == 1,
          f"{counter.get('history')}x")
    check("history value correct", h1 == h2 == "hist:AAPL:1y:1d")

    tk.history(period="1mo")
    check("different period -> new fetch", counter.get("history") == 2,
          f"{counter.get('history')}x")

    tk.option_chain("2026-01-16")
    tk.option_chain("2026-01-16")
    check("repeat option_chain -> 1 upstream call", counter.get("chain") == 1,
          f"{counter.get('chain')}x")

    tk.option_chain("2026-02-20")
    check("different expiry -> new fetch", counter.get("chain") == 2,
          f"{counter.get('chain')}x")

    _ = tk.options, tk.options, tk.options
    check("repeat .options -> 1 upstream call", counter.get("options") == 1,
          f"{counter.get('options')}x")

    _ = tk.info, tk.info
    check("repeat .info -> 1 upstream call", counter.get("info") == 1,
          f"{counter.get('info')}x")

    check("unknown attrs fall through to real ticker",
          tk.something_uncached() == "passthrough")
    check("underlying built only once", builds["n"] == 1, f"built {builds['n']}x")


# ──────────────────────────────────────────────────────────────────────────
# 7. Two "strategies" sharing one ticker symbol
# ──────────────────────────────────────────────────────────────────────────

def test_cross_strategy_sharing() -> None:
    section("7. Separate CachedTicker objects for one symbol share the cache")
    market_cache.clear()
    counter: dict = {}

    # Each strategy calls data_provider.get_ticker("AAPL") independently,
    # producing separate wrapper objects. The cache is keyed by symbol, so
    # the second wrapper must not refetch what the first already pulled.
    tk_a = market_cache.CachedTicker("AAPL", lambda: FakeTicker("AAPL", counter))
    tk_b = market_cache.CachedTicker("AAPL", lambda: FakeTicker("AAPL", counter))

    tk_a.history(period="1y")
    tk_b.history(period="1y")

    check("second strategy reuses first strategy's history",
          counter.get("history") == 1, f"{counter.get('history')}x")

    _ = tk_a.info
    _ = tk_b.info
    check("second strategy reuses first strategy's .info",
          counter.get("info") == 1, f"{counter.get('info')}x")


# ──────────────────────────────────────────────────────────────────────────
# 8. Stats and clear()
# ──────────────────────────────────────────────────────────────────────────

def test_stats() -> None:
    section("8. Stats track hits and misses")
    market_cache.clear()
    market_cache.reset_stats()

    market_cache.get_or_fetch("t:s", 60.0, lambda: 1)
    for _ in range(4):
        market_cache.get_or_fetch("t:s", 60.0, lambda: 1)

    s = market_cache.stats()
    check("1 miss recorded", s.get("misses") == 1, str(s))
    check("4 hits recorded", s.get("hits") == 4, str(s))

    removed = market_cache.clear("t:")
    check("clear(prefix) removed the entry", removed >= 1, f"removed {removed}")


def main() -> int:
    print("=" * 62)
    print("market_cache dedup tests")
    print("=" * 62)

    test_basic_dedup()
    test_key_isolation()
    test_expiry()
    test_inflight_dedup()
    test_error_caching()
    test_cached_ticker()
    test_cross_strategy_sharing()
    test_stats()

    print("\n" + "=" * 62)
    print(f"RESULT: {passed} passed, {failed} failed")
    print("=" * 62)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
