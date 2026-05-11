# ============================================================
#  CRYPTO BREAKOUT BOT — config.py
#  Tutte le variabili di configurazione centralizzate qui.
#  Cambia TESTNET = False per passare a live trading.
# ============================================================

import os

# ── ENVIRONMENT ──────────────────────────────────────────────
TESTNET = True   # True = Binance Testnet  |  False = Live

# ── BINANCE API ───────────────────────────────────────────────
# Testnet keys (https://testnet.binance.vision)
BINANCE_TESTNET_API_KEY    = "Cc67mkuZqs7Ywcrj2sXUNv4sayEYymVOUpE6BisOt1IOdgT3iEmABRSSAkIr3ZWA"
BINANCE_TESTNET_API_SECRET = "QcenSpFiASUb5M4pGxdbFNtNmD5EbYnDVlQ2ATe6LSUHwMiQTvWecxmhmF3W27Dx"

# Live keys
BINANCE_LIVE_API_KEY    = "YOUR_LIVE_API_KEY"
BINANCE_LIVE_API_SECRET = "YOUR_LIVE_API_SECRET"

# ── TELEGRAM ──────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "973221453") # es. "123456789"

# ── SUPABASE ──────────────────────────────────────────────────
SUPABASE_URL = "https://rfcwqxmpdjlfbsdsjxgs.supabase.co/rest/v1/"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InJmY3dxeG1wZGpsZmJzZHNqeGdzIiwicm9sZSI6ImFub24iLCJpYXQiOjE3Nzg0MDU2ODYsImV4cCI6MjA5Mzk4MTY4Nn0.-YwvoSLyognfCU-HRSJ6jMPMlHU2feh2n5WzqvBXSE0"

# ── ASSET LIST ────────────────────────────────────────────────
SYMBOLS = ["BTC/USDT", "ETH/USDT"]

# ── SESSIONE ─────────────────────────────────────────────────
SESSION_START_HOUR   = 0   
SESSION_END_HOUR     = 24  
FORCE_CLOSE_HOUR     = 23
FORCE_CLOSE_MINUTE   = 55
WEEKEND_FILTER       = False

# ── INDICATORI ────────────────────────────────────────────────
ATR_PERIOD   = 14
EMA_FAST     = 20
EMA_SLOW     = 50

# ── GESTIONE RISCHIO ──────────────────────────────────────────
RISK_REWARD_RATIO   = 2.0    # RR di default (modificabile via /set rr)
MAX_RISK_ATR        = 1.30   # SL = entry ± ATR * MAX_RISK_ATR
BREAKOUT_BUFFER     = 0.10   # % di buffer sopra/sotto PDH/PDL
RETEST_BUFFER       = 0.15   # % di tolleranza per il retest
RISK_PER_TRADE_PCT  = 1.0    # % del capitale da rischiare per trade

# ── LIMITI OPERATIVI ─────────────────────────────────────────
MAX_TRADES_PER_DAY_PER_ASSET = 1

# ── TIMEFRAME ────────────────────────────────────────────────
TF_SIGNAL   = "15m"   # timeframe per rilevare breakout + trend
TF_ENTRY    = "5m"    # timeframe per il retest e i pattern candele

# ── CANDLESTICK PATTERNS ABILITATI ───────────────────────────
PATTERNS_ENABLED = ["hammer", "bullish_engulfing", "bearish_engulfing", "shooting_star"]

# ── REPORT ────────────────────────────────────────────────────
REPORT_HOUR   = 22
REPORT_MINUTE = 0

# ── LOGGING ───────────────────────────────────────────────────
LOG_LEVEL = "INFO"   # DEBUG | INFO | WARNING | ERROR
LOG_FILE  = "bot.log"
