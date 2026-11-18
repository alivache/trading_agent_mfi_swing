"""H5 din IPOTEZE.md: regula H3 (RSI2) pe 11 ETF-uri in plus fata de SPY si QQQ.

Regula si calculele sunt cele din `backtest_zilnic.h3_rsi2`, neschimbate. Aici se adauga doar
criteriul corectat pentru testare multipla (t > 2.85) si rapoartele informative (suprapunerea
cu SPY, pozitii simultane).

Rulare (in WSL, din radacina proiectului):
    ./venv/bin/python cercetare/h5_rsi2_extins.py
"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from backtest_zilnic import (CALIBRARE, SECTOARE, VERIFICARE, date_zilnice, h3_rsi2,  # noqa: E402
                             metrici, perioada)

UNIVERS = ["IWM", "DIA"] + SECTOARE
PRAG_T = 2.85                       # Bonferroni pentru 11 teste (0.05/11, bilateral)
PRAG_T_PROMITATOR = 2.0


def statistici_tranzactii(tr: pd.DataFrame, p: tuple[str, str]) -> dict:
    """Tranzactiile inchise in perioada p: numar, medie, t, procent castigatoare."""
    if tr.empty:
        return {"n": 0, "medie": np.nan, "t": np.nan, "castig": np.nan}
    t = tr[(tr["iesire"] >= pd.Timestamp(p[0])) & (tr["iesire"] <= pd.Timestamp(p[1]))]
    n = len(t)
    medie = t["randament"].mean() if n else np.nan
    tstat = medie / (t["randament"].std() / np.sqrt(n)) if n > 2 and t["randament"].std() > 0 else np.nan
    return {"n": n, "medie": medie, "t": tstat, "castig": (t["randament"] > 0).mean() if n else np.nan}


def pozitie_zilnica(tr: pd.DataFrame, index: pd.DatetimeIndex) -> pd.Series:
    """1 in zilele in care exista pozitie deschisa la inchidere (intrare inclusiv, iesire exclusiv)."""
    poz = pd.Series(0, index=index)
    for _, x in tr.iterrows():
        poz[(index >= x["intrare"]) & (index < x["iesire"])] = 1
    return poz


def verdict(stat: dict, sharpe_s: float, sharpe_b: float) -> str:
    sharpe_ok = sharpe_s >= sharpe_b
    if stat["medie"] > 0 and stat["t"] > PRAG_T and sharpe_ok:
        return "TRECE"
    if stat["medie"] > 0 and stat["t"] > PRAG_T_PROMITATOR and sharpe_ok:
        return "promitator"
    return "nu trece"


def raport() -> dict:
    date = {**date_zilnice(["SPY", "QQQ"] + SECTOARE), **date_zilnice(["DIA", "IWM"])}
    rezultate, pozitii = {}, {}
    for s in ["SPY", "QQQ"] + UNIVERS:
        r = h3_rsi2(date[s])
        rezultate[s] = r
        pozitii[s] = pozitie_zilnica(r["tranzactii"], date[s].index)

    print(f"H5 — regula H3 pe {len(UNIVERS)} ETF-uri; trece: medie > 0, t > {PRAG_T}, Sharpe >= cumparare\n")
    antet = (f"{'':5s} {'calibrare 2016-2021':>26s} | {'VERIFICARE 2022 - nov 2026':>58s}\n"
             f"{'':5s} {'n':>4s} {'medie':>7s} {'t':>5s}      | {'n':>4s} {'medie':>7s} {'t':>5s} "
             f"{'castig':>6s} {'Sharpe':>6s} {'vs B&H':>6s} {'invest':>6s} {'cu SPY':>6s}  verdict")
    print(antet)
    verdicte = {}
    for s in ["SPY", "QQQ"] + UNIVERS:
        r = rezultate[s]
        c = statistici_tranzactii(r["tranzactii"], CALIBRARE)
        v = statistici_tranzactii(r["tranzactii"], VERIFICARE)
        ms, mb = metrici(perioada(r["strategie"], VERIFICARE)), metrici(perioada(r["cumparare"], VERIFICARE))
        poz_v = perioada(pozitii[s], VERIFICARE)
        spy_v = perioada(pozitii["SPY"], VERIFICARE).reindex(poz_v.index).fillna(0)
        cu_spy = (poz_v & spy_v).sum() / poz_v.sum() if poz_v.sum() else np.nan
        vd = verdict(v, ms["sharpe"], mb["sharpe"]) if s in UNIVERS else "(referinta)"
        if s in UNIVERS:
            verdicte[s] = vd
        print(f"{s:5s} {c['n']:4d} {c['medie']*100:+6.2f}% {c['t']:5.2f}      | "
              f"{v['n']:4d} {v['medie']*100:+6.2f}% {v['t']:5.2f} {v['castig']*100:5.0f}% "
              f"{ms['sharpe']:6.2f} {mb['sharpe']:6.2f} {poz_v.mean()*100:5.0f}% "
              f"{cu_spy*100:5.0f}%  {vd}")

    trec = [s for s, vd in verdicte.items() if vd == "TRECE"]
    portofoliu = ["SPY", "QQQ"] + trec
    simultan = pd.DataFrame({s: perioada(pozitii[s], VERIFICARE) for s in portofoliu}).fillna(0).sum(axis=1)
    print("\ninvest = % din zile in pozitie; cu SPY = % din zilele in pozitie in care si SPY e in pozitie")
    print(f"\nTrec (t > {PRAG_T}): {', '.join(trec) or 'niciunul'}")
    print(f"Promitatoare (2 < t <= {PRAG_T}, nu se adauga): "
          f"{', '.join(s for s, vd in verdicte.items() if vd == 'promitator') or 'niciunul'}")
    print(f"Pozitii simultane pe {' + '.join(portofoliu)} (verificare): maxim {int(simultan.max())}, "
          f"zile cu >= 1 pozitie {(simultan > 0).mean()*100:.0f}%")
    return verdicte


if __name__ == "__main__":
    raport()
