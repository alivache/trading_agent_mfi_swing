"""
Backtest cu 4 variante de filtre de intrare, comparate pe aceleasi date.
Foloseste SL/TP din varianta A (cea mai buna din testul anterior).
"""
import os
import csv
import yfinance as yf
import pandas as pd
import numpy as np
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()

ACTIUNI_RAW = os.getenv("ACTIUNI", "AAPL,MSFT,NVDA,GOOGL,AMZN,NFLX")
ACTIUNI = [s.strip().upper() for s in ACTIUNI_RAW.split(",")]
MAX_TRADE_SIZE_USD = float(os.getenv("MAX_TRADE_SIZE_USD", 2500))

PORTOFOLIU_INITIAL = 100000
MAX_RISC_PORTOFOLIU = 0.01
MAX_POZITII = 5
MAX_TRADES_PER_DAY = 10
COOLDOWN_ORE = 4
RSI_OVERBOUGHT = 78

# SL/TP fix — varianta A (cea mai buna)
SL = 0.015
TP = 0.040
TRAIL = 0.010
TRAIL_ACTIV = 0.015

# Variante de filtre
VARIANTE = {
    "BASELINE": {
        "pullback_max": 0.01, "rsi_15m_min": 30, "rsi_15m_max": 55,
        "rsi_5m_min": 45, "rsi_5m_max": 70, "cere_candle_verde": True
    },
    "R1 — pullback larg": {
        "pullback_max": 0.02, "rsi_15m_min": 25, "rsi_15m_max": 60,
        "rsi_5m_min": 45, "rsi_5m_max": 70, "cere_candle_verde": True
    },
    "R2 — 5m permisiv": {
        "pullback_max": 0.01, "rsi_15m_min": 30, "rsi_15m_max": 55,
        "rsi_5m_min": 40, "rsi_5m_max": 72, "cere_candle_verde": False
    },
    "R3 — combinata": {
        "pullback_max": 0.02, "rsi_15m_min": 25, "rsi_15m_max": 60,
        "rsi_5m_min": 40, "rsi_5m_max": 72, "cere_candle_verde": False
    },
}


# ═══════════════════════════════════════
# INDICATORI
# ═══════════════════════════════════════
def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def rsi(s, n=14):
    delta = s.diff()
    castig = delta.where(delta > 0, 0.0)
    pierdere = -delta.where(delta < 0, 0.0)
    avg_c = castig.rolling(n).mean()
    avg_p = pierdere.rolling(n).mean()
    rs = avg_c / avg_p.replace(0, 0.0001)
    return (100 - (100 / (1 + rs))).fillna(50)


def atr_calc(df, n=14):
    high = df["High"]
    low = df["Low"]
    close = df["Close"].shift(1)
    tr = pd.concat([high - low, (high - close).abs(), (low - close).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def fetch(simbol, interval, period):
    df = yf.download(simbol, period=period, interval=interval, progress=False, auto_adjust=False)
    if df.empty:
        return df
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df


def pregateste_date(simbol):
    print(f"  Descarc {simbol}...", end=" ", flush=True)
    try:
        df_1d = fetch(simbol, "1d", "1y")
        df_15m = fetch(simbol, "15m", "60d")
        df_5m = fetch(simbol, "5m", "60d")
        if df_1d.empty or df_15m.empty or df_5m.empty or len(df_1d) < 200:
            print("date insuficiente")
            return None
        df_1d["ema50"] = ema(df_1d["Close"], 50)
        df_1d["ema200"] = ema(df_1d["Close"], 200)
        df_1d["rsi"] = rsi(df_1d["Close"], 14)
        df_15m["ema20"] = ema(df_15m["Close"], 20)
        df_15m["ema50"] = ema(df_15m["Close"], 50)
        df_15m["rsi"] = rsi(df_15m["Close"], 14)
        df_5m["ema9"] = ema(df_5m["Close"], 9)
        df_5m["ema21"] = ema(df_5m["Close"], 21)
        df_5m["rsi"] = rsi(df_5m["Close"], 14)
        df_5m["atr"] = atr_calc(df_5m, 14)
        for d in [df_1d, df_15m, df_5m]:
            if d.index.tz is None:
                d.index = d.index.tz_localize("UTC")
            else:
                d.index = d.index.tz_convert("UTC")
        print("OK")
        return {"1d": df_1d, "15m": df_15m, "5m": df_5m}
    except Exception as e:
        print(f"eroare: {e}")
        return None


def check_1d(df_1d, t):
    df = df_1d[df_1d.index <= t]
    if len(df) < 200:
        return False
    last = df.iloc[-1]
    if pd.isna(last["ema200"]) or pd.isna(last["rsi"]):
        return False
    return last["Close"] > last["ema50"] > last["ema200"] and 40 < last["rsi"] < 75


def check_15m(df_15m, t, params):
    df = df_15m[df_15m.index <= t]
    if len(df) < 50:
        return False
    last = df.iloc[-1]
    if pd.isna(last["ema50"]) or pd.isna(last["rsi"]):
        return False
    pret = last["Close"]
    distanta = abs(pret - last["ema20"]) / last["ema20"]
    return (last["ema20"] > last["ema50"]
            and distanta < params["pullback_max"]
            and params["rsi_15m_min"] < last["rsi"] < params["rsi_15m_max"])


def check_5m(df_5m, t, params):
    df = df_5m[df_5m.index <= t]
    if len(df) < 21:
        return False, None
    last = df.iloc[-1]
    if pd.isna(last["ema21"]) or pd.isna(last["rsi"]) or pd.isna(last["atr"]):
        return False, None
    candle_verde = last["Close"] > last["Open"]
    trend_scurt = last["ema9"] > last["ema21"]
    rsi_ok = params["rsi_5m_min"] < last["rsi"] < params["rsi_5m_max"]
    rang = last["High"] - last["Low"]
    candle_solid = rang > last["atr"] * 0.3

    if params["cere_candle_verde"]:
        ok = candle_verde and trend_scurt and rsi_ok and candle_solid
    else:
        ok = trend_scurt and rsi_ok and candle_solid

    return ok, {"pret": last["Close"], "atr": last["atr"]}


def check_exit(df_5m, t, pret_intrare, pret_max, trailing_activ):
    df = df_5m[df_5m.index <= t]
    if df.empty:
        return False, None, pret_max, trailing_activ
    last = df.iloc[-1]
    pret = last["Close"]
    variatie = (pret - pret_intrare) / pret_intrare
    pret_max = max(pret_max, pret)
    if variatie >= TRAIL_ACTIV and not trailing_activ:
        trailing_activ = True
    if trailing_activ:
        drawdown = (pret_max - pret) / pret_max
        if drawdown >= TRAIL:
            return True, "TRAILING", pret_max, trailing_activ
    if variatie <= -SL:
        return True, "STOP_LOSS", pret_max, trailing_activ
    if variatie >= TP:
        return True, "TAKE_PROFIT", pret_max, trailing_activ
    if not pd.isna(last["ema21"]) and last["ema9"] < last["ema21"] and variatie > 0:
        return True, "EMA9<EMA21", pret_max, trailing_activ
    if not pd.isna(last["rsi"]) and last["rsi"] > RSI_OVERBOUGHT:
        return True, "RSI_OB", pret_max, trailing_activ
    return False, None, pret_max, trailing_activ


def simuleaza(date_simboluri, timestamps, params):
    portofoliu = PORTOFOLIU_INITIAL
    pozitii = {}
    cooldown = {}
    trades = []
    trades_per_zi = {}
    zi_curenta = None

    for t in timestamps:
        zi_t = t.date()
        if zi_t != zi_curenta:
            zi_curenta = zi_t
            trades_per_zi[zi_curenta] = 0

        t_hour = t.hour
        t_min = t.minute
        if t_hour < 13 or (t_hour == 13 and t_min < 30) or t_hour >= 20:
            continue
        eod = (t_hour == 19 and t_min >= 45)

        for s in list(pozitii.keys()):
            poz = pozitii[s]
            if s not in date_simboluri:
                continue
            if eod:
                df = date_simboluri[s]["5m"]
                df_t = df[df.index <= t]
                if df_t.empty:
                    continue
                pret_iesire = df_t.iloc[-1]["Close"]
                profit = (pret_iesire - poz["pret_intrare"]) * poz["cantitate"]
                trades.append({"simbol": s, "profit_usd": profit, "motiv": "EOD",
                               "rezultat": "WIN" if profit > 0 else "LOSS"})
                portofoliu += profit
                if profit < 0:
                    cooldown[s] = t
                del pozitii[s]
                continue

            exit_acum, motiv, pmax, trail = check_exit(
                date_simboluri[s]["5m"], t,
                poz["pret_intrare"], poz["pret_max"], poz["trailing"]
            )
            poz["pret_max"] = pmax
            poz["trailing"] = trail
            if exit_acum:
                df = date_simboluri[s]["5m"]
                df_t = df[df.index <= t]
                pret_iesire = df_t.iloc[-1]["Close"]
                profit = (pret_iesire - poz["pret_intrare"]) * poz["cantitate"]
                trades.append({"simbol": s, "profit_usd": profit, "motiv": motiv,
                               "rezultat": "WIN" if profit > 0 else "LOSS"})
                portofoliu += profit
                if profit < 0:
                    cooldown[s] = t
                del pozitii[s]

        if eod or len(pozitii) >= MAX_POZITII:
            continue
        if trades_per_zi[zi_curenta] >= MAX_TRADES_PER_DAY:
            continue

        for s in date_simboluri.keys():
            if s in pozitii:
                continue
            if s in cooldown:
                if (t - cooldown[s]).total_seconds() / 3600 < COOLDOWN_ORE:
                    continue
                del cooldown[s]
            d = date_simboluri[s]
            if not check_1d(d["1d"], t):
                continue
            if not check_15m(d["15m"], t, params):
                continue
            ok5, info5 = check_5m(d["5m"], t, params)
            if not ok5:
                continue
            pret = info5["pret"]
            atr_v = info5["atr"]
            risc_max = portofoliu * MAX_RISC_PORTOFOLIU
            sl_dinamic = max(SL, atr_v / pret * 1.5)
            cant_risc = int(risc_max / (pret * sl_dinamic))
            cant_size = int(MAX_TRADE_SIZE_USD / pret)
            cantitate = max(1, min(cant_risc, cant_size))
            pozitii[s] = {"pret_intrare": pret, "cantitate": cantitate,
                          "pret_max": pret, "trailing": False}
            trades_per_zi[zi_curenta] += 1
            if len(pozitii) >= MAX_POZITII:
                break

    return trades, portofoliu


def analizeaza(nume, trades, portofoliu):
    total = len(trades)
    if total == 0:
        return {"nume": nume, "total": 0, "win_rate": 0, "pf": 0,
                "randament": 0, "castig_mediu": 0, "pierdere_medie": 0, "rr": 0}
    wins = [t for t in trades if t["rezultat"] == "WIN"]
    losses = [t for t in trades if t["rezultat"] == "LOSS"]
    castig_brut = sum(t["profit_usd"] for t in wins)
    pierdere_bruta = abs(sum(t["profit_usd"] for t in losses))
    win_rate = len(wins) / total * 100
    pf = castig_brut / pierdere_bruta if pierdere_bruta > 0 else 999
    castig_mediu = castig_brut / len(wins) if wins else 0
    pierdere_medie = pierdere_bruta / len(losses) if losses else 0
    return {
        "nume": nume, "total": total, "win_rate": win_rate, "pf": pf,
        "randament": (portofoliu / PORTOFOLIU_INITIAL - 1) * 100,
        "castig_mediu": castig_mediu, "pierdere_medie": pierdere_medie,
        "rr": castig_mediu / pierdere_medie if pierdere_medie > 0 else 0
    }


def main():
    print("\n" + "=" * 75)
    print("BACKTEST RELAXARE FILTRE — 4 variante in paralel")
    print(f"SL/TP fix din varianta A: SL={SL:.1%} TP={TP:.1%} Trail={TRAIL:.1%}")
    print("=" * 75 + "\n")

    print("Descarc datele...")
    date_simboluri = {}
    for s in ACTIUNI:
        d = pregateste_date(s)
        if d:
            date_simboluri[s] = d
    if not date_simboluri:
        return

    timestamps = set()
    for d in date_simboluri.values():
        timestamps.update(d["5m"].index)
    timestamps = sorted(timestamps)
    print(f"\nTimeline: {len(timestamps)} timestamps\n")

    rezultate = []
    for nume, params in VARIANTE.items():
        print(f"Rulez: {nume}")
        if params["cere_candle_verde"]:
            cv = "DA"
        else:
            cv = "NU"
        print(f"  Pullback<{params['pullback_max']:.1%} | "
              f"RSI 15m {params['rsi_15m_min']}-{params['rsi_15m_max']} | "
              f"RSI 5m {params['rsi_5m_min']}-{params['rsi_5m_max']} | "
              f"Candle verde: {cv}")
        trades, portof = simuleaza(date_simboluri, timestamps, params)
        r = analizeaza(nume, trades, portof)
        rezultate.append(r)
        print(f"  → {r['total']} trades | WR={r['win_rate']:.0f}% | "
              f"PF={r['pf']:.2f} | Rand={r['randament']:+.2f}%\n")

    print("=" * 75)
    print("COMPARATIE")
    print("=" * 75)
    print(f"{'Varianta':<22} {'Trades':>7} {'WR':>6} {'PF':>6} "
          f"{'Castig':>9} {'Pierd':>9} {'C/P':>5} {'Rand.':>8}")
    print("-" * 75)
    for r in rezultate:
        if r["total"] == 0:
            print(f"{r['nume']:<22} {'0':>7} {'-':>6} {'-':>6}")
            continue
        print(f"{r['nume']:<22} {r['total']:>7} "
              f"{r['win_rate']:>5.0f}% {r['pf']:>6.2f} "
              f"${r['castig_mediu']:>7.2f} ${r['pierdere_medie']:>7.2f} "
              f"{r['rr']:>5.2f} {r['randament']:>+7.2f}%")
    print("=" * 75)

    valide = [r for r in rezultate if r["total"] > 0]
    if valide:
        bun_pf = max(valide, key=lambda r: r["pf"])
        bun_rand = max(valide, key=lambda r: r["randament"])
        print(f"\n🏆 Cel mai bun Profit Factor: {bun_pf['nume']} (PF={bun_pf['pf']:.2f})")
        print(f"💰 Cel mai bun Randament:    {bun_rand['nume']} ({bun_rand['randament']:+.2f}%)")
        print()


if __name__ == "__main__":
    main()