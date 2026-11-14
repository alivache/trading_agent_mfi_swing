"""Teste pentru backtest_zilnic.py: in primul rand, ca nicio regula nu priveste in viitor."""
import numpy as np
import pandas as pd
import pytest

from backtest_zilnic import h1_trend, h2_rotatie, h3_rsi2, metrici, rsi_wilder


def zile(n, start="2020-01-01"):
    return pd.bdate_range(start, periods=n)


def df_din(inchideri, deschideri=None):
    idx = zile(len(inchideri))
    c = pd.Series(inchideri, index=idx, dtype=float)
    o = pd.Series(deschideri if deschideri is not None else inchideri, index=idx, dtype=float)
    return pd.DataFrame({"open": o, "close": c})


def test_metrici_pe_randament_constant():
    r = pd.Series([0.001] * 252, index=zile(252))
    m = metrici(r)
    assert m["cagr"] == pytest.approx(1.001 ** 252 - 1)
    assert m["cadere_max"] == 0


def test_metrici_cadere_maxima():
    r = pd.Series([0.10, -0.50, 0.20], index=zile(3))
    assert metrici(r)["cadere_max"] == pytest.approx(-0.5)


def test_h1_pozitia_incepe_abia_la_deschiderea_de_dupa_semnal():
    # SMA(3); inchiderea sare peste medie in ziua 4 (index 3)
    df = df_din([10, 10, 10, 20, 20, 20, 20])
    r = h1_trend(df, n_sma=3, cost=0)
    poz = r["pozitie"]
    # semnalul de la inchiderea zilei 3 -> pozitie in intervalul care incepe la deschiderea zilei 4
    assert poz.loc[df.index[3]] == 0.0
    assert poz.loc[df.index[4]] == 1.0


def test_h1_nu_castiga_saltul_din_ziua_semnalului():
    # In ziua 6 pretul sare in timpul zilei de la 10 (deschidere) la 20 (inchidere). Semnalul
    # apare abia la inchidere, deci strategia NU poate prinde saltul; cumpararea-pastrarea da.
    df = df_din([10, 10, 10, 10, 10, 20, 20, 20], deschideri=[10, 10, 10, 10, 10, 10, 20, 20])
    r = h1_trend(df, n_sma=3, cost=0)
    assert r["strategie"].sum() == pytest.approx(0.0)
    assert r["cumparare"].sum() == pytest.approx(1.0)


def test_h1_cost_la_fiecare_schimbare():
    df = df_din([10, 10, 10, 20, 20, 20, 20])
    r = h1_trend(df, n_sma=3, cost=0.01)
    assert r["tranzactii"] == 1
    assert r["strategie"].loc[df.index[4]] == pytest.approx(-0.01)


def test_rsi_wilder_intre_0_si_100():
    c = pd.Series(np.cumsum(np.random.default_rng(1).normal(0, 1, 300)) + 100, index=zile(300))
    r = rsi_wilder(c, 2).dropna()
    assert r.between(0, 100).all()
    assert rsi_wilder(pd.Series(range(1, 30), dtype=float), 2).iloc[-1] == pytest.approx(100)


def test_h3_intra_si_iese_la_inchidere():
    # 210 zile de urcare lina (peste SMA200), apoi o cadere brusca (RSI2 < 10), apoi revenire
    c = list(np.linspace(100, 130, 210)) + [120, 115, 125, 135]
    df = df_din(c)
    r = h3_rsi2(df, cost=0)
    tr = r["tranzactii"]
    assert len(tr) == 1
    t = tr.iloc[0]
    assert t["intrare"] in (df.index[210], df.index[211])
    assert t["randament"] == pytest.approx(df["close"].loc[t["iesire"]] / df["close"].loc[t["intrare"]] - 1)


def test_h2_ponderile_lunii_se_aplica_de_a_doua_zi():
    idx = pd.bdate_range("2020-01-01", "2020-12-31")
    rng = np.random.default_rng(3)
    preturi = {}
    for k, s in enumerate(["A", "B", "C", "D"]):
        trend = 1 + (k - 1.5) * 0.001
        c = pd.Series(100 * trend ** np.arange(len(idx)) * (1 + rng.normal(0, 0.001, len(idx))), index=idx)
        preturi[s] = pd.DataFrame({"open": c, "close": c})
    r = h2_rotatie(preturi, top=2, lung=60, scurt=10, cost=0)
    p = r["ponderi"]
    # in fiecare zi: suma ponderilor <= 1, si se schimba doar in prima zi dupa sfarsitul lunii
    assert (p.sum(axis=1) <= 1.0 + 1e-9).all()
    schimbari = p.index[(p.diff().abs().sum(axis=1) > 0)]
    for zi in schimbari:
        anterior = p.index[p.index.get_loc(zi) - 1]
        assert anterior.month != zi.month             # ziua de dinainte e ultima din luna trecuta
    # cele mai puternice doua (C, D) sunt alese, cele cu trend negativ (A) nu
    assert p.iloc[-1]["D"] == pytest.approx(0.5) and p.iloc[-1]["A"] == 0
