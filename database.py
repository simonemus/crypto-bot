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
    conn = get_pool().getconn()
    try:
        # Verifica che la connessione sia ancora attiva
        conn.cursor().execute("SELECT 1")
    except Exception:
        # Connessione morta — ne crea una nuova
        get_pool().putconn(conn, close=True)
        conn = get_pool().getconn()
    return conn

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
                atr numeric,
                breakout_buffer numeric,
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

            ALTER TABLE trades ADD COLUMN IF NOT EXISTS atr numeric;
            ALTER TABLE trades ADD COLUMN IF NOT EXISTS breakout_buffer numeric;
            ALTER TABLE trades ADD COLUMN IF NOT EXISTS sl_order_id text;
            ALTER TABLE trades ADD COLUMN IF NOT EXISTS tp_order_id text;
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

def log_trade_open(symbol, direction, entry, sl, tp, qty, pattern, atr=0.0, breakout_buffer=0.0, sl_order_id=None, tp_order_id=None):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO trades (symbol, direction, entry, sl, tp, qty, pattern, status, date, opened_at, atr, breakout_buffer, sl_order_id, tp_order_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (symbol, direction, entry, sl, tp, qty, pattern, "open", _today_str(), _now_iso(), atr, breakout_buffer, sl_order_id, tp_order_id)
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

def get_open_trades() -> list[dict]:
    """Restituisce tutti i trade con status 'open' dal database."""
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "SELECT symbol, direction, entry, sl, tp, qty, pattern, atr, sl_order_id, tp_order_id FROM trades WHERE status='open'"
        )
        rows = cur.fetchall()
        release_db(conn)
        return [
            {
                "symbol":      r[0],
                "direction":   r[1],
                "entry":       float(r[2]),
                "sl":          float(r[3]),
                "tp":          float(r[4]),
                "qty":         float(r[5]),
                "pattern":     r[6],
                "atr":         float(r[7]) if r[7] else 0.0,
                "sl_order_id": r[8],
                "tp_order_id": r[9],
            }
            for r in rows
        ]
    except Exception as e:
        logger.error(f"DB get_open_trades error: {e}")
        return []

def get_stats_by_pattern() -> list[dict]:
    """Restituisce statistiche per tutti i pattern configurati."""
    import config
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT 
                pattern,
                COUNT(*) as total,
                SUM(CASE WHEN result = 'tp' THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN result = 'sl' THEN 1 ELSE 0 END) as losses,
                SUM(CASE WHEN result = 'force_close' THEN 1 ELSE 0 END) as force,
                SUM(CASE WHEN result = 'breakeven' THEN 1 ELSE 0 END) as breakeven
            FROM trades
            WHERE status = 'closed' AND pattern IS NOT NULL
            GROUP BY pattern
        """)
        rows = cur.fetchall()
        release_db(conn)

        # Crea dizionario con i dati dal DB
        db_data = {r[0]: {"total": r[1], "wins": r[2], "losses": r[3], "force": r[4], "breakeven": r[5]} for r in rows}

        # Aggiunge tutti i pattern configurati anche se con 0 trade
        result = []
        for pattern in config.PATTERNS_ENABLED:
            if pattern in db_data:
                d = db_data[pattern]
                result.append({
                    "pattern": pattern,
                    "total":   d["total"],
                    "wins":    d["wins"],
                    "losses":  d["losses"],
                    "force":   d["force"],
                    "breakeven": d["breakeven"],
                })
            else:
                result.append({
                    "pattern": pattern,
                    "total":   0,
                    "wins":    0,
                    "losses":  0,
                    "force":   0,
                    "breakeven": 0,
                })

        return sorted(result, key=lambda x: (x["total"]), reverse=True)
    except Exception as e:
        logger.error(f"DB get_stats_by_pattern error: {e}")
        return []

def get_stats_by_asset() -> list[dict]:
    """Restituisce statistiche per ogni asset."""
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT 
                symbol,
                COUNT(*) as total,
                SUM(CASE WHEN result = 'tp' THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN result = 'sl' THEN 1 ELSE 0 END) as losses,
                SUM(CASE WHEN result = 'force_close' THEN 1 ELSE 0 END) as force,
                SUM(CASE WHEN result = 'breakeven' THEN 1 ELSE 0 END) as breakeven,
                ROUND(AVG(pnl_pct)::numeric, 2) as avg_pnl
            FROM trades
            WHERE status = 'closed'
            GROUP BY symbol
            ORDER BY total DESC
        """)
        rows = cur.fetchall()
        release_db(conn)
        return [
            {
                "symbol":  r[0],
                "total":   r[1],
                "wins":    r[2],
                "losses":  r[3],
                "force":   r[4],
                "breakeven": r[5],
                "avg_pnl": float(r[6]) if r[6] else 0.0,
            }
            for r in rows
        ]
    except Exception as e:
        logger.error(f"DB get_stats_by_asset error: {e}")
        return []

def get_stats_by_direction() -> list[dict]:
    """Restituisce statistiche per direzione LONG e SHORT."""
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT 
                direction,
                COUNT(*) as total,
                SUM(CASE WHEN result = 'tp' THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN result = 'sl' THEN 1 ELSE 0 END) as losses,
                SUM(CASE WHEN result = 'force_close' THEN 1 ELSE 0 END) as force,
                SUM(CASE WHEN result = 'breakeven' THEN 1 ELSE 0 END) as breakeven,
                ROUND(AVG(pnl_pct)::numeric, 2) as avg_pnl
            FROM trades
            WHERE status = 'closed'
            GROUP BY direction
            ORDER BY direction
        """)
        rows = cur.fetchall()
        release_db(conn)
        return [
            {
                "direction": r[0],
                "total":     r[1],
                "wins":      r[2],
                "losses":    r[3],
                "force":     r[4],
                "breakeven": r[5],
                "avg_pnl":   float(r[6]) if r[6] else 0.0,
            }
            for r in rows
        ]
    except Exception as e:
        logger.error(f"DB get_stats_by_direction error: {e}")
        return []

def save_decay_cooldown(symbol: str) -> None:
    """Salva il timestamp del cooldown decadimento nel database."""
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO config (key, value, updated_at) VALUES (%s,%s,%s) ON CONFLICT (key) DO UPDATE SET value=%s, updated_at=%s",
            (f"cooldown_{symbol}", _now_iso(), _now_iso(), _now_iso(), _now_iso())
        )
        conn.commit()
        release_db(conn)
    except Exception as e:
        logger.error(f"DB save_decay_cooldown error: {e}")

def load_decay_cooldowns() -> dict:
    """Carica i cooldown attivi dal database."""
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT key, value FROM config WHERE key LIKE 'cooldown_%'")
        rows = cur.fetchall()
        release_db(conn)
        result = {}
        for r in rows:
            symbol = r[0].replace("cooldown_", "")
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(r[1])
            result[symbol] = dt
        return result
    except Exception as e:
        logger.error(f"DB load_decay_cooldowns error: {e}")
        return {}

def clear_decay_cooldown(symbol: str) -> None:
    """Rimuove il cooldown dal database."""
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("DELETE FROM config WHERE key=%s", (f"cooldown_{symbol}",))
        conn.commit()
        release_db(conn)
    except Exception as e:
        logger.error(f"DB clear_decay_cooldown error: {e}")

def get_stats_by_hour() -> list[dict]:
    """Restituisce statistiche per ora del giorno (sessione 7:00-20:00 UTC)."""
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT 
                EXTRACT(HOUR FROM opened_at) as hour,
                COUNT(*) as total,
                SUM(CASE WHEN result = 'tp' THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN result = 'sl' THEN 1 ELSE 0 END) as losses,
                SUM(CASE WHEN result = 'breakeven' THEN 1 ELSE 0 END) as breakeven,
                ROUND(AVG(pnl_pct)::numeric, 2) as avg_pnl
            FROM trades
            WHERE status = 'closed'
            GROUP BY hour
            ORDER BY hour
        """)
        rows = cur.fetchall()
        release_db(conn)

        db_data = {int(r[0]): {"total": r[1], "wins": r[2], "losses": r[3], "breakeven": r[4], "avg_pnl": float(r[5]) if r[5] else 0.0} for r in rows}

        result = []
        for hour in range(7, 20):
            if hour in db_data:
                d = db_data[hour]
                result.append({
                    "hour":      hour,
                    "total":     d["total"],
                    "wins":      d["wins"],
                    "losses":    d["losses"],
                    "breakeven": d["breakeven"],
                    "avg_pnl":   d["avg_pnl"],
                })
            else:
                result.append({
                    "hour":      hour,
                    "total":     0,
                    "wins":      0,
                    "losses":    0,
                    "breakeven": 0,
                    "avg_pnl":   0.0,
                })

        return result
    except Exception as e:
        logger.error(f"DB get_stats_by_hour error: {e}")
        return []

def get_weekly_trades(start_date: str, end_date: str) -> list[dict]:
    """Restituisce i trade chiusi in un intervallo di date."""
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT symbol, direction, entry, exit_price, pnl_pct, result, pattern, opened_at
            FROM trades
            WHERE status = 'closed'
            AND date >= %s AND date <= %s
            ORDER BY opened_at
        """, (start_date, end_date))
        rows = cur.fetchall()
        release_db(conn)
        return [
            {
                "symbol":     r[0],
                "direction":  r[1],
                "entry":      float(r[2]),
                "exit_price": float(r[3]) if r[3] else 0.0,
                "pnl_pct":    float(r[4]) if r[4] else 0.0,
                "result":     r[5],
                "pattern":    r[6],
                "opened_at":  r[7],
            }
            for r in rows
        ]
    except Exception as e:
        logger.error(f"DB get_weekly_trades error: {e}")
        return []

def get_trades_range(start_date: str, end_date: str) -> list[dict]:
    """Restituisce i trade chiusi in un intervallo di date."""
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT symbol, direction, entry, exit_price, pnl_pct, result, pattern, opened_at
            FROM trades
            WHERE status = 'closed'
            AND date >= %s AND date <= %s
            ORDER BY opened_at
        """, (start_date, end_date))
        rows = cur.fetchall()
        release_db(conn)
        return [
            {
                "symbol":     r[0],
                "direction":  r[1],
                "entry":      float(r[2]),
                "exit_price": float(r[3]) if r[3] else 0.0,
                "pnl_pct":    float(r[4]) if r[4] else 0.0,
                "result":     r[5],
                "pattern":    r[6],
                "opened_at":  r[7],
            }
            for r in rows
        ]
    except Exception as e:
        logger.error(f"DB get_trades_range error: {e}")
        return []

def update_sl_order_id(symbol: str, sl_order_id: str) -> None:
    """Aggiorna l'ID dell'ordine SL attivo nel DB dopo ogni modifica trailing."""
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "UPDATE trades SET sl_order_id=%s WHERE symbol=%s AND status='open'",
            (sl_order_id, symbol)
        )
        conn.commit()
        release_db(conn)
    except Exception as e:
        logger.error(f"DB update_sl_order_id error: {e}")        

def get_first_trade_date() -> str | None:
    """Restituisce la data del primo trade nel database."""
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT MIN(date) FROM trades WHERE status = 'closed'")
        row = cur.fetchone()
        release_db(conn)
        if row and row[0]:
            return str(row[0])
        return None
    except Exception as e:
        logger.error(f"DB get_first_trade_date error: {e}")
        return None                                                      