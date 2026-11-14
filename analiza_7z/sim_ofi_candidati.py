"""Are OFI edge de unul singur, fara confirmarea VWAP? (8-11 sep 2026)

Replay pe toti cei 214 candidati ai saptamanii, cu motorul de iesire al agentului
principal. Randamentele sunt in procente pe tranzactie, nu in dolari: intrarile se
suprapun masiv (44 doar pe TXN), deci un P&L cumulat ar presupune un portofoliu care
n-ar fi putut exista sub MAX_POZITII.

Control: aceleasi simboluri si aceleasi zile, dar intrari la fiecare 30 de minute,
indiferent de OFI. Fara el nu se poate spune daca un plus vine din semnal sau din
faptul ca saptamana a fost in crestere.
"""
import collections, csv, os, statistics as st, sys
import pandas as pd

AICI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(AICI))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(AICI), ".env"))
import alpaca_trade_api as tradeapi

from multi_tf_strategy import (calculeaza_ema, calculeaza_rsi, calculeaza_atr,
                               STOP_LOSS_MIN_PCT, TRAILING_ACTIVARE_PCT,
                               TRAILING_DISTANTA_PCT, TAKE_PROFIT_PCT,
                               RSI_5M_EXIT, EMA_CROSS_MIN_PROFIT_PCT)

api = tradeapi.REST(os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY"),
                    os.getenv("ALPACA_BASE_URL"), api_version="v2")
NY = "America/New_York"
_cache = {}


def bare_5m(simbol, zi):
    cheie = (simbol, zi)
    if cheie not in _cache:
        start = (pd.Timestamp(zi) - pd.Timedelta(days=4)).strftime("%Y-%m-%d")
        b = api.get_bars(simbol, "5Min", start=f"{start}T09:00:00-04:00",
                         end=f"{zi}T16:05:00-04:00", feed="iex").df
        if not b.empty:
            b = b.tz_convert(NY).copy()
            b["ema9"] = calculeaza_ema(b["close"], 9)
            b["ema21"] = calculeaza_ema(b["close"], 21)
            b["rsi"] = calculeaza_rsi(b["close"])
        _cache[cheie] = b
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
            return intrare * (1 - stop_pct), "STOP LOSS"
        if high >= intrare * (1 + TAKE_PROFIT_PCT):
            return intrare * (1 + TAKE_PROFIT_PCT), "TAKE PROFIT"
        if trailing and (pret_max - low) / pret_max >= TRAILING_DISTANTA_PCT:
            return pret_max * (1 - TRAILING_DISTANTA_PCT), "TRAILING STOP"
        pl = (close - intrare) / intrare
        if r["ema9"] < r["ema21"] and pl > EMA_CROSS_MIN_PROFIT_PCT:
            return close, "EMA CROSS"
        if r["rsi"] > RSI_5M_EXIT and pl >= TRAILING_ACTIVARE_PCT:
            return close, "RSI OVERBOUGHT"
        if ts.strftime("%H:%M") >= "15:45":
            return close, "END OF DAY"
    return (d["close"].iloc[-1], "END OF DAY") if not d.empty else (None, "FARA DATE")


def tranzactie(simbol, ts):
    """Intrare la deschiderea urmatoarei bare de 5m dupa momentul dat."""
    zi = ts.strftime("%Y-%m-%d")
    df = bare_5m(simbol, zi)
    if df.empty:
        return None
    urm = df[df.index > ts]
    if urm.empty:
        return None
    intrare_ts, intrare = urm.index[0], float(urm.iloc[0]["open"])
    istoric = df[df.index <= intrare_ts]
    atr = calculeaza_atr(istoric.rename(columns={"high": "High", "low": "Low",
                                                 "close": "Close"}), 14)
    sl = max(STOP_LOSS_MIN_PCT, atr / intrare * 1.5)
    pret, motiv = replay(df, intrare_ts, intrare, sl)
    if pret is None:
        return None
    return (pret - intrare) / intrare * 100, motiv, intrare_ts


def raport(rezultate, eticheta):
    r = [x[0] for x in rezultate]
    if not r:
        print(f"{eticheta}: niciun rezultat"); return
    w = [x for x in r if x > 0]
    print(f"\n{eticheta}")
    print(f"   n={len(r)}  medie={st.mean(r):+.3f}%  mediana={st.median(r):+.3f}%  "
          f"WR={100*len(w)/len(r):.0f}%  total simplu={sum(r):+.2f}%")
    print(f"   min={min(r):+.2f}%  max={max(r):+.2f}%  stdev={st.pstdev(r):.3f}%")
    d = collections.Counter(x[1] for x in rezultate)
    print("   iesiri: " + "  ".join(f"{k}={v}" for k, v in d.most_common()))


candidati = [r for r in csv.DictReader(open(os.path.join(AICI, "ofi_candidati_saptamana.csv")))]
print(f"candidati OFI in 8-11 sep: {len(candidati)}")

semnale, vazute = [], set()
for c in candidati:
    ts = pd.Timestamp(c["timestamp"]).tz_convert(NY)
    rez = tranzactie(c["simbol"], ts)
    if rez is None:
        continue
    semnale.append(rez)
    vazute.add((c["simbol"], rez[2]))          # o singura intrare pe bara de 5m
raport(semnale, "A. TOTI candidatii OFI (intrari suprapuse)")

unice, deja = [], set()
for c in candidati:
    ts = pd.Timestamp(c["timestamp"]).tz_convert(NY)
    rez = tranzactie(c["simbol"], ts)
    if rez is None or (c["simbol"], rez[2]) in deja:
        continue
    deja.add((c["simbol"], rez[2]))
    unice.append(rez)
raport(unice, "B. Candidati OFI, o intrare pe bara de 5m")

# Control: aceleasi simboluri si zile, intrari la fiecare 30 de minute
control = []
perechi = {(c["simbol"], c["timestamp"][:10]) for c in candidati}
for simbol, zi in sorted(perechi):
    df = bare_5m(simbol, zi)
    if df.empty:
        continue
    sesiune = df[(df.index >= f"{zi} 09:30") & (df.index <= f"{zi} 15:00")]
    for ts in sesiune.index[::6]:              # 6 bare de 5m = 30 min
        rez = tranzactie(simbol, ts)
        if rez:
            control.append(rez)
raport(control, "C. CONTROL — aceleasi simboluri/zile, intrari la 30 min, fara OFI")


# Diferenta OFI vs control e distinctibila de zgomot? (Welch, aproximatie normala)
def welch(a, b, eticheta):
    ma, mb = st.mean(a), st.mean(b)
    va, vb = st.variance(a) / len(a), st.variance(b) / len(b)
    se = (va + vb) ** 0.5
    t = (ma - mb) / se
    print(f"\n{eticheta}: diferenta={ma-mb:+.3f} pp/tranzactie  SE={se:.3f}  t={t:+.2f}")
    print("   " + ("distinctibila de zgomot" if abs(t) > 1.96 else
                   "NU e distinctibila de zgomot (|t| < 1.96)"))

welch([x[0] for x in unice], [x[0] for x in control], "B vs C")
