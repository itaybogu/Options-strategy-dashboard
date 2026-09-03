"""
Infrastructure test for the scanner lifecycle (no network, no deps).

Stubs out uvicorn/fastapi and the four strategy modules so server.py can be
imported in a bare interpreter, then exercises _run_all_strategies() under
four failure modes to prove:

  1. phase always reaches a terminal value (done | error)
  2. running is always cleared, so POST /run is never permanently blocked
  3. finished_at is always stamped
  4. a strategy calling sys.exit() does NOT kill the run or the other strategies

Run:  python test_lifecycle.py
"""

from __future__ import annotations

import sys
import types


# ── Stub third-party web deps ────────────────────────────────────────────────
def _stub(name: str, **attrs) -> types.ModuleType:
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


class _App:
    def __init__(self, *a, **k): pass
    def add_middleware(self, *a, **k): pass
    def _deco(self, *a, **k):
        def wrap(fn): return fn
        return wrap
    get = post = on_event = _deco


_stub("uvicorn", run=lambda *a, **k: None)
_stub("fastapi", FastAPI=_App)
_stub("fastapi.responses", HTMLResponse=object, StreamingResponse=object,
      JSONResponse=lambda *a, **k: None)
_stub("fastapi.staticfiles", StaticFiles=object)
_stub("fastapi.middleware", )
_stub("fastapi.middleware.cors", CORSMiddleware=object)


# ── Stub the four strategy modules  ──────────────────────────────────────────
def _make_strategy(name: str, behaviour: str):
    """behaviour: 'ok' | 'exit' | 'raise'"""
    m = types.ModuleType(name)

    # **kw so this stub also satisfies put_selling's richer signature
    # (mode/weighting/top_quintile/max_names). Without it the server's call would
    # raise TypeError and every scenario would report put_selling as 'error'
    # regardless of the behaviour under test.
    def run(on_progress=None, **kw):
        if behaviour == "exit":
            sys.exit(1)
        if behaviour == "raise":
            raise RuntimeError("simulated data-source failure")
        if on_progress:
            on_progress({"ticker": "AAPL", "status": "SCORED",
                         "_progress": {"current": 1, "total": 1}})
        return []

    m.run = run
    # put_selling only: the server calls portfolio_summary() on the returned
    # rows, so the stub must expose it or the strategy appears to fail.
    m.portfolio_summary = lambda results: {"count": len(results)}
    sys.modules[name] = m
    return m


def _install(vm: str, cal: str, earn: str, put: str) -> None:
    _make_strategy("strategy_vol_momentum", vm)
    _make_strategy("strategy_calendar", cal)
    _make_strategy("strategy_earnings", earn)
    _make_strategy("strategy_put_selling", put)


_install("ok", "ok", "ok", "ok")
import server  # noqa: E402


# ── Scenarios ────────────────────────────────────────────────────────────────
SCENARIOS = [
    #  label                      vm       cal      earn     put      phase
    ("all succeed",              "ok",    "ok",    "ok",    "ok",    "done"),
    ("calendar sys.exit(1)",     "ok",    "exit",  "ok",    "ok",    "done"),
    ("calendar raises",          "ok",    "raise", "ok",    "ok",    "done"),
    # put_selling failing alone must not taint the run: it is last in the
    # pipeline, so a regression there would otherwise go unnoticed.
    ("put_selling sys.exit(1)",  "ok",    "ok",    "ok",    "exit",  "done"),
    ("put_selling raises",       "ok",    "ok",    "ok",    "raise", "done"),
    ("all four fail",            "exit",  "raise", "exit",  "raise", "error"),
]

failures = 0
for label, vm, cal, earn, put, want_phase in SCENARIOS:
    _install(vm, cal, earn, put)
    server._run_all_strategies()

    st       = server._state
    statuses = {k: v["status"] for k, v in st["strategies"].items()}
    problems = []

    if st["phase"] != want_phase:
        problems.append(f"phase={st['phase']!r} want {want_phase!r}")
    if st["running"]:
        problems.append("running still True (POST /run would be blocked)")
    if not st["finished_at"]:
        problems.append("finished_at not set")
    if st["prefetch"]["status"] not in ("done", "error"):
        problems.append(f"prefetch={st['prefetch']['status']!r}")

    # a sys.exit strategy must be recorded as error, not silently 'idle'
    for key, beh in (("vol_momentum", vm), ("calendar", cal),
                     ("earnings", earn), ("put_selling", put)):
        want = "error" if beh in ("exit", "raise") else "done"
        if statuses[key] != want:
            problems.append(f"{key}={statuses[key]!r} want {want!r}")

    mark = "FAIL" if problems else "PASS"
    failures += bool(problems)
    print(f"[{mark}] {label:<24} phase={st['phase']:<6} {statuses}")
    for p in problems:
        print(f"         - {p}")

print()
print("ALL LIFECYCLE TESTS PASSED" if not failures else f"{failures} SCENARIO(S) FAILED")
sys.exit(1 if failures else 0)