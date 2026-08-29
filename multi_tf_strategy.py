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
MAX_POZITII_CLUSTER = 2             # maxim 2 pozitii simultane din acelasi cluster
RISC_PORTOFOLIU_PCT = 0.01          # 1% risc pe portofoliu
# Coborat de la 1.5% pe 29 aug 2026. Pe cele 10 inchideri de pe VM-ul nou
# (25-28 aug) MAE-ul separa curat rezultatele: niciun castigator n-a trecut
# de -0.82%, in timp ce cele doua stop loss-uri au fost -1.48% si -1.52%.
# O pozitie care merge peste ~0.9% impotriva nu s-a mai intors. Replay pe
# acelasi esantion: stop 0.9% -> +3.64 USD (3 oprite) vs stop 1.5% -> -25.13.
# Marimea pozitiei nu creste: notionalele reale (~2300 USD) sunt lipite de
# MAX_TRADE_SIZE_USD, deci in calculeaza_cantitate leaga plafonul de marime,
# nu cel de risc.
STOP_LOSS_MIN_PCT = 0.009           # stop loss minim 0.9%
STOP_LOSS_PCT = 0.015               # stop loss fix -1.5%
TAKE_PROFIT_PCT = 0.04              # +4%
TRAILING_ACTIVARE_PCT = 0.015       # trailing se activeaza la +1.5%
TRAILING_DISTANTA_PCT = 0.01        # iesire daca scade 1% de la max
RSI_5M_EXIT = 78                    # iesire daca RSI(5m) > 78
EMA_CROSS_MIN_PROFIT_PCT = 0.003    # EMA cross iese doar peste +0.3%
CORP_MIN_DIN_ATR = 0.3              # corpul lumanarii 5m, minim 30% din ATR
COOLDOWN_ORE = 4                    # cooldown 4h dupa o pierdere
COOLDOWN_REINTRARE_MIN = 45         # cooldown 45 min dupa o iesire pe plus
EARNINGS_BLOCARE_ZILE = 1           # blocheaza daca earnings in <= 1 zi
STOP_INTRARI_ORE_INAINTE = 2        # fara intrari noi cu 2h inainte de inchidere
INCHIDERE_MIN_INAINTE = 15          # inchide tot cu 15 min inainte de inchidere
CACHE_GRAFICE_CICLURI = 5           # scrie cache grafice la fiecare 5 cicluri (bursa deschisa)
CACHE_GRAFICE_CICLURI_INCHIS = 10   # la fiecare 10 cand bursa e inchisa
ORDIN_TIMEOUT_SEC = 20              # cat asteptam executia unui ordin market
ORDIN_POLL_SEC = 1                  # interval de interogare a starii ordinului

# Grupuri de simboluri care se misca impreuna. Filtrul 1D lasa sa treaca
# aproape numai semiconductoare, asa ca cele MAX_POZITII sloturi ajungeau sa fie
# un singur pariu pe sector: pe 28 iul - 14 aug, cele 29 de tranzactii semi au dat
# -170.85 USD, iar cele 17 din afara clusterului +27.20. Aceleasi nume tranzactionate
# izolat (fara alta semi deschisa) au dat +44.03 pe 5 tranzactii.
# Simbolurile care nu apar aici nu sunt restrictionate — dar oricum nu pot avea
# decat o pozitie fiecare, deci nu se poate acumula risc redundant prin ele.
CLUSTERE = {
    "semi": {"NVDA", "AMD", "AVGO", "INTC", "MU", "AMAT", "TXN", "QCOM"},
    "megacap": {"AAPL", "MSFT", "GOOGL", "AMZN", "META", "NFLX", "TSLA"},
    "software": {"CRM", "ADBE", "ORCL", "PYPL"},
}

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


_FMT_RFC3339 = "%Y-%m-%dT%H:%M:%SZ"
_DECALAJ_FALLBACK_MIN = 16   # folosit doar daca planul refuza barele recente


def get_date(simbol, interval, period):
    """Descarca OHLCV de la Alpaca IEX (nu yfinance — evita rate-limit).
    Cere barele pana in momentul curent; daca abonamentul le refuza,
    reincearca o singura data cu decalaj."""
    tf = _TF_MAP.get(interval, "1Day")
    zile = _PERIOD_ZILE.get(period, 30)
    acum = datetime.now(timezone.utc)
    start = (acum - timedelta(days=zile)).strftime(_FMT_RFC3339)
    try:
        bars = api.get_bars(simbol, tf, start=start,
                            end=acum.strftime(_FMT_RFC3339), feed="iex").df
    except Exception:
        try:
            end = (acum - timedelta(minutes=_DECALAJ_FALLBACK_MIN)).strftime(_FMT_RFC3339)
            bars = api.get_bars(simbol, tf, start=start, end=end, feed="iex").df
        except Exception as e:
            print(f"  ⚠️ get_date {simbol} {interval}: {e}")
            return None
    if bars is None or bars.empty:
        return None
    df = bars.rename(columns={
        "open": "Open", "high": "High", "low": "Low",
        "close": "Close", "volume": "Volume"
    })
    return df[["Open", "High", "Low", "Close", "Volume"]]


def get_pret_curent(simbol):
    """Ultimul pret tranzactionat, in timp real. Deciziile de iesire
    (SL/TP/trailing) trebuie luate pe pretul de acum, nu pe inchiderea
    ultimei bare, care poate fi veche de cateva minute."""
    try:
        return float(api.get_latest_trade(simbol).p)
    except Exception as e:
        print(f"  ⚠️ pret curent {simbol}: {e}")
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
    """Candle verde, EMA9 > EMA21, 45 < RSI < 70, corp > ATR*CORP_MIN_DIN_ATR."""
    df = get_date(simbol, "5m", "1d")
    if df is None or len(df) < 20:
        return False, {"motiv": "date 5m insuficiente"}
    inchideri = df["Close"].tolist()
    pret = inchideri[-1]
    open_ = df["Open"].iloc[-1]
    ema9 = calculeaza_ema(inchideri, 9)
    ema21 = calculeaza_ema(inchideri, 21)
    rsi = calculeaza_rsi(inchideri, 14)
    atr = calculeaza_atr(df, 14)
    verde = pret > open_
    # Corpul lumanarii, nu range-ul: o lumanare cu umbre lungi si corp mic
    # inseamna indecizie, nu impuls, si nu trebuie sa treaca filtrul.
    corp = abs(pret - open_)
    corp_solid = corp > atr * CORP_MIN_DIN_ATR
    ok = verde and (ema9 > ema21) and (45 < rsi < 70) and corp_solid
    return ok, {"pret": round(pret, 2), "ema9": round(ema9, 2),
                "ema21": round(ema21, 2), "rsi": round(rsi, 1),
                "atr": round(atr, 2), "corp": round(corp, 2)}


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
    if not os.path.exists(cale):
        return implicit
    try:
        with open(cale, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        # Nu inghitim eroarea in tacere: pe pozitii_active.json, un default gol
        # ar insemna "nicio pozitie deschisa" si agentul n-ar mai inchide nimic.
        print(f"❌ {cale} nu poate fi citit: {e}")
        try:
            avarie = f"{cale}.corupt"
            os.replace(cale, avarie)
            print(f"   fisierul a fost pastrat pentru inspectie: {avarie}")
        except Exception:
            pass
        return implicit


def salveaza_json(cale, date):
    """Scriere atomica: fisier temporar + rename. Daca procesul moare la
    mijloc, vechiul fisier ramane intact in loc sa fie trunchiat."""
    tmp = f"{cale}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(date, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, cale)
    except Exception as e:
        print(f"❌ Eroare salvare {cale}: {e}")
        try:
            os.remove(tmp)
        except Exception:
            pass


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


def metrici_pozitie(poz, pret_iesire):
    """MFE/MAE (procente fata de intrare) si durata (minute) a unei pozitii.

    Fara ele nu se poate calibra nici trailing-ul, nici take profit-ul:
    din istoric se vede doar unde s-a inchis pozitia, nu si cat de sus a
    ajuns inainte sa se intoarca. `TAKE_PROFIT_PCT` a stat la 4% fara nicio
    declansare in 27 de tranzactii tocmai pentru ca nimeni nu masura asta.

    Extremele sunt esantionate o data pe ciclu (60s) pe ultimul pret
    tranzactionat, nu pe maximul/minimul real al barei — sunt deci o limita
    inferioara a excursiei, la fel ca pretul pe care lucreaza trailing-ul.
    """
    pret_intrare = poz.get("pret_intrare")
    if not pret_intrare:
        return {}
    pret_max = max(poz.get("pret_max", pret_intrare), pret_iesire)
    pret_min = min(poz.get("pret_min", pret_intrare), pret_iesire)
    metrici = {
        "mfe_pct": round((pret_max - pret_intrare) / pret_intrare * 100, 2),
        "mae_pct": round((pret_min - pret_intrare) / pret_intrare * 100, 2),
    }
    # Pozitiile adoptate la reconciliere nu au ora de intrare: mai bine lipsa
    # decat o durata inventata de la repornirea agentului.
    intrare = poz.get("ora_intrare")
    if intrare:
        try:
            durata = acum_ny() - datetime.fromisoformat(intrare)
            metrici["durata_min"] = int(durata.total_seconds() // 60)
        except (TypeError, ValueError):
            pass
    return metrici


def log_tranzactie(memorie, simbol, tip, pret, cantitate, profit=None, motiv=None,
                   poz=None):
    acum = acum_ny()
    inregistrare = {
        "simbol": simbol, "tip": tip, "pret": round(pret, 2),
        "cantitate": cantitate, "profit": round(profit, 2) if profit is not None else None,
        "motiv": motiv, "ora": acum.strftime("%H:%M:%S"),
        "data": acum.strftime("%Y-%m-%d")
    }
    if tip == "close_long" and poz:
        inregistrare.update(metrici_pozitie(poz, pret))
    memorie["tranzactii"].append(inregistrare)
    if tip == "close_long" and profit is not None:
        if profit < 0:
            seteaza_cooldown(memorie, simbol, timedelta(hours=COOLDOWN_ORE))
            memorie["stats"]["losses"] += 1
        else:
            # Si dupa o iesire pe plus blocam reintrarea, ca acelasi semnal
            # sa nu ne bage inapoi peste cateva minute pe acelasi simbol.
            seteaza_cooldown(memorie, simbol, timedelta(minutes=COOLDOWN_REINTRARE_MIN))
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
def cluster_pentru(simbol):
    """Clusterul din care face parte simbolul, sau None daca e neincadrat."""
    for nume, membri in CLUSTERE.items():
        if simbol in membri:
            return nume
    return None


def cluster_plin(pozitii, simbol):
    """True daca clusterul simbolului are deja MAX_POZITII_CLUSTER pozitii deschise.

    Functie pura. Un simbol neincadrat nu e niciodata blocat: nu poate avea
    decat o pozitie, deci nu concentreaza risc.
    """
    grup = cluster_pentru(simbol)
    if grup is None:
        return False
    deschise = sum(1 for s in pozitii if cluster_pentru(s) == grup)
    return deschise >= MAX_POZITII_CLUSTER


def seteaza_cooldown(memorie, simbol, durata):
    """Blocheaza reintrarea pe simbol pana la un moment dat (ora NY)."""
    memorie["cooldown"][simbol] = (acum_ny() + durata).isoformat()


def in_cooldown(memorie, simbol):
    """True daca simbolul e blocat la reintrare (vezi seteaza_cooldown).

    Valoarea din memorie e momentul de EXPIRARE, nu cel al iesirii — asa nu
    trebuie sa stim ce durata s-a aplicat cand s-a inchis pozitia.
    """
    ts = memorie["cooldown"].get(simbol)
    if not ts:
        return False
    try:
        expira = datetime.fromisoformat(ts)
    except Exception:
        del memorie["cooldown"][simbol]
        return False
    if expira.tzinfo is None:
        # Intrari scrise inainte de trecerea la ore aware — le citim ca ora NY
        expira = expira.replace(tzinfo=NY_TZ)
    if acum_ny() >= expira:
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
    """Returneaza (cantitate, stop_loss_pct).

    Stopul returnat e cel folosit la dimensionare si trebuie sa fie exact
    stopul executat mai tarziu. Daca dimensionam pentru un stop de 3% dar
    iesim la 1.5% fix, riscul real per tranzactie e jumatate din cel
    planificat, iar pe simbolurile volatile stopul strans e atins de
    zgomotul normal, nu de invalidarea setup-ului.
    """
    portofoliu = get_portofoliu()
    risc_max = portofoliu * RISC_PORTOFOLIU_PCT
    stop_loss_pct = max(STOP_LOSS_MIN_PCT, atr / pret * 1.5)
    cantitate_risc = int(risc_max / (pret * stop_loss_pct))
    cantitate_size = int(MAX_TRADE_SIZE_USD / pret)
    return max(1, min(cantitate_risc, cantitate_size)), stop_loss_pct


# ═════════════════════════════════════════════════════════════
# ORDINE
# ═════════════════════════════════════════════════════════════
def plaseaza_ordin(simbol, cantitate, side):
    """Trimite ordinul si asteapta executia efectiva.

    Returneaza (pret_executie, cantitate_executata) sau None daca ordinul
    nu s-a executat. Un ordin acceptat de API nu inseamna un ordin executat:
    fara verificarea asta, pretul de intrare inregistrat ar fi o estimare,
    iar o pozitie ar putea exista local fara sa existe la broker.
    """
    try:
        ordin = api.submit_order(symbol=simbol, qty=cantitate, side=side,
                                 type="market", time_in_force="day")
    except Exception as e:
        print(f"❌ Ordin {side} {simbol} respins la trimitere: {e}")
        return None

    limita = time.monotonic() + ORDIN_TIMEOUT_SEC
    while time.monotonic() < limita:
        try:
            o = api.get_order(ordin.id)
        except Exception as e:
            print(f"  ⚠️ nu pot citi starea ordinului {simbol}: {e}")
            time.sleep(ORDIN_POLL_SEC)
            continue
        if o.status == "filled":
            return float(o.filled_avg_price), int(float(o.filled_qty))
        if o.status in ("canceled", "expired", "rejected", "suspended"):
            print(f"❌ Ordin {side} {simbol}: {o.status}")
            return None
        time.sleep(ORDIN_POLL_SEC)

    # Timeout — anulam ca sa nu ramana un ordin in asteptare peste cicluri,
    # apoi verificam daca s-a executat partial inainte de anulare.
    try:
        api.cancel_order(ordin.id)
    except Exception:
        pass
    try:
        o = api.get_order(ordin.id)
        qty = int(float(o.filled_qty or 0))
        if qty > 0:
            pret = float(o.filled_avg_price)
            print(f"  ⚠️ {simbol}: executie partiala {qty}/{cantitate} @ ${pret:.2f}")
            return pret, qty
    except Exception:
        pass
    print(f"❌ Ordin {side} {simbol}: neexecutat in {ORDIN_TIMEOUT_SEC}s, anulat")
    return None


# ═════════════════════════════════════════════════════════════
# STOP LA BROKER
#
# Stopul evaluat doar in bucla proprie protejeaza pozitia doar cat timp
# procesul ruleaza si Alpaca raspunde. Un stop lasat la broker ramane activ
# si daca agentul cade sau reteaua pica, si se executa la pretul lui, nu la
# cat a apucat sa alunece pretul intre doua cicluri.
# ═════════════════════════════════════════════════════════════
def plaseaza_stop(simbol, cantitate, pret_stop):
    """Trimite un ordin stop de vanzare la broker. Returneaza id-ul sau None."""
    try:
        o = api.submit_order(symbol=simbol, qty=cantitate, side="sell",
                             type="stop", time_in_force="day",
                             stop_price=round(pret_stop, 2))
        return o.id
    except Exception as e:
        print(f"  ⚠️ {simbol}: stop la broker neplasat: {e}")
        return None


def anuleaza_stop(simbol, poz):
    """Anuleaza stopul inainte de o vanzare initiata de agent.

    Actiunile sunt blocate de ordinul stop deschis, deci fara anulare
    ordinul de vanzare al agentului e respins pentru cantitate insuficienta.
    Returneaza ("anulat" | "executat" | "absent" | "necunoscut", executie),
    unde executie e (pret, cantitate) doar daca stopul s-a executat chiar
    in timpul anularii.
    """
    oid = poz.pop("stop_order_id", None)
    if not oid:
        return "absent", None
    try:
        api.cancel_order(oid)
    except Exception:
        pass  # poate fi deja intr-o stare terminala; verificam mai jos
    limita = time.monotonic() + ORDIN_TIMEOUT_SEC
    while time.monotonic() < limita:
        try:
            o = api.get_order(oid)
        except Exception:
            time.sleep(ORDIN_POLL_SEC)
            continue
        if o.status == "filled":
            return "executat", (float(o.filled_avg_price), int(float(o.filled_qty)))
        if o.status in ("canceled", "expired", "rejected", "suspended", "done_for_day"):
            return "anulat", None
        time.sleep(ORDIN_POLL_SEC)
    print(f"  ⚠️ {simbol}: stopul nu s-a anulat in {ORDIN_TIMEOUT_SEC}s")
    return "necunoscut", None


def sincronizeaza_stop(simbol, poz, memorie):
    """Aliniaza stopul de la broker cu starea locala, la inceputul fiecarui ciclu.

    Trei situatii: stopul s-a executat intre cicluri (pozitia e deja inchisa
    la broker si trebuie inregistrata local, altfel ramane o pozitie fantoma
    pe care agentul incearca s-o vanda inca o data), stopul lipseste ori a
    expirat (se replaseaza — asa se acopera si pozitiile adoptate la
    reconciliere si stopurile `day` expirate), sau e activ si nu se face nimic.

    Returneaza "iesit" daca stopul a inchis pozitia.
    """
    oid = poz.get("stop_order_id")
    if oid:
        try:
            o = api.get_order(oid)
        except Exception as e:
            print(f"  ⚠️ {simbol}: nu pot citi starea stopului: {e}")
            return None
        if o.status == "filled":
            pret_exec = float(o.filled_avg_price)
            qty_exec = int(float(o.filled_qty))
            profit = (pret_exec - poz["pret_intrare"]) * qty_exec
            log_tranzactie(memorie, simbol, "close_long", pret_exec, qty_exec,
                           profit, "STOP LOSS (broker)", poz)
            print(f"  🔴 IESIRE {simbol}: STOP LOSS la broker @ ${pret_exec:.2f} "
                  f"| ${profit:.2f}")
            poz.pop("stop_order_id", None)
            return "iesit"
        if o.status in ("new", "accepted", "held", "pending_new", "partially_filled"):
            # Cantitatea poate diverge dupa o vanzare partiala sau dupa
            # reconciliere; un stop pe alta cantitate lasa actiuni neacoperite.
            if int(float(o.qty)) == poz["cantitate"]:
                return None
            print(f"  🔧 {simbol}: stop pe {o.qty} vs {poz['cantitate']} in pozitie "
                  f"— replasat")
            stare, executie = anuleaza_stop(simbol, poz)
            if stare == "executat":
                pret_exec, qty_exec = executie
                profit = (pret_exec - poz["pret_intrare"]) * qty_exec
                log_tranzactie(memorie, simbol, "close_long", pret_exec, qty_exec,
                               profit, "STOP LOSS (broker)", poz)
                print(f"  🔴 IESIRE {simbol}: STOP LOSS la broker @ ${pret_exec:.2f} "
                      f"| ${profit:.2f}")
                return "iesit"
            if stare == "necunoscut":
                return None
        else:
            poz.pop("stop_order_id", None)  # terminal fara executie — se replaseaza

    pret_stop = poz["pret_intrare"] * (1 - poz.get("stop_loss_pct", STOP_LOSS_PCT))
    nou = plaseaza_stop(simbol, poz["cantitate"], pret_stop)
    if nou:
        poz["stop_order_id"] = nou
        print(f"  🛡️ {simbol}: stop la broker @ ${pret_stop:.2f}")
    return None


# ═════════════════════════════════════════════════════════════
# IESIRE — 5 conditii, prima adevarata castiga
# ═════════════════════════════════════════════════════════════
def verifica_iesire(simbol, poz, pret_curent, ema9, ema21, rsi_5m):
    """Returneaza (trebuie_iesire, motiv). Actualizeaza pret_max in poz."""
    pret_intrare = poz["pret_intrare"]
    pl_pct = (pret_curent - pret_intrare) / pret_intrare

    # Actualizeaza extremele parcurse (pentru MFE/MAE la inchidere)
    if pret_curent > poz.get("pret_max", pret_intrare):
        poz["pret_max"] = pret_curent
    if pret_curent < poz.get("pret_min", pret_intrare):
        poz["pret_min"] = pret_curent

    # 1. Trailing stop (activ la +1.5%, iesire daca scade 1% de la max)
    if pl_pct >= TRAILING_ACTIVARE_PCT:
        poz["trailing_activ"] = True
    if poz.get("trailing_activ"):
        scadere_de_la_max = (poz["pret_max"] - pret_curent) / poz["pret_max"]
        if scadere_de_la_max >= TRAILING_DISTANTA_PCT:
            return True, f"TRAILING STOP (-{scadere_de_la_max*100:.1f}% de la max)"

    # 2. Stop loss — acelasi procent folosit la dimensionarea pozitiei.
    # Pozitiile mai vechi sau cele adoptate la reconciliere nu au campul,
    # deci cad pe valoarea fixa.
    stop_loss_pct = poz.get("stop_loss_pct", STOP_LOSS_PCT)
    if pl_pct <= -stop_loss_pct:
        return True, f"STOP LOSS ({pl_pct*100:.1f}%)"

    # 3. Take profit +4%
    if pl_pct >= TAKE_PROFIT_PCT:
        return True, f"TAKE PROFIT (+{pl_pct*100:.1f}%)"

    # 4. EMA9 < EMA21, dar numai peste un profit care acopera costul iesirii.
    # Conditia veche (`pl_pct > 0`) se evalua pe ultimul pret tranzactionat
    # si executa un ordin market: la +0.0% ordinul se umplea pe partea
    # gresita a spread-ului si trade-ul se inchidea in pierdere (GOOGL,
    # 5 august: logat "+0.0%", realizat -0.35 USD). Sub prag lasam stopul
    # sa decida — nu inchidem in zgomot.
    if ema9 < ema21 and pl_pct > EMA_CROSS_MIN_PROFIT_PCT:
        return True, f"EMA CROSS (EMA9<EMA21, +{pl_pct*100:.1f}%)"

    # 5. RSI(5m) > 78, dar numai dupa ce pozitia a ajuns in zona de trailing.
    # Sub acest prag lasam trade-ul sa respire: altfel RSI-ul inchidea la
    # +0.2% si trailing-ul (activ de la +1.5%) nu apuca sa preia niciodata
    # gestiunea. Pe minusul din zona asta raspunde stop loss-ul, iar pe
    # momentum pierdut raspunde EMA cross.
    if rsi_5m > RSI_5M_EXIT and pl_pct >= TRAILING_ACTIVARE_PCT:
        return True, f"RSI OVERBOUGHT ({rsi_5m:.1f}, +{pl_pct*100:.1f}%)"

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
                    "pret_iesire", "profit_usd", "profit_pct", "mfe_pct", "mae_pct",
                    "durata_min", "motiv_exit", "rezultat"])
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
            # Tranzactiile de dinaintea instrumentarii nu au excursii — coloane
            # goale, nu zerouri: un 0 s-ar amesteca in orice medie ulterioara.
            w.writerow([t["data"] + "T" + t["ora"], t["simbol"], t["cantitate"],
                        pret_intrare, t["pret"], round(profit, 2),
                        round(profit_pct, 2), t.get("mfe_pct", ""),
                        t.get("mae_pct", ""), t.get("durata_min", ""),
                        t.get("motiv", ""), rezultat])
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
    print(f"🛑 SL=min {STOP_LOSS_MIN_PCT*100:.2f}% (1.5×ATR, la broker) | "
          f"TP={TAKE_PROFIT_PCT*100:.2f}% | "
          f"Trailing={TRAILING_DISTANTA_PCT*100:.1f}% (activ la {TRAILING_ACTIVARE_PCT*100:.1f}%)")
    print(f"🎯 15m: pullback<2.0% | RSI 25-60")
    print(f"📅 Blocare earnings: {EARNINGS_BLOCARE_ZILE} zi | Manual: {len(EARNINGS_MANUAL)} simboluri")
    print(f"🧩 Max {MAX_POZITII} pozitii, din care max {MAX_POZITII_CLUSTER} pe cluster "
          f"({', '.join(CLUSTERE)})")
    print(f"🔔 Inchidere: {INCHIDERE_MIN_INAINTE} min inainte | Stop intrari: {STOP_INTRARI_ORE_INAINTE}h inainte")
    print(f"⏱️  Scanare la fiecare {SCAN_INTERVAL_SEC}s | Cache grafice la {CACHE_GRAFICE_CICLURI} cicluri")
    print(f"📋 {len(ACTIUNI)} actiuni: {', '.join(ACTIUNI[:10])}{'...' if len(ACTIUNI) > 10 else ''}")
    print("-" * 60)


def reconciliaza(pozitii_locale, pozitii_broker):
    """Aliniaza starea locala la cea a brokerului. Functie pura.

    Brokerul e sursa de adevar: acolo sunt banii. Un fisier de stare
    desincronizat inseamna fie pozitii reale pe care agentul nu le mai
    urmareste (deci nu le mai inchide), fie pozitii fantoma care blocheaza
    sloturi din MAX_POZITII.

    `pozitii_broker` e o lista de (simbol, cantitate, pret_mediu_intrare).
    Returneaza (pozitii_corectate, mesaje).
    """
    corectate = {}
    mesaje = []
    broker = {s: (q, p) for s, q, p in pozitii_broker}

    for simbol, (qty, pret_mediu) in broker.items():
        local = pozitii_locale.get(simbol)
        if local is None:
            corectate[simbol] = {
                "pret_intrare": pret_mediu, "cantitate": qty,
                "pret_max": pret_mediu, "pret_min": pret_mediu,
                "trailing_activ": False,
            }
            mesaje.append(f"➕ {simbol}: pozitie la broker, absenta local — adoptata "
                          f"({qty} @ ${pret_mediu:.2f})")
        elif local.get("cantitate") != qty:
            local = dict(local)
            mesaje.append(f"🔧 {simbol}: cantitate {local.get('cantitate')} local "
                          f"vs {qty} la broker — aliniata la broker")
            local["cantitate"] = qty
            corectate[simbol] = local
        else:
            corectate[simbol] = local

    for simbol in pozitii_locale:
        if simbol not in broker:
            mesaje.append(f"➖ {simbol}: pozitie locala inexistenta la broker — eliminata")

    return corectate, mesaje


def reconciliaza_la_pornire(pozitii):
    """Citeste pozitiile reale de la Alpaca si aliniaza starea locala.
    Daca API-ul nu raspunde, lasa starea neatinsa — mai bine desincronizat
    decat sa stergem pozitii reale pe baza unui raspuns lipsa."""
    try:
        brute = api.list_positions()
    except Exception as e:
        print(f"⚠️ Reconciliere esuata (Alpaca nu raspunde): {e}")
        print("   Se continua cu starea din pozitii_active.json.")
        return pozitii

    pozitii_broker = [(p.symbol, int(float(p.qty)), float(p.avg_entry_price))
                      for p in brute]
    corectate, mesaje = reconciliaza(pozitii, pozitii_broker)

    # Stopurile ramase de la instanta precedenta au id-uri pe care nu le mai
    # putem lega de starea locala; lasate acolo s-ar dubla cu cele noi.
    # Se sterg, iar sincronizeaza_stop le replaseaza la primul ciclu.
    try:
        for o in api.list_orders(status="open"):
            if o.symbol in corectate and o.type == "stop" and o.side == "sell":
                api.cancel_order(o.id)
                print(f"  🧹 {o.symbol}: stop vechi anulat, se replaseaza")
    except Exception as e:
        print(f"  ⚠️ Nu pot curata ordinele stop vechi: {e}")
    for poz in corectate.values():
        poz.pop("stop_order_id", None)

    if mesaje:
        print("🔄 RECONCILIERE cu Alpaca:")
        for msg in mesaje:
            print(f"  {msg}")
        salveaza_pozitii(corectate)
    else:
        print(f"✅ Reconciliere: {len(corectate)} pozitii, starea locala e corecta")
    return corectate


def ruleaza():
    afiseaza_config()
    pozitii = incarca_pozitii()
    pozitii = reconciliaza_la_pornire(pozitii)
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
                    stare_stop, executie_stop = anuleaza_stop(simbol, poz)
                    if stare_stop == "executat":
                        pret_exec, qty_exec = executie_stop
                        profit = (pret_exec - poz["pret_intrare"]) * qty_exec
                        log_tranzactie(memorie, simbol, "close_long", pret_exec,
                                       qty_exec, profit, "STOP LOSS (broker)", poz)
                        print(f"  🔴 {simbol}: inchis de stopul de la broker "
                              f"@ ${pret_exec:.2f} | ${profit:.2f}")
                        del pozitii[simbol]
                        continue
                    if stare_stop == "necunoscut":
                        print(f"  ⚠️ {simbol}: stop in stare incerta — nu vand EOD")
                        continue
                    executie = plaseaza_ordin(simbol, poz["cantitate"], "sell")
                    if executie:
                        pret_exec, qty_exec = executie
                        profit = (pret_exec - poz["pret_intrare"]) * qty_exec
                        log_tranzactie(memorie, simbol, "close_long", pret_exec,
                                       qty_exec, profit, "END OF DAY", poz)
                        emoji = "🟢" if profit >= 0 else "🔴"
                        print(f"  {emoji} {simbol} inchis EOD: ${profit:.2f}")
                        if qty_exec < poz["cantitate"]:
                            poz["cantitate"] -= qty_exec
                            print(f"  ⚠️ {simbol}: raman {poz['cantitate']} actiuni nevandute")
                        else:
                            del pozitii[simbol]
                    else:
                        print(f"  ⚠️ {simbol}: NU s-a putut inchide EOD — pozitia ramane")
                salveaza_pozitii(pozitii)
                salveaza_memorie(memorie)
                genereaza_raport_csv(memorie, zi)
                raport_facut = True
                inchidere_facuta = True

            # ─── VERIFICARE IESIRI (pentru pozitiile deschise) ───
            for simbol in list(pozitii.keys()):
                poz = pozitii[simbol]
                try:
                    # Stopul de la broker se poate executa intre cicluri, si
                    # trebuie replasat daca lipseste — inainte de orice altceva.
                    if sincronizeaza_stop(simbol, poz, memorie) == "iesit":
                        del pozitii[simbol]
                        salveaza_pozitii(pozitii)
                        salveaza_memorie(memorie)
                        continue

                    df5 = get_date(simbol, "5m", "1d")
                    if df5 is None or len(df5) < 21:
                        continue
                    inchideri = df5["Close"].tolist()
                    # Indicatorii vin din bare, dar SL/TP/trailing se evalueaza
                    # pe pretul curent — o bara de 5m poate fi deja invechita.
                    pret = get_pret_curent(simbol) or inchideri[-1]
                    ema9 = calculeaza_ema(inchideri, 9)
                    ema21 = calculeaza_ema(inchideri, 21)
                    rsi_5m = calculeaza_rsi(inchideri, 14)
                    iesire, motiv = verifica_iesire(simbol, poz, pret, ema9, ema21, rsi_5m)
                    if iesire:
                        stare_stop, executie_stop = anuleaza_stop(simbol, poz)
                        if stare_stop == "executat":
                            pret_exec, qty_exec = executie_stop
                            profit = (pret_exec - poz["pret_intrare"]) * qty_exec
                            log_tranzactie(memorie, simbol, "close_long", pret_exec,
                                           qty_exec, profit, "STOP LOSS (broker)", poz)
                            print(f"  🔴 IESIRE {simbol}: stopul de la broker a "
                                  f"prins-o primul @ ${pret_exec:.2f} | ${profit:.2f}")
                            del pozitii[simbol]
                            salveaza_pozitii(pozitii)
                            salveaza_memorie(memorie)
                            continue
                        if stare_stop == "necunoscut":
                            print(f"  ⚠️ {simbol}: stop in stare incerta — aman iesirea")
                            continue
                        executie = plaseaza_ordin(simbol, poz["cantitate"], "sell")
                        if executie:
                            pret_exec, qty_exec = executie
                            profit = (pret_exec - poz["pret_intrare"]) * qty_exec
                            log_tranzactie(memorie, simbol, "close_long", pret_exec,
                                           qty_exec, profit, motiv, poz)
                            emoji = "🟢" if profit >= 0 else "🔴"
                            print(f"  {emoji} IESIRE {simbol}: {motiv} | ${profit:.2f}")
                            if qty_exec < poz["cantitate"]:
                                poz["cantitate"] -= qty_exec
                                print(f"  ⚠️ {simbol}: raman {poz['cantitate']} actiuni")
                            else:
                                del pozitii[simbol]
                            salveaza_pozitii(pozitii)
                            salveaza_memorie(memorie)
                        else:
                            print(f"  ⚠️ {simbol}: iesire esuata, pozitia ramane deschisa")
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
                    if cluster_plin(pozitii, simbol):
                        print(f"  ⏳ {simbol}: cluster {cluster_pentru(simbol)} plin "
                              f"({MAX_POZITII_CLUSTER}/{MAX_POZITII_CLUSTER})")
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
                            pret = get_pret_curent(simbol) or df5["Close"].iloc[-1]
                            atr = calculeaza_atr(df5, 14)
                            cantitate, stop_loss_pct = calculeaza_cantitate(pret, atr)
                            print(f"  ⭐ SEMNAL {simbol}: {motiv}")
                            executie = plaseaza_ordin(simbol, cantitate, "buy")
                            if executie:
                                # Pretul si cantitatea vin din executia reala,
                                # nu din estimarea de dinaintea ordinului.
                                pret_exec, qty_exec = executie
                                pozitii[simbol] = {
                                    "pret_intrare": pret_exec, "cantitate": qty_exec,
                                    "pret_max": pret_exec, "pret_min": pret_exec,
                                    "ora_intrare": acum_ny().isoformat(),
                                    "trailing_activ": False,
                                    "stop_loss_pct": stop_loss_pct
                                }
                                log_tranzactie(memorie, simbol, "open_long", pret_exec,
                                               qty_exec, None, motiv)
                                trades_azi += 1
                                print(f"  ✅ INTRARE {simbol}: {qty_exec} @ ${pret_exec:.2f} "
                                      f"| SL {stop_loss_pct*100:.2f}%")
                                # Stopul se calculeaza pe pretul real de executie,
                                # nu pe estimarea de dinaintea ordinului.
                                pret_stop = pret_exec * (1 - stop_loss_pct)
                                oid = plaseaza_stop(simbol, qty_exec, pret_stop)
                                if oid:
                                    pozitii[simbol]["stop_order_id"] = oid
                                    print(f"  🛡️ {simbol}: stop la broker @ ${pret_stop:.2f}")
                                else:
                                    print(f"  ⚠️ {simbol}: fara stop la broker — "
                                          f"se reincearca la ciclul urmator")
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
