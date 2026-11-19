"""
parity.py — put-call-parity twins for multi-leg option signals.

Why this exists
---------------
A bull call vertical (long K1 call / short K2 call) and a bull put vertical
(long K1 put / short K2 put) have identical payoff at expiry up to the cash
amount (K2 - K1) * e^(-rT). Likewise a call calendar and a put calendar at the
same strike differ only by K * (e^(-rT1) - e^(-rT2)) minus dividends between
the two expiries. The *mid* price of the two versions is therefore (almost)
the same, so the only thing that differs at execution is how far from mid you
have to pay to get filled: the sum of the legs' half-spreads.

Everything here is computed from the SAME `option_chain(...)` object the
strategy already fetched (calls and puts come back together), so both versions
are quoted at the same instant. Nothing in this module does network I/O.

Output contract (attached to a signal under key "parity"):

    {
      "available":   bool,            # False if the twin could not be quoted
      "reason":      str | None,      # why unavailable
      "original":    {"type": "CALL"|"PUT", "legs": [...], "slippage": float,
                      "slippage_pct": float, "net_mid": float},
      "twin":        {same shape},
      "savings":     float,           # original.slippage - twin.slippage ($/share)
      "savings_pct": float,           # savings / original.slippage
      "twin_better": bool,            # savings > PARITY_MIN_SAVINGS
      "warnings":    [str, ...],      # assignment / stale-quote flags
      "parity_gap":  float | None,    # |(C-P) - (S-K)| at anchor strike, $/share
      "parity_stale": bool            # gap wider than combined bid/ask
    }

Each leg:
    {"side": "LONG"|"SHORT", "type": "CALL"|"PUT", "strike": K,
     "expiry": "YYYY-MM-DD", "bid": b, "ask": a, "mid": m,
     "half_spread": (a-b)/2, "oi": int|None, "volume": int|None,
     "itm": bool}
"""

from __future__ import annotations

from typing import Optional

# A twin must beat the original by at least this many $/share of slippage
# (i.e. $1 per contract) before the UI advertises it. Avoids flashing
# "parity version is better" over a $0.005 difference.
PARITY_MIN_SAVINGS = 0.01


# ── quote helpers ────────────────────────────────────────────────────────────

def _f(v, default=None):
    try:
        if v is None:
            return default
        x = float(v)
        if x != x:  # NaN
            return default
        return x
    except Exception:
        return default


def _i(v) -> Optional[int]:
    x = _f(v)
    return int(x) if x is not None else None


def quote_at(chain_df, strike: float) -> Optional[dict]:
    """Two-sided quote at exactly `strike` from a calls or puts DataFrame."""
    if chain_df is None or strike is None:
        return None
    try:
        if chain_df.empty:
            return None
        rows = chain_df[(chain_df["strike"] - strike).abs() < 1e-6]
    except Exception:
        return None
    if rows.empty:
        return None
    row = rows.iloc[0]
    bid = _f(row.get("bid"), 0.0) or 0.0
    ask = _f(row.get("ask"), 0.0) or 0.0
    if bid <= 0 or ask < bid:
        return None
    return {
        "bid":    round(bid, 3),
        "ask":    round(ask, 3),
        "mid":    round((bid + ask) / 2.0, 3),
        "oi":     _i(row.get("openInterest")),
        "volume": _i(row.get("volume")),
    }


def _leg(side: str, otype: str, strike: float, expiry: str,
         q: Optional[dict], spot: float) -> Optional[dict]:
    if q is None:
        return None
    itm = (spot > strike) if otype == "CALL" else (spot < strike)
    return {
        "side":        side,
        "type":        otype,
        "strike":      strike,
        "expiry":      expiry,
        "bid":         q["bid"],
        "ask":         q["ask"],
        "mid":         q["mid"],
        "half_spread": round((q["ask"] - q["bid"]) / 2.0, 4),
        "oi":          q.get("oi"),
        "volume":      q.get("volume"),
        "itm":         bool(itm),
    }


def _structure(otype: str, legs: list[dict]) -> dict:
    slip = sum(l["half_spread"] for l in legs)
    # Signed net mid: positive = debit paid, negative = credit received.
    net_mid = sum(l["mid"] if l["side"] == "LONG" else -l["mid"] for l in legs)
    denom = abs(net_mid) if abs(net_mid) > 1e-9 else None
    return {
        "type":         otype,
        "legs":         legs,
        "slippage":     round(slip, 4),
        "slippage_pct": round(slip / denom * 100.0, 2) if denom else None,
        "net_mid":      round(net_mid, 3),
        "min_oi":       min((l["oi"] for l in legs if l["oi"] is not None), default=None),
    }


def _compare(original: dict, twin: dict, warnings: list[str],
             parity_gap: Optional[float], parity_stale: bool) -> dict:
    savings = original["slippage"] - twin["slippage"]
    return {
        "available":    True,
        "reason":       None,
        "original":     original,
        "twin":         twin,
        "savings":      round(savings, 4),
        "savings_pct":  round(savings / original["slippage"] * 100.0, 1)
                        if original["slippage"] > 1e-9 else None,
        "twin_better":  savings > PARITY_MIN_SAVINGS,
        "warnings":     warnings,
        "parity_gap":   parity_gap,
        "parity_stale": parity_stale,
    }


def _unavailable(reason: str, original: Optional[dict] = None) -> dict:
    return {"available": False, "reason": reason, "original": original,
            "twin": None, "savings": None, "savings_pct": None,
            "twin_better": False, "warnings": [], "parity_gap": None,
            "parity_stale": False}


def parity_gap(calls_df, puts_df, strike: float, spot: float) -> tuple[Optional[float], bool]:
    """
    Stale-quote detector. At one strike/expiry, C_mid - P_mid must sit close
    to S - K (ignoring the small rate/dividend carry). If the gap exceeds the
    combined bid-ask width of the two quotes, one of them is not a live price.
    """
    c = quote_at(calls_df, strike)
    p = quote_at(puts_df, strike)
    if c is None or p is None:
        return None, False
    gap = abs((c["mid"] - p["mid"]) - (spot - strike))
    width = (c["ask"] - c["bid"]) + (p["ask"] - p["bid"])
    # Carry allowance: ~1% of strike covers r*T for T<1y plus a typical
    # quarterly dividend; anything beyond width + carry is suspect.
    return round(gap, 3), gap > width + 0.01 * strike


# ── verticals ────────────────────────────────────────────────────────────────

def vertical_twin(chain, direction: str, long_strike: float, short_strike: float,
                  expiry: str, spot: float) -> dict:
    """
    chain: the object returned by ticker.option_chain(expiry) (.calls/.puts).
    direction: "CALL" (original = call vertical) or "PUT" (original = put vertical).
    Twin: same two strikes, same long/short sides, opposite option type.
    """
    calls = getattr(chain, "calls", None)
    puts  = getattr(chain, "puts", None)
    o_df, t_df = (calls, puts) if direction == "CALL" else (puts, calls)
    o_type, t_type = (direction, "PUT" if direction == "CALL" else "CALL")

    o_legs = [_leg("LONG",  o_type, long_strike,  expiry, quote_at(o_df, long_strike),  spot),
              _leg("SHORT", o_type, short_strike, expiry, quote_at(o_df, short_strike), spot)]
    if any(l is None for l in o_legs):
        return _unavailable("original legs not two-sided quoted")
    original = _structure(o_type, o_legs)

    t_legs = [_leg("LONG",  t_type, long_strike,  expiry, quote_at(t_df, long_strike),  spot),
              _leg("SHORT", t_type, short_strike, expiry, quote_at(t_df, short_strike), spot)]
    if any(l is None for l in t_legs):
        return _unavailable(f"{t_type.lower()} legs at these strikes have no two-sided quote",
                            original)
    twin = _structure(t_type, t_legs)

    warnings: list[str] = []
    for l in twin["legs"]:
        if l["side"] == "SHORT" and l["itm"]:
            warnings.append(f"Twin short {l['type'].lower()} {l['strike']} is ITM: early-assignment exposure")
    if twin["min_oi"] is not None and twin["min_oi"] < 50:
        warnings.append(f"Twin min open interest is {twin['min_oi']}: quote may not hold for size")

    gap, stale = parity_gap(calls, puts, long_strike, spot)
    if stale:
        warnings.append(f"Put-call parity gap ${gap} at {long_strike} exceeds bid/ask width: one quote may be stale")
    return _compare(original, twin, warnings, gap, stale)


# ── calendars ────────────────────────────────────────────────────────────────

def calendar_twin(chain_short, chain_long, strike: float, exp_short: str,
                  exp_long: str, spot: float, original_type: str = "CALL",
                  ex_div_date: Optional[str] = None) -> dict:
    """
    chain_short / chain_long: option_chain(...) objects for the two expiries.
    Original = `original_type` calendar (short front, long back, one strike).
    Twin = same structure in the other option type.
    """
    def pick(chain, otype):
        return getattr(chain, "calls" if otype == "CALL" else "puts", None)

    t_type = "PUT" if original_type == "CALL" else "CALL"

    o_legs = [_leg("SHORT", original_type, strike, exp_short, quote_at(pick(chain_short, original_type), strike), spot),
              _leg("LONG",  original_type, strike, exp_long,  quote_at(pick(chain_long,  original_type), strike), spot)]
    if any(l is None for l in o_legs):
        return _unavailable("original legs not two-sided quoted")
    original = _structure(original_type, o_legs)

    t_legs = [_leg("SHORT", t_type, strike, exp_short, quote_at(pick(chain_short, t_type), strike), spot),
              _leg("LONG",  t_type, strike, exp_long,  quote_at(pick(chain_long,  t_type), strike), spot)]
    if any(l is None for l in t_legs):
        return _unavailable(f"{t_type.lower()} legs at {strike} have no two-sided quote", original)
    twin = _structure(t_type, t_legs)

    warnings: list[str] = []
    short_twin = t_legs[0]
    if short_twin["itm"]:
        msg = f"Twin short {t_type.lower()} {strike} is ITM: early-assignment exposure"
        if t_type == "CALL" and ex_div_date and ex_div_date < exp_short:
            msg += f" (ex-dividend {ex_div_date} before front expiry)"
        warnings.append(msg)
    elif t_type == "CALL" and ex_div_date and ex_div_date < exp_short:
        warnings.append(f"Ex-dividend {ex_div_date} falls before front expiry {exp_short}: call version carries assignment risk if it goes ITM")
    if twin["min_oi"] is not None and twin["min_oi"] < 50:
        warnings.append(f"Twin min open interest is {twin['min_oi']}: quote may not hold for size")

    gap, stale = parity_gap(getattr(chain_short, "calls", None),
                            getattr(chain_short, "puts", None), strike, spot)
    if stale:
        warnings.append(f"Put-call parity gap ${gap} on front expiry exceeds bid/ask width: one quote may be stale")
    return _compare(original, twin, warnings, gap, stale)
