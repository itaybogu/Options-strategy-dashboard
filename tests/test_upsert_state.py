"""
State-management tests for continuous scanning (Task 4).

Deliberately does NOT touch the strategy modules or any market-data source.
The thing under test is server.py's bookkeeping -- row identity, timestamps,
cycle counting, pruning -- and wiring in real strategies would make the test
slow, network-dependent, and unable to control which rows appear on which
cycle. _scan_pipeline is monkeypatched with a scripted fake instead.

Run:  DATA_PROVIDER=yfinance python test_upsert_state.py
"""

from __future__ import annotations

import os
os.environ.setdefault("DATA_PROVIDER", "yfinance")

import time
import server


PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
_failures = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _failures
    print(f"  [{PASS if cond else FAIL}] {name}" + (f"  -- {detail}" if detail and not cond else ""))
    if not cond:
        _failures += 1


def reset_state() -> None:
    """Return the module state to a clean slate between tests."""
    with server._lock:
        server._state["cycle"]      = 0
        server._state["continuous"] = False
        server._state["running"]    = False
        for s in server._state["strategies"].values():
            s["results"] = []
            if "excluded" in s:
                s["excluded"] = []
    server._stop_event.clear()


def rows(strategy: str) -> list[dict]:
    with server._lock:
        return list(server._state["strategies"][strategy]["results"])


# ──────────────────────────────────────────────────────────────────────────────
# 1. Upsert replaces by identity instead of appending
# ──────────────────────────────────────────────────────────────────────────────

def test_upsert_replaces_by_ticker() -> None:
    print("\nupsert: one row per ticker (vol_momentum)")
    reset_state()

    server._upsert_rows("vol_momentum", [
        {"ticker": "AAPL", "score": 1.0},
        {"ticker": "MSFT", "score": 2.0},
    ])
    check("first cycle inserts both", len(rows("vol_momentum")) == 2)

    # Same names again, new scores -- the table must not grow.
    server._upsert_rows("vol_momentum", [
        {"ticker": "AAPL", "score": 9.9},
        {"ticker": "MSFT", "score": 8.8},
    ])
    r = rows("vol_momentum")
    check("re-scan does not duplicate", len(r) == 2, f"got {len(r)} rows")

    by_ticker = {x["ticker"]: x for x in r}
    check("value replaced, not stale", by_ticker["AAPL"]["score"] == 9.9)

    # New name arrives mid-session.
    server._upsert_rows("vol_momentum", [{"ticker": "NVDA", "score": 3.0}])
    check("new ticker appends", len(rows("vol_momentum")) == 3)


def test_upsert_preserves_order() -> None:
    """
    Replacement must happen in place. If a refreshed row jumped to the end of
    the list the dashboard would reshuffle under the user's cursor every cycle.
    """
    print("\nupsert: stable row ordering")
    reset_state()

    server._upsert_rows("vol_momentum", [
        {"ticker": "AAA"}, {"ticker": "BBB"}, {"ticker": "CCC"},
    ])
    server._upsert_rows("vol_momentum", [{"ticker": "AAA", "score": 5}])

    order = [r["ticker"] for r in rows("vol_momentum")]
    check("refreshed row keeps position", order == ["AAA", "BBB", "CCC"], f"got {order}")


# ──────────────────────────────────────────────────────────────────────────────
# 2. Calendar: composite identity
# ──────────────────────────────────────────────────────────────────────────────

def test_calendar_composite_identity() -> None:
    """
    Calendar legitimately emits several rows per ticker (different expiry
    pairs). Keying on ticker alone would collapse them to one -- this is the
    regression that motivated _row_identity.
    """
    print("\nupsert: composite identity (calendar)")
    reset_state()

    server._upsert_rows("calendar", [
        {"ticker": "SPY", "exp_short": "2024-07-19", "exp_long": "2024-08-16", "edge": 1.0},
        {"ticker": "SPY", "exp_short": "2024-08-16", "exp_long": "2024-09-20", "edge": 2.0},
    ])
    check("two expiry pairs coexist for one ticker", len(rows("calendar")) == 2,
          f"got {len(rows('calendar'))}")

    # Refresh only the first pair.
    server._upsert_rows("calendar", [
        {"ticker": "SPY", "exp_short": "2024-07-19", "exp_long": "2024-08-16", "edge": 7.0},
    ])
    r = rows("calendar")
    check("refresh hits only matching pair", len(r) == 2, f"got {len(r)}")

    edges = {(x["exp_short"], x["exp_long"]): x["edge"] for x in r}
    check("targeted pair updated", edges[("2024-07-19", "2024-08-16")] == 7.0)
    check("other pair untouched",  edges[("2024-08-16", "2024-09-20")] == 2.0)


# ──────────────────────────────────────────────────────────────────────────────
# 3. Timestamps
# ──────────────────────────────────────────────────────────────────────────────

def test_rows_are_stamped() -> None:
    print("\ntimestamps: as_of and cycle on every row")
    reset_state()

    with server._lock:
        server._state["cycle"] = 4

    server._upsert_rows("earnings", [{"ticker": "TSLA"}])
    row = rows("earnings")[0]

    check("as_of present", "as_of" in row)
    check("cycle stamped from state", row.get("cycle") == 4, f"got {row.get('cycle')}")

    # ISO8601 that round-trips -- the dashboard does Date.parse on this.
    import datetime as _dt
    try:
        _dt.datetime.fromisoformat(row["as_of"])
        parsed = True
    except (ValueError, TypeError):
        parsed = False
    check("as_of parses as ISO8601", parsed, repr(row.get("as_of")))


def test_stamp_refreshes_on_reupsert() -> None:
    """A row that survives into a new cycle must carry the NEW timestamp."""
    print("\ntimestamps: refreshed on re-upsert")
    reset_state()

    with server._lock:
        server._state["cycle"] = 1
    server._upsert_rows("earnings", [{"ticker": "TSLA"}])
    first = rows("earnings")[0]["as_of"]

    time.sleep(1.05)   # as_of has second resolution
    with server._lock:
        server._state["cycle"] = 2
    server._upsert_rows("earnings", [{"ticker": "TSLA"}])
    second_row = rows("earnings")[0]

    check("as_of advanced", second_row["as_of"] > first,
          f"{first} -> {second_row['as_of']}")
    check("cycle advanced", second_row["cycle"] == 2)


# ──────────────────────────────────────────────────────────────────────────────
# 4. Explicit drop
# ──────────────────────────────────────────────────────────────────────────────

def test_drop_row() -> None:
    print("\ndrop: rejected name removed immediately")
    reset_state()

    server._upsert_rows("vol_momentum", [
        {"ticker": "AAPL"}, {"ticker": "MSFT"},
    ])
    server._drop_row("vol_momentum", "AAPL")

    remaining = [r["ticker"] for r in rows("vol_momentum")]
    check("dropped ticker gone", "AAPL" not in remaining)
    check("sibling untouched", remaining == ["MSFT"], f"got {remaining}")

    # Must be a no-op, not a crash, for names that were never in the table.
    server._drop_row("vol_momentum", "NOSUCH")
    server._drop_row("vol_momentum", None)
    check("unknown ticker is a no-op", [r["ticker"] for r in rows("vol_momentum")] == ["MSFT"])


def test_drop_removes_all_rows_for_ticker() -> None:
    """For calendar, dropping a rejected name must clear all its expiry pairs."""
    print("\ndrop: clears every row for a ticker (calendar)")
    reset_state()

    server._upsert_rows("calendar", [
        {"ticker": "SPY", "exp_short": "A", "exp_long": "B"},
        {"ticker": "SPY", "exp_short": "C", "exp_long": "D"},
        {"ticker": "QQQ", "exp_short": "A", "exp_long": "B"},
    ])
    server._drop_row("calendar", "SPY")

    left = [r["ticker"] for r in rows("calendar")]
    check("all SPY pairs removed", left == ["QQQ"], f"got {left}")


# ──────────────────────────────────────────────────────────────────────────────
# 5. Pruning
# ──────────────────────────────────────────────────────────────────────────────

def test_prune_keeps_recent_drops_old() -> None:
    """
    Rows survive one missed cycle (transient data failure shouldn't make a good
    candidate flicker out) but are dropped once they fall keep_cycles behind.
    """
    print("\nprune: ages out rows that stop qualifying")
    reset_state()

    with server._lock:
        server._state["cycle"] = 1
    server._upsert_rows("earnings", [{"ticker": "OLD"}, {"ticker": "FRESH"}])

    # Cycle 2: only FRESH re-qualifies.
    with server._lock:
        server._state["cycle"] = 2
    server._upsert_rows("earnings", [{"ticker": "FRESH"}])
    server._prune_stale("earnings")

    left = sorted(r["ticker"] for r in rows("earnings"))
    check("one-cycle-old row survives", left == ["FRESH", "OLD"], f"got {left}")

    # Cycle 3: OLD is now 2 cycles behind -> pruned.
    with server._lock:
        server._state["cycle"] = 3
    server._upsert_rows("earnings", [{"ticker": "FRESH"}])
    server._prune_stale("earnings")

    left = [r["ticker"] for r in rows("earnings")]
    check("two-cycle-old row pruned", left == ["FRESH"], f"got {left}")


def test_prune_noop_on_first_cycle() -> None:
    """
    Everything written during cycle N is by definition current at cycle N.
    A prune right after the first upsert must not empty the table.
    """
    print("\nprune: no-op when everything is current")
    reset_state()

    with server._lock:
        server._state["cycle"] = 0
    server._upsert_rows("put_selling", [{"ticker": "KO"}, {"ticker": "PG"}])
    server._prune_stale("put_selling")

    check("fresh rows retained", len(rows("put_selling")) == 2,
          f"got {len(rows('put_selling'))}")


# ──────────────────────────────────────────────────────────────────────────────
# 6. Cycle lifecycle through _run_all_strategies
# ──────────────────────────────────────────────────────────────────────────────

def test_cycle_counter_and_reset_semantics() -> None:
    """
    Drive the real _run_all_strategies with a scripted fake pipeline. This is
    the integration point that matters: cycle must increment per pass, reset
    must only wipe on the first cycle, and rows must accumulate/refresh after.
    """
    print("\ncycle: counter increments, reset only on first pass")
    reset_state()

    emitted = [
        [{"ticker": "AAPL", "score": 1}],                       # cycle 1
        [{"ticker": "AAPL", "score": 2}, {"ticker": "MSFT"}],   # cycle 2
    ]
    calls = {"n": 0}

    def fake_pipeline() -> None:
        batch = emitted[min(calls["n"], len(emitted) - 1)]
        calls["n"] += 1
        server._upsert_rows("vol_momentum", [dict(r) for r in batch])
        server._update_strategy("vol_momentum", status="done")

    real = server._scan_pipeline
    server._scan_pipeline = fake_pipeline
    try:
        server._run_all_strategies(reset=True)
        check("cycle == 1 after first pass", server._state["cycle"] == 1,
              f"got {server._state['cycle']}")
        check("first pass rows", len(rows("vol_momentum")) == 1)

        server._run_all_strategies(reset=False)
        check("cycle == 2 after second pass", server._state["cycle"] == 2,
              f"got {server._state['cycle']}")

        r = {x["ticker"]: x for x in rows("vol_momentum")}
        check("no duplicates across cycles", len(rows("vol_momentum")) == 2,
              f"got {len(rows('vol_momentum'))}")
        check("existing row refreshed in place", r["AAPL"]["score"] == 2)
        check("new row added", "MSFT" in r)
        check("running cleared at end", server._state["running"] is False)
        check("finished_at recorded", bool(server._state.get("finished_at")))
        check("cycle_finished_at recorded", bool(server._state.get("cycle_finished_at")))
    finally:
        server._scan_pipeline = real


def test_reset_clears_previous_session() -> None:
    """reset=True is what a fresh one-shot /run does -- old rows must go."""
    print("\ncycle: reset=True wipes the previous session")
    reset_state()

    server._upsert_rows("vol_momentum", [{"ticker": "STALE"}])

    def fake_pipeline() -> None:
        server._upsert_rows("vol_momentum", [{"ticker": "NEW"}])
        server._update_strategy("vol_momentum", status="done")

    real = server._scan_pipeline
    server._scan_pipeline = fake_pipeline
    try:
        server._run_all_strategies(reset=True)
        left = [r["ticker"] for r in rows("vol_momentum")]
        check("previous rows cleared", left == ["NEW"], f"got {left}")
    finally:
        server._scan_pipeline = real


def test_failing_cycle_does_not_corrupt_state() -> None:
    """
    A strategy blowing up must still leave running=False and a usable table,
    otherwise the loop would wedge with the UI stuck on "scanning".
    """
    print("\ncycle: exception leaves state consistent")
    reset_state()

    server._upsert_rows("vol_momentum", [{"ticker": "KEEP"}])

    def exploding_pipeline() -> None:
        raise RuntimeError("simulated provider outage")

    real = server._scan_pipeline
    server._scan_pipeline = exploding_pipeline
    try:
        server._run_all_strategies(reset=False)
    except BaseException as e:
        check("exception contained by _run_all_strategies", False, repr(e))
    finally:
        server._scan_pipeline = real

    check("running cleared after failure", server._state["running"] is False)
    check("prior rows still visible", [r["ticker"] for r in rows("vol_momentum")] == ["KEEP"])
    check("phase reports error", server._state.get("phase") == "error",
          f"got {server._state.get('phase')}")


# ──────────────────────────────────────────────────────────────────────────────
# 7. Continuous loop
# ──────────────────────────────────────────────────────────────────────────────

def test_continuous_loop_runs_and_stops() -> None:
    """
    Exercise the real loop thread with a 1s interval and a trivial pipeline.
    The interval floor lives in the /run/continuous endpoint, not the loop, so
    calling _continuous_loop directly lets the test finish in a couple seconds.
    """
    print("\ncontinuous: loops, upserts, and stops on request")
    reset_state()

    import threading

    def fake_pipeline() -> None:
        server._upsert_rows("vol_momentum", [{"ticker": "LOOP"}])
        server._update_strategy("vol_momentum", status="done")

    real = server._scan_pipeline
    server._scan_pipeline = fake_pipeline
    with server._lock:
        server._state["continuous"] = True
    try:
        t = threading.Thread(target=server._continuous_loop, args=(1,), daemon=True)
        t.start()

        # Long enough for at least two cycles at a 1s gap.
        time.sleep(2.6)
        cycles_ran = server._state["cycle"]
        check("more than one cycle ran", cycles_ran >= 2, f"cycle={cycles_ran}")
        check("repeated cycles did not duplicate rows",
              len(rows("vol_momentum")) == 1, f"got {len(rows('vol_momentum'))}")

        server._stop_event.set()
        t.join(timeout=5)
        check("loop thread exited", not t.is_alive())
        check("continuous flag cleared", server._state["continuous"] is False)
        check("running flag cleared", server._state["running"] is False)
        check("next_cycle_at cleared", server._state.get("next_cycle_at") is None)
    finally:
        server._scan_pipeline = real
        server._stop_event.set()


def test_stop_interrupts_sleep_promptly() -> None:
    """
    The inter-cycle wait uses Event.wait, not time.sleep, specifically so a stop
    lands immediately. With a 60s interval a time.sleep implementation would
    keep the thread alive well past the join timeout here.
    """
    print("\ncontinuous: stop cuts the inter-cycle sleep short")
    reset_state()

    import threading

    def fake_pipeline() -> None:
        server._update_strategy("vol_momentum", status="done")

    real = server._scan_pipeline
    server._scan_pipeline = fake_pipeline
    with server._lock:
        server._state["continuous"] = True
    try:
        t = threading.Thread(target=server._continuous_loop, args=(60,), daemon=True)
        t.start()
        time.sleep(1.0)          # let the first cycle finish and enter the wait

        t0 = time.time()
        server._stop_event.set()
        t.join(timeout=5)
        elapsed = time.time() - t0

        check("thread exited", not t.is_alive())
        check("exit was prompt, not after 60s", elapsed < 2.0, f"took {elapsed:.1f}s")
    finally:
        server._scan_pipeline = real
        server._stop_event.set()


# ──────────────────────────────────────────────────────────────────────────────
# 8. Serialisation
# ──────────────────────────────────────────────────────────────────────────────

def test_results_payload_is_json_serialisable() -> None:
    """
    /results runs _sanitize over the state. Rows carry numpy scalars and NaNs
    from pandas in real runs; the endpoint must never emit invalid JSON.
    """
    print("\nserialisation: /results payload survives json.dumps")
    reset_state()

    import copy, json, math
    server._upsert_rows("vol_momentum", [
        {"ticker": "AAPL", "score": float("nan"), "iv": 0.31},
    ])

    with server._lock:
        payload = server._sanitize(copy.deepcopy(server._state))

    try:
        text = json.dumps(payload, allow_nan=False)
        ok = True
    except (ValueError, TypeError) as e:
        text, ok = str(e), False
    check("strict json.dumps succeeds (NaN scrubbed)", ok, text[:120])

    if ok:
        back = json.loads(text)
        row = back["strategies"]["vol_momentum"]["results"][0]
        check("as_of survives serialisation", "as_of" in row)
        check("NaN became null", row["score"] is None, f"got {row['score']!r}")


# ──────────────────────────────────────────────────────────────────────────────

def main() -> int:
    print("=" * 64)
    print("  Continuous-scan state management — Task 4")
    print("=" * 64)

    for fn in (
        test_upsert_replaces_by_ticker,
        test_upsert_preserves_order,
        test_calendar_composite_identity,
        test_rows_are_stamped,
        test_stamp_refreshes_on_reupsert,
        test_drop_row,
        test_drop_removes_all_rows_for_ticker,
        test_prune_keeps_recent_drops_old,
        test_prune_noop_on_first_cycle,
        test_cycle_counter_and_reset_semantics,
        test_reset_clears_previous_session,
        test_failing_cycle_does_not_corrupt_state,
        test_continuous_loop_runs_and_stops,
        test_stop_interrupts_sleep_promptly,
        test_results_payload_is_json_serialisable,
    ):
        try:
            fn()
        except BaseException as e:
            check(f"{fn.__name__} raised", False, f"{type(e).__name__}: {e}")

    print("\n" + "=" * 64)
    if _failures:
        print(f"  {_failures} check(s) FAILED")
    else:
        print("  all checks passed")
    print("=" * 64)
    return 1 if _failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
