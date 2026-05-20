# ============================================================
#  CRYPTO BREAKOUT BOT — main.py
#  Entry point: avvia il Telegram bot (blocking).
#  Il trading loop parte via /start da Telegram.
# ============================================================

import logging
logging.getLogger("httpx").setLevel(logging.WARNING)
import signal
import sys
import threading
import os
from http.server import HTTPServer, BaseHTTPRequestHandler

import config
from telegram_bot import start_telegram_bot, shutdown_telegram_bot
from database import init_db
init_db()

logging.basicConfig(
    level=config.LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)

logger = logging.getLogger(__name__)

# ── GRACEFUL SHUTDOWN ─────────────────────────────────────────

SHUTDOWN_EVENT = threading.Event()


def _handle_signal(signum, frame):
    sig_name = signal.Signals(signum).name
    logger.info(f"Segnale {sig_name} ricevuto — avvio shutdown graceful…")
    SHUTDOWN_EVENT.set()
    shutdown_telegram_bot()
    # Forza uscita entro 30 secondi se il processo non termina da solo
    def _force_exit():
        SHUTDOWN_EVENT.wait(timeout=30)
        logger.warning("Timeout shutdown — uscita forzata")
        sys.exit(0)
    t = threading.Thread(target=_force_exit, daemon=True)
    t.start()


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


# ── HEALTH CHECK HTTP SERVER ──────────────────────────────────

class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/healthz", "/health"):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        # Silenzia i log HTTP di default per non inquinare l'output
        pass


def _start_health_server(port: int = 8000):
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    logger.info(f"Health check server in ascolto su :{port}")
    return server


if __name__ == "__main__":
    print("🚀 Crypto Breakout Bot — avvio in corso…")
    print(f"   Modalità: {'TESTNET' if config.TESTNET else '🔴 LIVE'}")
    print(f"   Asset: {', '.join(config.SYMBOLS)}")
    print(f"   Usa /start su Telegram per avviare il trading.\n")

    # Usa la variabile PORT di Railway, altrimenti default a 8000
    port = int(os.environ.get("PORT", "8000"))
    _start_health_server(port=port)
    start_telegram_bot(shutdown_event=SHUTDOWN_EVENT)
