"""
market_cache.py — process-wide TTL cache for market data.

Why this exists
───────────────
The four strategies were written independently and each builds its own
`Ticker` object and pulls what it needs. Several of those pulls are for
*identical* data on *overlapping* universes, so a single scan cycle asked the
provider for the same bytes more than once. Measured overlap on a full S&P 500
cycle (see docs at the bottom of this file for the derivation):

  duplicate call                     duplicated by                  approx. calls
  ─────────────────────────────────  ─────────────────────────────  ─────────────
  S&P 500 constituent list           vol_momentum + calendar        2 HTTP
                                     (+ put_selling via calendar)
  30d batch OHLCV (volume filter)    vol_momentum + calendar        2 batch
  tk.history() daily bars            vm(1y) cal(2d) ps(2d) earn(3mo) up to 4/name
  tk.options expiry list             vm, cal, earn, ps              up to 4/name
  tk.option_chain(exp)               vm, cal, earn, ps              up to 4/name/exp
  tk.fast_info spot                  vm, cal, ps                    up to 3/name
  tk.info                            vm + fundamentals              up to 2/name

The chain and history calls dominate: they are the slow ones, and they are the
ones repeated per expiry. Caching them is the single highest-leverage change
for both scan latency and rate-limit pressure.

Design
──────
A wrapper, not a rewrite. `CachedTicker` implements the same duck-type the
strategies already consume (`.history()`, `.fast_info`, `.options`,
`.option_chain()`, `.info`, `.calendar`) and delegates to the real provider
ticker on a miss. Strategy code is untouched.

TTLs are per data *kind*, because staleness tolerance differs by orders of
magnitude — a quote goes stale in seconds, an expiry list in hours, a cash-flow
statement in months:

    quote / fast_info      QUOTE_TTL      default   15s
    option chains          CHAIN_TTL      default   60s
    daily history bars     HISTORY_TTL    default  900s (15m)
    expiry lists           EXPIRY_TTL     default 3600s (1h)
    .info / .calendar      INFO_TTL       default 3600s (1h)
    constituent lists      UNIVERSE_TTL   default 21600s (6h)

CRITICAL correctness note on `history`
──────────────────────────────────────
Callers request different `period` values ("1y", "3mo", "2d", "1d") and each
is cached under its own key. A 1y request is NOT served from a 2d entry, and a
2d request is deliberately NOT served by slicing the 1y entry either: the
providers apply different adjustment and session-inclusion rules per period,
so a slice is not always byte-identical to a direct fetch. Serving one from
the other would silently change strategy inputs. Dedup here is exact-match
only — same symbol, same period, same interval, same auto_adjust.

That means vol_momentum's 1y and calendar's 2d requests for the same name are
still two fetches. That is intended. The win comes from repeat calls with
identical parameters, which is where the actual duplication was.

Thread safety
─────────────
One `threading.RLock` guards the store. Values are returned as-is, NOT deep
copied — pandas DataFrames are large and copying them per access would undo
the savings. Callers must therefore treat returned frames as read-only. The
existing strategy code already does (it reads columns and builds new objects);
this constraint is documented here because violating it would corrupt the
cache for every later reader.

In-flight de-duplication: concurrent requests for the same key wait on a
per-key event rather than each firing their own fetch. This matters for the
batch `download()` path, where two strategies starting together would
otherwise both pull the same several-hundred-symbol frame.

Disable entirely with MC_ENABLED=false to A/B the cache against direct fetches.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError:
        log.warning("%s=%r is not a number; using %s", name, raw, default)
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


ENABLED      = _env_bool("MC_ENABLED", True)

QUOTE_TTL    = _env_float("MC_QUOTE_TTL",       15.0)
CHAIN_TTL    = _env_float("MC_CHAIN_TTL",       60.0)
HISTORY_TTL  = _env_float("MC_HISTORY_TTL",    900.0)
EXPIRY_TTL   = _env_float("MC_EXPIRY_TTL",    3600.0)
INFO_TTL     = _env_float("MC_INFO_TTL",      3600.0)
UNIVERSE_TTL = _env_float("MC_UNIVERSE_TTL", 21600.0)

# Hard ceiling on stored entries. Option chains are the memory hog (a chain
# DataFrame for a liquid name is ~100KB), so an unbounded cache on a 500-name
# universe with 4 expiries each would be GBs. Eviction is oldest-first by
# insertion time, which is a rough LRU that costs nothing to maintain.
MAX_ENTRIES  = int(_env_float("MC_MAX_ENTRIES", 8000))


# ─────────────────────────────────────────────────────────────────────────────
# CACHE CORE
# ─────────────────────────────────────────────────────────────────────────────

class _Entry:
    __slots__ = ("value", "expires_at", "stored_at", "is_error")

    def __init__(self, value: Any, expires_at: float, is_error: bool = False):
        self.value      = value
        self.expires_at = expires_at
        self.stored_at  = time.time()
        self.is_error   = is_error


_store: dict[str, _Entry] = {}
_inflight: dict[str, threading.Event] = {}
_lock = threading.RLock()

_stats = {
    "hits":      0,
    "misses":    0,
    "coalesced": 0,   # waited on another thread's in-flight fetch
    "errors":    0,
    "evictions": 0,
}


def stats() -> dict:
    """
    Snapshot of cache effectiveness. `saved_calls` = hits + coalesced, i.e. the
    number of provider round-trips that did NOT happen because of this module.
    Surfaced by /health so the dedup win is measurable rather than assumed.
    """
    with _lock:
        s = dict(_stats)
        s["entries"] = len(_store)
        total = s["hits"] + s["misses"]
        s["hit_rate"]    = round(s["hits"] / total, 4) if total else 0.0
        s["saved_calls"] = s["hits"] + s["coalesced"]
        return s


def reset_stats() -> None:
    with _lock:
        for k in _stats:
            _stats[k] = 0


def clear(prefix: Optional[str] = None) -> int:
    """
    Drop cached entries. With `prefix`, drops only matching keys (e.g. "chain:"
    to force fresh option data without discarding the universe list). Returns
    the number removed.
    """
    with _lock:
        if prefix is None:
            n = len(_store)
            _store.clear()
            return n
        doomed = [k for k in _store if k.startswith(prefix)]
        for k in doomed:
            del _store[k]
        return len(doomed)


def _evict_if_needed() -> None:
    """Caller must hold _lock."""
    if len(_store) <= MAX_ENTRIES:
        return
    # Drop expired first — free, and usually enough.
    now = time.time()
    for k in [k for k, e in _store.items() if e.expires_at <= now]:
        del _store[k]
        _stats["evictions"] += 1
    if len(_store) <= MAX_ENTRIES:
        return
    # Still over: drop oldest by insertion until back under the ceiling.
    overflow = len(_store) - MAX_ENTRIES
    for k, _ in sorted(_store.items(), key=lambda kv: kv[1].stored_at)[:overflow]:
        del _store[k]
        _stats["evictions"] += 1


def get_or_fetch(key: str, ttl: float, fetch: Callable[[], Any],
                 cache_errors: bool = True) -> Any:
    """
    Return cached value for `key`, else call `fetch()` and cache it.

    Concurrency: the first caller for a key marks it in-flight; later callers
    for the same key block on that event instead of duplicating the fetch, then
    read the result. Without this, N strategy threads starting simultaneously
    would each miss and each fetch.

    Error handling: exceptions are cached too, for a deliberately short window
    (min(ttl, 30s)) and re-raised. A dead symbol on a 500-name universe would
    otherwise be retried by every strategy on every cycle, turning one bad
    ticker into a sustained stream of failing requests. The window is kept
    short so a transient outage recovers quickly. Set cache_errors=False for
    calls where a retry is cheap and staleness is unacceptable.
    """
    if not ENABLED:
        return fetch()

    now = time.time()

    while True:
        waiter: Optional[threading.Event] = None
        with _lock:
            entry = _store.get(key)
            if entry is not None and entry.expires_at > now:
                _stats["hits"] += 1
                if entry.is_error:
                    raise entry.value
                return entry.value

            ev = _inflight.get(key)
            if ev is None:
                # We own this fetch.
                _stats["misses"] += 1
                _inflight[key] = threading.Event()
                break
            # Someone else is fetching; wait for them.
            waiter = ev
            _stats["coalesced"] += 1

        # Bounded wait: if the owning thread dies without clearing the flag we
        # must not block forever. On timeout we loop and re-evaluate, which may
        # promote us to owner.
        waiter.wait(timeout=120.0)
        now = time.time()

    # ── We are the fetching thread ────────────────────────────────────────────
    try:
        value = fetch()
    except BaseException as e:
        with _lock:
            if cache_errors:
                _store[key] = _Entry(e, time.time() + min(ttl, 30.0), is_error=True)
            _stats["errors"] += 1
            ev = _inflight.pop(key, None)
        if ev is not None:
            ev.set()
        raise
    else:
        with _lock:
            _store[key] = _Entry(value, time.time() + ttl)
            _evict_if_needed()
            ev = _inflight.pop(key, None)
        if ev is not None:
            ev.set()
        return value


# ─────────────────────────────────────────────────────────────────────────────
# CACHED TICKER WRAPPER
# ─────────────────────────────────────────────────────────────────────────────

class CachedTicker:
    """
    Drop-in stand-in for a provider Ticker, caching each accessor separately.

    The underlying ticker object is created lazily. That matters: constructing
    an IBKRTicker can trigger a TWS connection and contract resolution, so a
    cache hit on `.options` should not pay that cost. `_real()` is only called
    when something actually misses.

    Only the surface the strategies use is wrapped. `__getattr__` forwards
    anything else straight through uncached, so this cannot break a call path
    that was not anticipated here — it just will not accelerate it.
    """

    __slots__ = ("symbol", "_session", "_factory", "_tk", "_tk_lock", "_scope")

    def __init__(self, symbol: str, factory: Callable[[], Any], session: Any = None,
                 scope: str = ""):
        self.symbol   = (symbol or "").upper()
        self._session = session
        self._factory = factory
        self._tk      = None
        self._tk_lock = threading.Lock()
        # Cache-key namespace identifying which backend produced the data.
        # Captured at construction because that is when the provider was
        # resolved; this object's bytes belong to that provider for its whole
        # life, even if the global provider changes afterwards.
        self._scope   = scope

    def _k(self, suffix: str) -> str:
        """
        Namespace a cache key by provider.

        Without this, `quote:AAPL` written while IBKR was active would be
        served verbatim to a yfinance caller after a switch or a fallback.
        The alternative (flushing the whole cache on every switch) also throws
        away entries that are still perfectly valid, so scoping is preferred.
        """
        return f"{self._scope}:{suffix}" if self._scope else suffix

    def _real(self):
        # Double-checked locking: two strategies hitting the same cold symbol
        # concurrently must not build two provider tickers (and, for IBKR, two
        # contract resolutions).
        if self._tk is None:
            with self._tk_lock:
                if self._tk is None:
                    self._tk = self._factory()
        return self._tk

    # ── history ──────────────────────────────────────────────────────────────
    def history(self, period: str = "1mo", interval: str = "1d",
                auto_adjust: bool = True, **kwargs):
        """
        Cached OHLCV. Keyed on every parameter that changes the result, so a
        "2d" request never receives a "1y" frame. Unrecognised kwargs are part
        of the key too — if a caller passes something we do not model, it gets
        its own cache slot rather than silently sharing one.
        """
        extra = "" if not kwargs else "|" + ",".join(
            f"{k}={kwargs[k]!r}" for k in sorted(kwargs))
        key = self._k(f"hist:{self.symbol}:{period}:{interval}:{int(bool(auto_adjust))}{extra}")
        return get_or_fetch(
            key, HISTORY_TTL,
            lambda: self._real().history(period=period, interval=interval,
                                         auto_adjust=auto_adjust, **kwargs),
        )

    # ── quote ────────────────────────────────────────────────────────────────
    @property
    def fast_info(self):
        """
        Short TTL because this is the live quote. 15s is long enough to absorb
        three strategies asking within the same cycle, short enough that a
        price used for strike selection is never meaningfully stale.

        NOTE: callers access this inconsistently — vol_momentum uses
        `.get("lastPrice")` (dict style) while calendar and put_selling use
        `getattr(fi, "last_price")` (attribute style). Whatever the provider
        returns is cached and passed through untouched, so both styles keep
        working exactly as before.
        """
        return get_or_fetch(self._k(f"quote:{self.symbol}"), QUOTE_TTL,
                            lambda: self._real().fast_info)

    # ── expiries ─────────────────────────────────────────────────────────────
    @property
    def options(self):
        return get_or_fetch(self._k(f"exp:{self.symbol}"), EXPIRY_TTL,
                            lambda: self._real().options)

    # ── option chain ─────────────────────────────────────────────────────────
    def option_chain(self, date: str = None):
        """
        The highest-value entry in this cache. All four strategies pull chains,
        and several pull the *same* expiry for the same name within one cycle.
        """
        return get_or_fetch(self._k(f"chain:{self.symbol}:{date}"), CHAIN_TTL,
                            lambda: self._real().option_chain(date))

    # ── slow metadata ────────────────────────────────────────────────────────
    @property
    def info(self):
        return get_or_fetch(self._k(f"info:{self.symbol}"), INFO_TTL,
                            lambda: self._real().info)

    @property
    def calendar(self):
        return get_or_fetch(self._k(f"cal:{self.symbol}"), INFO_TTL,
                            lambda: self._real().calendar)

    # ── fundamentals statements ──────────────────────────────────────────────
    # Used by fundamentals.py for the put-selling FCF screen. These are the
    # slowest calls in the whole system and change quarterly, so they get the
    # long INFO_TTL and benefit most from surviving across scan cycles.
    @property
    def quarterly_cashflow(self):
        return get_or_fetch(self._k(f"qcf:{self.symbol}"), INFO_TTL,
                            lambda: self._real().quarterly_cashflow)

    @property
    def cashflow(self):
        return get_or_fetch(self._k(f"cf:{self.symbol}"), INFO_TTL,
                            lambda: self._real().cashflow)

    @property
    def balance_sheet(self):
        return get_or_fetch(self._k(f"bs:{self.symbol}"), INFO_TTL,
                            lambda: self._real().balance_sheet)

    @property
    def quarterly_balance_sheet(self):
        return get_or_fetch(self._k(f"qbs:{self.symbol}"), INFO_TTL,
                            lambda: self._real().quarterly_balance_sheet)

    # ── passthrough ──────────────────────────────────────────────────────────
    def unwrap(self):
        """
        The underlying provider ticker.

        Escape hatch for code that must know the concrete provider type
        (isinstance checks, IBKR-specific calls). Prefer the cached accessors
        above for normal data access — going around them bypasses the cache.
        """
        return self._real()

    def __getattr__(self, name):
        # Reached only for attributes not defined above. Deliberately uncached.
        return getattr(self._real(), name)

    def __repr__(self):
        return f"<CachedTicker {self.symbol}>"
