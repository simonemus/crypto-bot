# ============================================================
#  CRYPTO BREAKOUT BOT — database.py
#  Tutte le operazioni su Supabase:
#  trades, signals, config, equity
# ============================================================

import logging
from datetime import datetime, timezone, date
from supabase import create_client, Client

import config

logger = logging.getLogger(__name__)

_supabase: Client | None = None


def get_db() -> Client:
    global _supabase
    if _supabase is None:
        _supabase = create_client(config.SUPABASE_URL, config.SUPABASE_KEY)
    return _supabase


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_str() -> str:
    return date.today().isoformat()


# ── SIGNALS ───────────────────────────────────────────────────

def log_signal(symbol: str, direction: str, pdh: float, pdl: float) -> None:
    try:
        get_db().table("signals").insert({
            "symbol":    symbol,
            "direction": direction,
            "pdh":       pdh,
            "pdl":       pdl,
            "created_at": _now_iso(),
        }).execute()
    except Exception as e:
        logger.error(f"DB log_signal error: {e}")


# ── TRADES ────────────────────────────────────────────────────

def log_trade_open(symbol: str, direction: str, entry: float,
                   sl: float, tp: float, qty: float, pattern: str) -> None:
    try:
        get_db().table("trades").insert({
            "symbol":    symbol,
            "direction": direction,
            "entry":     entry,
            "sl":        sl,
            "tp":        tp,
            "qty":       qty,
            "pattern":   pattern,
            "status":    "open",
            "date":      _today_str(),
            "opened_at": _now_iso(),
        }).execute()
    except Exception as e:
        logger.error(f"DB log_trade_open error: {e}")


def log_trade_close(symbol: str, exit_price: float, result: str) -> None:
    """result: 'tp' | 'sl' | 'force_close'"""
    try:
        (
            get_db().table("trades")
            .update({
                "exit_price": exit_price,
                "result":     result,
                "status":     "closed",
                "closed_at":  _now_iso(),
            })
            .eq("symbol", symbol)
            .eq("status", "open")
            .execute()
        )
    except Exception as e:
        logger.error(f"DB log_trade_close error: {e}")


def get_daily_trade_count(symbol: str) -> int:
    try:
        res = (
            get_db().table("trades")
            .select("id", count="exact")
            .eq("symbol", symbol)
            .eq("date", _today_str())
            .execute()
        )
        return res.count or 0
    except Exception as e:
        logger.error(f"DB get_daily_trade_count error: {e}")
        return 0


def get_open_trade(symbol: str) -> dict | None:
    try:
        res = (
            get_db().table("trades")
            .select("*")
            .eq("symbol", symbol)
            .eq("status", "open")
            .limit(1)
            .execute()
        )
        return res.data[0] if res.data else None
    except Exception as e:
        logger.error(f"DB get_open_trade error: {e}")
        return None


def get_today_trades() -> list[dict]:
    try:
        res = (
            get_db().table("trades")
            .select("*")
            .eq("date", _today_str())
            .execute()
        )
        return res.data or []
    except Exception as e:
        logger.error(f"DB get_today_trades error: {e}")
        return []


# ── EQUITY ────────────────────────────────────────────────────

def log_equity(balance: float) -> None:
    try:
        get_db().table("equity").insert({
            "date":       _today_str(),
            "balance":    balance,
            "created_at": _now_iso(),
        }).execute()
    except Exception as e:
        logger.error(f"DB log_equity error: {e}")


def get_equity_history(days: int = 30) -> list[dict]:
    try:
        from datetime import timedelta
        since = (date.today() - timedelta(days=days)).isoformat()
        res = (
            get_db().table("equity")
            .select("date, balance")
            .gte("date", since)
            .order("date")
            .execute()
        )
        return res.data or []
    except Exception as e:
        logger.error(f"DB get_equity_history error: {e}")
        return []


# ── CONFIG ────────────────────────────────────────────────────

def get_config_param(key: str) -> str | None:
    try:
        res = (
            get_db().table("config")
            .select("value")
            .eq("key", key)
            .limit(1)
            .execute()
        )
        if res.data:
            return res.data[0]["value"]
        return None
    except Exception as e:
        logger.error(f"DB get_config_param error: {e}")
        return None


def set_config_param(key: str, value: str) -> None:
    try:
        db = get_db()
        existing = db.table("config").select("id").eq("key", key).execute()
        if existing.data:
            db.table("config").update({"value": value, "updated_at": _now_iso()}).eq("key", key).execute()
        else:
            db.table("config").insert({"key": key, "value": value, "updated_at": _now_iso()}).execute()
    except Exception as e:
        logger.error(f"DB set_config_param error: {e}")
