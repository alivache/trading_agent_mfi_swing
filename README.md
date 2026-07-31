# Trading Agent Multi-Timeframe (v3)

Sistem de paper trading pe actiuni americane. Long-only, ruleaza 24/7 pe VM Linux.
Strategie multi-timeframe: aliniere 1D + 15m + 5m pentru intrare, iesire pe trailing/SL/TP/EMA/RSI.

**Proiect de invatare, cont paper Alpaca ($100k virtuali). Nu e sistem de investitii real.**
Referinta strategie: profit factor ~1.55, win rate ~65% (129 trades in 21 zile).

## Structura

```
trading-v3/
├── multi_tf_strategy.py   # Agentul principal LIVE (bucla 60s)
├── dashboard.py           # Dashboard Flask (port 8080, read-only)
├── .env                   # Configurare (chei + actiuni) — NU in git
├── trading.service        # systemd agent
├── dashboard.service      # systemd dashboard
└── (fisiere de stare generate automat)
    ├── pozitii_active.json
    ├── memorie_multitf.json
    ├── grafice_cache.json
    ├── agent.log
    └── multitf_trades_YYYY-MM-DD.csv
```

## Strategie (parametri in multi_tf_strategy.py)

**Intrare** — toate 3 timeframe-uri aliniate:
- 1D: pret > EMA50 > EMA200, 40 < RSI < 75
- 15m: EMA20 > EMA50, pullback < 2%, 25 < RSI < 60
- 5m: candle verde, EMA9 > EMA21, 45 < RSI < 70, corp > ATR*0.3

**Iesire** (prima adevarata castiga):
1. Trailing stop (activ la +1.5%, iese la -1% de la max)
2. Stop loss -1.5%
3. Take profit +4%
4. EMA9 < EMA21 (doar pe profit)
5. RSI(5m) > 78

**Filtre**: cooldown 4h dupa pierdere, blocare earnings (1 zi), max 5 pozitii,
max trades/zi din .env, fara intrari cu 2h inainte de inchidere, inchidere fortata cu 15 min inainte.

## Instalare

```bash
# 1. Copiaza fisierele in ~/trading-v3/
# 2. Configureaza .env (copiaza din .env.example, pune cheile tale)
cp .env.example .env
nano .env

# 3. Instaleaza dependentele (foloseste venv-ul existent sau creeaza unul)
~/trading/venv/bin/pip install -r requirements.txt

# 4. Instaleaza serviciile
sudo cp trading.service /etc/systemd/system/trading-v3.service
sudo cp dashboard.service /etc/systemd/system/dashboard-v3.service
sudo systemctl daemon-reload
```

## Pornire / Oprire / Restart

```bash
# Pornire
sudo systemctl start trading-v3.service
sudo systemctl start dashboard-v3.service

# Pornire automata la boot
sudo systemctl enable trading-v3.service
sudo systemctl enable dashboard-v3.service

# Oprire
sudo systemctl stop trading-v3.service

# Restart (dupa modificari)
sudo systemctl restart trading-v3.service

# Status
systemctl status trading-v3.service --no-pager
```

## Citirea log-ului

```bash
# Log-ul agentului (live)
tail -f ~/trading-v3/agent.log

# Log-ul serviciului systemd
journalctl -u trading-v3.service -f

# Ultimele 50 linii
tail -50 ~/trading-v3/agent.log
```

Rulat din terminal, agentul scrie pe ecran. Sub systemd (stdout nu e terminal) scrie in `agent.log`.
Foloseste `--log` daca vrei redirectarea in fisier si dintr-un terminal.

**Toate orele si datele din log, CSV-uri si dashboard sunt in ora New York**, nu a masinii —
ziua de tranzactionare, resetarea contoarelor si cooldown-ul se raporteaza la bursa.

## Dashboard

http://<IP_VM>:8080  (sau prin DuckDNS)

Doua tab-uri: Dashboard (pozitii, tranzactii azi, log, grafice) si Statistici (PF, win rate, pe zi/simbol/motiv).
Plus API: `GET /api/stats` returneaza JSON.

## Probleme cunoscute si solutii

- **Sursa de date**: yfinance a fost eliminat complet — toate datele OHLCV vin de la Alpaca IEX
  (`get_date()`), iar earnings-urile doar din `EARNINGS` in .env. Planul gratuit Alpaca da 200 cereri/min
  si nu returneaza ultimele 15 min de date (de aici decalajul de 16 min din `get_date`).
  Un ciclu complet cere ~60-70 apeluri, deci limita nu e atinsa. Daca totusi vezi "Too Many Requests",
  mareste SCAN_INTERVAL_SEC sau redu numarul de actiuni.
- **Desincronizare pozitii**: daca pozitii_active.json nu mai corespunde cu Alpaca, opreste agentul,
  corecteaza manual fisierul (sau sterge-l ca sa reporneasca de la zero) si reporneste serviciul.
- **Bursa inchisa**: agentul doarme 300s si genereaza raportul. Normal.
- **Serviciul moare**: nu ar trebui (Restart=always + try/except pe bucla). Verifica `journalctl` pentru cauza.
- **VM mic (e2-micro, ~955MB RAM)**: cele doua servicii incap, dar fara marja. Nu porni procese grele in paralel.

## Long-only — avertisment

Strategia e long-only si PIERDE bani in piete descendente. Asta e normal, nu un bug.
Cand majoritatea actiunilor nu-s bullish, agentul sta deoparte (corect).
