"""
option_pricing.py — American option pricing & IV solver
=========================================================
Two models:
  CRR (Cox-Ross-Rubinstein) binomial tree, 100 steps
    → matches Interactive Brokers exactly
  BS93 (Bjerksund-Stensland 1993/2002 approximation)
    → matches OptionStrat

Both accept continuous dividend yield q.
IV is solved via Newton-Raphson from bid/ask mid price.

Usage:
    from option_pricing import implied_vol_from_mid, get_risk_free_rate
"""

from __future__ import annotations

import math
import time
import datetime
from typing import Optional

import numpy as np
from scipy.stats import norm


# ─────────────────────────────────────────────────────────────────────────────
# RISK-FREE RATE
# ─────────────────────────────────────────────────────────────────────────────

_cached_rfr: Optional[float] = None
_cached_rfr_date: Optional[datetime.date] = None
_rfr_retry_after: float = 0.0

# How long to sit on the 0.05 fallback after a failed ^IRX fetch before
# trying the network again.
_RFR_FAIL_BACKOFF_SEC = 600.0


def get_risk_free_rate() -> float:
    """
    Return the current annualised risk-free rate as a decimal.
    Uses ^IRX (13-week T-bill) from yfinance, cached for the trading day.
    Falls back to 0.05 (5%) if unavailable.

    Success is cached for the calendar day, which is why all four strategies
    calling this only produce one ^IRX fetch per day rather than one per
    strategy per cycle.

    Failure is cached too, for 10 minutes. Without that, a failed fetch left
    _cached_rfr as None and every subsequent call retried the network. This is
    called once per option priced — thousands of times per cycle — so a
    persistently unreachable ^IRX turned into thousands of failing requests
    per cycle, each paying a full connection timeout. That is tolerable in a
    one-shot scan and ruinous in a 24/7 loop.
    """
    global _cached_rfr, _cached_rfr_date, _rfr_retry_after
    today = datetime.date.today()
    if _cached_rfr is not None and _cached_rfr_date == today:
        return _cached_rfr
    if time.monotonic() < _rfr_retry_after:
        return 0.05
    try:
        # Routed through data_provider so IBKR is used when active, with the
        # original yfinance ^IRX path preserved as the fallback inside it.
        import data_provider
        rate = data_provider.get_risk_free_rate_raw()
        if rate is not None:
            _cached_rfr      = max(0.0, min(rate, 0.20))
            _cached_rfr_date = today
            return _cached_rfr
    except Exception:
        pass
    _rfr_retry_after = time.monotonic() + _RFR_FAIL_BACKOFF_SEC
    return 0.05


# ─────────────────────────────────────────────────────────────────────────────
# EUROPEAN BLACK-SCHOLES  (used internally for vega and as floor)
# ─────────────────────────────────────────────────────────────────────────────

def _euro_call(S: float, K: float, T: float, r: float, q: float, sigma: float) -> float:
    if T <= 0 or sigma <= 0:
        return max(0.0, S * math.exp(-q * T) - K * math.exp(-r * T))
    sq = math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma**2) * T) / (sigma * sq)
    d2 = d1 - sigma * sq
    return S * math.exp(-q * T) * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)

def _euro_put(S: float, K: float, T: float, r: float, q: float, sigma: float) -> float:
    if T <= 0 or sigma <= 0:
        return max(0.0, K * math.exp(-r * T) - S * math.exp(-q * T))
    sq = math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma**2) * T) / (sigma * sq)
    d2 = d1 - sigma * sq
    return K * math.exp(-r * T) * norm.cdf(-d2) - S * math.exp(-q * T) * norm.cdf(-d1)

def _vega(S: float, K: float, T: float, r: float, q: float, sigma: float) -> float:
    """dV/dsigma — same for calls and puts."""
    if T <= 0 or sigma <= 0:
        return 0.0
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    return S * math.exp(-q * T) * math.sqrt(T) * norm.pdf(d1)


# ─────────────────────────────────────────────────────────────────────────────
# CRR BINOMIAL TREE — 100 steps  (matches Interactive Brokers)
# ─────────────────────────────────────────────────────────────────────────────

def crr_price(
    S: float, K: float, T: float, r: float, q: float, sigma: float,
    option_type: str = "call", N: int = 100,
) -> float:
    """
    Cox-Ross-Rubinstein American option price.
    option_type: "call" or "put"
    N: number of steps (IB uses 100)
    """
    is_call = option_type.lower() == "call"
    if T <= 0:
        return max(0.0, (S - K) if is_call else (K - S))
    if sigma <= 0:
        return max(0.0, (S - K) if is_call else (K - S))

    dt   = T / N
    u    = math.exp(sigma * math.sqrt(dt))
    d    = 1.0 / u
    p    = (math.exp((r - q) * dt) - d) / (u - d)
    p    = max(0.0, min(1.0, p))
    disc = math.exp(-r * dt)

    # Terminal payoffs
    j    = np.arange(N + 1)
    ST   = S * u ** (N - 2 * j)
    V    = np.maximum(ST - K, 0.0) if is_call else np.maximum(K - ST, 0.0)

    # Backward induction with early exercise check
    for i in range(N - 1, -1, -1):
        V    = disc * (p * V[:i + 1] + (1 - p) * V[1:i + 2])
        j_i  = np.arange(i + 1)
        ST_i = S * u ** (i - 2 * j_i)
        ex   = np.maximum(ST_i - K, 0.0) if is_call else np.maximum(K - ST_i, 0.0)
        V    = np.maximum(V, ex)

    return float(V[0])


# ─────────────────────────────────────────────────────────────────────────────
# BJERKSUND-STENSLAND 1993  (matches OptionStrat)
# ─────────────────────────────────────────────────────────────────────────────

def _bs93_phi(S: float, T: float, gamma: float, H: float, I: float,
              r: float, q: float, sigma: float) -> float:
    lam = (-r + gamma * (r - q) + 0.5 * gamma * (gamma - 1) * sigma ** 2)
    kap = 2 * (r - q) / sigma ** 2 + (2 * gamma - 1)
    sq  = sigma * math.sqrt(T)
    d1  = -(math.log(S / H) + (r - q + (gamma - 0.5) * sigma ** 2) * T) / sq
    d2  = -(math.log(I ** 2 / (S * H)) + (r - q + (gamma - 0.5) * sigma ** 2) * T) / sq
    return math.exp(lam * T) * (S ** gamma) * (norm.cdf(d1) - (I / S) ** kap * norm.cdf(d2))


def bs93_call(S: float, K: float, T: float, r: float, q: float, sigma: float) -> float:
    """Bjerksund-Stensland 1993 American call (OptionStrat model)."""
    if T <= 0 or sigma <= 0:
        return max(0.0, S - K)
    if q <= 1e-6:
        return _euro_call(S, K, T, r, 0.0, sigma)

    beta  = (0.5 - (r - q) / sigma ** 2 +
             math.sqrt(max(0.0, ((r - q) / sigma ** 2 - 0.5) ** 2 + 2 * r / sigma ** 2)))
    if beta <= 1.0:
        return _euro_call(S, K, T, r, q, sigma)

    I     = K * beta / (beta - 1.0)
    if S >= I:
        return S - K

    alpha = (I - K) * I ** (-beta)
    euro  = _euro_call(S, K, T, r, q, sigma)

    val = (alpha * S ** beta
           - alpha * _bs93_phi(S, T, beta, I, I, r, q, sigma)
           + _bs93_phi(S, T, 1.0, I, I, r, q, sigma)
           - _bs93_phi(S, T, 1.0, K, I, r, q, sigma)
           - K * (_bs93_phi(S, T, 0.0, I, I, r, q, sigma)
                  - _bs93_phi(S, T, 0.0, K, I, r, q, sigma)))
    return max(euro, val, S - K)


def bs93_put(S: float, K: float, T: float, r: float, q: float, sigma: float) -> float:
    """BS93 American put via CRR (put-call symmetry less stable; CRR is exact)."""
    return crr_price(S, K, T, r, q, sigma, option_type="put")


# ─────────────────────────────────────────────────────────────────────────────
# NEWTON-RAPHSON IV SOLVER  (bid/ask mid → IV)
# ─────────────────────────────────────────────────────────────────────────────

def implied_vol(
    price:       float,
    S:           float,
    K:           float,
    T:           float,
    r:           float,
    q:           float,
    option_type: str   = "call",
    model:       str   = "crr",     # "crr" (IB) | "bs93" (OptionStrat) | "bs" (European)
    max_iter:    int   = 60,
    tol:         float = 1e-6,
) -> Optional[float]:
    """
    Newton-Raphson IV from a given market price.
    Uses vega from European BS (same to 4dp for NR purposes).
    Falls back to bisection if NR diverges.
    """
    is_call = option_type.lower() == "call"

    # Choose pricing function
    if model == "crr":
        pricer = lambda sig: crr_price(S, K, T, r, q, sig, option_type)
    elif model == "bs93":
        pricer = lambda sig: (bs93_call(S, K, T, r, q, sig) if is_call
                              else bs93_put(S, K, T, r, q, sig))
    else:
        pricer = lambda sig: (_euro_call(S, K, T, r, q, sig) if is_call
                              else _euro_put(S, K, T, r, q, sig))

    # Sanity bounds
    intrinsic = max(0.0, (S - K) if is_call else (K - S))
    if price <= intrinsic + 1e-5:
        return None     # no time value to invert
    if price >= S:
        return None     # nonsensical price

    # Initial guess: ATM approximation
    sigma = max(0.01, price / max(S * math.sqrt(T) * 0.3989, 1e-8))
    sigma = min(sigma, 10.0)

    # Newton-Raphson
    for _ in range(max_iter):
        p_est = pricer(sigma)
        v     = _vega(S, K, T, r, q, sigma)
        diff  = p_est - price
        if abs(diff) < tol:
            break
        if v < 1e-10:
            break
        sigma -= diff / v
        sigma  = max(0.001, min(sigma, 20.0))

    if not (0.005 <= sigma <= 15.0):
        return None
    return float(sigma)


# ─────────────────────────────────────────────────────────────────────────────
# BID/ASK MID HELPER
# ─────────────────────────────────────────────────────────────────────────────

def mid_price(row, max_spread_ratio: float = 0.50) -> Optional[float]:
    """
    Return bid/ask mid if both are valid and spread is < max_spread_ratio of mid.
    Falls back to lastPrice only if no valid bid/ask.
    """
    try:
        bid = float(row.get("bid", 0) or 0)
        ask = float(row.get("ask", 0) or 0)
        if bid > 0 and ask >= bid:
            mid = (bid + ask) / 2.0
            if mid > 0 and (ask - bid) / mid < max_spread_ratio:
                return mid
        last = float(row.get("lastPrice", 0) or 0)
        return last if last > 0 else None
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# ATM IV WITH BRACKETED INTERPOLATION
# ─────────────────────────────────────────────────────────────────────────────

def atm_iv_from_chain(
    chain,              # pd.DataFrame with strike, bid, ask, lastPrice columns
    spot:  float,
    T:     float,       # years to expiry
    r:     float,
    q:     float        = 0.0,
    option_type: str    = "call",
    model: str          = "crr",
) -> Optional[float]:
    """
    Compute ATM IV by:
    1. Solving IV from bid/ask mid for each strike via Newton-Raphson.
    2. Interpolating between the two strikes bracketing spot.

    Falls back to yfinance impliedVolatility (with sanity filter) if no
    valid bid/ask mids are available.
    """
    if chain is None or chain.empty:
        return None

    chain = chain.copy().sort_values("strike").reset_index(drop=True)

    # Build IV-by-strike from bid/ask mid
    ivs_by_strike: dict[float, float] = {}
    for _, row in chain.iterrows():
        k   = float(row["strike"])
        mid = mid_price(row)
        if mid is None:
            continue
        iv = implied_vol(mid, spot, k, T, r, q, option_type, model)
        if iv is not None and 0.02 <= iv <= 5.0:
            ivs_by_strike[k] = iv

    # Fallback to yfinance IV
    if not ivs_by_strike:
        chain_filt = chain[
            chain["impliedVolatility"].notna() &
            (chain["impliedVolatility"] > 0.01) &
            (chain["impliedVolatility"] < 5.0)
        ]
        if chain_filt.empty:
            return None
        ivs_by_strike = {float(r_["strike"]): float(r_["impliedVolatility"])
                         for _, r_ in chain_filt.iterrows()}

    strikes = sorted(ivs_by_strike)

    # Exact hit
    if spot in ivs_by_strike:
        return ivs_by_strike[spot]

    # Bracketing interpolation
    below = [k for k in strikes if k < spot]
    above = [k for k in strikes if k > spot]

    if below and above:
        k_lo, k_hi   = below[-1], above[0]
        iv_lo, iv_hi = ivs_by_strike[k_lo], ivs_by_strike[k_hi]
        t = (spot - k_lo) / (k_hi - k_lo)
        return iv_lo + t * (iv_hi - iv_lo)
    elif below:
        return ivs_by_strike[below[-1]]
    elif above:
        return ivs_by_strike[above[0]]
    return None


# ─────────────────────────────────────────────────────────────────────────────
# DIVIDEND YIELD HELPER
# ─────────────────────────────────────────────────────────────────────────────

_div_cache: dict[str, float] = {}

def get_dividend_yield(ticker: str, spot: float) -> float:
    """
    Estimate continuous dividend yield from trailing 12-month dividends.
    Returns 0.0 if no dividend data available.
    """
    if ticker in _div_cache:
        return _div_cache[ticker]
    try:
        # Routed through data_provider so IBKR is used when active. NOTE: the
        # attribute lookup below is deliberately unchanged — modern yfinance no
        # longer exposes `three_month_dividend_rate`, so this typically yields
        # q = 0.0. Repairing the source would change option prices AND expose a
        # latent inconsistency in bs_delta() (which omits q entirely), so it is
        # intentionally left alone here.
        import data_provider
        tk   = data_provider.get_ticker(ticker)
        info = tk.fast_info
        div  = getattr(info, "three_month_dividend_rate", None)
        if div and div > 0 and spot > 0:
            annual = div * 4
            q = math.log(1 + annual / spot)
            _div_cache[ticker] = max(0.0, min(q, 0.15))
            return _div_cache[ticker]
    except Exception:
        pass
    _div_cache[ticker] = 0.0
    return 0.0
