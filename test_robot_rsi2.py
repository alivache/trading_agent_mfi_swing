"""Teste pentru robot_rsi2.py. Fara retea si fara ordine reale: brokerul e inlocuit cu unul fals.

Cel mai important test e paritatea: rulat zi de zi pe aceleasi preturi, robotul trebuie sa
produca exact tranzactiile backtest-ului care a trecut criteriul pre-inregistrat (H3).
"""
import json
import os
import sys
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

import robot_rsi2
from analyst import NY
from robot_rsi2 import SIMBOLURI, cantitate, ce_de_facut, decide, pas, semnal, verifica

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "cercetare"))
from backtest_zilnic import h3_rsi2  # noqa: E402


@pytest.fixture(autouse=True)
def jurnal_temporar(tmp_path, monkeypatch):
    monkeypatch.setattr(robot_rsi2, "DIR_JURNAL", str(tmp_path))
    return tmp_path


# ---------------------------------------------------------------- paritate cu backtest-ul

def tranzactii_robot(inchideri: pd.Series) -> list[tuple]:
    """Robotul rulat zi de zi: la fiecare inchidere vede doar istoricul de pana atunci."""
    in_poz, intrare, rez = False, None, []
    for i in range(len(inchideri)):
        sem = semnal(list(inchideri.iloc[: i + 1].values), in_pozitie=in_poz)
        if sem["actiune"] == "cumpara":
            in_poz, intrare = True, inchideri.index[i]
        elif sem["actiune"] == "vinde":
            rez.append((intrare, inchideri.index[i]))
            in_poz = False
    return rez


def tranzactii_backtest(inchideri: pd.Series) -> list[tuple]:
    df = pd.DataFrame({"open": inchideri, "close": inchideri})
    tr = h3_rsi2(df, cost=0)["tranzactii"]
    return list(zip(tr["intrare"], tr["iesire"])) if len(tr) else []


def test_paritate_cu_backtestul_pe_date_sintetice():
    rng = np.random.default_rng(7)
    idx = pd.bdate_range("2018-01-01", periods=900)
    inchideri = pd.Series(100 * np.exp(np.cumsum(rng.normal(0.0004, 0.012, len(idx)))), index=idx)
    robot, test = tranzactii_robot(inchideri), tranzactii_backtest(inchideri)
    assert len(test) > 5                                     # scenariul chiar produce tranzactii
    assert robot == test


@pytest.mark.skipif(not os.path.exists(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                    "cercetare", "cache")),
                    reason="cache-ul cu datele reale exista doar local")
def test_paritate_cu_backtestul_pe_spy_si_qqq_reale():
    from backtest_zilnic import date_zilnice, SECTOARE
    date = date_zilnice(["SPY", "QQQ"] + SECTOARE)
    for s in ("SPY", "QQQ"):
        inchideri = date[s]["close"]
        assert tranzactii_robot(inchideri) == tranzactii_backtest(inchideri)


# ---------------------------------------------------------------- regula

def test_semnal_cere_istoric_de_200_de_zile():
    assert semnal([100.0] * 150, in_pozitie=False)["actiune"] == "nimic"


def test_cumpara_dupa_cadere_brusca_in_trend_crescator():
    p = list(np.linspace(100, 130, 220)) + [126.0, 122.0]
    sem = semnal(p, in_pozitie=False)
    assert sem["actiune"] == "cumpara" and sem["rsi2"] < 10 and sem["pret"] > sem["sma200"]


def test_nu_cumpara_sub_media_pe_200():
    p = list(np.linspace(130, 100, 220)) + [96.0, 92.0]      # trend descendent
    assert semnal(p, in_pozitie=False)["actiune"] == "nimic"


def test_vinde_peste_media_pe_5():
    p = list(np.linspace(100, 130, 220)) + [126.0, 122.0, 131.0]
    assert semnal(p, in_pozitie=True)["actiune"] == "vinde"


def test_cantitate_10000_usd():
    assert cantitate(680.0) == 14 and cantitate(0) == 0


def test_istoricul_nu_cere_date_sip_mai_noi_de_15_minute(monkeypatch):
    """Planul gratuit raspunde 403 la SIP recent: cererea trebuie sa se opreasca cu 16 min in urma."""
    cereri = []

    def bare_fals(client, simboluri, interval, start, end, ajustare="split"):
        cereri.append(end)
        return {simboluri[0]: []}

    broker = robot_rsi2.BrokerAlpaca.__new__(robot_rsi2.BrokerAlpaca)   # fara constructor (fara retea)
    broker.date, broker._bare = None, bare_fals
    acum = datetime.now(NY)
    broker.inchideri_zilnice("SPY", acum)
    assert cereri[0] <= acum - timedelta(minutes=15)


def test_robotul_refuza_alt_cont_decat_paper(monkeypatch):
    monkeypatch.setenv("ALPACA_BASE_URL", "https://api.alpaca.markets")
    with pytest.raises(robot_rsi2.EroareCont):
        robot_rsi2.verifica_paper()
    monkeypatch.setenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets/")
    robot_rsi2.verifica_paper()


# ---------------------------------------------------------------- pasii zilei, cu broker fals

ZI = date(2026, 11, 16)
INCHIDERE = datetime(2026, 11, 16, 16, 0, tzinfo=NY)


class BrokerFals:
    def __init__(self, preturi_istorice=None, pret_curent=None, la_broker=None, respinge_cls=False):
        sus = list(np.linspace(100, 130, 220)) + [126.0]
        self.istoric = preturi_istorice or {s: sus for s in SIMBOLURI}
        self.pret = pret_curent or {s: 122.0 for s in SIMBOLURI}       # a doua zi de cadere -> cumpara
        self.la_broker = la_broker or {}
        self.respinge_cls = respinge_cls
        self.ordine, self.trimise = {}, []
        self.azi = None                                                # bara zilnica a zilei curente

    def sesiune(self, zi):
        return None if zi.weekday() >= 5 else (datetime(zi.year, zi.month, zi.day, 9, 30, tzinfo=NY),
                                               datetime(zi.year, zi.month, zi.day, 16, 0, tzinfo=NY))

    def inchideri_zilnice(self, simbol, pana_la):
        valori = list(self.istoric[simbol])
        zile = list(pd.bdate_range(end=ZI - timedelta(days=1), periods=len(valori)).date)
        if self.azi is not None:
            valori.append(self.azi[simbol])
            zile.append(ZI)
        return pd.Series(valori, index=zile)

    def pret_curent(self, simbol):
        return self.pret[simbol]

    def cantitate_la_broker(self, simbol):
        return self.la_broker.get(simbol, 0.0)

    def trimite(self, simbol, parte, qty, cid, tif):
        if tif == "cls" and self.respinge_cls:
            raise RuntimeError("cls rejected")
        self.trimise.append((simbol, parte, qty, cid, tif))
        self.ordine[cid] = {"id": cid, "status": "accepted", "filled_qty": 0, "pret_mediu": None}
        return {"id": cid, "status": "accepted", "tif": tif}

    def ordin(self, cid):
        return self.ordine.get(cid)

    def executa_toate(self, pret):
        for o in self.ordine.values():
            o.update(status="filled", filled_qty=None, pret_mediu=pret)
        for (s, parte, qty, cid, tif) in self.trimise:
            self.ordine[cid]["filled_qty"] = qty


def stare_goala():
    return {"pozitii": {}, "in_asteptare": {}, "zile": {}}


def test_decizia_trimite_ordine_la_inchidere_cu_10000_usd():
    b, st = BrokerFals(), stare_goala()
    rez = decide(b, datetime(2026, 11, 16, 15, 44, tzinfo=NY), INCHIDERE, st)
    assert [r["actiune"] for r in rez] == ["cumpara", "cumpara"]
    assert b.trimise == [("SPY", "buy", 81, "rsi2-SPY-2026-11-16-buy", "cls"),
                         ("QQQ", "buy", 81, "rsi2-QQQ-2026-11-16-buy", "cls")]
    assert set(st["in_asteptare"]) == {"SPY", "QQQ"}


def test_repornirea_nu_trimite_ordinul_de_doua_ori():
    b, st = BrokerFals(), stare_goala()
    decide(b, datetime(2026, 11, 16, 15, 44, tzinfo=NY), INCHIDERE, st)
    decide(b, datetime(2026, 11, 16, 15, 45, tzinfo=NY), INCHIDERE, st)
    assert len(b.trimise) == 2


def test_cls_respins_trece_pe_ordin_market_obisnuit():
    b, st = BrokerFals(respinge_cls=True), stare_goala()
    rez = decide(b, datetime(2026, 11, 16, 15, 44, tzinfo=NY), INCHIDERE, st)
    assert all(t[4] == "day" and t[3].endswith("-day") for t in b.trimise)
    assert all("respins_cls" in r for r in rez)


def test_dupa_15_50_se_trimite_direct_ordin_day():
    b, st = BrokerFals(), stare_goala()
    decide(b, datetime(2026, 11, 16, 15, 52, tzinfo=NY), INCHIDERE, st)
    assert all(t[4] == "day" for t in b.trimise)


def test_pozitie_disparuta_la_broker_blocheaza_simbolul():
    b = BrokerFals(la_broker={"SPY": 0.0, "QQQ": 81.0})
    st = stare_goala()
    st["pozitii"] = {s: {"qty": 81, "pret_intrare": 120.0, "zi_intrare": "2026-11-12", "cid": "x"} for s in SIMBOLURI}
    b.pret = {s: 131.0 for s in SIMBOLURI}                           # peste SMA5 -> ar vinde
    rez = {r["simbol"]: r for r in decide(b, datetime(2026, 11, 16, 15, 44, tzinfo=NY), INCHIDERE, st)}
    assert "alarma" in rez["SPY"] and rez["SPY"]["actiune"] == "nimic"
    assert rez["QQQ"]["actiune"] == "vinde"
    assert [t[0] for t in b.trimise] == ["QQQ"]


def test_ciclu_complet_cumparare_apoi_vanzare_cu_profit(jurnal_temporar):
    b, st = BrokerFals(), stare_goala()
    decide(b, datetime(2026, 11, 16, 15, 44, tzinfo=NY), INCHIDERE, st)
    b.executa_toate(122.5)
    b.azi = {s: 122.5 for s in SIMBOLURI}
    verifica(b, datetime(2026, 11, 16, 16, 20, tzinfo=NY), st)
    assert st["pozitii"]["SPY"]["qty"] == 81 and st["pozitii"]["SPY"]["pret_intrare"] == 122.5
    assert st["in_asteptare"] == {}

    # a doua zi: revenire peste SMA5 -> vinde
    b2 = BrokerFals(la_broker={"SPY": 81, "QQQ": 81})
    b2.pret = {s: 131.0 for s in SIMBOLURI}
    decide(b2, datetime(2026, 11, 17, 15, 44, tzinfo=NY), datetime(2026, 11, 17, 16, 0, tzinfo=NY), st)
    assert [(t[0], t[1], t[2]) for t in b2.trimise] == [("SPY", "sell", 81), ("QQQ", "sell", 81)]
    b2.executa_toate(131.0)
    verifica(b2, datetime(2026, 11, 17, 16, 20, tzinfo=NY), st)
    assert st["pozitii"] == {}
    tranzactii = [json.loads(l) for l in (jurnal_temporar / "tranzactii.jsonl").read_text().splitlines()]
    assert len(tranzactii) == 2
    assert tranzactii[0]["profit_usd"] == pytest.approx((131.0 - 122.5) * 81)


def test_ordin_neexecutat_la_inchidere_elibereaza_asteptarea():
    b, st = BrokerFals(), stare_goala()
    decide(b, datetime(2026, 11, 16, 15, 44, tzinfo=NY), INCHIDERE, st)
    for o in b.ordine.values():
        o["status"] = "canceled"
    rez = verifica(b, datetime(2026, 11, 16, 16, 20, tzinfo=NY), st)
    assert st["pozitii"] == {} and st["in_asteptare"] == {}
    assert all(r["neexecutat"] == "canceled" for r in rez)


def test_verificarea_compara_cu_semnalul_pe_inchiderea_oficiala():
    b, st = BrokerFals(), stare_goala()
    decide(b, datetime(2026, 11, 16, 15, 44, tzinfo=NY), INCHIDERE, st)
    b.executa_toate(122.5)
    b.azi = {s: 129.9 for s in SIMBOLURI}       # inchiderea oficiala a revenit: testul n-ar fi cumparat
    rez = verifica(b, datetime(2026, 11, 16, 16, 20, tzinfo=NY), st)
    assert all(r["semnal_cu_inchiderea_oficiala"]["actiune"] == "nimic" for r in rez)


# ---------------------------------------------------------------- calendarul zilei

SESIUNE = (datetime(2026, 11, 16, 9, 30, tzinfo=NY), INCHIDERE)


@pytest.mark.parametrize("ora, asteptat", [((15, 43), None), ((15, 44), "decizie"), ((15, 57), "decizie"),
                                           ((15, 58), None), ((16, 19), None)])
def test_fereastra_deciziei(ora, asteptat):
    assert ce_de_facut(datetime(2026, 11, 16, *ora, tzinfo=NY), SESIUNE, stare_goala()) == asteptat


def test_verificarea_dupa_inchidere_doar_daca_s_a_incercat_decizia():
    st = stare_goala()
    assert ce_de_facut(datetime(2026, 11, 16, 16, 20, tzinfo=NY), SESIUNE, st) is None
    st["zile"]["2026-11-16"] = {"incercari_decizie": 1, "decizie": False}
    assert ce_de_facut(datetime(2026, 11, 16, 16, 20, tzinfo=NY), SESIUNE, st) == "verificare"


def test_zi_scurta_decizie_la_12_44():
    scurta = (datetime(2026, 11, 27, 9, 30, tzinfo=NY), datetime(2026, 11, 27, 13, 0, tzinfo=NY))
    assert ce_de_facut(datetime(2026, 11, 27, 12, 44, tzinfo=NY), scurta, stare_goala()) == "decizie"


def test_o_zi_intreaga_prin_bucla(jurnal_temporar):
    b, sesiuni = BrokerFals(), {}
    assert pas(b, datetime(2026, 11, 16, 12, 0, tzinfo=NY), sesiuni) is None
    assert pas(b, datetime(2026, 11, 16, 15, 44, tzinfo=NY), sesiuni) == "decizie"
    assert pas(b, datetime(2026, 11, 16, 15, 45, tzinfo=NY), sesiuni) is None      # nu se repeta
    b.executa_toate(122.5)
    assert pas(b, datetime(2026, 11, 16, 16, 20, tzinfo=NY), sesiuni) == "verificare"
    assert pas(b, datetime(2026, 11, 16, 16, 21, tzinfo=NY), sesiuni) is None
    assert robot_rsi2.citeste_stare()["pozitii"]["SPY"]["qty"] == 81
    assert pas(b, datetime(2026, 11, 14, 15, 44, tzinfo=NY), {}) is None           # sambata
