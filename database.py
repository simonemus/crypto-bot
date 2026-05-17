import logging
import psycopg2
from psycopg2 import pool
import os
from datetime import datetime, timezone, date, timedelta

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "")

_pool = None

def get_pool():
    global _pool
    if _pool is None:
        _pool = pool.ThreadedConnectionPool(
            minconn=2,
            maxconn=10,
            dsn=DATABASE_URL,
            keepalives=1,
            keepalives_idle=30,
            keepalives_interval=10,
            keepalives_count=5
        )
        logger.info("Connection pool inizializzato")
    return _pool

def get_db():
    return get_pool().getconn()

def release_db(conn):
    get_pool().putconn(conn)

def init_db():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id bigserial primary key,
                symbol text not null,
                direction text not null,
                pdh numeric,
                pdl numeric,
                created_at timestamptz default now()
            );
            CREATE TABLE IF NOT EXISTS trades (
                id bigserial primary key,
                symbol text not null,
                direction text not null,
                entry numeric,
                sl numeric,
                tp numeric,
                qty numeric,
                pattern text,
                status text default 'open',
                result text,
                exit_price numeric,
                pnl_pct numeric,
                date date,
                opened_at timestamptz default now(),
                closed_at timestamptz
            );
            CREATE TABLE IF NOT EXISTS config (
                id bigserial primary key,
                key text unique not null,
                value text not null,
                updated_at timestamptz default now()
            );
            CREATE TABLE IF NOT EXISTS equity (
                id bigserial primary key,
                date date unique,
                balance numeric,
                created_at timestamptz default now()
            );
            INSERT INTO config (key, value, updated_at)
            VALUES ('rr', '2.0', now())
            ON CONFLICT (key) DO NOTHING;
        """)
        conn.commit()
        release_db(conn)
        logger.info("Database inizializzato correttamente")
    except Exception as e:
        logger.error(f"Errore inizializzazione DB: {e}")    

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
        release_db(conn)
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
        release_db(conn)
    except Exception as e:
        logger.error(f"DB log_trade_open error: {e}")

def log_trade_close(symbol, exit_price, result, pnl_pct=0.0):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "UPDATE trades SET exit_price=%s, result=%s, status=%s, closed_at=%s, pnl_pct=%s WHERE symbol=%s AND status=%s",
            (exit_price, result, "closed", _now_iso(), pnl_pct, symbol, "open")
        )
        conn.commit()
        release_db(conn)
    except Exception as e:
        logger.error(f"DB log_trade_close error: {e}")

def get_daily_trade_count(symbol):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM trades WHERE symbol=%s AND date=%s", (symbol, _today_str()))
        count = cur.fetchone()[0]
        release_db(conn)
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
        release_db(conn)
        return row
    except Exception as e:
        logger.error(f"DB get_open_trade error: {e}")
        return None

def get_today_trades():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT symbol, direction, result, date, pnl_pct FROM trades WHERE date=%s", (_today_str(),))
        rows = cur.fetchall()
        release_db(conn)
        return [{"symbol": r[0], "direction": r[1], "result": r[2], "date": str(r[3]), "pnl_pct": float(r[4] or 0)} for r in rows]
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
        release_db(conn)
    except Exception as e:
        logger.error(f"DB log_equity error: {e}")

def get_equity_history(days=30):
    try:
        since = (date.today() - timedelta(days=days)).isoformat()
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT date, balance FROM equity WHERE date>=%s ORDER BY date", (since,))
        rows = cur.fetchall()
        release_db(conn)
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
        release_db(conn)
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
        release_db(conn)
    except Exception as e:
        logger.error(f"DB set_config_param error: {e}")

def get_all_trades():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT symbol, direction, result, date, pnl_pct FROM trades ORDER BY date DESC")
        rows = cur.fetchall()
        release_db(conn)
        return [{"symbol": r[0], "direction": r[1], "result": r[2], "date": str(r[3]), "pnl_pct": float(r[4] or 0)} for r in rows]
    except Exception as e:
        logger.error(f"DB get_all_trades error: {e}")
        return []

def get_trades_from(days):
    try:
        since = (date.today() - timedelta(days=days)).isoformat()
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT symbol, direction, result, date, pnl_pct FROM trades WHERE date>=%s ORDER BY date DESC", (since,))
        rows = cur.fetchall()
        release_db(conn)
        return [{"symbol": r[0], "direction": r[1], "result": r[2], "date": str(r[3]), "pnl_pct": float(r[4] or 0)} for r in rows]
    except Exception as e:
        logger.error(f"DB get_trades_from error: {e}")
        return []

def save_breakout(symbol, direction):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO config (key, value, updated_at) VALUES (%s,%s,%s) ON CONFLICT (key) DO UPDATE SET value=%s, updated_at=%s",
            (f"breakout_{symbol}", direction, _now_iso(), direction, _now_iso())
        )
        conn.commit()
        release_db(conn)
    except Exception as e:
        logger.error(f"DB save_breakout error: {e}")

def load_breakouts():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT key, value FROM config WHERE key LIKE 'breakout_%'")
        rows = cur.fetchall()
        release_db(conn)
        result = {}
        for r in rows:
            symbol = r[0].replace("breakout_", "")
            result[symbol] = r[1]
        return result
    except Exception as e:
        logger.error(f"DB load_breakouts error: {e}")
        return {}

def clear_breakout(symbol):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("DELETE FROM config WHERE key=%s", (f"breakout_{symbol}",))
        conn.commit()
        release_db(conn)
    except Exception as e:
        logger.error(f"DB clear_breakout error: {e}")

def clear_all_breakouts():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("DELETE FROM config WHERE key LIKE 'breakout_%'")
        conn.commit()
        release_db(conn)
    except Exception as e:
        logger.error(f"DB clear_all_breakouts error: {e}")        