# Crypto Breakout Bot

Bot di trading automatico su BTC/USDT e ETH/USDT basato sulla strategia **Breakout Previous Day High/Low** con conferma su retest e pattern candele.

---

## Strategia

1. **Breakout PDH/PDL su 15m** — il prezzo rompe il massimo o minimo del giorno precedente con un buffer configurabile
2. **Filtro trend EMA 20/50** — il breakout è valido solo se allineato al trend
3. **Retest su 5m** — il prezzo torna sul livello rotto entro la zona di tolleranza
4. **Pattern candele** — hammer, bullish/bearish engulfing, shooting star
5. **Filtro ATR** — lo stop loss non può essere più distante di `MAX_RISK_ATR × ATR`
6. **Sessione 14:00–22:00 UTC** — chiusura forzata alle 23:55

---

## Stack tecnico

| Componente | Tecnologia |
|---|---|
| Trading | Python + ccxt |
| Notifiche | python-telegram-bot |
| Database | Supabase (PostgreSQL) |
| Deploy | Railway (cloud 24/7) |

---

## Struttura file

```
crypto_breakout_bot/
├── main.py          # Entry point
├── config.py        # Tutte le variabili di configurazione
├── bot.py           # Loop principale di trading
├── binance_api.py   # Connessione Binance, indicatori, ordini
├── telegram_bot.py  # Comandi e notifiche Telegram
├── database.py      # Interfaccia Supabase
├── backtest.py      # Backtesting storico
├── requirements.txt
└── README.md
```

---

## Setup

### 1. Installa le dipendenze

```bash
pip install -r requirements.txt
```

### 2. Configura `config.py`

Inserisci le tue chiavi:

```python
TESTNET = True   # False per andare live

BINANCE_TESTNET_API_KEY    = "..."
BINANCE_TESTNET_API_SECRET = "..."

TELEGRAM_BOT_TOKEN = "..."
TELEGRAM_CHAT_ID   = "..."

SUPABASE_URL = "https://xxx.supabase.co"
SUPABASE_KEY = "your-anon-key"
```

### 3. Crea le tabelle Supabase

Esegui questo SQL nell'editor SQL di Supabase:

```sql
-- Segnali rilevati
create table signals (
  id          bigserial primary key,
  symbol      text not null,
  direction   text not null,
  pdh         numeric,
  pdl         numeric,
  created_at  timestamptz default now()
);

-- Trade eseguiti
create table trades (
  id          bigserial primary key,
  symbol      text not null,
  direction   text not null,
  entry       numeric,
  sl          numeric,
  tp          numeric,
  qty         numeric,
  pattern     text,
  status      text default 'open',
  result      text,
  exit_price  numeric,
  date        date,
  opened_at   timestamptz default now(),
  closed_at   timestamptz
);

-- Configurazione dinamica
create table config (
  id          bigserial primary key,
  key         text unique not null,
  value       text not null,
  updated_at  timestamptz default now()
);

-- Equity curve
create table equity (
  id          bigserial primary key,
  date        date unique,
  balance     numeric,
  created_at  timestamptz default now()
);
```

### 4. Avvia il bot

```bash
python main.py
```

Poi usa `/start` su Telegram.

---

## Comandi Telegram

| Comando | Descrizione |
|---|---|
| `/start` | Avvia il bot di trading |
| `/stop` | Ferma il bot |
| `/status` | Stato corrente, equity, posizioni aperte |
| `/trade` | Dettaglio trade aperti con PnL live |
| `/report` | Riepilogo trade del giorno |
| `/equity` | Curva equity ultimi 30 giorni |
| `/parametri` | Parametri correnti |
| `/set rr 2.5` | Modifica il Risk/Reward ratio |

---

## Notifiche automatiche

| Emoji | Evento |
|---|---|
| 🔍 | Segnale rilevato (breakout confermato) |
| 📈 | Ordine aperto |
| ✅ | Target (TP) raggiunto |
| 🔴 | Stop Loss colpito |
| 📊 | Report serale 22:00 |
| ⚠️ | Errori e chiusura forzata |

---

## Backtesting

```bash
python backtest.py --symbol BTC/USDT --days 90 --rr 2.0
python backtest.py --symbol ETH/USDT --days 60 --rr 2.5
```

---

## Deploy su Railway

1. Crea un nuovo progetto su [railway.app](https://railway.app)
2. Collega il repository GitHub
3. Aggiungi le variabili d'ambiente (le stesse di `config.py`)
4. Il bot si avvia automaticamente con `python main.py`
5. Ogni push su `main` fa il redeploy automatico

---

## Passaggio Testnet → Live

In `config.py`:

```python
TESTNET = False
```

Inserisci le chiavi live e riavvia. Il resto del codice non cambia.

---

## Parametri principali

| Parametro | Valore default | Descrizione |
|---|---|---|
| `ATR_PERIOD` | 14 | Periodo ATR |
| `EMA_FAST` | 20 | EMA veloce |
| `EMA_SLOW` | 50 | EMA lenta |
| `RISK_REWARD_RATIO` | 2.0 | RR (modificabile via `/set rr`) |
| `MAX_RISK_ATR` | 1.30 | Moltiplicatore max SL su ATR |
| `BREAKOUT_BUFFER` | 0.10% | Buffer sopra/sotto PDH/PDL |
| `RETEST_BUFFER` | 0.15% | Tolleranza zona retest |
| `RISK_PER_TRADE_PCT` | 1.0% | Rischio per trade sul capitale |
