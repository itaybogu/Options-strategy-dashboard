"""
Strategy 1: Options Volatility Surface & Momentum Screener — S&P 500
=====================================================================
Logic is identical to the original options_vol_screener_live.py.
Adapted to be callable as a module; results streamed via a callback.
"""

from __future__ import annotations

import math
import statistics
import warnings
import datetime
import time
from io import StringIO
from typing import Optional, Callable

import numpy as np
import pandas as pd
import requests
from scipy.stats import norm as scipy_norm
from option_pricing import implied_vol, mid_price, get_risk_free_rate, get_dividend_yield

import data_provider

try:
    import yfinance as yf
except ImportError as e:
    # Raise ImportError (not SystemExit) so an import failure inside the
    # server's scanner thread is catchable and reported per-strategy instead
    # of terminating the interpreter.
    raise ImportError("yfinance not installed \u2014 run: pip install yfinance") from e

warnings.filterwarnings("ignore")

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────

WEIGHT_F1 = 0.40
WEIGHT_F2 = 0.30
WEIGHT_F3 = 0.20
WEIGHT_F4 = 0.10

DTE_MIN                = 5
DTE_MAX                = 15
LONG_DELTA_LO          = 0.45
LONG_DELTA_HI          = 0.55
SHORT_DELTA_LO         = 0.10
SHORT_DELTA_HI         = 0.25
EARNINGS_BLACKOUT_DAYS = 15

MIN_AVG_VOLUME     = 1_500_000   # shares/day (30-day avg stock volume)
MAX_BID_ASK_SPREAD = 0.30        # max (ask-bid)/mid ratio on selected option legs

MOM_WINDOW_SHORT = 21
MOM_WINDOW_MED   = 63

INTER_TICKER_SLEEP = 1.5
TRADING_DAYS_YEAR  = 252

SECTOR_ETF: dict[str, str] = {
    "Technology":             "XLK",
    "Financial Services":     "XLF",
    "Health Care":            "XLV",
    "Consumer Cyclical":      "XLY",
    "Consumer Defensive":     "XLP",
    "Energy":                 "XLE",
    "Industrials":            "XLI",
    "Basic Materials":        "XLB",
    "Utilities":              "XLU",
    "Real Estate":            "XLRE",
    "Communication Services": "XLC",
}

BENCHMARK_SYMBOLS = ["SPY"] + list(SECTOR_ETF.values()) + ["^IRX"]

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

DEV_WATCHLIST: list[str] = [
    "AAPL", "NVDA", "MSFT", "HIMS", "COIN", "PLTR", "MSTR", "AMD",
    "TSLA", "META", "AMZN", "GOOGL", "JPM", "GS", "XOM", "CVX",
]

# ─────────────────────────────────────────────────────────────────────────────
# BENCHMARK DATA
# ─────────────────────────────────────────────────────────────────────────────

class BenchmarkData:
    __slots__ = (
        "spy_log_returns", "sector_log_returns", "risk_free_rate",
        "spy_r21d", "sector_r21d",
    )
    def __init__(self):
        self.spy_log_returns:    Optional[np.ndarray]  = None
        self.sector_log_returns: dict[str, np.ndarray] = {}
        self.risk_free_rate:     float                 = 0.05
        self.spy_r21d:           float                 = 0.0
        self.sector_r21d:        dict[str, float]      = {}


def fetch_benchmarks() -> BenchmarkData:
    bd = BenchmarkData()
    try:
        raw = data_provider.download(
            BENCHMARK_SYMBOLS, period="1y", auto_adjust=True,
            progress=False, threads=True,
        )
    except Exception:
        return bd

    def _get_closes(symbol: str) -> Optional[np.ndarray]:
        try:
            if isinstance(raw.columns, pd.MultiIndex):
                closes = raw["Close"][symbol].dropna().values.astype(float)
            else:
                closes = raw["Close"].dropna().values.astype(float)
            return closes if len(closes) >= 22 else None
        except (KeyError, TypeError):
            return None

    spy_closes = _get_closes("SPY")
    if spy_closes is not None:
        bd.spy_log_returns = np.diff(np.log(spy_closes))
        n = len(bd.spy_log_returns)
        bd.spy_r21d = float(np.sum(bd.spy_log_returns[-min(MOM_WINDOW_SHORT, n):]))

    for etf in SECTOR_ETF.values():
        closes = _get_closes(etf)
        if closes is not None:
            lr = np.diff(np.log(closes))
            bd.sector_log_returns[etf] = lr
            n = len(lr)
            bd.sector_r21d[etf] = float(np.sum(lr[-min(MOM_WINDOW_SHORT, n):]))

    try:
        if isinstance(raw.columns, pd.MultiIndex):
            irx_series = raw["Close"]["^IRX"].dropna()
        else:
            irx_series = raw["Close"].dropna()
        if len(irx_series) > 0:
            bd.risk_free_rate = float(irx_series.iloc[-1]) / 100.0
    except (KeyError, TypeError, IndexError):
        pass

    return bd

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

def _fetch_sp500_tickers_uncached() -> list[str]:
    for fetch_fn in (_fetch_github_csv, _fetch_wiki):
        try:
            tickers = fetch_fn()
            if len(tickers) > 400:
                return tickers
        except Exception:
            pass
    return DEV_WATCHLIST


def fetch_sp500_tickers() -> list[str]:
    """
    S&P 500 universe, sharing one cache entry with strategy_calendar and
    put_selling (key "universe:sp500", 6h TTL).

    This module and strategy_calendar had independent, near-identical fetch
    chains hitting the same two remote sources every cycle. Beyond the wasted
    requests, independent fetches meant the strategies could disagree about the
    universe within a single cycle if a source updated mid-run — one scanning
    503 names and another 504, with no indication anything differed.

    The local fallback still differs by design: this module falls back to its
    own DEV_WATCHLIST. That only applies when both remote sources fail, and
    whichever strategy populates the shared entry first now defines the
    universe for all of them, which is the consistent behaviour we want.
    """
    try:
        import market_cache
        return list(market_cache.get_or_fetch(
            "universe:sp500", market_cache.UNIVERSE_TTL,
            _fetch_sp500_tickers_uncached,
        ))
    except Exception:
        return _fetch_sp500_tickers_uncached()

# ─────────────────────────────────────────────────────────────────────────────
# SCORING FUNCTIONS
# ─────────────────────────────────────────────────────────────────────────────

def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))

def score_f1_skew_edge(raw_skew: float) -> float:
    if raw_skew >= 0.0:
        return 0.0
    return _clamp((-raw_skew / 0.30) * 100.0)

def score_m1_time_series(r_21d: float) -> float:
    return _clamp(50.0 + (r_21d / 0.10) * 50.0)

def score_m2_cross_sectional(rank: int, total: int) -> float:
    if total <= 1:
        return 50.0
    return _clamp((rank / (total - 1)) * 100.0)

def score_m3_market_alpha(r_21d_stock: float, r_21d_spy: float) -> float:
    alpha = r_21d_stock - r_21d_spy
    return _clamp(50.0 + (alpha / 0.05) * 50.0)

def score_m4_sector_alpha(r_21d_stock: float, sector: Optional[str], bd: BenchmarkData) -> float:
    if sector is None:
        return 50.0
    etf = SECTOR_ETF.get(sector)
    if etf is None or etf not in bd.sector_r21d:
        return 50.0
    alpha = r_21d_stock - bd.sector_r21d[etf]
    return _clamp(50.0 + (alpha / 0.05) * 50.0)

def score_m5_volume_momentum(volume_history: np.ndarray) -> float:
    n = len(volume_history)
    if n < MOM_WINDOW_MED:
        return 50.0
    vol_short = float(np.mean(volume_history[-MOM_WINDOW_SHORT:]))
    vol_long  = float(np.mean(volume_history[-MOM_WINDOW_MED:]))
    if vol_long <= 0:
        return 50.0
    return _clamp((vol_short / vol_long / 2.0) * 100.0)

def score_m6_52w_high(spot: float, high_52w: Optional[float]) -> float:
    if high_52w is None or high_52w <= 0:
        return 50.0
    return _clamp((spot / high_52w) * 100.0)

def score_f2_momentum_composite(m1, m2, m3, m4, m5, m6) -> float:
    return (m1 + m2 + m3 + m4 + m5 + m6) / 6.0

def score_f2_direction(f2: float, direction: str) -> float:
    if direction.upper() == "CALL":
        return _clamp(f2)
    return _clamp(100.0 - f2)

def score_f3_vrp(vrp: float) -> float:
    if vrp <= 0.0:
        return 100.0
    return _clamp(100.0 * (1.0 - vrp / 0.05))

def score_f4_skew_z(skew_z: float) -> float:
    return _clamp(50.0 - 25.0 * skew_z)

def composite_score(f1, f2, f3, f4) -> float:
    return WEIGHT_F1 * f1 + WEIGHT_F2 * f2 + WEIGHT_F3 * f3 + WEIGHT_F4 * f4

def assign_conclusion(score: float) -> str:
    if score >= 75.0:
        return "BUY SPREAD"
    if score >= 55.0:
        return "CONSIDER"
    return "AVOID"

# ─────────────────────────────────────────────────────────────────────────────
# BLACK-SCHOLES DELTA
# ─────────────────────────────────────────────────────────────────────────────

def bs_delta(spot, strike, iv, dte_days, option_type, r) -> Optional[float]:
    T = dte_days / 365.0
    if T <= 0.0 or iv <= 0.0 or spot <= 0.0 or strike <= 0.0:
        return None
    try:
        d1 = (math.log(spot / strike) + (r + 0.5 * iv ** 2) * T) / (iv * math.sqrt(T))
        if option_type.lower() == "call":
            return float(scipy_norm.cdf(d1))
        else:
            return float(scipy_norm.cdf(-d1))
    except (ValueError, ZeroDivisionError, OverflowError):
        return None

# ─────────────────────────────────────────────────────────────────────────────
# LIVE DATA HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _safe_float(val, default=None):
    try:
        v = float(val)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default

def get_history(ticker_obj) -> Optional[pd.DataFrame]:
    hist = ticker_obj.history(period="1y", auto_adjust=True)
    if hist is None or hist.empty or len(hist) < MOM_WINDOW_MED + 2:
        return None
    return hist

def get_spot_price(ticker_obj, hist) -> Optional[float]:
    try:
        spot = _safe_float(ticker_obj.fast_info.get("lastPrice"))
        if spot and spot > 0:
            return spot
    except Exception:
        pass
    try:
        if hist is not None and not hist.empty:
            return float(hist["Close"].iloc[-1])
    except Exception:
        pass
    return None

def compute_rv20(log_returns: np.ndarray) -> Optional[float]:
    if len(log_returns) < 20:
        return None
    return float(np.std(log_returns[-20:], ddof=1)) * math.sqrt(TRADING_DAYS_YEAR)

def get_next_earnings(ticker_obj, today: datetime.date) -> tuple:
    """Returns (days_until: int|None, date_str: str|None)."""
    try:
        cal = ticker_obj.calendar
        if cal is None:
            return None, None
        if isinstance(cal, dict):
            earn_dates = cal.get("Earnings Date")
            if earn_dates is None:
                return None, None
            if not hasattr(earn_dates, "__iter__") or isinstance(earn_dates, str):
                earn_dates = [earn_dates]
        else:
            if "Earnings Date" not in cal.index:
                return None, None
            earn_dates = cal.loc["Earnings Date"].dropna().tolist()
        future = []
        for d in earn_dates:
            try:
                if isinstance(d, datetime.datetime):
                    d = d.date()
                elif isinstance(d, str):
                    d = datetime.date.fromisoformat(d[:10])
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

def days_to_earnings(ticker_obj, today: datetime.date) -> Optional[int]:
    days, _ = get_next_earnings(ticker_obj, today)
    return days

def pick_best_expiry(ticker_obj, today: datetime.date):
    try:
        expirations = ticker_obj.options
    except Exception:
        return None, None
    target_dte = (DTE_MIN + DTE_MAX) / 2.0
    best_exp, best_dte, best_dist = None, None, float("inf")
    for exp_str in expirations:
        try:
            dte  = (datetime.date.fromisoformat(exp_str) - today).days
            dist = abs(dte - target_dte)
            if DTE_MIN <= dte <= DTE_MAX and dist < best_dist:
                best_exp, best_dte, best_dist = exp_str, dte, dist
        except ValueError:
            continue
    return best_exp, best_dte

def extract_legs(chain_df, spot, dte_days, option_type, r, ticker: str = ""):
    """
    Select the long (50Δ) and short (15Δ) legs from the option chain.
    IV is computed from bid/ask mid using the CRR binomial tree (matches IB).
    This correctly prices both ATM and OTM options — critical because
    the short leg is OTM and yfinance impliedVolatility uses stale lastPrice.

    ── Why we do NOT use yfinance `impliedVolatility` (verified 2026-08-02) ──
    Our IV reads ~1-1.5 pts below Yahoo's for the same strike. This is
    EXPECTED and our value is the more accurate one. Evidence:

    1. Put/call parity test (model-free, tests/test_iv_parity.py). Call IV and
       put IV at the same strike must agree. Measured over 233 liquid
       AAPL/MSFT/SPY strikes:
           Yahoo impliedVolatility : 2.18 pts median call-vs-put disagreement
           our mid-based CRR IV    : 0.35 pts   <- 6x more self-consistent
       Yahoo's number is internally inconsistent; ours is not.

    2. Yahoo inverts `lastPrice`, which is the last TRADE — often hours stale
       and struck at a different spot. Measured 25-79% of near-ATM lastPrice
       values fell OUTSIDE the current bid/ask. Some are plainly broken
       (AAPL 255C: last=82.85 vs mid=54.15). Our mid is always executable.

    3. Not a model/convention difference. Solving for the effective T that
       reproduces Yahoo's IV returns ~the actual DTE (1.66 vs 1, 3.38 vs 3,
       4.92 vs 5), so no hidden day-count or rate offset explains the gap.
       American (CRR) vs European (BS) moves IV only ~0.05 pts here.

    Consequence: Yahoo's IV is biased HIGH by stale ITM-side prints. Adopting
    it would inflate our vol-momentum signal and make spreads look cheaper
    than they are fillable. Mid-based IV is what we can actually trade on.
    """
    if chain_df is None or chain_df.empty:
        return None, None

    T = max(dte_days, 1) / 365.0
    q = get_dividend_yield(ticker, spot) if ticker else 0.0

    long_cands, short_cands = [], []

    for _, row in chain_df.iterrows():
        strike = _safe_float(row.get("strike"))
        if strike is None or strike <= 0:
            continue

        # Liquidity filter applied to every strike (ATM and OTM): a strike is
        # only usable if it has a live two-sided quote (bid > 0, ask > bid).
        # No spread-width cap here — a wide spread doesn't make the strike
        # unusable for selection, it just makes the resulting leg lower
        # quality. The actual spread ratio is stored in the result
        # (leg_spread_max) and filtering on it is left to the frontend
        # "Spread ≤30%" toggle, so users can see and control this rather
        # than have tickers silently disappear before they're ever scored.
        try:
            bid_v = float(row.get("bid", 0) or 0)
            ask_v = float(row.get("ask", 0) or 0)
            if bid_v <= 0 or ask_v <= bid_v:
                continue   # no live quote — reject (can't price this at all)
            mid_v = (bid_v + ask_v) / 2.0
            if mid_v <= 0:
                continue
        except Exception:
            continue       # any parse error → reject

        # Compute IV from bid/ask mid via CRR (IB model)
        iv = implied_vol(mid_v, spot, strike, T, r, q, option_type, model="crr")
        if iv is None:
            continue

        delta = bs_delta(spot, strike, iv, dte_days, option_type, r)
        if delta is None:
            continue

        if LONG_DELTA_LO  <= delta <= LONG_DELTA_HI:
            long_cands.append((abs(delta - 0.50), strike, iv, delta, bid_v, ask_v))
        if SHORT_DELTA_LO <= delta <= SHORT_DELTA_HI:
            short_cands.append((abs(delta - 0.15), strike, iv, delta, bid_v, ask_v))

    def _best(cands):
        if not cands:
            return None
        cands.sort(key=lambda x: x[0])
        _, strike, iv, delta, bid, ask = cands[0]
        return {"strike": strike, "iv": iv, "bs_delta": delta,
                "bid": round(bid, 3), "ask": round(ask, 3),
                "mid": round((bid + ask) / 2, 3)}

    return _best(long_cands), _best(short_cands)

# ─────────────────────────────────────────────────────────────────────────────
# TWO-PASS POST-PROCESSING
# ─────────────────────────────────────────────────────────────────────────────

def apply_cross_sectional_scores(results: list[dict]) -> None:
    scored = [r for r in results if r.get("status") == "SCORED"]
    n = len(scored)
    if n == 0:
        return

    if n > 1:
        sorted_by_ret = sorted(scored, key=lambda r: r["r_21d"])
        rank_map = {r["ticker"]: i for i, r in enumerate(sorted_by_ret)}
        for r in scored:
            rank    = rank_map[r["ticker"]]
            m2      = score_m2_cross_sectional(rank, n)
            r["m2"] = m2
            f2_bull = score_f2_momentum_composite(r["m1"], m2, r["m3"], r["m4"], r["m5"], r["m6"])
            r["f2_bull"] = f2_bull
            r["f2"]      = score_f2_direction(f2_bull, r["direction"])
    else:
        r = scored[0]
        r["m2"] = 50.0
        f2_bull = score_f2_momentum_composite(r["m1"], 50.0, r["m3"], r["m4"], r["m5"], r["m6"])
        r["f2_bull"] = f2_bull
        r["f2"]      = score_f2_direction(f2_bull, r["direction"])

    skews = [r["raw_skew"] for r in scored]
    med   = statistics.median(skews)
    mad   = statistics.median([abs(s - med) for s in skews])

    for r in scored:
        z  = (r["raw_skew"] - med) / mad if mad > 1e-8 else 0.0
        f4 = score_f4_skew_z(z)
        r["skew_z"]    = z
        r["f4"]        = f4
        r["composite"] = composite_score(r["f1"], r["f2"], r["f3"], f4)
        r["conclusion"] = assign_conclusion(r["composite"])

# ─────────────────────────────────────────────────────────────────────────────
# SINGLE-TICKER PIPELINE
# ─────────────────────────────────────────────────────────────────────────────

def screen_ticker(ticker: str, today: datetime.date, bd: BenchmarkData,
                  direction_override: Optional[str] = None) -> dict:
    base = {"ticker": ticker.upper()}
    tk   = data_provider.get_ticker(ticker)

    try:
        hist = get_history(tk)
    except Exception as e:
        return {**base, "status": "FILTERED", "reason": f"History error: {e}"}

    if hist is None:
        return {**base, "status": "FILTERED", "reason": "Insufficient history"}

    closes  = hist["Close"].dropna().values.astype(float)
    volumes = hist["Volume"].dropna().values.astype(float)

    if len(closes) < 22:
        return {**base, "status": "FILTERED", "reason": "Insufficient close data"}

    log_returns = np.diff(np.log(closes))

    spot = get_spot_price(tk, hist)
    if spot is None or spot <= 0:
        return {**base, "status": "FILTERED", "reason": "Spot price unavailable"}

    rv20 = compute_rv20(log_returns)
    if rv20 is None:
        return {**base, "status": "FILTERED", "reason": "Cannot compute RV20"}

    n_ret = len(log_returns)
    r_21d = float(np.sum(log_returns[-min(MOM_WINDOW_SHORT, n_ret):]))

    high_52w: Optional[float] = None
    try:
        info     = tk.info
        sector   = info.get("sector")
        high_52w = _safe_float(info.get("fiftyTwoWeekHigh"))
    except Exception:
        info   = {}
        sector = None

    if high_52w is None or high_52w <= 0:
        high_52w = float(np.max(closes)) if len(closes) > 0 else None

    # Compute 30-day avg volume — stored in result for frontend filtering
    avg_vol_30d = float(np.mean(volumes[-30:])) if len(volumes) >= 30 else float(np.mean(volumes))

    m1 = score_m1_time_series(r_21d)
    m2 = 50.0
    m3 = score_m3_market_alpha(r_21d, bd.spy_r21d)
    m4 = score_m4_sector_alpha(r_21d, sector, bd)
    m5 = score_m5_volume_momentum(volumes)
    m6 = score_m6_52w_high(spot, high_52w)

    f2_bull   = score_f2_momentum_composite(m1, m2, m3, m4, m5, m6)
    direction = direction_override.upper() if direction_override else ("CALL" if f2_bull >= 50.0 else "PUT")
    f2        = score_f2_direction(f2_bull, direction)

    try:
        earn_days, earn_date = get_next_earnings(tk, today)
    except Exception:
        earn_days, earn_date = None, None

    try:
        exp_str, dte = pick_best_expiry(tk, today)
    except Exception as e:
        return {**base, "status": "FILTERED", "reason": f"Options list error: {e}"}

    if exp_str is None:
        return {**base, "status": "FILTERED",
                "reason": f"No expiry in DTE [{DTE_MIN},{DTE_MAX}]"}

    try:
        chain    = tk.option_chain(exp_str)
        chain_df = chain.calls if direction == "CALL" else chain.puts
    except Exception as e:
        return {**base, "status": "FILTERED", "reason": f"Chain error: {e}"}

    r_f = get_risk_free_rate()  # live ^IRX rate, same as IB
    long_leg, short_leg = extract_legs(chain_df, spot, dte, direction.lower(), r_f, ticker=ticker)

    if long_leg is None:
        return {**base, "status": "FILTERED",
                "reason": f"No 50Δ leg in [{LONG_DELTA_LO},{LONG_DELTA_HI}]"}
    if short_leg is None:
        return {**base, "status": "FILTERED",
                "reason": f"No 15Δ leg in [{SHORT_DELTA_LO},{SHORT_DELTA_HI}]"}

    atm_iv = long_leg["iv"]
    otm_iv = short_leg["iv"]
    if atm_iv <= 0:
        return {**base, "status": "FILTERED", "reason": "ATM IV zero or invalid"}

    raw_skew = (atm_iv - otm_iv) / atm_iv
    vrp      = atm_iv - rv20
    f1       = score_f1_skew_edge(raw_skew)
    f3       = score_f3_vrp(vrp)
    f4       = 50.0
    comp     = composite_score(f1, f2, f3, f4)

    return {
        **base,
        "status":       "SCORED",
        "direction":    direction,
        "dte":          dte,
        "expiry":       exp_str,
        "spot":         spot,
        "sector":       sector,
        "avg_vol_30d":  round(avg_vol_30d / 1_000_000, 3),   # millions, for frontend filter
        "long_strike":  long_leg["strike"],
        "short_strike": short_leg["strike"],
        "long_delta":   long_leg["bs_delta"],
        "short_delta":  short_leg["bs_delta"],
        "long_bid":     long_leg.get("bid"),
        "long_ask":     long_leg.get("ask"),
        "long_mid":     long_leg.get("mid"),
        "short_bid":    short_leg.get("bid"),
        "short_ask":    short_leg.get("ask"),
        "short_mid":    short_leg.get("mid"),
        "leg_spread_max": round(max(
            (long_leg["ask"]  - long_leg["bid"])  / long_leg["mid"]  if long_leg.get("mid")  else 0,
            (short_leg["ask"] - short_leg["bid"]) / short_leg["mid"] if short_leg.get("mid") else 0,
        ), 4),  # max spread ratio across both legs, for frontend filter
        "atm_iv":       atm_iv,
        "otm_iv":       otm_iv,
        "rv20":         rv20,
        "vrp":          vrp,
        "raw_skew":     raw_skew,
        "skew_z":       0.0,
        "r_21d":        r_21d,
        "high_52w":     high_52w,
        "earn_days":    earn_days,
        "earn_date":    earn_date,
        "m1": m1, "m2": m2, "m3": m3, "m4": m4, "m5": m5, "m6": m6,
        "f2_bull":  f2_bull,
        "f1": f1, "f2": f2, "f3": f3, "f4": f4,
        "composite":  comp,
        "conclusion": assign_conclusion(comp),
    }

# ─────────────────────────────────────────────────────────────────────────────
# MAIN RUNNER  (callable from orchestrator)
# ─────────────────────────────────────────────────────────────────────────────

def run(
    watchlist: Optional[list[str]] = None,
    direction_override: Optional[str] = None,
    on_progress: Optional[Callable[[dict], None]] = None,
) -> list[dict]:
    """
    Run the vol/momentum screen.
    on_progress(result_dict) called after each ticker completes.
    Returns full results list after two-pass post-processing.
    """
    today = datetime.date.today()

    bd = fetch_benchmarks()

    if watchlist is None:
        watchlist = fetch_sp500_tickers()

    results: list[dict] = []
    total = len(watchlist)

    for i, ticker in enumerate(watchlist, 1):
        try:
            result = screen_ticker(ticker, today, bd, direction_override)
        except Exception as e:
            result = {"ticker": ticker.upper(), "status": "FILTERED",
                      "reason": f"Unexpected error: {e}"}

        result["_progress"] = {"current": i, "total": total}
        results.append(result)

        if on_progress:
            on_progress(result)

        if i < total:
            time.sleep(INTER_TICKER_SLEEP)

    apply_cross_sectional_scores(results)

    # Re-emit final scored results so UI gets updated composites
    if on_progress:
        for r in results:
            if r.get("status") == "SCORED":
                on_progress({**r, "_final_update": True})

    return results
