import yfinance as yf
import alpaca_trade_api as tradeapi
import os
import json
import time
import csv
import pandas as pd
from datetime import datetime, timezone, date

# ═══════════════════════════════════════
# CREDENTIALE — modifica aici
# ═══════════════════════════════════════
API_KEY = "PKS7PGH3LE5JXHYMCBOY7O2TNU"
SECRET_KEY = "E981oxjoewJKsQZYd9dXZjfAurKQ753q8CzaHDbRZXPQ"
BASE_URL = "https://paper-api.alpaca.markets"
MAX_TRADE_SIZE_USD = 2500.0
MAX_TRADES_PER_DAY = 100

api = tradeapi.REST(API_KEY, SECRET_KEY, BASE_URL)

# ═══════════════════════════════════════
# SETARI
# ═══════════════════════════════════════
ACTIUNI = [
    "AAPL", "MSFT", "NVDA", "AMD", "TSLA",
    "GOOGL", "AMZN", "NFLX", "COIN", "PYPL"
]

INTERVAL_SCANARE = 300
MEMORIE_FILE = "memorie_swing.json"

EMA_SCURTA = 20
EMA_LUNGA = 50
RSI_PERIOADA = 14
RSI_OVERSOLD = 45

STOP_LOSS_PCT = 0.02
TAKE_PROFIT_PCT = 0.06
TRAILING_STOP_PCT = 0.015
MAX_RISC_PORTOFOLIU = 0.02
MAX_POZITII = 5

trades_azi = 0
data_curenta = datetime.now().date()
pozitii_deschise = {}
raport_generat_azi = False


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
        "stats": {
            "total_profit": 0,
            "wins": 0,
            "losses": 0
        }
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

        if simbol not in memorie["performanta"]:
            memorie["performanta"][simbol] = {
                "profit": 0, "trades": 0, "wins": 0
            }
        p = memorie["performanta"][simbol]
        p["profit"] += profit
        p["trades"] += 1
        if profit > 0:
            p["wins"] += 1

    salveaza_memorie(memorie)


# ═══════════════════════════════════════
# EXPORT CSV AUTOMAT
# ═══════════════════════════════════════
def export_csv_automat(memorie):
    tranzactii = memorie["tranzactii"]
    azi = date.today().isoformat()

    inchideri = [
        t for t in tranzactii
        if t["tip"].startswith("close")
        and t.get("profit") is not None
        and t["data"].startswith(azi)
    ]

    if not inchideri:
        print("📊 Nicio tranzactie de exportat azi.")
        return

    CSV_FILE = f"swing_trades_{azi}.csv"
    campuri = [
        "data_iesire", "simbol", "directie", "cantitate",
        "pret_intrare", "pret_iesire", "profit_usd",
        "profit_pct", "motiv_exit", "rezultat"
    ]

    rows = []
    for t in inchideri:
        simbol = t["simbol"]
        directie = t["tip"].replace("close_", "")
        pret_intrare = 0

        for d in reversed(tranzactii):
            if d["simbol"] == simbol and d["tip"] == f"open_{directie}":
                if d["data"] < t["data"]:
                    pret_intrare = d["pret"]
                    break

        profit = t["profit"]
        profit_pct = (
            (profit / (pret_intrare * t["cantitate"]) * 100)
            if pret_intrare > 0 else 0
        )

        rows.append({
            "data_iesire": t["data"],
            "simbol": simbol,
            "directie": directie.upper(),
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

    print(f"\n{'=' * 50}")
    print(f"📊 RAPORT ZILNIC SWING — {azi}")
    print(f"{'=' * 50}")
    print(f"  🔄 Total trades:  {len(rows)}")
    print(f"  ✅ Wins:          {len(wins)}")
    print(f"  ❌ Losses:        {len(rows) - len(wins)}")
    print(f"  🎯 Win rate:      {win_rate:.1%}")
    print(f"  💰 Profit total:  ${profit_total:.2f}")
    print(f"  💾 CSV salvat:    {CSV_FILE}")
    print(f"{'=' * 50}\n")


# ═══════════════════════════════════════
# INDICATORI
# ═══════════════════════════════════════
def calculeaza_ema(preturi, perioada):
    prices = pd.Series(preturi)
    return prices.ewm(span=perioada, adjust=False).mean().iloc[-1]


def calculeaza_rsi(preturi, perioada=14):
    prices = pd.Series(preturi)
    delta = prices.diff()
    castig = delta.where(delta > 0, 0.0)
    pierdere = -delta.where(delta < 0, 0.0)
    avg_castig = castig.rolling(window=perioada).mean().iloc[-1]
    avg_pierdere = pierdere.rolling(window=perioada).mean().iloc[-1]
    if avg_pierdere == 0:
        return 100
    rs = avg_castig / avg_pierdere
    return 100 - (100 / (1 + rs))


def calculeaza_macd(preturi):
    prices = pd.Series(preturi)
    ema12 = prices.ewm(span=12, adjust=False).mean()
    ema26 = prices.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    histogram = macd - signal
    return macd.iloc[-1], signal.iloc[-1], histogram.iloc[-1]


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


def calculeaza_volum_ratio(volume):
    if len(volume) < 20:
        return 1.0
    vol_recent = sum(volume[-5:]) / 5
    vol_mediu = sum(volume[-20:]) / 20
    return vol_recent / vol_mediu if vol_mediu > 0 else 1.0


def get_date(simbol, interval="1h", period="30d"):
    df = yf.download(simbol, period=period, interval=interval, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df


# ═══════════════════════════════════════
# SEMNAL SWING TRADING — DOAR LONG
# ═══════════════════════════════════════
def analizeaza_semnal(simbol):
    try:
        df = get_date(simbol, interval="1h", period="30d")
        if df.empty or len(df) < 60:
            return None, {}

        preturi = df["Close"].tolist()
        volume = df["Volume"].tolist()
        pret_curent = preturi[-1]

        ema20 = calculeaza_ema(preturi, EMA_SCURTA)
        ema50 = calculeaza_ema(preturi, EMA_LUNGA)
        ema20_prev = calculeaza_ema(preturi[:-1], EMA_SCURTA)
        ema50_prev = calculeaza_ema(preturi[:-1], EMA_LUNGA)
        rsi = calculeaza_rsi(preturi, RSI_PERIOADA)
        macd, signal, histogram = calculeaza_macd(preturi)
        atr = calculeaza_atr(df)
        volum_ratio = calculeaza_volum_ratio(volume)

        info = {
            "pret": pret_curent,
            "ema20": ema20,
            "ema50": ema50,
            "rsi": rsi,
            "macd": macd,
            "signal": signal,
            "histogram": histogram,
            "atr": atr,
            "volum_ratio": volum_ratio
        }

        # ── DOAR LONG
        trend_bullish = ema20 > ema50
        crossover_bullish = ema20_prev < ema50_prev and ema20 > ema50
        rsi_ok_long = RSI_OVERSOLD < rsi < 70
        macd_bullish = macd > signal
        volum_ok = volum_ratio > 0.9

        semnal = None
        motiv = ""

        if trend_bullish and rsi_ok_long and macd_bullish and volum_ok:
            semnal = "long"
            motiv = (f"EMA20({ema20:.2f})>EMA50({ema50:.2f}) | "
                     f"RSI={rsi:.1f} | MACD bullish | Vol={volum_ratio:.1f}x")
            if crossover_bullish:
                motiv += " | ⭐ CROSSOVER"
            info["motiv_intrare"] = motiv

        return semnal, info

    except Exception as e:
        print(f"  Eroare analiza {simbol}: {e}")
        return None, {}


# ═══════════════════════════════════════
# VERIFICARE EXIT
# ═══════════════════════════════════════
def verifica_exit(simbol, pret_intrare, directie, pret_max):
    try:
        df = get_date(simbol, interval="1h", period="5d")
        if df.empty:
            return False, None, pret_max

        preturi = df["Close"].tolist()
        pret_curent = preturi[-1]
        rsi = calculeaza_rsi(preturi, RSI_PERIOADA)
        ema20 = calculeaza_ema(preturi, EMA_SCURTA)
        ema50 = calculeaza_ema(preturi, EMA_LUNGA)
        macd, signal, histogram = calculeaza_macd(preturi)
        variatie = (pret_curent - pret_intrare) / pret_intrare

        if directie == "long":
            pret_max = max(pret_max, pret_curent)
            drawdown = (pret_max - pret_curent) / pret_max

            if drawdown >= TRAILING_STOP_PCT and pret_curent > pret_intrare:
                return True, f"TRAILING STOP (profit={variatie:.2%})", pret_max
            if variatie <= -STOP_LOSS_PCT:
                return True, f"STOP LOSS ({variatie:.2%})", pret_max
            if variatie >= TAKE_PROFIT_PCT:
                return True, f"TAKE PROFIT ({variatie:.2%})", pret_max
            if ema20 < ema50:
                return True, f"EMA CROSSOVER BEARISH ({variatie:.2%})", pret_max
            if macd < signal and histogram < 0 and variatie > 0:
                return True, f"MACD BEARISH ({variatie:.2%})", pret_max
            if rsi > 75:
                return True, f"RSI OVERBOUGHT ({rsi:.1f})", pret_max

        return False, None, pret_max

    except Exception as e:
        return False, None, pret_max


# ═══════════════════════════════════════
# CALCUL CANTITATE
# ═══════════════════════════════════════
def calculeaza_cantitate(pret, atr):
    try:
        account = api.get_account()
        portofoliu = float(account.portfolio_value)
    except:
        portofoliu = 100000

    risc_max = portofoliu * MAX_RISC_PORTOFOLIU
    stop_loss_dinamic = max(STOP_LOSS_PCT, atr / pret * 2)
    cantitate_risc = int(risc_max / (pret * stop_loss_dinamic))
    cantitate_size = int(MAX_TRADE_SIZE_USD / pret)
    cantitate = min(cantitate_risc, cantitate_size)
    return max(1, cantitate), stop_loss_dinamic


# ═══════════════════════════════════════
# TRANZACTIONARE
# ═══════════════════════════════════════
def deschide_pozitie(simbol, directie, pret, cantitate, stop_loss, motiv, memorie):
    global trades_azi
    try:
        api.submit_order(
            symbol=simbol, qty=cantitate,
            side="buy", type="market", time_in_force="gtc"
        )
        print(f"  ✅ LONG {cantitate}x {simbol} @ ${pret:.2f}")
        print(f"     Motiv: {motiv}")
        print(f"     SL={stop_loss:.2%} | TP={TAKE_PROFIT_PCT:.2%}")

        pozitii_deschise[simbol] = {
            "directie": "long",
            "pret_intrare": pret,
            "cantitate": cantitate,
            "pret_max": pret,
            "stop_loss": stop_loss
        }
        trades_azi += 1
        log_tranzactie(memorie, simbol, "open_long", pret, cantitate)

    except Exception as e:
        print(f"  Eroare deschidere {simbol}: {e}")


def inchide_pozitie(simbol, motiv, memorie):
    if simbol not in pozitii_deschise:
        return
    pozitie = pozitii_deschise[simbol]
    try:
        df = get_date(simbol, interval="1h", period="1d")
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

    except Exception as e:
        print(f"  Eroare inchidere {simbol}: {e}")


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
        print("\n📂 Nicio pozitie deschisa")
        return
    print("\n📂 POZITII DESCHISE:")
    for simbol, poz in pozitii_deschise.items():
        variatie = 0
        try:
            df = get_date(simbol, interval="1h", period="1d")
            pret_curent = df["Close"].iloc[-1]
            variatie = (pret_curent - poz["pret_intrare"]) / poz["pret_intrare"]
        except:
            pass
        emoji = "🟢" if variatie > 0 else "🔴"
        print(f"  {emoji} {simbol} LONG | "
              f"Intrare=${poz['pret_intrare']:.2f} | "
              f"P&L={variatie:.2%} | "
              f"SL={poz['stop_loss']:.2%}")


def afiseaza_performanta(memorie):
    if not memorie["performanta"]:
        return
    print("\n🏆 PERFORMANTA PE SIMBOL:")
    print(f"  {'Simbol':<8} {'Trades':<8} {'Win%':<8} {'Profit'}")
    print(f"  {'-' * 35}")
    for simbol, p in sorted(
        memorie["performanta"].items(),
        key=lambda x: x[1]["profit"],
        reverse=True
    ):
        wr = p["wins"] / p["trades"] if p["trades"] > 0 else 0
        emoji = "🟢" if p["profit"] > 0 else "🔴"
        print(f"  {emoji} {simbol:<8} {p['trades']:<8} {wr:<8.0%} ${p['profit']:.2f}")


# ═══════════════════════════════════════
# AGENT PRINCIPAL
# ═══════════════════════════════════════
def agent():
    global trades_azi, data_curenta, raport_generat_azi

    print("🤖 SWING TRADING AGENT — DOAR LONG")
    print(f"📊 Strategie: EMA({EMA_SCURTA}/{EMA_LUNGA}) + RSI({RSI_PERIOADA}) + MACD")
    print(f"💰 Max trade: ${MAX_TRADE_SIZE_USD} | Max pozitii: {MAX_POZITII}")
    print(f"🛑 SL={STOP_LOSS_PCT:.1%} | TP={TAKE_PROFIT_PCT:.1%} | Trailing={TRAILING_STOP_PCT:.1%}")
    print(f"⏱️  Scanare la fiecare {INTERVAL_SCANARE // 60} minute")
    print("-" * 60)

    memorie = incarca_memorie()
    ciclu = 0

    while True:
        try:
            if datetime.now().date() != data_curenta:
                data_curenta = datetime.now().date()
                trades_azi = 0
                raport_generat_azi = False
                print("🔄 Zi noua — reset counter")

            print(f"\n⏰ {datetime.now().strftime('%H:%M:%S')} | "
                  f"Ciclu #{ciclu} | "
                  f"Trades azi: {trades_azi}/{MAX_TRADES_PER_DAY} | "
                  f"Pozitii: {len(pozitii_deschise)}/{MAX_POZITII}")

            clock = api.get_clock()
            if not clock.is_open:
                ora_acum = datetime.now().hour
                minut_acum = datetime.now().minute
                if ora_acum == 23 and minut_acum < 5 and not raport_generat_azi:
                    print("🔔 Bursa s-a inchis — generez raport...")
                    export_csv_automat(memorie)
                    raport_generat_azi = True

                print(f"❌ Bursa inchisa. Se deschide la: {clock.next_open}")
                time.sleep(300)
                continue

            # EXIT pozitii deschise
            for simbol in list(pozitii_deschise.keys()):
                poz = pozitii_deschise[simbol]
                exit_acum, motiv, pret_max_nou = verifica_exit(
                    simbol,
                    poz["pret_intrare"],
                    poz["directie"],
                    poz["pret_max"]
                )
                pozitii_deschise[simbol]["pret_max"] = pret_max_nou
                if exit_acum:
                    inchide_pozitie(simbol, motiv, memorie)

            # CAUTA NOI INTRARI LONG
            locuri_libere = MAX_POZITII - len(pozitii_deschise)

            if locuri_libere > 0 and trades_azi < MAX_TRADES_PER_DAY:
                print(f"\n🔍 Scanez {len(ACTIUNI)} actiuni | Locuri: {locuri_libere}")

                candidati = []
                for simbol in ACTIUNI:
                    if simbol in pozitii_deschise:
                        continue

                    semnal, info = analizeaza_semnal(simbol)

                    if info and "rsi" in info:
                        print(f"  {simbol}: P=${info.get('pret', 0):.2f} | "
                              f"EMA20=${info.get('ema20', 0):.2f} | "
                              f"EMA50=${info.get('ema50', 0):.2f} | "
                              f"RSI={info.get('rsi', 0):.1f} | "
                              f"MACD={'▲' if info.get('macd', 0) > info.get('signal', 0) else '▼'} | "
                              f"Vol={info.get('volum_ratio', 0):.1f}x | "
                              f"Semnal={'🟢 LONG' if semnal == 'long' else '⏳ none'}")

                    if semnal:
                        candidati.append((simbol, semnal, info))

                candidati.sort(key=lambda x: x[2].get("volum_ratio", 0), reverse=True)

                for simbol, semnal, info in candidati[:locuri_libere]:
                    if trades_azi >= MAX_TRADES_PER_DAY:
                        break
                    cantitate, stop_loss = calculeaza_cantitate(
                        info["pret"], info["atr"]
                    )
                    deschide_pozitie(
                        simbol, semnal, info["pret"],
                        cantitate, stop_loss,
                        info.get("motiv_intrare", ""),
                        memorie
                    )
            else:
                print(f"\n⏳ Portofoliu plin sau limita trades atinsa")

            afiseaza_pozitii()
            afiseaza_stats(memorie)
            afiseaza_performanta(memorie)

            ciclu += 1
            print(f"\n⏳ Urmatoarea scanare in {INTERVAL_SCANARE // 60} minute...")
            time.sleep(INTERVAL_SCANARE)

        except Exception as e:
            print(f"❌ Eroare generala: {e}")
            time.sleep(60)


# ═══════════════════════════════════════
# START
# ═══════════════════════════════════════
def start():
    clock = api.get_clock()
    if clock.is_open:
        print("✅ Bursa DESCHISA!")
        agent()
    else:
        print(f"❌ Bursa INCHISA — se deschide la: {clock.next_open}")
        if input("Pornesc si astept? (da/nu): ").lower() == "da":
            agent()


start()
