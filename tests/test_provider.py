"""
test_provider.py — offline validation of the data-provider abstraction.

Runs with NO TWS and NO network. It installs a fake `ib_insync` plus a fake
`yfinance` into sys.modules, then exercises data_provider to prove:

  1. set_provider() / get_provider() round-trip and reject typos safely.
  2. get_ticker() returns an IBKRTicker when provider == "ibkr".
  3. When IBKR raises, fallback to yfinance happens and is REPORTED
     (provider_status.effective flips to "yfinance").
  4. With fallback disabled, IBKR errors propagate instead of hiding.
  5. download() returns the yf.download-shaped MultiIndex either way.
  6. get_risk_free_rate_raw() prefers IBKR, degrades to yfinance, then None.
  7. provider_status() never raises, even with ib_insync absent.
  8. shutdown() is idempotent and safe when never connected.

Run:  python test_provider.py
"""

from __future__ import annotations

import sys
import types
import traceback

import pandas as pd

# ── bookkeeping ───────────────────────────────────────────────────────────────
_results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((name, bool(cond), detail))
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))


# ── fake yfinance ────────────────────────────────────────────────────────────
class _FakeYFTicker:
    """Minimal stand-in that records that yfinance was the one asked."""

    def __init__(self, symbol, session=None):
        self.symbol = symbol
        self.session = session
        self.fast_info = {"last_price": 111.11, "lastPrice": 111.11}
        self.info = {"sector": "Technology", "fiftyTwoWeekHigh": 200.0}

    def history(self, period="1mo", interval="1d", auto_adjust=True, **kw):
        idx = pd.date_range("2024-01-02", periods=5, freq="B")
        return pd.DataFrame(
            {"Open": 100.0, "High": 101.0, "Low": 99.0,
             "Close": [100, 101, 102, 103, 104], "Volume": 1_000_000},
            index=idx,
        )


def _fake_yf_download(tickers, **kw):
    syms = tickers.split() if isinstance(tickers, str) else list(tickers)
    idx = pd.date_range("2024-01-02", periods=4, freq="B")
    cols = pd.MultiIndex.from_product([["Close", "Volume"], syms])
    return pd.DataFrame(1.0, index=idx, columns=cols)


def install_fake_yfinance() -> types.ModuleType:
    m = types.ModuleType("yfinance")
    m.Ticker = _FakeYFTicker
    m.download = _fake_yf_download
    sys.modules["yfinance"] = m
    return m


# ── fake ibkr_provider behaviours ────────────────────────────────────────────
class _FakeIBKRTicker:
    def __init__(self, symbol, host=None, port=None, client_id=None):
        self.symbol = symbol
        self.fast_info = {"last_price": 222.22, "lastPrice": 222.22}


def install_fake_ibkr(mode: str = "ok") -> types.ModuleType:
    """
    mode:
      "ok"      → everything works
      "nofn"    → constructing IBKRTicker raises ConnectionError (TWS down)
      "norate"  → tickers fine, risk_free_rate returns None
    """
    m = types.ModuleType("ibkr_provider")

    def _mk_ticker(symbol, host=None, port=None, client_id=None):
        if mode == "nofn":
            raise ConnectionError("TWS not reachable on 127.0.0.1:7497")
        return _FakeIBKRTicker(symbol, host, port, client_id)

    m.IBKRTicker = _mk_ticker

    def _batch_history(tickers, period="1mo", interval="1d", **kw):
        if mode == "nofn":
            raise ConnectionError("TWS not reachable")
        syms = tickers.split() if isinstance(tickers, str) else list(tickers)
        idx = pd.date_range("2024-01-02", periods=4, freq="B")
        cols = pd.MultiIndex.from_product([["Close", "Volume"], syms])
        return pd.DataFrame(2.0, index=idx, columns=cols)

    m.batch_history = _batch_history

    def _rate(**kw):
        if mode in ("nofn", "norate"):
            return None
        return 0.0531

    m.risk_free_rate = _rate
    m.connection_status = lambda: {"connected": mode != "nofn"}
    m.disconnect_all = lambda: None
    sys.modules["ibkr_provider"] = m
    return m


def provider_type(tk):
    """
    Concrete provider ticker behind whatever get_ticker() returned.

    get_ticker() returns a CachedTicker wrapper (Task 4) so repeated access
    within a scan cycle hits the cache instead of the network. These scenarios
    assert *which provider was selected*, which is a property of the wrapped
    object, so unwrap before doing isinstance checks.
    """
    unwrap = getattr(tk, "unwrap", None)
    return unwrap() if callable(unwrap) else tk


def fresh_data_provider():
    """Re-import data_provider so module-level state is clean per scenario."""
    for name in ("data_provider",):
        sys.modules.pop(name, None)
    import data_provider
    return data_provider


# ── scenarios ────────────────────────────────────────────────────────────────
def scenario_ibkr_happy():
    print("\n[1] IBKR available — no fallback expected")
    install_fake_yfinance()
    install_fake_ibkr("ok")
    dp = fresh_data_provider()

    check("set_provider('ibkr') returns 'ibkr'", dp.set_provider("ibkr") == "ibkr")
    check("get_provider() == 'ibkr'", dp.get_provider() == "ibkr")

    tk = dp.get_ticker("AAPL")
    check("get_ticker returns IBKR ticker",
          isinstance(provider_type(tk), _FakeIBKRTicker),
          f"got {type(provider_type(tk)).__name__}")
    check("IBKR price surfaced", tk.fast_info["last_price"] == 222.22)

    st = dp.provider_status()
    check("effective == 'ibkr'", st["effective"] == "ibkr", str(st))
    check("fallback_active is False", st["fallback_active"] is False)
    check("status carries ibkr block", isinstance(st.get("ibkr"), dict))

    df = dp.download(["AAPL", "MSFT"], period="1mo")
    check("download MultiIndex columns", isinstance(df.columns, pd.MultiIndex))
    check("download came from IBKR (value 2.0)", float(df.iloc[0, 0]) == 2.0)

    check("risk-free rate from IBKR", dp.get_risk_free_rate_raw() == 0.0531)


def scenario_ibkr_down_fallback_on():
    print("\n[2] TWS down, fallback ENABLED — silent degrade, but reported")
    install_fake_yfinance()
    install_fake_ibkr("nofn")
    dp = fresh_data_provider()
    dp.IBKR_FALLBACK_TO_YFINANCE = True
    dp.set_provider("ibkr")

    tk = dp.get_ticker("AAPL")
    check("fell back to yfinance ticker",
          isinstance(provider_type(tk), _FakeYFTicker),
          f"got {type(provider_type(tk)).__name__}")

    st = dp.provider_status()
    check("configured still 'ibkr'", st["configured"] == "ibkr")
    check("effective flipped to 'yfinance'", st["effective"] == "yfinance", str(st))
    check("fallback_active True", st["fallback_active"] is True)
    check("fallback_reason recorded", bool(st["fallback_reason"]), str(st))

    df = dp.download(["AAPL"], period="1mo")
    check("download fell back (value 1.0)", float(df.iloc[0, 0]) == 1.0)

    # yfinance fake has no ^IRX history path returning 5d Close/100 → uses
    # _FakeYFTicker.history, Close last = 104 → 1.04
    rate = dp.get_risk_free_rate_raw()
    check("rate fell back to yfinance", rate is not None and abs(rate - 1.04) < 1e-9,
          f"rate={rate}")


def scenario_ibkr_down_fallback_off():
    print("\n[3] TWS down, fallback DISABLED — errors must be loud")
    install_fake_yfinance()
    install_fake_ibkr("nofn")
    dp = fresh_data_provider()
    dp.IBKR_FALLBACK_TO_YFINANCE = False
    dp.set_provider("ibkr")

    raised = False
    try:
        dp.get_ticker("AAPL")
    except Exception:
        raised = True
    check("get_ticker raises when fallback off", raised)

    raised = False
    try:
        dp.download(["AAPL"], period="1mo")
    except Exception:
        raised = True
    check("download raises when fallback off", raised)

    check("rate returns None when fallback off", dp.get_risk_free_rate_raw() is None)


def scenario_yfinance_explicit():
    print("\n[4] provider='yfinance' — IBKR never touched")
    install_fake_yfinance()
    # Poison IBKR so any accidental use blows up loudly.
    bad = types.ModuleType("ibkr_provider")

    def _boom(*a, **k):
        raise AssertionError("IBKR was used while provider=yfinance")

    bad.IBKRTicker = _boom
    bad.batch_history = _boom
    bad.risk_free_rate = _boom
    bad.connection_status = lambda: {"connected": False}
    bad.disconnect_all = lambda: None
    sys.modules["ibkr_provider"] = bad

    dp = fresh_data_provider()
    check("set_provider('yfinance')", dp.set_provider("yfinance") == "yfinance")

    ok = True
    try:
        tk = dp.get_ticker("AAPL")
        df = dp.download(["AAPL"], period="1mo")
        dp.get_risk_free_rate_raw()
    except AssertionError as e:
        ok = False
        print(f"      {e}")
    check("no IBKR calls made", ok)
    check("yfinance ticker returned",
          isinstance(provider_type(tk), _FakeYFTicker))
    check("yfinance download shape", isinstance(df.columns, pd.MultiIndex))

    st = dp.provider_status()
    check("effective == 'yfinance'", st["effective"] == "yfinance")
    check("no ibkr block for yf provider", "ibkr" not in st, str(st))


def scenario_robustness():
    print("\n[5] Robustness — typos, missing ib_insync, idempotent shutdown")
    install_fake_yfinance()
    sys.modules.pop("ibkr_provider", None)          # simulate module absent
    dp = fresh_data_provider()

    check("typo provider defaults to ibkr", dp.set_provider("ibrk") == "ibkr")
    check("empty provider defaults to ibkr", dp.set_provider("") == "ibkr")
    check("None provider defaults to ibkr", dp.set_provider(None) == "ibkr")

    # provider_status must not raise even though ibkr_provider import will fail
    ok, st = True, None
    try:
        st = dp.provider_status()
    except Exception as e:
        ok = False
        print(f"      raised {type(e).__name__}: {e}")
    check("provider_status survives missing ibkr_provider", ok)
    if st:
        check("status reports ibkr not connected",
              st.get("ibkr", {}).get("connected") is False, str(st.get("ibkr")))

    # get_ticker should still work via fallback
    dp.IBKR_FALLBACK_TO_YFINANCE = True
    dp.set_provider("ibkr")
    ok = True
    try:
        tk = dp.get_ticker("AAPL")
        ok = isinstance(provider_type(tk), _FakeYFTicker)
    except Exception as e:
        ok = False
        print(f"      raised {type(e).__name__}: {e}")
    check("ticker still served with ibkr_provider missing", ok)

    ok = True
    try:
        dp.shutdown()
        dp.shutdown()
    except Exception as e:
        ok = False
        print(f"      raised {type(e).__name__}: {e}")
    check("shutdown() idempotent and safe", ok)


def scenario_session_passthrough():
    print("\n[6] session= kwarg parity (strategy_earnings passes one)")
    install_fake_yfinance()
    install_fake_ibkr("nofn")
    dp = fresh_data_provider()
    dp.IBKR_FALLBACK_TO_YFINANCE = True
    dp.set_provider("yfinance")

    sentinel = object()
    tk = dp.get_ticker("AAPL", session=sentinel)
    check("session forwarded to yf.Ticker", getattr(tk, "session", None) is sentinel)

    # IBKR ignores session rather than crashing on the extra kwarg
    dp.set_provider("ibkr")
    ok = True
    try:
        dp.get_ticker("AAPL", session=sentinel)
    except TypeError as e:
        ok = False
        print(f"      TypeError: {e}")
    check("session accepted (ignored) under ibkr", ok)


def main() -> int:
    print("=" * 68)
    print("  data_provider — offline abstraction tests (no TWS, no network)")
    print("=" * 68)

    for fn in (scenario_ibkr_happy,
               scenario_ibkr_down_fallback_on,
               scenario_ibkr_down_fallback_off,
               scenario_yfinance_explicit,
               scenario_robustness,
               scenario_session_passthrough):
        try:
            fn()
        except Exception:
            _results.append((fn.__name__, False, "scenario crashed"))
            print(f"  FAIL  {fn.__name__} crashed:")
            traceback.print_exc()

    passed = sum(1 for _, ok, _ in _results if ok)
    total = len(_results)
    print("\n" + "=" * 68)
    print(f"  {passed}/{total} checks passed")
    print("=" * 68)
    if passed != total:
        print("\nFailures:")
        for n, ok, d in _results:
            if not ok:
                print(f"  - {n}  {d}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
