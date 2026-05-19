# ============================================================
#  CRYPTO BREAKOUT BOT — telegram_bot.py
#  Gestione comandi Telegram e notifiche automatiche.
#  Usa python-telegram-bot v20+ (asyncio).
# ============================================================

import logging
import threading
import re
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes
)
from telegram.constants import ParseMode

import config
from database import (
    get_config_param, set_config_param,
    get_today_trades, get_equity_history,
)

logger = logging.getLogger(__name__)

# Riferimento al thread del bot di trading (impostato da main)
_bot_thread: threading.Thread | None = None
_stop_callback = None    # funzione stop_bot() da bot.py
_start_callback = None   # funzione run_bot() da bot.py


# ── INVIO MESSAGGI ────────────────────────────────────────────

def _get_app():
    """Crea e restituisce l'application Telegram (singleton lazy)."""
    if not hasattr(_get_app, "_instance"):
        _get_app._instance = ApplicationBuilder().token(config.TELEGRAM_BOT_TOKEN).build()
    return _get_app._instance


import queue
import threading
_message_queue = queue.Queue()

def send_message(text: str) -> None:
    _message_queue.put(text)

def send_error(text: str) -> None:
    send_message(f"⚠️ *ERRORE*\n{text}")

def _message_worker():
    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    bot = None
    
    async def _init():
        from telegram import Bot
        return Bot(token=config.TELEGRAM_BOT_TOKEN)
    
    bot = loop.run_until_complete(_init())
    
    while True:
        try:
            text = _message_queue.get(timeout=1)
            async def _send(t):
                await bot.send_message(
                    chat_id=config.TELEGRAM_CHAT_ID,
                    text=t,
                    parse_mode="Markdown",
                )
            loop.run_until_complete(_send(text))
        except queue.Empty:
            continue
        except Exception as e:
            logger.error(f"Errore invio messaggio: {e}")

_worker_thread = threading.Thread(target=_message_worker, daemon=True)
_worker_thread.start()


# ── COMANDI ───────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/start — Avvia il bot di trading."""
    global _bot_thread
    from bot import run_bot, BOT_RUNNING

    if BOT_RUNNING:
        await update.message.reply_text("ℹ️ Il bot è già in esecuzione.")
        return

    _bot_thread = threading.Thread(target=run_bot, daemon=True)
    _bot_thread.start()
    await update.message.reply_text("✅ *Bot avviato con successo!*", parse_mode=ParseMode.MARKDOWN)


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/stop — Ferma il bot di trading."""
    from bot import stop_bot, BOT_RUNNING

    if not BOT_RUNNING:
        await update.message.reply_text("ℹ️ Il bot non è in esecuzione.")
        return

    stop_bot()
    await update.message.reply_text("🔴 *Bot fermato.*", parse_mode=ParseMode.MARKDOWN)


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/status — Stato corrente del bot e posizioni aperte."""
    from bot import BOT_RUNNING, open_trades, in_session, now_utc
    from binance_api import get_exchange, get_balance_usdt

    stato = "🟢 In esecuzione" if BOT_RUNNING else "🔴 Fermo"
    sessione = "✅ Sessione attiva" if in_session() else "⏸ Fuori sessione"
    ora = now_utc().strftime("%H:%M UTC")
    rr = get_config_param("rr") or config.RISK_REWARD_RATIO

    try:
        exchange = get_exchange()
        balance = get_balance_usdt(exchange)
        equity_str = f"`{balance:.2f} USDT`"
    except Exception:
        equity_str = "N/D"

    lines = [
        f"*Status Bot*",
        f"Stato: {stato}",
        f"Sessione: {sessione}",
        f"Ora: {ora}",
        f"Equity: {equity_str}",
        f"RR attuale: {rr}",
        f"",
        f"*Posizioni aperte: {len(open_trades)}*",
    ]
    for sym, t in open_trades.items():
        lines.append(
            f"• {sym} {t['direction'].upper()} — entry `{t['entry']:.4f}` "
            f"SL `{t['sl']:.4f}` TP `{t['tp']:.4f}`"
        )

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_trade(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/trade — Dettaglio dei trade aperti con PnL live."""
    from bot import open_trades
    from binance_api import get_exchange, get_ticker_price

    if not open_trades:
        await update.message.reply_text("Nessun trade aperto al momento.")
        return

    exchange = get_exchange()
    lines = ["*Trade aperti*"]
    for sym, t in open_trades.items():
        try:
            price = get_ticker_price(exchange, sym)
            pnl_pct = (price - t["entry"]) / t["entry"] * 100
            if t["direction"] == "short":
                pnl_pct = -pnl_pct
            pnl_sign = "+" if pnl_pct >= 0 else ""
            lines.append(
                f"*{sym}* — {t['direction'].upper()}\n"
                f"Entry: `{t['entry']:.4f}` | Live: `{price:.4f}`\n"
                f"SL: `{t['sl']:.4f}` | TP: `{t['tp']:.4f}`\n"
                f"PnL: `{pnl_sign}{pnl_pct:.2f}%`\n"
                f"Pattern: `{t.get('pattern', 'N/D')}`\n"
            )
        except Exception as e:
            lines.append(f"{sym}: errore dati ({e})")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/report — Riepilogo trade del giorno corrente."""
    trades    = get_today_trades()
    wins      = [t for t in trades if t.get("result") == "tp"]
    losses    = [t for t in trades if t.get("result") == "sl"]
    breakeven = [t for t in trades if t.get("result") == "breakeven"]
    total     = len(trades)
    winrate   = round(len(wins) / total * 100, 1) if total else 0

    pnl_total = round(sum(t.get("pnl_pct", 0) for t in trades), 2)
    pnl_medio = round(pnl_total / total, 2) if total else 0
    sign_total = "+" if pnl_total >= 0 else ""
    sign_medio = "+" if pnl_medio >= 0 else ""

    lines = [
        f"📊 *Report giornaliero*",
        f"Trade totali: {total}",
        f"✅ Win: {len(wins)} | 🔴 Loss: {len(losses)} | ⚖️ BE: {len(breakeven)}",
        f"Win rate: {winrate}%",
        f"PnL medio: {sign_medio}{pnl_medio}%",
        f"PnL totale: {sign_total}{pnl_total}%",
    ]
    for t in trades:
        risultato = t.get("result") or "aperto"
        emoji = {"tp": "✅", "sl": "🔴", "breakeven": "⚖️"}.get(risultato, "•")
        lines.append(
            f"{emoji} {t['symbol']} {t.get('direction','').upper()} "
            f"— {risultato}"
        )

    await update.message.reply_text("\n".join(lines))

def _format_report(trades, titolo, show_equity=False):
    from binance_api import get_exchange, get_balance_usdt
    from database import get_equity_history

    wins      = [t for t in trades if t.get("result") == "tp"]
    losses    = [t for t in trades if t.get("result") == "sl"]
    breakeven = [t for t in trades if t.get("result") == "breakeven"]
    total     = len(trades)
    winrate   = round(len(wins) / total * 100, 1) if total else 0

    pnl_total = round(sum(t.get("pnl_pct", 0) for t in trades), 2)
    pnl_medio = round(pnl_total / total, 2) if total else 0
    sign_total = "+" if pnl_total >= 0 else ""
    sign_medio = "+" if pnl_medio >= 0 else ""

    lines = [
        f"📊 *{titolo}*",
        f"Trade totali: {total}",
        f"✅ Win: {len(wins)} | 🔴 Loss: {len(losses)} | ⚖️ BE: {len(breakeven)}",
        f"Win rate: {winrate}%",
        f"PnL medio: {sign_medio}{pnl_medio}%",
        f"PnL totale: {sign_total}{pnl_total}%",
    ]

    if show_equity:
        try:
            exchange = get_exchange()
            equity_now = get_balance_usdt(exchange)
            history = get_equity_history(30)
            if history:
                equity_start = history[-1]["balance"]
                guadagno = round(equity_now - equity_start, 2)
                guadagno_pct = round((equity_now - equity_start) / equity_start * 100, 2)
                sign_g = "+" if guadagno >= 0 else ""
                lines.append(f"Equity iniziale: `{equity_start:.2f} USDT`")
                lines.append(f"Equity attuale: `{equity_now:.2f} USDT`")
                lines.append(f"Guadagno: `{sign_g}{guadagno:.2f} USDT ({sign_g}{guadagno_pct}%)`")
            else:
                lines.append(f"Equity attuale: `{equity_now:.2f} USDT`")
        except Exception as e:
            lines.append(f"⚠️ Errore equity: {e}")

    return "\n".join(lines)


async def cmd_report_week(update, ctx):
    from database import get_trades_range
    from datetime import date, timedelta

    today = date.today()
    if config.WEEKEND_FILTER:
        start = today - timedelta(days=today.weekday())
    else:
        start = today - timedelta(days=6)

    end = today
    year = today.year
    start_str = start.strftime("%d/%m")
    end_str = end.strftime("%d/%m")

    trades = get_trades_range(str(start), str(end))
    msg = _format_report(trades, f"Report settimanale {year} — {start_str} al {end_str}", show_equity=True)
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


async def cmd_report_month(update, ctx):
    from database import get_trades_range
    from datetime import date, timedelta

    today = date.today()
    start = today - timedelta(days=29)
    end = today
    year = today.year
    start_str = start.strftime("%d/%m")
    end_str = end.strftime("%d/%m")

    trades = get_trades_range(str(start), str(end))
    msg = _format_report(trades, f"Report mensile {year} — {start_str} al {end_str}", show_equity=True)
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


async def cmd_report_all(update, ctx):
    from database import get_all_trades, get_first_trade_date
    from datetime import date, datetime

    trades = get_all_trades()
    first_date = get_first_trade_date()

    if first_date:
        first_dt = datetime.strptime(first_date, "%Y-%m-%d").date()
        days_active = (date.today() - first_dt).days + 1
        titolo = f"Report totale — attivo dal {first_dt.strftime('%d/%m/%Y')} ({days_active} giorni)"
    else:
        titolo = "Report totale"

    msg = _format_report(trades, titolo, show_equity=True)
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/stats — Statistiche winrate per pattern candele."""
    from database import get_stats_by_pattern
    stats = get_stats_by_pattern()

    if not stats:
        await update.message.reply_text("Nessun dato disponibile ancora.")
        return

    # Trova il pattern con winrate più alto (solo tra quelli con almeno 1 trade)
    best_pattern = None
    best_winrate = -1
    for s in stats:
        if s["total"] > 0:
            wr = s["wins"] / s["total"] * 100
            if wr > best_winrate:
                best_winrate = wr
                best_pattern = s["pattern"]

    lines = ["📊 Statistiche per pattern\n"]
    for s in stats:
        total = s["total"]
        wins  = s["wins"]
        winrate = round(wins / total * 100, 1) if total else 0
        star = " ⭐" if s["pattern"] == best_pattern else ""
        wr_str = f"{winrate}%" if total > 0 else "N/D"
        lines.append(
            f"🕯 {s['pattern']}{star}\n"
            f"Trade: {total} | Win: {wins} | Loss: {s['losses']} | BE: {s['breakeven']}\n"
            f"Win rate: {wr_str}\n"
        )

    await update.message.reply_text("\n".join(lines))

async def cmd_stats_asset(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/statsasset — Statistiche winrate per asset."""
    from database import get_stats_by_asset
    stats = get_stats_by_asset()

    if not stats:
        await update.message.reply_text("Nessun dato disponibile ancora.")
        return

    # Trova asset con winrate più alto, in parità vince PnL medio più alto
    best_asset = None
    best_winrate = -1
    best_pnl = -9999
    for s in stats:
        if s["total"] > 0:
            wr = s["wins"] / s["total"] * 100
            if wr > best_winrate or (wr == best_winrate and s["avg_pnl"] > best_pnl):
                best_winrate = wr
                best_pnl = s["avg_pnl"]
                best_asset = s["symbol"]

    lines = ["📊 Statistiche per asset\n"]
    for s in stats:
        total = s["total"]
        wins  = s["wins"]
        winrate = round(wins / total * 100, 1) if total else 0
        star = " ⭐" if s["symbol"] == best_asset else ""
        wr_str = f"{winrate}%" if total > 0 else "N/D"
        avg_pnl = s["avg_pnl"]
        sign = "+" if avg_pnl >= 0 else ""
        lines.append(
            f"💰 {s['symbol']}{star}\n"
            f"Trade: {total} | Win: {wins} | Loss: {s['losses']} | BE: {s['breakeven']}\n"
            f"Win rate: {wr_str} | PnL medio: {sign}{avg_pnl}%\n"
        )

    await update.message.reply_text("\n".join(lines))

async def cmd_stats_direction(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/statsdirection — Statistiche winrate per direzione LONG e SHORT."""
    from database import get_stats_by_direction
    stats = get_stats_by_direction()

    if not stats:
        await update.message.reply_text("Nessun dato disponibile ancora.")
        return

    # Trova direzione con winrate più alto, in parità vince PnL medio più alto
    best_dir = None
    best_winrate = -1
    best_pnl = -9999
    for s in stats:
        if s["total"] > 0:
            wr = s["wins"] / s["total"] * 100
            if wr > best_winrate or (wr == best_winrate and s["avg_pnl"] > best_pnl):
                best_winrate = wr
                best_pnl = s["avg_pnl"]
                best_dir = s["direction"]

    lines = ["📊 Statistiche per direzione\n"]
    for s in stats:
        total = s["total"]
        wins  = s["wins"]
        winrate = round(wins / total * 100, 1) if total else 0
        star = " ⭐" if s["direction"] == best_dir else ""
        wr_str = f"{winrate}%" if total > 0 else "N/D"
        avg_pnl = s["avg_pnl"]
        sign = "+" if avg_pnl >= 0 else ""
        emoji = "📈" if s["direction"] == "long" else "📉"
        lines.append(
            f"{emoji} {s['direction'].upper()}{star}\n"
            f"Trade: {total} | Win: {wins} | Loss: {s['losses']} | BE: {s['breakeven']}\n"
            f"Win rate: {wr_str} | PnL medio: {sign}{avg_pnl}%\n"
        )

    await update.message.reply_text("\n".join(lines))

async def cmd_stats_hour(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/statshour — Statistiche winrate per ora del giorno."""
    from database import get_stats_by_hour
    stats = get_stats_by_hour()

    if not stats:
        await update.message.reply_text("Nessun dato disponibile ancora.")
        return

    # Trova ora con winrate più alto, in parità vince PnL medio più alto
    best_hour = None
    best_winrate = -1
    best_pnl = -9999
    for s in stats:
        if s["total"] > 0:
            wr = s["wins"] / s["total"] * 100
            if wr > best_winrate or (wr == best_winrate and s["avg_pnl"] > best_pnl):
                best_winrate = wr
                best_pnl = s["avg_pnl"]
                best_hour = s["hour"]

    lines = ["📊 Statistiche per ora del giorno (ora italiana)\n"]
    for s in stats:
        total = s["total"]
        wins  = s["wins"]
        winrate = round(wins / total * 100, 1) if total else 0
        star = " ⭐" if s["hour"] == best_hour else ""
        wr_str = f"{winrate}%" if total > 0 else "N/D"
        avg_pnl = s["avg_pnl"]
        sign = "+" if avg_pnl >= 0 else ""
        italian_hour = s['hour'] + 2
        lines.append(
            f"🕐 {italian_hour:02d}:00-{italian_hour+1:02d}:00{star}\n"
            f"Trade: {total} | Win: {wins} | Loss: {s['losses']} | BE: {s['breakeven']}\n"
            f"Win rate: {wr_str} | PnL medio: {sign}{avg_pnl}%\n"
        )

    await update.message.reply_text("\n".join(lines))


async def cmd_equity(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/equity — Curva equity degli ultimi 30 giorni."""
    history = get_equity_history(days=30)
    if not history:
        await update.message.reply_text("Nessun dato equity disponibile.")
        return

    lines = ["📈 *Equity ultimi 30 giorni*"]
    for row in history[-10:]:   # mostra ultimi 10 record
        lines.append(f"• {row['date']}: `{row['balance']:.2f} USDT`")

    first = history[0]["balance"]
    last  = history[-1]["balance"]
    delta = last - first
    sign  = "+" if delta >= 0 else ""
    lines.append(f"\nVariazione: `{sign}{delta:.2f} USDT`")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_parametri(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/parametri — Mostra i parametri correnti del bot."""
    rr  = get_config_param("rr")  or config.RISK_REWARD_RATIO
    msg = (
        f"⚙️ *Parametri correnti*\n"
        f"RR: `{rr}`\n"
        f"ATR period: `{config.ATR_PERIOD}`\n"
        f"EMA fast/slow: `{config.EMA_FAST}/{config.EMA_SLOW}`\n"
        f"Max risk ATR: `{config.MAX_RISK_ATR}`\n"
        f"Breakout buffer: `{config.BREAKOUT_BUFFER}%`\n"
        f"Retest buffer: `{config.RETEST_BUFFER}%`\n"
        f"Margine/trade: `{config.MARGIN_PER_TRADE_PCT}%`\n"
        f"Sessione: `{config.SESSION_START_HOUR}:00–{config.SESSION_END_HOUR}:00 UTC`\n"
        f"Weekend filter: `{'ON' if config.WEEKEND_FILTER else 'OFF'}`\n"
        f"Testnet: `{'SI' if config.TESTNET else 'NO'}`"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


async def cmd_set(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/set rr 2.5 — Imposta un parametro dinamico."""
    args = ctx.args   # es. ["rr", "2.5"]
    if not args or len(args) < 2:
        await update.message.reply_text(
            "Uso: `/set rr <valore>`\nEsempio: `/set rr 2.5`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    param = args[0].lower()
    value = args[1]

    if param == "rr":
        try:
            rr_val = float(value)
            if not (0.5 <= rr_val <= 10):
                raise ValueError
            set_config_param("rr", str(rr_val))
            await update.message.reply_text(
                f"✅ RR aggiornato a `{rr_val}`", parse_mode=ParseMode.MARKDOWN
            )
        except ValueError:
            await update.message.reply_text("⚠️ Valore non valido. RR deve essere tra 0.5 e 10.")
    else:
        await update.message.reply_text(f"⚠️ Parametro `{param}` non riconosciuto.")


async def cmd_test(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/test — Manda una notifica di prova per verificare che tutto funzioni."""
    send_message("🔍 *Test notifica* — il bot sta funzionando correttamente!")
    await update.message.reply_text("✅ Notifica di prova inviata!", parse_mode=ParseMode.MARKDOWN)

async def cmd_livelli(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/livelli — Mostra PDH e PDL correnti di tutti gli asset con prezzo live."""
    from binance_api import get_exchange, get_previous_day_hl, fetch_ohlcv, add_indicators, get_ticker_price
    from datetime import datetime, timezone, timedelta
    try:
        exchange = get_exchange()
        now_it = datetime.now(timezone.utc) + timedelta(hours=2)
        now_str = now_it.strftime("%d/%m/%Y %H:%M")
        lines = ["📊 *Livelli PDH/PDL correnti*\n"]
        for symbol in config.SYMBOLS:
            pdh, pdl = get_previous_day_hl(exchange, symbol)
            range_pct = round((pdh - pdl) / pdl * 100, 2)

            df_15 = fetch_ohlcv(exchange, symbol, "15m", limit=20)
            df_15 = add_indicators(df_15)
            last = df_15.iloc[-2]
            atr = float(last["atr"])
            price = get_ticker_price(exchange, symbol)
            atr_pct = atr / price
            buffer = round(max(0.0020, atr_pct * 1.5) * 100, 3)

            dist_pdh = round((pdh - price) / pdh * 100, 2)
            dist_pdl = round((price - pdl) / pdl * 100, 2)

            lines.append(
                f"*{symbol}*\n"
                f"Live: `{price:.2f}` {now_str}\n"
                f"PDH: `{pdh:.4f}` distanza `{dist_pdh}%`\n"
                f"PDL: `{pdl:.4f}` distanza `{dist_pdl}%`\n"
                f"Range: `{range_pct}%`\n"
                f"ATR 15m: `{atr:.4f}` | Buffer: `{buffer}%`\n"
            )
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Errore: {e}")

async def cmd_cooldown(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/cooldown — Mostra asset in cooldown e tempo rimanente."""
    from bot import decay_cooldown
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    lines = ["⏳ *Stato cooldown*\n"]

    has_cooldown = False
    for symbol in config.SYMBOLS:
        if symbol in decay_cooldown:
            elapsed = (now - decay_cooldown[symbol]).total_seconds()
            remaining = max(0, int((3600 - elapsed) // 60))
            lines.append(f"🔴 *{symbol}* — riprende tra {remaining} minuti")
            has_cooldown = True
        else:
            lines.append(f"🟢 *{symbol}* — nessun cooldown")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

async def cmd_reset_cooldown(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/resetcooldown — Resetta manualmente il cooldown di un asset."""
    keyboard = []
    row = []
    for symbol in config.SYMBOLS:
        asset = symbol.split('/')[0]
        row.append(InlineKeyboardButton(asset, callback_data=f"reset_{asset}"))
    keyboard.append(row)
    keyboard.append([InlineKeyboardButton("🔄 Reset tutti", callback_data="reset_ALL")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Seleziona l'asset da resettare:", reply_markup=reply_markup)


async def cmd_reset_cooldown_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Gestisce i bottoni del reset cooldown."""
    from bot import decay_cooldown
    from database import clear_decay_cooldown

    query = update.callback_query
    await query.answer()

    target = query.data.replace("reset_", "")

    if target == "ALL":
        for symbol in config.SYMBOLS:
            if symbol in decay_cooldown:
                del decay_cooldown[symbol]
            clear_decay_cooldown(symbol)
        await query.edit_message_text("✅ Cooldown resettato per tutti gli asset.")
        return

    symbol_match = None
    for symbol in config.SYMBOLS:
        if target in symbol:
            symbol_match = symbol
            break

    if not symbol_match:
        await query.edit_message_text(f"⚠️ Asset {target} non trovato.")
        return

    if symbol_match in decay_cooldown:
        del decay_cooldown[symbol_match]
    clear_decay_cooldown(symbol_match)
    await query.edit_message_text(f"✅ Cooldown resettato per {symbol_match}.")            

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/help — Mostra tutti i comandi disponibili."""
    msg = (
        "🤖 *Comandi disponibili*\n\n"
        "⚙️ *Controllo bot*\n"
        "/start — Avvia il bot di trading\n"
        "/stop — Ferma il bot\n"
        "/status — Stato corrente e posizioni aperte\n\n"
        "💹 *Trading*\n"
        "/trade — Posizioni aperte con PnL live\n"
        "/livelli — PDH e PDL correnti (all asset)\n"
        "/cooldown — Stato cooldown per asset\n"
        "/resetcooldown — Reset cooldown x asset\n\n"
        "📈 *Report*\n"
        "/report — Report giornaliero\n"
        "/reportweek — Report ultimi 7 giorni\n"
        "/reportmonth — Report ultimi 30 giorni\n"
        "/reportall — Report totale\n"
        "/equity — Equity ultimi 30 giorni\n\n"
        "📊 *Statistiche*\n"
        "/stats — Statistiche winrate per pattern\n"
        "/statsasset — Statistiche winrate per asset\n"
        "/statsdirection — Statistiche winrate per direzione\n"
        "/statshour — Statistiche winrate per ora\n\n"
        "⚙️ *Parametri*\n"
        "/parametri — Parametri correnti\n"
        "/set rr 2.5 — Modifica il Risk/Reward\n\n"
        "🔧 *Altro*\n"
        "/test — Notifica di prova\n"
        "/help — Mostra questo messaggio"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)            


# ── AVVIO APPLICATION ─────────────────────────────────────────

def start_telegram_bot() -> None:
    app = ApplicationBuilder().token(config.TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("statsasset", cmd_stats_asset))
    app.add_handler(CommandHandler("statsdirection", cmd_stats_direction))
    app.add_handler(CommandHandler("statshour", cmd_stats_hour))
    app.add_handler(CommandHandler("cooldown", cmd_cooldown))
    app.add_handler(CommandHandler("resetcooldown", cmd_reset_cooldown))
    app.add_handler(CallbackQueryHandler(cmd_reset_cooldown_callback, pattern="^reset_"))

    app.add_handler(CommandHandler("start",       cmd_start))
    app.add_handler(CommandHandler("stop",        cmd_stop))
    app.add_handler(CommandHandler("status",      cmd_status))
    app.add_handler(CommandHandler("trade",       cmd_trade))
    app.add_handler(CommandHandler("report",      cmd_report))
    app.add_handler(CommandHandler("equity",      cmd_equity))
    app.add_handler(CommandHandler("parametri",   cmd_parametri))
    app.add_handler(CommandHandler("set",         cmd_set))
    app.add_handler(CommandHandler("reportweek",  cmd_report_week))
    app.add_handler(CommandHandler("reportmonth", cmd_report_month))
    app.add_handler(CommandHandler("reportall",   cmd_report_all))
    app.add_handler(CommandHandler("test",        cmd_test))
    app.add_handler(CommandHandler("livelli",     cmd_livelli))
    app.add_handler(CommandHandler("help",        cmd_help))

    logger.info("Telegram bot in ascolto…")
    app.run_polling(drop_pending_updates=True)
