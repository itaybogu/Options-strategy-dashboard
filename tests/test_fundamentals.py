"""
test_fundamentals.py — regression tests for the TTM FCF yield engine.

RUN:  python test_fundamentals.py            (offline, deterministic)
      python test_fundamentals.py --live     (adds real yfinance checks)

WHY THESE TESTS
───────────────
`fundamentals.get_fcf_yield()` is the single input the whole GS put-selling
strategy depends on, and a wrong value fails SILENTLY: a bad FCF yield produces
a wrong strike target that still looks perfectly reasonable on screen. There is
no downstream sanity check that would catch it.

The specific defect being locked out: yfinance's `info["freeCashflow"]` reports
MSFT at ~16.4B against an actual TTM of ~67.0B (4x low). Because AAPL's value
happens to be roughly right, a one-ticker spot check passes and the bug ships.
Test 3 asserts we do NOT use that field when statements are available.

The offline tests use hand-built fixtures with known arithmetic, so they cannot
be broken by market moves or network conditions. The --live tests confirm the
real yfinance schema still parses; they are opt-in because they need network and
their exact numbers drift with price.
"""

from __future__ import annotations

import os
import sys
import tempfile

# Cache off, and pointed at a temp dir, BEFORE importing the module: the module
# reads these at import time, and a test must never touch the real cache file.
_TMPDIR = tempfile.mkdtemp(prefix="fundtest_")
os.environ["FUNDAMENTALS_CACHE_DIR"]  = _TMPDIR
os.environ["FUNDAMENTALS_CACHE_DAYS"] = "0"

import pandas as pd

import fundamentals as F

# ── Tiny test harness (matches the style of test_provider.py) ─────────────────
_passed = 0
_failed = 0


def check(label: str, cond: bool, detail: str = "") -> bool:
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed += 1
        print(f"  FAIL  {label}" + (f"\n        -> {detail}" if detail else ""))
    return cond


def approx(a, b, tol=1e-6) -> bool:
    if a is None or b is None:
        return False
    return abs(float(a) - float(b)) <= tol * max(1.0, abs(float(b)))


def section(title: str) -> None:
    print(f"\n{title}\n" + "-" * len(title))


# ── Fixtures ──────────────────────────────────────────────────────────────────

class FakeTicker:
    """
    Minimal yf.Ticker duck-type. Only what fundamentals.py touches.

    Built by hand so the expected FCF is exact arithmetic rather than whatever
    the market did today.
    """

    def __init__(self, info=None, quarterly=None, annual=None, raise_on=()):
        self._info      = info if info is not None else {}
        self._quarterly = quarterly
        self._annual    = annual
        self._raise_on  = set(raise_on)

    @property
    def info(self):
        if "info" in self._raise_on:
            raise RuntimeError("simulated info failure")
        return self._info

    @property
    def quarterly_cashflow(self):
        if "quarterly" in self._raise_on:
            raise RuntimeError("simulated quarterly failure")
        return self._quarterly

    @property
    def cashflow(self):
        if "annual" in self._raise_on:
            raise RuntimeError("simulated annual failure")
        return self._annual


def make_cashflow(ocf: list, capex: list, dates: list, fcf: list = None):
    """Build a cash-flow DataFrame shaped like yfinance's (rows=lines, cols=dates)."""
    data = {}
    if ocf   is not None: data["Operating Cash Flow"]  = ocf
    if capex is not None: data["Capital Expenditure"]  = capex
    if fcf   is not None: data["Free Cash Flow"]       = fcf
    cols = [pd.Timestamp(d) for d in dates]
    return pd.DataFrame(data, index=cols).T if data else pd.DataFrame()


QUARTERS = ["2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30"]


# ── 1. Core TTM arithmetic ────────────────────────────────────────────────────
def test_ttm_arithmetic():
    section("1. TTM arithmetic: 4 quarters of OCF + Capex")

    # OCF sums to 100, capex to -30  ->  FCF 70 on a 1000 market cap = 7.00%
    tk = FakeTicker(
        info={"marketCap": 1000.0, "currentPrice": 10.0,
              "sharesOutstanding": 100.0},
        quarterly=make_cashflow(ocf=[40, 30, 20, 10],
                                capex=[-12, -8, -6, -4],
                                dates=QUARTERS),
    )
    r = F.get_fcf_yield("TEST", tk=tk, use_cache=False)

    check("uses quarterly_ttm source", r["source"] == "quarterly_ttm", r["source"])
    check("OCF summed across 4 quarters", approx(r.get("ocf_ttm"), 100.0),
          f"got {r.get('ocf_ttm')}")
    check("capex summed across 4 quarters", approx(r.get("capex_ttm"), -30.0),
          f"got {r.get('capex_ttm')}")
    check("FCF = OCF + capex = 70", approx(r["fcf_ttm"], 70.0), f"got {r['fcf_ttm']}")
    check("FCF yield = 70/1000 = 7%", approx(r["fcf_yield"], 0.07),
          f"got {r['fcf_yield']}")
    check("reports 4 periods used", r["periods_used"] == 4, str(r["periods_used"]))
    check("asof is newest quarter", r["asof"] == "2026-06-30", str(r["asof"]))
    check("marked sane", r["sane"] is True)
    check("no warnings on the clean path", not r["warnings"], str(r["warnings"]))


# ── 2. Capex sign robustness ──────────────────────────────────────────────────
def test_capex_sign():
    section("2. Capex sign normalisation (the doubling trap)")

    base_info = {"marketCap": 1000.0}

    neg = F.get_fcf_yield("NEG", use_cache=False, tk=FakeTicker(
        info=dict(base_info),
        quarterly=make_cashflow(ocf=[25, 25, 25, 25], capex=[-10, -10, -10, -10],
                                dates=QUARTERS)))

    # Same magnitudes, positive convention. Must produce the SAME answer.
    pos = F.get_fcf_yield("POS", use_cache=False, tk=FakeTicker(
        info=dict(base_info),
        quarterly=make_cashflow(ocf=[25, 25, 25, 25], capex=[10, 10, 10, 10],
                                dates=QUARTERS)))

    check("negative-convention capex -> FCF 60", approx(neg["fcf_ttm"], 60.0),
          f"got {neg['fcf_ttm']}")
    check("positive-convention capex -> ALSO FCF 60 (not 140)",
          approx(pos["fcf_ttm"], 60.0),
          f"got {pos['fcf_ttm']} — capex was added instead of subtracted")
    check("both yield 6%", approx(neg["fcf_yield"], 0.06) and approx(pos["fcf_yield"], 0.06))
    check("positive capex is flagged in warnings",
          any("positive" in w.lower() for w in pos["warnings"]), str(pos["warnings"]))


# ── 3. THE KEY TEST: info['freeCashflow'] must not win ────────────────────────
def test_rejects_bad_info_field():
    section("3. info['freeCashflow'] must NOT override real statements")

    # This mirrors the real MSFT discrepancy: the info field is 4x too low.
    # Statements say 67.0B; info claims 16.4B.
    tk = FakeTicker(
        info={"marketCap": 3.69e12, "freeCashflow": 16.4e9},
        quarterly=make_cashflow(
            ocf=[20e9, 18e9, 17e9, 16e9],       # 71B
            capex=[-1e9, -1e9, -1e9, -1e9],     # -4B  -> FCF 67B
            dates=QUARTERS),
    )
    r = F.get_fcf_yield("MSFTLIKE", tk=tk, use_cache=False)

    check("prefers statements over info field", r["source"] == "quarterly_ttm",
          f"source={r['source']}")
    check("FCF is the statement value (67B), not the info value (16.4B)",
          approx(r["fcf_ttm"], 67e9, tol=1e-3),
          f"got {r['fcf_ttm']:.4g}")
    check("yield ~1.82%, not ~0.44%", 0.017 < r["fcf_yield"] < 0.019,
          f"got {r['fcf_yield']:.4%}")

    # And when it IS the only option, it must be loudly flagged.
    only_info = FakeTicker(info={"marketCap": 1000.0, "freeCashflow": 50.0})
    r2 = F.get_fcf_yield("INFOONLY", tk=only_info, use_cache=False)
    check("falls back to info field when no statements exist",
          r2["source"] == "info_field", str(r2["source"]))
    check("info-field fallback still computes a yield", approx(r2["fcf_yield"], 0.05))
    check("info-field fallback is flagged unreliable",
          any("unreliable" in w.lower() for w in r2["warnings"]), str(r2["warnings"]))


# ── 4. Partial data must not masquerade as TTM ─────────────────────────────────
def test_partial_quarters_rejected():
    section("4. Incomplete TTM windows fall through instead of under-reporting")

    # Only 3 quarters available. Summing them would understate FCF ~25% while
    # looking like a valid answer, so quarterly must be refused; annual is used.
    tk = FakeTicker(
        info={"marketCap": 1000.0},
        quarterly=make_cashflow(ocf=[25, 25, 25], capex=[-5, -5, -5],
                                dates=QUARTERS[:3]),
        annual=make_cashflow(ocf=[100], capex=[-20], dates=["2025-12-31"]),
    )
    r = F.get_fcf_yield("PARTIAL", tk=tk, use_cache=False)

    check("does not report a 3-quarter sum as TTM", r["source"] != "quarterly_ttm",
          f"source={r['source']}")
    check("uses the annual statement instead", r["source"] == "annual_ttm",
          f"source={r['source']}")
    check("annual FCF = 100 - 20 = 80", approx(r["fcf_ttm"], 80.0), str(r["fcf_ttm"]))
    check("staleness is flagged",
          any("stale" in w.lower() for w in r["warnings"]), str(r["warnings"]))

    # NaN in the window is the same situation.
    tk_nan = FakeTicker(
        info={"marketCap": 1000.0},
        quarterly=make_cashflow(ocf=[25, float("nan"), 25, 25],
                                capex=[-5, -5, -5, -5], dates=QUARTERS),
        annual=make_cashflow(ocf=[100], capex=[-20], dates=["2025-12-31"]),
    )
    r_nan = F.get_fcf_yield("NANQ", tk=tk_nan, use_cache=False)
    check("NaN inside the TTM window rejects quarterly",
          r_nan["source"] == "annual_ttm", f"source={r_nan['source']}")


# ── 5. Column ordering ────────────────────────────────────────────────────────
def test_column_ordering():
    section("5. Newest-first ordering is enforced, not assumed")

    # Deliberately oldest-first, with a distinctive newest quarter. If ordering
    # were trusted blindly, `asof` would report the oldest date.
    dates_asc = list(reversed(QUARTERS))
    tk = FakeTicker(
        info={"marketCap": 1000.0},
        quarterly=make_cashflow(ocf=[10, 20, 30, 40], capex=[-1, -2, -3, -4],
                                dates=dates_asc),
    )
    r = F.get_fcf_yield("ORDER", tk=tk, use_cache=False)
    check("asof is the newest date despite ascending input",
          r["asof"] == "2026-06-30", str(r["asof"]))
    # All 4 quarters are in the window either way, so the total is unchanged;
    # that is the point — ordering affects `asof`, not the sum.
    check("full-window sum is order-independent", approx(r["fcf_ttm"], 90.0),
          str(r["fcf_ttm"]))


# ── 6. Market cap resolution ───────────────────────────────────────────────────
def test_market_cap():
    section("6. Market cap: explicit field, then price x shares")

    direct = F.get_fcf_yield("MC1", use_cache=False, tk=FakeTicker(
        info={"marketCap": 2000.0},
        quarterly=make_cashflow(ocf=[50, 50, 50, 50], capex=[-25, -25, -25, -25],
                                dates=QUARTERS)))
    check("uses marketCap when present", approx(direct["market_cap"], 2000.0))
    check("yield = 100/2000 = 5%", approx(direct["fcf_yield"], 0.05))

    derived = F.get_fcf_yield("MC2", use_cache=False, tk=FakeTicker(
        info={"currentPrice": 20.0, "sharesOutstanding": 100.0},
        quarterly=make_cashflow(ocf=[50, 50, 50, 50], capex=[-25, -25, -25, -25],
                                dates=QUARTERS)))
    check("derives market cap from price x shares", approx(derived["market_cap"], 2000.0),
          str(derived["market_cap"]))
    check("derivation is flagged",
          any("price x shares" in w for w in derived["warnings"]), str(derived["warnings"]))

    none_cap = F.get_fcf_yield("MC3", use_cache=False, tk=FakeTicker(
        info={},
        quarterly=make_cashflow(ocf=[50, 50, 50, 50], capex=[-25, -25, -25, -25],
                                dates=QUARTERS)))
    check("no market cap -> yield is None", none_cap["fcf_yield"] is None)
    check("FCF is still reported", approx(none_cap["fcf_ttm"], 100.0))
    check("reason explains the failure", "market cap" in none_cap["reason"].lower(),
          none_cap["reason"])


# ── 7. Failure modes never raise ──────────────────────────────────────────────
def test_failures_are_graceful():
    section("7. Bad input degrades, never raises")

    empty = F.get_fcf_yield("NODATA", tk=FakeTicker(info={}), use_cache=False)
    check("no data at all -> None yield", empty["fcf_yield"] is None)
    check("no data -> not sane", empty["sane"] is False)
    check("no data -> reason populated", bool(empty["reason"]), empty["reason"])

    raising = F.get_fcf_yield("RAISER", use_cache=False,
                              tk=FakeTicker(info={"marketCap": 1000.0},
                                            raise_on=("quarterly", "annual", "info")))
    check("raising ticker does not propagate", raising["fcf_yield"] is None)

    blank = F.get_fcf_yield("", use_cache=False)
    check("empty symbol handled", blank["fcf_yield"] is None and "empty" in blank["reason"])

    # Negative FCF is legitimate (cash-burning company), not an error.
    burner = F.get_fcf_yield("BURN", use_cache=False, tk=FakeTicker(
        info={"marketCap": 1000.0},
        quarterly=make_cashflow(ocf=[10, 10, 10, 10], capex=[-30, -30, -30, -30],
                                dates=QUARTERS)))
    check("negative FCF is preserved", approx(burner["fcf_ttm"], -80.0),
          str(burner["fcf_ttm"]))
    check("negative yield still marked sane (real, just unattractive)",
          burner["sane"] is True and burner["fcf_yield"] < 0)

    # Absurd yield -> flagged insane so it can't top the ranking.
    absurd = F.get_fcf_yield("ABSURD", use_cache=False, tk=FakeTicker(
        info={"marketCap": 100.0},
        quarterly=make_cashflow(ocf=[100, 100, 100, 100], capex=[0, 0, 0, 0],
                                dates=QUARTERS)))
    check("400% yield flagged not-sane", absurd["sane"] is False,
          f"yield={absurd['fcf_yield']}")
    check("insanity is explained in warnings",
          any("plausible range" in w for w in absurd["warnings"]), str(absurd["warnings"]))


# ── 8. Ranking ────────────────────────────────────────────────────────────────
def test_ranking():
    section("8. Cross-sectional ranking and quintiles")

    results = {}
    # 10 names, yields 10% down to 1%.
    for i in range(10):
        sym = f"S{i}"
        results[sym] = {"ticker": sym, "fcf_yield": (10 - i) / 100.0, "sane": True}
    # Plus two that must be excluded from ranking.
    results["BAD"]  = {"ticker": "BAD",  "fcf_yield": 0.99, "sane": False}
    results["NONE"] = {"ticker": "NONE", "fcf_yield": None, "sane": False}

    F.rank_by_fcf_yield(results, quantiles=5)

    check("highest yield ranks 1", results["S0"]["fcfy_rank"] == 1,
          str(results["S0"]["fcfy_rank"]))
    check("lowest yield ranks 10", results["S9"]["fcfy_rank"] == 10,
          str(results["S9"]["fcfy_rank"]))
    check("top name is in quintile 1", results["S0"]["fcfy_quintile"] == 1,
          str(results["S0"]["fcfy_quintile"]))
    check("bottom name is in quintile 5", results["S9"]["fcfy_quintile"] == 5,
          str(results["S9"]["fcfy_quintile"]))
    check("insane high yield is NOT ranked (can't fake top quintile)",
          results["BAD"]["fcfy_rank"] is None, str(results["BAD"]["fcfy_rank"]))
    check("missing yield is not ranked", results["NONE"]["fcfy_rank"] is None)
    check("top percentile is 1.0", approx(results["S0"]["fcfy_pctile"], 1.0))

    quintiles = {results[f"S{i}"]["fcfy_quintile"] for i in range(10)}
    check("all 5 quintiles populated with 10 names", quintiles == {1, 2, 3, 4, 5},
          str(sorted(quintiles)))

    # Degenerate universe smaller than the quantile count.
    tiny = {"A": {"ticker": "A", "fcf_yield": 0.05, "sane": True}}
    F.rank_by_fcf_yield(tiny, quantiles=5)
    check("single-name universe gets quintile 1, not 0",
          tiny["A"]["fcfy_quintile"] == 1, str(tiny["A"]["fcfy_quintile"]))

    empty: dict = {}
    F.rank_by_fcf_yield(empty, quantiles=5)
    check("empty universe does not raise", empty == {})


# ── 9. Cache behaviour ────────────────────────────────────────────────────────
def test_cache():
    section("9. Cache round-trip and TTL")

    # Re-enable caching just for this test, pointed at the temp dir.
    orig_ttl = F.CACHE_TTL_SEC
    F.CACHE_TTL_SEC = 7 * 86400.0
    F.clear_cache()
    try:
        tk = FakeTicker(
            info={"marketCap": 1000.0},
            quarterly=make_cashflow(ocf=[25, 25, 25, 25], capex=[-5, -5, -5, -5],
                                    dates=QUARTERS))

        first = F.get_fcf_yield("CACHED", tk=tk, use_cache=True)
        check("first call is not a cache hit", first.get("cached") is False,
              str(first.get("cached")))

        # No ticker passed: only a cache hit can answer this.
        second = F.get_fcf_yield("CACHED", use_cache=True)
        check("second call served from cache", second.get("cached") is True,
              str(second.get("cached")))
        check("cached value matches original", approx(second["fcf_yield"], first["fcf_yield"]),
              f"{second['fcf_yield']} vs {first['fcf_yield']}")
        check("cached source preserved", second["source"] == "quarterly_ttm",
              str(second["source"]))

        stats = F.cache_stats()
        check("cache_stats counts the entry", stats["entries"] >= 1, str(stats))
        check("cache_stats reports it fresh", stats["fresh"] >= 1, str(stats))

        n = F.clear_cache()
        check("clear_cache reports entries removed", n >= 1, str(n))
        check("cache is empty after clear", F.cache_stats()["entries"] == 0)

        # Expired entries must be ignored.
        F.get_fcf_yield("EXPIRED", tk=tk, use_cache=True)
        F._cache["EXPIRED"]["_cached_at"] = 0.0     # epoch = long expired
        stale = F._cache_get("EXPIRED")
        check("expired entry is not served", stale is None, str(stale))
    finally:
        F.CACHE_TTL_SEC = orig_ttl
        F.clear_cache()


# ── 10. Batch ─────────────────────────────────────────────────────────────────
def test_batch():
    section("10. Batch fetch and progress reporting")

    events = []
    # Offline: these symbols have no fixtures, so every one fails — which is
    # exactly what we want to test, since a real 500-name scan will contain
    # failures and must still complete.
    res = F.get_fcf_yields(["AAA", "BBB", "CCC"],
                           on_progress=lambda e: events.append(e),
                           use_cache=False)
    check("returns one entry per ticker", len(res) == 3, str(list(res)))
    check("keys are upper-cased", set(res) == {"AAA", "BBB", "CCC"}, str(set(res)))
    check("progress fired once per ticker", len(events) == 3, str(len(events)))
    check("progress carries current/total",
          events[-1]["_progress"] == {"current": 3, "total": 3},
          str(events[-1]["_progress"]))

    # A callback that raises must not abort the batch.
    def bad_cb(_):
        raise RuntimeError("callback blew up")

    res2 = F.get_fcf_yields(["XXX", "YYY"], on_progress=bad_cb, use_cache=False)
    check("broken progress callback does not abort batch", len(res2) == 2, str(len(res2)))


# ── 11. Live sanity (opt-in) ──────────────────────────────────────────────────
def test_live():
    section("11. LIVE yfinance schema check")

    F.clear_cache()
    # Known-good reference values measured during research. Ranges are wide
    # enough to absorb price moves but tight enough to catch a 4x error.
    expectations = {
        "MSFT": (0.010, 0.030),
        "AAPL": (0.020, 0.045),
        "T":    (0.060, 0.160),
    }
    for sym, (lo, hi) in expectations.items():
        r = F.get_fcf_yield(sym, use_cache=False)
        y = r["fcf_yield"]
        ok = y is not None and lo <= y <= hi
        check(f"{sym} FCFY in [{lo:.1%}, {hi:.1%}]", ok,
              f"got {y if y is None else format(y, '.4%')} via {r['source']}")
        check(f"{sym} used a clean 4-quarter TTM",
              r["source"] == "quarterly_ttm" and r["periods_used"] == 4,
              f"source={r['source']} periods={r['periods_used']}")

    # The original bug, re-verified against live data: if the info field still
    # disagrees materially with the statements, our preference for statements
    # is still load-bearing.
    try:
        import yfinance as yf
        tk   = yf.Ticker("MSFT")
        info = tk.info or {}
        raw  = info.get("freeCashflow")
        ours = F.get_fcf_yield("MSFT", use_cache=False)["fcf_ttm"]
        if raw and ours:
            ratio = ours / float(raw)
            print(f"  INFO  live MSFT: statements={ours/1e9:.1f}B  "
                  f"info['freeCashflow']={float(raw)/1e9:.1f}B  ratio={ratio:.2f}x")
            check("statement-derived FCF is the larger, credible figure",
                  ours >= float(raw) * 0.9,
                  "info field exceeded statements — investigate before trusting either")
    except Exception as e:
        print(f"  SKIP  live info-field comparison: {e}")


# ── Runner ────────────────────────────────────────────────────────────────────
def main() -> int:
    live = "--live" in sys.argv

    print("=" * 68)
    print("FUNDAMENTALS TEST SUITE — TTM FCF yield engine")
    print("=" * 68)

    test_ttm_arithmetic()
    test_capex_sign()
    test_rejects_bad_info_field()
    test_partial_quarters_rejected()
    test_column_ordering()
    test_market_cap()
    test_failures_are_graceful()
    test_ranking()
    test_cache()
    test_batch()

    if live:
        test_live()
    else:
        print("\n(skipping live yfinance checks — pass --live to include them)")

    print("\n" + "=" * 68)
    print(f"RESULT: {_passed} passed, {_failed} failed")
    print("=" * 68)
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
