"""Ce s-ar fi intamplat cu cele doua stop-uri din 3-4 sep sub pragul minim vechi de 1.5%."""
import os, sys
import pandas as pd
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()
import alpaca_trade_api as tradeapi

from multi_tf_strategy import (calculeaza_ema, calculeaza_rsi, TRAILING_ACTIVARE_PCT,
                               TRAILING_DISTANTA_PCT, TAKE_PROFIT_PCT, RSI_5M_EXIT,
                               EMA_CROSS_MIN_PROFIT_PCT)

api = tradeapi.REST(os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET"),
                    os.getenv("ALPACA_BASE_URL"), api_version="v2")

CAZURI = [
    # simbol, zi, ora_intrare_NY, pret_intrare, qty_reala, stop_real_pct, pierdere_reala
    ("MU",   "2026-09-03", "11:07", 953.91, 2, 0.009, -17.45),
    ("AMZN", "2026-09-04", "09:37", 259.39, 5, 0.009, -11.88),
]

def bare_5m(simbol, zi):
    b = api.get_bars(simbol, "5Min", start=f"{zi}T09:00:00-04:00",
                     end=f"{zi}T16:05:00-04:00", feed="iex").df
    return b.tz_convert("America/New_York")

for simbol, zi, ora, intrare, qty_real, sl_real, pierdere_reala in CAZURI:
    df = bare_5m(simbol, zi)
    if df.empty:
        print(f"{simbol} {zi}: fara date IEX")
        continue
    df["ema9"] = calculeaza_ema(df["close"], 9)
    df["ema21"] = calculeaza_ema(df["close"], 21)
    df["rsi"] = calculeaza_rsi(df["close"])
    start = pd.Timestamp(f"{zi} {ora}", tz="America/New_York")
    d = df[df.index >= start]

    for sl_pct, eticheta in ((0.009, "prag NOU 0.9%"), (0.015, "prag VECHI 1.5%")):
        # calculeaza_cantitate = min(risc, plafon marime); la preturi mari plafonul leaga
        risc = int(100000 * 0.01 / (intrare * sl_pct))
        size = int(2500 / intrare)
        qty = max(1, min(risc, size))
        pret_max = intrare
        trailing = False
        iesire = motiv = None
        for ts, r in d.iterrows():
            low, high, close = r["low"], r["high"], r["close"]
            pret_max = max(pret_max, high)
            if (high - intrare) / intrare >= TRAILING_ACTIVARE_PCT:
                trailing = True
            if low <= intrare * (1 - sl_pct):
                iesire, motiv = intrare * (1 - sl_pct), f"STOP LOSS (-{sl_pct*100:.1f}%)"; break
            if high >= intrare * (1 + TAKE_PROFIT_PCT):
                iesire, motiv = intrare * (1 + TAKE_PROFIT_PCT), "TAKE PROFIT (+4%)"; break
            if trailing and (pret_max - low) / pret_max >= TRAILING_DISTANTA_PCT:
                iesire, motiv = pret_max * (1 - TRAILING_DISTANTA_PCT), "TRAILING STOP"; break
            pl = (close - intrare) / intrare
            if r["ema9"] < r["ema21"] and pl > EMA_CROSS_MIN_PROFIT_PCT:
                iesire, motiv = close, f"EMA CROSS (+{pl*100:.1f}%)"; break
            if r["rsi"] > RSI_5M_EXIT and pl >= TRAILING_ACTIVARE_PCT:
                iesire, motiv = close, "RSI OVERBOUGHT"; break
            if ts.strftime("%H:%M") >= "15:45":
                iesire, motiv = close, "END OF DAY"; break
        if iesire is None:
            iesire, motiv = d["close"].iloc[-1], "END OF DAY (ultima bara)"
        pnl = (iesire - intrare) * qty
        print(f"{simbol} {zi} {eticheta:16s} qty={qty:2d} iesire=${iesire:8.2f} "
              f"({(iesire-intrare)/intrare*100:+5.2f}%) P&L=${pnl:+7.2f}  {motiv}  [risc={risc} plafon={size}]")
    print(f"   real: qty={qty_real} P&L=${pierdere_reala:+.2f} | "
          f"drum zi: max={d['high'].max():.2f} ({(d['high'].max()-intrare)/intrare*100:+.2f}%) "
          f"min={d['low'].min():.2f} ({(d['low'].min()-intrare)/intrare*100:+.2f}%) "
          f"close={d['close'].iloc[-1]:.2f}\n")
