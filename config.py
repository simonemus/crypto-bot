# ============================================================
#  CRYPTO BREAKOUT BOT — config.py
#  Tutte le variabili di configurazione centralizzate qui.
#  Cambia TESTNET = False per passare a live trading.
# ============================================================

import os

# ── ENVIRONMENT ──────────────────────────────────────────────
TESTNET = True   # True = Binance Testnet  |  False = Live

# ── BINANCE API ───────────────────────────────────────────────
# Live keys
BINANCE_LIVE_API_KEY    = "YOUR_LIVE_API_KEY"
BINANCE_LIVE_API_SECRET = "YOUR_LIVE_API_SECRET"

# Futures Testnet keys
BINANCE_FUTURES_TESTNET_API_KEY    = os.getenv("BINANCE_FUTURES_TESTNET_API_KEY", "")
BINANCE_FUTURES_TESTNET_API_SECRET = os.getenv("BINANCE_FUTURES_TESTNET_API_SECRET", "")

# Futures Live keys
BINANCE_FUTURES_LIVE_API_KEY    = os.getenv("BINANCE_FUTURES_LIVE_API_KEY", "")
BINANCE_FUTURES_LIVE_API_SECRET = os.getenv("BINANCE_FUTURES_LIVE_API_SECRET", "")

# ── TELEGRAM ──────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "973221453") # es. "123456789"

# ── ASSET LIST ────────────────────────────────────────────────
SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]

# ── SESSIONE ─────────────────────────────────────────────────
SESSION_START_HOUR   = 7        # 09:00 Italia
SESSION_END_HOUR     = 20       # 22:00 Italia
FORCE_CLOSE_HOUR     = 21       # 23:00 Italia (chiusura forzata di tutte le posizioni aperte)
FORCE_CLOSE_MINUTE   = 0
WEEKEND_FILTER       = True     # Non operare durante il weekend (venerdì 22:00 - lunedì 08:00 Italia)

# ── INDICATORI ────────────────────────────────────────────────
ATR_PERIOD   = 14
EMA_FAST     = 20
EMA_SLOW     = 50

# ── GESTIONE RISCHIO ──────────────────────────────────────────
RISK_REWARD_RATIO   = 2.0    # RR di default (modificabile via /set rr)
MAX_RISK_ATR        = 1.30   # SL = entry ± ATR * MAX_RISK_ATR
BREAKOUT_BUFFER     = 0.20   # % di buffer sopra/sotto PDH/PDL
RETEST_BUFFER       = 0.50   # % di tolleranza per il retest
RISK_PER_TRADE_PCT  = 1.0    # % del capitale da rischiare per trade
SIGNAL_DECAY_BUFFER = 0.80   # % massima distanza dal livello rotto prima che il segnale decada

# ── TRAILING STOP LOSS ────────────────────────────────────────
TRAILING_BREAKEVEN_MULTIPLIER = 0.5   # ATR × 0.5 per attivare breakeven
TRAILING_DISTANCE_MULTIPLIER  = 1.0   # ATR × 1.0 distanza trailing dopo breakeven

# ── LIMITI OPERATIVI ─────────────────────────────────────────
MAX_TRADES_PER_DAY_PER_ASSET = 1

# ── TIMEFRAME ────────────────────────────────────────────────
TF_SIGNAL   = "15m"   # timeframe per rilevare breakout + trend
TF_ENTRY    = "5m"    # timeframe per il retest e i pattern candele

# ── CANDLESTICK PATTERNS ABILITATI ───────────────────────────
PATTERNS_ENABLED = [
    "hammer",
    "bullish_engulfing",
    "bearish_engulfing",
    "shooting_star",
    "doji",
    "evening_star",
    "morning_star",
    "dark_cloud_cover",
    "piercing_line",
]

# ── REPORT ────────────────────────────────────────────────────
REPORT_HOUR   = 22
REPORT_MINUTE = 0

# ── LOGGING ───────────────────────────────────────────────────
LOG_LEVEL = "INFO"   # DEBUG | INFO | WARNING | ERROR
LOG_FILE  = "bot.log"

# ── NOTIFICHE PNL ─────────────────────────────────────────────
PNL_NOTIFY_INTERVAL_MINUTES = 60   # Intervallo in minuti per notifica PnL automatica

# ── NOTIFICHE AVVICINAMENTO PDH/PDL ──────────────────────────
PROXIMITY_ALERT_PCT = 0.50   # % di distanza dal PDH/PDL per inviare notifica