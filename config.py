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
BREAKOUT_BUFFER     = 0.20   # % di buffer sopra/sotto PDH/PDL
RETEST_BUFFER       = 0.50   # % di tolleranza per il retest
SIGNAL_DECAY_BUFFER = 0.80   # % massima distanza dal livello rotto prima che il segnale decada

SL_PCT                  = 0.015  # Stop Loss fisso 1.5%
TP_PCT                  = 0.030  # Take Profit fisso 3.0%
TRAILING_ACTIVATION_PCT = 0.015  # Trailing attivo a +1.5%
RISK_USDT               = 15.0   # Rischio fisso per trade in USDT
MAX_MARGIN_USDT         = 500.0  # Margine massimo per trade in USDT

TRAILING_CALLBACK = {
    "BTC/USDT": 0.6,
    "ETH/USDT": 0.8,
    "SOL/USDT": 1.0,
}

ATR_FILTERS = {
    "BTC/USDT": {"min": 0.10, "ideal_min": 0.25, "ideal_max": 0.55, "max": 0.75},
    "ETH/USDT": {"min": 0.20, "ideal_min": 0.30, "ideal_max": 0.65, "max": 0.90},
    "SOL/USDT": {"min": 0.30, "ideal_min": 0.45, "ideal_max": 0.90, "max": 1.20},
}
   
# ── LIMITI OPERATIVI ─────────────────────────────────────────
MAX_TRADES_PER_DAY_PER_ASSET = 1
LEVERAGE = 2
DAILY_MAX_LOSS_PCT = 2.0      # % massima perdita giornaliera prima del blocco

# ── DURATA MASSIMA TRADE ─────────────────────────────────────
TRADE_MAX_DURATION_MINUTES = 240   # Hard exit: chiude dopo 4 ore se trailing non attivo
TRADE_SOFT_CHECK_MINUTES   = 120   # Soft check: a 2 ore se pnl < MIN_PROGRESS chiude
TRADE_MIN_PROGRESS_PCT     = 1.0   # Progresso minimo % (con leva) al soft check

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