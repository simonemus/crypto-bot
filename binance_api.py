# ============================================================
#  CRYPTO BREAKOUT BOT — binance_api.py
#  Gestisce connessione Binance (testnet/live), dati OHLCV,
#  calcolo indicatori, pattern candele e invio ordini.
# ============================================================

import ccxt
import pandas as pd
import numpy as np
import logging
from datetime import datetime, timezone, timedelta
from config import (
    TESTNET,
    BINANCE_FUTURES_TESTNET_API_KEY, BINANCE_FUTURES_TESTNET_API_SECRET,
    BINANCE_FUTURES_LIVE_API_KEY,    BINANCE_FUTURES_LIVE_API_SECRET,
    BINANCE_LIVE_API_KEY,            BINANCE_LIVE_API_SECRET,
    ATR_PERIOD, EMA_FAST, EMA_SLOW,
    BREAKOUT_BUFFER, RETEST_BUFFER, MAX_RISK_ATR,
    TF_SIGNAL, TF_ENTRY, PATTERNS_ENABLED,
)
logger = logging.getLogger(__name__)


# ── CONNESSIONE ───────────────────────────────────────────────

def get_exchange() -> ccxt.binance:
    """Restituisce istanza ccxt.binance configurata per futures testnet o live."""
    if TESTNET:
        exchange = ccxt.binance({
            "apiKey":  BINANCE_FUTURES_TESTNET_API_KEY,
            "secret":  BINANCE_FUTURES_TESTNET_API_SECRET,
            "options": {"defaultType": "future"},
        })
        exchange.set_sandbox_mode(True)
        logger.info("Connesso a Binance FUTURES TESTNET")
    else:
        exchange = ccxt.binance({
            "apiKey":  BINANCE_FUTURES_LIVE_API_KEY,
            "secret":  BINANCE_FUTURES_LIVE_API_SECRET,
            "options": {"defaultType": "future"},
        })
        logger.info("Connesso a Binance FUTURES LIVE")

    exchange.load_markets()
    return exchange


# ── DATI OHLCV ────────────────────────────────────────────────

def fetch_ohlcv(exchange: ccxt.binance, symbol: str, timeframe: str, limit: int = 100) -> pd.DataFrame:
    """
    Scarica candele OHLCV e restituisce DataFrame con colonne:
    timestamp, open, high, low, close, volume
    """
    raw = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df.set_index("timestamp", inplace=True)
    return df


def get_previous_day_hl(exchange: ccxt.binance, symbol: str) -> tuple[float, float]:
    """
    Restituisce (PDH, PDL) — high e low della candela daily precedente.
    """
    df = fetch_ohlcv(exchange, symbol, "1d", limit=3)
    # index -2 = giorno precedente (index -1 = giorno corrente parziale)
    pdh = float(df.iloc[-2]["high"])
    pdl = float(df.iloc[-2]["low"])
    logger.debug(f"{symbol} PDH={pdh:.4f}  PDL={pdl:.4f}")
    return pdh, pdl


# ── INDICATORI ────────────────────────────────────────────────

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Aggiunge ATR, EMA20, EMA50 al DataFrame."""
    df = df.copy()

    # EMA
    df["ema_fast"] = df["close"].ewm(span=EMA_FAST, adjust=False).mean()
    df["ema_slow"] = df["close"].ewm(span=EMA_SLOW, adjust=False).mean()

    # ATR (True Range → media mobile semplice)
    df["prev_close"] = df["close"].shift(1)
    df["tr"] = df[["high", "prev_close"]].max(axis=1) - df[["low", "prev_close"]].min(axis=1)
    df["atr"] = df["tr"].rolling(ATR_PERIOD).mean()

    return df


def trend_ok(df: pd.DataFrame, direction: str) -> bool:
    """
    Filtra il trend con EMA20/EMA50 sull'ultima candela chiusa.
    direction: 'long' oppure 'short'
    """
    last = df.iloc[-2]   # ultima candela CHIUSA
    if direction == "long":
        return last["ema_fast"] > last["ema_slow"]
    else:
        return last["ema_fast"] < last["ema_slow"]


def atr_ok(df: pd.DataFrame, entry: float, stop: float) -> bool:
    """Verifica che la distanza SL non superi MAX_RISK_ATR * ATR."""
    atr = df.iloc[-2]["atr"]
    sl_distance = abs(entry - stop)
    return sl_distance <= MAX_RISK_ATR * atr


# ── CANDLESTICK PATTERNS ──────────────────────────────────────

def _body(row) -> float:
    return abs(row["close"] - row["open"])

def _upper_wick(row) -> float:
    return row["high"] - max(row["close"], row["open"])

def _lower_wick(row) -> float:
    return min(row["close"], row["open"]) - row["low"]

def is_hammer(row) -> bool:
    """Hammer: lower wick ≥ 2× body, upper wick piccola, corpo nella metà superiore."""
    if "hammer" not in PATTERNS_ENABLED:
        return False
    body = _body(row)
    if body == 0:
        return False
    lower = _lower_wick(row)
    upper = _upper_wick(row)
    return lower >= 2 * body and upper <= 0.3 * body

def is_shooting_star(row) -> bool:
    """Shooting Star: upper wick ≥ 2× body, lower wick piccola."""
    if "shooting_star" not in PATTERNS_ENABLED:
        return False
    body = _body(row)
    if body == 0:
        return False
    upper = _upper_wick(row)
    lower = _lower_wick(row)
    return upper >= 2 * body and lower <= 0.3 * body

def is_bullish_engulfing(prev, curr) -> bool:
    """Bullish Engulfing: candela verde che ingloba il corpo della rossa precedente."""
    if "bullish_engulfing" not in PATTERNS_ENABLED:
        return False
    prev_bearish = prev["close"] < prev["open"]
    curr_bullish  = curr["close"] > curr["open"]
    engulfs = curr["open"] <= prev["close"] and curr["close"] >= prev["open"]
    return prev_bearish and curr_bullish and engulfs

def is_bearish_engulfing(prev, curr) -> bool:
    """Bearish Engulfing: candela rossa che ingloba il corpo della verde precedente."""
    if "bearish_engulfing" not in PATTERNS_ENABLED:
        return False
    prev_bullish = prev["close"] > prev["open"]
    curr_bearish  = curr["close"] < curr["open"]
    engulfs = curr["open"] >= prev["close"] and curr["close"] <= prev["open"]
    return prev_bullish and curr_bearish and engulfs

def is_doji(row) -> bool:
    """Doji: corpo molto piccolo rispetto al prezzo."""
    body = _body(row)
    price = row["close"]
    return body / price <= 0.001    

def detect_pattern(df: pd.DataFrame, direction: str) -> str | None:
    """
    Controlla le ultime due candele chiuse.
    Restituisce il nome del pattern trovato o None.
    direction: 'long' | 'short'
    """
    curr = df.iloc[-2]
    prev = df.iloc[-3]

    if direction == "long":
        if is_hammer(curr):
            return "hammer"
        if is_bullish_engulfing(prev, curr):
            return "bullish_engulfing"
        if is_doji(curr):
            return "doji"
    else:
        if is_shooting_star(curr):
            return "shooting_star"
        if is_bearish_engulfing(prev, curr):
            return "bearish_engulfing"
        if is_doji(curr):
            return "doji"

    return None


# ── LOGICA BREAKOUT + RETEST ──────────────────────────────────

def check_breakout(df_15m: pd.DataFrame, pdh: float, pdl: float) -> str | None:
    """
    Verifica su 15m se il prezzo ha rotto PDH (long) o PDL (short)
    con buffer dinamico basato su ATR.
    """
    last = df_15m.iloc[-2]
    last_close = float(last["close"])
    current_price = last_close
    atr = float(last["atr"])

    # Buffer dinamico: max(0.20%, ATR% × 0.50)
    atr_pct = atr / current_price
    breakout_buffer = max(0.0020, atr_pct * 1.5)

    buf_h = pdh * (1 + breakout_buffer)
    buf_l = pdl * (1 - breakout_buffer)

    logger.debug(f"Buffer dinamico: {breakout_buffer*100:.3f}% (ATR%={atr_pct*100:.3f}%)")

    if last_close > buf_h:
        logger.info(f"Breakout LONG confermato: close={last_close:.4f} > PDH_buf={buf_h:.4f} (buffer={breakout_buffer*100:.2f}%)")
        return "long"
    if last_close < buf_l:
        logger.info(f"Breakout SHORT confermato: close={last_close:.4f} < PDL_buf={buf_l:.4f} (buffer={breakout_buffer*100:.2f}%)")
        return "short"
    return None


def check_retest(df_5m: pd.DataFrame, pdh: float, pdl: float, direction: str) -> bool:
    """
    Verifica su 5m se il prezzo è tornato a fare retest del livello rotto.
    Buffer asimmetrico: più tollerante verso il livello, più restrittivo oltre.
    """
    last = df_5m.iloc[-2]
    
    if direction == "long":
        # Retest del PDH da sotto
        retest_zone_hi = pdh * (1 + 0.0010)   # 0.10% sopra PDH
        retest_zone_lo = pdh * (1 - 0.0050)   # 0.50% sotto PDH
        touched = retest_zone_lo <= last["low"] <= retest_zone_hi or \
                  retest_zone_lo <= last["close"] <= retest_zone_hi
    else:
        # Retest del PDL da sopra
        retest_zone_hi = pdl * (1 + 0.0010)   # 0.10% sopra PDL
        retest_zone_lo = pdl * (1 - 0.0050)   # 0.50% sotto PDL
        touched = retest_zone_lo <= last["high"] <= retest_zone_hi or \
                  retest_zone_lo <= last["close"] <= retest_zone_hi

    if touched:
        logger.info(f"Retest confermato su {'PDH' if direction == 'long' else 'PDL'}")
    return touched


# ── CALCOLO SL / TP ───────────────────────────────────────────

def calc_sl_tp(entry: float, direction: str, atr: float, rr: float) -> tuple[float, float]:
    """
    Calcola Stop Loss e Take Profit.
    SL distante MAX_RISK_ATR * ATR dall'entry.
    TP = entry ± (SL_distance * RR)
    """
    sl_dist = MAX_RISK_ATR * atr
    if direction == "long":
        sl = entry - sl_dist
        tp = entry + sl_dist * rr
    else:
        sl = entry + sl_dist
        tp = entry - sl_dist * rr

    return round(sl, 6), round(tp, 6)


def calc_quantity(exchange: ccxt.binance, symbol: str, entry: float,
                  sl: float, capital_usdt: float, risk_pct: float) -> float:
    """
    Calcola la quantità da acquistare/vendere in base al rischio %.
    """
    risk_usdt  = capital_usdt * (risk_pct / 100)
    sl_dist    = abs(entry - sl)
    if sl_dist == 0:
        return 0.0
    qty = risk_usdt / sl_dist

    # Arrotonda alla precisione del mercato
    market = exchange.market(symbol)
    precision = market.get("precision", {}).get("amount", 6)
    qty = float(exchange.amount_to_precision(symbol, qty))
    return qty


# ── INVIO ORDINI ──────────────────────────────────────────────

def place_market_order(exchange: ccxt.binance, symbol: str,
                       side: str, qty: float) -> dict:
    """
    Apre un ordine market.
    side: 'buy' | 'sell'
    """
    logger.info(f"Invio ordine MARKET {side.upper()} {qty} {symbol}")
    order = exchange.create_order(symbol, "market", side, qty)
    return order


def place_oco_order(exchange: ccxt.binance, symbol: str, side: str,
                    qty: float, tp: float, sl: float, sl_limit_offset_pct: float = 0.1) -> dict:
    """
    Invia ordine OCO (One-Cancels-the-Other) per TP e SL.
    sl_limit_offset_pct: offset % del SL limit rispetto al SL stop.
    """
    sl_limit = sl * (1 - sl_limit_offset_pct / 100) if side == "sell" else sl * (1 + sl_limit_offset_pct / 100)
    sl_limit = round(sl_limit, 6)

    logger.info(f"Invio OCO {side.upper()} qty={qty} tp={tp} sl={sl} sl_limit={sl_limit}")

    params = {
        "stopPrice":      str(sl),
        "stopLimitPrice": str(sl_limit),
        "stopLimitTimeInForce": "GTC",
    }
    order = exchange.create_order(
        symbol, "oco", side, qty, tp, params
    )
    return order


def cancel_all_orders(exchange: ccxt.binance, symbol: str) -> None:
    """Cancella tutti gli ordini aperti su un simbolo (forza chiusura)."""
    try:
        exchange.cancel_all_orders(symbol)
        logger.info(f"Tutti gli ordini cancellati per {symbol}")
    except Exception as e:
        logger.error(f"Errore nella cancellazione ordini {symbol}: {e}")


def close_position_market(exchange: ccxt.binance, symbol: str,
                           direction: str, qty: float) -> dict | None:
    """Chiude una posizione aperta con ordine market."""
    side = "sell" if direction == "long" else "buy"
    try:
        order = place_market_order(exchange, symbol, side, qty)
        logger.info(f"Posizione {direction} chiusa: {order}")
        return order
    except Exception as e:
        logger.error(f"Errore chiusura posizione {symbol}: {e}")
        return None


# ── BILANCIO ──────────────────────────────────────────────────

def get_balance_usdt(exchange: ccxt.binance) -> float:
    """Restituisce il saldo disponibile in USDT — compatibile con futures e spot."""
    try:
        balance = exchange.fetch_balance()
        # Futures
        if "USDT" in balance.get("free", {}):
            return float(balance["free"]["USDT"])
        # Futures alternativo
        assets = balance.get("info", {}).get("assets", [])
        for asset in assets:
            if asset.get("asset") == "USDT":
                return float(asset.get("availableBalance", 0))
        return 0.0
    except Exception as e:
        logger.error(f"Errore get_balance_usdt: {e}")
        return 0.0


def get_ticker_price(exchange: ccxt.binance, symbol: str) -> float:
    """Prezzo corrente del ticker — compatibile con futures e spot."""
    ticker = exchange.fetch_ticker(symbol)
    price = ticker.get("last") or ticker.get("ask") or ticker.get("close")
    return float(price)
