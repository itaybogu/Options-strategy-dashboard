"""
Strategy 2: Calendar Spread Screener — Forward Factor Strategy
==============================================================
Logic is identical to the original calendar screener.
Adapted to be callable as a module; results streamed via a callback.
"""

from __future__ import annotations

import math
import time
import logging
from datetime import datetime, date
from io import StringIO
from typing import Optional, Callable

import requests
import yfinance as yf          # retained: fallback path inside data_provider
import pandas as pd

import data_provider
from option_pricing import atm_iv_from_chain, get_risk_free_rate, get_dividend_yield

log = logging.getLogger(__name__)


class DataSourceError(RuntimeError):
    """
    Raised when an upstream data source (ticker universe, batch price/volume
    download) fails hard enough that the scan cannot continue.

    Previously these paths called sys.exit(1). Inside the server's scanner
    thread that raised SystemExit, which killed the run without clearing the
    running flag and left the dashboard spinning forever. Raising a normal
    exception lets server.py mark just this strategy as failed and continue.
    """


# Same universe used by strategy_vol_momentum when the S&P 500 sources are
# unreachable. Keeps the calendar screener usable offline / when Wikipedia and
# the GitHub CSV mirror are both down, instead of aborting the whole run.
DEV_WATCHLIST: list[str] = [
    "AAPL", "NVDA", "MSFT", "HIMS", "COIN", "PLTR", "MSTR", "AMD",
    "TSLA", "META", "AMZN", "GOOGL", "JPM", "GS", "XOM", "CVX",
]


def _get_next_earnings(tk, today: date) -> tuple:
    """Returns (earn_days: int|None, earn_date: str|None)."""
    try:
        raw = tk.calendar
        if raw is None:
            return None, None
        if isinstance(raw, dict):
            dates = raw.get("Earnings Date")
            if dates is None:
                return None, None
            if not hasattr(dates, "__iter__") or isinstance(dates, str):
                dates = [dates]
        else:
            if "Earnings Date" not in raw.index:
                return None, None
            dates = raw.loc["Earnings Date"].dropna().tolist()
        future = []
        for d in dates:
            try:
                if hasattr(d, "date"):
                    d = d.date()
                elif isinstance(d, str):
                    d = date.fromisoformat(d[:10])
                if d >= today:
                    future.append(d)
            except Exception:
                continue
        if not future:
            return None, None
        nearest = min(future)
        return (nearest - today).days, nearest.strftime("%Y-%m-%d")
    except Exception:
        return None, None


# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────

MIN_VOL_SHARES    = 1_500_000
MAX_VOL_SHARES    = 5_000_000
VOL_LOOKBACK_DAYS = 30

MIN_FF    = 0.20
TOP_PAIRS = 3

# DTE rules (from strategy spec):
#   - Long leg (back month): max 90 DTE
#   - Short leg (front month): max 60 DTE  (= long DTE - min gap)
#   - Gap between legs: min 7 days, max 30 days
DTE_GAP_BUCKETS = [
    (20, 30),   # primary: 20–30d gap
    (10, 20),   # secondary: 10–20d gap
    (7,  10),   # tertiary: 7–10d gap
]

MIN_DTE = 7    # minimum DTE for either leg
MAX_DTE = 90   # maximum DTE for the long (back) leg

FETCH_DELAY = 1.5

MAX_BID_ASK_SPREAD = 0.30        # max (ask-bid)/mid on option legs

_GITHUB_CSV_URL = (
    "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
)
_WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://www.google.com/",
}

# ─────────────────────────────────────────────────────────────────────────────
# UNIVERSE
# ─────────────────────────────────────────────────────────────────────────────

def _clean_tickers(tickers: list[str]) -> list[str]:
    return [t.strip().replace(".", "-") for t in tickers if isinstance(t, str) and t.strip()]

def _fetch_github_csv() -> list[str]:
    resp = requests.get(_GITHUB_CSV_URL, timeout=15)
    resp.raise_for_status()
    df = pd.read_csv(StringIO(resp.text))
    if "Symbol" not in df.columns:
        raise ValueError(f"Unexpected columns: {df.columns.tolist()}")
    return _clean_tickers(df["Symbol"].tolist())

def _fetch_wiki() -> list[str]:
    resp = requests.get(_WIKI_URL, headers=_BROWSER_HEADERS, timeout=15)
    resp.raise_for_status()
    try:
        tables = pd.read_html(resp.text, attrs={"id": "constituents"}, flavor="lxml")
    except Exception:
        tables = pd.read_html(resp.text, attrs={"id": "constituents"})
    if not tables:
        raise ValueError("No table with id='constituents' found")
    return _clean_tickers(tables[0]["Symbol"].tolist())

def _get_sp500_tickers_uncached() -> list[str]:
    """
    S&P 500 universe with a graceful fallback chain:
        GitHub CSV mirror -> Wikipedia -> DEV_WATCHLIST

    Never aborts the process. If both remote sources fail we degrade to the
    dev watchlist and log loudly, so the scan still produces (fewer) results
    instead of killing the whole run.
    """
    for fetch_fn in (_fetch_github_csv, _fetch_wiki):
        try:
            tickers = fetch_fn()
            if len(tickers) > 400:
                return tickers
            log.warning(
                f"{fetch_fn.__name__} returned only {len(tickers)} tickers; trying next source."
            )
        except Exception as e:
            log.warning(f"Source failed: {e}")

    log.error(
        "All S&P 500 data sources failed \u2014 falling back to DEV_WATCHLIST "
        f"({len(DEV_WATCHLIST)} tickers). Results will be a partial universe."
    )
    return list(DEV_WATCHLIST)


def get_sp500_tickers() -> list[str]:
    """
    Cached S&P 500 universe (6h TTL by default, MC_UNIVERSE_TTL).

    Three callers want this list every cycle: this module, put_selling (which
    imports this function directly), and vol_momentum (via its own near-identical
    fetch_sp500_tickers, now routed to the same cache key). Index membership
    changes a handful of times a year, so re-fetching it per strategy per cycle
    was pure waste — and worse, it made the three strategies capable of scanning
    *different* universes within one cycle if Wikipedia changed mid-run.

    A copy is returned because callers slice and truncate the result (put_selling
    applies UNIVERSE_MAX to it); handing out the cached list itself would let one
    caller mutate the universe seen by the others.
    """
    try:
        import market_cache
        return list(market_cache.get_or_fetch(
            "universe:sp500", market_cache.UNIVERSE_TTL,
            _get_sp500_tickers_uncached,
        ))
    except Exception as e:
        log.warning(f"market_cache unavailable for S&P 500 list: {e}")
        return _get_sp500_tickers_uncached()

# ─────────────────────────────────────────────────────────────────────────────
# VOLUME FILTER
# ─────────────────────────────────────────────────────────────────────────────

def filter_by_volume(tickers: list[str]) -> tuple[list[str], dict]:
    """
    Batch pre-filter by 30-day average volume, applied BEFORE per-ticker
    scanning for performance (avoids fetching option chains for all 500
    S&P 500 tickers, which would take 12+ minutes and risk rate limits).

    Only a floor (MIN_VOL_SHARES) is enforced — the old 5M ceiling was
    arbitrary and excluded liquid large-caps for no strong reason.
    Returns (candidate_tickers, {ticker: avg_vol_millions}) so the volume
    value can be stored per-ticker and used by the frontend toggle.
    """
    try:
        raw = data_provider.download(
            tickers, period=f"{VOL_LOOKBACK_DAYS}d",
            auto_adjust=True, progress=False, threads=True,
        )
    except Exception as e:
        log.error(f"Batch download failed: {e}")
        raise DataSourceError(f"volume batch download failed: {e}") from e

    try:
        has_volume = "Volume" in raw.columns.get_level_values(0)
    except Exception:
        has_volume = "Volume" in getattr(raw, "columns", [])

    if not has_volume:
        log.error("No volume data returned.")
        raise DataSourceError("volume batch download returned no 'Volume' column")

    vol     = raw["Volume"]
    avg_vol = vol.mean()
    mask    = avg_vol >= MIN_VOL_SHARES
    passed  = avg_vol[mask].sort_values(ascending=False)
    vol_map = {t: round(v / 1_000_000, 3) for t, v in passed.items()}
    return passed.index.tolist(), vol_map

# ─────────────────────────────────────────────────────────────────────────────
# ATM IV / FORWARD IV / FF
# ─────────────────────────────────────────────────────────────────────────────


def _get_atm_row(chain, spot: float) -> Optional[dict]:
    """
    Return the bid/ask/mid of the ATM strike (closest to spot) from a chain.
    Used to expose live quotes alongside IV in the result dict.
    """
    if chain is None or chain.empty:
        return None
    chain = chain.copy().sort_values("strike").reset_index(drop=True)
    below = chain[chain["strike"] <= spot]
    above = chain[chain["strike"] >  spot]

    candidates = []
    if not below.empty:
        candidates.append(below.iloc[-1])
    if not above.empty:
        candidates.append(above.iloc[0])
    if not candidates:
        return None

    # Pick the one whose strike is closer to spot
    best = min(candidates, key=lambda r: abs(r["strike"] - spot))
    try:
        bid = float(best.get("bid", 0) or 0)
        ask = float(best.get("ask", 0) or 0)
        if bid > 0 and ask >= bid:
            mid = (bid + ask) / 2.0
            return {"bid": round(bid, 3), "ask": round(ask, 3), "mid": round(mid, 3)}
    except Exception:
        pass
    return None


def get_atm_iv(chain, spot: float, dte_days: int = 30,
               ticker: str = "", option_type: str = "call") -> Optional[float]:
    """
    Compute ATM IV using CRR binomial tree (matches Interactive Brokers).
    Interpolates between the two bracketing strikes around spot.

    No spread-width cap applied here — strikes just need a live two-sided
    quote to be usable for IV computation. The resulting leg's spread ratio
    is stored separately (short_spread / long_spread in the result) and
    filtering on it is left to the frontend "Spread ≤30%" toggle, so
    wide-spread tickers are still scored and shown rather than silently
    disappearing before they're ever evaluated.
    """
    if chain is None or chain.empty:
        return None
    r = get_risk_free_rate()
    q = get_dividend_yield(ticker, spot) if ticker else 0.0
    T = max(dte_days, 1) / 365.0

    def _has_quote(row) -> bool:
        try:
            bid_v = float(row.get("bid", 0) or 0)
            ask_v = float(row.get("ask", 0) or 0)
            return bid_v > 0 and ask_v > bid_v
        except Exception:
            return False

    chain_filt = chain[chain.apply(_has_quote, axis=1)]
    if chain_filt.empty:
        return None

    return atm_iv_from_chain(chain_filt, spot, T, r, q, option_type, model="crr")


def compute_forward_iv(iv1: float, t1: float, iv2: float, t2: float) -> Optional[float]:
    variance_diff = (iv2 ** 2) * t2 - (iv1 ** 2) * t1
    if variance_diff <= 0:
        return None
    return math.sqrt(variance_diff / (t2 - t1))

def compute_ff(iv_front: float, forward_iv: float) -> float:
    return (iv_front - forward_iv) / forward_iv

# ─────────────────────────────────────────────────────────────────────────────
# DTE PAIR SELECTION
# ─────────────────────────────────────────────────────────────────────────────

# Short leg cap: sell leg must be ≤ 60 DTE (long leg is capped by MAX_DTE=90)
MAX_SHORT_DTE = 60

def find_valid_dte_pairs(expiry_dates: list[date], today: date):
    dtemap = {}
    for exp in expiry_dates:
        dte = (exp - today).days
        if MIN_DTE <= dte <= MAX_DTE:
            dtemap[exp] = dte

    exps  = sorted(dtemap.keys())
    pairs = []
    seen  = set()

    for i, exp1 in enumerate(exps):
        dte1 = dtemap[exp1]
        if dte1 > MAX_SHORT_DTE:
            continue   # short (sell) leg must be ≤ 60 DTE
        for exp2 in exps[i + 1:]:
            dte2 = dtemap[exp2]
            gap  = dte2 - dte1
            for (min_gap, max_gap) in DTE_GAP_BUCKETS:
                if min_gap <= gap <= max_gap:
                    key = (exp1, exp2)
                    if key not in seen:
                        seen.add(key)
                        label = f"{min_gap}-{max_gap}d gap"
                        pairs.append((exp1, exp2, dte1, dte2, label))
                    break

    return pairs

# ─────────────────────────────────────────────────────────────────────────────
# PER-TICKER ANALYSIS
# ─────────────────────────────────────────────────────────────────────────────

def analyze_ticker(ticker: str, avg_vol_30d: Optional[float] = None) -> list[dict]:
    try:
        tk = data_provider.get_ticker(ticker)

        # Spot price: prefer fast_info.last_price (intraday), fall back to
        # most recent daily close. fast_info can return stale pre/post-market
        # prices so we cross-check against the last close.
        spot = None
        try:
            fi = tk.fast_info
            lp = getattr(fi, "last_price", None)
            if lp and lp > 0:
                spot = float(lp)
        except Exception:
            pass
        if spot is None or spot <= 0:
            try:
                h = tk.history(period="2d", auto_adjust=True)
                if h is not None and not h.empty:
                    spot = float(h["Close"].iloc[-1])
            except Exception:
                pass
        if spot is None or spot <= 0:
            return []

        expiry_strings = tk.options
        if not expiry_strings or len(expiry_strings) < 2:
            return []

        today        = date.today()
        expiry_dates = []
        for es in expiry_strings:
            try:
                expiry_dates.append(datetime.strptime(es, "%Y-%m-%d").date())
            except ValueError:
                pass

        valid_pairs = find_valid_dte_pairs(expiry_dates, today)
        if not valid_pairs:
            return []

        # Fetch next earnings date once per ticker
        try:
            earn_days, earn_date = _get_next_earnings(tk, today)
        except Exception:
            earn_days, earn_date = None, None

        chain_cache: dict[date, pd.DataFrame] = {}

        def fetch_calls(exp: date) -> Optional[pd.DataFrame]:
            if exp in chain_cache:
                return chain_cache[exp]
            exp_str = exp.strftime("%Y-%m-%d")
            try:
                chain = tk.option_chain(exp_str)
                calls = chain.calls if hasattr(chain, "calls") else None
                chain_cache[exp] = calls
                return calls
            except Exception as e:
                log.debug(f"{ticker} {exp_str}: fetch failed: {e}")
                chain_cache[exp] = None
                return None

        results = []

        for (exp1, exp2, dte1, dte2, bucket) in valid_pairs:
            t1 = dte1 / 365.0
            t2 = dte2 / 365.0

            calls1 = fetch_calls(exp1)
            calls2 = fetch_calls(exp2)

            iv1 = get_atm_iv(calls1, spot, dte_days=dte1, ticker=ticker, option_type="call")
            iv2 = get_atm_iv(calls2, spot, dte_days=dte2, ticker=ticker, option_type="call")

            if iv1 is None or iv2 is None:
                continue

            fwd_iv = compute_forward_iv(iv1, t1, iv2, t2)
            if fwd_iv is None:
                continue

            ff = compute_ff(iv1, fwd_iv)
            if ff < MIN_FF:
                continue

            def atm_strike(chain, spot):
                if chain is None or chain.empty:
                    return None
                c = chain.copy()
                c["d"] = (c["strike"] - spot).abs()
                return float(c.sort_values("d").iloc[0]["strike"])

            # Fetch bid/ask for each leg at the ATM strike
            quote_short = _get_atm_row(calls1, spot)
            quote_long  = _get_atm_row(calls2, spot)

            results.append({
                "ticker":       ticker,
                "spot":         round(spot, 2),
                "exp_short":    exp1.strftime("%Y-%m-%d"),
                "exp_long":     exp2.strftime("%Y-%m-%d"),
                "dte_short":    dte1,
                "dte_long":     dte2,
                "gap_days":     dte2 - dte1,
                "bucket":       bucket,
                "iv_short":     round(iv1 * 100, 2),
                "iv_long":      round(iv2 * 100, 2),
                "forward_iv":   round(fwd_iv * 100, 2),
                "ff":           round(ff * 100, 2),
                "strike_short": atm_strike(calls1, spot),
                "strike_long":  atm_strike(calls2, spot),
                "short_bid":    quote_short["bid"] if quote_short else None,
                "short_ask":    quote_short["ask"] if quote_short else None,
                "short_mid":    quote_short["mid"] if quote_short else None,
                "long_bid":     quote_long["bid"]  if quote_long  else None,
                "long_ask":     quote_long["ask"]  if quote_long  else None,
                "long_mid":     quote_long["mid"]  if quote_long  else None,
                "avg_vol_30d":  avg_vol_30d,
                "leg_spread_max": round(max(
                    (quote_short["ask"] - quote_short["bid"]) / quote_short["mid"] if quote_short and quote_short.get("mid") else 0,
                    (quote_long["ask"]  - quote_long["bid"])  / quote_long["mid"]  if quote_long  and quote_long.get("mid")  else 0,
                ), 4),
                "earn_days":    earn_days,
                "earn_date":    earn_date,
            })

        results.sort(key=lambda x: x["ff"], reverse=True)
        return results[:TOP_PAIRS]

    except Exception as e:
        log.warning(f"{ticker}: unexpected error — {e}")
        return []

# ─────────────────────────────────────────────────────────────────────────────
# MAIN RUNNER
# ─────────────────────────────────────────────────────────────────────────────

def run(
    on_progress: Optional[Callable[[dict], None]] = None,
) -> list[dict]:
    """
    Run the calendar spread screen.
    on_progress(event_dict) called after each ticker.
    Returns full list of candidate pairs.
    """
    tickers = get_sp500_tickers()
    candidates, vol_map = filter_by_volume(tickers)

    if not candidates:
        log.error("No tickers passed the volume filter.")
        return []

    all_results = []
    total       = len(candidates)

    for i, ticker in enumerate(candidates, 1):
        results = analyze_ticker(ticker, avg_vol_30d=vol_map.get(ticker))

        progress_event = {
            "ticker":   ticker,
            "found":    len(results),
            "pairs":    results,
            "_progress": {"current": i, "total": total},
        }

        if results:
            all_results.extend(results)

        if on_progress:
            on_progress(progress_event)

        time.sleep(FETCH_DELAY)

    return all_results
