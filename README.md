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
- 5m: candle verde, EMA9 > EMA21, 45 < RSI < 70, corp `|close-open|` > ATR*0.3

**Iesire** (prima adevarata castiga):
1. Trailing stop (activ la +1.5%, iese la -1% de la max)
2. Stop loss -1.5%
3. Take profit +4%
4. EMA9 < EMA21 (doar peste +0.3%, cat sa acopere costul iesirii)
5. RSI(5m) > 78

Fiecare inchidere retine si excursia parcursa — `mfe_pct` (cat de sus a ajuns pozitia),
`mae_pct` (cat de jos) si `durata_min` — in `memorie_multitf.json` si in CSV-ul zilnic.
Excursiile sunt esantionate la fiecare ciclu (60s) pe ultimul pret tranzactionat, deci sunt
o limita inferioara a miscarii reale. Sunt baza pentru calibrarea trailing-ului si a lui
`TAKE_PROFIT_PCT` — fara ele nu se vede decat unde s-a inchis pozitia, nu si unde a ajuns.
Tranzactiile de dinaintea instrumentarii au coloanele goale.

**Filtre**: cooldown 4h dupa pierdere, blocare earnings (1 zi), max 5 pozitii,
max trades/zi din .env, fara intrari cu 2h inainte de inchidere, inchidere fortata cu 15 min inainte.

## Instalare

```bash
# 1. Copiaza fisierele in ~/trading-v3/
# 2. Configureaza .env (copiaza din .env.example, pune cheile tale)
cp .env.example .env
nano .env

# 3. Instaleaza dependentele (foloseste venv-ul existent sau creeaza unul)
~/trading-v3/venv/bin/pip install -r requirements.txt

# 4. Instaleaza serviciile
sudo cp trading.service /etc/systemd/system/trading-v3.service
sudo cp dashboard.service /etc/systemd/system/dashboard-v3.service
sudo cp vwap-shadow.service /etc/systemd/system/vwap-shadow.service
sudo cp ofi-vwap-shadow.service /etc/systemd/system/ofi-vwap-shadow.service
sudo systemctl daemon-reload
```

## VWAP Pullback — shadow mode

Scannerul `vwap-shadow.service` calculeaza VWAP pe bare de 5 minute si scrie
semnalele confirmate in `vwap_shadow_signals.csv`, fara sa trimita ordine.

Analiza se face **doar pe ultima bara incheiata din sesiunea regulara** (09:30-16:00 NY):

- barele de pre-market si after-hours sunt excluse — pe IEX sunt subtiri si ancorau VWAP-ul
  de sesiune pe cateva print-uri cu volum aproape nul, ceea ce facea ca orice bara de dupa
  deschidere sa para pullback;
- bara in formare e taiata (`VWAP_DURATA_BARA_MIN`, 5) — inainte `Close` si `Volume` se mai
  schimbau dupa ce semnalul era deja scris;
- barele mai vechi de `VWAP_VECHIME_MAXIMA_MIN` (15) sunt respinse cu `bara invechita`, ca sa
  nu se mai emita semnale pe ultima bara de ieri cand la deschidere nu exista inca date de azi;
- primele `VWAP_WARMUP_MIN` (30) minute de sesiune sunt sarite: `VolumeMA20` acopera acolo
  barele de peste noapte, deci filtrul de volum trecea automat.

Pentru status si log:

```bash
systemctl status vwap-shadow.service --no-pager
journalctl -u vwap-shadow.service -f
```

State-ul pentru deduplicarea semnalelor este in `vwap_shadow_state.json`.

## OFI + VWAP — shadow mode

`ofi-vwap-shadow.service` asculta quote-uri Alpaca IEX, calculeaza OFI real
pe minute si cere confirmarea VWAP Pullback. Nu trimite ordine. Barele OFI
sunt scrise in `ofi_shadow_bars.csv`, iar semnalele confirmate in
`ofi_vwap_shadow_signals.csv`.

**Toate** barele se scriu in `ofi_shadow_bars.csv`, si cele respinse — coloana `motiv` spune
de ce (`quote-uri insuficiente` sub `OFI_MIN_QUOTES`, `ofi sub prag` sub `OFI_MIN_RATIO`,
`candidat` daca s-a cerut confirmarea VWAP). Fara asta nu se putea distinge "n-au fost date"
de "a fost filtrat", deci nu se puteau calibra pragurile. Daca schema CSV-ului se schimba,
fisierul vechi e rotit in `.bak` si se scrie unul nou cu antetul curent.

Agregarea pe minut a fost reparata: timestamp-urile Alpaca au precizie de nanosecunda,
iar `replace(microsecond=0)` nu atinge campul `nanosecond`, deci fiecare quote primea
propriul bucket si nu se agrega nimic. De aceea aproape toate barele cadeau sub
`OFI_MIN_QUOTES` si serviciul parea mut.

Barele se inchid pe baza timpului, pe toate simbolurile — nu doar pe cel care tocmai a
primit un quote — altfel barele simbolurilor tacute ramaneau blocate in memorie, nescrise
si neevaluate. Confirmarea VWAP (apel HTTP blocant) ruleaza pe un thread separat, ca sa nu
opreasca stream-ul de quote-uri.

Pragurile au fost recalibrate pe 6 septembrie 2026, pe 57.762 de bare eligibile colectate
intre 26 august si 4 septembrie:

- `OFI_MIN_QUOTES` (10) **nu mai e constrangerea** — dupa repararea agregarii pe minut,
  mediana e 208 quote-uri/minut (p25=58), iar sub prag cad doar 988 de bare din ~60.000.
- `OFI_MIN_RATIO` a coborat de la **0.25 la 0.08**. Distributia reala a lui `ofi_ratio` e
  p90=+0.021, p95=+0.033, p99=+0.068, max=+0.539 — cvasi-simetrica in jurul lui zero, deci
  fluxul de ordine pe IEX nu are directie persistenta. La 0.25 treceau 8 bare in 8 zile
  (~1/zi pe 20 de simboluri) si nu s-a confirmat niciun semnal; la 0.08 trec ~45 de bare/zi.

Comparatia se face pe valoarea **cu semn**, nu pe modul: doar presiunea de cumparare
conteaza, fiind o strategie long-only.

```bash
systemctl status ofi-vwap-shadow.service --no-pager
journalctl -u ofi-vwap-shadow.service -f
```

## Pornire / Oprire / Restart

```bash
# Pornire
sudo systemctl start trading-v3.service
sudo systemctl start dashboard-v3.service
sudo systemctl start vwap-shadow.service
sudo systemctl start ofi-vwap-shadow.service

# Pornire automata la boot
sudo systemctl enable trading-v3.service
sudo systemctl enable dashboard-v3.service
sudo systemctl enable vwap-shadow.service
sudo systemctl enable ofi-vwap-shadow.service

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
