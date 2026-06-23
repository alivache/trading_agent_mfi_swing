import yfinance as yf
import alpaca_trade_api as tradeapi
import os
import sys
import json
import time
import csv
import pandas as pd
from dotenv import load_dotenv
from datetime import datetime, date, timedelta

load_dotenv()

# ═══════════════════════════════════════
# REDIRECT OUTPUT CATRE agent.log
# ═══════════════════════════════════════
_log = open("/home/liviu_anton/trading/agent.log", "a", buffering=1)
sys.stdout = _log
sys.stderr = _log

# ═══════════════════════════════════════
# CONFIGURARE
# ═══════════════════════════════════════
API_KEY = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
BASE_URL = os.getenv("ALPACA_BASE_URL")
MAX_TRADE_SIZE_USD = float(os.getenv("MAX_TRADE_SIZE_USD", 2500))
MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES_PER_DAY", 10))

ACTIUNI_RAW = os.getenv("ACTIUNI", "AAPL,MSFT,NVDA,GOOGL,AMZN,NFLX")
ACTIUNI = [s.strip().upper() for s in ACTIUNI_RAW.split(",")]

EARNINGS_RAW = os.getenv("EARNINGS", "")
EARNINGS_MANUAL = {}
for item in EARNINGS_RAW.split(","):
    if ":" in item:
        sym, d = item.strip().split(":", 1)
        EARNINGS_MANUAL[sym.strip().upper()] = d.strip()

ZILE_BLOCARE_EARNINGS = 1

api = tradeapi.REST(API_KEY, SECRET_KEY, BASE_URL)

INTERVAL_SCANARE = 60
MEMORIE_FILE = "/home/liviu_anton/trading/memorie_multitf.json"
POZITII_FILE = "/home/liviu_anton/trading/pozitii_active.json"
GRAFICE_CACHE_FILE = "/home/liviu_anton/trading/grafice_cache.json"

# ═══════════════════════════════════════
# PARAMETRI OPTIMIZATI (R1 + varianta A din backtest)
# ═══════════════════════════════════════
STOP_LOSS_PCT = 0.015
TAKE_PROFIT_PCT = 0.040
TRAILING_STOP_PCT = 0.010
TRAILING_ACTIV_PCT = 0.015
PULLBACK_MAX_15M = 0.02
RSI_15M_MIN = 25
RSI_15M_MAX = 60

MAX_RISC_PORTOFOLIU = 0.01
MAX_POZITII = 5
COOLDOWN_ORE = 4

MINUTE_INAINTE_CLOSE = 15
ORE_STOP_INTRARI = 2  # Nu mai deschide pozitii noi in ultimele 2 ore

trades_azi = 0
data_curenta = datetime.now().date()
pozitii_deschise = {}
raport_generat_azi = False
inchidere_facuta_azi = False

_earnings_cache = {}
_earnings_cache_data = None


# ═══════════════════════════════════════
# POZITII ACTIVE (persistente la restart)
# ═══════════════════════════════════════
def incarca_pozitii_active():
    if os.path.exists(POZITII_FILE):
        try:
            with open(POZITII_FILE, "r") as f:
                poz = json.load(f)
            if poz:
                print(f"📥 Incarcat {len(poz)} pozitii active la pornire: {', '.join(poz.keys())}")
            return poz
        except Exception as e:
            print(f"  Eroare incarcare pozitii: {e}")
    return {}


def salveaza_pozitii_active():
    try:
        with open(POZITII_FILE, "w") as f:
            json.dump(pozitii_deschise, f, indent=2, default=str)
    except Exception as e:
        print(f"  Eroare salvare pozitii: {e}")


# ═══════════════════════════════════════
# MEMORIE
# ═══════════════════════════════════════
def incarca_memorie():
    if os.path.exists(MEMORIE_FILE):
        with open(MEMORIE_FILE, "r") as f:
            return json.load(f)
    return {
        "tranzactii": [],
        "performanta": {},
        "cooldown": {},
        "stats": {"total_profit": 0, "wins": 0, "losses": 0}
    }


def salveaza_memorie(memorie):
    with open(MEMORIE_FILE, "w") as f:
        json.dump(memorie, f, indent=2, default=str)


def log_tranzactie(memorie, simbol, tip, pret, cantitate, profit=None, motiv=None):
    memorie["tranzactii"].append({
        "simbol": simbol,
        "tip": tip,
        "pret": pret,
        "cantitate": cantitate,
        "profit": profit,
        "motiv": motiv,
        "ora": datetime.now().hour,
        "data": datetime.now().isoformat()
    })

    if profit is not None:
        memorie["stats"]["total_profit"] += profit
        if profit > 0:
            memorie["stats"]["wins"] += 1
        else:
            memorie["stats"]["losses"] += 1
            memorie["cooldown"][simbol] = datetime.now().isoformat()

        if simbol not in memorie["performanta"]:
            memorie["performanta"][simbol] = {"profit": 0, "trades": 0, "wins": 0}
        p = memorie["performanta"][simbol]
        p["profit"] += profit
        p["trades"] += 1
        if profit > 0:
            p["wins"] += 1

    salveaza_memorie(memorie)


def simbol_in_cooldown(simbol, memorie):
    if simbol not in memorie.get("cooldown", {}):
        return False
    data_pierdere = datetime.fromisoformat(memorie["cooldown"][simbol])
    ore_trecute = (datetime.now() - data_pierdere).total_seconds() / 3600
    if ore_trecute < COOLDOWN_ORE:
        return True
    del memorie["cooldown"][simbol]
    salveaza_memorie(memorie)
    return False


# ═══════════════════════════════════════
# VERIFICARE EARNINGS
# ═══════════════════════════════════════
def are_earnings_curand(simbol):
    global _earnings_cache, _earnings_cache_data
    azi = datetime.now().date()

    if _earnings_cache_data != azi:
        _earnings_cache = {}
        _earnings_cache_data = azi

    if simbol in _earnings_cache:
        return _earnings_cache[simbol]

    limita = azi + timedelta(days=ZILE_BLOCARE_EARNINGS)
    rezultat = (False, None, None)

    try:
        ticker = yf.Ticker(simbol)
        cal = ticker.calendar
        data_earnings = None
        if isinstance(cal, dict):
            ed = cal.get("Earnings Date")
            if ed:
                data_earnings = ed[0] if isinstance(ed, list) else ed
        if data_earnings is not None:
            if hasattr(data_earnings, "date"):
                data_earnings = data_earnings.date()
            if azi <= data_earnings <= limita:
                rezultat = (True, str(data_earnings), "yfinance")
    except Exception:
        pass

    if not rezultat[0] and simbol in EARNINGS_MANUAL:
        try:
            data_man = datetime.strptime(EARNINGS_MANUAL[simbol], "%Y-%m-%d").date()
            if azi <= data_man <= limita:
                rezultat = (True, str(data_man), "manual")
        except Exception:
            pass

    _earnings_cache[simbol] = rezultat
    return rezultat


# ═══════════════════════════════════════
# INDICATORI
# ═══════════════════════════════════════
def calculeaza_ema(preturi, perioada):
    return pd.Series(preturi).ewm(span=perioada, adjust=False).mean().iloc[-1]


def calculeaza_rsi(preturi, perioada=14):
    prices = pd.Series(preturi)
    delta = prices.diff()
    castig = delta.where(delta > 0, 0.0)
    pierdere = -delta.where(delta < 0, 0.0)
    avg_c = castig.rolling(window=perioada).mean().iloc[-1]
    avg_p = pierdere.rolling(window=perioada).mean().iloc[-1]
    if avg_p == 0:
        return 100
    rs = avg_c / avg_p
    return 100 - (100 / (1 + rs))


def calculeaza_atr(df, perioada=14):
    high = df["High"]
    low = df["Low"]
    close = df["Close"].shift(1)
    tr = pd.concat([
        high - low,
        (high - close).abs(),
        (low - close).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(perioada).mean().iloc[-1]


def get_date(simbol, interval, period):
    df = yf.download(simbol, period=period, interval=interval, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df


# ═══════════════════════════════════════
# CACHE GRAFICE (pentru dashboard)
# ═══════════════════════════════════════
def salveaza_grafice_cache():
    cache = {"_updated": datetime.now().isoformat(), "actiuni": {}}
    for simbol in ACTIUNI:
        try:
            df = get_date(simbol, interval="5m", period="2d")
            if df.empty:
                continue
            df = df.tail(80)
            close = df["Close"].tolist()
            ema9 = pd.Series(close).ewm(span=9, adjust=False).mean().tolist()
            ema21 = pd.Series(close).ewm(span=21, adjust=False).mean().tolist()

            s = pd.Series(close)
            delta = s.diff()
            castig = delta.where(delta > 0, 0.0)
            pierdere = -delta.where(delta < 0, 0.0)
            avg_c = castig.rolling(14).mean()
            avg_p = pierdere.rolling(14).mean()
            rs = avg_c / avg_p.replace(0, 0.0001)
            rsi_v = (100 - (100 / (1 + rs))).fillna(50).tolist()

            st, change = "?", 0
            df1d = get_date(simbol, interval="1d", period="200d")
            if not df1d.empty and len(df1d) >= 200:
                p1d = df1d["Close"].tolist()
                pc = p1d[-1]
                e50 = pd.Series(p1d).ewm(span=50, adjust=False).mean().iloc[-1]
                e200 = pd.Series(p1d).ewm(span=200, adjust=False).mean().iloc[-1]
                if pc > e50 > e200:
                    st = "🟢 1D bullish"
                elif pc > e50:
                    st = "🟠 partial"
                else:
                    st = "⏳ nu"
                if len(p1d) >= 2:
                    change = (p1d[-1] - p1d[-2]) / p1d[-2] * 100

            cache["actiuni"][simbol] = {
                "dates": df.index.strftime("%Y-%m-%d %H:%M").tolist(),
                "open": df["Open"].tolist(), "high": df["High"].tolist(),
                "low": df["Low"].tolist(), "close": close,
                "ema9": ema9, "ema21": ema21, "rsi": rsi_v,
                "status_1d": st, "change": change
            }
        except Exception as e:
            print(f"  Cache grafic {simbol}: {e}")

    try:
        with open(GRAFICE_CACHE_FILE, "w") as f:
            json.dump(cache, f)
    except Exception as e:
        print(f"  Eroare salvare cache grafice: {e}")


# ═══════════════════════════════════════
# ANALIZA 3 TIMEFRAME
# ═══════════════════════════════════════
def analiza_1d(simbol):
    try:
        df = get_date(simbol, interval="1d", period="200d")
        if df.empty or len(df) < 200:
            return False, {}

        preturi = df["Close"].tolist()
        pret_curent = preturi[-1]
        ema50 = calculeaza_ema(preturi, 50)
        ema200 = calculeaza_ema(preturi, 200)
        rsi = calculeaza_rsi(preturi, 14)

        trend_bullish = pret_curent > ema50 > ema200
        rsi_ok = 40 < rsi < 75

        info = {
            "pret_1d": pret_curent,
            "ema50_1d": ema50,
            "ema200_1d": ema200,
            "rsi_1d": rsi,
            "trend_1d": "bullish" if trend_bullish else "bearish"
        }
        return trend_bullish and rsi_ok, info
    except Exception as e:
        return False, {}


def analiza_15m(simbol):
    try:
        df = get_date(simbol, interval="15m", period="5d")
        if df.empty or len(df) < 50:
            return False, {}

        preturi = df["Close"].tolist()
        pret_curent = preturi[-1]
        ema20 = calculeaza_ema(preturi, 20)
        ema50 = calculeaza_ema(preturi, 50)
        rsi = calculeaza_rsi(preturi, 14)

        trend_bullish = ema20 > ema50
        distanta_ema20 = abs(pret_curent - ema20) / ema20
        pullback = distanta_ema20 < PULLBACK_MAX_15M
        rsi_pullback = RSI_15M_MIN < rsi < RSI_15M_MAX

        info = {
            "pret_15m": pret_curent,
            "ema20_15m": ema20,
            "ema50_15m": ema50,
            "rsi_15m": rsi,
            "distanta_ema": distanta_ema20,
            "pullback": pullback
        }
        return trend_bullish and pullback and rsi_pullback, info
    except Exception as e:
        return False, {}


def analiza_5m(simbol):
    try:
        df = get_date(simbol, interval="5m", period="1d")
        if df.empty or len(df) < 20:
            return False, {}, 0

        preturi = df["Close"].tolist()
        high = df["High"].tolist()
        low = df["Low"].tolist()
        open_p = df["Open"].tolist()
        pret_curent = preturi[-1]

        ema9 = calculeaza_ema(preturi, 9)
        ema21 = calculeaza_ema(preturi, 21)
        rsi = calculeaza_rsi(preturi, 14)
        atr = calculeaza_atr(df, 14)

        candle_verde = preturi[-1] > open_p[-1]
        trend_scurt = ema9 > ema21
        rsi_in_crestere = rsi > 45 and rsi < 70
        rang_candle = high[-1] - low[-1]
        candle_solid = rang_candle > atr * 0.3

        info = {
            "pret_5m": pret_curent,
            "ema9_5m": ema9,
            "ema21_5m": ema21,
            "rsi_5m": rsi,
            "atr_5m": atr,
            "candle_verde": candle_verde,
            "trend_scurt": trend_scurt
        }

        confirmat = candle_verde and trend_scurt and rsi_in_crestere and candle_solid
        return confirmat, info, atr
    except Exception as e:
        return False, {}, 0


# ═══════════════════════════════════════
# SEMNAL COMBINAT
# ═══════════════════════════════════════
def analizeaza_semnal(simbol, memorie):
    try:
        if simbol_in_cooldown(simbol, memorie):
            return None, {}

        earnings_curand, data_e, sursa = are_earnings_curand(simbol)
        if earnings_curand:
            return None, {"step": "earnings", "data_earnings": data_e, "sursa_earnings": sursa}

        ok_1d, info_1d = analiza_1d(simbol)
        if not ok_1d:
            return None, {"step": "1d_fail", **info_1d}

        ok_15m, info_15m = analiza_15m(simbol)
        if not ok_15m:
            return None, {"step": "15m_fail", **info_1d, **info_15m}

        ok_5m, info_5m, atr = analiza_5m(simbol)
        if not ok_5m:
            return None, {"step": "5m_fail", **info_1d, **info_15m, **info_5m}

        info = {**info_1d, **info_15m, **info_5m, "atr": atr,
                "step": "all_ok",
                "pret": info_5m["pret_5m"]}

        motiv = (
            f"1D=BULLISH(RSI={info_1d['rsi_1d']:.0f}) | "
            f"15m=PULLBACK(RSI={info_15m['rsi_15m']:.0f}) | "
            f"5m=ENTRY(RSI={info_5m['rsi_5m']:.0f})"
        )
        info["motiv_intrare"] = motiv

        return "long", info

    except Exception as e:
        print(f"  Eroare analiza {simbol}: {e}")
        return None, {}


# ═══════════════════════════════════════
# EXIT
# ═══════════════════════════════════════
def verifica_exit(simbol, pret_intrare, pret_max, trailing_activ):
    try:
        df = get_date(simbol, interval="5m", period="1d")
        if df.empty:
            return False, None, pret_max, trailing_activ

        preturi = df["Close"].tolist()
        pret_curent = preturi[-1]
        rsi = calculeaza_rsi(preturi, 14)
        ema9 = calculeaza_ema(preturi, 9)
        ema21 = calculeaza_ema(preturi, 21)
        variatie = (pret_curent - pret_intrare) / pret_intrare

        pret_max = max(pret_max, pret_curent)

        if variatie >= TRAILING_ACTIV_PCT and not trailing_activ:
            trailing_activ = True
            print(f"  🎯 {simbol} — Trailing activat la {variatie:.2%}")

        if trailing_activ:
            drawdown = (pret_max - pret_curent) / pret_max
            if drawdown >= TRAILING_STOP_PCT:
                return True, f"TRAILING STOP (profit={variatie:.2%})", pret_max, trailing_activ

        if variatie <= -STOP_LOSS_PCT:
            return True, f"STOP LOSS ({variatie:.2%})", pret_max, trailing_activ

        if variatie >= TAKE_PROFIT_PCT:
            return True, f"TAKE PROFIT ({variatie:.2%})", pret_max, trailing_activ

        if ema9 < ema21 and variatie > 0:
            return True, f"EMA9<EMA21 ({variatie:.2%})", pret_max, trailing_activ

        if rsi > 78:
            return True, f"RSI OVERBOUGHT ({rsi:.1f})", pret_max, trailing_activ

        return False, None, pret_max, trailing_activ

    except Exception as e:
        return False, None, pret_max, trailing_activ


# ═══════════════════════════════════════
# CANTITATE
# ═══════════════════════════════════════
def calculeaza_cantitate(pret, atr):
    try:
        account = api.get_account()
        portofoliu = float(account.portfolio_value)
    except:
        portofoliu = 100000

    risc_max = portofoliu * MAX_RISC_PORTOFOLIU
    stop_loss_dinamic = max(STOP_LOSS_PCT, atr / pret * 1.5)
    cantitate_risc = int(risc_max / (pret * stop_loss_dinamic))
    cantitate_size = int(MAX_TRADE_SIZE_USD / pret)
    cantitate = min(cantitate_risc, cantitate_size)
    return max(1, cantitate)


# ═══════════════════════════════════════
# TRANZACTIONARE
# ═══════════════════════════════════════
def deschide_pozitie(simbol, pret, cantitate, motiv, memorie):
    global trades_azi
    try:
        api.submit_order(
            symbol=simbol, qty=cantitate,
            side="buy", type="market", time_in_force="gtc"
        )
        print(f"  ✅ LONG {cantitate}x {simbol} @ ${pret:.2f}")
        print(f"     {motiv}")
        print(f"     SL={STOP_LOSS_PCT:.2%} | TP={TAKE_PROFIT_PCT:.2%}")

        pozitii_deschise[simbol] = {
            "pret_intrare": pret,
            "cantitate": cantitate,
            "pret_max": pret,
            "trailing_activ": False
        }
        trades_azi += 1
        salveaza_pozitii_active()
        log_tranzactie(memorie, simbol, "open_long", pret, cantitate)

    except Exception as e:
        print(f"  Eroare deschidere {simbol}: {e}")


def inchide_pozitie(simbol, motiv, memorie):
    if simbol not in pozitii_deschise:
        return
    pozitie = pozitii_deschise[simbol]
    try:
        df = get_date(simbol, interval="5m", period="1d")
        pret_curent = df["Close"].iloc[-1]

        api.submit_order(
            symbol=simbol, qty=pozitie["cantitate"],
            side="sell", type="market", time_in_force="gtc"
        )
        profit = (pret_curent - pozitie["pret_intrare"]) * pozitie["cantitate"]

        emoji = "🟢" if profit > 0 else "🔴"
        print(f"  {emoji} INCHIS {simbol} | {motiv} | Profit: ${profit:.2f}")
        log_tranzactie(
            memorie, simbol, "close_long",
            pret_curent, pozitie["cantitate"], profit, motiv
        )
        del pozitii_deschise[simbol]
        salveaza_pozitii_active()

    except Exception as e:
        print(f"  Eroare inchidere {simbol}: {e}")


def inchide_toate_pozitiile(memorie):
    if not pozitii_deschise:
        return False
    print(f"\n🔔 INCHIDERE AUTOMATA — aproape de inchiderea bursei")
    print(f"📂 Inchidem {len(pozitii_deschise)} pozitii...")
    for simbol in list(pozitii_deschise.keys()):
        inchide_pozitie(simbol, "END OF DAY", memorie)
    return True


# ═══════════════════════════════════════
# EXPORT CSV
# ═══════════════════════════════════════
def export_csv_automat(memorie, zi=None):
    tranzactii = memorie["tranzactii"]
    if zi is None:
        zi = date.today().isoformat()

    inchideri = [
        t for t in tranzactii
        if t["tip"] == "close_long"
        and t.get("profit") is not None
        and t["data"].startswith(zi)
    ]

    if not inchideri:
        print(f"📊 Nicio inchidere pentru raport ({zi})")
        return

    CSV_FILE = f"/home/liviu_anton/trading/multitf_trades_{zi}.csv"
    campuri = [
        "data_iesire", "simbol", "cantitate",
        "pret_intrare", "pret_iesire",
        "profit_usd", "profit_pct",
        "motiv_exit", "rezultat"
    ]

    rows = []
    for t in inchideri:
        simbol = t["simbol"]
        pret_intrare = 0
        for d in reversed(tranzactii):
            if d["simbol"] == simbol and d["tip"] == "open_long":
                if d["data"] < t["data"]:
                    pret_intrare = d["pret"]
                    break

        profit = t["profit"]
        profit_pct = (profit / (pret_intrare * t["cantitate"]) * 100) if pret_intrare > 0 else 0

        rows.append({
            "data_iesire": t["data"],
            "simbol": simbol,
            "cantitate": t["cantitate"],
            "pret_intrare": round(pret_intrare, 4),
            "pret_iesire": round(t["pret"], 4),
            "profit_usd": round(profit, 2),
            "profit_pct": round(profit_pct, 2),
            "motiv_exit": t.get("motiv", ""),
            "rezultat": "WIN" if profit > 0 else "LOSS"
        })

    with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=campuri)
        writer.writeheader()
        writer.writerows(rows)

    wins = [r for r in rows if r["rezultat"] == "WIN"]
    profit_total = sum(r["profit_usd"] for r in rows)
    win_rate = len(wins) / len(rows) if rows else 0

    print(f"\n📊 RAPORT {zi}: {len(rows)} trades | "
          f"Win rate={win_rate:.1%} | Profit=${profit_total:.2f}")


# ═══════════════════════════════════════
# DISPLAY
# ═══════════════════════════════════════
def afiseaza_stats(memorie):
    stats = memorie["stats"]
    total = stats["wins"] + stats["losses"]
    rata = stats["wins"] / total if total > 0 else 0
    print(f"\n📊 STATS: Profit=${stats['total_profit']:.2f} | "
          f"Win rate={rata:.1%} | Trades={total}")


def afiseaza_pozitii():
    if not pozitii_deschise:
        return
    print("\n📂 POZITII:")
    for simbol, poz in pozitii_deschise.items():
        try:
            df = get_date(simbol, interval="5m", period="1d")
            pret_curent = df["Close"].iloc[-1]
            variatie = (pret_curent - poz["pret_intrare"]) / poz["pret_intrare"]
        except:
            variatie = 0
        emoji = "🟢" if variatie > 0 else "🔴"
        trailing = "🎯" if poz.get("trailing_activ") else ""
        print(f"  {emoji} {simbol} | ${poz['pret_intrare']:.2f} | P&L={variatie:.2%} {trailing}")


# ═══════════════════════════════════════
# AGENT PRINCIPAL
# ═══════════════════════════════════════
def agent():
    global trades_azi, data_curenta, raport_generat_azi, inchidere_facuta_azi, pozitii_deschise

    print("🤖 MULTI-TIMEFRAME OPTIMIZAT (R1 + A + stop intrari 2h)")
    print(f"📋 Actiuni: {', '.join(ACTIUNI)}")
    print(f"💰 Max trade: ${MAX_TRADE_SIZE_USD} | Max pozitii: {MAX_POZITII}")
    print(f"🛑 SL={STOP_LOSS_PCT:.2%} | TP={TAKE_PROFIT_PCT:.2%} | Trailing={TRAILING_STOP_PCT:.2%} (activ la {TRAILING_ACTIV_PCT:.1%})")
    print(f"🎯 15m: pullback<{PULLBACK_MAX_15M:.1%} | RSI {RSI_15M_MIN}-{RSI_15M_MAX}")
    print(f"📅 Blocare earnings: {ZILE_BLOCARE_EARNINGS} zi | Manual: {len(EARNINGS_MANUAL)} simboluri")
    print(f"🔔 Inchidere: {MINUTE_INAINTE_CLOSE} min inainte | Stop intrari: {ORE_STOP_INTRARI}h inainte")
    print(f"⏱️  Scanare la fiecare {INTERVAL_SCANARE}s | Cache grafice la 5 cicluri")
    print("-" * 60)

    memorie = incarca_memorie()
    pozitii_deschise = incarca_pozitii_active()
    ciclu = 0

    while True:
        try:
            if datetime.now().date() != data_curenta:
                data_curenta = datetime.now().date()
                trades_azi = 0
                raport_generat_azi = False
                inchidere_facuta_azi = False
                print("🔄 Zi noua")

            print(f"\n⏰ {datetime.now().strftime('%H:%M:%S')} | "
                  f"Ciclu #{ciclu} | "
                  f"Trades: {trades_azi}/{MAX_TRADES_PER_DAY} | "
                  f"Pozitii: {len(pozitii_deschise)}/{MAX_POZITII}")

            clock = api.get_clock()
            secunde_pana_close = (clock.next_close - clock.timestamp).total_seconds()
            ore_pana_close = secunde_pana_close / 3600

            # INCHIDERE + RAPORT cu X min inainte de inchiderea reala
            if clock.is_open and not inchidere_facuta_azi:
                if secunde_pana_close <= MINUTE_INAINTE_CLOSE * 60:
                    print(f"\n🔔 Mai sunt {secunde_pana_close/60:.0f} min pana la inchidere")
                    if pozitii_deschise:
                        inchide_toate_pozitiile(memorie)
                    inchidere_facuta_azi = True
                    if not raport_generat_azi:
                        export_csv_automat(memorie, zi=data_curenta.isoformat())
                        raport_generat_azi = True

            if not clock.is_open:
                if not raport_generat_azi:
                    export_csv_automat(memorie, zi=data_curenta.isoformat())
                    raport_generat_azi = True
                if ciclu % 10 == 0:
                    salveaza_grafice_cache()
                print(f"❌ Bursa inchisa. Se deschide: {clock.next_open}")
                ciclu += 1
                time.sleep(300)
                continue

            if inchidere_facuta_azi:
                print("🌙 Inchidere automata facuta. Astept inchiderea bursei...")
                time.sleep(60)
                continue

            # EXIT — verifica pozitiile existente (mereu, indiferent de ora)
            for simbol in list(pozitii_deschise.keys()):
                poz = pozitii_deschise[simbol]
                exit_acum, motiv, pret_max_nou, trailing_nou = verifica_exit(
                    simbol, poz["pret_intrare"], poz["pret_max"],
                    poz.get("trailing_activ", False)
                )
                pozitii_deschise[simbol]["pret_max"] = pret_max_nou
                pozitii_deschise[simbol]["trailing_activ"] = trailing_nou
                if exit_acum:
                    inchide_pozitie(simbol, motiv, memorie)
                else:
                    salveaza_pozitii_active()

            # CAUTA INTRARI — blocate in ultimele ORE_STOP_INTRARI ore
            locuri_libere = MAX_POZITII - len(pozitii_deschise)

            if ore_pana_close < ORE_STOP_INTRARI:
                print(f"  ⏰ Mai sunt {ore_pana_close:.1f}h pana la inchidere — nu mai deschid pozitii noi")
            elif locuri_libere > 0 and trades_azi < MAX_TRADES_PER_DAY:
                print(f"\n🔍 Scanez {len(ACTIUNI)} actiuni")

                for simbol in ACTIUNI:
                    if simbol in pozitii_deschise:
                        continue

                    semnal, info = analizeaza_semnal(simbol, memorie)
                    step = info.get("step", "?")

                    if step == "all_ok":
                        print(f"  ⭐ {simbol}: TOATE TF aliniate! {info.get('motiv_intrare', '')}")
                    elif step == "earnings":
                        print(f"  📅 {simbol}: BLOCAT — earnings pe {info.get('data_earnings')} ({info.get('sursa_earnings')})")
                    elif step == "5m_fail":
                        print(f"  🟡 {simbol}: 1D+15m OK, asteapt 5m entry")
                    elif step == "15m_fail":
                        print(f"  🟠 {simbol}: 1D OK, asteapt pullback 15m")
                    else:
                        print(f"  ⏳ {simbol}: trend 1D nu e bullish")

                    if semnal == "long" and trades_azi < MAX_TRADES_PER_DAY:
                        atr = info.get("atr", info["pret"] * 0.01)
                        cantitate = calculeaza_cantitate(info["pret"], atr)
                        if cantitate >= 1:
                            deschide_pozitie(
                                simbol, info["pret"], cantitate,
                                info.get("motiv_intrare", ""), memorie
                            )

            afiseaza_pozitii()
            afiseaza_stats(memorie)

            if ciclu % 5 == 0:
                salveaza_grafice_cache()

            ciclu += 1
            time.sleep(INTERVAL_SCANARE)

        except Exception as e:
            print(f"❌ Eroare: {e}")
            time.sleep(60)


def start():
    clock = api.get_clock()
    if clock.is_open:
        print("✅ Bursa DESCHISA!")
    else:
        print(f"❌ Bursa INCHISA — se deschide la: {clock.next_open}")
    agent()


start()