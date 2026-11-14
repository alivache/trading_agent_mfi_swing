"""Backtest pe bare zilnice pentru ipotezele H1, H2, H3 din IPOTEZE.md.

Regulile sunt exact cele pre-inregistrate; aici nu se optimizeaza nimic. Calculele sunt
functii pure pe DataFrame-uri (testabile fara retea); descarcarea e in `date_zilnice`.

Convenții (de ce asa):
  - un semnal de la inchiderea zilei t se executa la deschiderea zilei t+1 (H1, H2);
    randamentele se masoara deschidere -> deschidere, ca sa nu folosim pretul la care
    semnalul a fost calculat;
  - H3 intra si iese la inchidere (ordin "market on close"), cu semnal calculat din
    datele de pana la inchiderea aceleiasi zile;
  - banii neinvestiti au randament 0 (conservator).

Rulare (in WSL, din radacina proiectului):
    ./venv/bin/python cercetare/backtest_zilnic.py
"""
import os
import pickle
import sys
from datetime import datetime

import numpy as np
import pandas as pd

AICI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(AICI))

ZILE_AN = 252
COST_ETF_PE_SENS = 0.0005            # 0.05% pe sens
CALIBRARE = ("2016-01-01", "2021-12-31")
VERIFICARE = ("2022-01-01", "2026-11-13")
SECTOARE = ["XLB", "XLE", "XLF", "XLI", "XLK", "XLP", "XLU", "XLV", "XLY"]
DIR_CACHE = os.path.join(AICI, "cache")


# ---------------------------------------------------------------- metrici

def metrici(r: pd.Series) -> dict:
    """Randament anual, volatilitate, Sharpe (fara rata fara risc), cadere maxima."""
    r = r.dropna()
    if len(r) < 2:
        return {}
    capital = (1 + r).cumprod()
    ani = len(r) / ZILE_AN
    vol = r.std() * np.sqrt(ZILE_AN)
    return {
        "cagr": capital.iloc[-1] ** (1 / ani) - 1,
        "vol": vol,
        "sharpe": r.mean() / r.std() * np.sqrt(ZILE_AN) if r.std() > 0 else np.nan,
        "cadere_max": (capital / capital.cummax() - 1).min(),
        "zile": len(r),
    }


def perioada(r: pd.Series, p: tuple[str, str]) -> pd.Series:
    return r[(r.index >= pd.Timestamp(p[0])) & (r.index <= pd.Timestamp(p[1]))]


def rsi_wilder(close: pd.Series, n: int) -> pd.Series:
    d = close.diff()
    castig = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    pierdere = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = castig / pierdere
    return 100 - 100 / (1 + rs)


# ---------------------------------------------------------------- H1: trend pe indice

def h1_trend(df: pd.DataFrame, n_sma: int = 200, cost: float = COST_ETF_PE_SENS) -> dict:
    """Detin indicele cand inchiderea e peste SMA(n); altfel cash. Executie la deschiderea urmatoare.

    `df` are coloanele open, close, indexate pe zile. Intoarce randamentele zilnice ale
    strategiei si ale cumpararii-pastrarii (ambele deschidere->deschidere) si pozitia.
    """
    sma = df["close"].rolling(n_sma).mean()
    semnal = (df["close"] > sma).astype(float)
    semnal[sma.isna()] = np.nan                     # inainte de 200 de zile: nedefinit
    # pozitia in intervalul deschidere(t) -> deschidere(t+1) e semnalul de la inchiderea t-1
    pozitie = semnal.shift(1)
    r_oo = df["open"].shift(-1) / df["open"] - 1     # deschidere t -> deschidere t+1
    schimbari = pozitie.diff().abs().fillna(0)
    strategie = pozitie * r_oo - schimbari * cost
    valid = pozitie.notna() & r_oo.notna()
    return {"strategie": strategie[valid], "cumparare": r_oo[valid], "pozitie": pozitie[valid],
            "tranzactii": int(schimbari[valid].sum())}


# ---------------------------------------------------------------- H2: rotatie pe sectoare

def h2_rotatie(preturi: dict[str, pd.DataFrame], top: int = 3, lung: int = 126, scurt: int = 21,
               cost: float = COST_ETF_PE_SENS) -> dict:
    """Top `top` sectoare dupa randamentul t-lung..t-scurt, rebalansare lunara, filtru absolut."""
    inchideri = pd.DataFrame({s: d["close"] for s, d in preturi.items()}).dropna()
    deschideri = pd.DataFrame({s: d["open"] for s, d in preturi.items()}).reindex(inchideri.index)
    mom = inchideri.shift(scurt) / inchideri.shift(lung) - 1
    ultima_din_luna = inchideri.index.to_series().groupby(inchideri.index.to_period("M")).transform("max")
    e_rebalansare = inchideri.index == ultima_din_luna.values

    ponderi = pd.DataFrame(np.nan, index=inchideri.index, columns=inchideri.columns)
    for zi in inchideri.index[e_rebalansare]:
        m = mom.loc[zi].dropna()
        if len(m) < len(inchideri.columns):
            continue                                # inca nu exista 6 luni de istoric
        alesi = [s for s in m.sort_values(ascending=False).index[:top] if m[s] > 0]
        ponderi.loc[zi] = 0.0
        for s in alesi:
            ponderi.loc[zi, s] = 1.0 / top          # sectorul respins de filtru -> cash
    ponderi = ponderi.ffill()
    # ponderile decise la inchiderea t se aplica de la deschiderea t+1
    activ = ponderi.shift(1)
    r_oo = deschideri.shift(-1) / deschideri - 1
    turnover = activ.diff().abs().sum(axis=1)
    strategie = (activ * r_oo).sum(axis=1, min_count=1) - turnover * cost
    valid = activ.notna().all(axis=1) & r_oo.notna().all(axis=1)
    return {"strategie": strategie[valid], "ponderi": activ[valid]}


# ---------------------------------------------------------------- H3: revenire RSI(2)

def h3_rsi2(df: pd.DataFrame, cost: float = COST_ETF_PE_SENS) -> dict:
    """Cumpar la inchidere cand RSI(2) < 10 si inchiderea > SMA200; vand la inchidere peste SMA5."""
    c = df["close"]
    rsi = rsi_wilder(c, 2)
    sma200, sma5 = c.rolling(200).mean(), c.rolling(5).mean()
    pozitie = pd.Series(0.0, index=c.index)
    tranzactii, in_pozitie, intrare_zi = [], False, None
    for i, zi in enumerate(c.index):
        if np.isnan(sma200.iloc[i]) or np.isnan(rsi.iloc[i]):
            continue
        if not in_pozitie and rsi.iloc[i] < 10 and c.iloc[i] > sma200.iloc[i]:
            in_pozitie, intrare_zi = True, zi
        elif in_pozitie and c.iloc[i] > sma5.iloc[i]:
            tranzactii.append({"intrare": intrare_zi, "iesire": zi,
                               "randament": c.loc[zi] / c.loc[intrare_zi] - 1 - 2 * cost})
            in_pozitie = False
        pozitie.iloc[i] = 1.0 if in_pozitie else 0.0
    r_cc = c.pct_change()
    # pozitia la inchiderea t castiga randamentul inchidere t -> inchidere t+1
    pos_prev = pozitie.shift(1).fillna(0)
    schimbari = pozitie.diff().abs().fillna(0)
    strategie = pos_prev * r_cc - schimbari * cost
    valid = sma200.notna()
    return {"strategie": strategie[valid].fillna(0), "cumparare": r_cc[valid].fillna(0),
            "tranzactii": pd.DataFrame(tranzactii), "timp_investit": pozitie[valid].mean()}


# ---------------------------------------------------------------- date

def date_zilnice(simboluri: list[str]) -> dict[str, pd.DataFrame]:
    """Bare zilnice SIP ajustate complet (split + dividende), cu cache pe disc."""
    os.makedirs(DIR_CACHE, exist_ok=True)
    cale = os.path.join(DIR_CACHE, "zilnic_etf_" + "_".join(sorted(simboluri)) + ".pkl")
    if os.path.exists(cale):
        with open(cale, "rb") as f:
            return pickle.load(f)
    import gap_scanner
    from analyst import NY
    brute = gap_scanner.bare(gap_scanner.ClientDate(), simboluri, "1Day",
                             datetime(2016, 1, 1, tzinfo=NY), datetime(2026, 11, 14, tzinfo=NY),
                             ajustare="all")
    rezultat = {}
    for s, lista in brute.items():
        df = pd.DataFrame(lista)
        df.index = pd.to_datetime(df["t"]).dt.tz_convert("America/New_York").dt.tz_localize(None).dt.normalize()
        rezultat[s] = df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})[
            ["open", "high", "low", "close", "volume"]]
    with open(cale, "wb") as f:
        pickle.dump(rezultat, f)
    return rezultat


# ---------------------------------------------------------------- raport

def _linie(nume: str, m: dict) -> str:
    if not m:
        return f"    {nume:28s} (fara date)"
    return (f"    {nume:28s} CAGR {m['cagr']*100:+6.1f}%  vol {m['vol']*100:5.1f}%  "
            f"Sharpe {m['sharpe']:5.2f}  cadere max {m['cadere_max']*100:6.1f}%")


def raport() -> dict:
    date = date_zilnice(["SPY", "QQQ"] + SECTOARE)
    verdicte = {}

    print("H1 — trend: detin indicele peste SMA200, altfel cash")
    trece_h1 = True
    for idx in ("SPY", "QQQ"):
        r = h1_trend(date[idx])
        for nume, p in (("calibrare", CALIBRARE), ("VERIFICARE", VERIFICARE)):
            s, b = metrici(perioada(r["strategie"], p)), metrici(perioada(r["cumparare"], p))
            investit = perioada(r["pozitie"], p).mean()
            print(f"  {idx} {nume} ({p[0][:4]}–{p[1][:4]}), investit {investit*100:.0f}% din timp, "
                  f"{int(perioada(r['pozitie'], p).diff().abs().sum())} schimbari")
            print(_linie("strategie", s))
            print(_linie("cumparare si pastrare", b))
            if nume == "VERIFICARE":
                ok = s["sharpe"] >= b["sharpe"] and s["cadere_max"] >= 0.7 * b["cadere_max"]
                trece_h1 &= ok
                print(f"    criteriu: Sharpe {s['sharpe']:.2f} >= {b['sharpe']:.2f} si cadere "
                      f"{s['cadere_max']*100:.1f}% <= 70% din {b['cadere_max']*100:.1f}% -> "
                      f"{'DA' if ok else 'NU'}")
    verdicte["H1"] = trece_h1

    print("\nH2 — rotatie: top 3 sectoare dupa momentum 6-1 luni, lunar, filtru absolut")
    r2 = h2_rotatie({s: date[s] for s in SECTOARE})
    spy = h1_trend(date["SPY"])["cumparare"]
    trece_h2 = False
    for nume, p in (("calibrare", CALIBRARE), ("VERIFICARE", VERIFICARE)):
        s = metrici(perioada(r2["strategie"], p))
        b = metrici(perioada(spy.reindex(r2["strategie"].index), p))
        cash = (perioada(r2["ponderi"], p).sum(axis=1) < 0.99).mean()
        print(f"  {nume} ({p[0][:4]}–{p[1][:4]}), zile cu macar un loc in cash: {cash*100:.0f}%")
        print(_linie("strategie", s))
        print(_linie("SPY cumparare si pastrare", b))
        if nume == "VERIFICARE":
            trece_h2 = s["cagr"] > b["cagr"] and s["sharpe"] > b["sharpe"]
            print(f"    criteriu: CAGR {s['cagr']*100:+.1f}% > {b['cagr']*100:+.1f}% si Sharpe "
                  f"{s['sharpe']:.2f} > {b['sharpe']:.2f} -> {'DA' if trece_h2 else 'NU'}")
    verdicte["H2"] = trece_h2

    print("\nH3 — revenire: RSI(2) < 10 peste SMA200, iesire peste SMA5")
    trece_h3 = True
    for idx in ("SPY", "QQQ"):
        r = h3_rsi2(date[idx])
        tr = r["tranzactii"]
        for nume, p in (("calibrare", CALIBRARE), ("VERIFICARE", VERIFICARE)):
            t = tr[(tr["iesire"] >= pd.Timestamp(p[0])) & (tr["iesire"] <= pd.Timestamp(p[1]))]
            s, b = metrici(perioada(r["strategie"], p)), metrici(perioada(r["cumparare"], p))
            n = len(t)
            medie = t["randament"].mean() if n else np.nan
            tstat = medie / (t["randament"].std() / np.sqrt(n)) if n > 2 else np.nan
            print(f"  {idx} {nume}: {n} tranzactii, medie {medie*100:+.2f}%/tranzactie (t={tstat:.2f}), "
                  f"castig {(t['randament'] > 0).mean()*100:.0f}%")
            print(_linie("strategie (toate zilele)", s))
            print(_linie("cumparare si pastrare", b))
            if nume == "VERIFICARE":
                ok = medie > 0 and tstat > 2 and s["sharpe"] >= b["sharpe"]
                trece_h3 &= ok
                print(f"    criteriu: medie > 0 cu t > 2 si Sharpe {s['sharpe']:.2f} >= {b['sharpe']:.2f} "
                      f"-> {'DA' if ok else 'NU'}")
    verdicte["H3"] = trece_h3

    print("\nVERDICT (perioada de verificare 2022 – nov 2026):")
    for h, ok in verdicte.items():
        print(f"  {h}: {'TRECE' if ok else 'nu trece'}")
    return verdicte


if __name__ == "__main__":
    raport()
