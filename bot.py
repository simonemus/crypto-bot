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
    place_market_order, place_oco_order,
    close_position_market, cancel_all_orders,
    get_balance_usdt, get_ticker_price,
)
from telegram_bot import send_message, send_error
from database import (
    log_signal, log_trade_open, log_trade_close,
    log_equity, get_config_param, set_config_param,
    get_daily_trade_count, get_open_trade,
    save_breakout, load_breakouts, clear_breakout, clear_all_breakouts,
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

        # --- Retest su 5m ---
        df_5 = fetch_ohlcv(exchange, symbol, config.TF_ENTRY, limit=60)
        df_5 = add_indicators(df_5)

        if not check_retest(df_5, pdh, pdl, direction):
            return

        # --- Pattern candele su 5m ---
        pattern = detect_pattern(df_5, direction)
        if not pattern:
            logger.info(f"{symbol} retest ok ma nessun pattern — skip")
            return

        # --- Calcolo entry / SL / TP ---
        entry = get_ticker_price(exchange, symbol)
        atr_val = float(df_15.iloc[-2]["atr"])
        sl, tp  = calc_sl_tp(entry, direction, atr_val, rr)

        if not atr_ok(df_15, entry, sl):
            logger.info(f"{symbol} — filtro ATR fallito (SL troppo lontano)")
            del breakout_seen[symbol]
            clear_breakout(symbol)
            return

        # --- Dimensionamento posizione ---
        capital = get_balance_usdt(exchange)
        qty = calc_quantity(exchange, symbol, entry, sl, capital, config.RISK_PER_TRADE_PCT)
        if qty <= 0:
            logger.warning(f"{symbol} — qty calcolata = 0, skip")
            return

        # --- Apertura ordine market ---
        side = "buy" if direction == "long" else "sell"
        order = place_market_order(exchange, symbol, side, qty)
        logger.info(f"Ordine aperto: {order}")

        # --- OCO per SL/TP ---
        oco_side = "sell" if direction == "long" else "buy"
        try:
            oco = place_oco_order(exchange, symbol, oco_side, qty, tp, sl)
        except Exception as oco_err:
            logger.error(f"OCO fallito, gestione SL/TP manuale: {oco_err}")
            oco = {}

        open_trades[symbol] = {
            "direction": direction,
            "entry":     entry,
            "sl":        sl,
            "tp":        tp,
            "qty":       qty,
            "order_id":  order.get("id"),
            "oco_id":    oco.get("id"),
            "pattern":   pattern,
        }
        del breakout_seen[symbol]
        clear_breakout(symbol)

        # --- DB + notifica ---
        log_trade_open(symbol, direction, entry, sl, tp, qty, pattern)
        send_message(
            f"📈 *Ordine aperto* — {symbol}\n"
            f"Direzione: *{direction.upper()}*\n"
            f"Entry: `{entry:.4f}` | SL: `{sl:.4f}` | TP: `{tp:.4f}`\n"
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

    logger.info("Chiusura forzata 23:55 — chiudo tutte le posizioni")
    send_message("⚠️ Chiusura forzata 23:55 — chiudo tutte le posizioni aperte")

    for symbol, trade in list(open_trades.items()):
        try:
            cancel_all_orders(exchange, symbol)
            price = get_ticker_price(exchange, symbol)
            close_position_market(exchange, symbol, trade["direction"], trade["qty"])

            pnl_pct = (price - trade["entry"]) / trade["entry"] * 100
            if trade["direction"] == "short":
                pnl_pct = -pnl_pct
            pnl_pct = round(pnl_pct, 2)

            log_trade_close(symbol, price, "force_close", pnl_pct)

            sign = "+" if pnl_pct >= 0 else ""
            send_message(
                f"⚠️ *Force Close* — {symbol}\n"
                f"Exit: `{price:.4f}` | PnL: `{sign}{pnl_pct}%`"
            )
        except Exception as e:
            logger.error(f"Errore force close {symbol}: {e}")
        finally:
            del open_trades[symbol]
            clear_breakout(symbol)

def send_evening_report(exchange) -> None:
    """Invia il report serale delle 22:00."""
    try:
        balance = get_balance_usdt(exchange)
        log_equity(balance)

        # Recupera i trade del giorno da DB
        from database import get_today_trades
        trades = get_today_trades()
        wins   = [t for t in trades if t.get("result") == "tp"]
        losses = [t for t in trades if t.get("result") == "sl"]

        msg = (
            f"📊 *Report serale — {now_utc().strftime('%d/%m/%Y')}*\n"
            f"Trade oggi: {len(trades)} | ✅ Win: {len(wins)} | 🔴 Loss: {len(losses)}\n"
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

    logger.info("=== BOT AVVIATO ===")
    send_message("🟢 Bot avviato")

    exchange = get_exchange()
    report_sent_today = False
    force_closed_today = False

    while BOT_RUNNING:
        try:
            now = now_utc()

            # Reset giornaliero
            if now.hour == 0 and now.minute < 2:
                breakout_seen = {}
                clear_all_breakouts()
                report_sent_today = False
                force_closed_today = False

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
            send_error(f"⚠️ Errore loop: {e}")

        time.sleep(30)   # pausa 30 secondi tra ogni ciclo

    logger.info("=== BOT FERMATO ===")
    send_message("🔴 Bot fermato")


def stop_bot() -> None:
    global BOT_RUNNING
    BOT_RUNNING = False


if __name__ == "__main__":
    run_bot()
