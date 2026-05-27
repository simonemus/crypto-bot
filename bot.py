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
    detect_pattern, trend_ok, classify_atr,
    calc_sl_tp, calc_quantity,
    place_market_order, place_tp_order, place_sl_order, place_trailing_order,
    close_position_market, cancel_all_orders, cancel_algo_orders, cancel_all_symbol_orders,
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

if not logger.handlers:
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
proximity_alerted = {}    # {symbol: {"pdh": ultima_soglia_notificata, "pdl": ultima_soglia_notificata}}
daily_loss_blocked = False  # True se daily max loss raggiunto

# ── UTILITÀ ORARIO ────────────────────────────────────────────

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def is_weekend() -> bool:
    return now_utc().weekday() >= 5   # 5=sabato, 6=domenica


def in_session() -> bool:
    from database import get_config_param
    now = now_utc()
    weekend_filter = get_config_param("weekend_filter")
    if weekend_filter is None:
        weekend_filter = config.WEEKEND_FILTER
    weekend_filter_enabled = str(weekend_filter).lower() == "true"
    if weekend_filter_enabled and is_weekend():
        return False
    return config.SESSION_START_HOUR <= now.hour < config.SESSION_END_HOUR


def is_force_close_time() -> bool:
    now = now_utc()
    return now.hour == config.FORCE_CLOSE_HOUR and now.minute >= config.FORCE_CLOSE_MINUTE


def is_report_time() -> bool:
    now = now_utc()
    return now.hour == config.REPORT_HOUR and now.minute == config.REPORT_MINUTE


# ── CORE LOGIC ────────────────────────────────────────────────

def scan_symbol(exchange, symbol: str) -> None:
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

    # Nessun nuovo trade dopo le 18:30 UTC (20:30 Italia)
    # Garantisce almeno 2h30 prima del force close alle 21:00 UTC
    _now = now_utc()
    if _now.hour > 18 or (_now.hour == 18 and _now.minute >= 30):
        logger.info(f"{symbol} — oltre l'ora di last entry (18:30 UTC), skip")
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

        # Filtro ATR% — verifica volatilità min/max
        atr_pct = float(df_15.iloc[-2]["atr_pct"])
        atr_class = classify_atr(symbol, atr_pct)
        logger.info(f"{symbol} — ATR%: {atr_pct:.3f}% — {atr_class}")
        if atr_class == "NO_TRADE_LOW_VOLATILITY":
            logger.info(f"{symbol} — ATR troppo basso, skip")
            increment_filter_stat(symbol, "scartati_atr")
            return
        if atr_class == "NO_TRADE_HIGH_VOLATILITY":
            logger.info(f"{symbol} — ATR troppo alto, skip")
            increment_filter_stat(symbol, "scartati_atr")
            return

        # SL e TP percentuali fissi
        sl, tp = calc_sl_tp(entry, direction)

        # --- Dimensionamento posizione ---
        qty = calc_quantity(exchange, symbol, entry)
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
                del breakout_seen[symbol]
                clear_breakout(symbol)
                decay_cooldown[symbol] = now_utc()
                save_decay_cooldown(symbol)
                send_message(f"⚠️ Ordine fallito — {symbol}\nErrore: {str(order_err)[:100]}\nCooldown attivo per 60 minuti.")
                return

        # --- Calcola activation price trailing ---
        oco_side = "sell" if direction == "long" else "buy"

        trailing_act_pct = float(get_config_param("trailing_activation_pct") or config.TRAILING_ACTIVATION_PCT * 100) / 100
        if direction == "long":
            activation_price = round(entry * (1 + trailing_act_pct), 6)
        else:
            activation_price = round(entry * (1 - trailing_act_pct), 6)

        # Piazza TP e SL — trailing verrà piazzato in monitor_open_trades
        tp_order = place_tp_order(exchange, symbol, oco_side, qty, tp)
        sl_order = place_sl_order(exchange, symbol, oco_side, qty, sl)

        if not sl_order:
            logger.error(f"{symbol} — SL non piazzato. Chiudo posizione per sicurezza.")
            cancel_all_symbol_orders(exchange, symbol)
            close_position_market(exchange, symbol, direction, qty)
            send_error(f"🚨 {symbol} — SL non piazzato. Posizione chiusa per sicurezza.")
            return

        if not tp_order:
            logger.warning(f"{symbol} — TP non piazzato. Trade aperto senza TP server-side.")

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
            "opened_at_dt":     now_utc(),
            "soft_check_done":  False,
        }
        del breakout_seen[symbol]
        clear_breakout(symbol)

        # Calcola e salva il buffer dinamico usato al momento del breakout
        atr_pct_buf = atr_val / entry
        buf_used = round(max(0.0020, atr_pct_buf * 1.5) * 100, 4)
        log_trade_open(symbol, direction, entry, sl, tp, qty, pattern, atr=atr_val, breakout_buffer=buf_used, atr_pct=atr_pct)
        increment_filter_stat(symbol, "trade_aperti")
        global last_pnl_notify
        last_pnl_notify = now_utc()
        send_message(
            f"📈 Ordine aperto — {symbol}\n"
            f"Direzione: {direction.upper()}\n"
            f"Entry: {entry:.4f} | SL: {sl:.4f} | TP: {tp:.4f}\n"
            f"Qty: {qty} | Pattern: {pattern.replace('_', ' ')}\n"
            f"SL: {config.SL_PCT*100:.1f}% | TP: {config.TP_PCT*100:.1f}% | "
            f"Trailing da: {activation_price:.4f} | ATR: {atr_pct:.3f}% {atr_emoji}"
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

            # Piazza trailing quando prezzo raggiunge +1.5%
            if not trade.get("trailing_placed", False):
                activation = trade.get("activation_price")
                if activation:
                    should_place = (
                        (direction == "long" and price >= activation) or
                        (direction == "short" and price <= activation)
                    )
                    if should_place:
                        trailing_order = place_trailing_order(
                            exchange, symbol, oco_side,
                            trade["qty"], activation
                        )
                        if trailing_order:
                            open_trades[symbol]["trailing_placed"] = True
                            send_message(
                                f"🎯 Trailing attivato — {symbol}\n"
                                f"Prezzo: {price:.4f} | Activation: {activation:.4f}\n"
                                f"Callback: {config.TRAILING_CALLBACK.get(symbol, 0.6)}%"
                            )
                        else:
                            logger.error(f"{symbol} — trailing NON piazzato, riprovo al prossimo ciclo")
                            send_error(f"⚠️ {symbol} — trailing non piazzato, SL fisso resta attivo")

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

            # ── DURATA MASSIMA TRADE ─────────────────────────────
            if hit is None and not trade.get("trailing_placed", False):
                trade_age_min = (now_utc() - trade["opened_at_dt"]).total_seconds() / 60

                soft_check_min = float(
                    get_config_param("trade_soft_check_minutes") or config.TRADE_SOFT_CHECK_MINUTES
                )
                hard_exit_min = float(
                    get_config_param("trade_max_duration_minutes") or config.TRADE_MAX_DURATION_MINUTES
                )
                min_progress = float(
                    get_config_param("trade_min_progress_pct") or config.TRADE_MIN_PROGRESS_PCT
                )

                # HARD EXIT — 4 ore
                if trade_age_min >= hard_exit_min:
                    hit = "time_exit_hard"
                    logger.info(f"{symbol} — HARD EXIT: trade aperto da {trade_age_min:.0f} minuti")

                # SOFT CHECK — esattamente a 2 ore, una sola volta
                elif trade_age_min >= soft_check_min and not trade.get("soft_check_done", False):
                    trade["soft_check_done"] = True
                    pnl_live = ((price - trade["entry"]) / trade["entry"] * 100) * config.LEVERAGE
                    if trade["direction"] == "short":
                        pnl_live = -pnl_live
                    if pnl_live < min_progress:
                        hit = "time_exit_soft"
                        logger.info(
                            f"{symbol} — SOFT EXIT: dopo {trade_age_min:.0f} min "
                            f"progresso {pnl_live:.2f}% < {min_progress}% richiesto"
                        )
                    else:
                        logger.info(
                            f"{symbol} — Soft check OK: +{pnl_live:.2f}% dopo {trade_age_min:.0f} min"
                        )
                        send_message(
                            f"⏱ Soft check OK — {symbol}\n"
                            f"Dopo {int(soft_check_min)} min: +{pnl_live:.2f}% ✅\n"
                            f"Trade continua…"
                        )

            # Controlla se Binance ha chiuso la posizione (server-side SL/TP/trailing)
            if hit is None:
                trade_age = (now_utc() - trade["opened_at_dt"]).total_seconds()
                if trade_age > 300 and not has_open_position(exchange, symbol):
                    hit = "closed_by_binance"

            if hit:
                # Cancella ordini normali e Algo
                try:
                    cancel_all_symbol_orders(exchange, symbol)
                except Exception:
                    pass
                # Chiude posizione solo se ancora aperta su Binance
                try:
                    if has_open_position(exchange, symbol):
                        close_position_market(exchange, symbol, direction, trade["qty"])
                    else:
                        logger.info(f"{symbol} — posizione già chiusa su Binance, non invio market close")
                except Exception:
                    pass

                pnl_pct = ((price - trade["entry"]) / trade["entry"] * 100) * config.LEVERAGE
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
                elif hit == "time_exit_soft":
                    sign = "+" if pnl_pct >= 0 else ""
                    exit_reason = "time_exit_soft"
                    msg = (
                        f"⏱ Soft Exit — {symbol}\n"
                        f"Aperto da 2h senza progresso sufficiente\n"
                        f"Exit: {price:.4f} | PnL: {sign}{pnl_pct}%"
                    )
                elif hit == "time_exit_hard":
                    sign = "+" if pnl_pct >= 0 else ""
                    exit_reason = "time_exit_hard"
                    msg = (
                        f"⏰ Hard Exit — {symbol}\n"
                        f"Durata massima raggiunta (4 ore)\n"
                        f"Exit: {price:.4f} | PnL: {sign}{pnl_pct}%"
                    )    
                elif hit == "closed_by_binance":
                    if trade.get("trailing_placed", False):
                        if pnl_pct > 0:
                            exit_reason = "trailing_win"
                            msg = f"📈 Trailing Win — {symbol}\nExit: {price:.4f} | PnL: +{pnl_pct}%"
                        elif pnl_pct < 0:
                            exit_reason = "trailing_loss"
                            msg = f"📉 Trailing Loss — {symbol}\nExit: {price:.4f} | PnL: {pnl_pct}%"
                        else:
                            exit_reason = "breakeven"
                            msg = f"⚖️ Breakeven — {symbol}\nExit: {price:.4f} | PnL: {pnl_pct}%"
                    else:
                        exit_reason = "sl"
                        msg = f"🔴 Stop Loss — {symbol}\nExit: {price:.4f} | PnL: {pnl_pct}%"

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
            cancel_all_symbol_orders(exchange, symbol)
            price = get_ticker_price(exchange, symbol)
            close_position_market(exchange, symbol, trade["direction"], trade["qty"])

            pnl_pct = ((price - trade["entry"]) / trade["entry"] * 100) * config.LEVERAGE
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


PROXIMITY_SOGLIE = [0.50, 0.40, 0.30, 0.20, 0.10]

def check_proximity_alert(exchange, symbol: str, pdh: float, pdl: float, atr: float = 0) -> None:
    """
    Controlla se il prezzo si avvicina al livello di breakout.
    Notifiche progressive: 0.50%, 0.40%, 0.30%, 0.20%, 0.10%
    Ogni soglia viene notificata una sola volta.
    Si resetta solo se il prezzo risale sopra quella soglia.
    """
    global proximity_alerted
    from datetime import datetime, timezone, timedelta

    try:
        if symbol in breakout_seen:
            return
        if symbol in open_trades:
            return
        if get_daily_trade_count(symbol) >= config.MAX_TRADES_PER_DAY_PER_ASSET:
            return

        if symbol not in proximity_alerted:
            proximity_alerted[symbol] = {"pdh": None, "pdl": None}

        price = get_ticker_price(exchange, symbol)
        now_it = datetime.now(timezone.utc) + timedelta(hours=2)
        now_str = now_it.strftime("%d/%m/%Y %H:%M")

        atr_pct = atr / price if atr > 0 else 0
        buffer_pct = max(0.0020, atr_pct * 1.5)

        breakout_long = pdh * (1 + buffer_pct)
        breakout_short = pdl * (1 - buffer_pct)

        dist_to_long = (breakout_long - price) / price
        dist_to_short = (price - breakout_short) / price

        from binance_api import classify_atr
        atr_pct_raw = atr / price * 100
        atr_class = classify_atr(symbol, atr_pct_raw)
        atr_emoji = {
            "IDEAL_VOLATILITY":    "🟢",
            "VALID_BUT_NOT_IDEAL": "🟡",
        }.get(atr_class)

        if atr_emoji is None:
            proximity_alerted[symbol] = {"pdh": None, "pdl": None}
            return

        # --- Breakout LONG ---
        if 0 < dist_to_long:
            for soglia in PROXIMITY_SOGLIE:
                soglia_dec = soglia / 100
                if dist_to_long < soglia_dec:
                    last = proximity_alerted[symbol]["pdh"]
                    if last is None or soglia < last:
                        proximity_alerted[symbol]["pdh"] = soglia
                        send_message(
                            f"⚡ {symbol} si avvicina al Breakout LONG\n"
                            f"Live: {price:,.2f} — {now_str}\n"
                            f"Breakout da: {breakout_long:,.2f}\n"
                            f"Manca: {round(dist_to_long * 100, 2)}%\n"
                            f"ATR%: {round(atr_pct_raw, 3)}% {atr_emoji}"
                        )
                    break
        else:
            proximity_alerted[symbol]["pdh"] = None

        # --- Breakout SHORT ---
        if 0 < dist_to_short:
            for soglia in PROXIMITY_SOGLIE:
                soglia_dec = soglia / 100
                if dist_to_short < soglia_dec:
                    last = proximity_alerted[symbol]["pdl"]
                    if last is None or soglia < last:
                        proximity_alerted[symbol]["pdl"] = soglia
                        send_message(
                            f"⚡ {symbol} si avvicina al Breakout SHORT\n"
                            f"Live: {price:,.2f} — {now_str}\n"
                            f"Breakout da: {breakout_short:,.2f}\n"
                            f"Manca: {round(dist_to_short * 100, 2)}%\n"
                            f"ATR%: {round(atr_pct_raw, 3)}% {atr_emoji}"
                        )
                    break
        else:
            proximity_alerted[symbol]["pdl"] = None

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
            pnl_pct = ((price - trade["entry"]) / trade["entry"] * 100) * config.LEVERAGE
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
        weekend_filter = get_config_param("weekend_filter") or str(config.WEEKEND_FILTER).lower()
        if str(weekend_filter).lower() == "true":
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
                t["trailing_placed"] = False
                # Usa opened_at reale dal DB per i timer durata trade
                opened_at_real = t.get("opened_at")
                if opened_at_real is not None:
                    if opened_at_real.tzinfo is None:
                        opened_at_real = opened_at_real.replace(tzinfo=timezone.utc)
                    t["opened_at_dt"] = opened_at_real
                else:
                    t["opened_at_dt"] = now_utc()
                # Se già oltre 2h al restart → soft check non va rieseguito
                age_at_restart = (now_utc() - t["opened_at_dt"]).total_seconds() / 60
                t["soft_check_done"] = age_at_restart >= float(
                    get_config_param("trade_soft_check_minutes") or config.TRADE_SOFT_CHECK_MINUTES
                )
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
    algo_methods = [m for m in dir(exchange) if "algo" in m.lower()]
    logger.info(f"CCXT Algo methods: {algo_methods}")
    report_sent_today = False
    force_closed_today = False
    last_pnl_notify = None
    heartbeat_sent_today = False
    weekly_report_sent = False
    session_close_sent_today = False

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
                session_close_sent_today = False
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

            # Notifica chiusura sessione — 20:00 UTC (22:00 Italia)
            if now.hour == config.SESSION_END_HOUR and now.minute == 0 and not session_close_sent_today:
                is_weekday = now.weekday() < 5
                weekend_filter = str(get_config_param("weekend_filter") or config.WEEKEND_FILTER).lower() == "true"
                if is_weekday or not weekend_filter:
                    from datetime import datetime, timezone, timedelta
                    now_it = datetime.now(timezone.utc) + timedelta(hours=2)
                    send_message(
                        f"🔴 Sessione chiusa — {now_it.strftime('%d/%m/%Y')}\n"
                        f"Ora: {now_it.strftime('%H:%M')}\n"
                        f"Prossima sessione: domani alle 09:00"
                    )
                session_close_sent_today = True    
                
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
                for symbol in config.SYMBOLS:
                    logger.info(f"Scansione — {symbol} — {now_utc().strftime('%H:%M:%S')} UTC")
                    scan_symbol(exchange, symbol)
                    pdh, pdl = get_previous_day_hl(exchange, symbol)
                    df_15_prox = fetch_ohlcv(exchange, symbol, config.TF_SIGNAL, limit=120)
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
