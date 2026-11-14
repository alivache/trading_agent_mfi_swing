"""Ce ar fi castigat semnalele OFI+VWAP din ultima saptamana (8-11 sep 2026).

Semnalele nu au reguli proprii de iesire — sunt shadow mode, doar intrari. Le dam
exact motorul de iesire al agentului principal (stop, trailing, TP, EMA cross, RSI,
END OF DAY) si sizing-ul lui, ca sa fie comparabile cu tranzactiile reale.

Semnalele duplicate pe aceeasi bara VWAP sunt ignorate (bug reparat pe 13 sep 2026):
intrarea s-ar fi facut o singura data.
"""
import csv, os, sys
import pandas as pd

AICI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(AICI))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(AICI), ".env"))
import alpaca_trade_api as tradeapi

from multi_tf_strategy import (calculeaza_ema, calculeaza_rsi, calculeaza_atr,
                               MAX_TRADE_SIZE_USD, STOP_LOSS_MIN_PCT,
                               TRAILING_ACTIVARE_PCT, TRAILING_DISTANTA_PCT,
                               TAKE_PROFIT_PCT, RSI_5M_EXIT, EMA_CROSS_MIN_PROFIT_PCT)

api = tradeapi.REST(os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY"),
                    os.getenv("ALPACA_BASE_URL"), api_version="v2")
NY = "America/New_York"


def bare_5m(simbol, zi):
    start = (pd.Timestamp(zi) - pd.Timedelta(days=4)).strftime("%Y-%m-%d")
    b = api.get_bars(simbol, "5Min", start=f"{start}T09:00:00-04:00",
                     end=f"{zi}T16:05:00-04:00", feed="iex").df
    return b.tz_convert(NY) if not b.empty else b


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


semnale = list(csv.DictReader(open(os.path.join(AICI, "ofi_semnale_vm.csv"))))
semnale = [s for s in semnale if s["timestamp"] >= "2026-09-08"]

vazute, total = set(), 0.0
print(f"{'semnal':17s} {'sim':5s} {'intrare':>9s} {'qty':>4s} {'iesire':>9s} {'pnl$':>8s}  motiv")
for s in semnale:
    cheie = (s["simbol"], s["close"], s["vwap"])   # aceeasi bara VWAP = acelasi eveniment
    if cheie in vazute:
        print(f"{s['timestamp'][:16]:17s} {s['simbol']:5s}  (duplicat pe aceeasi bara VWAP — ignorat)")
        continue
    vazute.add(cheie)

    ts = pd.Timestamp(s["timestamp"]).tz_convert(NY)
    zi = ts.strftime("%Y-%m-%d")
    df = bare_5m(s["simbol"], zi).copy()
    df["ema9"] = calculeaza_ema(df["close"], 9)
    df["ema21"] = calculeaza_ema(df["close"], 21)
    df["rsi"] = calculeaza_rsi(df["close"])

    # intrarea se face la urmatoarea bara de 5m dupa semnal, la deschiderea ei
    urm = df[df.index > ts]
    if urm.empty:
        print(f"{s['timestamp'][:16]:17s} {s['simbol']:5s}  fara bare dupa semnal")
        continue
    intrare_ts = urm.index[0]
    intrare = float(urm.iloc[0]["open"])
    qty = int(MAX_TRADE_SIZE_USD / intrare)

    istoric = df[df.index <= intrare_ts]
    atr = calculeaza_atr(istoric.rename(columns={"high": "High", "low": "Low",
                                                 "close": "Close"}), 14)
    sl = max(STOP_LOSS_MIN_PCT, atr / intrare * 1.5)
    pret, motiv = replay(df, intrare_ts, intrare, sl)
    pnl = (pret - intrare) * qty
    total += pnl
    print(f"{s['timestamp'][:16]:17s} {s['simbol']:5s} {intrare:9.2f} {qty:4d} {pret:9.2f} "
          f"{pnl:+8.2f}  {motiv} (SL {sl*100:.2f}%, {(pret-intrare)/intrare*100:+.2f}%)")

print(f"\nTOTAL {len(vazute)} semnale distincte: ${total:+.2f}")
