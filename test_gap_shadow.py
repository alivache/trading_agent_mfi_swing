"""Teste pentru pasul 3: bucla zilnica, comportamentul la esec si evaluarea de seara.

Ceasul e simulat (trimitem `acum` explicit), deci testele nu asteapta nimic si nu ating
reteaua. Reutilizam scenariul si clientii falsi din test_gap_scanner.
"""
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import requests

import evalueaza_gap
import gap_scanner
import gap_shadow
from analyst import NY
from gap_shadow import ce_de_facut, pas
from test_gap_scanner import ACUM, ClaudeFals, ClientDateFals, bara, jurnal_temporar, scenariu  # noqa: F401

ZI = "2026-11-16"
SESIUNE = {"open": datetime(2026, 11, 16, 9, 30, tzinfo=NY),
           "close": datetime(2026, 11, 16, 16, 0, tzinfo=NY)}


def la(ora: int, minut: int = 0, zi: int = 16) -> datetime:
    return datetime(2026, 11, zi, ora, minut, tzinfo=NY)


# ---------------------------------------------------------------- decizia (functie pura)

@pytest.mark.parametrize("acum, asteptat", [
    (la(8, 59), None),
    (la(9, 0), "analiza"),
    (la(9, 24), "analiza"),
    (la(9, 25), None),            # pornit prea tarziu: ziua se sare
    (la(16, 29), None),
])
def test_fereastra_de_analiza(acum, asteptat):
    assert ce_de_facut(acum, SESIUNE, {}) == asteptat


def test_zi_fara_bursa_nu_face_nimic():
    assert ce_de_facut(la(9, 0), None, {}) is None


def test_analiza_nu_se_repeta_in_aceeasi_zi():
    stare = {ZI: {"analiza_facuta": True, "incercari_analiza": 1, "ultima_analiza": la(9, 0).isoformat()}}
    assert ce_de_facut(la(9, 5), SESIUNE, stare) is None


def test_analiza_esuata_se_reincearca_dupa_pauza():
    stare = {ZI: {"analiza_facuta": False, "incercari_analiza": 1, "ultima_analiza": la(9, 0).isoformat()}}
    assert ce_de_facut(la(9, 1), SESIUNE, stare) is None
    assert ce_de_facut(la(9, 2), SESIUNE, stare) == "analiza"


def test_dupa_trei_esecuri_nu_mai_incearca():
    stare = {ZI: {"analiza_facuta": False, "incercari_analiza": 3, "ultima_analiza": la(9, 4).isoformat()}}
    assert ce_de_facut(la(9, 20), SESIUNE, stare) is None


def test_evaluarea_vine_la_30_min_dupa_inchidere_si_doar_daca_a_existat_analiza():
    facuta = {ZI: {"analiza_facuta": True}}
    assert ce_de_facut(la(16, 29), SESIUNE, facuta) is None
    assert ce_de_facut(la(16, 30), SESIUNE, facuta) == "evaluare"
    assert ce_de_facut(la(16, 30), SESIUNE, {}) is None


def test_zi_scurta_evaluare_dupa_inchiderea_de_la_13():
    scurta = {"open": la(9, 30, zi=27), "close": la(13, 0, zi=27)}
    stare = {"2026-11-27": {"analiza_facuta": True}}
    assert ce_de_facut(la(13, 30, zi=27), scurta, stare) == "evaluare"


# ---------------------------------------------------------------- bucla cap-coada, cu clienti falsi

def bare_sesiune(pret_start: float, pret_final: float, varf: float | None = None,
                 minim: float | None = None) -> list[dict]:
    """Bare de 1 min 9:30-15:59, pret liniar; varf/minim intr-o bara la 11:00."""
    rezultat, n = [], 390
    for i in range(n):
        t = la(9, 30) + timedelta(minutes=i)
        p = pret_start + (pret_final - pret_start) * i / (n - 1)
        b = bara(t, p, 1000)
        b["o"] = pret_start if i == 0 else p
        if t == la(11, 0):
            b["h"] = varf if varf is not None else p
            b["l"] = minim if minim is not None else p
        rezultat.append(b)
    return rezultat


class ClientCuCalendar(ClientDateFals):
    """Scenariul din test_gap_scanner + calendar + bare de 1 minut pentru evaluare."""

    def __init__(self, *args, minute1=None, zile_bursa=(ZI,), **kw):
        super().__init__(*args, **kw)
        self.minute1 = minute1 or {}
        self.zile_bursa = zile_bursa

    def get(self, cale, params=None, trading=False):
        if self.eroare:
            raise self.eroare
        if cale == "/v2/calendar":
            zi = params["start"]
            return [{"date": zi, "open": "09:30", "close": "16:00"}] if zi in self.zile_bursa else []
        if cale == "/v2/stocks/bars" and params["timeframe"] == "1Min":
            start = datetime.fromisoformat(params["start"])
            end = datetime.fromisoformat(params["end"])
            return {"bars": {s: [b for b in self.minute1.get(s, [])
                                 if start <= datetime.fromisoformat(b["t"].replace("Z", "+00:00")) <= end]
                             for s in params["symbols"].split(",")}, "next_page_token": None}
        return super().get(cale, params, trading)


def client_complet():
    baza = scenariu()
    minute1 = {
        # BUNB (avoid in ClaudeFals): +5% de la deschidere; BUNA (tradeable): -2%
        "BUNB": bare_sesiune(22.0, 23.1),
        "BUNA": bare_sesiune(11.6, 11.368, minim=9.9),     # coboara sub inchiderea de ieri (10)
        "QQQ": bare_sesiune(600.0, 606.0),                 # piata +1%
    }
    # pretul de dupa iesire (15:50) nu trebuie sa conteze
    for b in minute1["BUNA"]:
        if datetime.fromisoformat(b["t"].replace("Z", "+00:00")).astimezone(NY) >= la(15, 50):
            b["o"] = b["c"] = b["h"] = b["l"] = 50.0
    return ClientCuCalendar(baza.active, baza.movers, baza.zilnic, baza.minute5,
                            baza.stiri_lista, minute1=minute1)


def test_o_zi_completa_analiza_dimineata_evaluare_seara(jurnal_temporar):
    client, claude, sesiuni = client_complet(), ClaudeFals(), {}
    assert pas(la(8, 50), client, sesiuni, claude) is None
    assert pas(la(9, 0), client, sesiuni, claude) == "analiza"
    assert pas(la(9, 1), client, sesiuni, claude) is None          # nu se repeta
    assert pas(la(12, 0), client, sesiuni, claude) is None
    assert pas(la(16, 30), client, sesiuni, claude) == "evaluare"
    assert pas(la(16, 31), client, sesiuni, claude) is None

    evaluari = {e["simbol"]: e for e in evalueaza_gap.toate_evaluarile()}
    buna, bunb = evaluari["BUNA"], evaluari["BUNB"]
    assert buna["verdict"] == "tradeable" and bunb["verdict"] == "avoid"
    # iesirea e inchiderea barei de 15:49 (a 380-a bara din 390), nu pretul final de la 15:59
    fractie = 379 / 389
    bunb_asteptat = ((22.0 + 1.1 * fractie) / 22.0 - 1) * 100
    qqq_asteptat = ((600.0 + 6.0 * fractie) / 600.0 - 1) * 100
    assert bunb["randament_pct"] == pytest.approx(bunb_asteptat, abs=0.002)
    assert bunb["qqq_pct"] == pytest.approx(qqq_asteptat, abs=0.002)
    assert bunb["exces_pct"] == pytest.approx(bunb_asteptat - qqq_asteptat, abs=0.003)
    assert buna["randament_pct"] < 0 and buna["iesire"] < 20     # bara de 50 de dupa 15:50 ignorata
    assert buna["mae_pct"] == pytest.approx((9.9 / 11.6 - 1) * 100, abs=0.01)
    assert buna["gap_umplut"] is True and bunb["gap_umplut"] is False
    assert buna["gap_pana_la_deschidere_pct"] == pytest.approx((11.6 / 11.5 - 1) * 100, abs=0.01)


def test_repornirea_serviciului_nu_reia_analiza(jurnal_temporar):
    client, claude = client_complet(), ClaudeFals()
    assert pas(la(9, 0), client, {}, claude) == "analiza"
    # proces nou (dictionar de sesiuni gol), aceeasi stare pe disc
    assert pas(la(9, 3), client, {}, claude) is None


def test_scanner_cazut_la_9_se_reincearca_si_reuseste(jurnal_temporar):
    client, claude, sesiuni = client_complet(), ClaudeFals(), {}
    sesiuni[ZI] = SESIUNE                       # calendarul a fost deja citit
    client.eroare = ConnectionError("Alpaca indisponibil")
    assert pas(la(9, 0), client, sesiuni, claude) == "analiza"
    assert gap_shadow.citeste_stare()[ZI]["analiza_facuta"] is False
    client.eroare = None
    assert pas(la(9, 1), client, sesiuni, claude) is None          # inca in pauza
    assert pas(la(9, 2), client, sesiuni, claude) == "analiza"
    assert gap_shadow.citeste_stare()[ZI]["analiza_facuta"] is True


def test_claude_cazut_toti_candidatii_devin_avoid(jurnal_temporar):
    class ClaudeCazut:
        beta = SimpleNamespace(messages=SimpleNamespace(
            create=lambda **kw: (_ for _ in ()).throw(ConnectionError("API jos"))))
    r = gap_shadow.ruleaza_analiza(ACUM, client_date=scenariu(), client_claude=ClaudeCazut())
    assert len(r["verdicte"]) == 2
    assert all(v["verdict"] == "avoid" and v["factor_marime"] == 0.0 and not v["valid"]
               for v in r["verdicte"])


def test_zi_fara_bursa_nu_cheama_scannerul(jurnal_temporar):
    client = client_complet()
    client.zile_bursa = ()
    assert pas(la(9, 0), client, {}, ClaudeFals()) is None
    assert not any(c == "/v2/stocks/bars" for c, _ in client.cereri)


# ---------------------------------------------------------------- rezumatul evaluarilor

def test_rezumat_pe_grupe_de_verdict():
    evaluari = [
        {"zi": ZI, "verdict": "tradeable", "valid": True, "randament_pct": 2.0, "exces_pct": 1.0, "gap_umplut": False},
        {"zi": ZI, "verdict": "tradeable", "valid": True, "randament_pct": -1.0, "exces_pct": -2.0, "gap_umplut": True},
        {"zi": ZI, "verdict": "avoid", "valid": True, "randament_pct": 3.0, "exces_pct": 3.0, "gap_umplut": False},
        {"zi": ZI, "verdict": "avoid", "valid": False, "randament_pct": 1.0, "exces_pct": 1.0, "gap_umplut": False},
        {"zi": ZI, "verdict": "avoid", "valid": True, "eroare": "fara bare"},   # ignorat
    ]
    r = evalueaza_gap.rezumat(evaluari)
    assert r["tradeable"]["n"] == 2 and r["tradeable"]["exces_mediu_pct"] == -0.5
    assert r["tradeable"]["rata_castig_pct"] == 50.0 and r["tradeable"]["gap_umplut_pct"] == 50.0
    assert r["avoid"]["n"] == 1
    assert r["invalid (esec analist)"]["n"] == 1


# ---------------------------------------------------------------- reincercari la Alpaca

class RaspunsFals:
    def __init__(self, cod):
        self.status_code = cod

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)

    def json(self):
        return {"ok": True}


def client_cu_raspunsuri(monkeypatch, coduri):
    c = gap_scanner.ClientDate(pauza_intre_cereri_s=0)
    raspunsuri = iter(coduri)
    apeluri = []

    def get(url, params=None, timeout=None):
        apeluri.append(url)
        return RaspunsFals(next(raspunsuri))
    monkeypatch.setattr(c.sesiune, "get", get)
    return c, apeluri


def test_eroare_500_se_reincearca(monkeypatch):
    c, apeluri = client_cu_raspunsuri(monkeypatch, [500, 500, 200])
    assert c.get("/x", pauza_s=0) == {"ok": True}
    assert len(apeluri) == 3


def test_eroare_400_nu_se_reincearca(monkeypatch):
    c, apeluri = client_cu_raspunsuri(monkeypatch, [403, 200])
    with pytest.raises(requests.HTTPError):
        c.get("/x", pauza_s=0)
    assert len(apeluri) == 1


def test_dupa_toate_incercarile_eroarea_iese(monkeypatch):
    c, apeluri = client_cu_raspunsuri(monkeypatch, [503, 503, 503])
    with pytest.raises(requests.HTTPError):
        c.get("/x", pauza_s=0)
    assert len(apeluri) == 3
