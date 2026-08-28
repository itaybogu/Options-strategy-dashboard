"""
ibkr_provider.py — Interactive Brokers TWS market data, exposed through a
yfinance-compatible interface.

Requires:  pip install ib_insync
           TWS or IB Gateway running with "Enable ActiveX and Socket Clients"
           checked (Global Config → API → Settings).
           Default port 7497 = paper, 7496 = live.

Threading model
───────────────
The scanner runs in a background thread, but ib_insync is built on asyncio and
expects a running event loop bound to a single thread. To avoid cross-thread
loop corruption, this module owns ONE dedicated IB connection thread with its
own event loop; all requests are marshalled onto it via
`asyncio.run_coroutine_threadsafe` and block on the result. That keeps the
strategy modules fully synchronous, exactly as they were with yfinance.

Data-shape contract
───────────────────
Everything returned here matches what the strategy code already consumes:
  • history()      → DataFrame indexed by date, cols Open/High/Low/Close/Volume
  • fast_info      → object exposing .last_price / ["lastPrice"]
  • options        → tuple of "YYYY-MM-DD" strings
  • option_chain() → object with .calls / .puts DataFrames carrying
                     strike, bid, ask, lastPrice, impliedVolatility,
                     volume, openInterest
  • info           → dict with sector / fiftyTwoWeekHigh

IMPORTANT — impliedVolatility here is IBKR's own model IV (from
reqMktData generic tick 106), which is genuinely superior to yfinance's
lastPrice-derived value. option_pricing still prefers solving IV from
bid/ask mid; this only serves as the fallback path.

Earnings dates
──────────────
IBKR's API does NOT expose an earnings calendar without a Wall Street Horizon
subscription. `calendar` therefore returns None and callers fall back to the
existing yfinance/Nasdaq path. This is a deliberate, documented gap.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import math
import threading
import time
from typing import Any, Optional

log = logging.getLogger(__name__)

# Tunables
CONNECT_TIMEOUT   = 10.0    # seconds to establish the TWS socket
REQUEST_TIMEOUT   = 30.0    # per-request ceiling
QUOTE_SETTLE_SECS = 2.0     # let streaming ticks populate before snapshotting
MAX_CHAIN_STRIKES = 40      # strikes per expiry (centred on spot)

_conn_lock = threading.Lock()
_conn: Optional["_IBConnection"] = None


# ─────────────────────────────────────────────────────────────────────────────
# CONNECTION — one event loop on one dedicated thread
# ─────────────────────────────────────────────────────────────────────────────
class _IBConnection:
    def __init__(self, host: str, port: int, client_id: int):
        self.host, self.port, self.client_id = host, port, client_id
        self.ib: Any = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._err: Optional[BaseException] = None
        self._start()

    def _start(self) -> None:
        def runner() -> None:
            try:
                from ib_insync import IB
            except ImportError as e:
                self._err = ImportError(
                    "ib_insync is not installed. Run: pip install ib_insync"
                )
                self._ready.set()
                return

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self.loop = loop
            self.ib = IB()
            try:
                loop.run_until_complete(
                    asyncio.wait_for(
                        self.ib.connectAsync(
                            self.host, self.port, clientId=self.client_id
                        ),
                        timeout=CONNECT_TIMEOUT,
                    )
                )
            except BaseException as e:
                self._err = e
                self._ready.set()
                try:
                    loop.close()
                except Exception:
                    pass
                return

            self._ready.set()
            try:
                loop.run_forever()      # serve marshalled coroutines
            finally:
                try:
                    if self.ib.isConnected():
                        self.ib.disconnect()
                except Exception:
                    pass
                try:
                    loop.close()
                except Exception:
                    pass

        self._thread = threading.Thread(
            target=runner, name="ibkr-loop", daemon=True
        )
        self._thread.start()
        # +5s so a slow TWS handshake surfaces as our error, not a race
        self._ready.wait(timeout=CONNECT_TIMEOUT + 5)
        if self._err is not None:
            raise ConnectionError(
                f"Cannot connect to TWS at {self.host}:{self.port} "
                f"(clientId={self.client_id}): {self._err}. "
                "Is TWS/IB Gateway running with API access enabled?"
            )
        if self.loop is None or self.ib is None or not self.ib.isConnected():
            raise ConnectionError(
                f"TWS connection to {self.host}:{self.port} did not become ready."
            )
        log.info("Connected to IBKR TWS at %s:%s (clientId=%s)",
                 self.host, self.port, self.client_id)

    def is_connected(self) -> bool:
        try:
            return bool(self.ib and self.ib.isConnected())
        except Exception:
            return False

    def run(self, coro, timeout: float = REQUEST_TIMEOUT):
        """Execute a coroutine on the IB loop from any thread; block for result."""
        if not self.is_connected():
            raise ConnectionError("IBKR connection is not active")
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return fut.result(timeout=timeout)

    def close(self) -> None:
        try:
            if self.loop and self.loop.is_running():
                self.loop.call_soon_threadsafe(self.loop.stop)
        except Exception:
            pass


def _get_conn(host: str, port: int, client_id: int) -> _IBConnection:
    """Lazily create / reuse the shared connection, reconnecting if dropped."""
    global _conn
    with _conn_lock:
        if _conn is not None and _conn.is_connected():
            return _conn
        if _conn is not None:
            log.warning("IBKR connection lost — reconnecting.")
            _conn.close()
            _conn = None
        _conn = _IBConnection(host, port, client_id)
        return _conn


def connection_status() -> dict:
    with _conn_lock:
        c = _conn
    if c is None:
        return {"connected": False, "detail": "not yet connected"}
    return {
        "connected": c.is_connected(),
        "host": c.host, "port": c.port, "client_id": c.client_id,
    }


def disconnect_all() -> None:
    global _conn
    with _conn_lock:
        if _conn is not None:
            _conn.close()
            _conn = None


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def _period_to_duration(period: str) -> str:
    """Map a yfinance period string to an IBKR durationStr."""
    p = (period or "").strip().lower()
    return {
        "1d": "2 D", "2d": "3 D", "5d": "6 D", "1mo": "1 M", "2mo": "2 M",
        "3mo": "3 M", "6mo": "6 M", "1y": "1 Y", "2y": "2 Y", "5y": "5 Y",
        "ytd": "1 Y", "max": "5 Y",
    }.get(p, "1 Y")


def _valid(x: Any) -> bool:
    """IBKR uses -1 and NaN for 'no data'."""
    try:
        if x is None:
            return False
        v = float(x)
        return math.isfinite(v) and v >= 0
    except (TypeError, ValueError):
        return False


def _num(x: Any, default: float = float("nan")) -> float:
    return float(x) if _valid(x) else default


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC API
# ─────────────────────────────────────────────────────────────────────────────
def risk_free_rate(host: str, port: int, client_id: int) -> Optional[float]:
    """13-week T-bill (IRX) as a decimal. None if index data isn't subscribed."""
    try:
        from ib_insync import Index
        conn = _get_conn(host, port, client_id)

        async def _fetch():
            c = Index("IRX", "CBOE")
            details = await conn.ib.reqContractDetailsAsync(c)
            if not details:
                return None
            bars = await conn.ib.reqHistoricalDataAsync(
                details[0].contract, endDateTime="", durationStr="5 D",
                barSizeSetting="1 day", whatToShow="TRADES",
                useRTH=True, formatDate=1,
            )
            return float(bars[-1].close) / 100.0 if bars else None

        return conn.run(_fetch(), timeout=20)
    except Exception as e:
        log.debug("IBKR risk-free rate failed: %s", e)
        return None


def batch_history(tickers, period: str = "1mo", interval: str = "1d",
                  host: str = "127.0.0.1", port: int = 7497,
                  client_id: int = 17):
    """
    Batch OHLCV shaped like yf.download(group_by='column'):
    MultiIndex columns (field, ticker).

    Requests are issued concurrently on the IB loop but capped, since TWS
    throttles at ~50 simultaneous historical-data requests.
    """
    import pandas as pd
    from ib_insync import Stock

    if isinstance(tickers, str):
        tickers = tickers.split()
    tickers = [t for t in tickers if t]
    if not tickers:
        return pd.DataFrame()

    conn     = _get_conn(host, port, client_id)
    duration = _period_to_duration(period)
    bar_size = "1 day" if interval in ("1d", "1day", None, "") else "1 hour"

    async def _one(sym: str):
        try:
            contract = Stock(sym.replace(".", " "), "SMART", "USD")
            det = await conn.ib.reqContractDetailsAsync(contract)
            if not det:
                return sym, None
            bars = await conn.ib.reqHistoricalDataAsync(
                det[0].contract, endDateTime="", durationStr=duration,
                barSizeSetting=bar_size, whatToShow="TRADES",
                useRTH=True, formatDate=1,
            )
            if not bars:
                return sym, None
            return sym, pd.DataFrame([{
                "Date":   pd.to_datetime(b.date),
                "Open":   float(b.open),  "High":   float(b.high),
                "Low":    float(b.low),   "Close":  float(b.close),
                "Volume": float(b.volume) * 100.0,   # IBKR reports in lots
            } for b in bars]).set_index("Date")
        except Exception as e:
            log.debug("IBKR history %s failed: %s", sym, e)
            return sym, None

    async def _all():
        sem = asyncio.Semaphore(20)

        async def _guarded(s):
            async with sem:
                return await _one(s)

        return await asyncio.gather(*[_guarded(s) for s in tickers])

    results = conn.run(_all(), timeout=max(60.0, len(tickers) * 1.2))

    frames = {sym: df for sym, df in results if df is not None and not df.empty}
    if not frames:
        return pd.DataFrame()

    # Assemble (field, ticker) MultiIndex to mirror yf.download exactly.
    out = {}
    for field in ("Open", "High", "Low", "Close", "Volume"):
        for sym, df in frames.items():
            if field in df.columns:
                out[(field, sym)] = df[field]
    combined = pd.DataFrame(out)
    combined.columns = pd.MultiIndex.from_tuples(combined.columns)
    return combined.sort_index()


class _FastInfo(dict):
    """
    dict subclass that also serves attribute access, because callers use both
    `fast_info.last_price` (calendar) and `fast_info.get("lastPrice")` (vol/mom).
    """
    def __getattr__(self, item):
        try:
            return self[item]
        except KeyError:
            raise AttributeError(item)


class _Chain:
    """Mirror of yfinance's option_chain namedtuple."""
    def __init__(self, calls, puts):
        self.calls, self.puts = calls, puts


class IBKRTicker:
    """
    yfinance-Ticker-compatible wrapper over TWS.

    Only the surface actually used by the strategies is implemented; anything
    else raises AttributeError so gaps fail loudly rather than silently
    returning wrong numbers.
    """

    def __init__(self, symbol: str, host: str = "127.0.0.1",
                 port: int = 7497, client_id: int = 17):
        self.symbol = (symbol or "").upper().strip()
        self._host, self._port, self._cid = host, port, client_id
        self._conn = _get_conn(host, port, client_id)
        self._contract = None
        self._expiry_cache: Optional[tuple] = None
        self._chain_cache: dict[str, _Chain] = {}
        self._spot: Optional[float] = None

    # ── contract resolution ──────────────────────────────────────────────
    def _stock(self):
        if self._contract is not None:
            return self._contract
        from ib_insync import Stock

        async def _resolve():
            c = Stock(self.symbol.replace(".", " "), "SMART", "USD")
            det = await self._conn.ib.reqContractDetailsAsync(c)
            return det[0].contract if det else None

        self._contract = self._conn.run(_resolve(), timeout=15)
        if self._contract is None:
            raise ValueError(f"IBKR could not resolve contract for {self.symbol}")
        return self._contract

    # ── history ──────────────────────────────────────────────────────────
    def history(self, period: str = "1mo", interval: str = "1d",
                auto_adjust: bool = True, **kwargs):
        """Daily OHLCV. `auto_adjust` accepted for signature parity (IBKR
        TRADES bars are already split-adjusted; dividends are not back-adjusted)."""
        import pandas as pd
        contract = self._stock()
        duration = _period_to_duration(period)
        bar_size = "1 day" if interval in ("1d", "1day", None, "") else "1 hour"

        async def _fetch():
            return await self._conn.ib.reqHistoricalDataAsync(
                contract, endDateTime="", durationStr=duration,
                barSizeSetting=bar_size, whatToShow="TRADES",
                useRTH=True, formatDate=1,
            )

        bars = self._conn.run(_fetch(), timeout=REQUEST_TIMEOUT)
        if not bars:
            return pd.DataFrame()

        df = pd.DataFrame([{
            "Date":   pd.to_datetime(b.date),
            "Open":   float(b.open),  "High":   float(b.high),
            "Low":    float(b.low),   "Close":  float(b.close),
            "Volume": float(b.volume) * 100.0,
        } for b in bars]).set_index("Date")
        df.index.name = "Date"
        return df

    # ── quote ────────────────────────────────────────────────────────────
    @property
    def fast_info(self):
        px = self._last_price()
        return _FastInfo({
            "last_price": px, "lastPrice": px,
            "previous_close": None, "currency": "USD",
        })

    def _last_price(self) -> Optional[float]:
        if self._spot is not None:
            return self._spot
        contract = self._stock()

        async def _fetch():
            tk = self._conn.ib.reqMktData(contract, "", False, False)
            await asyncio.sleep(QUOTE_SETTLE_SECS)
            # Preference order: last trade → mid → close. Mid before close
            # matters pre/post-market when `last` is stale.
            price = None
            if _valid(getattr(tk, "last", None)):
                price = float(tk.last)
            elif _valid(getattr(tk, "bid", None)) and _valid(getattr(tk, "ask", None)):
                price = (float(tk.bid) + float(tk.ask)) / 2.0
            elif _valid(getattr(tk, "close", None)):
                price = float(tk.close)
            self._conn.ib.cancelMktData(contract)
            return price

        try:
            px = self._conn.run(_fetch(), timeout=REQUEST_TIMEOUT)
        except Exception as e:
            log.debug("%s: quote failed (%s); using last close", self.symbol, e)
            px = None

        if px is None or px <= 0:
            try:
                h = self.history(period="5d")
                if h is not None and not h.empty:
                    px = float(h["Close"].iloc[-1])
            except Exception:
                px = None

        self._spot = px
        return px

    # ── expiries ─────────────────────────────────────────────────────────
    @property
    def options(self) -> tuple:
        if self._expiry_cache is not None:
            return self._expiry_cache
        contract = self._stock()

        async def _fetch():
            return await self._conn.ib.reqSecDefOptParamsAsync(
                contract.symbol, "", contract.secType, contract.conId
            )

        try:
            params = self._conn.run(_fetch(), timeout=REQUEST_TIMEOUT)
        except Exception as e:
            log.debug("%s: reqSecDefOptParams failed: %s", self.symbol, e)
            self._expiry_cache = tuple()
            return self._expiry_cache

        expiries: set[str] = set()
        for p in params or []:
            # Prefer SMART; some names only publish per-exchange params.
            if getattr(p, "exchange", "") not in ("SMART", ""):
                continue
            for e in getattr(p, "expirations", []) or []:
                try:
                    expiries.add(
                        _dt.datetime.strptime(str(e), "%Y%m%d")
                        .date().strftime("%Y-%m-%d")
                    )
                except ValueError:
                    continue

        if not expiries:                      # fall back to any exchange
            for p in params or []:
                for e in getattr(p, "expirations", []) or []:
                    try:
                        expiries.add(
                            _dt.datetime.strptime(str(e), "%Y%m%d")
                            .date().strftime("%Y-%m-%d")
                        )
                    except ValueError:
                        continue

        self._expiry_cache = tuple(sorted(expiries))
        return self._expiry_cache

    def _strikes_for(self, expiry_yyyymmdd: str) -> list[float]:
        contract = self._stock()

        async def _fetch():
            return await self._conn.ib.reqSecDefOptParamsAsync(
                contract.symbol, "", contract.secType, contract.conId
            )

        params = self._conn.run(_fetch(), timeout=REQUEST_TIMEOUT)
        strikes: set[float] = set()
        for p in params or []:
            if expiry_yyyymmdd in (getattr(p, "expirations", []) or []):
                for s in getattr(p, "strikes", []) or []:
                    try:
                        strikes.add(float(s))
                    except (TypeError, ValueError):
                        continue
        return sorted(strikes)

    # ── option chain ─────────────────────────────────────────────────────
    def option_chain(self, expiry: str):
        """
        Chain for one "YYYY-MM-DD" expiry.

        Strikes are limited to the MAX_CHAIN_STRIKES nearest spot: a full
        S&P-name chain can exceed 200 strikes per side, and TWS enforces a
        ~100 concurrent market-data line cap. The strategies only ever look
        at 15Δ–50Δ strikes near the money, so this loses nothing they use.
        """
        if expiry in self._chain_cache:
            return self._chain_cache[expiry]

        import pandas as pd
        from ib_insync import Option

        try:
            ib_exp = _dt.datetime.strptime(expiry, "%Y-%m-%d").strftime("%Y%m%d")
        except ValueError:
            raise ValueError(f"Bad expiry {expiry!r}; expected YYYY-MM-DD")

        spot = self._last_price()
        if not spot or spot <= 0:
            raise ValueError(f"{self.symbol}: no spot price for chain build")

        strikes = self._strikes_for(ib_exp)
        if not strikes:
            empty = _empty_chain()
            self._chain_cache[expiry] = empty
            return empty

        strikes = sorted(strikes, key=lambda k: abs(k - spot))[:MAX_CHAIN_STRIKES]
        strikes = sorted(strikes)

        contract = self._stock()
        trading_class = getattr(contract, "tradingClass", "") or self.symbol

        async def _fetch_side(right: str):
            opts = [
                Option(self.symbol, ib_exp, k, right, "SMART",
                       tradingClass=trading_class)
                for k in strikes
            ]
            qualified = await self._conn.ib.qualifyContractsAsync(*opts)
            qualified = [q for q in qualified if q and getattr(q, "conId", 0)]
            if not qualified:
                return []

            # Generic tick 106 = option implied volatility.
            tickers = [
                self._conn.ib.reqMktData(q, "106", False, False)
                for q in qualified
            ]
            await asyncio.sleep(QUOTE_SETTLE_SECS + 1.5)

            rows = []
            for q, tk in zip(qualified, tickers):
                greeks = (getattr(tk, "modelGreeks", None)
                          or getattr(tk, "lastGreeks", None))
                iv = getattr(greeks, "impliedVol", None) if greeks else None
                rows.append({
                    "strike":            float(q.strike),
                    "bid":               _num(getattr(tk, "bid", None)),
                    "ask":               _num(getattr(tk, "ask", None)),
                    "lastPrice":         _num(getattr(tk, "last", None)),
                    "impliedVolatility": float(iv) if _valid(iv) else float("nan"),
                    "volume":            _num(getattr(tk, "volume", None), 0.0),
                    "openInterest":      _num(
                        getattr(tk, "callOpenInterest", None)
                        if right == "C" else
                        getattr(tk, "putOpenInterest", None), 0.0),
                    "contractSymbol":    f"{self.symbol}{ib_exp}{right}"
                                         f"{int(float(q.strike) * 1000):08d}",
                })
                try:
                    self._conn.ib.cancelMktData(q)
                except Exception:
                    pass
            return rows

        async def _both():
            # Sequential by design: concurrent call+put would double the
            # market-data lines in flight and risk tripping the TWS cap.
            calls = await _fetch_side("C")
            puts  = await _fetch_side("P")
            return calls, puts

        try:
            calls, puts = self._conn.run(
                _both(), timeout=max(REQUEST_TIMEOUT, len(strikes) * 0.6 + 30)
            )
        except Exception as e:
            log.debug("%s %s: chain fetch failed: %s", self.symbol, expiry, e)
            empty = _empty_chain()
            self._chain_cache[expiry] = empty
            return empty

        cols = ["strike", "bid", "ask", "lastPrice", "impliedVolatility",
                "volume", "openInterest", "contractSymbol"]
        chain = _Chain(
            pd.DataFrame(calls, columns=cols) if calls else _empty_df(cols),
            pd.DataFrame(puts,  columns=cols) if puts  else _empty_df(cols),
        )
        self._chain_cache[expiry] = chain
        return chain

    # ── metadata ─────────────────────────────────────────────────────────
    @property
    def info(self) -> dict:
        """
        Minimal `info` equivalent. IBKR exposes an industry taxonomy but not
        Yahoo's `sector` strings, and mapping between them would silently
        change strategy-1's sector-ETF alpha factor — so sector is None and
        the existing fallback path is used.
        """
        out: dict[str, Any] = {"symbol": self.symbol, "sector": None,
                               "fiftyTwoWeekHigh": None}
        try:
            h = self.history(period="1y")
            if h is not None and not h.empty:
                out["fiftyTwoWeekHigh"] = float(h["High"].max())
        except Exception:
            pass
        return out

    @property
    def calendar(self):
        """
        Not available: IBKR gates earnings dates behind a Wall Street Horizon
        subscription. Returning None lets callers use their existing
        yfinance / Nasdaq / Yahoo fallback chain unchanged.
        """
        return None

    @property
    def dividends(self):
        import pandas as pd
        return pd.Series(dtype=float)


def _empty_df(cols):
    import pandas as pd
    return pd.DataFrame({c: pd.Series(dtype="float64") for c in cols})


def _empty_chain() -> _Chain:
    cols = ["strike", "bid", "ask", "lastPrice", "impliedVolatility",
            "volume", "openInterest", "contractSymbol"]
    return _Chain(_empty_df(cols), _empty_df(cols))
