#!/usr/bin/env python3
# multi_tf_strategy.py
# Agent principal LIVE — strategie multi-timeframe (1D + 15m + 5m), long-only.
# Bucla infinita, scanare la 60s. Nu moare niciodata.
import os
import sys
import json
import time
import traceback
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
# yfinance eliminat - folosim Alpaca IEX
from dotenv import load_dotenv
import alpaca_trade_api as tradeapi

FOLDER = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(FOLDER, "agent.log")

# ─────────────────────────────────────────────────────────────
# FUSUL ORAR AL PIETEI
# Toata logica de "zi de tranzactionare" (resetare contoare, nume CSV,
# ora din log, cooldown, earnings) se raporteaza la New York, nu la ceasul
# masinii. Altfel un VM pe UTC si unul pe ora Romaniei dau rezultate diferite.
# ─────────────────────────────────────────────────────────────
NY_TZ = ZoneInfo("America/New_York")


def acum_ny():
    """Momentul curent in fusul bursei (aware)."""
    return datetime.now(NY_TZ)


def _redirect_log():
    """Redirecteaza stdout/stderr spre agent.log. Doar cand rulam ca serviciu."""
    _logfile = open(LOG_PATH, "a", buffering=1, encoding="utf-8")
    sys.stdout = _logfile
    sys.stderr = _logfile


load_dotenv(os.path.join(FOLDER, ".env"))

# ─────────────────────────────────────────────────────────────
# CONFIGURARE
# ─────────────────────────────────────────────────────────────
API_KEY = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
MAX_TRADE_SIZE_USD = float(os.getenv("MAX_TRADE_SIZE_USD", 2500))
MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES_PER_DAY", 100))
ACTIUNI_RAW = os.getenv("ACTIUNI", "AAPL,MSFT,NVDA,GOOGL,AMZN,NFLX")
EARNINGS_RAW = os.getenv("EARNINGS", "")

ACTIUNI = [s.strip().upper() for s in ACTIUNI_RAW.split(",") if s.strip()]

EARNINGS_MANUAL = {}
for parte in EARNINGS_RAW.split(","):
    if ":" in parte:
        sym, d = parte.split(":", 1)
        EARNINGS_MANUAL[sym.strip().upper()] = d.strip()

# ─────────────────────────────────────────────────────────────
# PARAMETRI STRATEGIE (constante cu nume)
# ─────────────────────────────────────────────────────────────
SCAN_INTERVAL_SEC = 60
MAX_POZITII = 5
RISC_PORTOFOLIU_PCT = 0.01          # 1% risc pe portofoliu
STOP_LOSS_MIN_PCT = 0.015           # stop loss minim 1.5%
STOP_LOSS_PCT = 0.015               # stop loss fix -1.5%
TAKE_PROFIT_PCT = 0.04              # +4%
TRAILING_ACTIVARE_PCT = 0.015       # trailing se activeaza la +1.5%
TRAILING_DISTANTA_PCT = 0.01        # iesire daca scade 1% de la max
RSI_5M_EXIT = 78                    # iesire daca RSI(5m) > 78
COOLDOWN_ORE = 4                    # cooldown 4h dupa o pierdere
EARNINGS_BLOCARE_ZILE = 1           # blocheaza daca earnings in <= 1 zi
STOP_INTRARI_ORE_INAINTE = 2        # fara intrari noi cu 2h inainte de inchidere
INCHIDERE_MIN_INAINTE = 15          # inchide tot cu 15 min inainte de inchidere
CACHE_GRAFICE_CICLURI = 5           # scrie cache grafice la fiecare 5 cicluri (bursa deschisa)
CACHE_GRAFICE_CICLURI_INCHIS = 10   # la fiecare 10 cand bursa e inchisa

# Fisiere de stare
POZITII_FILE = os.path.join(FOLDER, "pozitii_active.json")
MEMORIE_FILE = os.path.join(FOLDER, "memorie_multitf.json")
GRAFICE_FILE = os.path.join(FOLDER, "grafice_cache.json")

# ─────────────────────────────────────────────────────────────
# API Alpaca
# ─────────────────────────────────────────────────────────────
api = tradeapi.REST(API_KEY, SECRET_KEY, BASE_URL, api_version="v2")


# ═════════════════════════════════════════════════════════════
# INDICATORI (calculati manual cu pandas)
# ═════════════════════════════════════════════════════════════
def calculeaza_ema(preturi, perioada):
    return pd.Series(preturi).ewm(span=perioada, adjust=False).mean().iloc[-1]


def calculeaza_rsi(preturi, perioada=14):
    """RSI cu medii SIMPLE (rolling), nu Wilder. Media pierderilor 0 -> 100."""
    prices = pd.Series(preturi)
    delta = prices.diff()
    castig = delta.where(delta > 0, 0.0)
    pierdere = -delta.where(delta < 0, 0.0)
    avg_c = castig.rolling(window=perioada).mean().iloc[-1]
    avg_p = pierdere.rolling(window=perioada).mean().iloc[-1]
    if avg_p == 0:
        return 100.0
    rs = avg_c / avg_p
    return 100 - (100 / (1 + rs))


def calculeaza_atr(df, perioada=14):
    high = df["High"]
    low = df["Low"]
    close_prev = df["Close"].shift(1)
    tr = pd.concat([
        high - low,
        (high - close_prev).abs(),
        (low - close_prev).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(perioada).mean().iloc[-1]


_TF_MAP = {"1d": "1Day", "15m": "15Min", "5m": "5Min"}
_PERIOD_ZILE = {"200d": 300, "5d": 8, "1d": 3}


def get_date(simbol, interval, period):
    """Descarca OHLCV de la Alpaca IEX (nu yfinance — evita rate-limit)."""
    from datetime import datetime, timedelta, timezone
    tf = _TF_MAP.get(interval, "1Day")
    zile = _PERIOD_ZILE.get(period, 30)
    end = (datetime.now(timezone.utc) - timedelta(minutes=16)).strftime("%Y-%m-%dT%H:%M:%SZ")
    start = (datetime.now(timezone.utc) - timedelta(days=zile)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        bars = api.get_bars(simbol, tf, start=start, end=end, feed="iex").df
        if bars is None or bars.empty:
            return None
        df = bars.rename(columns={
            "open": "Open", "high": "High", "low": "Low",
            "close": "Close", "volume": "Volume"
        })
        return df[["Open", "High", "Low", "Close", "Volume"]]
    except Exception as e:
        print(f"  ⚠️ get_date {simbol} {interval}: {e}")
        return None


# ═════════════════════════════════════════════════════════════
# LOGICA DE INTRARE — 3 timeframe-uri aliniate (short-circuit)
# ═════════════════════════════════════════════════════════════
def verifica_1d(simbol):
    """Prets > EMA50 > EMA200 si 40 < RSI < 75."""
    df = get_date(simbol, "1d", "200d")
    if df is None or len(df) < 200:
        return False, {"motiv": "date 1D insuficiente"}
    inchideri = df["Close"].tolist()
    pret = inchideri[-1]
    ema50 = calculeaza_ema(inchideri, 50)
    ema200 = calculeaza_ema(inchideri, 200)
    rsi = calculeaza_rsi(inchideri, 14)
    ok = (pret > ema50 > ema200) and (40 < rsi < 75)
    return ok, {"pret": round(pret, 2), "ema50": round(ema50, 2),
                "ema200": round(ema200, 2), "rsi": round(rsi, 1)}


def verifica_15m(simbol):
    """EMA20 > EMA50, pullback < 2%, 25 < RSI < 60."""
    df = get_date(simbol, "15m", "5d")
    if df is None or len(df) < 50:
        return False, {"motiv": "date 15m insuficiente"}
    inchideri = df["Close"].tolist()
    pret = inchideri[-1]
    ema20 = calculeaza_ema(inchideri, 20)
    ema50 = calculeaza_ema(inchideri, 50)
    rsi = calculeaza_rsi(inchideri, 14)
    pullback = abs(pret - ema20) / ema20
    ok = (ema20 > ema50) and (pullback < 0.02) and (25 < rsi < 60)
    return ok, {"pret": round(pret, 2), "ema20": round(ema20, 2),
                "ema50": round(ema50, 2), "rsi": round(rsi, 1),
                "pullback": round(pullback * 100, 2)}


def verifica_5m(simbol):
    """Candle verde, EMA9 > EMA21, 45 < RSI < 70, corp > ATR*0.3."""
    df = get_date(simbol, "5m", "1d")
    if df is None or len(df) < 20:
        return False, {"motiv": "date 5m insuficiente"}
    inchideri = df["Close"].tolist()
    pret = inchideri[-1]
    open_ = df["Open"].iloc[-1]
    high = df["High"].iloc[-1]
    low = df["Low"].iloc[-1]
    ema9 = calculeaza_ema(inchideri, 9)
    ema21 = calculeaza_ema(inchideri, 21)
    rsi = calculeaza_rsi(inchideri, 14)
    atr = calculeaza_atr(df, 14)
    verde = pret > open_
    corp_solid = (high - low) > atr * 0.3
    ok = verde and (ema9 > ema21) and (45 < rsi < 70) and corp_solid
    return ok, {"pret": round(pret, 2), "ema9": round(ema9, 2),
                "ema21": round(ema21, 2), "rsi": round(rsi, 1),
                "atr": round(atr, 2)}


def analizeaza_semnal(simbol):
    """Verifica cele 3 timeframe-uri cu short-circuit. Returneaza (bool, motiv, info)."""
    ok1, i1 = verifica_1d(simbol)
    if not ok1:
        return False, "1D nu e bullish", {"1d": i1}
    ok2, i2 = verifica_15m(simbol)
    if not ok2:
        return False, "15m fara pullback", {"1d": i1, "15m": i2}
    ok3, i3 = verifica_5m(simbol)
    if not ok3:
        return False, "5m fara entry", {"1d": i1, "15m": i2, "5m": i3}
    motiv = (f"1D=BULLISH(RSI={i1['rsi']:.0f}) | "
             f"15m=PULLBACK(RSI={i2['rsi']:.0f}) | "
             f"5m=ENTRY(RSI={i3['rsi']:.0f})")
    return True, motiv, {"1d": i1, "15m": i2, "5m": i3}


# ═════════════════════════════════════════════════════════════
# PERSISTENTA
# ═════════════════════════════════════════════════════════════
def incarca_json(cale, implicit):
    if os.path.exists(cale):
        try:
            with open(cale, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return implicit
    return implicit


def salveaza_json(cale, date):
    try:
        with open(cale, "w", encoding="utf-8") as f:
            json.dump(date, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"❌ Eroare salvare {cale}: {e}")


def incarca_pozitii():
    return incarca_json(POZITII_FILE, {})


def salveaza_pozitii(pozitii):
    salveaza_json(POZITII_FILE, pozitii)


def incarca_memorie():
    return incarca_json(MEMORIE_FILE, {
        "tranzactii": [], "performanta": {}, "cooldown": {},
        "stats": {"total_profit": 0, "wins": 0, "losses": 0}
    })


def salveaza_memorie(memorie):
    salveaza_json(MEMORIE_FILE, memorie)


def log_tranzactie(memorie, simbol, tip, pret, cantitate, profit=None, motiv=None):
    acum = acum_ny()
    memorie["tranzactii"].append({
        "simbol": simbol, "tip": tip, "pret": round(pret, 2),
        "cantitate": cantitate, "profit": round(profit, 2) if profit is not None else None,
        "motiv": motiv, "ora": acum.strftime("%H:%M:%S"),
        "data": acum.strftime("%Y-%m-%d")
    })
    if tip == "close_long" and profit is not None:
        if profit < 0:
            memorie["cooldown"][simbol] = acum.isoformat()
            memorie["stats"]["losses"] += 1
        else:
            memorie["stats"]["wins"] += 1
        memorie["stats"]["total_profit"] += profit
        if simbol not in memorie["performanta"]:
            memorie["performanta"][simbol] = {"profit": 0, "trades": 0, "wins": 0}
        p = memorie["performanta"][simbol]
        p["profit"] += profit
        p["trades"] += 1
        if profit > 0:
            p["wins"] += 1


# ═════════════════════════════════════════════════════════════
# FILTRE
# ═════════════════════════════════════════════════════════════
def in_cooldown(memorie, simbol):
    """True daca simbolul e in cooldown (pierdere in ultimele COOLDOWN_ORE)."""
    ts = memorie["cooldown"].get(simbol)
    if not ts:
        return False
    try:
        moment = datetime.fromisoformat(ts)
    except Exception:
        return False
    if moment.tzinfo is None:
        # Intrari scrise inainte de trecerea la ore aware — le citim ca ora NY
        moment = moment.replace(tzinfo=NY_TZ)
    if acum_ny() - moment > timedelta(hours=COOLDOWN_ORE):
        del memorie["cooldown"][simbol]
        return False
    return True


_earnings_cache = {"data": None, "valori": {}}


def are_earnings_curand(simbol):
    """True daca simbolul are earnings in <= EARNINGS_BLOCARE_ZILE. Cache pe zi."""
    azi = acum_ny().strftime("%Y-%m-%d")
    if _earnings_cache["data"] != azi:
        _earnings_cache["data"] = azi
        _earnings_cache["valori"] = {}
    if simbol in _earnings_cache["valori"]:
        return _earnings_cache["valori"][simbol]

    rezultat = False
    data_earnings = None
    if simbol in EARNINGS_MANUAL:
        try:
            data_earnings = datetime.strptime(EARNINGS_MANUAL[simbol], "%Y-%m-%d").date()
        except Exception:
            data_earnings = None

    if data_earnings is not None:
        try:
            if hasattr(data_earnings, "date"):
                data_earnings = data_earnings.date()
            zile = (data_earnings - acum_ny().date()).days
            if 0 <= zile <= EARNINGS_BLOCARE_ZILE:
                rezultat = True
        except Exception:
            rezultat = False

    _earnings_cache["valori"][simbol] = rezultat
    return rezultat


# ═════════════════════════════════════════════════════════════
# DIMENSIONARE POZITIE
# ═════════════════════════════════════════════════════════════
def get_portofoliu():
    try:
        acc = api.get_account()
        return float(acc.portfolio_value)
    except Exception:
        return 100000.0


def calculeaza_cantitate(pret, atr):
    portofoliu = get_portofoliu()
    risc_max = portofoliu * RISC_PORTOFOLIU_PCT
    stop_loss_dinamic = max(STOP_LOSS_MIN_PCT, atr / pret * 1.5)
    cantitate_risc = int(risc_max / (pret * stop_loss_dinamic))
    cantitate_size = int(MAX_TRADE_SIZE_USD / pret)
    return max(1, min(cantitate_risc, cantitate_size))


# ═════════════════════════════════════════════════════════════
# ORDINE
# ═════════════════════════════════════════════════════════════
def plaseaza_ordin(simbol, cantitate, side):
    try:
        api.submit_order(symbol=simbol, qty=cantitate, side=side,
                         type="market", time_in_force="gtc")
        return True
    except Exception as e:
        print(f"❌ Ordin {side} {simbol} esuat: {e}")
        return False


# ═════════════════════════════════════════════════════════════
# IESIRE — 5 conditii, prima adevarata castiga
# ═════════════════════════════════════════════════════════════
def verifica_iesire(simbol, poz, pret_curent, ema9, ema21, rsi_5m):
    """Returneaza (trebuie_iesire, motiv). Actualizeaza pret_max in poz."""
    pret_intrare = poz["pret_intrare"]
    pl_pct = (pret_curent - pret_intrare) / pret_intrare

    # Actualizeaza maximul
    if pret_curent > poz.get("pret_max", pret_intrare):
        poz["pret_max"] = pret_curent

    # 1. Trailing stop (activ la +1.5%, iesire daca scade 1% de la max)
    if pl_pct >= TRAILING_ACTIVARE_PCT:
        poz["trailing_activ"] = True
    if poz.get("trailing_activ"):
        scadere_de_la_max = (poz["pret_max"] - pret_curent) / poz["pret_max"]
        if scadere_de_la_max >= TRAILING_DISTANTA_PCT:
            return True, f"TRAILING STOP (-{scadere_de_la_max*100:.1f}% de la max)"

    # 2. Stop loss -1.5%
    if pl_pct <= -STOP_LOSS_PCT:
        return True, f"STOP LOSS ({pl_pct*100:.1f}%)"

    # 3. Take profit +4%
    if pl_pct >= TAKE_PROFIT_PCT:
        return True, f"TAKE PROFIT (+{pl_pct*100:.1f}%)"

    # 4. EMA9 < EMA21, doar daca pe profit
    if ema9 < ema21 and pl_pct > 0:
        return True, f"EMA CROSS (EMA9<EMA21, +{pl_pct*100:.1f}%)"

    # 5. RSI(5m) > 78
    if rsi_5m > RSI_5M_EXIT:
        return True, f"RSI OVERBOUGHT ({rsi_5m:.1f})"

    return False, None


# ═════════════════════════════════════════════════════════════
# RAPORT CSV ZILNIC
# ═════════════════════════════════════════════════════════════
def genereaza_raport_csv(memorie, zi):
    import csv
    cale = os.path.join(FOLDER, f"multitf_trades_{zi}.csv")
    inchideri = [t for t in memorie["tranzactii"]
                 if t["data"] == zi and t["tip"] == "close_long"]
    if not inchideri:
        print(f"📊 Nicio inchidere pentru raport ({zi})")
        return
    with open(cale, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["data_iesire", "simbol", "cantitate", "pret_intrare",
                    "pret_iesire", "profit_usd", "profit_pct", "motiv_exit", "rezultat"])
        for t in inchideri:
            # Cauta pretul de intrare: primul open_long anterior pe acelasi simbol
            pret_intrare = None
            idx = memorie["tranzactii"].index(t)
            for k in range(idx - 1, -1, -1):
                tp = memorie["tranzactii"][k]
                if tp["simbol"] == t["simbol"] and tp["tip"] == "open_long":
                    pret_intrare = tp["pret"]
                    break
            profit = t.get("profit", 0) or 0
            profit_pct = 0
            if pret_intrare:
                profit_pct = (t["pret"] - pret_intrare) / pret_intrare * 100
            rezultat = "WIN" if profit >= 0 else "LOSS"
            w.writerow([t["data"] + "T" + t["ora"], t["simbol"], t["cantitate"],
                        pret_intrare, t["pret"], round(profit, 2),
                        round(profit_pct, 2), t.get("motiv", ""), rezultat])
    print(f"📄 Raport generat: {cale} ({len(inchideri)} inchideri)")


# ═════════════════════════════════════════════════════════════
# CACHE GRAFICE (pentru dashboard, ca sa nu faca cereri yfinance)
# ═════════════════════════════════════════════════════════════
def actualizeaza_cache_grafice(pozitii):
    cache = {}
    for simbol in ACTIUNI:
        try:
            df = get_date(simbol, "5m", "1d")
            if df is None or len(df) < 21:
                continue
            df = df.tail(80)
            inchideri = df["Close"].tolist()
            ema9_serie = pd.Series(inchideri).ewm(span=9, adjust=False).mean().tolist()
            ema21_serie = pd.Series(inchideri).ewm(span=21, adjust=False).mean().tolist()
            rsi_serie = []
            s = pd.Series(inchideri)
            delta = s.diff()
            castig = delta.where(delta > 0, 0.0).rolling(14).mean()
            pierdere = (-delta.where(delta < 0, 0.0)).rolling(14).mean()
            rs = castig / pierdere.replace(0, 0.0001)
            rsi_serie = (100 - (100 / (1 + rs))).fillna(50).tolist()

            # Status 1D
            ok1, i1 = verifica_1d(simbol)
            if ok1:
                status_1d = "🟢 1D bullish"
            else:
                status_1d = "⏳ 1D nu"

            # Variatie zilnica
            df_zi = get_date(simbol, "1d", "5d")
            var_zi = 0
            if df_zi is not None and len(df_zi) >= 2:
                c = df_zi["Close"].tolist()
                var_zi = (c[-1] - c[-2]) / c[-2] * 100

            cache[simbol] = {
                "dates": [str(x) for x in df.index.tolist()],
                "open": df["Open"].tolist(), "high": df["High"].tolist(),
                "low": df["Low"].tolist(), "close": inchideri,
                "ema9": ema9_serie, "ema21": ema21_serie, "rsi": rsi_serie,
                "status_1d": status_1d, "var_zi": round(var_zi, 2)
            }
        except Exception:
            continue
    salveaza_json(GRAFICE_FILE, cache)


# ═════════════════════════════════════════════════════════════
# BUCLA PRINCIPALA
# ═════════════════════════════════════════════════════════════
def afiseaza_config():
    print("=" * 60)
    print("🤖 AGENT MULTI-TIMEFRAME (1D + 15m + 5m) — LIVE")
    print("=" * 60)
    print(f"🛑 SL={STOP_LOSS_PCT*100:.2f}% | TP={TAKE_PROFIT_PCT*100:.2f}% | "
          f"Trailing={TRAILING_DISTANTA_PCT*100:.1f}% (activ la {TRAILING_ACTIVARE_PCT*100:.1f}%)")
    print(f"🎯 15m: pullback<2.0% | RSI 25-60")
    print(f"📅 Blocare earnings: {EARNINGS_BLOCARE_ZILE} zi | Manual: {len(EARNINGS_MANUAL)} simboluri")
    print(f"🔔 Inchidere: {INCHIDERE_MIN_INAINTE} min inainte | Stop intrari: {STOP_INTRARI_ORE_INAINTE}h inainte")
    print(f"⏱️  Scanare la fiecare {SCAN_INTERVAL_SEC}s | Cache grafice la {CACHE_GRAFICE_CICLURI} cicluri")
    print(f"📋 {len(ACTIUNI)} actiuni: {', '.join(ACTIUNI[:10])}{'...' if len(ACTIUNI) > 10 else ''}")
    print("-" * 60)


def ruleaza():
    afiseaza_config()
    pozitii = incarca_pozitii()
    memorie = incarca_memorie()

    ciclu = 0
    zi_curenta = acum_ny().strftime("%Y-%m-%d")
    trades_azi = 0
    raport_facut = False
    inchidere_facuta = False

    while True:
        try:
            ciclu += 1
            acum = acum_ny()
            zi = acum.strftime("%Y-%m-%d")

            # Detectare zi noua — reset contoare
            if zi != zi_curenta:
                zi_curenta = zi
                trades_azi = 0
                raport_facut = False
                inchidere_facuta = False
                print(f"\n🌅 Zi noua: {zi} — contoare resetate")

            # Status bursa
            try:
                clock = api.get_clock()
                bursa_deschisa = clock.is_open
                next_close = clock.next_close
                next_open = clock.next_open
            except Exception as e:
                print(f"❌ Eroare clock Alpaca: {e}")
                time.sleep(SCAN_INTERVAL_SEC)
                continue

            print(f"⏰ {acum.strftime('%H:%M:%S')} | Ciclu #{ciclu} | "
                  f"Trades: {trades_azi}/{MAX_TRADES_PER_DAY} | Pozitii: {len(pozitii)}/{MAX_POZITII}")

            # ─── BURSA INCHISA ───
            if not bursa_deschisa:
                if not raport_facut:
                    genereaza_raport_csv(memorie, zi)
                    salveaza_memorie(memorie)
                    raport_facut = True
                if ciclu % CACHE_GRAFICE_CICLURI_INCHIS == 0:
                    actualizeaza_cache_grafice(pozitii)
                try:
                    print(f"❌ Bursa inchisa. Se deschide: {next_open}")
                except Exception:
                    print("❌ Bursa inchisa.")
                time.sleep(300)
                continue

            # ─── BURSA DESCHISA ───
            # Minute pana la inchidere
            try:
                min_pana_inchidere = (next_close - acum.astimezone(next_close.tzinfo)).total_seconds() / 60
            except Exception:
                min_pana_inchidere = 999

            # Inchidere fortata cu 15 min inainte
            if min_pana_inchidere <= INCHIDERE_MIN_INAINTE and not inchidere_facuta and pozitii:
                print(f"🔔 END OF DAY — inchid toate pozitiile ({len(pozitii)})")
                for simbol in list(pozitii.keys()):
                    poz = pozitii[simbol]
                    try:
                        df5 = get_date(simbol, "5m", "1d")
                        pret = df5["Close"].iloc[-1] if df5 is not None else poz["pret_intrare"]
                    except Exception:
                        pret = poz["pret_intrare"]
                    if plaseaza_ordin(simbol, poz["cantitate"], "sell"):
                        profit = (pret - poz["pret_intrare"]) * poz["cantitate"]
                        log_tranzactie(memorie, simbol, "close_long", pret,
                                       poz["cantitate"], profit, "END OF DAY")
                        emoji = "🟢" if profit >= 0 else "🔴"
                        print(f"  {emoji} {simbol} inchis EOD: ${profit:.2f}")
                        del pozitii[simbol]
                salveaza_pozitii(pozitii)
                salveaza_memorie(memorie)
                genereaza_raport_csv(memorie, zi)
                raport_facut = True
                inchidere_facuta = True

            # ─── VERIFICARE IESIRI (pentru pozitiile deschise) ───
            for simbol in list(pozitii.keys()):
                poz = pozitii[simbol]
                try:
                    df5 = get_date(simbol, "5m", "1d")
                    if df5 is None or len(df5) < 21:
                        continue
                    inchideri = df5["Close"].tolist()
                    pret = inchideri[-1]
                    ema9 = calculeaza_ema(inchideri, 9)
                    ema21 = calculeaza_ema(inchideri, 21)
                    rsi_5m = calculeaza_rsi(inchideri, 14)
                    iesire, motiv = verifica_iesire(simbol, poz, pret, ema9, ema21, rsi_5m)
                    if iesire:
                        if plaseaza_ordin(simbol, poz["cantitate"], "sell"):
                            profit = (pret - poz["pret_intrare"]) * poz["cantitate"]
                            log_tranzactie(memorie, simbol, "close_long", pret,
                                           poz["cantitate"], profit, motiv)
                            emoji = "🟢" if profit >= 0 else "🔴"
                            print(f"  {emoji} IESIRE {simbol}: {motiv} | ${profit:.2f}")
                            del pozitii[simbol]
                            salveaza_pozitii(pozitii)
                            salveaza_memorie(memorie)
                    else:
                        salveaza_pozitii(pozitii)  # pentru pret_max/trailing actualizat
                except Exception as e:
                    print(f"  ❌ Eroare iesire {simbol}: {e}")

            # ─── CAUTARE INTRARI ───
            stop_intrari = min_pana_inchidere <= STOP_INTRARI_ORE_INAINTE * 60
            poate_intra = (len(pozitii) < MAX_POZITII
                           and trades_azi < MAX_TRADES_PER_DAY
                           and not stop_intrari)

            if poate_intra:
                for simbol in ACTIUNI:
                    if len(pozitii) >= MAX_POZITII or trades_azi >= MAX_TRADES_PER_DAY:
                        break
                    if simbol in pozitii:
                        continue
                    if in_cooldown(memorie, simbol):
                        print(f"  ⏳ {simbol}: cooldown activ")
                        continue
                    if are_earnings_curand(simbol):
                        print(f"  📅 {simbol}: BLOCAT — earnings curand")
                        continue
                    try:
                        semnal, motiv, info = analizeaza_semnal(simbol)
                        if semnal:
                            df5 = get_date(simbol, "5m", "1d")
                            pret = df5["Close"].iloc[-1]
                            atr = calculeaza_atr(df5, 14)
                            cantitate = calculeaza_cantitate(pret, atr)
                            print(f"  ⭐ SEMNAL {simbol}: {motiv}")
                            if plaseaza_ordin(simbol, cantitate, "buy"):
                                pozitii[simbol] = {
                                    "pret_intrare": pret, "cantitate": cantitate,
                                    "pret_max": pret, "trailing_activ": False
                                }
                                log_tranzactie(memorie, simbol, "open_long", pret,
                                               cantitate, None, motiv)
                                trades_azi += 1
                                print(f"  ✅ INTRARE {simbol}: {cantitate} @ ${pret:.2f}")
                                salveaza_pozitii(pozitii)
                                salveaza_memorie(memorie)
                        else:
                            print(f"  ⏳ {simbol}: {motiv}")
                    except Exception as e:
                        print(f"  ❌ Eroare analiza {simbol}: {e}")

            # ─── CACHE GRAFICE ───
            if ciclu % CACHE_GRAFICE_CICLURI == 0:
                actualizeaza_cache_grafice(pozitii)

            salveaza_memorie(memorie)
            time.sleep(SCAN_INTERVAL_SEC)

        except Exception as e:
            print(f"❌ EROARE in bucla principala: {e}")
            traceback.print_exc()
            time.sleep(SCAN_INTERVAL_SEC)


if __name__ == "__main__":
    # In terminal vrem output pe ecran; sub systemd (stdout nu e terminal)
    # scriem in agent.log. --log forteaza redirectarea si din terminal.
    if "--log" in sys.argv or not sys.stdout.isatty():
        _redirect_log()
    ruleaza()
