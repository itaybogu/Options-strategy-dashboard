"""
Scan priority control for on-demand refreshes.

Three tiers share the data provider, highest first:

  1. SIGNAL refresh   -- `priority()`         (the ⟳ in the payoff window)
  2. STRATEGY refresh -- `strategy_priority()` (the ⟳ on a strategy header)
  3. Background scan  -- the continuous cycle

Every ticker's data work, in any tier, runs inside `ticker_slot()`. A slot
only starts when no higher tier is waiting, so:

  * a strategy refresh pauses the background scan at its next ticker
    boundary and runs that strategy's full pass; the background cycle then
    resumes exactly where it stopped;
  * a signal refresh pauses BOTH: the strategy refresh finishes the ticker
    it is on, the signal is refreshed alone, then the strategy refresh
    carries on, and only after it ends does the background scan continue.

The tier of the calling thread is kept in a thread-local, so strategy code
calls the same `ticker_slot()` whichever thread runs it.

A plain lock is not enough: Python locks are not fair, and a scanner that
releases and immediately re-acquires between tickers could starve a refresh.
The pending counters close that gap.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager

_work = threading.Lock()        # held while ONE ticker (any tier) is fetched
_cond = threading.Condition()   # guards the counters below
_signal_pending = 0             # signal refreshes waiting for / holding _work
_strategy_active = 0            # strategy refreshes in progress

_tls = threading.local()        # .tier: 1 = strategy refresh thread, else background


def is_paused() -> bool:
    """True while the background scan is held by any refresh."""
    with _cond:
        return _signal_pending > 0 or _strategy_active > 0


def _blocked(tier: int) -> bool:
    if tier == 1:
        return _signal_pending > 0
    return _signal_pending > 0 or _strategy_active > 0


@contextmanager
def ticker_slot():
    """Wrap ONE scanner ticker. Waits while a higher-priority refresh is pending."""
    tier = getattr(_tls, "tier", 2)
    with _cond:
        while _blocked(tier):
            _cond.wait()
    _work.acquire()
    try:
        yield
    finally:
        _work.release()


@contextmanager
def priority():
    """Single-signal refresh: highest priority, pauses every scan."""
    global _signal_pending
    with _cond:
        _signal_pending += 1
    _work.acquire()
    try:
        yield
    finally:
        _work.release()
        with _cond:
            _signal_pending -= 1
            _cond.notify_all()


@contextmanager
def strategy_priority():
    """
    Mark the CURRENT thread as running a strategy refresh. The background scan
    stops taking tickers until this exits; signal refreshes still preempt it.
    """
    global _strategy_active
    with _cond:
        _strategy_active += 1
    prev = getattr(_tls, "tier", 2)
    _tls.tier = 1
    try:
        yield
    finally:
        _tls.tier = prev
        with _cond:
            _strategy_active -= 1
            _cond.notify_all()
