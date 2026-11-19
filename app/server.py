"""
Options Scanner Suite — Backend Server
=======================================
FastAPI server that:
  - Serves the dashboard HTML
  - Runs all 4 strategies on demand (POST /run)
  - Streams live progress via Server-Sent Events (GET /stream)
  - Returns latest results (GET /results)

Run:
    pip install fastapi uvicorn
    python server.py
"""

from __future__ import annotations

import json
import asyncio
import threading
import time
import datetime
import traceback
import gc
import tracemalloc
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI
try:
    from fastapi import Request
except ImportError:  # minimal fastapi stub in tests/test_lifecycle.py
    from typing import Any as Request  # type: ignore[assignment]
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

import data_provider
import scan_control

# ─────────────────────────────────────────────────────────────────────────────
# DATA PROVIDER CONFIG  ←── change these to switch market-data source
# ─────────────────────────────────────────────────────────────────────────────
# "ibkr"     → Interactive Brokers TWS / IB Gateway (default; real bid/ask,
#              no HTTP rate limiting, far more reliable than Yahoo)
# "yfinance" → the original Yahoo path, kept fully intact as a fallback
#
# Requirements for "ibkr":
#   1. pip install ib_insync
#   2. TWS or IB Gateway running and logged in
#   3. Global Config → API → Settings → "Enable ActiveX and Socket Clients"
#   4. Port below matches TWS (7497 = paper, 7496 = live)
#
# Every value below can be overridden by an environment variable of the same
# name, so deployments don't have to patch this file:
#   DATA_PROVIDER=yfinance IBKR_PORT=7496 python server.py
import os as _os


def _env_str(name: str, default: str) -> str:
    v = _os.getenv(name)
    return default if v is None or not v.strip() else v.strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env_str(name, str(default)))
    except ValueError:
        print(f"[server] {name} is not an integer; using {default}")
        return default


def _env_bool(name: str, default: bool) -> bool:
    return _env_str(name, "true" if default else "false").lower() in (
        "1", "true", "yes", "on")


DATA_PROVIDER  = _env_str("DATA_PROVIDER", "yfinance").lower()

IBKR_HOST      = _env_str("IBKR_HOST", "127.0.0.1")
IBKR_PORT      = _env_int("IBKR_PORT", 7497)       # 7497 paper · 7496 live
IBKR_CLIENT_ID = _env_int("IBKR_CLIENT_ID", 17)    # unique per API client

# When True, an IBKR failure (TWS down, ib_insync missing, contract unresolved)
# transparently falls back to yfinance for that call rather than failing the
# scan. Set False to make IBKR problems loud instead of silent.
IBKR_FALLBACK_TO_YFINANCE = _env_bool("IBKR_FALLBACK_TO_YFINANCE", True)

# ── Put-selling strategy config ─────────────────────────────────────────────────
# Exposed here rather than only inside the strategy module because these three
# change the *meaning* of the output, not just a threshold, so an operator needs
# to set them without editing code:
#   PS_MODE    fcf_premium | delta | moneyness   — how the strike is chosen
#   PS_WEIGHT  risk | equal | premium | kelly_lite — how the book is weighted
#   PS_QUINTILE  true  — restrict to the top FCF-yield quintile (the paper's rule).
#                Set false for a wider scan; phase 2 then costs ~5x more.
#   PS_UNIVERSE_MAX  0 = no cap. Caps the universe BEFORE the phase-1 fundamentals
#                sweep. This is the knob that shortens time-to-first-row: phase 2
#                (which emits rows) cannot start until phase 1 finishes for every
#                name, so on a cold cache the put-selling table is empty for the
#                entire fundamentals sweep. PS_MAX_NAMES does NOT help here --
#                it trims survivors after phase 1 has already run in full.
#   PS_MAX_NAMES 0 = no cap. A cap is useful for a fast smoke test on a full
#                universe without waiting for every chain.
PS_MODE      = _env_str("PS_MODE", "fcf_premium").lower()
PS_WEIGHT    = _env_str("PS_WEIGHT", "risk").lower()
PS_QUINTILE  = _env_bool("PS_QUINTILE", True)
PS_MAX_NAMES = _env_int("PS_MAX_NAMES", 0)
# Seconds to pause before the put-selling phase-2 chain sweep. It runs LAST,
# after three strategies have already hammered Yahoo, and the logs showed
# every chain fetch in that window coming back "Too Many Requests" -- which
# surfaced as "101 had no viable strike/chain" and an empty table. A cool-down
# lets the rate limiter reset before the most expensive sweep begins.
PS_COOLDOWN_SEC = _env_int("PS_COOLDOWN_SEC", 90)

# ── Continuous-mode default ─────────────────────────────────────────────────
# The scanner should be running the moment the process comes up — not only
# after someone happens to open the dashboard in a browser and its JS fires
# a /run/continuous call. CONTINUOUS_DEFAULT starts the loop automatically;
# CONTINUOUS_INTERVAL_SEC sets the gap *after* each cycle finishes before the
# next one begins (never a fixed wall-clock schedule — see _continuous_loop).
# This is wired up via a FastAPI startup event further down (not an
# `if __name__ == "__main__":` guard), because this app is launched as
# `uvicorn server:app` in production — that imports this file as a module,
# so __name__ is "server", not "__main__", and a __main__-guarded block would
# silently never run. Set CONTINUOUS_DEFAULT=false to disable.
CONTINUOUS_DEFAULT      = _env_bool("CONTINUOUS_DEFAULT", True)
CONTINUOUS_INTERVAL_SEC = max(30, _env_int("CONTINUOUS_INTERVAL_SEC", 60))

# ── Strategy selection ──────────────────────────────────────────────────────
# Which of the four strategies run (and are shown on the dashboard).
#   python server.py --strategies "1,3"      -> vol_momentum + earnings only
#   STRATEGIES="1,3" uvicorn server:app ...  -> same, for docker / uvicorn
# Numbers follow the dashboard order; key names ("calendar") are accepted too.
# Default: all four. The CLI flag wins over the env var.
STRATEGY_ORDER: list[str] = ["vol_momentum", "calendar", "earnings", "put_selling"]
_STRATEGY_ALIASES: dict[str, str] = {
    "1": "vol_momentum", "2": "calendar", "3": "earnings", "4": "put_selling",
    "vol_momentum": "vol_momentum", "vm": "vol_momentum", "vertical": "vol_momentum",
    "calendar": "calendar", "cal": "calendar",
    "earnings": "earnings", "straddle": "earnings",
    "put_selling": "put_selling", "ps": "put_selling", "csp": "put_selling",
}


def _parse_strategy_selection(raw: Optional[str]) -> list[str]:
    """'1,3' / '1 3' / 'vm,csp' -> ordered unique list of strategy keys."""
    if raw is None or not str(raw).strip():
        return list(STRATEGY_ORDER)
    chosen: list[str] = []
    for tok in str(raw).replace(";", ",").replace(" ", ",").split(","):
        tok = tok.strip().lower()
        if not tok:
            continue
        key = _STRATEGY_ALIASES.get(tok)
        if key is None:
            raise SystemExit(f"--strategies: unknown strategy '{tok}' "
                            f"(use 1-4 or {', '.join(STRATEGY_ORDER)})")
        if key not in chosen:
            chosen.append(key)
    return [k for k in STRATEGY_ORDER if k in chosen]


def _strategy_selection_from_cli() -> Optional[str]:
    """
    Read --strategies from sys.argv without disturbing uvicorn's own parser:
    parse_known_args ignores anything it doesn't recognise, and the flag is
    read at import time so `uvicorn server:app` and `python server.py` behave
    the same way.
    """
    import argparse
    import sys
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--strategies", "-s", default=None)
    ns, _ = p.parse_known_args(sys.argv[1:])
    return ns.strategies


ENABLED_STRATEGIES: list[str] = _parse_strategy_selection(
    _strategy_selection_from_cli() or _env_str("STRATEGIES", "")
)
if ENABLED_STRATEGIES != STRATEGY_ORDER:
    print(f"[server] Strategies enabled: {', '.join(ENABLED_STRATEGIES)} "
          f"(disabled: {', '.join(k for k in STRATEGY_ORDER if k not in ENABLED_STRATEGIES)})")


def _enabled(key: str) -> bool:
    return key in ENABLED_STRATEGIES

# ── Memory diagnostics (opt-in) ─────────────────────────────────────────────
# tracemalloc adds real overhead (it tracks every allocation's call site), so
# it's off unless explicitly requested — set MEMORY_DEBUG=true and restart to
# turn it on for a diagnostic run, then hit GET /debug/memory.
MEMORY_DEBUG = _env_bool("MEMORY_DEBUG", False)
if MEMORY_DEBUG:
    tracemalloc.start(10)  # keep 10 stack frames per allocation


def _configure_data_provider() -> None:
    """
    Push the config above into data_provider. Runs at import time, before any
    scanner thread can start, so strategies never resolve a provider mid-flight.
    """
    data_provider.IBKR_HOST      = IBKR_HOST
    data_provider.IBKR_PORT      = IBKR_PORT
    data_provider.IBKR_CLIENT_ID = IBKR_CLIENT_ID
    data_provider.IBKR_FALLBACK_TO_YFINANCE = IBKR_FALLBACK_TO_YFINANCE
    active = data_provider.set_provider(DATA_PROVIDER)
    detail = ""
    if active == "ibkr":
        detail = (f" (TWS {IBKR_HOST}:{IBKR_PORT}, clientId={IBKR_CLIENT_ID}, "
                  f"fallback={'on' if IBKR_FALLBACK_TO_YFINANCE else 'off'})")
    print(f"[server] Data provider: {active}{detail}")


_configure_data_provider()

# ─────────────────────────────────────────────────────────────────────────────
# STATE  (shared between scanner thread and HTTP handlers)
# ─────────────────────────────────────────────────────────────────────────────


def _sanitize(obj):
    """
    Recursively replace NaN/Inf float values with None so json.dumps never
    raises "Out of range float values are not JSON compliant". This is a
    safety net — individual strategies should avoid producing NaN/Inf in
    the first place, but this guarantees the server never crashes a
    response even if one slips through.
    """
    if isinstance(obj, float):
        if obj != obj or obj in (float("inf"), float("-inf")):  # obj != obj is the NaN check
            return None
        return obj
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v) for v in obj]
    return obj


def _now_iso(timespec: str = "seconds") -> str:
    """
    Timezone-AWARE UTC timestamp, e.g. '2026-09-24T10:15:00+00:00'.

    Every timestamp the server hands to the browser MUST go through this.
    datetime.now().isoformat() yields a *naive* string (no offset); JavaScript's
    Date parser treats a naive ISO string as the browser's LOCAL time, so a UTC
    container talking to a UTC+3 browser made every row look 3h older than it
    was. With an explicit offset the browser converts correctly no matter what
    zone either side runs in.
    """
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec=timespec)


_lock = threading.Lock()

_state: dict = {
    # phase: idle | prefetch | scanning | done | error
    # The dashboard's DOMContentLoaded handler branches on this to decide
    # whether to restore a previous scan or auto-start a new one. Without it
    # a page reload silently discards a completed scan and re-runs everything.
    "phase":     "idle",
    "prefetch":  {"status": "idle", "detail": ""},
    "running":   False,
    "started_at":  None,
    "finished_at": None,

    # ── Continuous mode ───────────────────────────────────────────────────
    # `continuous` is the operator's intent (keep looping); `running` is the
    # instantaneous fact (a cycle is executing right now). They are separate
    # because between cycles the scanner sleeps: continuous=True, running=False.
    # Collapsing them into one flag would make /stop ambiguous during the sleep.
    "continuous":     False,
    "cycle":          0,      # increments once per full pass over all strategies
    "cycle_started_at":  None,
    "cycle_finished_at": None,
    "next_cycle_at":     None,   # ISO time the next pass begins (drives the UI countdown)
    "stop_requested": False,
    "continuous_interval_sec": None,  # gap after a cycle finishes; set when the loop starts

    "strategies": {
        "vol_momentum": {
            "label":    "Vol & Momentum Screener",
            "status":   "idle",      # idle | running | done | error
            "progress": {"current": 0, "total": 0},
            "results":  [],
            # [{ticker, reason}] for tickers that never reached scoring.
            # Surfaced by the dashboard's "Excluded" tile.
            "excluded": [],
        },
        "calendar": {
            "label":    "Calendar Spread Screener",
            "status":   "idle",
            "progress": {"current": 0, "total": 0},
            "results":  [],
        },
        "earnings": {
            "label":    "Earnings Options Scanner",
            "status":   "idle",
            "progress": {"current": 0, "total": 0},
            "results":  [],
        },
        "put_selling": {
            "label":    "Put Selling (GS FCF)",
            "status":   "idle",
            "progress": {"current": 0, "total": 0},
            "results":  [],
            # This strategy runs in two phases with different totals (universe
            # fundamentals, then chains for survivors only), so the progress bar
            # would otherwise appear to jump backwards when phase 2 starts. The
            # dashboard reads `phase` to label which stage is being counted.
            "phase":    "",
            # Book-level aggregates from portfolio_summary(). Kept separate from
            # `results` because they describe the set, not any single row.
            "summary":  {},
        },
    },
}

# SSE event queue — list of JSON strings pushed to all listeners
_sse_events: list[str] = []
_sse_lock   = threading.Lock()


def _push_event(kind: str, strategy: str, payload: dict) -> None:
    """Append a SSE-compatible JSON event to the queue."""
    try:
        event = json.dumps({"kind": kind, "strategy": strategy, "payload": _sanitize(payload),
                            "ts": _now_iso("microseconds")})
    except (TypeError, ValueError) as e:
        # Never let a bad payload kill the scan thread — log and skip this event
        print(f"[_push_event] Failed to serialize event ({kind}/{strategy}): {e}")
        return
    with _sse_lock:
        _sse_events.append(event)
    # Keep queue bounded
    with _sse_lock:
        if len(_sse_events) > 10_000:
            del _sse_events[:5_000]


def _update_strategy(key: str, **kwargs) -> None:
    with _lock:
        for k, v in kwargs.items():
            _state["strategies"][key][k] = v


# ──────────────────────────────────────────────────────────────────────────
# ROW IDENTITY + TIMESTAMPS
#
# In continuous mode a strategy re-scans the same names every cycle, so rows
# must be REPLACED in place rather than appended. Appending was fine for a
# one-shot run but would grow the table without bound once the scanner loops.
#
# Each strategy needs its own notion of "same row":
#   vol_momentum / earnings / put_selling  → one row per ticker
#   calendar                               → one row per (ticker, expiry pair);
#                                            a single ticker legitimately yields
#                                            several rows for different DTE
#                                            buckets, so keying on ticker alone
#                                            would discard all but the last.
# ──────────────────────────────────────────────────────────────────────────

def _row_identity(strategy: str, row: dict) -> tuple:
    """Return the tuple that uniquely identifies `row` within `strategy`."""
    ticker = row.get("ticker")
    if strategy == "calendar":
        return (ticker, row.get("exp_short"), row.get("exp_long"))
    return (ticker,)


def _stamp(row: dict, cycle: int) -> dict:
    """
    Attach freshness metadata to a result row.

    `as_of` is what the dashboard renders as row age. It is set when the row is
    written, not when the cycle started, because a cycle can take many minutes
    and the first ticker scanned is genuinely older than the last.
    """
    # `_keep_as_of` marks a re-published previous row (the ticker's fresh
    # evaluation failed transiently): refresh the cycle stamp so it survives
    # pruning, but keep the honest timestamp of when the signal was computed.
    if not row.pop("_keep_as_of", False) or not row.get("as_of"):
        row["as_of"] = _now_iso()
    row["cycle"] = cycle
    return row


def _upsert_rows(strategy: str, rows: list[dict]) -> None:
    """
    Insert-or-replace `rows` into a strategy's result list, stamping each with
    the current time and cycle number.

    Replacing by identity is what makes continuous scanning non-destructive:
    the previous cycle's row stays visible (and keeps its old timestamp) until
    the new one is ready to take its place, so the dashboard never blanks out.
    """
    if not rows:
        return
    with _lock:
        cycle    = _state.get("cycle", 0)
        existing = _state["strategies"][strategy]["results"]
        index    = {_row_identity(strategy, r): i for i, r in enumerate(existing)}
        for row in rows:
            _stamp(row, cycle)
            key = _row_identity(strategy, row)
            idx = index.get(key)
            if idx is not None:
                existing[idx] = row
            else:
                index[key] = len(existing)
                existing.append(row)


def _drop_row(strategy: str, ticker: str | None) -> None:
    """
    Remove every row for `ticker` from a strategy's results.

    Used when a name that previously produced a row is rejected on a later
    cycle. Upserting alone can't express this: there is no new row to overwrite
    the old one with, so without an explicit delete the rejected candidate
    would linger until _prune_stale eventually aged it out.
    """
    if not ticker:
        return
    with _lock:
        rows = _state["strategies"][strategy]["results"]
        kept = [r for r in rows if r.get("ticker") != ticker]
        if len(kept) != len(rows):
            _state["strategies"][strategy]["results"] = kept


# A ticker whose FILTERED reason matches one of these was rejected because the
# DATA SOURCE failed, not because the name stopped qualifying. Such a rejection
# must never delete a previously published signal: the signal stays until the
# same ticker is successfully re-evaluated (either re-scored, which replaces the
# row, or genuinely rejected on real data, which removes it).
_TRANSIENT_REASON_MARKERS = (
    "too many requests", "rate limit", "timed out", "timeout", "connection",
    "history error", "options list error", "chain error", "unexpected error",
    "fetch failed", "no options found", "could not determine", "all chain fetches",
    "insufficient history", "insufficient close", "spot price unavailable",
    "options list empty", "empty option chain", "no two-sided quotes",
    "cannot compute rv20", "possibly delisted", "no price data",
)


def _is_transient_reason(reason) -> bool:
    text = str(reason or "").lower()
    return any(m in text for m in _TRANSIENT_REASON_MARKERS)


def _prune_stale(strategy: str, keep_cycles: int = 2, min_fresh_rows: int = 1) -> None:
    """
    Drop rows that have not been refreshed for `keep_cycles` cycles.

    A name can stop qualifying (volume dries up, earnings pass, spread widens).
    Without pruning it would sit in the table forever showing an ever-older
    timestamp. Rows are kept for more than one cycle so a single transient data
    failure doesn't make a good candidate flicker out of the dashboard.

    Guard: pruning only runs when the cycle that just finished actually
    produced data for this strategy (at least `min_fresh_rows` rows stamped
    with the current cycle). A cycle that came back empty -- Yahoo returning
    429 for every ticker, the volume batch download yielding nothing -- has
    told us nothing about whether the old signals still hold, so it must not
    age them out. Left unguarded, two consecutive rate-limited cycles wiped
    every table overnight, which is exactly what happened on a 12h run.
    """
    with _lock:
        cycle = _state.get("cycle", 0)
        rows  = _state["strategies"][strategy]["results"]
        n_fresh_now = sum(1 for r in rows if r.get("cycle") == cycle)
        if n_fresh_now < min_fresh_rows:
            print(f"[scanner] {strategy}: cycle {cycle} produced no rows -- "
                  f"skipping stale-prune, keeping {len(rows)} previous rows")
            return
        fresh = [r for r in rows if cycle - r.get("cycle", cycle) < keep_cycles]
        dropped = len(rows) - len(fresh)
        if dropped:
            _state["strategies"][strategy]["results"] = fresh
    if dropped:
        _push_event("rows_pruned", strategy, {"dropped": dropped, "remaining": len(fresh)})


def _set_phase(phase: str) -> None:
    """
    Set the top-level run phase: idle | prefetch | scanning | done | error.

    The dashboard reads this on page load to decide whether to restore the
    previous scan's results or auto-start a fresh run. /health reports it too.
    """
    with _lock:
        _state["phase"] = phase


def _set_prefetch(status: str, detail: str = "") -> None:
    with _lock:
        _state["prefetch"] = {"status": status, "detail": detail}


def _strategy_failed(key: str, exc: BaseException) -> None:
    """
    Record a strategy failure and decide whether it should propagate.

    Strategy modules have historically called sys.exit() on data-source
    failure. sys.exit raises SystemExit, which derives from BaseException and
    is therefore NOT caught by `except Exception`. When that happened the
    scanner thread died mid-run, _state["running"] stayed True forever, and
    every later POST /run replied "already running" until the process was
    restarted. Callers now catch BaseException and route here.

    This also prints a full traceback to the server console. Previously this
    function only updated internal state and fired a terse SSE event — a
    strategy that failed on every cycle looked, from the dashboard, exactly
    like one that simply never returned any candidates. In continuous mode
    that distinction matters a lot more than it used to: a strategy that
    fetches for hundreds of tickers every cycle, forever, is far more likely
    to eventually hit a rate limit than the same code run manually once in a
    while, and without this there was no way to see that from `kubectl logs`.

    KeyboardInterrupt is re-raised so a real Ctrl-C still stops the process;
    everything else (including SystemExit) is downgraded to a per-strategy
    error so the remaining strategies still get their turn.
    """
    _update_strategy(key, status="error")
    detail = str(exc) or type(exc).__name__
    if isinstance(exc, SystemExit):
        detail = f"strategy aborted (sys.exit({exc.code})) — see server log"
    print(f"[scanner] {key} failed: {type(exc).__name__}: {exc}")
    traceback.print_exception(type(exc), exc, exc.__traceback__)
    _push_event("strategy_error", key, {"error": detail})
    if isinstance(exc, KeyboardInterrupt):
        raise exc


# ─────────────────────────────────────────────────────────────────────────────
# SCANNER THREAD
# ─────────────────────────────────────────────────────────────────────────────

def _scan_pipeline(only: Optional[str] = None) -> None:
    """
    Run the strategies in sequence. Called by _run_all_strategies (all of them)
    and by the strategy-refresh endpoint (`only` = that one strategy).
    """
    def _skip(key: str) -> bool:
        if not _enabled(key):
            _update_strategy(key, status="disabled")
            return True
        return only is not None and key != only

    # ── Strategy 1: Vol & Momentum ────────────────────────────────────────────
    if _skip("vol_momentum"):
        pass
    else:
      try:
        import strategy_vol_momentum as s1
        _update_strategy("vol_momentum", status="running")
        _push_event("strategy_started", "vol_momentum", {"label": "Vol & Momentum Screener"})

        vm_results: list[dict] = []
        # Rows genuinely rejected this cycle are NOT deleted immediately. They
        # are collected and only removed at the end of the cycle if the cycle
        # itself was healthy (it scored at least one name). A cycle where Yahoo
        # throttled everything can still emit a few "genuine-looking" rejections
        # (a 429 dressed up as an empty chain); deleting on those, one ticker
        # per cycle, is how the table drained to "Awaiting scan" over a night
        # even with the stale-prune guard in place.
        vm_pending_drops: set[str] = set()

        def on_vm_progress(result: dict) -> None:
            prog = result.get("_progress", {})
            _update_strategy(
                "vol_momentum",
                progress={"current": prog.get("current", 0), "total": prog.get("total", 0)},
            )
            if result.get("status") == "SCORED" or result.get("_final_update"):
                # Insert-or-replace by ticker, stamped with as_of + cycle.
                clean = {k: v for k, v in result.items() if not k.startswith("_")}
                _upsert_rows("vol_momentum", [clean])
                vm_results.append(result)
                # Push full result immediately so frontend renders this row now
                _push_event("ticker_result", "vol_momentum", clean)

            _push_event("ticker_done", "vol_momentum", {
                "ticker":   result.get("ticker"),
                "status":   result.get("status"),
                "progress": prog,
                "conclusion": result.get("conclusion"),
                "composite":  result.get("composite"),
                # Why a ticker was dropped. Set by the strategy on every
                # FILTERED return; without it the Excluded tile can only show
                # a count and the user has no way to see what was skipped.
                "reason":     result.get("reason"),
            })
            if result.get("status") == "FILTERED":
                # Replace by ticker, not append: the same name is re-excluded
                # every cycle and a blind append would multiply the Excluded
                # tile by the cycle count.
                entry = {
                    "ticker": result.get("ticker"),
                    "reason": result.get("reason") or "Filtered",
                    "as_of":  datetime.datetime.now().isoformat(timespec="seconds"),
                }
                with _lock:
                    exc = _state["strategies"]["vol_momentum"].setdefault("excluded", [])
                    i = next((n for n, e in enumerate(exc)
                              if e.get("ticker") == entry["ticker"]), None)
                    if i is not None:
                        exc[i] = entry
                    else:
                        exc.append(entry)
                # A ticker that was scored last cycle but is genuinely filtered
                # now must lose its stale result row. A rejection caused by the
                # data source failing (rate limit, timeout, empty chain) says
                # nothing about the signal, so the old row is kept until the
                # ticker is re-evaluated on real data.
                if not _is_transient_reason(result.get("reason")):
                    tkr = result.get("ticker")
                    if tkr:
                        vm_pending_drops.add(tkr)

        s1.run(on_progress=on_vm_progress)

        # Apply deferred drops only when this cycle proved the feed was alive.
        # A ticker that was re-scored later in the same cycle is never dropped.
        n_scored = sum(1 for r in vm_results if r.get("status") == "SCORED")
        rescored = {r.get("ticker") for r in vm_results if r.get("status") == "SCORED"}
        if n_scored > 0:
            for tkr in vm_pending_drops - rescored:
                _drop_row("vol_momentum", tkr)
        elif vm_pending_drops:
            print(f"[scanner] vol_momentum: cycle scored 0 names -- deferring "
                  f"{len(vm_pending_drops)} genuine-looking rejections, keeping old rows")
        _prune_stale("vol_momentum")
        _update_strategy("vol_momentum", status="done")
        _push_event("strategy_done", "vol_momentum", {})

      except BaseException as e:
        _strategy_failed("vol_momentum", e)

    # ── Strategy 2: Calendar Spread ───────────────────────────────────────────
    if _skip("calendar"):
        pass
    else:
      try:
        import strategy_calendar as s2
        _update_strategy("calendar", status="running")
        _push_event("strategy_started", "calendar", {"label": "Calendar Spread Screener"})

        def on_cal_progress(event: dict) -> None:
            prog = event.get("_progress", {})
            _update_strategy(
                "calendar",
                progress={"current": prog.get("current", 0), "total": prog.get("total", 0)},
            )
            pairs = event.get("pairs", [])
            if pairs:
                # This event is the ticker's COMPLETE fresh result set, so
                # expiry pairs it no longer contains have stopped qualifying:
                # drop them, then upsert the new ones on (ticker, exp_short,
                # exp_long) so a re-quoted pair replaces last cycle's row
                # instead of stacking a duplicate beside it. A ticker that
                # returns zero pairs is NOT touched -- on yfinance that is
                # nearly always a failed chain fetch, not a lost signal.
                t = event.get("ticker")
                keep = {(p.get("exp_short"), p.get("exp_long")) for p in pairs}
                with _lock:
                    rows = _state["strategies"]["calendar"]["results"]
                    _state["strategies"]["calendar"]["results"] = [
                        r for r in rows
                        if r.get("ticker") != t
                        or (r.get("exp_short"), r.get("exp_long")) in keep
                    ]
                _upsert_rows("calendar", pairs)
                # Push each pair immediately so frontend renders rows as found
                _push_event("ticker_result", "calendar", {
                    "ticker": event.get("ticker"),
                    "pairs":  pairs,
                })

            _push_event("ticker_done", "calendar", {
                "ticker":   event.get("ticker"),
                "found":    event.get("found", 0),
                "progress": prog,
            })

        s2.run(on_progress=on_cal_progress)
        _prune_stale("calendar")
        _update_strategy("calendar", status="done")
        _push_event("strategy_done", "calendar", {})

      except BaseException as e:
        _strategy_failed("calendar", e)

    # ── Strategy 3: Earnings Scanner ──────────────────────────────────────────
    if _skip("earnings"):
        pass
    else:
      try:
        import strategy_earnings as s3
        _update_strategy("earnings", status="running")
        _push_event("strategy_started", "earnings", {"label": "Earnings Options Scanner"})

        def on_earn_progress(result: dict) -> None:
            prog = result.get("_progress", {})
            _update_strategy(
                "earnings",
                progress={"current": prog.get("current", 0), "total": prog.get("total", 0)},
            )
            clean = {k: v for k, v in result.items() if not k.startswith("_")}
            # A data-source failure (rate limit, empty chain) must not replace
            # a verdict we already hold for this ticker. Publish the error row
            # only when there is nothing better on the table; otherwise keep
            # the previous verdict and just refresh its cycle stamp so it is
            # not aged out by _prune_stale.
            publish = clean
            if clean.get("error") and _is_transient_reason(clean.get("error")):
                with _lock:
                    prev = next((r for r in _state["strategies"]["earnings"]["results"]
                                 if r.get("ticker") == clean.get("ticker") and not r.get("error")),
                                None)
                if prev is not None:
                    publish = dict(prev)
                    publish["_keep_as_of"] = True
            _upsert_rows("earnings", [publish])
            publish.pop("_keep_as_of", None)
            # Push full result immediately so frontend renders this row now
            _push_event("ticker_result", "earnings", publish)

            _push_event("ticker_done", "earnings", {
                "ticker":   result.get("ticker"),
                "verdict":  result.get("verdict"),
                "error":    result.get("error"),
                "progress": prog,
            })

        s3.run(on_progress=on_earn_progress)
        # Earnings rows go stale in a way the others don't: once a name has
        # reported, the strategy stops emitting it, so pruning is the only
        # thing that clears a past event off the table.
        _prune_stale("earnings")
        _update_strategy("earnings", status="done")
        _push_event("strategy_done", "earnings", {})

      except BaseException as e:
        _strategy_failed("earnings", e)

    # ── Strategy 4: Put Selling (GS FCF) ────────────────────────────────────────
    #
    # Structurally different from the other three, in a way that dictates the code
    # below. The first three emit a finished row per ticker, so the server appends
    # each one as it arrives. This strategy cannot: portfolio weights are relative
    # (1/(IV x delta), normalised across the whole set), so no row's weight or
    # contract count is knowable until every candidate exists. Streaming rows as
    # they arrive would publish weights that are wrong and then silently change.
    #
    # So progress events stream for the UI (the scan takes minutes and a dead
    # progress bar looks like a hang), but `results` is populated once from run()'s
    # return value, after weighting. Each streamed row is marked provisional so the
    # dashboard can render it greyed and replace it on strategy_done.
    if _skip("put_selling"):
        return
    try:
        import strategy_put_selling as s4
        _update_strategy("put_selling", status="running")
        _push_event("strategy_started", "put_selling", {"label": "Put Selling (GS FCF)"})

        if PS_COOLDOWN_SEC > 0 and data_provider.provider_status().get("effective") == "yfinance":
            _update_strategy("put_selling", phase="cooldown")
            _push_event("phase_update", "put_selling", {
                "phase":   "cooldown",
                "message": f"Cooling down {PS_COOLDOWN_SEC}s to let Yahoo rate limit reset",
                "progress": {"current": 0, "total": 0},
            })
            time.sleep(PS_COOLDOWN_SEC)

        def on_ps_progress(event: dict) -> None:
            prog  = event.get("_progress", {}) or {}
            phase = event.get("phase", "")
            _update_strategy(
                "put_selling",
                progress={"current": prog.get("current", 0), "total": prog.get("total", 0)},
                phase=phase,
            )

            # Phase boundary announcements (universe size, survivor count).
            if event.get("message"):
                _push_event("phase_update", "put_selling", {
                    "phase":    phase,
                    "message":  event.get("message"),
                    "eligible": event.get("eligible"),
                    "progress": prog,
                })

            cand = event.get("candidate")
            if cand:
                provisional = {k: v for k, v in cand.items() if not k.startswith("_")}
                provisional["_provisional"] = True
                _push_event("ticker_result", "put_selling", provisional)

            # Only per-ticker events carry a ticker. The two boundary events
            # ("Fetching FCF for N names", "K of N passed FCF screen") do not,
            # and emitting ticker_done for them would put null-ticker rows in the
            # dashboard's activity log.
            if event.get("ticker"):
                _push_event("ticker_done", "put_selling", {
                    "ticker":    event.get("ticker"),
                    "phase":     phase,
                    "found":     event.get("found", 0),
                    "fcf_yield": event.get("fcf_yield"),
                    "progress":  prog,
                })

        ps_results = s4.run(
            on_progress=on_ps_progress,
            mode=PS_MODE,
            weighting=PS_WEIGHT,
            top_quintile=PS_QUINTILE,
            max_names=PS_MAX_NAMES or None,
        ) or []
        ps_summary = s4.portfolio_summary(ps_results)

        # Upsert rather than assign. Weights are only final here, so this is
        # still a single atomic publication of the whole book -- but going
        # through _upsert_rows stamps every row with as_of/cycle and leaves
        # last cycle's rows in place if this cycle returned nothing (e.g. a
        # transient fundamentals outage), which a bare assignment would erase.
        _upsert_rows("put_selling", [
            {k: v for k, v in r.items() if not k.startswith("_")}
            for r in ps_results
        ])
        _prune_stale("put_selling")
        with _lock:
            # Only overwrite the book summary when this cycle actually produced
            # a book. On a dry cycle the previous rows are kept (see
            # _upsert_rows / _prune_stale), so the summary must stay consistent
            # with them rather than collapsing to {"count": 0}.
            merged_rows = _state["strategies"]["put_selling"]["results"]
        if ps_results:
            # Weights were normalised over THIS cycle's set only; after the
            # upsert the visible book is the union with surviving older rows,
            # so recompute the strip over exactly the rows on screen.
            ps_summary = s4.portfolio_summary(merged_rows)
            with _lock:
                _state["strategies"]["put_selling"]["summary"] = ps_summary
        else:
            with _lock:
                ps_summary = _state["strategies"]["put_selling"].get("summary") or ps_summary

        _update_strategy("put_selling", status="done")
        # Final payload carries the authoritative weighted rows. The dashboard
        # replaces any provisional rows with these.
        # Ship the MERGED server-side book (this cycle's rows upserted over the
        # survivors from earlier cycles), not only this cycle's return value.
        # The dashboard replaces its table wholesale with this payload, so
        # sending a partial set -- e.g. the single name that priced before Yahoo
        # started returning 429 -- collapsed the visible book to one row.
        _push_event("strategy_done", "put_selling", {
            "results": _sanitize(merged_rows),
            "summary": _sanitize(ps_summary),
            "count":   len(merged_rows),
            "fresh":   len(ps_results),
        })

    except BaseException as e:
        _strategy_failed("put_selling", e)


def _run_all_strategies(reset: bool = True) -> None:
    """
    Run one full pass over all four strategies.

    `reset=True` (a fresh manual run) clears prior results so the dashboard
    starts from a clean table. `reset=False` is used by the continuous loop:
    rows must survive between cycles because _upsert_rows refreshes them in
    place, and wiping the list every cycle would make the dashboard flash empty
    for the several minutes it takes to repopulate.

    The try/finally is the important part: no matter how _scan_pipeline exits
    (normal return, unexpected exception, or a stray sys.exit inside a strategy
    module) the running flag is cleared and a terminal phase is published. That
    guarantees POST /run can always start a new scan and the dashboard never
    gets stuck on a spinner.
    """
    with _lock:
        _state["phase"]       = "prefetch"
        _state["prefetch"]    = {"status": "idle", "detail": ""}
        _state["running"]     = True
        _state["started_at"]  = _state["started_at"] or _now_iso()
        _state["finished_at"] = None
        _state["cycle"]      += 1
        _state["cycle_started_at"]  = _now_iso()
        _state["cycle_finished_at"] = None
        _state["next_cycle_at"]     = None
        cycle_no = _state["cycle"]
        for s in _state["strategies"].values():
            s["status"]   = "idle"
            s["progress"] = {"current": 0, "total": 0}
            if reset:
                s["results"] = []
                if "excluded" in s:
                    s["excluded"] = []
                # put_selling only. Without this a new run that finds zero
                # candidates would still display the previous run's book
                # summary, which reads as a live result rather than stale data.
                if "summary" in s:
                    s["summary"] = {}
            if "phase" in s:
                s["phase"] = ""

    _push_event("run_started", "all", {"cycle": cycle_no, "reset": reset})

    # Prefetch phase: import the strategy modules up front so heavyweight
    # imports (yfinance/pandas/scipy) and any import-time failure surface
    # before the first ticker is fetched. Failures are only recorded here —
    # _scan_pipeline re-imports and routes them through _strategy_failed so a
    # single broken module never blocks the others.
    _set_phase("prefetch")
    _set_prefetch("running", "loading strategy modules")
    _prefetch_errors: list[str] = []
    for _mod in ("strategy_vol_momentum", "strategy_calendar", "strategy_earnings",
                 "strategy_put_selling"):
        if _mod.replace("strategy_", "") not in ENABLED_STRATEGIES:
            continue
        try:
            __import__(_mod)
        except BaseException as e:
            _prefetch_errors.append(f"{_mod}: {type(e).__name__}: {e}")
    if _prefetch_errors:
        _set_prefetch("error", "; ".join(_prefetch_errors))
        _push_event("prefetch_error", "all", {"errors": _prefetch_errors})
    else:
        _set_prefetch("done", "strategy modules loaded")

    _set_phase("scanning")

    failed = False
    try:
        _scan_pipeline()
    except BaseException as e:
        failed = True
        print(f"[scanner] run aborted: {type(e).__name__}: {e}")
        _push_event("run_error", "all", {"error": f"{type(e).__name__}: {e}"})
    finally:
        with _lock:
            _state["running"]     = False
            _state["finished_at"] = _now_iso()
            _state["cycle_finished_at"] = _state["finished_at"]
            statuses = [s["status"] for k, s in _state["strategies"].items()
                        if _enabled(k)]
            # "error" only when the whole run blew up or every strategy failed;
            # a partial failure still counts as done so results stay viewable.
            if failed or all(st == "error" for st in statuses):
                _state["phase"] = "error"
            else:
                _state["phase"] = "done"
            final_phase = _state["phase"]
            cycle_no    = _state["cycle"]
            counts      = {k: len(v["results"]) for k, v in _state["strategies"].items()}
        _push_event("run_complete", "all", {
            "phase": final_phase, "cycle": cycle_no, "counts": counts,
        })


# ──────────────────────────────────────────────────────────────────────────────
# CONTINUOUS MODE
#
# One long-lived thread runs cycle after cycle. It is deliberately a loop inside
# a single thread rather than a thread per cycle: the strategies share a data
# provider and a process-wide cache, and overlapping cycles would double the
# request rate against TWS while producing rows whose timestamps interleave.
# ──────────────────────────────────────────────────────────────────────────

# threading.Event, not a bare bool: it makes the inter-cycle sleep interruptible.
# With time.sleep() a /stop during a 5-minute gap would not take effect until the
# sleep expired, so the UI would sit on "stopping..." for minutes.
_stop_event   = threading.Event()
_loop_thread: Optional[threading.Thread] = None


def _continuous_loop(interval_sec: int) -> None:
    """
    Run cycles back-to-back until stopped, sleeping `interval_sec` between them.

    The first cycle resets (clean table for a freshly-started session); every
    later cycle upserts on top of the previous one so rows are refreshed in
    place instead of the dashboard blanking and slowly refilling.
    """
    first = True
    try:
        while not _stop_event.is_set():
            try:
                _run_all_strategies(reset=first)
            except BaseException as e:
                # A cycle must never kill the loop. _run_all_strategies already
                # has its own guard; this is the backstop for anything that
                # escapes it (e.g. a failure inside the finally block itself).
                print(f"[scanner] cycle failed: {type(e).__name__}: {e}")
                _push_event("run_error", "all", {"error": f"{type(e).__name__}: {e}"})
            first = False

            if _stop_event.is_set():
                break

            # Resource line per cycle: if open_fds or threads climbs cycle
            # after cycle, something is leaking and these logs show it days
            # before the OS limit is hit.
            try:
                import os
                _fds = len(os.listdir("/proc/self/fd"))
            except Exception:
                _fds = -1
            print(f"[scanner] resources: open_fds={_fds} threads={threading.active_count()} "
                  f"sse_clients={_sse_clients} rss_mb={_rss_mb()}")

            nxt = (datetime.datetime.now(datetime.timezone.utc)
                   + datetime.timedelta(seconds=interval_sec))
            with _lock:
                _state["next_cycle_at"] = nxt.isoformat(timespec="seconds")
            _push_event("cycle_sleep", "all", {
                "next_cycle_at": nxt.isoformat(timespec="seconds"),
                "interval_sec":  interval_sec,
            })
            # Returns True the moment /stop fires, so the wait is cut short.
            _stop_event.wait(interval_sec)
    finally:
        with _lock:
            _state["continuous"]     = False
            _state["running"]        = False
            _state["next_cycle_at"]  = None
            _state["stop_requested"] = False
            _state["continuous_interval_sec"] = None
        _push_event("continuous_stopped", "all", {})


# ─────────────────────────────────────────────────────────────────────────────
# FASTAPI APP
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(title="Options Scanner Suite")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DASHBOARD_PATH = Path(__file__).parent / "dashboard.html"


@app.on_event("startup")
async def _auto_start_continuous() -> None:
    """
    Start the continuous scan loop the moment the app comes up — not only
    once someone opens the dashboard in a browser.

    This used to live in `if __name__ == "__main__":`, which only runs when
    this file is executed directly (`python server.py`). In production this
    app is launched as `uvicorn server:app ...` (see the Dockerfile), which
    *imports* this module instead of running it as a script — so __name__ is
    "server", not "__main__", and that block silently never ran. A FastAPI
    startup event fires on every launch path (direct script, uvicorn CLI,
    gunicorn+uvicorn workers), so this is the one place that's guaranteed to
    run regardless of how the container starts the process.
    """
    if not CONTINUOUS_DEFAULT:
        print("[server] Continuous scanning: OFF (CONTINUOUS_DEFAULT=false) — "
              "single-shot only until /run/continuous is called")
        return
    print(f"[server] Continuous scanning: ON — next cycle starts "
          f"{CONTINUOUS_INTERVAL_SEC}s after each one finishes "
          f"(CONTINUOUS_INTERVAL_SEC / CONTINUOUS_DEFAULT=false to change)")
    _start_continuous(CONTINUOUS_INTERVAL_SEC)


@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    if DASHBOARD_PATH.exists():
        return HTMLResponse(content=DASHBOARD_PATH.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>dashboard.html not found</h1>", status_code=404)


@app.post("/run")
async def start_run():
    """Single one-shot cycle. Resets the tables first."""
    with _lock:
        if _state["running"]:
            return JSONResponse({"ok": False, "reason": "already running"})
        # A one-shot run while the loop is mid-sleep would produce a cycle the
        # loop doesn't know about, and both would then write rows concurrently.
        if _state["continuous"]:
            return JSONResponse(
                {"ok": False, "reason": "continuous mode active — stop it first"}
            )

    t = threading.Thread(target=_run_all_strategies, daemon=True)
    t.start()
    return JSONResponse({"ok": True})


def _start_continuous(interval_sec: int) -> dict:
    """
    Launch the continuous loop thread, or report why it can't start.

    Shared by POST /run/continuous and the startup-event auto-start below, so
    both paths get the same guard against double-starting (already
    continuous) and against racing a one-shot scan that's mid-flight
    (already running).
    """
    interval_sec = max(30, int(interval_sec))

    global _loop_thread
    with _lock:
        if _state["continuous"] or _state["running"]:
            return {"ok": False, "reason": "already running"}
        _state["continuous"]     = True
        _state["stop_requested"] = False
        _state["continuous_interval_sec"] = interval_sec

    _stop_event.clear()
    _loop_thread = threading.Thread(
        target=_continuous_loop, args=(interval_sec,), daemon=True
    )
    _loop_thread.start()
    _push_event("continuous_started", "all", {"interval_sec": interval_sec})
    return {"ok": True, "interval_sec": interval_sec}


@app.post("/run/continuous")
async def start_continuous(interval_sec: int = CONTINUOUS_INTERVAL_SEC):
    """
    Start looping cycles until /stop.

    interval_sec is the gap *between* cycles, not a fixed period: a cycle that
    overruns the interval is never overlapped by the next one.
    """
    return JSONResponse(_start_continuous(interval_sec))


@app.post("/stop")
async def stop_continuous():
    """
    Ask the loop to stop.

    Returns immediately rather than joining the thread: an in-flight cycle can
    take minutes, and blocking the HTTP response that long would look like the
    UI had hung. The loop clears `continuous` and emits continuous_stopped when
    it actually exits, which is what the dashboard listens for.
    """
    with _lock:
        if not _state["continuous"]:
            return JSONResponse({"ok": False, "reason": "not running"})
        _state["stop_requested"] = True
        mid_cycle = _state["running"]

    _stop_event.set()
    _push_event("stop_requested", "all", {"mid_cycle": mid_cycle})
    return JSONResponse({"ok": True, "mid_cycle": mid_cycle})


# ──────────────────────────────────────────────────────────────────────────
# ON-DEMAND SIGNAL REFRESH
#
# The dashboard's ⟳ Refresh button re-fetches ONE signal on live data. The
# background scan is paused (scan_control.priority) for the duration: it
# finishes the ticker it is on, then waits, so the refresh has the data
# provider to itself and is not queued behind hundreds of tickers.
# ──────────────────────────────────────────────────────────────────────────
import scan_control

_refresh_lock = threading.Lock()   # one refresh at a time


def _refresh_signal(strategy: str, ticker: str, row_key: dict) -> dict:
    import market_cache
    t = ticker.upper()
    with _lock:
        prev_rows = [dict(r) for r in _state["strategies"][strategy]["results"]
                     if r.get("ticker") == t]

    _push_event("refresh_started", strategy, {"ticker": t})
    with scan_control.priority():
        market_cache.clear_symbol(t)

        if strategy == "vol_momentum":
            import strategy_vol_momentum as s1
            r = s1.refresh_one(t, prev_rows[0] if prev_rows else None)
            if r.get("status") != "SCORED":
                return {"ok": False, "reason": r.get("reason") or "no longer qualifies"}
            clean = {k: v for k, v in r.items() if not k.startswith("_")}
            _upsert_rows("vol_momentum", [clean])
            return {"ok": True, "row": clean}

        if strategy == "calendar":
            import strategy_calendar as s2
            pairs = s2.refresh_one(t, prev_rows)
            if not pairs:
                return {"ok": False, "reason": "no qualifying pair or no two-sided quote on refresh"}
            keep = {(p.get("exp_short"), p.get("exp_long")) for p in pairs}
            with _lock:
                rows = _state["strategies"]["calendar"]["results"]
                _state["strategies"]["calendar"]["results"] = [
                    r for r in rows
                    if r.get("ticker") != t or (r.get("exp_short"), r.get("exp_long")) in keep]
            _upsert_rows("calendar", pairs)
            want = (row_key.get("exp_short"), row_key.get("exp_long"))
            row = next((p for p in pairs if (p.get("exp_short"), p.get("exp_long")) == want), None)
            return {"ok": True, "pairs": pairs, "row": row,
                    "reason": None if row else "this expiry pair no longer qualifies; "
                                              "the ticker's other pairs were updated"}

        if strategy == "earnings":
            import strategy_earnings as s3
            r = s3.refresh_one(t, prev_rows[0] if prev_rows else None)
            clean = {k: v for k, v in r.items() if not k.startswith("_")}
            if clean.get("error"):
                return {"ok": False, "reason": clean["error"]}
            _upsert_rows("earnings", [clean])
            return {"ok": True, "row": clean}

        if strategy == "put_selling":
            import strategy_put_selling as s4
            cand = s4.refresh_one(t, prev_rows[0] if prev_rows else None, mode=PS_MODE)
            if not cand:
                return {"ok": False, "reason": "no qualifying strike on refresh"}
            clean = {k: v for k, v in cand.items() if not k.startswith("_")}
            _upsert_rows("put_selling", [clean])
            # Weights are relative to the whole book: reweight every row.
            with _lock:
                book = _state["strategies"]["put_selling"]["results"]
                s4.apply_weights(book, scheme=PS_WEIGHT)
                _state["strategies"]["put_selling"]["summary"] = s4.portfolio_summary(book)
                row = next((dict(r) for r in book if r.get("ticker") == t), clean)
                results_snap = [dict(r) for r in book]
                summary = _state["strategies"]["put_selling"]["summary"]
            return {"ok": True, "row": row, "results": results_snap, "summary": summary}

    return {"ok": False, "reason": f"unknown strategy {strategy}"}


@app.post("/refresh_signal")
def refresh_signal(body: dict):
    """
    Body: {"strategy": "vol_momentum"|"calendar"|"earnings"|"put_selling",
           "ticker": "AAPL", "exp_short": ..., "exp_long": ...}
    Sync endpoint: FastAPI runs it in its threadpool, so the blocking fetch
    never stalls the event loop (SSE keeps streaming meanwhile).
    """
    strategy = (body or {}).get("strategy")
    ticker   = ((body or {}).get("ticker") or "").strip()
    if strategy not in _state["strategies"] or not ticker:
        return JSONResponse({"ok": False, "reason": "strategy and ticker required"}, status_code=400)
    if not _enabled(strategy):
        return JSONResponse({"ok": False, "reason": "strategy disabled"}, status_code=400)
    if not _refresh_lock.acquire(blocking=False):
        return JSONResponse({"ok": False, "reason": "another refresh is running"}, status_code=409)
    t0 = time.time()
    try:
        out = _refresh_signal(strategy, ticker, body)
    except Exception as e:
        out = {"ok": False, "reason": f"{type(e).__name__}: {e}"}
    finally:
        _refresh_lock.release()
    out["elapsed_sec"] = round(time.time() - t0, 1)
    out = _sanitize(out)
    _push_event("refresh_done", strategy, {"ticker": ticker.upper(), "ok": out.get("ok")})
    print(f"[refresh] {strategy} {ticker.upper()}: ok={out.get('ok')} "
          f"in {out['elapsed_sec']}s {out.get('reason') or ''}")
    return JSONResponse(out)


# ──────────────────────────────────────────────────────────────────────────
# ON-DEMAND STRATEGY REFRESH
#
# The ⟳ on a strategy header re-runs that strategy's full pass right away.
# The background cycle pauses at its next ticker boundary (scan_control.
# strategy_priority) and resumes where it stopped once this pass ends. A
# single-signal refresh still preempts it (scan_control.priority).
# ──────────────────────────────────────────────────────────────────────────
_strategy_refresh_active: set[str] = set()
_strategy_refresh_guard = threading.Lock()


def _strategy_refresh_worker(key: str) -> None:
    t0 = time.time()
    ok = True
    try:
        with scan_control.strategy_priority():
            _push_event("strategy_refresh_started", key, {})
            _scan_pipeline(only=key)
    except BaseException as e:
        ok = False
        print(f"[strategy-refresh] {key} failed: {type(e).__name__}: {e}")
    finally:
        with _strategy_refresh_guard:
            _strategy_refresh_active.discard(key)
        el = round(time.time() - t0, 1)
        _push_event("strategy_refresh_done", key, {"ok": ok, "elapsed_sec": el})
        print(f"[strategy-refresh] {key}: ok={ok} in {el}s")


@app.post("/refresh_strategy")
def refresh_strategy(body: dict):
    """Body: {"strategy": "vol_momentum"|"calendar"|"earnings"|"put_selling"}"""
    key = (body or {}).get("strategy")
    if key not in _state["strategies"]:
        return JSONResponse({"ok": False, "reason": "unknown strategy"}, status_code=400)
    if not _enabled(key):
        return JSONResponse({"ok": False, "reason": "strategy disabled"}, status_code=400)
    with _strategy_refresh_guard:
        if key in _strategy_refresh_active:
            return JSONResponse({"ok": False, "reason": "this strategy is already refreshing"},
                                status_code=409)
        if _strategy_refresh_active:
            # One strategy refresh at a time; a second one would just interleave
            # with the first at ticker granularity and finish both later.
            busy = next(iter(_strategy_refresh_active))
            return JSONResponse({"ok": False, "reason": f"{busy} is refreshing; try again when it finishes"},
                                status_code=409)
        _strategy_refresh_active.add(key)
    threading.Thread(target=_strategy_refresh_worker, args=(key,),
                     name=f"strategy-refresh-{key}", daemon=True).start()
    return JSONResponse({"ok": True, "started": True})


@app.get("/results")
async def get_results():
    with _lock:
        import copy
        snap = copy.deepcopy(_state)
    # Tell the dashboard which sections to show. Disabled strategies stay in
    # the dict (the JS indexes it by key) but are flagged and carry no rows.
    snap["enabled_strategies"] = list(ENABLED_STRATEGIES)
    for k, s in snap.get("strategies", {}).items():
        s["enabled"] = _enabled(k)
    return JSONResponse(_sanitize(snap))


def _rss_mb() -> Optional[float]:
    """Actual resident memory, straight from the kernel — the number that
    matters for OOM, as opposed to anything Python's own accounting reports
    (which can miss memory held by C extensions, e.g. pandas/numpy buffers
    or whatever ib_insync/asyncio allocate outside Python's own heap)."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024, 1)  # kB -> MB
    except Exception:
        pass
    return None


@app.get("/debug/memory")
async def debug_memory():
    """
    Diagnostic snapshot for tracking down memory growth in a long-running
    continuous-mode process. Three independent views, since each catches a
    different kind of leak:

      - gc object counts by type: a runaway Python-level structure (a list
        or dict growing without bound) shows up as one type's count being
        absurdly large relative to how many of that thing should ever exist
        at once.
      - tracemalloc top allocations (only when MEMORY_DEBUG=true, since it
        has real overhead): points at the exact file/line responsible,
        rather than just the object type.
      - rss_mb: the actual number the kernel would OOM-kill on. Useful as a
        sanity check against the two views above — if this keeps climbing
        but neither view shows a runaway Python object, the leak is likely
        in a C extension's own memory (pandas, numpy, or ib_insync/asyncio
        internals) that neither gc nor tracemalloc can see into.
    """
    gc.collect()
    type_counts: dict[str, int] = {}
    for obj in gc.get_objects():
        t = type(obj).__name__
        type_counts[t] = type_counts.get(t, 0) + 1
    top_types = sorted(type_counts.items(), key=lambda kv: -kv[1])[:25]

    # Open file descriptors: the number that actually caused the 11-day crash
    # ("[Errno 24] Too many open files"). Linux only; a steadily rising value
    # across cycles while rss stays flat means a socket/session leak.
    try:
        import os
        open_fds: Optional[int] = len(os.listdir("/proc/self/fd"))
    except Exception:
        open_fds = None

    result = {
        "rss_mb": _rss_mb(),
        "open_fds": open_fds,
        "threads": threading.active_count(),
        "sse_clients": _sse_clients,
        "gc_total_objects": len(gc.get_objects()),
        "gc_top_object_types": [{"type": t, "count": c} for t, c in top_types],
        "sse_events_count": len(_sse_events),
    }

    try:
        import market_cache
        result["market_cache_stats"] = market_cache.stats()
    except Exception:
        pass

    try:
        import ibkr_provider
        result["ibkr_connection_status"] = ibkr_provider.connection_status()
    except Exception:
        pass

    if MEMORY_DEBUG:
        snapshot = tracemalloc.take_snapshot()
        top = snapshot.statistics("lineno")[:25]
        result["tracemalloc_top_allocations"] = [
            {"location": str(stat.traceback[0]),
             "size_mb": round(stat.size / (1024 * 1024), 3),
             "count": stat.count}
            for stat in top
        ]
    else:
        result["tracemalloc"] = ("disabled — set MEMORY_DEBUG=true and "
                                  "restart for file/line-level allocation data")

    return JSONResponse(result)


@app.get("/health")
async def health():
    """Quick liveness check — also verifies strategy modules import correctly."""
    errors = []
    for mod in ("strategy_vol_momentum", "strategy_calendar",
                "strategy_earnings", "strategy_put_selling",
                "fundamentals", "option_pricing"):
        try:
            __import__(mod)
        except BaseException as e:
            # BaseException, not Exception: a module that raises SystemExit at
            # import time would otherwise escape this handler and take down the
            # request (and, under some servers, the worker) instead of being
            # reported as a health error.
            errors.append(f"{mod}: {type(e).__name__}: {e}")
    with _lock:
        phase       = _state.get("phase", "idle")
        prefetch    = dict(_state.get("prefetch", {"status": "idle", "detail": ""}))
        running     = _state.get("running", False)
        started_at  = _state.get("started_at")
        finished_at = _state.get("finished_at")
        continuous  = {
            "enabled":       _state.get("continuous", False),
            "cycle":         _state.get("cycle", 0),
            "next_cycle_at": _state.get("next_cycle_at"),
            "stop_requested": _state.get("stop_requested", False),
            "interval_sec":  _state.get("continuous_interval_sec"),
        }
        strategies  = {
            k: {"status": v.get("status"), "error": v.get("error")}
            for k, v in _state.get("strategies", {}).items()
        }
    try:
        provider = data_provider.provider_status()
    except Exception as e:
        provider = {"error": f"{type(e).__name__}: {e}"}

    return JSONResponse({
        "ok":          len(errors) == 0,
        "phase":       phase,
        "prefetch":    prefetch,
        "running":     running,
        "started_at":  started_at,
        "finished_at": finished_at,
        "continuous":  continuous,
        "strategies":  strategies,
        "provider":    provider,
        "errors":      errors,
    })


@app.get("/provider")
def provider_info():
    """
    Market-data source diagnostics.

    Reports `configured` vs `effective` separately: with fallback enabled these
    diverge the moment TWS becomes unreachable, and that distinction is the
    difference between "using IBKR" and "silently back on Yahoo". This endpoint
    is read-only; POST /provider/probe forces a live fetch to verify setup
    without launching a full scan.
    """
    status = data_provider.provider_status()
    return JSONResponse(_sanitize({
        "configured":  status.get("configured"),
        "effective":   status.get("effective"),
        "fallback":    {
            "enabled": status.get("fallback_enabled"),
            "active":  status.get("fallback_active"),
            "reason":  status.get("fallback_reason"),
        },
        "ibkr":        status.get("ibkr"),
        "settings":    {
            "host": IBKR_HOST, "port": IBKR_PORT,
            "client_id": IBKR_CLIENT_ID,
        },
    }))


@app.post("/provider/probe")
def provider_probe():
    """
    Actively test the configured provider by fetching one quote (AAPL).
    Returns which source answered and the price, so a misconfigured TWS shows
    up immediately rather than 40 minutes into a scan.
    """
    sym = "AAPL"
    try:
        tk = data_provider.get_ticker(sym)
        fi = tk.fast_info
        px = None
        for key in ("last_price", "lastPrice"):
            try:
                v = fi[key] if isinstance(fi, dict) else getattr(fi, key, None)
            except Exception:
                v = None
            if v:
                px = float(v)
                break
        st = data_provider.provider_status()
        return JSONResponse(_sanitize({
            "ok":        px is not None,
            "symbol":    sym,
            "price":     px,
            "source":    st.get("effective"),
            "configured": st.get("configured"),
            "fallback_active": st.get("fallback_active"),
            "fallback_reason": st.get("fallback_reason"),
            "ticker_class":    type(tk).__name__,
        }))
    except BaseException as e:
        return JSONResponse(_sanitize({
            "ok": False,
            "symbol": sym,
            "error": f"{type(e).__name__}: {e}",
            "provider": data_provider.provider_status(),
        }), status_code=200)


# ── SSE client accounting ────────────────────────────────────────────────────
#
# Every /stream connection holds a socket for its whole life. The previous
# implementation was a sync generator with `while True` + time.sleep(1): it
# never checked whether the browser was still there, so every closed tab,
# laptop sleep, reconnect-after-3s and bot probe left one socket AND one
# threadpool worker pinned forever. Over 11 days that is how the process ran
# out of file descriptors ("Too many open files") and why the UI got slower
# first -- the blocking sleeps starved the threadpool that serves /results.
#
# Now: async generator (no thread pinned), disconnect detected via
# request.is_disconnected() and the generator's finally block, a hard cap on
# simultaneous streams, and a lifetime cap so even a half-open socket the OS
# never reports as closed is recycled (the browser's EventSource reconnects
# transparently and receives a fresh snapshot).
_SSE_MAX_CLIENTS   = _env_int("SSE_MAX_CLIENTS", 20)
_SSE_MAX_LIFETIME  = _env_int("SSE_MAX_LIFETIME_SEC", 3600)
_sse_clients       = 0
_sse_clients_lock  = threading.Lock()


@app.get("/stream")
async def stream_events(request: Request):
    """
    Server-Sent Events endpoint.
    The client connects once and receives all events as they are pushed.
    """
    global _sse_clients
    with _sse_clients_lock:
        if _sse_clients >= _SSE_MAX_CLIENTS:
            return JSONResponse({"error": "too many SSE clients"}, status_code=503)
        _sse_clients += 1

    async def event_generator():
        global _sse_clients
        started = time.monotonic()
        try:
            # Send current state snapshot first
            with _lock:
                snapshot = json.dumps({"kind": "snapshot", "strategy": "all",
                                       "payload": {**_sanitize(_state),
                                                   "enabled_strategies": list(ENABLED_STRATEGIES)},
                                       "ts": _now_iso("microseconds")})
            yield f"data: {snapshot}\n\n"

            last_idx = len(_sse_events)
            last_beat = time.monotonic()

            while True:
                if await request.is_disconnected():
                    break
                if time.monotonic() - started > _SSE_MAX_LIFETIME:
                    # Recycle: client reconnects and gets a fresh snapshot.
                    break

                with _sse_lock:
                    # The queue is trimmed from the front when it exceeds
                    # 10k; if that happened under us, clamp rather than
                    # replaying from a shifted index.
                    if last_idx > len(_sse_events):
                        last_idx = len(_sse_events)
                    new_events = _sse_events[last_idx:]
                    last_idx   = len(_sse_events)

                for ev in new_events:
                    yield f"data: {ev}\n\n"

                # Heartbeat every 5s keeps proxies from closing the socket
                # and makes a dead peer surface as a write error promptly.
                now = time.monotonic()
                if now - last_beat >= 5.0:
                    yield ": heartbeat\n\n"
                    last_beat = now
                await asyncio.sleep(1.0)
        finally:
            with _sse_clients_lock:
                _sse_clients = max(0, _sse_clients - 1)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":               "no-cache",
            "X-Accel-Buffering":           "no",
            "Access-Control-Allow-Origin": "*",
        },
    )


if __name__ == "__main__":
    print("\n" + "═" * 60)
    print("  Options Scanner Suite — Backend")
    print("  Open http://localhost:8000 in your browser")
    print(f"  Data provider: {DATA_PROVIDER}"
          + (f"  (TWS {IBKR_HOST}:{IBKR_PORT})" if DATA_PROVIDER == "ibkr" else ""))
    print(f"  Strategies: {', '.join(ENABLED_STRATEGIES)}"
          + ("" if ENABLED_STRATEGIES == STRATEGY_ORDER else "  (--strategies)"))
    print("═" * 60 + "\n")
    try:
        uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")
    finally:
        # Close the TWS socket and stop its event-loop thread. Without this the
        # daemon thread can leave a half-open API session that blocks the next
        # start with "client id already in use".
        data_provider.shutdown()