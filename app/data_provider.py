"""
data_provider.py — pluggable market-data layer.

Why this exists
───────────────
Every strategy module previously called yfinance directly. yfinance is
rate-limited, frequently returns stale `lastPrice`-derived data, and has no
real quote stream. This module introduces a provider abstraction so the same
strategy code can be driven by Interactive Brokers TWS instead.

Design constraint
─────────────────
The strategy modules consume a very specific duck-type — `yf.Ticker` — using:

    tk.history(period=..., auto_adjust=...) -> DataFrame[Open,High,Low,Close,Volume]
    tk.fast_info                            -> obj/dict with last_price / lastPrice
    tk.options                              -> tuple[str] of "YYYY-MM-DD"
    tk.option_chain(exp_str)                -> obj with .calls / .puts DataFrames
    tk.calendar                             -> earnings calendar (dict or DataFrame)
    tk.info                                 -> dict (sector, fiftyTwoWeekHigh)

Rather than rewrite all four modules, every provider returns an object
implementing that same interface. Swapping providers is then a one-line
change and the scoring code is untouched.

Option-chain DataFrames MUST carry these columns, because option_pricing
depends on them:
    strike, bid, ask, lastPrice, impliedVolatility, volume, openInterest

Selection
─────────
    set_provider("ibkr")   # default — TWS on 127.0.0.1:7497
    set_provider("yfinance")
    get_ticker("AAPL")     # provider-appropriate Ticker-like object

IBKR is the default. If TWS is unreachable and IBKR_FALLBACK_TO_YFINANCE is
true (default), the layer degrades to yfinance rather than failing the scan —
the old code paths are retained, not deleted, exactly as requested.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Optional

log = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────────────
# Env vars let you override without editing code; server.py sets these
# explicitly at startup so the dashboard and scan agree on one source.
def _env_int(name: str, default: int) -> int:
    """
    Tolerant int parse. A malformed value must not raise here: this module is
    imported at server start-up, so an exception would abort the whole process
    before any error handling exists to report it.
    """
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        log.warning("%s=%r is not an integer; using %d", name, raw, default)
        return default


DEFAULT_PROVIDER = os.getenv("DATA_PROVIDER", "ibkr").strip().lower()

IBKR_HOST      = os.getenv("IBKR_HOST", "127.0.0.1") or "127.0.0.1"
IBKR_PORT      = _env_int("IBKR_PORT", 7497)      # 7497 paper / 7496 live
IBKR_CLIENT_ID = _env_int("IBKR_CLIENT_ID", 17)

# When True, an IBKR failure silently falls back to yfinance for that call.
IBKR_FALLBACK_TO_YFINANCE = os.getenv(
    "IBKR_FALLBACK_TO_YFINANCE", "true"
).strip().lower() in ("1", "true", "yes")

_VALID_PROVIDERS = ("ibkr", "yfinance")

_state_lock = threading.Lock()
_active_provider: str = DEFAULT_PROVIDER
_fallback_active: bool = False       # True once we've degraded to yfinance
_fallback_reason: str = ""


# ── Provider selection ───────────────────────────────────────────────────────
def set_provider(name: str) -> str:
    """
    Select the active provider. Returns the name actually set.
    Unknown names fall back to 'ibkr' with a warning rather than raising,
    so a typo in config can never hard-stop the server.
    """
    global _active_provider, _fallback_active, _fallback_reason
    n = (name or "").strip().lower()
    if n not in _VALID_PROVIDERS:
        log.warning("Unknown provider %r; valid: %s. Defaulting to 'ibkr'.",
                    name, ", ".join(_VALID_PROVIDERS))
        n = "ibkr"
    with _state_lock:
        changed = (n != _active_provider) or _fallback_active
        _active_provider = n
        _fallback_active = False
        _fallback_reason = ""

    # No cache flush on switch, deliberately.
    #
    # Every cache key is provider-scoped: CachedTicker prefixes its keys via
    # _k() with the scope captured at construction, and the batch download key
    # embeds _cache_scope(). A post-switch caller therefore reads from a
    # different keyspace than the pre-switch data and cannot be served it.
    #
    # An earlier revision called market_cache.clear() here. It was correct but
    # far too blunt: switching to yfinance and back discarded every still-valid
    # entry (universe lists, quarterly fundamentals on a 1h TTL), and it also
    # dropped the memoised ticker objects, so each switch forced a full rebuild
    # of state that had not gone stale. Scoping the keys solves the staleness
    # problem without paying that cost.
    if changed:
        log.debug("Provider changed to %r; cache retained (keys are scoped)", n)

    log.info("Data provider set to %r", n)
    return n


def get_provider() -> str:
    with _state_lock:
        return _active_provider


def _cache_scope() -> str:
    """
    Cache-key namespace for the provider actually serving data right now.

    Includes the fallback state, not just the configured provider: while
    degraded, `configured` is still "ibkr" but the bytes are yfinance's. Keying
    on the configured name alone would let pre-fallback IBKR entries be served
    to post-fallback callers.
    """
    with _state_lock:
        if _active_provider == "ibkr" and _fallback_active:
            return "yfinance"
        return _active_provider


def _mark_fallback(reason: str) -> None:
    global _fallback_active, _fallback_reason
    with _state_lock:
        if not _fallback_active:
            log.warning("Falling back to yfinance: %s", reason)
        _fallback_active = True
        _fallback_reason = reason


def provider_status() -> dict:
    """Diagnostic snapshot for /health and the dashboard."""
    with _state_lock:
        active   = _active_provider
        fb       = _fallback_active
        fb_why   = _fallback_reason

    effective = "yfinance" if (active == "ibkr" and fb) else active
    status: dict[str, Any] = {
        "configured":       active,
        "effective":        effective,
        "fallback_active":  fb,
        "fallback_reason":  fb_why,
        "fallback_enabled": IBKR_FALLBACK_TO_YFINANCE,
    }
    if active == "ibkr":
        status["ibkr"] = _ibkr_status()
    return status


def _ibkr_status() -> dict:
    info: dict[str, Any] = {
        "host": IBKR_HOST, "port": IBKR_PORT, "client_id": IBKR_CLIENT_ID,
    }
    try:
        import ibkr_provider
        info.update(ibkr_provider.connection_status())
    except Exception as e:
        info["connected"] = False
        info["error"] = f"{type(e).__name__}: {e}"
    return info


# ── Ticker factory ───────────────────────────────────────────────────────────
def get_ticker(symbol: str, session: Any = None):
    """
    Return a yf.Ticker-compatible object from the active provider.

    `session` is accepted for signature compatibility with strategy_earnings,
    which passes a custom requests.Session for UA rotation. It is only
    meaningful for yfinance and is ignored by IBKR.

    Caching: the returned object is a market_cache.CachedTicker wrapping the
    real provider ticker, so repeated identical requests across strategies
    within one scan cycle hit the process cache instead of the network. The
    wrapper is transparent — it exposes the same duck-type — so strategy code
    is unchanged. Use `.unwrap()` to reach the underlying provider object.

    Eager vs lazy — the two halves are deliberately different:

      * PROVIDER SELECTION is eager. Constructing an IBKRTicker is what
        actually contacts TWS, so it is also what fails. Deferring it into the
        wrapper's factory broke two contracts: fallback to yfinance went
        unrecorded in provider_status() until something happened to touch the
        ticker, and with fallback disabled the error surfaced at some later
        attribute access instead of at acquisition. Callers must learn at call
        time which provider they got.

      * YFINANCE OBJECT CONSTRUCTION stays lazy. yf.Ticker() is a cheap local
        constructor that performs no I/O and cannot fail, so there is nothing
        to report eagerly. Deferring it means a caller that only ever hits
        cached accessors never builds one at all, and the build is memoised
        per symbol so repeated get_ticker() calls across strategies share a
        single underlying object.
    """
    real = None          # set only when the provider was resolved eagerly
    factory = None       # set only when construction is deferred

    if get_provider() == "ibkr":
        try:
            import ibkr_provider
            # Eager: this is the call that can fail, and the caller must see it.
            real = ibkr_provider.IBKRTicker(
                symbol, host=IBKR_HOST, port=IBKR_PORT, client_id=IBKR_CLIENT_ID
            )
        except Exception as e:
            if not IBKR_FALLBACK_TO_YFINANCE:
                raise
            # Record the degrade now, at call time, even though the
            # replacement yfinance object is built lazily below.
            _mark_fallback(f"get_ticker({symbol}): {type(e).__name__}: {e}")
            factory = _yf_factory(symbol, session)
    else:
        factory = _yf_factory(symbol, session)

    try:
        import market_cache
        if market_cache.ENABLED:
            return market_cache.CachedTicker(
                symbol, factory if factory is not None else (lambda: real),
                session=session,
                # Scope captured now: _mark_fallback() above has already run,
                # so this reflects the provider that will actually serve data.
                scope=_cache_scope(),
            )
    except Exception as e:
        # A cache problem must never block market data. Hand back the
        # unwrapped provider ticker rather than failing the call.
        log.warning("market_cache unavailable, using uncached ticker: %s", e)

    return real if real is not None else factory()


def _yf_factory(symbol: str, session: Any = None):
    """
    Deferred yf.Ticker builder, memoised per symbol.

    Memoisation matters because several strategies call get_ticker() for the
    same symbol in one cycle; without it each would build its own object and
    yfinance would re-fetch per object. Keyed by provider scope so a provider
    switch cannot hand back an object built for the old backend.

    Only memoised when no custom session is supplied. strategy_earnings passes
    a rotating requests.Session, and sharing one object across differing
    sessions would silently apply the wrong one.
    """
    if session is not None:
        return lambda: _yf_ticker(symbol, session)

    def _build():
        try:
            import market_cache
        except ImportError:
            # Cache module genuinely unavailable: build directly.
            return _yf_ticker(symbol, None)

        # Deliberately NOT wrapped in a broad try/except. A bug in this call
        # (wrong TTL constant, bad signature) must surface as a real error
        # rather than being silently swallowed into a permanently uncached
        # path that still "works" while defeating the whole point of the cache.
        return market_cache.get_or_fetch(
            f"tickerobj:{_cache_scope()}:{symbol.upper()}",
            ttl=market_cache.QUOTE_TTL,
            fetch=lambda: _yf_ticker(symbol, None),
            cache_errors=False,   # cheap to retry; never worth pinning
        )

    return _build


def _yf_ticker(symbol: str, session: Any = None):
    import yfinance as yf
    if session is not None:
        try:
            return yf.Ticker(symbol, session=session)
        except TypeError:
            # Older/newer yfinance may not accept `session`.
            return yf.Ticker(symbol)
    return yf.Ticker(symbol)


# ── Batch history ────────────────────────────────────────────────────────────
def download(tickers, period: str = "1mo", interval: str = "1d",
             auto_adjust: bool = True, progress: bool = False,
             group_by: str = "column", threads: bool = True, **kwargs):
    """
    Batch OHLCV download, mirroring yf.download()'s MultiIndex output
    (level 0 = field, level 1 = ticker) so callers need no changes.

    Used by the volume pre-filters in strategies 1 and 2.

    Cached. vol_momentum and calendar both run a 30-day volume pre-filter over
    the same S&P 500 universe, which was two full batch downloads of identical
    data per cycle. The cache key includes the sorted ticker set, so it only
    dedupes when the request really is identical; a different universe or a
    different period still fetches.
    """
    def _fetch():
        if get_provider() == "ibkr":
            try:
                import ibkr_provider
                return ibkr_provider.batch_history(
                    tickers, period=period, interval=interval,
                    host=IBKR_HOST, port=IBKR_PORT, client_id=IBKR_CLIENT_ID,
                )
            except Exception as e:
                if not IBKR_FALLBACK_TO_YFINANCE:
                    raise
                _mark_fallback(f"download(): {type(e).__name__}: {e}")

        import yfinance as yf
        return yf.download(
            tickers, period=period, interval=interval, auto_adjust=auto_adjust,
            progress=progress, group_by=group_by, threads=threads, **kwargs
        )

    try:
        import market_cache
        if not market_cache.ENABLED:
            return _fetch()
        # Hash the symbol set rather than embedding it: a 500-ticker key string
        # would be ~3KB and is compared on every lookup.
        import hashlib
        syms = tickers if isinstance(tickers, (list, tuple, set)) else [tickers]
        digest = hashlib.md5(
            ",".join(sorted(str(s) for s in syms)).encode()
        ).hexdigest()[:16]
        # Provider is part of the key. IBKR and yfinance return subtly
        # different frames for the same request (adjustment rules, session
        # inclusion), so a cached IBKR batch must never satisfy a yfinance
        # caller after a runtime provider switch or a fallback.
        key = (f"batch:{_cache_scope()}:{digest}:{len(syms)}:{period}:"
               f"{interval}:{int(bool(auto_adjust))}:{group_by}")
        return market_cache.get_or_fetch(key, market_cache.HISTORY_TTL, _fetch)
    except Exception as e:
        log.warning("market_cache unavailable for download(): %s", e)
        return _fetch()


# ── Risk-free rate ───────────────────────────────────────────────────────────
def get_risk_free_rate_raw() -> Optional[float]:
    """
    Annualised risk-free rate as a decimal (e.g. 0.0525), or None.

    IBKR: 13-week T-bill index. Not all market-data subscriptions include
    index data, so failure here is expected and non-fatal — option_pricing
    already falls back to a 5% constant.
    """
    if get_provider() == "ibkr":
        try:
            import ibkr_provider
            rate = ibkr_provider.risk_free_rate(
                host=IBKR_HOST, port=IBKR_PORT, client_id=IBKR_CLIENT_ID
            )
            if rate is not None:
                return rate
        except Exception as e:
            log.debug("IBKR risk-free rate unavailable: %s", e)
        if not IBKR_FALLBACK_TO_YFINANCE:
            return None

    try:
        import yfinance as yf
        h = yf.Ticker("^IRX").history(period="5d")
        if h is not None and not h.empty:
            return float(h["Close"].iloc[-1]) / 100.0
    except Exception as e:
        log.debug("yfinance risk-free rate unavailable: %s", e)
    return None


def shutdown() -> None:
    """Disconnect IBKR cleanly. Safe to call when never connected."""
    try:
        import ibkr_provider
        ibkr_provider.disconnect_all()
    except Exception:
        pass
