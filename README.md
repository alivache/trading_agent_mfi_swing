# 🤖 Trading Agent — Ghid Complet

Sistem automat de tranzacționare pe acțiuni americane, rulând pe Google Cloud VM.
Cont paper Alpaca ($100k virtual). Strategii: multi-timeframe (1D+15m+5m) și swing HMM.

---

## 📋 Structura Proiectului

```
/home/liviu_anton/trading/
├── multi_tf_strategy.py      # Strategia principală (live)
├── swing_final.py            # Strategia swing cu HMM (de testat)
├── dashboard.py              # Dashboard web Flask (port 8080)
├── statistici.py             # Statistici cumulate din terminal
├── genereaza_csv.py          # Recuperare manuală raport CSV
├── raport_zilnic.py          # Raport automat (cron, 16:15 ET)
├── sync_pozitii.py           # Sincronizare poziții Alpaca → memorie
├── backtest.py               # Backtest strategia multi_tf
├── backtest_optim.py         # Backtest 4 variante SL/TP
├── backtest_swing.py         # Backtest strategia swing HMM
├── backtest_relaxare.py      # Backtest relaxare filtre intrare
├── .env                      # Chei API Alpaca + configurare
├── memorie_multitf.json      # Memorie agent (tranzacții, stats)
├── pozitii_active.json       # Poziții deschise (persistă la restart)
├── grafice_cache.json        # Cache grafice (scris de agent, citit de dashboard)
├── agent.log                 # Log agent live
├── dashboard.log             # Log dashboard
├── cron_raport.log           # Log raport zilnic automat
├── multitf_trades_*.csv      # Rapoarte zilnice generate automat
└── venv/                     # Virtual environment Python
```

---

## 🖥️ Mediul de Lucru

| Element | Detalii |
|---|---|
| VM | Google Cloud, e2-micro, Ubuntu 24.04, us-west1 |
| Nume VM | trading-agent |
| User | liviu_anton |
| Fus orar VM | America/New_York (setat cu timedatectl) |
| Python | ~/trading/venv/bin/python3 (folosește fără activare venv) |
| Dashboard URL | http://mini-trading.duckdns.org:8080 |
| Alpaca | Paper trading, cont principal |

---

## 🚀 Pornire / Oprire Servicii

### Strategia multi_tf (LIVE)

```bash
# Pornire
sudo systemctl start trading.service

# Oprire
sudo systemctl stop trading.service

# Repornire (dupa modificari cod)
sudo systemctl restart trading.service

# Verifica status
sudo systemctl status trading.service

# Vezi log-ul live
tail -30 ~/trading/agent.log

# Urmareste log-ul in timp real
tail -f ~/trading/agent.log

# Goleste log-ul (datele raman in CSV si memorie)
> ~/trading/agent.log
```

### Dashboard Web

```bash
# Pornire
sudo systemctl start dashboard.service

# Oprire
sudo systemctl stop dashboard.service

# Repornire
sudo systemctl restart dashboard.service

# Verifica status
sudo systemctl status dashboard.service

# Test local
curl -s http://localhost:8080 | head -5
```

### Repornire ambele servicii (dupa update cod)

```bash
sudo systemctl restart trading.service
sudo systemctl restart dashboard.service
```

### Reincarcarea configuratiei systemd (dupa modificare fisiere .service)

```bash
sudo systemctl daemon-reload
sudo systemctl restart trading.service
```

---

## 📊 Dashboard — Cum se Folosește

Accesează: **http://mini-trading.duckdns.org:8080**

### Tab Dashboard (📊)
- **Carduri sus**: Cash, Portofoliu, Profit AZI, P&L Total, Win Rate, Status bursă
- **Tranzacții AZI**: Tabel live cu toate operațiunile zilei curente
- **Log agent**: Ultimele 30 linii din agent.log, colorate pe tipuri
- **Poziții deschise**: Grafice candlestick cu EMA9/EMA21 + SL/TP + RSI
- **Watchlist**: Toate cele 20 acțiuni, 2 pe rând, cu status 1D

### Tab Statistici (📈)
- **Verdict profit factor**: Bandă colorată sus
- **Metrici cumulate**: Win rate, PF, câștig/pierdere medie
- **Performanță pe zi**: Click pe orice dată → vezi tranzacțiile din ziua respectivă
- **Performanță pe simbol** și **pe motiv de ieșire**

Auto-refresh la 120 secunde. Graficele vin din cache-ul agentului (zero cereri yfinance).

---

## ⚙️ Configurare (.env)

Fișierul `/home/liviu_anton/trading/.env`:

```env
ALPACA_API_KEY=cheia_ta
ALPACA_SECRET_KEY=secretul_tau
ALPACA_BASE_URL=https://paper-api.alpaca.markets
MAX_TRADE_SIZE_USD=2500
MAX_TRADES_PER_DAY=100
ACTIUNI=AAPL,MSFT,NVDA,GOOGL,AMZN,NFLX,META,AMD,TSLA,AVGO,CRM,ADBE,ORCL,CSCO,INTC,QCOM,TXN,AMAT,MU,PYPL
EARNINGS=NVDA:2026-05-20,AAPL:2026-07-30,MSFT:2026-07-29,GOOGL:2026-07-29,AMZN:2026-07-30,META:2026-07-29
```

---

## 📈 Strategia multi_tf (LIVE) — Parametri Optimizați

Fișier: `multi_tf_strategy.py`

### Logica de intrare (3 timeframe-uri aliniate):
1. **1D**: Preț > EMA50 > EMA200, RSI 40-75 (trend bullish)
2. **15m**: EMA20 > EMA50, pullback < 2% de EMA20, RSI 25-60
3. **5m**: Candle verde, EMA9 > EMA21, RSI 45-70, range > 30% ATR

### Logica de ieșire:
- Stop Loss: 1.5%
- Take Profit: 4.0%
- Trailing Stop: 1.0% (se activează la +1.5%)
- RSI Overbought: > 78
- EMA9 < EMA21 (dacă e pe profit)
- End of Day: 15 min înainte de închidere

### Reguli suplimentare:
- Nu deschide poziții noi în ultimele 2 ore
- Cooldown 4h după pierdere pe un simbol
- Max 5 poziții simultane
- Earnings guard (blochează 1 zi înainte de raport)
- Raport CSV automat la închiderea bursei
- Cache grafice la fiecare 5 cicluri (pt dashboard)

### Rezultate backtest (60 zile):
- PF: 1.65 | Win rate: 73% | 64 trades

### Rezultate live (21 zile):
- PF: 1.55 | Win rate: 65% | 129 trades | +$336

---

## 🧠 Strategia swing_final — Parametri

Fișier: `swing_final.py`

### Logica:
- Timeframe: 1H + confirmare 4H
- HMM (Hidden Markov Model) pentru detecție regim piață
- EMA20 > EMA50 + RSI > 45 + MACD bullish + HMM bullish
- Stop Loss: 2%, Trailing după +3%
- Scanare la 5 minute

### Rezultate backtest (60 zile):
- PF: 3.32 | Win rate: 73% | 26 trades | +$771
- ATENȚIE: doar 26 trades = statistic fragil

### NU rulează live momentan (necesită cont Alpaca separat pentru rulare în paralel)

---

## 🔧 Comenzi Utile

### Verificare poziții reale la Alpaca

```bash
~/trading/venv/bin/python3 -c "
import os
from dotenv import load_dotenv
import alpaca_trade_api as tradeapi
load_dotenv()
api = tradeapi.REST(os.getenv('ALPACA_API_KEY'), os.getenv('ALPACA_SECRET_KEY'), os.getenv('ALPACA_BASE_URL'))
poz = api.list_positions()
print(f'Pozitii: {len(poz)}')
for p in poz:
    print(f'  {p.symbol}: {p.qty} @ \${float(p.avg_entry_price):.2f} | P&L: \${float(p.unrealized_pl):.2f}')
"
```

### Statistici cumulate (toate zilele)

```bash
~/trading/venv/bin/python3 ~/trading/statistici.py
```

### Recuperare raport CSV pentru o zi lipsă

```bash
~/trading/venv/bin/python3 ~/trading/genereaza_csv.py 2026-06-17
```

### Sincronizare poziții Alpaca → memorie (după restart sau bug)

```bash
~/trading/venv/bin/python3 ~/trading/sync_pozitii.py
sudo systemctl restart trading.service
```

### Rulare backtest

```bash
# Backtest multi_tf pe 60 zile
~/trading/venv/bin/python3 ~/trading/backtest.py

# Backtest optimizare SL/TP (4 variante)
~/trading/venv/bin/python3 ~/trading/backtest_optim.py

# Backtest relaxare filtre (4 variante)
~/trading/venv/bin/python3 ~/trading/backtest_relaxare.py

# Backtest swing_final cu HMM (lent, 30-60 min)
# Recomandare: ruleaza in screen dupa inchiderea bursei
screen -S backtest
~/trading/venv/bin/python3 ~/trading/backtest_swing.py
# Detaseaza: Ctrl+A apoi D
# Revino: screen -r backtest
```

### Tranzacțiile din ultima săptămână (rapid)

```bash
for f in ~/trading/multitf_trades_2026-06-*.csv; do
    zi=$(basename "$f" | sed 's/multitf_trades_//;s/.csv//')
    trades=$(tail -n +2 "$f" | wc -l)
    profit=$(tail -n +2 "$f" | awk -F',' '{sum+=$6} END {printf "%.2f", sum}')
    echo "  $zi: $trades trades | Profit: \$$profit"
done
```

### Verificare fus orar VM

```bash
date
# Trebuie să afișeze ora America/New_York (EDT/EST)
```

---

## ⏰ Cron Jobs

Editare: `crontab -e`
Verificare: `crontab -l`

### Raport zilnic la 16:15 ET (luni-vineri)
```cron
15 16 * * 1-5 /home/liviu_anton/trading/venv/bin/python3 /home/liviu_anton/trading/raport_zilnic.py >> /home/liviu_anton/trading/cron_raport.log 2>&1
```

---

## 🛡️ Fișiere Systemd

### /etc/systemd/system/trading.service
```ini
[Unit]
Description=Trading Agent
After=network.target

[Service]
Type=simple
User=liviu_anton
WorkingDirectory=/home/liviu_anton/trading
ExecStart=/home/liviu_anton/trading/venv/bin/python3 /home/liviu_anton/trading/multi_tf_strategy.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

### /etc/systemd/system/dashboard.service
```ini
[Unit]
Description=Trading Dashboard
After=network.target

[Service]
Type=simple
User=liviu_anton
WorkingDirectory=/home/liviu_anton/trading
ExecStart=/home/liviu_anton/trading/venv/bin/python3 /home/liviu_anton/trading/dashboard.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

---

## 🐛 Probleme Cunoscute și Soluții

### yfinance "possibly delisted"
**Cauza**: Rate-limit de la Yahoo Finance (prea multe cereri).
**Soluție**: Așteaptă 10-30 min. Dashboard-ul citește din cache, nu din yfinance direct. Agentul are prioritate.

### Poziții pierdute la restart
**Soluție**: Pozițiile se salvează automat în `pozitii_active.json`. Dacă totuși sunt desincronizate:
```bash
~/trading/venv/bin/python3 ~/trading/sync_pozitii.py
sudo systemctl restart trading.service
```

### CSV-ul zilnic nu s-a generat
**Cauza**: Agent repornit sau eroare la generare.
**Soluție**: Recuperează manual:
```bash
~/trading/venv/bin/python3 ~/trading/genereaza_csv.py 2026-06-17
```

### Log-ul e prea mare
**Soluție**: Golește-l (datele sunt în CSV-uri și memorie):
```bash
> ~/trading/agent.log
```

---

## 📊 Performanță Curentă (21 zile, 20 mai - 22 iunie 2026)

| Metric | Valoare |
|---|---|
| Total trades | 129 |
| Win rate | 65.1% |
| Profit total | +$336.12 |
| Profit factor | 1.55 |
| Câștig mediu | $11.32 |
| Pierdere medie | $13.65 |
| Cel mai bun trade | MU +$57.45 |
| Cel mai prost trade | MU -$42.64 |

### Top simboluri: AMD (+$149), MU (+$96), AMAT (+$65)
### Cel mai profitabil mecanism: RSI Overbought (+$578 din 40 trades)

---

## ⚠️ Avertismente Importante

- Acesta este un proiect de ÎNVĂȚARE, nu un sistem de investiții real
- Rulează pe cont PAPER (bani virtuali) — nu investește bani reali fără luni de validare
- Strategia LONG-only pierde în piețe descendente
- Performanța trecută NU garantează rezultate viitoare
- Dashboard-ul este PUBLIC (fără parolă) — nu adăuga butoane de control
- yfinance poate da rate-limit la deschiderea bursei
