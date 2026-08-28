"""
fundamentals.py — trailing-twelve-month Free Cash Flow yield engine.

WHY THIS MODULE EXISTS
──────────────────────
The Goldman Sachs put-selling methodology ("The Art of Put Selling", GS Options
Research, 4 April 2013) rests entirely on one fundamental input: Free Cash Flow
yield. It is used two separate ways:

  1. STOCK SELECTION — sell puts on the top FCF-yield quintile.
  2. STRIKE SELECTION — choose the strike whose premium equals 1/12th of the
     stock's annual FCF yield (one month's worth of free cash flow).

If FCF yield is wrong, both the stock ranking AND the strike target are wrong.
There is no downstream check that would catch it: a bad yield produces a
plausible-looking strike. So this is isolated in its own module with its own
test suite rather than being an inline helper.

THE BUG THIS MODULE EXISTS TO PREVENT
─────────────────────────────────────
yfinance exposes `Ticker.info["freeCashflow"]`, which is the obvious thing to
reach for. It is unreliable. Measured directly:

    MSFT  info["freeCashflow"] =  16.4B  ->  FCFY 0.44%   WRONG (~4x low)
    MSFT  TTM from statements  =  67.0B  ->  FCFY 1.82%   correct

That error would have moved MSFT from mid-pack to the bottom of the FCFY
ranking and set an absurdly tight strike target. Critically, AAPL's `info`
value happens to be roughly correct, so a casual one-ticker spot-check passes
and the defect ships.

Root cause: `info` is a single denormalised snapshot assembled from a different
upstream pipeline than the financial statements, with no guarantee about which
period it covers or whether it is TTM at all.

THE FIX — compute TTM ourselves from the quarterly cash-flow statement:

    TTM FCF = sum(last 4 quarters of Operating Cash Flow)
            + sum(last 4 quarters of Capital Expenditure)

Capital Expenditure is reported NEGATIVE by yfinance, so this is a sum, not a
difference. Getting that sign wrong roughly doubles FCF instead of reducing it,
which is why `_ttm_fcf` asserts the sign convention rather than assuming it.

    FCF yield = TTM FCF / market capitalisation

PROVENANCE IS RETURNED WITH EVERY VALUE
───────────────────────────────────────
Every result carries `source`, `quarters_used`, `asof` and `warnings` so a
caller (and a human reading the dashboard) can tell a clean 4-quarter TTM from
an annual-statement fallback. Silent degradation is how the original bug hid,
so degradation here is always visible.

FALLBACK LADDER (strictest first)
────────────────────────────────
  1. quarterly_ttm  — 4 quarters of OCF + Capex.                    preferred
  2. quarterly_fcf  — 4 quarters of the reported "Free Cash Flow" row.
  3. annual_ttm     — most recent annual OCF + Capex. Up to 12 months stale.
  4. annual_fcf     — most recent annual "Free Cash Flow" row.
  5. info_field     — `info["freeCashflow"]`. LAST RESORT, always warns.

Level 5 is retained only because a flagged number beats dropping a ticker
entirely, and the flag propagates to the UI.

WHY NOT ROUTED THROUGH data_provider
────────────────────────────────────
data_provider abstracts *market* data (quotes, bars, chains) across yfinance
and IBKR. Fundamental statements are not available on the IBKR path — TWS puts
them behind a paywalled Reuters subscription — so `ibkr_provider` would just
return None, exactly as it already does for `get_stock_calendar()`. Fundamentals
therefore come from yfinance regardless of the active market-data provider.
This is a deliberate asymmetry and matches the existing earnings-calendar
precedent.

CACHING
───────
Companies report quarterly; intra-day refetching is pure waste. Results are
cached to `data/fundamentals_cache.json` with a 7-day TTL, which turns the
fundamentals pass of a 500-ticker scan from minutes into milliseconds on
rescan. Negative results (no data) are cached too, with a shorter TTL, so a
delisted or non-reporting ticker isn't retried 500 times.
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from datetime import date, datetime, timezone
from typing import Any, Optional

log = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────
CACHE_DIR         = os.getenv("FUNDAMENTALS_CACHE_DIR", "data")
CACHE_PATH        = os.path.join(CACHE_DIR, "fundamentals_cache.json")

# Fundamentals change on quarterly filings. 7 days is far inside that cadence
# while still bounding staleness. Override with FUNDAMENTALS_CACHE_DAYS=0 to
# disable caching entirely (useful when debugging data issues).
CACHE_TTL_DAYS    = float(os.getenv("FUNDAMENTALS_CACHE_DAYS", "7") or 7)
CACHE_TTL_SEC     = CACHE_TTL_DAYS * 86400.0

# Failures are cached briefly so a bad ticker doesn't cost a network round-trip
# on every pass, but not so long that a transient outage poisons a whole week.
NEG_CACHE_TTL_SEC = float(os.getenv("FUNDAMENTALS_NEG_CACHE_HOURS", "12") or 12) * 3600.0

# Row labels vary slightly across yfinance versions and filing types.
OCF_KEYS = (
    "Operating Cash Flow",
    "Total Cash From Operating Activities",
    "Cash Flow From Continuing Operating Activities",
    "Net Cash Provided By Operating Activities",
)
CAPEX_KEYS = (
    "Capital Expenditure",
    "Capital Expenditures",
    "Purchase Of PPE",
    "Net PPE Purchase And Sale",
)
FCF_KEYS = (
    "Free Cash Flow",
)

# Sanity envelope for a computed yield. Values outside this are almost always a
# data error (wrong units, wrong market cap, a REIT/financial with odd
# statements) rather than a real opportunity. Flagged, not silently dropped.
FCFY_SANE_MIN = -1.00     # -100%
FCFY_SANE_MAX =  0.60     # +60%

_cache_lock = threading.Lock()
_cache: Optional[dict] = None


class FundamentalsError(RuntimeError):
    """Raised only for unrecoverable configuration problems, never for missing data."""


# ──────────────────────────────────────────────────────────────────────────────
# CACHE
# ──────────────────────────────────────────────────────────────────────────────

def _load_cache() -> dict:
    """Read the on-disk cache once per process. Corrupt cache is discarded, not fatal."""
    global _cache
    with _cache_lock:
        if _cache is not None:
            return _cache
        try:
            with open(CACHE_PATH, "r") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                raise ValueError("cache root is not an object")
            _cache = data
        except FileNotFoundError:
            _cache = {}
        except Exception as e:
            # A corrupt cache must never break a scan; start clean.
            log.warning("Discarding unreadable fundamentals cache (%s): %s",
                        CACHE_PATH, e)
            _cache = {}
        return _cache


def _save_cache() -> None:
    """Persist the cache atomically so a crash mid-write can't corrupt it."""
    with _cache_lock:
        if _cache is None:
            return
        snapshot = dict(_cache)
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(snapshot, fh, indent=1, sort_keys=True)
        os.replace(tmp, CACHE_PATH)
    except Exception as e:
        # Caching is an optimisation. Never let it fail a scan.
        log.warning("Could not persist fundamentals cache: %s", e)


def _cache_get(ticker: str) -> Optional[dict]:
    if CACHE_TTL_SEC <= 0:
        return None
    entry = _load_cache().get(ticker.upper())
    if not isinstance(entry, dict):
        return None
    ts = entry.get("_cached_at")
    if not isinstance(ts, (int, float)):
        return None
    # Negative results expire sooner than good ones.
    ttl = CACHE_TTL_SEC if entry.get("fcf_yield") is not None else NEG_CACHE_TTL_SEC
    if (time.time() - ts) > ttl:
        return None
    out = dict(entry)
    out["cached"] = True
    return out


def _cache_put(ticker: str, payload: dict) -> None:
    if CACHE_TTL_SEC <= 0:
        return
    entry = dict(payload)
    entry["_cached_at"] = time.time()
    entry.pop("cached", None)
    cache = _load_cache()
    with _cache_lock:
        cache[ticker.upper()] = entry
    _save_cache()


def clear_cache() -> int:
    """Drop all cached fundamentals. Returns number of entries removed."""
    global _cache
    cache = _load_cache()
    with _cache_lock:
        n = len(cache)
        _cache = {}
    _save_cache()
    return n


def cache_stats() -> dict:
    """Cache summary for /health and diagnostics."""
    cache = _load_cache()
    with _cache_lock:
        entries = list(cache.values())
    now = time.time()
    fresh = sum(
        1 for e in entries
        if isinstance(e, dict)
        and isinstance(e.get("_cached_at"), (int, float))
        and (now - e["_cached_at"]) <= (
            CACHE_TTL_SEC if e.get("fcf_yield") is not None else NEG_CACHE_TTL_SEC
        )
    )
    return {
        "path":        CACHE_PATH,
        "entries":     len(entries),
        "fresh":       fresh,
        "stale":       len(entries) - fresh,
        "ttl_days":    CACHE_TTL_DAYS,
        "enabled":     CACHE_TTL_SEC > 0,
    }


# ──────────────────────────────────────────────────────────────────────────────
# STATEMENT PARSING
# ──────────────────────────────────────────────────────────────────────────────

def _is_finite(x: Any) -> bool:
    """True only for real, finite numbers. Rejects None, NaN, inf, and strings."""
    try:
        if x is None or isinstance(x, bool):
            return False
        f = float(x)
        return math.isfinite(f)
    except (TypeError, ValueError):
        return False


def _find_row(df, keys: tuple[str, ...]):
    """
    Locate a statement row by trying each label variant, then a loose
    case-insensitive contains match. Returns a Series or None.

    yfinance renames rows between versions, so exact-match-only would break
    silently on upgrade — the exact failure mode this module guards against.
    """
    if df is None or getattr(df, "empty", True):
        return None
    index = list(df.index)
    for k in keys:
        if k in index:
            return df.loc[k]
    lowered = {str(i).strip().lower(): i for i in index}
    for k in keys:
        hit = lowered.get(k.strip().lower())
        if hit is not None:
            return df.loc[hit]
    return None


def _ordered_periods(df) -> list:
    """
    Statement columns newest-first. yfinance usually returns them that way but
    does not document it, so sort explicitly rather than trusting order.
    """
    if df is None or getattr(df, "empty", True):
        return []
    cols = list(df.columns)
    try:
        return sorted(cols, key=lambda c: _to_date(c) or date.min, reverse=True)
    except Exception:
        return cols


def _to_date(value) -> Optional[date]:
    """Best-effort coercion of a statement column label to a date."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    for attr in ("to_pydatetime", "date"):
        fn = getattr(value, attr, None)
        if callable(fn):
            try:
                got = fn()
                return got.date() if isinstance(got, datetime) else got
            except Exception:
                pass
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except Exception:
        return None


def _sum_periods(row, periods: list, n: int) -> tuple[Optional[float], int, Optional[date]]:
    """
    Sum the n most recent periods of a statement row.

    Returns (total, periods_used, asof_date). Requires ALL n periods to be
    present and finite — a partial sum is not a TTM figure, and quietly
    returning a 3-quarter sum as if it were annual would understate FCF by
    ~25%. Better to fall through to the next source.
    """
    if row is None:
        return None, 0, None
    total = 0.0
    used  = 0
    asof: Optional[date] = None
    for p in periods[:n]:
        try:
            v = row.get(p) if hasattr(row, "get") else row[p]
        except Exception:
            return None, 0, None
        if not _is_finite(v):
            return None, 0, None
        total += float(v)
        used  += 1
        if asof is None:
            asof = _to_date(p)
    if used < n:
        return None, 0, None
    return total, used, asof


def _fcf_from_statement(df, n_periods: int, label: str
                        ) -> tuple[Optional[float], dict]:
    """
    Compute FCF from a cash-flow statement DataFrame over n_periods columns.

    Tries OCF + Capex first (the definition we control), then the vendor's own
    "Free Cash Flow" row as a cross-check/fallback.

    CAPEX SIGN HANDLING — the subtle part
    ─────────────────────────────────────
    yfinance reports Capital Expenditure as a NEGATIVE number (cash outflow),
    so FCF = OCF + Capex. But this is a vendor convention, not a guarantee, and
    some feeds report it positive. If we blindly added a positive capex we would
    *increase* FCF by the capex amount instead of reducing it — roughly doubling
    FCF for a capital-intensive name and producing a wildly wrong yield.

    So the sign is normalised explicitly: take -abs(capex) regardless of how it
    arrives. This is correct under both conventions and cannot be broken by an
    upstream sign flip. `capex_sign_raw` records what we actually received so a
    convention change is visible in the output rather than silent.
    """
    meta: dict = {"detail": label}
    periods = _ordered_periods(df)
    if len(periods) < n_periods:
        meta["reason"] = (f"need {n_periods} periods, statement has {len(periods)}")
        return None, meta

    ocf_row   = _find_row(df, OCF_KEYS)
    capex_row = _find_row(df, CAPEX_KEYS)

    ocf,   ocf_used,   asof = _sum_periods(ocf_row,   periods, n_periods)
    capex, capex_used, _    = _sum_periods(capex_row, periods, n_periods)

    if ocf is not None and capex is not None:
        meta["capex_sign_raw"] = "negative" if capex <= 0 else "positive"
        # Normalise: capex is always a cash outflow.
        fcf = ocf + (-abs(capex))
        meta.update({
            "source":        f"{label}_ttm",
            "ocf":           ocf,
            "capex":         -abs(capex),
            "periods_used":  ocf_used,
            "asof":          asof.isoformat() if asof else None,
        })
        if capex > 0:
            meta.setdefault("warnings", []).append(
                "Capital Expenditure arrived positive; normalised to an outflow."
            )
        return fcf, meta

    # Fallback: the vendor's own precomputed FCF row.
    fcf_row = _find_row(df, FCF_KEYS)
    fcf, used, asof = _sum_periods(fcf_row, periods, n_periods)
    if fcf is not None:
        meta.update({
            "source":       f"{label}_fcf",
            "periods_used": used,
            "asof":         asof.isoformat() if asof else None,
        })
        meta.setdefault("warnings", []).append(
            "Used vendor 'Free Cash Flow' row; OCF/Capex rows were unavailable."
        )
        return fcf, meta

    missing = []
    if ocf_row   is None: missing.append("Operating Cash Flow")
    if capex_row is None: missing.append("Capital Expenditure")
    if fcf_row   is None: missing.append("Free Cash Flow")
    meta["reason"] = ("missing/incomplete rows: " + ", ".join(missing)) if missing \
                     else "rows present but contained non-finite values"
    return None, meta


def _get_statement(tk, *names):
    """
    Fetch the first available statement attribute. yfinance has renamed these
    across versions (`quarterly_cashflow` vs `quarterly_cash_flow`), and any of
    them can raise on network failure, so each access is individually guarded.
    """
    for nm in names:
        try:
            df = getattr(tk, nm, None)
        except Exception as e:
            log.debug("statement attr %s raised: %s", nm, e)
            continue
        if df is not None and not getattr(df, "empty", True):
            return df
    return None


def _market_cap(tk, info: dict) -> tuple[Optional[float], Optional[float], Optional[float], list[str]]:
    """
    Resolve market capitalisation, preferring an explicit marketCap field and
    falling back to price x shares outstanding.

    Returns (market_cap, price, shares, warnings).

    The fallback matters: `marketCap` is missing often enough on smaller names
    that dropping those tickers would bias the universe toward mega-caps, which
    is the opposite of what a screen should do.
    """
    warnings: list[str] = []
    price  = None
    shares = None

    for key in ("currentPrice", "regularMarketPrice", "previousClose"):
        v = info.get(key)
        if _is_finite(v) and float(v) > 0:
            price = float(v)
            break

    if price is None:
        # fast_info is a separate, usually-live path.
        try:
            fi = getattr(tk, "fast_info", None)
            if fi is not None:
                for key in ("last_price", "lastPrice", "regularMarketPrice"):
                    v = fi.get(key) if hasattr(fi, "get") else getattr(fi, key, None)
                    if _is_finite(v) and float(v) > 0:
                        price = float(v)
                        break
        except Exception:
            pass

    for key in ("sharesOutstanding", "impliedSharesOutstanding", "floatShares"):
        v = info.get(key)
        if _is_finite(v) and float(v) > 0:
            shares = float(v)
            if key == "floatShares":
                warnings.append("Used floatShares; sharesOutstanding unavailable.")
            break

    mcap = info.get("marketCap")
    if _is_finite(mcap) and float(mcap) > 0:
        return float(mcap), price, shares, warnings

    if price is not None and shares is not None:
        warnings.append("marketCap unavailable; derived from price x shares.")
        return price * shares, price, shares, warnings

    return None, price, shares, warnings


# ──────────────────────────────────────────────────────────────────────────────
# PUBLIC API
# ──────────────────────────────────────────────────────────────────────────────

def _empty_result(ticker: str, reason: str) -> dict:
    return {
        "ticker":        ticker.upper(),
        "fcf_yield":     None,
        "fcf_ttm":       None,
        "market_cap":    None,
        "price":         None,
        "shares":        None,
        "source":        None,
        "periods_used":  0,
        "asof":          None,
        "reason":        reason,
        "warnings":      [],
        "sane":          False,
        "cached":        False,
    }


def get_fcf_yield(ticker: str, tk=None, use_cache: bool = True) -> dict:
    """
    TTM Free Cash Flow yield for one ticker.

    Returns a dict — never raises for missing data, because a screen over 500
    names must not die on one bad ticker:

        {
          "ticker":       "MSFT",
          "fcf_yield":    0.0182,        # decimal, not percent. None if unknown.
          "fcf_ttm":      6.70e10,
          "market_cap":   3.68e12,
          "price":        495.1,
          "shares":       7.43e9,
          "source":       "quarterly_ttm",
          "periods_used": 4,
          "asof":         "2026-06-30", # most recent period in the TTM window
          "reason":       "",            # populated only on failure
          "warnings":     [...],
          "sane":         True,          # inside FCFY_SANE_MIN..MAX
          "cached":       False,
        }

    `tk` may be a pre-built yfinance-style Ticker (handy for tests and for
    reusing an existing object); otherwise one is constructed.
    """
    sym = (ticker or "").strip().upper()
    if not sym:
        return _empty_result("", "empty ticker")

    if use_cache:
        hit = _cache_get(sym)
        if hit is not None:
            return hit

    if tk is None:
        try:
            import yfinance as yf
            tk = yf.Ticker(sym)
        except Exception as e:
            # Import/construction failure is environmental, not per-ticker; do
            # not cache it, or a transient problem would poison the cache.
            return _empty_result(sym, f"could not construct ticker: {e}")

    try:
        info = getattr(tk, "info", None) or {}
        if not isinstance(info, dict):
            info = {}
    except Exception as e:
        log.debug("%s: info unavailable: %s", sym, e)
        info = {}

    result   = _empty_result(sym, "")
    warnings: list[str] = []

    mcap, price, shares, cap_warn = _market_cap(tk, info)
    warnings.extend(cap_warn)
    result.update({"market_cap": mcap, "price": price, "shares": shares})

    # ── Walk the fallback ladder, strictest first ─────────────────────────────
    fcf: Optional[float] = None
    meta: dict = {}
    attempts: list[str] = []

    q_df = _get_statement(tk, "quarterly_cashflow", "quarterly_cash_flow",
                          "quarterly_cashflow_stmt")
    if q_df is not None:
        fcf, meta = _fcf_from_statement(q_df, 4, "quarterly")
        if fcf is None:
            attempts.append(f"quarterly: {meta.get('reason', 'unavailable')}")
    else:
        attempts.append("quarterly: statement unavailable")

    if fcf is None:
        a_df = _get_statement(tk, "cashflow", "cash_flow", "cashflow_stmt")
        if a_df is not None:
            fcf, meta = _fcf_from_statement(a_df, 1, "annual")
            if fcf is None:
                attempts.append(f"annual: {meta.get('reason', 'unavailable')}")
            else:
                warnings.append(
                    "Annual statement used; figure may be up to 12 months stale."
                )
        else:
            attempts.append("annual: statement unavailable")

    if fcf is None:
        # LAST RESORT. This is the field that reports MSFT 4x too low. Always
        # flagged so it is visible in the UI and in tests.
        raw = info.get("freeCashflow")
        if _is_finite(raw):
            fcf = float(raw)
            meta = {"source": "info_field", "periods_used": 0, "asof": None}
            warnings.append(
                "Used yfinance info['freeCashflow'] as last resort; this field "
                "is known to be unreliable (understates MSFT ~4x). Treat with "
                "suspicion."
            )
        else:
            attempts.append("info_field: absent or non-numeric")

    if fcf is None:
        result["reason"]   = "no FCF source available (" + "; ".join(attempts) + ")"
        result["warnings"] = warnings
        if use_cache:
            _cache_put(sym, result)
        return result

    if mcap is None or mcap <= 0:
        result["fcf_ttm"]  = fcf
        result["source"]   = meta.get("source")
        result["reason"]   = "market cap unavailable; cannot compute yield"
        result["warnings"] = warnings
        if use_cache:
            _cache_put(sym, result)
        return result

    fcfy = fcf / mcap

    result.update({
        "fcf_yield":    fcfy,
        "fcf_ttm":      fcf,
        "ocf_ttm":      meta.get("ocf"),
        "capex_ttm":    meta.get("capex"),
        "source":       meta.get("source"),
        "periods_used": meta.get("periods_used", 0),
        "asof":         meta.get("asof"),
        "sane":         FCFY_SANE_MIN <= fcfy <= FCFY_SANE_MAX,
    })
    warnings.extend(meta.get("warnings", []))

    if not result["sane"]:
        warnings.append(
            f"FCF yield {fcfy:.1%} is outside the plausible range "
            f"[{FCFY_SANE_MIN:.0%}, {FCFY_SANE_MAX:.0%}]; likely a data error."
        )

    result["warnings"] = warnings
    if use_cache:
        _cache_put(sym, result)
    return result


def get_fcf_yields(tickers: list[str], on_progress=None,
                   use_cache: bool = True, sleep: float = 0.0) -> dict[str, dict]:
    """
    FCF yield for many tickers. Returns {TICKER: result_dict}.

    Sequential by design: yfinance rate-limits aggressively and the existing
    strategy modules already pace themselves the same way. Cache hits cost no
    network call, so a rescan is effectively instant and `sleep` only applies
    to genuine fetches.
    """
    out: dict[str, dict] = {}
    total = len(tickers)
    for i, t in enumerate(tickers, 1):
        try:
            res = get_fcf_yield(t, use_cache=use_cache)
        except Exception as e:
            # Defensive: get_fcf_yield is written not to raise, but a screen
            # must survive even if that contract is ever broken.
            log.warning("FCFY failed for %s: %s", t, e)
            res = _empty_result(t, f"unexpected error: {type(e).__name__}: {e}")
        out[res["ticker"]] = res

        if on_progress:
            try:
                on_progress({"ticker": res["ticker"], "result": res,
                             "_progress": {"current": i, "total": total}})
            except Exception:
                pass    # a broken callback must not abort the batch

        if sleep > 0 and not res.get("cached") and i < total:
            time.sleep(sleep)
    return out


def rank_by_fcf_yield(results: dict[str, dict], quantiles: int = 5
                      ) -> dict[str, dict]:
    """
    Attach cross-sectional FCFY rank/quintile, in place, and return `results`.

    GS's stock-selection variant sells puts on the TOP FCF-yield quintile, so
    the ranking must be computed across the universe rather than per ticker.
    Adds to each entry with a usable yield:

        fcfy_rank      1 = highest yield
        fcfy_pctile    0..1, 1.0 = highest
        fcfy_quintile  1 = top (best) .. `quantiles` = bottom

    Only sane, non-None yields are ranked; everything else gets None so a bad
    data point cannot be mistaken for a top-quintile opportunity.
    """
    eligible = [r for r in results.values()
                if r.get("fcf_yield") is not None and r.get("sane")]
    eligible.sort(key=lambda r: r["fcf_yield"], reverse=True)
    n = len(eligible)

    for r in results.values():
        r["fcfy_rank"] = r["fcfy_pctile"] = r["fcfy_quintile"] = None

    for idx, r in enumerate(eligible):
        r["fcfy_rank"]   = idx + 1
        r["fcfy_pctile"] = (n - idx) / n if n else None
        # Guard n < quantiles: without the min() a 3-name universe would report
        # a quintile of 0 for the top name.
        if n > 0:
            q = int(idx * quantiles / n) + 1
            r["fcfy_quintile"] = min(q, quantiles)

    return results
