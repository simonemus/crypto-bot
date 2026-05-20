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

def get_exchange():
    """Restituisce istanza ccxt configurata per futures demo o live."""
    if TESTNET:
        exchange = ccxt.binanceusdm({
            "apiKey":  BINANCE_FUTURES_TESTNET_API_KEY,
            "secret":  BINANCE_FUTURES_TESTNET_API_SECRET,
            "options": {
                "defaultType": "future",
                "fetchCurrencies": False,
            },
            "urls": {
                "api": {
                    "fapiPublic":   "https://demo-fapi.binance.com/fapi/v1",
                    "fapiPrivate":  "https://demo-fapi.binance.com/fapi/v1",
                    "fapiPublicV2": "https://demo-fapi.binance.com/fapi/v2",
                    "fapiPrivateV2":"https://demo-fapi.binance.com/fapi/v2",
                    "fapiPublicV3": "https://demo-fapi.binance.com/fapi/v3",
                    "fapiPrivateV3":"https://demo-fapi.binance.com/fapi/v3",
                }
            },
        })
        logger.info("Connesso a Binance FUTURES DEMO")
    else:
        exchange = ccxt.binanceusdm({
            "apiKey":  BINANCE_FUTURES_LIVE_API_KEY,
            "secret":  BINANCE_FUTURES_LIVE_API_SECRET,
            "options": {"defaultType": "future"},
        })
        logger.info("Connesso a Binance FUTURES LIVE")

    exchange.load_markets()
    return exchange

def get_exchange_with_retry(max_retries: int = 5) -> ccxt.binanceusdm:
    """
    Tenta di connettersi a Binance con retry automatico.
    Attese: 0s, 5s, 15s, 60s, 60s
    """
    wait_times = [0, 5, 15, 60, 60]
    
    for attempt in range(max_retries):
        try:
            exchange = get_exchange()
            return exchange
        except Exception as e:
            if attempt < max_retries - 1:
                wait = wait_times[attempt]
                logger.warning(f"Connessione Binance fallita (tentativo {attempt+1}/{max_retries}) — riprovo tra {wait}s: {e}")
                if wait > 0:
                    time.sleep(wait)
            else:
                logger.error(f"Connessione Binance fallita dopo {max_retries} tentativi: {e}")
                raise    


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

def is_evening_star(prev2, prev1, curr) -> bool:
    """
    Evening Star (3 candele) — segnale SHORT:
    1. Candela verde grande
    2. Candela piccola (doji o corpo piccolo)
    3. Candela rossa che chiude oltre il 50% della prima
    """
    if "bearish_engulfing" not in PATTERNS_ENABLED:
        return False
    # Candela 1: verde grande
    c1_bullish = prev2["close"] > prev2["open"]
    c1_body = _body(prev2)
    # Candela 2: corpo piccolo
    c2_body = _body(prev1)
    c2_small = c2_body <= c1_body * 0.3
    # Candela 3: rossa che chiude oltre il 50% della prima
    c3_bearish = curr["close"] < curr["open"]
    c3_closes_below = curr["close"] < (prev2["open"] + prev2["close"]) / 2
    return c1_bullish and c2_small and c3_bearish and c3_closes_below


def is_morning_star(prev2, prev1, curr) -> bool:
    """
    Morning Star (3 candele) — segnale LONG:
    1. Candela rossa grande
    2. Candela piccola (doji o corpo piccolo)
    3. Candela verde che chiude oltre il 50% della prima
    """
    if "bullish_engulfing" not in PATTERNS_ENABLED:
        return False
    # Candela 1: rossa grande
    c1_bearish = prev2["close"] < prev2["open"]
    c1_body = _body(prev2)
    # Candela 2: corpo piccolo
    c2_body = _body(prev1)
    c2_small = c2_body <= c1_body * 0.3
    # Candela 3: verde che chiude oltre il 50% della prima
    c3_bullish = curr["close"] > curr["open"]
    c3_closes_above = curr["close"] > (prev2["open"] + prev2["close"]) / 2
    return c1_bearish and c2_small and c3_bullish and c3_closes_above


def is_dark_cloud_cover(prev, curr) -> bool:
    """
    Dark Cloud Cover (2 candele) — segnale SHORT:
    1. Candela verde grande
    2. Candela rossa che apre sopra il massimo della verde
       e chiude oltre il 50% del corpo della verde
    """
    if "bearish_engulfing" not in PATTERNS_ENABLED:
        return False
    c1_bullish = prev["close"] > prev["open"]
    c1_body = _body(prev)
    c2_bearish = curr["close"] < curr["open"]
    c2_opens_above = curr["open"] > prev["high"]
    c2_closes_below_midpoint = curr["close"] < (prev["open"] + prev["close"]) / 2
    return c1_bullish and c2_bearish and c2_opens_above and c2_closes_below_midpoint


def is_piercing_line(prev, curr) -> bool:
    """
    Piercing Line (2 candele) — segnale LONG:
    1. Candela rossa grande
    2. Candela verde che apre sotto il minimo della rossa
       e chiude oltre il 50% del corpo della rossa
    """
    if "bullish_engulfing" not in PATTERNS_ENABLED:
        return False
    c1_bearish = prev["close"] < prev["open"]
    c2_bullish = curr["close"] > curr["open"]
    c2_opens_below = curr["open"] < prev["low"]
    c2_closes_above_midpoint = curr["close"] > (prev["open"] + prev["close"]) / 2
    return c1_bearish and c2_bullish and c2_opens_below and c2_closes_above_midpoint        

def detect_pattern(df: pd.DataFrame, direction: str) -> str | None:
    """
    Controlla le ultime tre candele chiuse.
    Restituisce il nome del pattern trovato o None.
    direction: 'long' | 'short'
    """
    curr = df.iloc[-2]
    prev = df.iloc[-3]
    prev2 = df.iloc[-4]

    if direction == "long":
        if is_morning_star(prev2, prev, curr):
            return "morning_star"
        if is_piercing_line(prev, curr):
            return "piercing_line"
        if is_bullish_engulfing(prev, curr):
            return "bullish_engulfing"
        if is_hammer(curr):
            return "hammer"
        if is_doji(curr):
            return "doji"
    else:
        if is_evening_star(prev2, prev, curr):
            return "evening_star"
        if is_dark_cloud_cover(prev, curr):
            return "dark_cloud_cover"
        if is_bearish_engulfing(prev, curr):
            return "bearish_engulfing"
        if is_shooting_star(curr):
            return "shooting_star"
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

def check_signal_decay(current_price: float, pdh: float, pdl: float, 
                        direction: str, decay_buffer: float) -> bool:
    """
    Verifica se il segnale è decaduto.
    Decade se il prezzo si allontana troppo dal livello rotto
    in ENTRAMBE le direzioni — sia contro che a favore.
    """
    buf = decay_buffer / 100

    if direction == "long":
        # Decade se scende troppo sotto PDH (breakout falso)
        # O se sale troppo sopra PDH (retest impossibile)
        distance = abs(current_price - pdh) / pdh
        return distance > buf
    else:
        # Decade se risale troppo sopra PDL (breakout falso)
        # O se scende troppo sotto PDL (retest impossibile)
        distance = abs(current_price - pdl) / pdl
        return distance > buf    


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


def calc_quantity(exchange, symbol: str, entry: float,
                  sl: float, capital_usdt: float, risk_pct: float,
                  leverage: int = 2) -> float:
    """
    Calcola la quantità in base al rischio reale per trade.
    Rischio 1% = perdi esattamente 1% del capitale se SL colpito.
    Cap di sicurezza: margine mai superiore al 10% del capitale.
    """
    # Rischio reale in USDT
    risk_usdt = capital_usdt * (risk_pct / 100)

    # Distanza SL in USDT
    sl_dist = abs(entry - sl)
    if sl_dist == 0:
        return 0.0

    # Quantità = rischio / distanza SL
    qty = risk_usdt / sl_dist

    # Cap di sicurezza — margine massimo 10% del capitale
    max_margin = capital_usdt * 0.10
    max_position = max_margin * leverage
    max_qty = max_position / entry

    if qty > max_qty:
        logger.warning(f"Quantità ridotta per cap margine: {qty:.6f} → {max_qty:.6f}")
        qty = max_qty

    # Arrotonda alla precisione del mercato
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


def place_sl_tp_orders(exchange, symbol: str, side: str,
                       qty: float, tp: float, sl: float) -> dict:
    """
    Invia ordini separati TAKE_PROFIT_MARKET e STOP_MARKET per futures.
    side: 'sell' per LONG, 'buy' per SHORT
    """
    results = {}

    # Take Profit
    try:
        tp_order = exchange.create_order(
            symbol, "TAKE_PROFIT_MARKET", side, qty,
            params={
                "stopPrice": tp,
                "closePosition": True,
                "workingType": "MARK_PRICE",
            }
        )
        results["tp_order"] = tp_order
        logger.info(f"TP order piazzato: {tp}")
    except Exception as e:
        logger.error(f"Errore TP order: {e}")

    # Stop Loss
    try:
        sl_order = exchange.create_order(
            symbol, "STOP_MARKET", side, qty,
            params={
                "stopPrice": sl,
                "closePosition": True,
                "workingType": "MARK_PRICE",
            }
        )
        results["sl_order"] = sl_order
        logger.info(f"SL order piazzato: {sl}")
    except Exception as e:
        logger.error(f"Errore SL order: {e}")

    return results


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
    """Restituisce il saldo disponibile in USDT — compatibile con futures demo."""
    try:
        balance = exchange.fetch_balance()
        assets = balance.get("info", {}).get("assets", [])
        for asset in assets:
            if asset.get("asset") == "USDT":
                return float(asset.get("availableBalance", 0))
        # Fallback
        free = balance.get("free", {})
        if "USDT" in free:
            return float(free["USDT"])
        return 0.0
    except Exception as e:
        logger.error(f"Errore get_balance_usdt: {e}")
        return 0.0


def get_ticker_price(exchange: ccxt.binance, symbol: str) -> float:
    """Prezzo corrente del ticker — compatibile con futures e spot."""
    ticker = exchange.fetch_ticker(symbol)
    price = ticker.get("last") or ticker.get("ask") or ticker.get("close")
    return float(price)

def _update_sl_order(exchange, symbol: str, new_sl: float, qty: float, direction: str, old_sl_order_id: str = None) -> str | None:
    """
    Cancella il vecchio SL per ID e piazza il nuovo.
    Restituisce il nuovo order_id, oppure None se qualcosa va storto.
    REGOLA: se la cancellazione fallisce, NON crea il nuovo SL.
    """
    side = "sell" if direction == "long" else "buy"
    raw_symbol = symbol.replace("/", "").replace(":USDT", "")

    # Step 1 — cancella il vecchio SL per ID
    if old_sl_order_id:
        try:
            exchange.cancel_order(str(old_sl_order_id), symbol)
            logger.info(f"{symbol} — vecchio SL cancellato (id={old_sl_order_id})")
        except Exception as e:
            logger.error(f"{symbol} — ERRORE cancellazione vecchio SL (id={old_sl_order_id}): {e} — provo fallback per tipo")
            # Fallback — cerca per tipo
            try:
                open_orders = exchange.fapiPrivateGetOpenOrders({"symbol": raw_symbol})
                for order in open_orders:
                    order_type = str(order.get("type", "")).lower()
                    order_id = order.get("orderId")
                    if "stop" in order_type and "take" not in order_type and order_id:
                        exchange.cancel_order(str(order_id), symbol)
                        logger.info(f"{symbol} — vecchio SL cancellato per tipo (id={order_id})")
            except Exception as e2:
                logger.error(f"{symbol} — ERRORE fallback cancellazione SL: {e2}")
                return None
    else:
        # Nessun ID salvato — cerca per tipo come fallback
        logger.warning(f"{symbol} — nessun sl_order_id salvato, cerco per tipo (fallback)")
        try:
            open_orders = exchange.fapiPrivateGetOpenOrders({"symbol": raw_symbol})
            for order in open_orders:
                order_type = str(order.get("type", "")).lower()
                order_id = order.get("orderId")
                if "stop" in order_type and "take" not in order_type and order_id:
                    exchange.cancel_order(str(order_id), symbol)
                    logger.info(f"{symbol} — vecchio SL cancellato per tipo (id={order_id})")
        except Exception as e:
            logger.error(f"{symbol} — ERRORE cancellazione SL per tipo: {e}")
            return None

    # Step 2 — verifica che non ci siano ancora SL aperti
    try:
        open_orders = exchange.fapiPrivateGetOpenOrders({"symbol": raw_symbol})
        remaining = [
            o for o in open_orders
            if "stop" in str(o.get("type", "")).lower()
            and "take" not in str(o.get("type", "")).lower()
        ]
        if remaining:
            logger.error(f"{symbol} — {len(remaining)} SL ancora aperti dopo cancellazione!")
            from telegram_bot import send_message
            send_message(
                f"🚨 ATTENZIONE — {symbol}\n"
                f"Trovati {len(remaining)} SL aperti dopo cancellazione.\n"
                f"Trailing stop bloccato per sicurezza."
            )
            return None
    except Exception as e:
        logger.warning(f"{symbol} — impossibile verificare ordini dopo cancellazione: {e}")

    # Step 3 — piazza il nuovo SL
    try:
        new_order = exchange.create_order(
            symbol, "STOP_MARKET", side, qty,
            params={
                "stopPrice": new_sl,
                "closePosition": True,
                "workingType": "MARK_PRICE",
            }
        )
        new_id = str(new_order.get("id", ""))
        logger.info(f"{symbol} — nuovo SL piazzato a {new_sl:.4f} (id={new_id})")
        return new_id
    except Exception as e:
        logger.error(f"{symbol} — ERRORE piazzamento nuovo SL: {e}")
        return None

def set_leverage_all(exchange, symbols: list, leverage: int = 2) -> None:
    """
    Imposta la leva per tutti gli asset all'avvio del bot.
    """
    for symbol in symbols:
        try:
            # ccxt vuole il simbolo senza il suffisso :USDT
            market_symbol = symbol.replace("/USDT:USDT", "/USDT")
            exchange.set_leverage(leverage, market_symbol)
            logger.info(f"Leva impostata a {leverage}x per {market_symbol}")
        except Exception as e:
            logger.error(f"Errore impostazione leva {symbol}: {e}")
def get_open_positions(exchange) -> list[dict]:
    """
    Restituisce le posizioni aperte su Binance Futures.
    """
    try:
        positions = exchange.fetch_positions()
        return [
            p for p in positions
            if float(p.get("contracts", 0)) != 0
        ]
    except Exception as e:
        logger.error(f"Errore get_open_positions: {e}")
        return []


def has_open_position(exchange, symbol: str) -> bool:
    """
    Verifica se esiste già una posizione aperta su Binance per il simbolo.
    """
    try:
        positions = get_open_positions(exchange)
        for p in positions:
            if p.get("symbol") == symbol and float(p.get("contracts", 0)) != 0:
                return True
        return False
    except Exception as e:
        logger.error(f"Errore has_open_position: {e}")
        return False                        
