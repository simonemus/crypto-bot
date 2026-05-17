# ============================================================
#  CRYPTO BREAKOUT BOT — bot.py
#  Loop principale: scansione segnali, gestione trade,
#  chiusura forzata, report serale.
# ============================================================

import time
import logging
import logging.handlers
from datetime import datetime, timezone, timedelta

import config
from binance_api import (
    get_exchange, fetch_ohlcv, add_indicators,
    get_previous_day_hl, check_breakout, check_retest,
    detect_pattern, trend_ok, atr_ok,
    calc_sl_tp, calc_quantity,
    place_market_order, place_sl_tp_orders,
    close_position_market, cancel_all_orders,
    get_balance_usdt, get_ticker_price,
    _update_sl_order, check_signal_decay,
    set_leverage_all, has_open_position,
    get_exchange_with_retry,
)
from telegram_bot import send_message, send_error
from database import (
    log_signal, log_trade_open, log_trade_close,
    log_equity, get_config_param, set_config_param,
    get_daily_trade_count, get_open_trade,
    save_breakout, load_breakouts, clear_breakout, clear_all_breakouts,
    get_open_trades,
)

# ── LOGGING ───────────────────────────────────────────────────
logger = logging.getLogger()
logger.setLevel(config.LOG_LEVEL)

formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s — %(message)s")

fh = logging.handlers.RotatingFileHandler(config.LOG_FILE, maxBytes=5_000_000, backupCount=3)
fh.setFormatter(formatter)

ch = logging.StreamHandler()
ch.setFormatter(formatter)

logger.addHandler(fh)
logger.addHandler(ch)

# ── STATO RUNTIME ─────────────────────────────────────────────
BOT_RUNNING   = False
open_trades   = {}   # {symbol: {direction, entry, sl, tp, qty, order_id}}
breakout_seen = {}   # {symbol: direction}  — breakout confermato, attesa retest
last_pattern_candle = {}   # {symbol: timestamp}  — evita di controllare la stessa candela 5m due volte
trailing_state = {}       # {symbol: {'breakeven_hit': bool, 'highest': float, 'lowest': float}}

# ── UTILITÀ ORARIO ────────────────────────────────────────────

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def is_weekend() -> bool:
    return now_utc().weekday() >= 5   # 5=sabato, 6=domenica


def in_session() -> bool:
    now = now_utc()
    if config.WEEKEND_FILTER and is_weekend():
        return False
    return config.SESSION_START_HOUR <= now.hour < config.SESSION_END_HOUR


def is_force_close_time() -> bool:
    now = now_utc()
    return now.hour == config.FORCE_CLOSE_HOUR and now.minute >= config.FORCE_CLOSE_MINUTE


def is_report_time() -> bool:
    now = now_utc()
    return now.hour == config.REPORT_HOUR and now.minute == config.REPORT_MINUTE


# ── CORE LOGIC ────────────────────────────────────────────────

def scan_symbol(exchange, symbol: str, rr: float) -> None:
    """
    Scansiona un simbolo: rileva breakout su 15m, retest su 5m,
    pattern candele, filtra ATR + trend, apre il trade.
    """
    global open_trades, breakout_seen

    # Già in posizione → skip
    if symbol in open_trades:
        return

    # Verifica posizioni aperte su Binance
    if has_open_position(exchange, symbol):
        logger.info(f"{symbol} — posizione già aperta su Binance, skip")
        return

    # Limite trade/giorno
    daily_count = get_daily_trade_count(symbol)
    if daily_count >= config.MAX_TRADES_PER_DAY_PER_ASSET:
        return

    try:
        # --- dati 15m con indicatori ---
        df_15 = fetch_ohlcv(exchange, symbol, config.TF_SIGNAL, limit=120)
        df_15 = add_indicators(df_15)

        pdh, pdl = get_previous_day_hl(exchange, symbol)

        # --- Breakout su 15m ---
        if symbol not in breakout_seen:
            direction = check_breakout(df_15, pdh, pdl)
            if not direction:
                return

            if not trend_ok(df_15, direction):
                logger.info(f"{symbol} breakout {direction} — filtro TREND fallito")
                return

            breakout_seen[symbol] = direction
            save_breakout(symbol, direction)
            send_message(
                f"🔍 *Segnale rilevato* — {symbol}\n"
                f"Direzione: *{direction.upper()}*\n"
                f"PDH: {pdh:.4f}  PDL: {pdl:.4f}\n"
                f"Attendo retest su {config.TF_ENTRY}…"
            )
            log_signal(symbol, direction, pdh, pdl)
            return

        direction = breakout_seen.get(symbol)

        # --- Controllo decadimento segnale ---
        current_price = get_ticker_price(exchange, symbol)
        if check_signal_decay(current_price, pdh, pdl, direction, config.SIGNAL_DECAY_BUFFER):
            logger.info(f"{symbol} — segnale decaduto, prezzo troppo lontano dal livello")
            del breakout_seen[symbol]
            clear_breakout(symbol)
            send_message(f"⚠️ Segnale decaduto — {symbol}\nIl prezzo si è allontanato troppo dal livello rotto.")
            return

        # --- Retest su 5m ---
        df_5 = fetch_ohlcv(exchange, symbol, config.TF_ENTRY, limit=60)
        df_5 = add_indicators(df_5)

        if not check_retest(df_5, pdh, pdl, direction):
            return

        # --- Pattern candele su 5m ---
        last_candle_time = df_5.index[-2]
        if last_pattern_candle.get(symbol) == last_candle_time:
            return
        last_pattern_candle[symbol] = last_candle_time

        pattern = detect_pattern(df_5, direction)
        if not pattern:
            last = df_5.iloc[-2]
            prev = df_5.iloc[-3]
            logger.info(
                f"{symbol} retest ok ma nessun pattern — skip\n"
                f"  candela corrente: open={last['open']:.4f} high={last['high']:.4f} "
                f"low={last['low']:.4f} close={last['close']:.4f}\n"
                f"  candela precedente: open={prev['open']:.4f} high={prev['high']:.4f} "
                f"low={prev['low']:.4f} close={prev['close']:.4f}"
            )
            return

        # --- Calcolo entry / SL / TP ---
        entry = get_ticker_price(exchange, symbol)
        atr_val = float(df_15.iloc[-2]["atr"])
        sl, tp  = calc_sl_tp(entry, direction, atr_val, rr)

        if not atr_ok(df_15, entry, sl):
            logger.info(f"{symbol} — filtro ATR fallito (SL troppo lontano)")
            return

        # --- Dimensionamento posizione ---
        capital = get_balance_usdt(exchange)
        qty = calc_quantity(exchange, symbol, entry, sl, capital, config.RISK_PER_TRADE_PCT)
        if qty <= 0:
            logger.warning(f"{symbol} — qty calcolata = 0, skip")
            return

        # --- Apertura ordine market ---
        side = "buy" if direction == "long" else "sell"
        try:
            order = place_market_order(exchange, symbol, side, qty)
            logger.info(f"Ordine aperto: {order}")
        except Exception as order_err:
            logger.error(f"Errore apertura ordine {symbol}: {order_err}")
            # Verifica se la posizione è stata aperta su Binance
            if has_open_position(exchange, symbol):
                logger.info(f"{symbol} — posizione aperta su Binance nonostante errore, continuo")
                order = {"id": None}
            else:
                logger.info(f"{symbol} — posizione non aperta su Binance, skip")
                return

        # --- OCO per SL/TP ---
        oco_side = "sell" if direction == "long" else "buy"
        oco = place_sl_tp_orders(exchange, symbol, oco_side, qty, tp, sl)

        open_trades[symbol] = {
            "direction": direction,
            "entry":     entry,
            "sl":        sl,
            "tp":        tp,
            "qty":       qty,
            "atr":       atr_val,
            "order_id":  order.get("id"),
            "oco_id":    oco.get("id"),
            "pattern":   pattern,
        }
        del breakout_seen[symbol]
        clear_breakout(symbol)

        # Calcola e salva il buffer dinamico usato al momento del breakout
        atr_pct = atr_val / entry
        buf_used = round(max(0.0020, atr_pct * 1.5) * 100, 4)
        log_trade_open(symbol, direction, entry, sl, tp, qty, pattern, atr=atr_val, breakout_buffer=buf_used)
        send_message(
            f"📈 Ordine aperto — {symbol}\n"
            f"Direzione: {direction.upper()}\n"
            f"Entry: {entry:.4f} | SL: {sl:.4f} | TP: {tp:.4f}\n"
            f"Qty: {qty} | Pattern: {pattern}\n"
            f"RR: {rr}"
        )

    except Exception as e:
        logger.error(f"Errore scan {symbol}: {e}", exc_info=True)
        send_error(f"⚠️ Errore scan {symbol}: {e}")


def monitor_open_trades(exchange) -> None:
    """
    Monitora i trade aperti: controlla se SL o TP sono stati raggiunti
    (come fallback se l'OCO non è disponibile su testnet).
    """
    global open_trades
    to_remove = []

    for symbol, trade in open_trades.items():
        try:
            price = get_ticker_price(exchange, symbol)
            update_trailing_stop(exchange, symbol, trade)
            direction = trade["direction"]
            hit = None

            if direction == "long":
                if price <= trade["sl"]:
                    hit = "sl"
                elif price >= trade["tp"]:
                    hit = "tp"
            else:
                if price >= trade["sl"]:
                    hit = "sl"
                elif price <= trade["tp"]:
                    hit = "tp"

            if hit:
                cancel_all_orders(exchange, symbol)
                close_position_market(exchange, symbol, direction, trade["qty"])
                pnl_pct = ((price - trade["entry"]) / trade["entry"] * 100)
                if direction == "short":
                    pnl_pct = -pnl_pct

                log_trade_close(symbol, price, hit, round(pnl_pct, 2))
                to_remove.append(symbol)

                if hit == "tp":
                    send_message(
                        f"✅ *Target raggiunto* — {symbol}\n"
                        f"Exit: `{price:.4f}` | PnL: +{pnl_pct:.2f}%"
                    )
                else:
                    send_message(
                        f"🔴 *Stop Loss* — {symbol}\n"
                        f"Exit: `{price:.4f}` | PnL: {pnl_pct:.2f}%"
                    )

        except Exception as e:
            logger.error(f"Errore monitor {symbol}: {e}", exc_info=True)

    for sym in to_remove:
        del open_trades[sym]


def force_close_all(exchange) -> None:
    global open_trades
    if not open_trades:
        return

    logger.info("Chiusura forzata — chiudo tutte le posizioni")
    send_message("⚠️ Chiusura forzata — chiudo tutte le posizioni aperte")

    for symbol, trade in list(open_trades.items()):
        try:
            cancel_all_orders(exchange, symbol)
            price = get_ticker_price(exchange, symbol)
            close_position_market(exchange, symbol, trade["direction"], trade["qty"])

            pnl_pct = (price - trade["entry"]) / trade["entry"] * 100
            if trade["direction"] == "short":
                pnl_pct = -pnl_pct
            pnl_pct = round(pnl_pct, 2)

            # Determina il risultato in base al PnL
            if pnl_pct > 0:
                result = "tp"
                emoji = "✅"
            elif pnl_pct < 0:
                result = "sl"
                emoji = "🔴"
            else:
                result = "force_close"
                emoji = "⚠️"

            log_trade_close(symbol, price, result, pnl_pct)
            sign = "+" if pnl_pct >= 0 else ""
            send_message(
                f"{emoji} *Force Close* — {symbol}\n"
                f"Exit: `{price:.4f}` | PnL: `{sign}{pnl_pct}%`\n"
                f"Risultato: {result}"
            )
        except Exception as e:
            logger.error(f"Errore force close {symbol}: {e}")
        finally:
            if symbol in open_trades:
                del open_trades[symbol]
            clear_breakout(symbol)

def update_trailing_stop(exchange, symbol: str, trade: dict) -> None:
    """
    Gestisce il trailing stop loss in due fasi:
    Fase 1 — Breakeven: quando il prezzo supera entry ± ATR × BREAKEVEN_MULTIPLIER
    Fase 2 — Trailing: lo SL segue il prezzo con distanza ATR × DISTANCE_MULTIPLIER
    """
    global trailing_state

    try:
        price = get_ticker_price(exchange, symbol)
        direction = trade["direction"]
        entry = trade["entry"]
        atr = trade.get("atr", 0)

        if atr == 0:
            return

        breakeven_trigger = atr * config.TRAILING_BREAKEVEN_MULTIPLIER
        trailing_dist = atr * config.TRAILING_DISTANCE_MULTIPLIER

        if symbol not in trailing_state:
            trailing_state[symbol] = {
                "breakeven_hit": False,
                "highest": price if direction == "long" else float("inf"),
                "lowest":  price if direction == "short" else float("inf"),
            }

        state = trailing_state[symbol]

        if direction == "long":
            # Aggiorna il massimo raggiunto
            if price > state["highest"]:
                state["highest"] = price

            if not state["breakeven_hit"]:
                # Fase 1 — Breakeven
                if price >= entry + breakeven_trigger:
                    new_sl = entry
                    state["breakeven_hit"] = True
                    logger.info(f"{symbol} TRAILING — Breakeven attivato, SL → {new_sl:.4f}")
                    _update_sl_order(exchange, symbol, new_sl, trade["qty"], direction)
                    trade["sl"] = new_sl
                    send_message(f"🔒 Breakeven — {symbol}\nSL spostato a entry: `{new_sl:.4f}`")
            else:
                # Fase 2 — Trailing
                new_sl = state["highest"] - trailing_dist
                if new_sl > trade["sl"]:
                    old_sl = trade["sl"]
                    logger.info(f"{symbol} TRAILING — SL aggiornato → {new_sl:.4f}")
                    _update_sl_order(exchange, symbol, new_sl, trade["qty"], direction)
                    trade["sl"] = new_sl
                    price = get_ticker_price(exchange, symbol)
                    pnl_pct = round((price - trade["entry"]) / trade["entry"] * 100, 2)
                    send_message(
                        f"📈 Trailing Stop aggiornato — {symbol} LONG\n"
                        f"Nuovo SL: {new_sl:.4f}\n"
                        f"Precedente SL: {old_sl:.4f}\n"
                        f"Massimo raggiunto: {state['highest']:.4f}\n"
                        f"PnL attuale: +{pnl_pct}%"
                    )

        else:  # short
            # Aggiorna il minimo raggiunto
            if price < state["lowest"]:
                state["lowest"] = price

            if not state["breakeven_hit"]:
                # Fase 1 — Breakeven
                if price <= entry - breakeven_trigger:
                    new_sl = entry
                    state["breakeven_hit"] = True
                    logger.info(f"{symbol} TRAILING — Breakeven attivato, SL → {new_sl:.4f}")
                    _update_sl_order(exchange, symbol, new_sl, trade["qty"], direction)
                    trade["sl"] = new_sl
                    send_message(f"🔒 Breakeven — {symbol}\nSL spostato a entry: `{new_sl:.4f}`")
            else:
                # Fase 2 — Trailing
                new_sl = state["lowest"] + trailing_dist
                if new_sl < trade["sl"]:
                    old_sl = trade["sl"]
                    logger.info(f"{symbol} TRAILING — SL aggiornato → {new_sl:.4f}")
                    _update_sl_order(exchange, symbol, new_sl, trade["qty"], direction)
                    trade["sl"] = new_sl
                    price = get_ticker_price(exchange, symbol)
                    pnl_pct = round((trade["entry"] - price) / trade["entry"] * 100, 2)
                    send_message(
                        f"📉 Trailing Stop aggiornato — {symbol} SHORT\n"
                        f"Nuovo SL: {new_sl:.4f}\n"
                        f"Precedente SL: {old_sl:.4f}\n"
                        f"Minimo raggiunto: {state['lowest']:.4f}\n"
                        f"PnL attuale: +{pnl_pct}%"
                    )

    except Exception as e:
        logger.error(f"Errore trailing stop {symbol}: {e}")            

def send_pnl_update(exchange) -> None:
    """Manda aggiornamento PnL delle posizioni aperte."""
    if not open_trades:
        return
    try:
        lines = ["📊 *Aggiornamento PnL*\n"]
        for symbol, trade in open_trades.items():
            price = get_ticker_price(exchange, symbol)
            pnl_pct = (price - trade["entry"]) / trade["entry"] * 100
            if trade["direction"] == "short":
                pnl_pct = -pnl_pct
            pnl_pct = round(pnl_pct, 2)
            sign = "+" if pnl_pct >= 0 else ""
            lines.append(
                f"*{symbol}* {trade['direction'].upper()}\n"
                f"Entry: `{trade['entry']:.4f}` | Live: `{price:.4f}`\n"
                f"PnL: `{sign}{pnl_pct}%`\n"
                f"SL: `{trade['sl']:.4f}` | TP: `{trade['tp']:.4f}`\n"
                f"Pattern: `{trade.get('pattern', 'N/D')}`"
            )
        send_message("\n".join(lines))
    except Exception as e:
        logger.error(f"Errore send_pnl_update: {e}")            

def send_evening_report(exchange) -> None:
    """Invia il report serale delle 22:00."""
    try:
        balance = get_balance_usdt(exchange)
        log_equity(balance)

        from database import get_today_trades
        trades = get_today_trades()
        wins   = [t for t in trades if t.get("result") == "tp"]
        losses = [t for t in trades if t.get("result") == "sl"]
        force  = [t for t in trades if t.get("result") == "force_close"]
        total  = len(trades)
        winrate = round(len(wins) / total * 100, 1) if total else 0

        msg = (
            f"📊 *Report serale — {now_utc().strftime('%d/%m/%Y')}*\n"
            f"Trade oggi: {total} | ✅ Win: {len(wins)} | 🔴 Loss: {len(losses)} | ⚠️ Force: {len(force)}\n"
            f"Win rate: {winrate}%\n"
            f"Equity attuale: `{balance:.2f} USDT`"
        )
        send_message(msg)
    except Exception as e:
        logger.error(f"Errore report serale: {e}")
        send_error(f"⚠️ Errore report serale: {e}")


# ── MAIN LOOP ─────────────────────────────────────────────────

def run_bot() -> None:
    """Loop principale del bot — chiama start() da telegram_bot.py."""
    global BOT_RUNNING, breakout_seen
    BOT_RUNNING = True
    breakout_seen = load_breakouts()
    logger.info(f"Breakout caricati dal DB: {breakout_seen}")

    # Recupero trade aperti dopo riavvio
    trades_db = get_open_trades()
    if trades_db:
        for t in trades_db:
            symbol = t["symbol"]
            if symbol not in open_trades:
                open_trades[symbol] = t
                logger.info(f"Trade recuperato dal DB: {symbol} {t['direction']} entry={t['entry']}")
                send_message(
                    f"♻️ Trade recuperato dopo riavvio — {symbol}\n"
                    f"Direzione: {t['direction'].upper()}\n"
                    f"Entry: {t['entry']:.4f} | SL: {t['sl']:.4f} | TP: {t['tp']:.4f}"
                )

    logger.info("=== BOT AVVIATO ===")
    send_message("🟢 Bot avviato")

    exchange = get_exchange_with_retry()
    set_leverage_all(exchange, config.SYMBOLS, leverage=2)
    report_sent_today = False
    force_closed_today = False
    last_pnl_notify = None
    heartbeat_sent_today = False

    while BOT_RUNNING:
        try:
            now = now_utc()

            # Reset giornaliero
            if now.hour == 0 and now.minute < 2:
                breakout_seen = {}
                clear_all_breakouts()
                report_sent_today = False
                force_closed_today = False
                heartbeat_sent_today = False

            # Notifica PnL oraria
            if open_trades:
                now = now_utc()
                if last_pnl_notify is None or \
                   (now - last_pnl_notify).seconds >= config.PNL_NOTIFY_INTERVAL_MINUTES * 60:
                    send_pnl_update(exchange)
                    last_pnl_notify = now

            # Heartbeat mattutino
            if now.hour == 6 and now.minute == 50 and not heartbeat_sent_today:
                balance = get_balance_usdt(exchange)
                send_message(
                    f"🟢 Bot attivo — {now.strftime('%d/%m/%Y')}\n"
                    f"Sessione: tra 10 minuti (07:00 UTC)\n"
                    f"Equity: {balance:.2f} USDT\n"
                    f"Asset: {' | '.join(config.SYMBOLS)}\n"
                    f"Leva: 2x"
                )
                heartbeat_sent_today = True

            # Heartbeat mattutino
            if now.hour == 6 and now.minute == 50 and not heartbeat_sent_today:
                balance = get_balance_usdt(exchange)
                send_message(
                    f"🟢 Bot attivo — {now.strftime('%d/%m/%Y')}\n"
                    f"Sessione: tra 10 minuti (07:00 UTC)\n"
                    f"Equity: {balance:.2f} USDT\n"
                    f"Asset: {' | '.join(config.SYMBOLS)}\n"
                    f"Leva: 2x"
                )
                heartbeat_sent_today = True                

            # Report serale
            if is_report_time() and not report_sent_today:
                send_evening_report(exchange)
                report_sent_today = True

            # Chiusura forzata
            if is_force_close_time() and not force_closed_today:
                force_close_all(exchange)
                force_closed_today = True

            # Sessione attiva
            if in_session():
                rr = float(get_config_param("rr") or config.RISK_REWARD_RATIO)
                for symbol in config.SYMBOLS:
                    logger.info(f"Scansione — {symbol} — {now_utc().strftime('%H:%M:%S')} UTC")
                    scan_symbol(exchange, symbol, rr)
                monitor_open_trades(exchange)
            else:
                # Fuori sessione: monitora solo eventuali trade aperti
                if open_trades:
                    monitor_open_trades(exchange)

        except Exception as e:
            logger.error(f"Errore nel loop principale: {e}", exc_info=True)
            send_message(
                f"🚨 Errore critico nel loop\n"
                f"Errore: {str(e)[:200]}\n"
                f"Il bot continua a girare..."
            )

        time.sleep(30)   # pausa 30 secondi tra ogni ciclo

    logger.info("=== BOT FERMATO ===")
    send_message("🚨 Bot fermato inaspettatamente — verifica su Railway!")


def stop_bot() -> None:
    global BOT_RUNNING
    BOT_RUNNING = False


if __name__ == "__main__":
    run_bot()
