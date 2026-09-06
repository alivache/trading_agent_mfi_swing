"""Replay al tuturor tranzactiilor inchise (25 aug - 4 sep 2026) sub cele doua
praguri minime de stop loss: 0.9% (actual, commit c1b3e0d) vs 1.5% (vechiul).

Stopul efectiv e max(prag, 1.5xATR/pret), ca in calculeaza_cantitate, deci pragul
conteaza doar pe tranzactiile cu ATR mic. Cantitatea e cea reala din CSV pentru
ambele scenarii: pe esantionul asta notionalele sunt lipite de MAX_TRADE_SIZE_USD,
deci plafonul de marime leaga, nu cel de risc, si pragul nu schimba sizing-ul.
"""
import os, sys, glob, csv
import pandas as pd

AICI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(AICI))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(AICI), ".env"))
import alpaca_trade_api as tradeapi

from multi_tf_strategy import (calculeaza_ema, calculeaza_rsi, calculeaza_atr,
                               TRAILING_ACTIVARE_PCT, TRAILING_DISTANTA_PCT,
                               TAKE_PROFIT_PCT, RSI_5M_EXIT, EMA_CROSS_MIN_PROFIT_PCT)

api = tradeapi.REST(os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET"),
                    os.getenv("ALPACA_BASE_URL"), api_version="v2")
NY = "America/New_York"
_cache = {}


def bare_5m(simbol, zi):
    """Bare 5m pe 4 zile calendaristice inainte de `zi`, ca ATR(14) sa aiba istoric."""
    cheie = (simbol, zi)
    if cheie not in _cache:
        start = (pd.Timestamp(zi) - pd.Timedelta(days=4)).strftime("%Y-%m-%d")
        b = api.get_bars(simbol, "5Min", start=f"{start}T09:00:00-04:00",
                         end=f"{zi}T16:05:00-04:00", feed="iex").df
        _cache[cheie] = b.tz_convert(NY) if not b.empty else b
    return _cache[cheie]


def replay(df, intrare_ts, intrare, stop_pct):
    d = df[df.index >= intrare_ts]
    pret_max, trailing = intrare, False
    for ts, r in d.iterrows():
        low, high, close = r["low"], r["high"], r["close"]
        pret_max = max(pret_max, high)
        if (high - intrare) / intrare >= TRAILING_ACTIVARE_PCT:
            trailing = True
        if low <= intrare * (1 - stop_pct):
            return intrare * (1 - stop_pct), f"STOP LOSS (-{stop_pct*100:.2f}%)"
        if high >= intrare * (1 + TAKE_PROFIT_PCT):
            return intrare * (1 + TAKE_PROFIT_PCT), "TAKE PROFIT (+4%)"
        if trailing and (pret_max - low) / pret_max >= TRAILING_DISTANTA_PCT:
            return pret_max * (1 - TRAILING_DISTANTA_PCT), "TRAILING STOP"
        pl = (close - intrare) / intrare
        if r["ema9"] < r["ema21"] and pl > EMA_CROSS_MIN_PROFIT_PCT:
            return close, f"EMA CROSS (+{pl*100:.1f}%)"
        if r["rsi"] > RSI_5M_EXIT and pl >= TRAILING_ACTIVARE_PCT:
            return close, "RSI OVERBOUGHT"
        if ts.strftime("%H:%M") >= "15:45":
            return close, "END OF DAY"
    return (d["close"].iloc[-1], "END OF DAY (ultima bara)") if not d.empty else (intrare, "FARA DATE")


trades = []
for f in sorted(glob.glob(os.path.join(AICI, "multitf_trades_2026-*.csv"))):
    trades += list(csv.DictReader(open(f)))

PRAGURI = [("NOU 0.9%", 0.009), ("VECHI 1.5%", 0.015)]
total = {e: 0.0 for e, _ in PRAGURI}
total_real = 0.0
n_diferite = 0

print(f"{'zi':11s} {'sim':5s} {'qty':>3s} {'real$':>8s} | "
      + " | ".join(f"{e:>10s} {'SL':>6s} {'$':>8s}  {'iesire':<22s}" for e, _ in PRAGURI))

for t in trades:
    simbol = t["simbol"]
    iesire_ts = pd.Timestamp(t["data_iesire"], tz=NY)
    intrare_ts = iesire_ts - pd.Timedelta(minutes=int(t["durata_min"]))
    zi = intrare_ts.strftime("%Y-%m-%d")
    intrare = float(t["pret_intrare"])
    qty = int(t["cantitate"])
    real = float(t["profit_usd"])
    total_real += real

    df = bare_5m(simbol, zi)
    if df.empty:
        print(f"{zi} {simbol:5s} fara date IEX")
        continue
    df = df.copy()
    df["ema9"] = calculeaza_ema(df["close"], 9)
    df["ema21"] = calculeaza_ema(df["close"], 21)
    df["rsi"] = calculeaza_rsi(df["close"])

    istoric = df[df.index <= intrare_ts]
    atr = calculeaza_atr(istoric.rename(columns={"high": "High", "low": "Low",
                                                 "close": "Close"}), 14)
    atr_pct = atr / intrare

    linie = f"{zi} {simbol:5s} {qty:3d} {real:8.2f} |"
    stopuri = []
    for eticheta, prag in PRAGURI:
        sl = max(prag, atr_pct * 1.5)
        stopuri.append(sl)
        pret, motiv = replay(df, intrare_ts, intrare, sl)
        pnl = (pret - intrare) * qty
        total[eticheta] += pnl
        linie += f" {(pret-intrare)/intrare*100:+9.2f}% {sl*100:5.2f}% {pnl:+8.2f}  {motiv:<22s} |"
    if abs(stopuri[0] - stopuri[1]) > 1e-9:
        n_diferite += 1
    print(linie)

print(f"\ntranzactii: {len(trades)}   dintre care pragul chiar difera (ATR mic): {n_diferite}")
print(f"P&L real (executat, stop 1.5% pana pe 5 sep):  ${total_real:+.2f}")
for e, _ in PRAGURI:
    print(f"P&L simulat prag {e:11s}: ${total[e]:+.2f}")
