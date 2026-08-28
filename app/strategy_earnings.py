"""
Strategy 3: Earnings Options Scanner
=====================================
Logic is identical to the original scanner.py.
Adapted to be callable as a module; results streamed via a callback.
"""

from __future__ import annotations

import sys
import math
import re
import json
import time
import random
import requests
import yfinance as yf          # retained: fallback path inside data_provider
import numpy as np

import data_provider
from datetime import datetime, timedelta
from typing import Optional, Callable
from scipy.interpolate import interp1d

# ─────────────────────────────────────────────────────────────────────────────
# EARNINGS CALENDAR  (3-layer fallback)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_earnings_calendar(days_ahead: int = 7) -> list[dict]:
    today = datetime.today().date()
    end   = today + timedelta(days=days_ahead)

    # --- 1. yf.Calendars ---
    try:
        cal = yf.Calendars(
            start=today.strftime("%Y-%m-%d"),
            end=end.strftime("%Y-%m-%d"),
        )
        df = cal.get_earnings_calendar(limit=500)
        if df is not None and not df.empty:
            results = _parse_yf_calendar_df(df)
            if results:
                return results
    except Exception:
        pass

    # --- 2. Nasdaq API ---
    try:
        results = _fetch_nasdaq_calendar(today, end)
        if results:
            return results
    except Exception:
        pass

    # --- 3. Yahoo scrape ---
    try:
        results = _fetch_yahoo_calendar_scrape(today, end)
        if results:
            return results
    except Exception:
        pass

    return []


def _parse_yf_calendar_df(df) -> list[dict]:
    results, seen = [], set()
    for symbol, row in df.iterrows():
        symbol = str(symbol).strip().upper()
        if not symbol or "." in symbol or symbol in seen:
            continue
        seen.add(symbol)
        raw_date = row.get("Event Start Date", None)
        try:
            edate = raw_date.date().strftime("%Y-%m-%d") if hasattr(raw_date, "date") else str(raw_date)[:10]
        except Exception:
            edate = "—"
        ttype      = str(row.get("Start Datetime Type", "TNS")).strip().upper()
        time_label = {"BMO": "Pre-mkt", "AMC": "After-mkt", "TNS": "TBD"}.get(ttype, ttype)
        results.append({
            "ticker":        symbol,
            "company":       str(row.get("Company", "")).strip(),
            "earnings_date": edate,
            "earnings_time": time_label,
        })
    return results


def _fetch_nasdaq_calendar(start_date, end_date) -> list[dict]:
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept":     "application/json, text/plain, */*",
        "Origin":     "https://www.nasdaq.com",
        "Referer":    "https://www.nasdaq.com/market-activity/earnings",
    })
    results, seen = [], set()
    current = start_date
    while current <= end_date:
        date_str = current.strftime("%Y-%m-%d")
        url = f"https://api.nasdaq.com/api/calendar/earnings?date={date_str}"
        try:
            resp = session.get(url, timeout=10)
            resp.raise_for_status()
            rows = resp.json().get("data", {}).get("rows") or []
            for row in rows:
                sym = str(row.get("symbol", "")).strip().upper()
                if not sym or sym in seen:
                    continue
                seen.add(sym)
                raw_time   = str(row.get("time", "")).lower()
                time_label = "Pre-mkt" if "pre" in raw_time else ("After-mkt" if "after" in raw_time or "post" in raw_time else "TBD")
                results.append({
                    "ticker":        sym,
                    "company":       str(row.get("name", "")).strip(),
                    "earnings_date": date_str,
                    "earnings_time": time_label,
                })
        except Exception:
            pass
        time.sleep(random.uniform(0.8, 1.5))
        current += timedelta(days=1)
    return results


def _fetch_yahoo_calendar_scrape(start_date, end_date) -> list[dict]:
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        session.get("https://finance.yahoo.com", timeout=8)
        time.sleep(1.0)
    except Exception:
        pass
    results, seen = [], set()
    current = start_date
    while current <= end_date:
        date_str = current.strftime("%Y-%m-%d")
        url = f"https://finance.yahoo.com/calendar/earnings?day={date_str}"
        try:
            resp  = session.get(url, timeout=12)
            match = re.search(r'"earnings":\s*\{"rows":\s*(\[.*?\])', resp.text, re.DOTALL)
            if match:
                rows = json.loads(match.group(1))
                for row in rows:
                    sym = str(row.get("ticker", "")).strip().upper()
                    if not sym or sym in seen:
                        continue
                    seen.add(sym)
                    raw_time   = str(row.get("startdatetimetype", "")).upper()
                    time_label = {"BMO": "Pre-mkt", "AMC": "After-mkt"}.get(raw_time, "TBD")
                    results.append({
                        "ticker":        sym,
                        "company":       str(row.get("companyshortname", "")).strip(),
                        "earnings_date": date_str,
                        "earnings_time": time_label,
                    })
        except Exception:
            pass
        time.sleep(random.uniform(1.0, 2.0))
        current += timedelta(days=1)
    return results

# ─────────────────────────────────────────────────────────────────────────────
# SESSION / RATE-LIMIT HELPERS
# ─────────────────────────────────────────────────────────────────────────────

_UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
]


def _make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent":                random.choice(_UA_POOL),
        "Accept":                    "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language":           "en-US,en;q=0.9",
        "Accept-Encoding":           "gzip, deflate, br",
        "Connection":                "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    })
    try:
        s.get("https://finance.yahoo.com", timeout=10)
        time.sleep(random.uniform(0.5, 1.2))
    except Exception:
        pass
    return s


def _ticker(symbol: str):
    """
    Single chokepoint for every per-symbol data fetch in this module.

    Returns whatever the active provider serves: an IBKRTicker under "ibkr",
    a yf.Ticker under "yfinance". Deliberately un-annotated — the return type
    is provider-dependent, and naming yf.Ticker here would misrepresent the
    IBKR path. The `session` is only meaningful to yfinance (UA rotation to
    dodge Yahoo throttling) and is ignored by IBKR.
    """
    return data_provider.get_ticker(symbol, session=_make_session())


def _sleep(base: float, jitter: float = 0.5) -> None:
    time.sleep(max(0.1, base + random.uniform(-jitter, jitter)))


def _fetch_with_retry(fn, retries: int = 5, base_delay: float = 3.0):
    for attempt in range(retries):
        try:
            return fn()
        except Exception as e:
            err = str(e).lower()
            is_rate = any(k in err for k in (
                "429", "rate limit", "too many requests", "timeout",
                "connection", "read timed out", "try after",
                "please try", "throttl", "slowdown", "toomany",
            ))
            if attempt < retries - 1:
                if is_rate:
                    delay = max(10.0, base_delay * (2 ** attempt)) + random.uniform(0, 5)
                    time.sleep(delay)
                else:
                    raise
            else:
                raise
    raise RuntimeError("Max retries exceeded.")

# ─────────────────────────────────────────────────────────────────────────────
# CORE ANALYSIS
# ─────────────────────────────────────────────────────────────────────────────

def filter_dates(dates):
    today  = datetime.today().date()
    cutoff = today + timedelta(days=45)
    sorted_dates = sorted(datetime.strptime(d, "%Y-%m-%d").date() for d in dates)
    for i, d in enumerate(sorted_dates):
        if d >= cutoff:
            arr = [x.strftime("%Y-%m-%d") for x in sorted_dates[: i + 1]]
            if arr and arr[0] == today.strftime("%Y-%m-%d"):
                return arr[1:]
            return arr
    raise ValueError("No expiry ≥ 45 days out.")


def yang_zhang(price_data, window=30, trading_periods=252):
    """
    Yang-Zhang volatility estimator.
    Guards against negative variance (rare, but possible with the
    Rogers-Satchell component on certain OHLC patterns) which would
    otherwise produce NaN from sqrt() of a negative number — that NaN
    then propagates into iv30_rv30 and breaks JSON serialization.
    """
    log_ho = (price_data["High"] / price_data["Open"]).apply(np.log)
    log_lo = (price_data["Low"]  / price_data["Open"]).apply(np.log)
    log_co = (price_data["Close"]/ price_data["Open"]).apply(np.log)
    log_oc = (price_data["Open"] / price_data["Close"].shift(1)).apply(np.log)
    log_cc = (price_data["Close"]/ price_data["Close"].shift(1)).apply(np.log)
    rs        = log_ho * (log_ho - log_co) + log_lo * (log_lo - log_co)
    close_vol = (log_cc ** 2).rolling(window).sum() / (window - 1)
    open_vol  = (log_oc ** 2).rolling(window).sum() / (window - 1)
    window_rs = rs.rolling(window).sum() / (window - 1)
    k = 0.34 / (1.34 + (window + 1) / (window - 1))
    variance = open_vol + k * close_vol + (1 - k) * window_rs
    variance = variance.clip(lower=0)   # guard against negative variance → NaN
    result   = variance.apply(np.sqrt) * np.sqrt(trading_periods)
    val = result.iloc[-1]
    if val is None or (isinstance(val, float) and math.isnan(val)) or not np.isfinite(val) or val <= 0:
        return None
    return float(val)


def build_term_structure(days, ivs):
    days, ivs = np.array(days), np.array(ivs)
    idx       = days.argsort()
    days, ivs = days[idx], ivs[idx]
    spline    = interp1d(days, ivs, kind="linear", fill_value="extrapolate")
    def ts(dte):
        if dte < days[0]:  return ivs[0]
        if dte > days[-1]: return ivs[-1]
        return float(spline(dte))
    return ts


def _mid(df, idx):
    try:
        bid, ask = df.loc[idx, "bid"], df.loc[idx, "ask"]
        if bid is not None and ask is not None:
            return (float(bid) + float(ask)) / 2.0
    except Exception:
        pass
    return None


def analyse(symbol: str, meta: dict | None = None) -> dict:
    ticker = symbol.strip().upper()
    base = {
        "ticker":        ticker,
        "company":       (meta or {}).get("company", ""),
        "earnings_date": (meta or {}).get("earnings_date", "—"),
        "earnings_time": (meta or {}).get("earnings_time", "—"),
        "timestamp":     datetime.now().isoformat(timespec="seconds"),
        "error":         None,
    }

    stock = _ticker(ticker)
    _sleep(2.0)

    try:
        options = _fetch_with_retry(lambda: stock.options, retries=5, base_delay=4.0)
    except Exception:
        _sleep(5.0, jitter=2.0)
        stock = _ticker(ticker)
        try:
            options = _fetch_with_retry(lambda: stock.options, retries=3, base_delay=6.0)
        except Exception as e:
            return {**base, "error": f"options fetch failed: {e}"}

    if not options:
        return {**base, "error": "no options found"}

    try:
        exp_dates = filter_dates(list(options))
    except ValueError as e:
        return {**base, "error": str(e)}

    chains = {}
    for i, exp in enumerate(exp_dates):
        if i > 0:
            _sleep(4.0, jitter=1.5)
        try:
            chains[exp] = _fetch_with_retry(lambda d=exp: stock.option_chain(d))
        except Exception:
            continue

    if not chains:
        return {**base, "error": "all chain fetches failed"}

    _sleep(1.5)
    try:
        hist1d = _fetch_with_retry(lambda: stock.history(period="1d"))
        if hist1d.empty:
            raise ValueError("empty")
        spot = hist1d["Close"].iloc[0]
    except Exception as e:
        return {**base, "error": f"spot price: {e}"}

    atm_iv, straddle = {}, None
    call_bid = call_ask = call_mid = None
    put_bid  = put_ask  = put_mid  = None
    # ATM strike + front-expiry IV of the *first* expiry, captured inside the
    # i == 0 branch below. `ci` is rebound on every iteration, so it cannot be
    # read after the loop -- it would point at the last expiry, not the front.
    atm_strike  = None
    atm_iv_used = None
    for i, (exp, chain) in enumerate(chains.items()):
        calls, puts = chain.calls, chain.puts
        if calls.empty or puts.empty:
            continue
        ci = (calls["strike"] - spot).abs().idxmin()
        pi = (puts["strike"]  - spot).abs().idxmin()
        atm_iv[exp] = (calls.loc[ci, "impliedVolatility"] +
                       puts.loc[pi,  "impliedVolatility"]) / 2.0
        if i == 0:
            cm, pm = _mid(calls, ci), _mid(puts, pi)
            if cm and pm:
                straddle = cm + pm
            # Front-expiry ATM strike + blended IV, for the payoff chart.
            try:
                atm_strike = float(calls.loc[ci, "strike"])
            except Exception:
                pass
            try:
                v = float(atm_iv[exp])
                if math.isfinite(v) and v > 0:
                    atm_iv_used = v
            except Exception:
                pass
            # Store individual leg bid/ask for display
            try:
                call_bid  = float(calls.loc[ci, "bid"]  or 0) or None
                call_ask  = float(calls.loc[ci, "ask"]  or 0) or None
                call_mid  = cm
            except Exception:
                pass
            try:
                put_bid   = float(puts.loc[pi, "bid"]   or 0) or None
                put_ask   = float(puts.loc[pi, "ask"]   or 0) or None
                put_mid   = pm
            except Exception:
                pass

    if not atm_iv:
        return {**base, "error": "could not determine ATM IV"}

    today = datetime.today().date()
    dtes  = [(datetime.strptime(d, "%Y-%m-%d").date() - today).days for d in atm_iv]
    ivs   = list(atm_iv.values())
    ts    = build_term_structure(dtes, ivs)
    slope = (ts(45) - ts(dtes[0])) / (45 - dtes[0]) if dtes[0] != 45 else 0.0

    _sleep(1.5)
    try:
        ph = _fetch_with_retry(lambda: stock.history(period="3mo"))

        # Drop rows with missing/invalid OHLCV data before computing anything.
        # A single bad row (e.g. a holiday Yahoo mishandled, a data gap) would
        # otherwise poison every 30-day rolling window that includes it —
        # this is why RV30 was failing for many otherwise-liquid names.
        # Dropping bad rows is data hygiene, not a change to the Yang-Zhang
        # formula itself.
        ohlcv_cols = ["Open", "High", "Low", "Close", "Volume"]
        ph = ph.dropna(subset=[c for c in ohlcv_cols if c in ph.columns])
        # Also drop rows where OHLC ordering is invalid (High < Open/Close,
        # Low > Open/Close) — these break the Rogers-Satchell component's
        # non-negativity guarantee and indicate corrupt data for that day.
        if all(c in ph.columns for c in ("Open", "High", "Low", "Close")):
            valid_ohlc = (
                (ph["High"] >= ph["Open"])  & (ph["High"] >= ph["Close"]) &
                (ph["Low"]  <= ph["Open"])  & (ph["Low"]  <= ph["Close"]) &
                (ph["Open"] > 0) & (ph["High"] > 0) & (ph["Low"] > 0) & (ph["Close"] > 0)
            )
            ph = ph[valid_ohlc]

        if len(ph) < 31:
            return {**base, "error": f"Insufficient clean history ({len(ph)} valid rows, need 31)"}

        rv30 = yang_zhang(ph)

        vol_roll = ph["Volume"].rolling(30).mean().dropna()
        if vol_roll.empty:
            return {**base, "error": "Insufficient volume history for 30d average"}
        avg_vol = vol_roll.iloc[-1]
    except Exception as e:
        return {**base, "error": f"historical data: {e}"}

    if rv30 is None or rv30 <= 0:
        return {**base, "error": "RV30 unavailable (insufficient/invalid price data)"}

    iv30_val   = ts(30)
    if iv30_val is None or not math.isfinite(iv30_val):
        return {**base, "error": "IV30 unavailable from term structure"}

    iv30_rv30  = iv30_val / rv30
    if not math.isfinite(iv30_rv30):
        return {**base, "error": "IV30/RV30 ratio invalid"}

    pass_vol   = bool(avg_vol   >= 1_500_000)
    pass_ivr   = bool(iv30_rv30 >= 1.25)
    pass_slope = bool(math.isfinite(slope) and slope <= -0.00406)

    if pass_vol and pass_ivr and pass_slope:
        verdict = "Recommended"
    elif pass_slope and ((pass_vol and not pass_ivr) or (pass_ivr and not pass_vol)):
        verdict = "Consider"
    else:
        verdict = "Avoid"

    return {
        **base,
        "verdict":       verdict,
        "avg_volume":    pass_vol,
        "iv30_rv30":     pass_ivr,
        "ts_slope":      pass_slope,
        "iv30_rv30_val": round(float(iv30_rv30), 3),
        "avg_vol_m":     round(float(avg_vol) / 1e6, 2),
        "ts_slope_val":  round(float(slope), 6),
        "expected_move": f"{round(straddle / spot * 100, 2)}%" if straddle else "—",
        # Underlying/strike/IV context for the payoff chart. `spot` is a numpy
        # scalar here, so cast to float or json.dumps will refuse it.
        "spot":          round(float(spot), 2),
        "strike":        round(atm_strike, 2)  if atm_strike  else None,
        "atm_iv_used":   round(atm_iv_used, 4) if atm_iv_used else None,
        "call_bid":      round(call_bid, 3) if call_bid else None,
        "call_ask":      round(call_ask, 3) if call_ask else None,
        "call_mid":      round(call_mid, 3) if call_mid else None,
        "put_bid":       round(put_bid,  3) if put_bid  else None,
        "put_ask":       round(put_ask,  3) if put_ask  else None,
        "put_mid":       round(put_mid,  3) if put_mid  else None,
        "straddle_mid":  round(straddle, 3) if straddle else None,
        "leg_spread_max": round(max(
            (call_ask - call_bid) / call_mid if call_bid and call_ask and call_mid else 0,
            (put_ask  - put_bid)  / put_mid  if put_bid  and put_ask  and put_mid  else 0,
        ), 4),
    }

# ─────────────────────────────────────────────────────────────────────────────
# MAIN RUNNER
# ─────────────────────────────────────────────────────────────────────────────

def run(
    entries: Optional[list[dict]] = None,
    days_ahead: int = 7,
    on_progress: Optional[Callable[[dict], None]] = None,
) -> list[dict]:
    """
    Run the earnings options scan.
    entries: list of {ticker, company, earnings_date, earnings_time}
             If None, fetches calendar automatically.
    on_progress(result_dict) called after each ticker.
    Returns full results list.
    """
    if entries is None:
        entries = fetch_earnings_calendar(days_ahead=days_ahead)

    if not entries:
        return []

    total   = len(entries)
    results = []

    for i, entry in enumerate(entries, 1):
        ticker = entry["ticker"]
        try:
            result = analyse(ticker, meta=entry)
        except Exception as e:
            result = {
                "ticker":        ticker,
                "company":       entry.get("company", ""),
                "earnings_date": entry.get("earnings_date", "—"),
                "earnings_time": entry.get("earnings_time", "—"),
                "error":         str(e),
                "timestamp":     datetime.now().isoformat(timespec="seconds"),
            }

        result["_progress"] = {"current": i, "total": total}
        results.append(result)

        if on_progress:
            on_progress(result)

        # Cooldown between tickers — every 5 tickers take a longer break
        if i % 5 == 0 and i < total:
            time.sleep(random.uniform(15, 22))
        elif i < total:
            _sleep(5.0, jitter=2.0)

    return results
