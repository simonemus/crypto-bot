# ============================================================
#  CRYPTO BREAKOUT BOT — binance_api.py
#  Gestisce connessione Binance (testnet/live), dati OHLCV,
#  calcolo indicatori, pattern candele e invio ordini.
# ============================================================

import ccxt
import time
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
    SL_PCT, TP_PCT, TRAILING_ACTIVATION_PCT,
    RISK_USDT, MAX_MARGIN_USDT,
    TRAILING_CALLBACK, ATR_FILTERS,
    TF_SIGNAL, TF_ENTRY, PATTERNS_ENABLED,
)

logger = logging.getLogger(__name__)

def _get_param(key, default):
    """Legge un parametro dal DB, fallback al valore default."""
    try:
        from database import get_config_param
        val = get_config_param(key)
        if val is not None:
            return float(val)
    except Exception:
        pass
    return default


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

    # TEST ordine reale minimo con nuove API key
    try:
        result = exchange.fapiPrivatePostOrder({
            "symbol": "ETHUSDT",
            "side": "BUY",
            "type": "MARKET",
            "quantity": "0.01",
            "timestamp": exchange.milliseconds(),
        })
        logger.info("RAW MARKET ORDER OK: %s", result)
    except Exception as e:
        logger.error("RAW MARKET ORDER ERROR: %s", e)

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

    # ATR Wilder/RMA — allineato a TradingView/Binance
    df["prev_close"] = df["close"].shift(1)
    df["tr"] = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["prev_close"]).abs(),
        (df["low"] - df["prev_close"]).abs()
    ], axis=1).max(axis=1)
    df["atr"] = df["tr"].ewm(
        alpha=1 / ATR_PERIOD,
        adjust=False,
        min_periods=ATR_PERIOD
    ).mean()
    df["atr_pct"] = (df["atr"] / df["close"]) * 100

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


def classify_atr(symbol: str, atr_pct: float) -> str:
    """
    Classifica la volatilità ATR% rispetto ai filtri per asset.
    Riceve direttamente atr_pct — non ricalcola.
    Legge i valori min/max dal DB, fallback a config.py.
    Restituisce: IDEAL_VOLATILITY, VALID_BUT_NOT_IDEAL,
                 NO_TRADE_LOW_VOLATILITY, NO_TRADE_HIGH_VOLATILITY,
                 ATR_NOT_READY, ATR_FILTER_NOT_FOUND
    """
    if atr_pct is None or pd.isna(atr_pct):
        return "ATR_NOT_READY"
    f = ATR_FILTERS.get(symbol)
    if not f:
        return "ATR_FILTER_NOT_FOUND"
    atr_min = _get_param(f"atr_min_{symbol}", f["min"])
    atr_max = _get_param(f"atr_max_{symbol}", f["max"])
    ideal_min = f["ideal_min"]
    ideal_max = f["ideal_max"]

    if atr_pct < atr_min:
        return "NO_TRADE_LOW_VOLATILITY"
    if atr_pct > atr_max:
        return "NO_TRADE_HIGH_VOLATILITY"
    if ideal_min <= atr_pct <= ideal_max:
        return "IDEAL_VOLATILITY"
    return "VALID_BUT_NOT_IDEAL"


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
    if "evening_star" not in PATTERNS_ENABLED:
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
    if "morning_star" not in PATTERNS_ENABLED:
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
    if "dark_cloud_cover" not in PATTERNS_ENABLED:
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
    if "piercing_line" not in PATTERNS_ENABLED:
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

def calc_sl_tp(entry: float, direction: str) -> tuple[float, float]:
    """
    Calcola Stop Loss e Take Profit con percentuali fisse.
    Legge SL% e TP% dal DB, fallback a config.py.
    """
    sl_pct = _get_param("sl_pct", SL_PCT * 100) / 100
    tp_pct = _get_param("tp_pct", TP_PCT * 100) / 100

    if direction == "long":
        sl = entry * (1 - sl_pct)
        tp = entry * (1 + tp_pct)
    else:
        sl = entry * (1 + sl_pct)
        tp = entry * (1 - tp_pct)

    return round(sl, 6), round(tp, 6)


def calc_quantity(exchange, symbol: str, entry: float) -> float:
    """
    Calcola la quantità in base al rischio fisso per trade.
    RISK_USDT = 15 USDT
    SL_PCT = 1.5%
    Quantità = RISK_USDT / (entry * SL_PCT)
    Cap: margine massimo 500 USDT con leva 2x → posizione max 1000 USDT
    """
    # Quantità = rischio / (entry * SL%) — legge sl_pct dal DB
    sl_pct = _get_param("sl_pct", SL_PCT * 100) / 100
    qty = RISK_USDT / (entry * sl_pct)

    # Cap margine — posizione max 1000 USDT
    position_value = qty * entry
    max_position = MAX_MARGIN_USDT * 2  # leva 2x

    if position_value > max_position:
        logger.warning(f"Quantità ridotta per cap margine: {qty:.6f} → {max_position/entry:.6f}")
        qty = max_position / entry

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


def place_tp_order(exchange, symbol: str, side: str,
                   qty: float, tp: float) -> dict:
    """Piazza Take Profit Market reduceOnly."""
    try:
        tp_price = exchange.price_to_precision(symbol, tp)
        qty_precise = exchange.amount_to_precision(symbol, qty)
        tp_order = exchange.create_order(
            symbol,
            "TAKE_PROFIT_MARKET",
            side,
            qty_precise,
            params={
                "stopPrice": tp_price,
                "workingType": "MARK_PRICE",
                "reduceOnly": True,
            }
        )
        logger.info(f"TP order piazzato: {symbol} {side.upper()} trigger={tp_price}")
        return tp_order
    except Exception as e:
        logger.error(f"Errore TP order {symbol}: {e}")
        return {}


def place_sl_order(exchange, symbol: str, side: str,
                   qty: float, sl: float) -> dict:
    """
    Piazza Stop Loss Market reduceOnly.
    side: lato di uscita.
    - Se posizione LONG: side='sell'
    - Se posizione SHORT: side='buy'
    """
    try:
        sl_price = exchange.price_to_precision(symbol, sl)
        qty_precise = exchange.amount_to_precision(symbol, qty)
        sl_order = exchange.create_order(
            symbol,
            "STOP_MARKET",
            side,
            qty_precise,
            params={
                "stopPrice": sl_price,
                "workingType": "MARK_PRICE",
                "reduceOnly": True,
            }
        )
        logger.info(f"SL order piazzato: {symbol} {side.upper()} stop={sl_price}")
        return sl_order
    except Exception as e:
        logger.error(f"Errore SL order {symbol}: {e}")
        return {}


def place_trailing_order(exchange, symbol: str, side: str,
                         qty: float, activation_price: float) -> dict:
    """
    Piazza il trailing stop quando il prezzo raggiunge +1.5%.
    Callback rate fisso per asset da config.
    """
    try:
        market = exchange.market(symbol)
        raw_symbol = market["id"]
        qty_precise = exchange.amount_to_precision(symbol, qty)
        activation_precise = exchange.price_to_precision(symbol, activation_price)
        default_callback = TRAILING_CALLBACK.get(symbol, 0.6)
        callback_rate = _get_param(f"callback_{symbol}", default_callback)

        algo_order = exchange.fapiPrivatePostAlgoOrder({
            "algoType": "CONDITIONAL",
            "symbol": raw_symbol,
            "side": side.upper(),
            "type": "TRAILING_STOP_MARKET",
            "quantity": str(qty_precise),
            "callbackRate": str(callback_rate),
            "activatePrice": str(activation_precise),
            "workingType": "MARK_PRICE",
            "reduceOnly": "true",
            "clientAlgoId": f"trail_{raw_symbol}_{int(time.time())}",
            "newOrderRespType": "RESULT",
        })
        logger.info(
            f"Trailing Stop piazzato: symbol={symbol} side={side.upper()} "
            f"callbackRate={callback_rate}% activatePrice={activation_precise} "
            f"algoId={algo_order.get('algoId')}"
        )
        return algo_order
    except Exception as e:
        logger.error(f"Errore Trailing Stop order: {e}")
        return {}


def cancel_all_orders(exchange: ccxt.binance, symbol: str) -> None:
    """Cancella tutti gli ordini standard aperti su un simbolo."""
    try:
        exchange.cancel_all_orders(symbol)
        logger.info(f"Tutti gli ordini standard cancellati per {symbol}")
    except Exception as e:
        logger.error(f"Errore nella cancellazione ordini {symbol}: {e}")


def cancel_algo_orders(exchange, symbol: str) -> None:
    """
    Cancella tutti gli ordini Algo aperti su un simbolo.
    Usa DELETE /fapi/v1/algoOpenOrders.
    """
    try:
        market = exchange.market(symbol)
        raw_symbol = market["id"]
        response = exchange.fapiPrivateDeleteAlgoOpenOrders({
            "symbol": raw_symbol
        })
        logger.info(f"Algo orders cancellati per {symbol}: {response}")
    except AttributeError:
        logger.error(
            f"Metodo fapiPrivateDeleteAlgoOpenOrders non trovato in CCXT — "
            f"verifica dir(exchange) per il nome corretto"
        )
    except Exception as e:
        logger.error(f"Errore cancellazione ordini Algo {symbol}: {e}")


def cancel_all_symbol_orders(exchange, symbol: str) -> None:
    """
    Cancella sia ordini standard sia ordini Algo.
    Da usare in force close, monitor e chiusure critiche.
    """
    try:
        cancel_all_orders(exchange, symbol)
    except Exception as e:
        logger.error(f"Errore cancellazione ordini standard {symbol}: {e}")
    try:
        cancel_algo_orders(exchange, symbol)
    except Exception as e:
        logger.error(f"Errore cancellazione ordini Algo {symbol}: {e}")        


def close_position_market(exchange: ccxt.binance, symbol: str,
                           direction: str, qty: float) -> dict | None:
    """Chiude una posizione aperta con ordine market reduceOnly."""
    side = "sell" if direction == "long" else "buy"
    try:
        quantity = exchange.amount_to_precision(symbol, qty)
        order = exchange.create_order(
            symbol,
            "market",
            side,
            quantity,
            params={
                "reduceOnly": True
            }
        )
        logger.info(f"Posizione {direction} chiusa reduceOnly: {order}")
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


def normalize_symbol(symbol: str) -> str:
    """Normalizza il simbolo rimuovendo il suffisso :USDT."""
    return symbol.replace(":USDT", "")


def has_open_position(exchange, symbol: str) -> bool:
    """
    Verifica se esiste già una posizione aperta su Binance per il simbolo.
    Normalizza i simboli per evitare mismatch BTC/USDT vs BTC/USDT:USDT.
    """
    try:
        target = normalize_symbol(symbol)
        positions = get_open_positions(exchange)
        for p in positions:
            p_symbol = normalize_symbol(p.get("symbol", ""))
            contracts = float(p.get("contracts") or 0)
            if p_symbol == target and contracts != 0:
                return True
        return False
    except Exception as e:
        logger.error(f"Errore has_open_position: {e}")
        return False                        
