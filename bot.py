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
    place_market_order, place_tp_order, place_sl_order, place_trailing_order,
    close_position_market, cancel_all_orders, cancel_algo_orders,
    get_balance_usdt, get_ticker_price,
    check_signal_decay, set_leverage_all,
    has_open_position, get_exchange_with_retry,
)
from telegram_bot import send_message, send_error
from database import (
    log_signal, log_trade_open, log_trade_close,
    log_equity, get_config_param, set_config_param,
    get_daily_trade_count,
    save_breakout, load_breakouts, clear_breakout, clear_all_breakouts,
    get_open_trades,
    save_decay_cooldown, load_decay_cooldowns, clear_decay_cooldown,
    get_weekly_trades, get_daily_pnl,
    increment_filter_stat,
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
decay_cooldown = {}       # {symbol: timestamp} — cooldown dopo decadimento segnale
proximity_alerted = {}    # {symbol: 'pdh'|'pdl'|None} — evita notifiche ripetute
daily_loss_blocked = False  # True se daily max loss raggiunto

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

    # Daily max loss raggiunto → skip
    if daily_loss_blocked:
        logger.info(f"{symbol} — daily max loss raggiunto, skip")
        return    

    # Cooldown dopo decadimento — aspetta 15 minuti prima di rilevare nuovo breakout
    if symbol in decay_cooldown:
        elapsed = (now_utc() - decay_cooldown[symbol]).total_seconds()
        if elapsed < 3600:  # 60 minuti
            remaining = int((3600 - elapsed) // 60)
            logger.info(f"{symbol} — cooldown attivo, riprendo tra {remaining} minuti")
            return
        else:
            del decay_cooldown[symbol]
            clear_decay_cooldown(symbol)    

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
                increment_filter_stat(symbol, "breakout_rilevati")
                increment_filter_stat(symbol, "scartati_trend")
                return

            increment_filter_stat(symbol, "breakout_rilevati")
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
            decay_cooldown[symbol] = now_utc()
            save_decay_cooldown(symbol)
            increment_filter_stat(symbol, "scartati_decadimento")
            send_message(f"⚠️ Segnale decaduto — {symbol}\nIl prezzo si è allontanato troppo dal livello rotto.")
            return

        # --- Retest su 5m ---
        df_5 = fetch_ohlcv(exchange, symbol, config.TF_ENTRY, limit=60)
        df_5 = add_indicators(df_5)

        if not check_retest(df_5, pdh, pdl, direction):
            return

        increment_filter_stat(symbol, "arrivati_retest")

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
            increment_filter_stat(symbol, "scartati_pattern")
            return

        # --- Calcolo entry / SL / TP ---
        entry = get_ticker_price(exchange, symbol)
        atr_val = float(df_15.iloc[-2]["atr"])
        sl, _ = calc_sl_tp(entry, direction, atr_val, rr)

        if not atr_ok(df_15, entry, sl):
            logger.info(f"{symbol} — filtro ATR fallito (SL troppo lontano)")
            increment_filter_stat(symbol, "scartati_atr")
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

        # --- TP fisso a RR 3.0 + Trailing piazzato dopo a +1R ---
        oco_side = "sell" if direction == "long" else "buy"

        # Calcola TP a RR 3.0
        sl_dist = abs(entry - sl)
        if direction == "long":
            tp = round(entry + sl_dist * config.TP_RR, 6)
            activation_price = round(entry + sl_dist, 2)
        else:
            tp = round(entry - sl_dist * config.TP_RR, 6)
            activation_price = round(entry - sl_dist, 2)

        # Piazza TP e SL — trailing verrà piazzato in monitor_open_trades
        place_tp_order(exchange, symbol, oco_side, qty, tp)
        place_sl_order(exchange, symbol, oco_side, qty, sl)

        open_trades[symbol] = {
            "direction":        direction,
            "entry":            entry,
            "sl":               sl,
            "tp":               tp,
            "qty":              qty,
            "atr":              atr_val,
            "order_id":         order.get("id"),
            "pattern":          pattern,
            "activation_price": activation_price,
            "trailing_placed":  False,
        }
        del breakout_seen[symbol]
        clear_breakout(symbol)

        # Calcola e salva il buffer dinamico usato al momento del breakout
        atr_pct = atr_val / entry
        buf_used = round(max(0.0020, atr_pct * 1.5) * 100, 4)
        log_trade_open(symbol, direction, entry, sl, tp, qty, pattern, atr=atr_val, breakout_buffer=buf_used)
        increment_filter_stat(symbol, "trade_aperti")
        send_message(
            f"📈 Ordine aperto — {symbol}\n"
            f"Direzione: {direction.upper()}\n"
            f"Entry: {entry:.4f} | SL: {sl:.4f} | TP: {tp:.4f}\n"
            f"Qty: {qty} | Pattern: {pattern.replace('_', ' ')}\n"
            f"RR: {config.TP_RR} | Trailing da: {activation_price:.4f}"
        )

    except Exception as e:
        logger.error(f"Errore scan {symbol}: {e}", exc_info=True)
        send_error(f"⚠️ Errore scan {symbol}: {e}")


def monitor_open_trades(exchange) -> None:
    """
    Monitora i trade aperti: controlla se SL o TP sono stati raggiunti.
    """
    global open_trades, daily_loss_blocked
    to_remove = []

    for symbol, trade in open_trades.items():
        try:
            price = get_ticker_price(exchange, symbol)
            direction = trade["direction"]
            hit = None
            oco_side = "sell" if direction == "long" else "buy"

            # Piazza trailing quando prezzo raggiunge +1R
            if not trade.get("trailing_placed", False):
                activation = trade.get("activation_price")
                if activation:
                    if direction == "long" and price >= activation:
                        place_trailing_order(exchange, symbol, oco_side, trade["qty"], trade["atr"], activation)
                        open_trades[symbol]["trailing_placed"] = True
                        send_message(f"🎯 Trailing attivato — {symbol}\nPrezzo: {price:.4f} | Activation: {activation:.4f}")
                    elif direction == "short" and price <= activation:
                        place_trailing_order(exchange, symbol, oco_side, trade["qty"], trade["atr"], activation)
                        open_trades[symbol]["trailing_placed"] = True
                        send_message(f"🎯 Trailing attivato — {symbol}\nPrezzo: {price:.4f} | Activation: {activation:.4f}")

            # Controlla SL e TP fissi
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

            # Dopo 5 minuti dall'apertura controlla se Binance ha chiuso la posizione
            if hit is None:
                if "opened_at_dt" not in trade:
                    trade["opened_at_dt"] = now_utc()
                trade_age = (now_utc() - trade["opened_at_dt"]).total_seconds()
                if trade_age > 300 and not has_open_position(exchange, symbol):
                    hit = "closed_by_binance"

            if hit:
                # Cancella ordini normali e Algo — ignora errori se Binance ha già chiuso
                try:
                    cancel_all_orders(exchange, symbol)
                except Exception:
                    pass
                try:
                    cancel_algo_orders(exchange, symbol)
                except Exception:
                    pass
                try:
                    close_position_market(exchange, symbol, direction, trade["qty"])
                except Exception:
                    pass

                pnl_pct = ((price - trade["entry"]) / trade["entry"] * 100)
                if direction == "short":
                    pnl_pct = -pnl_pct
                pnl_pct = round(pnl_pct, 2)

                # Determina exit_reason
                if hit == "tp":
                    exit_reason = "tp"
                    msg = f"✅ Take Profit — {symbol}\nExit: {price:.4f} | PnL: +{pnl_pct}%"
                elif hit == "sl":
                    exit_reason = "sl"
                    msg = f"🔴 Stop Loss — {symbol}\nExit: {price:.4f} | PnL: {pnl_pct}%"
                elif hit == "closed_by_binance":
                    if pnl_pct > 0:
                        exit_reason = "trailing_win"
                        msg = f"📈 Trailing Win — {symbol}\nExit: {price:.4f} | PnL: +{pnl_pct}%"
                    elif pnl_pct < 0:
                        exit_reason = "trailing_loss"
                        msg = f"📉 Trailing Loss — {symbol}\nExit: {price:.4f} | PnL: {pnl_pct}%"
                    else:
                        exit_reason = "breakeven"
                        msg = f"⚖️ Breakeven — {symbol}\nExit: {price:.4f} | PnL: {pnl_pct}%"

                log_trade_close(symbol, price, exit_reason, pnl_pct, exit_reason=exit_reason)
                to_remove.append(symbol)
                send_message(msg)

                # Controlla daily max loss
                max_loss = float(get_config_param("max_loss") or config.DAILY_MAX_LOSS_PCT)
                daily_pnl = get_daily_pnl()
                if daily_pnl <= -max_loss:
                    daily_loss_blocked = True
                    send_message(
                        f"🚨 *DAILY MAX LOSS RAGGIUNTO*\n"
                        f"Perdita giornaliera: `{daily_pnl:.2f}%` su limite `-{max_loss:.1f}%`\n"
                        f"Nessun nuovo trade fino a domani."
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
            cancel_algo_orders(exchange, symbol)
            price = get_ticker_price(exchange, symbol)
            close_position_market(exchange, symbol, trade["direction"], trade["qty"])

            pnl_pct = (price - trade["entry"]) / trade["entry"] * 100
            if trade["direction"] == "short":
                pnl_pct = -pnl_pct
            pnl_pct = round(pnl_pct, 2)

            # Determina exit_reason
            if pnl_pct > 0:
                exit_reason = "force_close_win"
                emoji = "⚠️"
            elif pnl_pct < 0:
                exit_reason = "force_close_loss"
                emoji = "⚠️"
            else:
                exit_reason = "breakeven"
                emoji = "⚖️"

            log_trade_close(symbol, price, exit_reason, pnl_pct, exit_reason=exit_reason)
            sign = "+" if pnl_pct >= 0 else ""
            send_message(
                f"{emoji} *Force Close* — {symbol}\n"
                f"Exit: `{price:.4f}` | PnL: `{sign}{pnl_pct}%`\n"
                f"Risultato: {exit_reason}"
            )
        except Exception as e:
            logger.error(f"Errore force close {symbol}: {e}")
        finally:
            if symbol in open_trades:
                del open_trades[symbol]
            clear_breakout(symbol)


def check_proximity_alert(exchange, symbol: str, pdh: float, pdl: float, atr: float = 0) -> None:
    """
    Controlla se il prezzo si avvicina al livello di breakout entro la soglia configurata.
    """
    global proximity_alerted
    from datetime import datetime, timezone, timedelta

    try:
        price = get_ticker_price(exchange, symbol)
        now_it = datetime.now(timezone.utc) + timedelta(hours=2)
        now_str = now_it.strftime("%d/%m/%Y %H:%M")

        # Calcola buffer dinamico ATR
        atr_pct = atr / price if atr > 0 else 0
        buffer_pct = max(0.0020, atr_pct * 1.5)

        # Livelli di breakout
        breakout_long = pdh * (1 + buffer_pct)
        breakout_short = pdl * (1 - buffer_pct)

        # Distanza dal prezzo live al livello di breakout
        dist_to_long = (breakout_long - price) / price
        dist_to_short = (price - breakout_short) / price

        buf = config.PROXIMITY_ALERT_PCT / 100

        if 0 < dist_to_long < buf:
            if proximity_alerted.get(symbol) != "pdh":
                proximity_alerted[symbol] = "pdh"
                send_message(
                    f"⚡ {symbol} si avvicina al Breakout LONG\n"
                    f"Live: {price:,.2f} — {now_str}\n"
                    f"Breakout da: {breakout_long:,.2f}\n"
                    f"Manca: {round(dist_to_long * 100, 2)}%"
                )
        elif 0 < dist_to_short < buf:
            if proximity_alerted.get(symbol) != "pdl":
                proximity_alerted[symbol] = "pdl"
                send_message(
                    f"⚡ {symbol} si avvicina al Breakout SHORT\n"
                    f"Live: {price:,.2f} — {now_str}\n"
                    f"Breakout da: {breakout_short:,.2f}\n"
                    f"Manca: {round(dist_to_short * 100, 2)}%"
                )
        elif dist_to_long > buf and dist_to_short > buf:
            proximity_alerted[symbol] = None

    except Exception as e:
        logger.error(f"Errore check_proximity_alert {symbol}: {e}")                    

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
                f"Pattern: `{trade.get('pattern', 'N/D').replace('_', ' ')}`"
            )
        send_message("\n".join(lines))
    except Exception as e:
        logger.error(f"Errore send_pnl_update: {e}")

def send_weekly_report(exchange) -> None:
    """Invia il report settimanale ogni lunedì alle 8:50 italiane."""
    try:
        from datetime import date, timedelta
        from telegram_bot import _format_report
        today = date.today()
        last_monday = today - timedelta(days=today.weekday() + 7)
        if config.WEEKEND_FILTER:
            last_end = last_monday + timedelta(days=4)
        else:
            last_end = last_monday + timedelta(days=6)

        trades    = get_weekly_trades(str(last_monday), str(last_end))
        balance   = get_balance_usdt(exchange)
        year      = last_monday.year
        start_str = last_monday.strftime("%d/%m")
        end_str   = last_end.strftime("%d/%m")

        titolo = f"Report settimanale {year} — {start_str} al {end_str}"
        msg    = _format_report(trades, titolo)
        msg   += f"\nEquity: `{balance:.2f} USDT`"
        send_message(msg)
    except Exception as e:
        logger.error(f"Errore report settimanale: {e}")                    

def send_evening_report(exchange) -> None:
    """Invia il report serale delle 22:00."""
    try:
        balance = get_balance_usdt(exchange)
        log_equity(balance)

        from database import get_today_trades
        from telegram_bot import _format_report
        trades = get_today_trades()
        titolo = f"Report serale — {now_utc().strftime('%d/%m/%Y')}"
        msg    = _format_report(trades, titolo)
        msg   += f"\nEquity attuale: `{balance:.2f} USDT`"
        send_message(msg)
    except Exception as e:
        logger.error(f"Errore report serale: {e}")
        send_error(f"⚠️ Errore report serale: {e}")


# ── MAIN LOOP ─────────────────────────────────────────────────

def run_bot() -> None:
    """Loop principale del bot — chiama start() da telegram_bot.py."""
    global BOT_RUNNING, breakout_seen, decay_cooldown, daily_loss_blocked
    BOT_RUNNING = True
    breakout_seen = load_breakouts()
    logger.info(f"Breakout caricati dal DB: {breakout_seen}")
    decay_cooldown = load_decay_cooldowns()
    logger.info(f"Cooldown caricati dal DB: {decay_cooldown}")

    # Recupero trade aperti dopo riavvio
    trades_db = get_open_trades()
    if trades_db:
        for t in trades_db:
            symbol = t["symbol"]
            if symbol not in open_trades:
                # Ricalcola activation_price da entry e sl
                sl_dist = abs(t["entry"] - t["sl"])
                if t["direction"] == "long":
                    activation_price = round(t["entry"] + sl_dist, 2)
                else:
                    activation_price = round(t["entry"] - sl_dist, 2)
                t["activation_price"] = activation_price
                t["trailing_placed"] = True  # assumiamo trailing già piazzato
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
    weekly_report_sent = False

    while BOT_RUNNING:
        try:
            now = now_utc()

            # Reset giornaliero
            if now.hour == 0 and now.minute < 2:
                breakout_seen = {}
                last_pattern_candle.clear()
                clear_all_breakouts()
                report_sent_today = False
                force_closed_today = False
                heartbeat_sent_today = False
                weekly_report_sent = False
                daily_loss_blocked = False

                decay_cooldown = {}
                for sym in list(config.SYMBOLS):
                    clear_decay_cooldown(sym)

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

            # Report settimanale — ogni lunedì alle 6:50 UTC (8:50 italiane)
            if now.weekday() == 0 and now.hour == 6 and now.minute == 50 and not weekly_report_sent:
                send_weekly_report(exchange)
                weekly_report_sent = True
                
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
                    pdh, pdl = get_previous_day_hl(exchange, symbol)
                    df_15_prox = fetch_ohlcv(exchange, symbol, config.TF_SIGNAL, limit=20)
                    df_15_prox = add_indicators(df_15_prox)
                    atr_prox = float(df_15_prox.iloc[-2]["atr"])
                    check_proximity_alert(exchange, symbol, pdh, pdl, atr_prox)
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
