"""
strategy_put_selling.py — Goldman Sachs "Art of Put Selling" screen.

SOURCE
──────
Goldman Sachs Equity Derivatives Research, "The Art of Put Selling" (Apr 2013).
The paper tests put-writing overlays on single stocks 2003-2012 and reports two
separate, independently useful findings:

  1. STOCK SELECTION.  Restrict the universe to the top quintile by free-cash-flow
     yield and sell 50-delta (roughly at-the-money) 1-month puts.  Reported
     9.8% annualised with ~12% vol.

  2. STRIKE SELECTION.  Take all names with positive FCF yield and, for each,
     pick the strike whose premium equals one twelfth of the stock's ANNUAL FCF
     yield.  Reported 5.1% annualised on ~4% vol — the highest Sharpe in the
     study, despite the lower absolute return.

The logic behind (2) is the part worth restating, because it is what makes the
rule more than numerology.  Selling a one-month put is a one-month commitment,
so the premium you collect should be compared against one month of the company's
cash generation — FCFY/12.  A strike whose premium clears that bar is one where
the option market is paying you at least as much, per unit time, as owning the
business would.  It is a relative-value test between the vol surface and the
cash flows, not a return target.

Both are implemented here as selectable modes, plus a plain fixed-delta mode for
users who want the mechanic without the fundamental overlay.

SCOPE — READ THIS
─────────────────
This is a SCREEN. It ranks and sizes candidates from a current snapshot. It is
not a backtest and it does not reproduce the paper's returns; those came from a
2003-2012 sample on a different universe with monthly rebalancing and
assignment handling that a snapshot cannot represent. Every yield figure emitted
below is a forward-looking arithmetic projection of TODAY's premium, clearly
named as such (`ann_yield_if_repeated`), and it assumes the trade can be
re-struck twelve times a year on identical terms. It cannot. Treat it as a
cross-sectional comparison metric, not an expected return.

Assignment is the other thing a snapshot cannot model. A put seller's actual
P&L depends entirely on what happens when short strikes go in-the-money, and the
paper's returns embed the assumption that assigned stock is taken and the
position re-struck. The `assign_prob` column here is the risk-neutral
probability of finishing ITM (from the pricing model, not a real-world forecast)
and is provided so the user can see which candidates carry that risk, not
because the screen handles it.
"""

from __future__ import annotations

import logging
import math
import os
import time
from datetime import date, datetime
from typing import Callable, Optional

import numpy as np
import pandas as pd

import data_provider
import fundamentals
import option_pricing as op
from strategy_calendar import get_sp500_tickers

log = logging.getLogger("put_selling")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


# ── Tunables ──────────────────────────────────────────────────────────────────

# Expiry window. The paper uses 1-month options; monthly listings rarely land
# exactly 30 days out, so accept a window and prefer whatever is closest to 30.
DTE_MIN            = _env_int("PS_DTE_MIN", 21)
DTE_MAX            = _env_int("PS_DTE_MAX", 45)
DTE_TARGET         = _env_int("PS_DTE_TARGET", 30)

# Liquidity floors. A screen that surfaces an untradeable strike is worse than
# one that surfaces nothing, because it costs the user the time to discover that.
MIN_OPEN_INTEREST  = _env_int("PS_MIN_OI", 50)
MIN_OPTION_VOLUME  = _env_int("PS_MIN_OPT_VOL", 0)
MAX_SPREAD_RATIO   = _env_float("PS_MAX_SPREAD", 0.25)
MIN_PREMIUM_ABS    = _env_float("PS_MIN_PREMIUM", 0.10)
MIN_STOCK_VOLUME   = _env_int("PS_MIN_STOCK_VOL", 500_000)
MIN_PRICE          = _env_float("PS_MIN_PRICE", 10.0)

# Economic-substance floors. These are NOT liquidity filters and they exist for
# a specific reason discovered in live testing.
#
# The FCF premium rule sets a bar of FCFY/12. For a low-FCFY name that bar is
# tiny -- MSFT at 1.81% FCFY needs only 0.146% of strike -- and *every* strike on
# the board clears it, including $415 puts on a $498 stock quoting $0.77 with
# 1.4% implied vol and a delta of -0.0004. The selector was therefore free to
# walk to the deepest, cheapest strike, and it did.
#
# Those strikes are not a put-selling strategy. There is no meaningful optionality
# being sold, the "IV" is a rounding artefact of a penny-wide quote on a stale
# book, and the annualised-yield figure computed from it is noise that would sort
# straight to the top of a yield-ranked table. Requiring a minimum |delta| means
# the name must be offering real premium for real assignment risk, or it does not
# appear at all. Capping moneyness bounds the same failure from the strike side.
MIN_ABS_DELTA      = _env_float("PS_MIN_ABS_DELTA", 0.05)
MAX_MONEYNESS      = _env_float("PS_MAX_MONEYNESS", 0.30)

# Strategy parameters
TARGET_DELTA       = _env_float("PS_TARGET_DELTA", 0.50)   # mode="delta"
TARGET_MONEYNESS   = _env_float("PS_TARGET_MONEYNESS", 0.10)
TOP_QUINTILE_ONLY  = os.environ.get("PS_TOP_QUINTILE", "1") not in ("0", "false", "False")
CONTRACT_SIZE      = 100

FETCH_DELAY        = _env_float("PS_FETCH_DELAY", 0.15)     # phase 2: ~1 request/ticker
FCF_FETCH_DELAY    = _env_float("PS_FCF_FETCH_DELAY", 0.5)  # phase 1: up to 3 requests/ticker
PRICING_MODEL      = os.environ.get("PS_MODEL", "crr")

# Hard cap on how many names enter PHASE 1, applied BEFORE any fundamentals are
# fetched. This is distinct from `max_names`, which trims the survivor list after
# phase 1 has already paid the full universe cost and therefore does nothing to
# shorten the wait before the first row appears.
#
# Why this exists: phase 1 is a sequential, uncached-by-default yfinance sweep
# needing ~2-3 network round-trips per name (.info, quarterly_cashflow, and an
# annual fallback). On a cold cache across ~500 S&P names that is tens of minutes
# during which the table is necessarily empty -- phase 2 emits the first row only
# after phase 1 finishes for EVERY name. The UI is working correctly in that
# window; it simply has nothing to draw, which is indistinguishable from a hang.
#
# 0 = no cap (full index, the paper's intent). Set a value for interactive runs.
# NOTE: the universe arrives in index order (alphabetical), so a cap takes an
# alphabetical slice, not the "best" names. Quintile ranking is then computed
# within that slice -- top-quintile of 120 names is not top-quintile of the S&P
# 500. Use for smoke tests and impatient interactive scans, not for research.
UNIVERSE_MAX       = _env_int("PS_UNIVERSE_MAX", 0)

MODES = ("fcf_premium", "delta", "moneyness")


# ══════════════════════════════════════════════════════════════════════════════
# GREEKS
# ══════════════════════════════════════════════════════════════════════════════

def put_greeks(S: float, K: float, T: float, r: float, q: float,
               sigma: float) -> dict:
    """
    Put delta/gamma/vega/theta plus risk-neutral P(finish ITM).

    Analytic Black-Scholes rather than finite-differencing the CRR tree. The
    American early-exercise premium on a 30-day equity put is small, and the
    closed form avoids both the tree's discretisation noise and ~100x the
    compute — which matters when this runs across 500 names x every strike.
    Prices still come from the tree via option_pricing; only the greeks are
    analytic.

    delta is returned NEGATIVE (long-put convention). A short put therefore has
    positive delta exposure. Callers comparing against a target delta should use
    abs(). Getting this backwards silently inverts every risk weight, so the sign
    convention is asserted in the test suite.
    """
    out = {"delta": None, "gamma": None, "vega": None, "theta": None,
           "prob_itm": None}
    try:
        if not all(x is not None for x in (S, K, T, r, q, sigma)):
            return out
        if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
            return out

        sqrtT = math.sqrt(T)
        vs    = sigma * sqrtT
        d1    = (math.log(S / K) + (r - q + 0.5 * sigma ** 2) * T) / vs
        d2    = d1 - vs

        # Standard normal pdf/cdf without scipy (not a guaranteed dependency).
        pdf   = math.exp(-0.5 * d1 * d1) / math.sqrt(2.0 * math.pi)
        cdf   = lambda x: 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

        disc_q = math.exp(-q * T)
        disc_r = math.exp(-r * T)

        out["delta"] = -disc_q * cdf(-d1)                     # negative
        out["gamma"] = disc_q * pdf / (S * vs)
        out["vega"]  = S * disc_q * pdf * sqrtT / 100.0        # per 1 vol point
        out["theta"] = (
            (-S * disc_q * pdf * sigma / (2.0 * sqrtT)
             + q * S * disc_q * cdf(-d1)
             - r * K * disc_r * cdf(-d2)) / 365.0             # per calendar day
        )
        # Risk-neutral P(S_T < K). Not a real-world probability: it is computed
        # under the pricing measure and embeds the risk premium, so it overstates
        # true assignment odds for names with positive expected drift.
        out["prob_itm"] = cdf(-d2)
    except Exception:
        return {"delta": None, "gamma": None, "vega": None, "theta": None,
                "prob_itm": None}
    return out


# ══════════════════════════════════════════════════════════════════════════════
# CHAIN PREPARATION
# ══════════════════════════════════════════════════════════════════════════════

def _pick_expiry(expiry_strings: list[str], today: date) -> Optional[tuple]:
    """Choose the listed expiry closest to DTE_TARGET within [DTE_MIN, DTE_MAX]."""
    best = None
    for es in expiry_strings or []:
        try:
            exp = datetime.strptime(es, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue
        dte = (exp - today).days
        if DTE_MIN <= dte <= DTE_MAX:
            score = abs(dte - DTE_TARGET)
            if best is None or score < best[0]:
                best = (score, es, exp, dte)
    if best is None:
        return None
    return best[1], best[2], best[3]


def _prepare_puts(puts: pd.DataFrame, spot: float, T: float, r: float,
                  q: float) -> list[dict]:
    """
    Filter a put chain to tradeable rows and attach mid price, IV and greeks.

    Every row is priced off the bid/ask mid, never lastPrice. A put that last
    traded three days ago at a stale quote will otherwise dominate any
    premium-based ranking — the screen would systematically select the least
    liquid strikes, which is precisely backwards.
    """
    if puts is None or len(puts) == 0:
        return []

    rows = []
    for _, raw in puts.iterrows():
        try:
            K = float(raw.get("strike", 0) or 0)
            if K <= 0 or K > spot * 1.5:      # far ITM puts aren't the trade
                continue

            oi  = int(raw.get("openInterest", 0) or 0)
            vol = int(raw.get("volume", 0) or 0)
            if oi < MIN_OPEN_INTEREST or vol < MIN_OPTION_VOLUME:
                continue

            bid = float(raw.get("bid", 0) or 0)
            ask = float(raw.get("ask", 0) or 0)
            if bid <= 0 or ask < bid:
                continue

            mid = (bid + ask) / 2.0
            if mid < MIN_PREMIUM_ABS:
                continue

            spread_ratio = (ask - bid) / mid if mid > 0 else 9.99
            if spread_ratio > MAX_SPREAD_RATIO:
                continue

            iv = op.implied_vol(mid, spot, K, T, r, q,
                                option_type="put", model=PRICING_MODEL)
            if iv is None or not (0.01 < iv < 5.0):
                continue

            g = put_greeks(spot, K, T, r, q, iv)
            if g["delta"] is None:
                continue

            # Economic substance. See MIN_ABS_DELTA note above: without this the
            # FCF rule happily selects a -0.0004 delta strike whose premium is a
            # quoting artefact, and that candidate then tops a yield-sorted table.
            if abs(g["delta"]) < MIN_ABS_DELTA:
                continue

            moneyness = (spot - K) / spot
            if moneyness > MAX_MONEYNESS:
                continue

            rows.append({
                "strike":       K,
                "bid":          bid,
                "ask":          ask,
                "premium":      mid,
                "spread_ratio": spread_ratio,
                "open_interest": oi,
                "opt_volume":   vol,
                "iv":           iv,
                "delta":        g["delta"],
                "abs_delta":    abs(g["delta"]),
                "gamma":        g["gamma"],
                "vega":         g["vega"],
                "theta":        g["theta"],
                "assign_prob":  g["prob_itm"],
                "moneyness":    (spot - K) / spot,     # >0 = OTM put
                "prem_pct":     mid / K,               # premium / collateral
            })
        except Exception:
            continue

    rows.sort(key=lambda x: x["strike"])
    return rows


# ══════════════════════════════════════════════════════════════════════════════
# STRIKE SELECTION
# ══════════════════════════════════════════════════════════════════════════════

def select_strike_fcf(rows: list[dict], fcf_yield: float,
                      dte: int) -> Optional[dict]:
    """
    The paper's strike rule: premium should equal 1/12th of the ANNUAL FCF yield.

    Two decisions here that the paper leaves implicit and that materially change
    the output:

    1. PREMIUM AS A FRACTION OF WHAT?  We use premium/strike, not premium/spot.
       The strike is the capital actually committed — a cash-secured put on a $90
       strike ties up $9,000 regardless of where spot is. Using spot would
       overstate yield on OTM puts by exactly the moneyness, biasing selection
       toward deeper OTM strikes for the wrong reason. This matters most for the
       high-FCFY value names the screen is designed to surface, which is where
       the bias would do the most damage.

    2. FIXED /12 OR SCALED BY DTE?  The rule says one twelfth, which presumes a
       one-month option. Our expiry window is 21-45 days, so a literal /12 would
       compare a 45-day premium against a 30-day cash-flow bar and flatter long
       expiries. We scale the target by dte/30 so the comparison is per-unit-time
       consistent. `target_prem_pct_raw` retains the unscaled figure for anyone
       reconciling against the paper.

    Selection is the strike closest to the target from ABOVE where possible: the
    rule is a minimum acceptable compensation, so overshooting is acceptable and
    undershooting is not. If no strike clears the bar we return the closest one
    and flag `target_met=False` rather than dropping the name, because "the whole
    surface is too cheap for this company's cash flows" is itself a finding worth
    displaying.
    """
    if not rows or fcf_yield is None or fcf_yield <= 0:
        return None

    scale          = max(dte, 1) / 30.0
    target_raw     = fcf_yield / 12.0
    target_pct     = target_raw * scale

    clearing = [r for r in rows if r["prem_pct"] >= target_pct]
    if clearing:
        # Cheapest strike that still clears -> highest strike -> least OTM.
        # Among those, prefer the one closest to target (least over-payment of
        # assignment risk for premium we did not need).
        pick = min(clearing, key=lambda r: r["prem_pct"] - target_pct)
        met  = True
    else:
        pick = max(rows, key=lambda r: r["prem_pct"])
        met  = False

    out = dict(pick)
    out["target_prem_pct"]     = target_pct
    out["target_prem_pct_raw"] = target_raw
    out["target_met"]          = met
    out["target_gap"]          = pick["prem_pct"] - target_pct
    return out


def select_strike_delta(rows: list[dict], target: float = None) -> Optional[dict]:
    """Closest strike to a target absolute delta (paper's 50-delta variant)."""
    if not rows:
        return None
    tgt  = TARGET_DELTA if target is None else target
    pick = min(rows, key=lambda r: abs(r["abs_delta"] - tgt))
    out  = dict(pick)
    out["target_delta"] = tgt
    out["target_met"]   = abs(pick["abs_delta"] - tgt) <= 0.10
    return out


def select_strike_moneyness(rows: list[dict],
                            target: float = None) -> Optional[dict]:
    """Closest strike to a fixed OTM percentage. No fundamental input."""
    if not rows:
        return None
    tgt  = TARGET_MONEYNESS if target is None else target
    pick = min(rows, key=lambda r: abs(r["moneyness"] - tgt))
    out  = dict(pick)
    out["target_moneyness"] = tgt
    out["target_met"]       = abs(pick["moneyness"] - tgt) <= 0.03
    return out


# ══════════════════════════════════════════════════════════════════════════════
# PER-TICKER ANALYSIS
# ══════════════════════════════════════════════════════════════════════════════

def analyze_ticker(ticker: str, fcf: dict, mode: str = "fcf_premium",
                   avg_vol_30d: Optional[float] = None) -> Optional[dict]:
    """
    Evaluate one name. Returns a single candidate dict, or None if unsuitable.

    `fcf` is the pre-fetched fundamentals record; it is passed in rather than
    fetched here so the caller can rank the whole universe by FCFY before
    deciding which names are worth the (much more expensive) option-chain fetch.
    """
    try:
        tk = data_provider.get_ticker(ticker)

        # Spot: intraday last if available, else last daily close. Mirrors
        # strategy_calendar so the two screens never disagree on price.
        spot = None
        try:
            lp = getattr(tk.fast_info, "last_price", None)
            if lp and float(lp) > 0:
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
        if spot is None or spot <= 0 or spot < MIN_PRICE:
            return None

        picked = _pick_expiry(getattr(tk, "options", None) or [], date.today())
        if picked is None:
            return None
        exp_str, exp_date, dte = picked

        T = dte / 365.0
        r = op.get_risk_free_rate()
        q = op.get_dividend_yield(ticker, spot)

        try:
            chain = tk.option_chain(exp_str)
            puts  = getattr(chain, "puts", None)
        except Exception as e:
            log.debug(f"{ticker}: chain fetch failed: {e}")
            return None

        rows = _prepare_puts(puts, spot, T, r, q)
        if not rows:
            return None

        fcfy = fcf.get("fcf_yield") if fcf else None

        if mode == "fcf_premium":
            sel = select_strike_fcf(rows, fcfy, dte)
        elif mode == "delta":
            sel = select_strike_delta(rows)
        elif mode == "moneyness":
            sel = select_strike_moneyness(rows)
        else:
            raise ValueError(f"unknown mode: {mode}")

        if sel is None:
            return None

        premium    = sel["premium"]
        strike     = sel["strike"]
        collateral = strike * CONTRACT_SIZE          # cash-secured basis

        # Period return on committed capital, then a naive annualisation.
        period_ret = premium / strike
        ann_ret    = period_ret * (365.0 / max(dte, 1))

        result = {
            "ticker":        ticker,
            "mode":          mode,
            "spot":          round(spot, 2),
            "expiry":        exp_str,
            "dte":           dte,
            "strike":        strike,
            "bid":           round(sel["bid"], 2),
            "ask":           round(sel["ask"], 2),
            "premium":       round(premium, 3),
            "spread_ratio":  round(sel["spread_ratio"], 4),
            "open_interest": sel["open_interest"],
            "opt_volume":    sel["opt_volume"],

            "iv":            round(sel["iv"], 4),
            "delta":         round(sel["delta"], 4),
            "abs_delta":     round(sel["abs_delta"], 4),
            "gamma":         round(sel["gamma"], 6),
            "vega":          round(sel["vega"], 4),
            "theta":         round(sel["theta"], 4),
            "assign_prob":   round(sel["assign_prob"], 4) if sel["assign_prob"] is not None else None,
            "moneyness":     round(sel["moneyness"], 4),
            "otm_pct":       round(sel["moneyness"] * 100, 2),

            # Naming is deliberate: these are arithmetic projections of today's
            # premium, not expected returns. See module docstring.
            "prem_pct":              round(sel["prem_pct"], 5),
            "period_yield":          round(period_ret, 5),
            "ann_yield_if_repeated": round(ann_ret, 5),

            "collateral":    round(collateral, 2),
            "premium_cash":  round(premium * CONTRACT_SIZE, 2),

            "fcf_yield":     fcfy,
            "fcf_ttm":       fcf.get("fcf_ttm") if fcf else None,
            "fcfy_rank":     fcf.get("fcfy_rank") if fcf else None,
            "fcfy_quintile": fcf.get("fcfy_quintile") if fcf else None,
            "fcf_source":    fcf.get("source") if fcf else None,
            "fcf_asof":      fcf.get("asof") if fcf else None,
            "fcf_warnings":  list(fcf.get("warnings") or []) if fcf else [],

            "avg_vol_30d":   avg_vol_30d,
            "strikes_considered": len(rows),
        }

        # Mode-specific target diagnostics
        for k in ("target_prem_pct", "target_prem_pct_raw", "target_met",
                  "target_gap", "target_delta", "target_moneyness"):
            if k in sel:
                v = sel[k]
                result[k] = round(v, 5) if isinstance(v, float) else v

        return result

    except Exception as e:
        log.debug(f"{ticker}: analyze failed: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
# PORTFOLIO WEIGHTING
# ══════════════════════════════════════════════════════════════════════════════

def apply_weights(results: list[dict], scheme: str = "risk") -> list[dict]:
    """
    Attach portfolio weights in place and return the list.

    The paper prefers risk-based weighting over equal-weighting, and the reason is
    worth spelling out. Equal-weighting by CONTRACT count is not equal-weighting
    by risk: one contract of a 60-vol biotech at 50-delta carries several times
    the loss potential of one contract of a 15-vol utility at the same delta. A
    put-selling book equal-weighted by contracts is therefore dominated by its
    most volatile names, which is exactly the concentration that produces the
    left-tail losses put-selling is criticised for.

    Weight proportional to 1 / (IV x |delta|):

      - |delta| is the first-order probability-weighted exposure to being
        assigned. Halve delta, halve the expected assignment exposure.
      - IV scales the magnitude of the move you are exposed to when it happens.

    Their product is a first-order estimate of risk contribution per contract, so
    its reciprocal equalises risk across positions. This is a heuristic, not a
    covariance-based risk parity: it ignores correlation entirely, so a book of
    ten high-FCFY energy names will still be a concentrated sector bet no matter
    how the weights come out. Sector concentration is the user's job to check —
    the screen surfaces `fcfy_quintile` and lets them see it.

    Schemes:
      "risk"      1/(IV x |delta|)  — paper's preference, the default
      "equal"     1/N
      "premium"   proportional to premium collected (yield-chasing; included for
                  comparison because it makes the risk concentration obvious)
      "kelly_lite" risk-weight tilted by the premium/target ratio, only meaningful
                  in fcf_premium mode where a target exists

    Also emits `contracts_per_100k` — contracts to buy at a $100k notional book —
    because a weight fraction is not directly actionable but a contract count is.
    Rounded DOWN, since rounding up silently breaches the collateral budget.
    """
    if not results:
        return results

    raw: list[float] = []
    for r in results:
        iv  = r.get("iv") or 0.0
        dlt = abs(r.get("delta") or 0.0)
        if scheme == "equal":
            w = 1.0
        elif scheme == "premium":
            w = max(r.get("premium") or 0.0, 0.0)
        elif scheme == "kelly_lite":
            base  = 1.0 / (iv * dlt) if iv > 0 and dlt > 0 else 0.0
            tgt   = r.get("target_prem_pct") or 0.0
            ratio = (r.get("prem_pct") or 0.0) / tgt if tgt > 0 else 1.0
            # Cap the tilt: an unbounded ratio would let one mispriced quote
            # swamp the book.
            w = base * max(0.0, min(ratio, 3.0))
        else:  # "risk"
            w = 1.0 / (iv * dlt) if iv > 0 and dlt > 0 else 0.0
        raw.append(w)

    total = sum(raw)
    if total <= 0:
        # Degenerate (e.g. every IV or delta missing) — fall back to equal rather
        # than emitting zero or NaN weights that would silently size nothing.
        n = len(results)
        for r in results:
            w = 1.0 / n
            collat = r.get("collateral") or 0.0
            r["weight"]        = round(w, 6)
            r["weight_pct"]    = round(w * 100, 3)
            r["weight_scheme"] = scheme + "_fallback_equal"
            r["alloc_per_100k"] = round(100_000.0 * w, 2)
            r["contracts_per_100k"] = int((100_000.0 * w) // collat) if collat > 0 else 0
            r["premium_cash_at_weight"] = round(
                r["contracts_per_100k"] * (r.get("premium_cash") or 0.0), 2)
        return results

    BOOK = 100_000.0
    for r, w in zip(results, raw):
        weight = w / total
        r["weight"]        = round(weight, 6)
        r["weight_scheme"] = scheme
        r["weight_pct"]    = round(weight * 100, 3)

        collat = r.get("collateral") or 0.0
        r["contracts_per_100k"] = int((BOOK * weight) // collat) if collat > 0 else 0
        r["alloc_per_100k"]     = round(BOOK * weight, 2)
        # Premium actually collectable at that (integer) contract count.
        r["premium_cash_at_weight"] = round(
            r["contracts_per_100k"] * (r.get("premium_cash") or 0.0), 2)

    return results


def portfolio_summary(results: list[dict]) -> dict:
    """
    Aggregate book-level figures for the weighted candidate set.

    `book_ann_yield_if_repeated` is collateral-weighted, not a mean of the
    per-name yields. Averaging percentages across positions of different sizes
    would overstate the book yield whenever the high-yield names are the ones
    receiving small weights — which, under risk weighting, is systematically the
    case, since high yield comes with high IV and high IV gets down-weighted.
    """
    if not results:
        return {"count": 0}

    weighted     = [r for r in results if r.get("contracts_per_100k", 0) > 0]
    total_collat = sum((r.get("collateral") or 0.0) * r.get("contracts_per_100k", 0)
                       for r in weighted)
    total_prem   = sum(r.get("premium_cash_at_weight") or 0.0 for r in weighted)

    def _mean(key):
        vals = [r[key] for r in results if r.get(key) is not None]
        return round(float(np.mean(vals)), 4) if vals else None

    out = {
        "count":            len(results),
        "count_funded":     len(weighted),
        "total_collateral": round(total_collat, 2),
        "total_premium":    round(total_prem, 2),
        "avg_iv":           _mean("iv"),
        "avg_abs_delta":    _mean("abs_delta"),
        "avg_fcf_yield":    _mean("fcf_yield"),
        "avg_assign_prob":  _mean("assign_prob"),
        "avg_dte":          _mean("dte"),
    }
    if total_collat > 0:
        period = total_prem / total_collat
        dtes   = [r["dte"] for r in weighted if r.get("dte")]
        avg_dte = float(np.mean(dtes)) if dtes else 30.0
        out["book_period_yield"]          = round(period, 5)
        out["book_ann_yield_if_repeated"] = round(period * (365.0 / max(avg_dte, 1)), 5)
    return out


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def run(
    on_progress: Optional[Callable[[dict], None]] = None,
    tickers:     Optional[list[str]] = None,
    mode:        str   = "fcf_premium",
    weighting:   str   = "risk",
    top_quintile: Optional[bool] = None,
    max_names:   Optional[int]   = None,
) -> list[dict]:
    """
    Run the put-selling screen.

    Two-phase by design, and the ordering is the whole performance story:

      PHASE 1  Fetch fundamentals for the entire universe and rank by FCF yield.
               Cheap (cached, ~1 request/name, no chains).
      PHASE 2  Fetch option chains ONLY for names that survive the fundamental
               screen. Expensive (1 chain request + an IV solve per strike).

    Screening on fundamentals first is what makes an S&P 500 scan practical: in
    top-quintile mode phase 2 touches ~100 names instead of 500, cutting the
    dominant cost by ~80%.

    on_progress receives one event per ticker in phase 2, plus phase-1 events, in
    the same `_progress: {current, total}` shape the other three strategies use.
    """
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")

    use_quintile = TOP_QUINTILE_ONLY if top_quintile is None else top_quintile
    universe     = list(tickers) if tickers else get_sp500_tickers()
    if not universe:
        log.error("put_selling: empty universe")
        return []

    # Cap BEFORE phase 1. Applied only to the auto-fetched index; an explicit
    # `tickers` list is the caller's deliberate choice and is never truncated.
    if UNIVERSE_MAX > 0 and not tickers and len(universe) > UNIVERSE_MAX:
        log.warning(
            "put_selling: universe capped %d -> %d by PS_UNIVERSE_MAX. This is an "
            "alphabetical slice, so FCF quintiles are computed within the subset "
            "and are NOT S&P 500 quintiles.",
            len(universe), UNIVERSE_MAX,
        )
        universe = universe[:UNIVERSE_MAX]

    def emit(ev: dict) -> None:
        if on_progress:
            try:
                on_progress(ev)
            except Exception as e:
                log.debug(f"progress callback failed: {e}")

    # ── PHASE 1: fundamentals ────────────────────────────────────────────────
    total_u = len(universe)
    emit({"phase": "fundamentals", "message": f"Fetching FCF for {total_u} names",
          "_progress": {"current": 0, "total": total_u}})

    def fund_progress(ev: dict) -> None:
        emit({"phase": "fundamentals", "ticker": ev.get("ticker"),
              "fcf_yield": ev.get("fcf_yield"), "found": 0,
              "_progress": ev.get("_progress", {})})

    # get_fcf_yields defaults to sleep=0.0 -- fine for a handful of tickers,
    # but phase 1 can be the whole S&P 500 and each ticker can cost up to 3
    # separate requests (info, quarterly cashflow, annual cashflow fallback).
    # Unthrottled, that's a burst of up to ~1500 requests with zero pacing --
    # exactly what was triggering "Too Many Requests" from Yahoo in
    # production, which then bled into whatever ran next since the rate
    # limit doesn't clear instantly. FCF_FETCH_DELAY is more conservative
    # than phase 2's FETCH_DELAY below, since each ticker here costs more.
    fcf_all = fundamentals.get_fcf_yields(universe, on_progress=fund_progress,
                                          sleep=FCF_FETCH_DELAY)
    fundamentals.rank_by_fcf_yield(fcf_all, quantiles=5)

    # A cycle where phase 1 comes back mostly/all-failed produces the exact
    # same downstream symptom as a cycle where FCF genuinely disqualified
    # everyone: zero eligible names, no exception, nothing for
    # _strategy_failed to catch. Print a breakdown so that distinction is
    # visible in server logs instead of having to guess again.
    n_total  = len(fcf_all)
    n_sane   = sum(1 for r in fcf_all.values() if r.get("sane"))
    n_failed = n_total - n_sane
    if n_failed:
        reasons: dict[str, int] = {}
        for r in fcf_all.values():
            if not r.get("sane"):
                reason = r.get("reason") or "unknown"
                key = ("rate limited" if "Too Many Requests" in reason
                                       or "429" in reason
                       else reason[:60])
                reasons[key] = reasons.get(key, 0) + 1
        breakdown = ", ".join(f"{v}x {k!r}" for k, v in
                               sorted(reasons.items(), key=lambda kv: -kv[1]))
        print(f"[put_selling] phase 1: {n_sane}/{n_total} FCF fetches OK, "
              f"{n_failed} failed -- {breakdown}")

    # Screen. Both paper variants require positive FCF yield; a company burning
    # cash fails the premise of the strike rule (there is no cash flow to compare
    # premium against), so it is excluded rather than ranked last.
    n_fetched_ok = n_no_yield = n_negative_yield = n_quintile_cut = 0
    eligible = []
    for sym in universe:
        rec = fcf_all.get(sym.upper()) or {}
        y   = rec.get("fcf_yield")
        if y is None or not rec.get("sane"):
            continue
        n_fetched_ok += 1
        if mode == "fcf_premium" and y <= 0:
            n_negative_yield += 1
            continue
        if use_quintile and rec.get("fcfy_quintile") not in (1,):
            n_quintile_cut += 1
            continue
        eligible.append(sym)

    # Rank remaining by FCFY descending so the most attractive names are analysed
    # first — a user watching the stream sees the best candidates immediately, and
    # an aborted scan still yields the names that mattered.
    eligible.sort(key=lambda s: fcf_all.get(s.upper(), {}).get("fcf_yield") or -9,
                  reverse=True)
    n_before_cap = len(eligible)
    if max_names:
        eligible = eligible[:max_names]

    emit({"phase": "screened",
          "message": (f"{len(eligible)} of {total_u} passed FCF screen "
                      f"(quintile_only={use_quintile}, mode={mode})"),
          "eligible": len(eligible),
          "_progress": {"current": total_u, "total": total_u}})

    if not eligible:
        # Same reasoning as the phase-1 breakdown above: "0 eligible" looks
        # identical on screen whether the cause was rate limiting upstream,
        # every fetched name failing the yield/quintile cut, or the max_names
        # cap zeroing things out. print(), not log.warning() -- see note
        # above about uvicorn silently dropping pre-existing app loggers.
        print(f"[put_selling] phase 1: 0 eligible names -- of {total_u} "
              f"universe: {n_fetched_ok} fetched OK, {n_negative_yield} "
              f"non-positive yield, {n_quintile_cut} cut by quintile filter, "
              f"{n_before_cap} passed screen before max_names cap "
              f"(max_names={max_names or 'unlimited'})")
        return []

    # ── PHASE 2: option chains ───────────────────────────────────────────────
    results: list[dict] = []
    total_e = len(eligible)

    n_analyze_none = n_analyze_error = 0
    for i, sym in enumerate(eligible, 1):
        rec = fcf_all.get(sym.upper()) or {}
        try:
            cand = analyze_ticker(sym, rec, mode=mode)
        except Exception as e:
            log.debug(f"{sym}: unexpected analyze error: {e}")
            n_analyze_error += 1
            cand = None

        if cand:
            results.append(cand)
        else:
            n_analyze_none += 1

        emit({"phase": "options", "ticker": sym, "found": 1 if cand else 0,
              "candidate": cand, "fcf_yield": rec.get("fcf_yield"),
              "_progress": {"current": i, "total": total_e}})

        time.sleep(FETCH_DELAY)

    apply_weights(results, scheme=weighting)

    # Present best-first on premium efficiency. Under fcf_premium mode, names
    # that met the target sort above those that did not, since a candidate that
    # missed the bar is a different kind of result and should not outrank one
    # that cleared it on raw yield alone.
    results.sort(key=lambda r: (bool(r.get("target_met")),
                                r.get("ann_yield_if_repeated") or 0),
                 reverse=True)

    print(f"[put_selling] phase 2: {len(results)} candidates from {total_e} "
          f"screened ({mode}, {weighting}-weighted); {n_analyze_none} had no "
          f"viable strike/chain, {n_analyze_error} raised an exception "
          f"(see debug log for exceptions if any)")
    return results