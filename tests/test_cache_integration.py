"""
test_cache_integration.py — verifies the cache is actually WIRED IN, not just
that market_cache works in isolation.

test_market_cache.py proves the cache module is correct. This file proves the
call sites use it: that data_provider.get_ticker returns a cached wrapper, that
data_provider.download dedupes by symbol set, and that the three modules which
each used to fetch the S&P 500 list now share one entry.

The heavy third-party deps (pandas/numpy/scipy/yfinance/requests) are not
required. Minimal stubs are installed into sys.modules first, so this runs
offline and never touches the network. The stubs exist only to let the modules
import; the logic under test is ours.

Run:  python3 test_cache_integration.py
"""

from __future__ import annotations

import sys
import types


# ──────────────────────────────────────────────────────────────────────────
# Stub out third-party deps BEFORE importing project modules
# ──────────────────────────────────────────────────────────────────────────

def _stub(name: str, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


yf_calls = {"ticker": 0, "download": 0}


class _StubYFTicker:
    def __init__(self, symbol, session=None):
        yf_calls["ticker"] += 1
        self.symbol = symbol

    def history(self, period="1mo", interval="1d", auto_adjust=True, **kw):
        return f"hist:{self.symbol}:{period}"


def _stub_download(tickers, **kw):
    yf_calls["download"] += 1
    n = len(tickers) if isinstance(tickers, (list, tuple, set)) else 1
    return f"batch:{n}:{kw.get('period')}"


_stub("yfinance", Ticker=_StubYFTicker, download=_stub_download)

import market_cache          # noqa: E402
import data_provider         # noqa: E402


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
# 1. get_ticker returns a cached wrapper
# ──────────────────────────────────────────────────────────────────────────

def test_get_ticker_wrapped() -> None:
    section("1. data_provider.get_ticker returns a CachedTicker")
    market_cache.clear()
    yf_calls["ticker"] = 0

    tk = data_provider.get_ticker("AAPL")
    check("returns CachedTicker", isinstance(tk, market_cache.CachedTicker),
          type(tk).__name__)
    check("underlying ticker not built yet (lazy)", yf_calls["ticker"] == 0,
          f"built {yf_calls['ticker']}x")

    h1 = tk.history(period="1y")
    h2 = tk.history(period="1y")
    check("history value correct", h1 == h2 == "hist:AAPL:1y", str(h1))


# ──────────────────────────────────────────────────────────────────────────
# 2. Two strategies calling get_ticker separately share cached data
# ──────────────────────────────────────────────────────────────────────────

def test_cross_call_sharing() -> None:
    section("2. Independent get_ticker() calls share one cache entry")
    market_cache.clear()
    yf_calls["ticker"] = 0

    # This is the real duplication pattern: vol_momentum and calendar each
    # call data_provider.get_ticker("AAPL") and each ask for 1y history.
    tk_a = data_provider.get_ticker("AAPL")
    tk_b = data_provider.get_ticker("AAPL")

    a = tk_a.history(period="1y")
    b = tk_b.history(period="1y")

    check("both callers got same data", a == b == "hist:AAPL:1y")
    check("underlying yf.Ticker built only once", yf_calls["ticker"] == 1,
          f"built {yf_calls['ticker']}x")

    # A different symbol must still fetch.
    data_provider.get_ticker("MSFT").history(period="1y")
    check("different symbol still fetches", yf_calls["ticker"] == 2,
          f"built {yf_calls['ticker']}x")


# ──────────────────────────────────────────────────────────────────────────
# 3. download() dedupes identical batch requests
# ──────────────────────────────────────────────────────────────────────────

def test_download_dedup() -> None:
    section("3. data_provider.download dedupes identical batch requests")
    market_cache.clear()
    yf_calls["download"] = 0

    universe = ["AAPL", "MSFT", "NVDA", "AMZN"]

    r1 = data_provider.download(universe, period="1mo", interval="1d")
    r2 = data_provider.download(universe, period="1mo", interval="1d")

    check("identical batch -> 1 upstream download", yf_calls["download"] == 1,
          f"called {yf_calls['download']}x")
    check("both callers got same result", r1 == r2, f"{r1} vs {r2}")

    # Same symbols in a different order is the same request.
    data_provider.download(["MSFT", "AAPL", "NVDA", "AMZN"],
                           period="1mo", interval="1d")
    check("symbol order does not defeat the cache", yf_calls["download"] == 1,
          f"called {yf_calls['download']}x")

    # A different period is genuinely a different request.
    data_provider.download(universe, period="1y", interval="1d")
    check("different period -> new download", yf_calls["download"] == 2,
          f"called {yf_calls['download']}x")

    # A different universe is genuinely a different request.
    data_provider.download(universe + ["TSLA"], period="1mo", interval="1d")
    check("different universe -> new download", yf_calls["download"] == 3,
          f"called {yf_calls['download']}x")


# ──────────────────────────────────────────────────────────────────────────
# 4. The S&P 500 universe is shared across modules
# ──────────────────────────────────────────────────────────────────────────

def test_universe_shared() -> None:
    section("4. S&P 500 universe shared across calendar + vol_momentum")

    # strategy_calendar pulls in requests, pandas and option_pricing
    # (which needs numpy + scipy.stats). Stub whatever is absent.
    _stub("requests", get=lambda *a, **k: None, Session=object)

    for name, attrs in (
        ("pandas", dict(DataFrame=object, read_csv=lambda *a, **k: None,
                        concat=lambda *a, **k: None, isna=lambda *a, **k: False)),
        ("numpy", dict(arange=lambda *a, **k: None, maximum=lambda *a, **k: None,
                       ndarray=object)),
    ):
        try:
            __import__(name)
        except ImportError:
            _stub(name, **attrs)

    try:
        import scipy.stats  # noqa: F401
    except ImportError:
        _norm = type("norm", (), {"cdf": staticmethod(lambda x: 0.5),
                                  "pdf": staticmethod(lambda x: 0.4)})()
        scipy_mod = _stub("scipy")
        stats_mod = _stub("scipy.stats", norm=_norm)
        scipy_mod.stats = stats_mod

    try:
        import strategy_calendar
    except Exception as e:
        check("strategy_calendar importable", False, f"{type(e).__name__}: {e}")
        return

    market_cache.clear()
    calls = {"n": 0}

    def fake_universe():
        calls["n"] += 1
        return ["AAA", "BBB", "CCC"]

    # Patch the *uncached* inner functions. If the public functions are wired
    # to the shared cache key, only one of these ever runs.
    strategy_calendar._get_sp500_tickers_uncached = fake_universe

    a = strategy_calendar.get_sp500_tickers()
    b = strategy_calendar.get_sp500_tickers()
    check("repeat call -> 1 fetch", calls["n"] == 1, f"called {calls['n']}x")
    check("values correct", a == b == ["AAA", "BBB", "CCC"], str(a))

    try:
        import strategy_vol_momentum
        strategy_vol_momentum._fetch_sp500_tickers_uncached = fake_universe
        c = strategy_vol_momentum.fetch_sp500_tickers()
        check("vol_momentum reuses calendar's universe", calls["n"] == 1,
              f"called {calls['n']}x")
        check("vol_momentum got same list", c == ["AAA", "BBB", "CCC"], str(c))
    except Exception as e:
        check("strategy_vol_momentum importable", False,
              f"{type(e).__name__}: {e}")

    # Callers truncate this list (put_selling applies UNIVERSE_MAX). Mutating
    # the returned list must not corrupt what the next caller sees.
    a.append("ZZZ")
    a.pop(0)
    d = strategy_calendar.get_sp500_tickers()
    check("cache immune to caller mutation", d == ["AAA", "BBB", "CCC"], str(d))


# ──────────────────────────────────────────────────────────────────────────
# 5. Disabling the cache restores uncached behaviour
# ──────────────────────────────────────────────────────────────────────────

def test_kill_switch() -> None:
    section("5. MC_ENABLED=0 kill switch bypasses the cache")
    market_cache.clear()
    original = market_cache.ENABLED
    try:
        market_cache.ENABLED = False
        yf_calls["ticker"] = 0
        yf_calls["download"] = 0

        tk = data_provider.get_ticker("AAPL")
        check("get_ticker returns raw ticker when disabled",
              not isinstance(tk, market_cache.CachedTicker), type(tk).__name__)

        data_provider.download(["AAPL", "MSFT"], period="1mo")
        data_provider.download(["AAPL", "MSFT"], period="1mo")
        check("download not deduped when disabled", yf_calls["download"] == 2,
              f"called {yf_calls['download']}x")
    finally:
        market_cache.ENABLED = original

    check("kill switch restored", market_cache.ENABLED == original)


def main() -> int:
    print("=" * 62)
    print("market_cache INTEGRATION tests (wiring, not just the module)")
    print("=" * 62)

    test_get_ticker_wrapped()
    test_cross_call_sharing()
    test_download_dedup()
    test_universe_shared()
    test_kill_switch()

    print("\n" + "=" * 62)
    print(f"RESULT: {passed} passed, {failed} failed")
    print("=" * 62)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
