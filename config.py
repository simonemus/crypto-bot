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

# ── ASSET LIST ────────────────────────────────────────────────
SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]

# ── SESSIONE ─────────────────────────────────────────────────
SESSION_START_HOUR   = 8        # 10:00 Italia
SESSION_END_HOUR     = 20       # 22:00 Italia
FORCE_CLOSE_HOUR     = 20       # 22:00 Italia (chiusura forzata di tutte le posizioni aperte)
FORCE_CLOSE_MINUTE   = 5
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
