# ============================================================
#  CRYPTO BREAKOUT BOT — telegram_bot.py
#  Gestione comandi Telegram e notifiche automatiche.
#  Usa python-telegram-bot v20+ (asyncio).
# ============================================================

import logging
import threading
import re
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes
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
            )
        except Exception as e:
            lines.append(f"{sym}: errore dati ({e})")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/report — Riepilogo trade del giorno corrente."""
    trades = get_today_trades()
    wins   = [t for t in trades if t.get("result") == "tp"]
    losses = [t for t in trades if t.get("result") == "sl"]
    force  = [t for t in trades if t.get("result") == "force_close"]
    total  = len(trades)
    winrate = round(len(wins) / total * 100, 1) if total else 0

    lines = [
        f"📊 *Report giornaliero*",
        f"Trade totali: {total}",
        f"✅ Win: {len(wins)} | 🔴 Loss: {len(losses)} | ⚠️ Force: {len(force)}",
        f"Win rate: {winrate}%",
    ]
    for t in trades:
        emoji = {"tp": "✅", "sl": "🔴", "force_close": "⚠️"}.get(t.get("result", ""), "•")
        lines.append(
            f"{emoji} {t['symbol']} {t.get('direction','').upper()} "
            f"— {t.get('result','N/D')}"
        )

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

def _format_report(trades, titolo):
    wins   = [t for t in trades if t.get("result") == "tp"]
    losses = [t for t in trades if t.get("result") == "sl"]
    force  = [t for t in trades if t.get("result") == "force_close"]
    total  = len(trades)
    winrate = round(len(wins) / total * 100, 1) if total else 0

    pnl_wins   = sum(t.get("pnl_pct", 0) for t in wins)
    pnl_losses = sum(t.get("pnl_pct", 0) for t in losses)
    pnl_force  = sum(t.get("pnl_pct", 0) for t in force)
    pnl_total  = round(pnl_wins + pnl_losses + pnl_force, 2)

    sign = "+" if pnl_total >= 0 else ""

    lines = [
        f"📊 *{titolo}*",
        f"Trade totali: {total}",
        f"✅ Win: {len(wins)} | 🔴 Loss: {len(losses)} | ⚠️ Force: {len(force)}",
        f"Win rate: {winrate}%",
        f"PnL totale: `{sign}{pnl_total}%`",
    ]
    return "\n".join(lines)


async def cmd_report_week(update, ctx):
    from database import get_trades_from
    trades = get_trades_from(7)
    msg = _format_report(trades, "Report settimanale")
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


async def cmd_report_month(update, ctx):
    from database import get_trades_from
    trades = get_trades_from(30)
    msg = _format_report(trades, "Report mensile")
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


async def cmd_report_all(update, ctx):
    from database import get_all_trades
    trades = get_all_trades()
    msg = _format_report(trades, "Report totale")
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


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
        f"Risk/trade: `{config.RISK_PER_TRADE_PCT}%`\n"
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
    """/livelli — Mostra PDH e PDL correnti di tutti gli asset."""
    from binance_api import get_exchange, get_previous_day_hl, fetch_ohlcv, add_indicators
    try:
        exchange = get_exchange()
        lines = ["📊 *Livelli PDH/PDL correnti*\n"]
        for symbol in config.SYMBOLS:
            pdh, pdl = get_previous_day_hl(exchange, symbol)
            range_pct = round((pdh - pdl) / pdl * 100, 2)

            df_15 = fetch_ohlcv(exchange, symbol, "15m", limit=20)
            df_15 = add_indicators(df_15)
            last = df_15.iloc[-2]
            atr = float(last["atr"])
            price = float(last["close"])
            atr_pct = atr / price
            buffer = round(max(0.0020, atr_pct * 0.50) * 100, 3)

            lines.append(
                f"*{symbol}*\n"
                f"PDH: `{pdh:.4f}`\n"
                f"PDL: `{pdl:.4f}`\n"
                f"Range: `{range_pct}%`\n"
                f"ATR 15m: `{atr:.4f}`\n"
                f"Buffer attuale: `{buffer}%`\n"
            )
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Errore: {e}")

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/help — Mostra tutti i comandi disponibili."""
    msg = (
        "🤖 *Comandi disponibili*\n\n"
        "⚙️ *Controllo bot*\n"
        "/start — Avvia il bot di trading\n"
        "/stop — Ferma il bot\n"
        "/status — Stato corrente e posizioni aperte\n\n"
        "📊 *Trading*\n"
        "/trade — Posizioni aperte con PnL live\n"
        "/livelli — PDH e PDL correnti di tutti gli asset\n\n"
        "📈 *Report*\n"
        "/report — Report giornaliero\n"
        "/reportweek — Report ultimi 7 giorni\n"
        "/reportmonth — Report ultimi 30 giorni\n"
        "/reportall — Report totale\n"
        "/equity — Equity ultimi 30 giorni\n\n"
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
