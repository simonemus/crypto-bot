import logging
import psycopg2
import os
from datetime import datetime, timezone, date, timedelta

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "")

def get_db():
    return psycopg2.connect(DATABASE_URL)

def _now_iso():
    return datetime.now(timezone.utc).isoformat()

def _today_str():
    return date.today().isoformat()

def log_signal(symbol, direction, pdh, pdl):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO signals (symbol, direction, pdh, pdl, created_at) VALUES (%s,%s,%s,%s,%s)",
            (symbol, direction, pdh, pdl, _now_iso())
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"DB log_signal error: {e}")

def log_trade_open(symbol, direction, entry, sl, tp, qty, pattern):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO trades (symbol, direction, entry, sl, tp, qty, pattern, status, date, opened_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (symbol, direction, entry, sl, tp, qty, pattern, "open", _today_str(), _now_iso())
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"DB log_trade_open error: {e}")

def log_trade_close(symbol, exit_price, result):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "UPDATE trades SET exit_price=%s, result=%s, status=%s, closed_at=%s WHERE symbol=%s AND status=%s",
            (exit_price, result, "closed", _now_iso(), symbol, "open")
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"DB log_trade_close error: {e}")

def get_daily_trade_count(symbol):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM trades WHERE symbol=%s AND date=%s", (symbol, _today_str()))
        count = cur.fetchone()[0]
        conn.close()
        return count
    except Exception as e:
        logger.error(f"DB get_daily_trade_count error: {e}")
        return 0

def get_open_trade(symbol):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM trades WHERE symbol=%s AND status=%s LIMIT 1", (symbol, "open"))
        row = cur.fetchone()
        conn.close()
        return row
    except Exception as e:
        logger.error(f"DB get_open_trade error: {e}")
        return None

def get_today_trades():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT symbol, direction, result FROM trades WHERE date=%s", (_today_str(),))
        rows = cur.fetchall()
        conn.close()
        return [{"symbol": r[0], "direction": r[1], "result": r[2]} for r in rows]
    except Exception as e:
        logger.error(f"DB get_today_trades error: {e}")
        return []

def log_equity(balance):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO equity (date, balance, created_at) VALUES (%s,%s,%s) ON CONFLICT (date) DO UPDATE SET balance=%s",
            (_today_str(), balance, _now_iso(), balance)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"DB log_equity error: {e}")

def get_equity_history(days=30):
    try:
        since = (date.today() - timedelta(days=days)).isoformat()
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT date, balance FROM equity WHERE date>=%s ORDER BY date", (since,))
        rows = cur.fetchall()
        conn.close()
        return [{"date": str(r[0]), "balance": float(r[1])} for r in rows]
    except Exception as e:
        logger.error(f"DB get_equity_history error: {e}")
        return []

def get_config_param(key):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT value FROM config WHERE key=%s LIMIT 1", (key,))
        row = cur.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception as e:
        logger.error(f"DB get_config_param error: {e}")
        return None

def set_config_param(key, value):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO config (key, value, updated_at) VALUES (%s,%s,%s) ON CONFLICT (key) DO UPDATE SET value=%s, updated_at=%s",
            (key, value, _now_iso(), value, _now_iso())
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"DB set_config_param error: {e}")