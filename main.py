# ============================================================
#  CRYPTO BREAKOUT BOT — main.py
#  Entry point: avvia il Telegram bot (blocking).
#  Il trading loop parte via /start da Telegram.
# ============================================================

import logging
import config
from telegram_bot import start_telegram_bot

logging.basicConfig(
    level=config.LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)

if __name__ == "__main__":
    print("🚀 Crypto Breakout Bot — avvio in corso…")
    print(f"   Modalità: {'TESTNET' if config.TESTNET else '🔴 LIVE'}")
    print(f"   Asset: {', '.join(config.SYMBOLS)}")
    print(f"   Usa /start su Telegram per avviare il trading.\n")
    start_telegram_bot()
