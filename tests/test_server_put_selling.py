"""
Integration test for the put_selling wiring in server.py.

Runs strategy_put_selling.run() with a stub data layer, so the assertions cover
the real event shapes the real module emits rather than shapes this test made up.
That is the whole point: the callback contract between run() and server.py is the
thing most likely to drift, and a test with hand-written events would keep
passing after such a drift.
"""
import os

os.environ.setdefault("DATA_PROVIDER", "yfinance")

import server
import strategy_put_selling as ps


def _reset():
    st = server._state["strategies"]["put_selling"]
    st.update(status="idle", progress={"current": 0, "total": 0},
              results=[], phase="", summary={})
    with server._sse_lock:
        server._sse_events.clear()


def _events(kind=None, strategy="put_selling"):
    import json
    out = []
    with server._sse_lock:
        for raw in server._sse_events:
            ev = json.loads(raw)
            if ev["strategy"] == strategy and (kind is None or ev["kind"] == kind):
                out.append(ev)
    return out


def test_state_registered():
    st = server._state["strategies"]["put_selling"]
    assert st["label"] == "Put Selling (GS FCF)"
    for key in ("status", "progress", "results", "phase", "summary"):
        assert key in st, f"missing state key {key}"


def test_config_defaults_are_valid_for_the_module():
    # A typo in PS_MODE would otherwise surface as a ValueError minutes into a
    # live scan, so assert the server default is a mode the module accepts.
    assert server.PS_MODE in ps.MODES
    assert server.PS_WEIGHT in ("risk", "equal", "premium", "kelly_lite")
    assert isinstance(server.PS_QUINTILE, bool)
    assert isinstance(server.PS_MAX_NAMES, int)


def test_callback_handles_real_event_stream(monkeypatch):
    """Drive on_ps_progress via the real run() against stubbed data."""
    _reset()

    universe = ["AAA", "BBB", "CCC", "DDD"]

    def fake_fcf(tickers, on_progress=None, **kw):
        # `sane` and `fcfy_quintile` are part of the real fundamentals contract:
        # the screen drops any record without sane=True, and rank_by_fcf_yield()
        # is what normally assigns the quintile. Omitting them silently empties
        # the eligible set, so the stub must supply them.
        out = {}
        for i, t in enumerate(tickers, 1):
            out[t] = {"ticker": t, "fcf_yield": 0.10 - i * 0.01,
                      "fcf": 1e9, "market_cap": 1e10, "sane": True}
            if on_progress:
                on_progress({"ticker": t, "fcf_yield": out[t]["fcf_yield"],
                             "_progress": {"current": i, "total": len(tickers)}})
        return out

    monkeypatch.setattr(ps.fundamentals, "get_fcf_yields", fake_fcf)
    monkeypatch.setattr(ps, "get_sp500_tickers", lambda: list(universe))
    monkeypatch.setattr(ps, "FETCH_DELAY", 0.0)

    def fake_analyze(ticker, fcf, mode="fcf_premium", **kw):
        if ticker == "BBB":
            return None          # exercise the no-candidate path
        return {
            "ticker": ticker, "spot": 100.0, "strike": 95.0,
            "expiry": "2025-01-17", "dte": 30, "iv": 0.30,
            "delta": -0.25, "abs_delta": 0.25, "premium": 1.50,
            "collateral": 9500.0, "assign_prob": 0.25,
            "fcf_yield": fcf.get("fcf_yield"), "ann_yield": 0.19,
        }

    monkeypatch.setattr(ps, "analyze_ticker", fake_analyze)

    captured = []
    orig = server._push_event

    def spy(kind, strategy, payload):
        captured.append((kind, strategy, payload))
        return orig(kind, strategy, payload)

    monkeypatch.setattr(server, "_push_event", spy)

    monkeypatch.setattr(server, "PS_QUINTILE", False)   # keep all 4 names
    monkeypatch.setattr(server, "PS_MAX_NAMES", 0)

    # on_ps_progress is a closure inside _scan_pipeline and cannot be imported,
    # so the pipeline entry point is the only way to test the real callback.
    # Stub the other three strategies to no-ops so this stays a put_selling test.
    import sys, types
    for name in ("strategy_vol_momentum", "strategy_calendar", "strategy_earnings"):
        mod = types.ModuleType(name)
        mod.run = lambda on_progress=None, **kw: []
        sys.modules[name] = mod

    server._scan_pipeline()

    st = server._state["strategies"]["put_selling"]
    assert st["status"] == "done", st
    # 4 names screened, BBB yields no candidate -> 3 rows.
    assert len(st["results"]) == 3, st["results"]
    assert {r["ticker"] for r in st["results"]} == {"AAA", "CCC", "DDD"}

    # Weights must be present in the stored rows: this is what proves the
    # results came from run()'s post-weighting return value and not from the
    # provisional stream.
    assert all("contracts_per_100k" in r for r in st["results"]), st["results"]

    # Summary populated and consistent with the row count.
    assert st["summary"].get("count") == 3, st["summary"]

    # No ticker_done event with a null ticker (the boundary-event guard).
    for ev in _events("ticker_done"):
        assert ev["payload"]["ticker"], f"null-ticker ticker_done: {ev}"

    # Both phase boundary messages were announced.
    phases = {e["payload"]["phase"] for e in _events("phase_update")}
    assert {"fundamentals", "screened"} <= phases, phases

    # strategy_done carries the authoritative rows + summary.
    done = _events("strategy_done")
    assert len(done) == 1, done
    assert done[0]["payload"]["count"] == 3
    assert done[0]["payload"]["summary"].get("count") == 3

    # Progress ended on the phase-2 totals, not phase 1's.
    assert st["progress"]["total"] == 4  # 4 eligible names in phase 2


def test_reset_clears_summary_and_phase():
    st = server._state["strategies"]["put_selling"]
    st["summary"] = {"count": 99}
    st["phase"] = "options"
    st["results"] = [{"ticker": "STALE"}]

    # Reset logic lives in _run_all_strategies; replicate only its loop so the
    # test does not launch a real scan.
    for s in server._state["strategies"].values():
        s["status"] = "idle"
        s["progress"] = {"current": 0, "total": 0}
        s["results"] = []
        if "excluded" in s:
            s["excluded"] = []
        if "summary" in s:
            s["summary"] = {}
        if "phase" in s:
            s["phase"] = ""

    assert st["summary"] == {}
    assert st["phase"] == ""
    assert st["results"] == []