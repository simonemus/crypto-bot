# ============================================================
#  CRYPTO BREAKOUT BOT — backtest.py
#  Backtesting della strategia Breakout PDH/PDL su dati storici.
#  Uso: python backtest.py --symbol BTC/USDT --days 90
# ============================================================

import argparse
import logging
from datetime import datetime, timezone, timedelta

import pandas as pd

import config
from binance_api import (
    get_exchange, fetch_ohlcv, add_indicators,
    check_breakout, check_retest, detect_pattern,
    trend_ok, atr_ok, calc_sl_tp,
)

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)


# ── BACKTEST ENGINE ───────────────────────────────────────────

def run_backtest(symbol: str, days: int = 90, rr: float = None) -> dict:
    """
    Esegue il backtest per `symbol` sugli ultimi `days` giorni.
    Restituisce un dizionario con statistiche complete.
    """
    rr = rr or config.RISK_REWARD_RATIO
    print(f"\n{'='*55}")
    print(f" BACKTEST — {symbol} | Ultimi {days} giorni | RR={rr}")
    print(f"{'='*55}")

    exchange = get_exchange()

    # Scarica tutto il 15m necessario
    limit_15m = days * 24 * 4 + 200   # candele 15m per `days` giorni + buffer
    df_15_all = fetch_ohlcv(exchange, symbol, "15m", limit=min(limit_15m, 1000))
    df_15_all = add_indicators(df_15_all)

    # Scarica tutto il 5m
    limit_5m = days * 24 * 12 + 200
    df_5_all  = fetch_ohlcv(exchange, symbol, "5m",  limit=min(limit_5m, 1000))
    df_5_all  = add_indicators(df_5_all)

    # Scarica daily per PDH/PDL
    df_day = fetch_ohlcv(exchange, symbol, "1d", limit=days + 5)

    trades = []
    current_date = None
    pdh = pdl = None
    daily_trade_count = 0

    print(f"\n{'Symbol':<12} {'Date':<12} {'Dir':<6} {'Entry':<12} {'SL':<12} {'TP':<12} {'Result':<8} {'PnL%':<8}")
    print("-" * 80)

    for i in range(2, len(df_day) - 1):
        day_row  = df_day.iloc[i]
        prev_row = df_day.iloc[i - 1]
        day_date = day_row.name.date()

        # Weekend filter
        if config.WEEKEND_FILTER and day_date.weekday() >= 5:
            continue

        pdh = float(prev_row["high"])
        pdl = float(prev_row["low"])
        daily_trade_count = 0

        # Filtra le candele 15m di questo giorno nella sessione
        day_start = pd.Timestamp(day_date, tz="UTC") + pd.Timedelta(hours=config.SESSION_START_HOUR)
        day_end   = pd.Timestamp(day_date, tz="UTC") + pd.Timedelta(hours=config.SESSION_END_HOUR)

        df_15_day = df_15_all[(df_15_all.index >= day_start) & (df_15_all.index < day_end)]
        df_5_day  = df_5_all[ (df_5_all.index  >= day_start) & (df_5_all.index  < day_end)]

        breakout_dir   = None
        breakout_found = False

        for j in range(2, len(df_15_day)):
            if daily_trade_count >= config.MAX_TRADES_PER_DAY_PER_ASSET:
                break

            slice_15 = df_15_day.iloc[:j + 1]

            # Breakout
            direction = check_breakout(slice_15, pdh, pdl)
            if not direction:
                continue
            if breakout_found and breakout_dir == direction:
                pass   # già trovato, cerco il retest
            else:
                if not trend_ok(slice_15, direction):
                    continue
                breakout_dir   = direction
                breakout_found = True

            # Retest su 5m
            candle_time = slice_15.index[-1]
            df_5_window = df_5_day[df_5_day.index <= candle_time + pd.Timedelta(minutes=60)]
            if len(df_5_window) < 3:
                continue

            if not check_retest(df_5_window, pdh, pdl, direction):
                continue

            # Pattern candele
            pattern = detect_pattern(df_5_window, direction)
            if not pattern:
                continue

            # Entry / SL / TP
            entry = float(df_5_window.iloc[-2]["close"])
            atr_v = float(slice_15.iloc[-2]["atr"])
            sl, tp = calc_sl_tp(entry, direction, atr_v, rr)

            if not atr_ok(slice_15, entry, sl):
                continue

            # Simula l'esito: scorri le candele 5m successive
            future_5m = df_5_day[df_5_day.index > df_5_window.index[-1]]

            # Chiusura forzata ora
            force_close_time = pd.Timestamp(day_date, tz="UTC") + pd.Timedelta(
                hours=config.FORCE_CLOSE_HOUR, minutes=config.FORCE_CLOSE_MINUTE
            )

            result    = "open"
            exit_price = entry

            for _, frow in future_5m.iterrows():
                if frow.name >= force_close_time:
                    result     = "force_close"
                    exit_price = float(frow["close"])
                    break
                if direction == "long":
                    if frow["low"]  <= sl: result = "sl"; exit_price = sl; break
                    if frow["high"] >= tp: result = "tp"; exit_price = tp; break
                else:
                    if frow["high"] >= sl: result = "sl"; exit_price = sl; break
                    if frow["low"]  <= tp: result = "tp"; exit_price = tp; break

            if result == "open":
                result = "force_close"; exit_price = entry

            pnl_pct = (exit_price - entry) / entry * 100
            if direction == "short":
                pnl_pct = -pnl_pct

            trade = {
                "date":      str(day_date),
                "direction": direction,
                "entry":     entry,
                "sl":        sl,
                "tp":        tp,
                "pattern":   pattern,
                "result":    result,
                "exit":      exit_price,
                "pnl_pct":   round(pnl_pct, 3),
            }
            trades.append(trade)
            daily_trade_count += 1
            breakout_found = False   # reset per non ri-entrare

            sign = "+" if pnl_pct >= 0 else ""
            marker = "✅" if result == "tp" else ("🔴" if result == "sl" else "⚠️")
            print(
                f"{symbol:<12} {str(day_date):<12} {direction:<6} "
                f"{entry:<12.4f} {sl:<12.4f} {tp:<12.4f} "
                f"{marker}{result:<7} {sign}{pnl_pct:.2f}%"
            )

    # ── STATISTICHE ────────────────────────────────────────────
    total  = len(trades)
    wins   = [t for t in trades if t["result"] == "tp"]
    losses = [t for t in trades if t["result"] == "sl"]
    force  = [t for t in trades if t["result"] == "force_close"]

    winrate   = len(wins) / total * 100 if total else 0
    avg_win   = sum(t["pnl_pct"] for t in wins)   / len(wins)   if wins   else 0
    avg_loss  = sum(t["pnl_pct"] for t in losses) / len(losses) if losses else 0
    total_pnl = sum(t["pnl_pct"] for t in trades)

    # Equity simulata (partendo da 1000 unità)
    equity = 1000.0
    peak   = equity
    max_dd = 0.0
    for t in trades:
        equity *= (1 + t["pnl_pct"] / 100)
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak * 100
        if dd > max_dd:
            max_dd = dd

    stats = {
        "symbol":     symbol,
        "days":       days,
        "rr":         rr,
        "total":      total,
        "wins":       len(wins),
        "losses":     len(losses),
        "force":      len(force),
        "winrate":    round(winrate, 1),
        "avg_win":    round(avg_win, 2),
        "avg_loss":   round(avg_loss, 2),
        "total_pnl":  round(total_pnl, 2),
        "final_eq":   round(equity, 2),
        "max_dd":     round(max_dd, 2),
    }

    print(f"\n{'─'*55}")
    print(f" Totale trade : {total}")
    print(f" Win/Loss/FC  : {len(wins)} / {len(losses)} / {len(force)}")
    print(f" Win rate     : {winrate:.1f}%")
    print(f" Avg win      : +{avg_win:.2f}%")
    print(f" Avg loss     : {avg_loss:.2f}%")
    print(f" PnL totale   : {'+' if total_pnl >= 0 else ''}{total_pnl:.2f}%")
    print(f" Equity finale: {equity:.2f} (start 1000)")
    print(f" Max drawdown : -{max_dd:.2f}%")
    print(f"{'='*55}\n")

    return stats


# ── CLI ───────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backtest Breakout Bot")
    parser.add_argument("--symbol", default="BTC/USDT", help="es. BTC/USDT")
    parser.add_argument("--days",   type=int, default=90, help="Giorni storici")
    parser.add_argument("--rr",     type=float, default=None, help="Risk/Reward ratio")
    args = parser.parse_args()

    run_backtest(symbol=args.symbol, days=args.days, rr=args.rr)
