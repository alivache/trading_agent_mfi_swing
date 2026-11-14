"""Teste pentru gap_scanner.py si gap_shadow.py — pasul 2: scannerul + integrarea.

Totul pe date sintetice si cu un client Alpaca fals: niciun test nu atinge reteaua.
Ziua de test: luni 16 nov 2026, analiza la 9:00 ET -> date pana la 8:44 ET.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

import analyst
import gap_scanner
import gap_shadow
from analyst import NY
from gap_scanner import (inchidere_anterioara, scaneaza, simbol_eligibil,
                         statistici_premarket, stiri_pe_simbol)

ACUM = datetime(2026, 11, 16, 9, 0, tzinfo=NY)
LIMITA = ACUM - gap_scanner.INTARZIERE_DATE          # 8:44 ET


@pytest.fixture(autouse=True)
def jurnal_temporar(tmp_path, monkeypatch):
    monkeypatch.setattr(analyst, "DIR_JURNAL", str(tmp_path))
    monkeypatch.setattr(analyst, "FISIER_CONTOR", str(tmp_path / "contor_apeluri.json"))
    return tmp_path


def t_utc(dt_et: datetime) -> str:
    """Formatul de timp folosit de Alpaca: UTC, cu Z la final."""
    return dt_et.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def bara(dt_et: datetime, c: float, v: int) -> dict:
    return {"t": t_utc(dt_et), "o": c, "h": c, "l": c, "c": c, "v": v}


def zile_anterioare(n: int) -> list:
    """Ultimele n zile lucratoare dinaintea lui ACUM."""
    zile, z = [], ACUM.date()
    while len(zile) < n:
        z -= timedelta(days=1)
        if z.weekday() < 5:
            zile.append(z)
    return sorted(zile)


def bare_premarket(zi, pret: float, volum_pe_bara: int, pana_la=(9, 0)) -> list[dict]:
    """Bare de 5 min de la 4:00 pana la `pana_la` (exclusiv) in ziua `zi`."""
    rezultat, t = [], datetime(zi.year, zi.month, zi.day, 4, 0, tzinfo=NY)
    sfarsit = t.replace(hour=pana_la[0], minute=pana_la[1])
    while t < sfarsit:
        rezultat.append(bara(t, pret, volum_pe_bara))
        t += timedelta(minutes=5)
    return rezultat


# ---------------------------------------------------------------- calcule pure

@pytest.mark.parametrize("activ, ok, motiv", [
    ({"symbol": "ABCD", "name": "Abcd Inc. Common Stock", "class": "us_equity",
      "tradable": True, "exchange": "NASDAQ"}, True, ""),
    ({"symbol": "ABCDW", "name": "Abcd Inc. Warrant", "class": "us_equity",
      "tradable": True, "exchange": "NASDAQ"}, False, "warrant"),
    ({"symbol": "ABCDR", "name": "Abcd Rights", "class": "us_equity",
      "tradable": True, "exchange": "NASDAQ"}, False, "warrant/right"),
    ({"symbol": "ABCDU", "name": "Abcd Acquisition Corp Units", "class": "us_equity",
      "tradable": True, "exchange": "NASDAQ"}, False, "unit"),
    ({"symbol": "SLND.WS", "name": "Something", "class": "us_equity",
      "tradable": True, "exchange": "NYSE"}, False, "punct"),
    ({"symbol": "OTCX", "name": "Otc Co", "class": "us_equity",
      "tradable": True, "exchange": "OTC"}, False, "bursa OTC"),
    ({"symbol": "NOPE", "name": "Nope Inc", "class": "us_equity",
      "tradable": False, "exchange": "NYSE"}, False, "netranzactionabil"),
    (None, False, "necunoscut"),
])
def test_simbol_eligibil(activ, ok, motiv):
    rezultat, m = simbol_eligibil(activ)
    assert rezultat is ok
    assert motiv in m


def test_inchidere_anterioara_ia_ultima_sesiune_dinainte():
    zile = zile_anterioare(3)
    bare_z = [bara(datetime(z.year, z.month, z.day, 0, 0, tzinfo=NY), 10 + i, 1)
              for i, z in enumerate(zile)]
    bare_z.append(bara(datetime(2026, 11, 16, 0, 0, tzinfo=NY), 99, 1))   # bara de azi, ignorata
    assert inchidere_anterioara(bare_z, ACUM.date()) == 12


def test_statistici_premarket_doar_bare_terminate_inainte_de_limita():
    azi = bare_premarket(ACUM.date(), 10.0, 100, pana_la=(9, 0))
    stat = statistici_premarket(azi, LIMITA)
    # 4:00 .. 8:35 inclusiv = 56 bare (bara de 8:40 se termina la 8:45, dupa 8:44)
    assert stat["volum_premarket"] == 56 * 100
    assert stat["volum_relativ"] is None          # fara istoric


def test_volum_relativ_compara_aceeasi_fereastra_din_zilele_anterioare():
    istoric = []
    for z in zile_anterioare(10):
        istoric += bare_premarket(z, 10.0, 50, pana_la=(16, 0))   # barele de dupa 8:44 nu conteaza
    azi = bare_premarket(ACUM.date(), 11.0, 200)
    stat = statistici_premarket(istoric + azi, LIMITA)
    assert stat["volum_relativ"] == pytest.approx(4.0)
    assert stat["ultim_pret"] == 11.0


def test_volum_relativ_foloseste_doar_ultimele_n_sesiuni(monkeypatch):
    monkeypatch.setattr(gap_scanner, "ZILE_ISTORIC_VOLUM", 2)
    zile = zile_anterioare(3)
    istoric = (bare_premarket(zile[0], 1, 1000) + bare_premarket(zile[1], 1, 10)
               + bare_premarket(zile[2], 1, 10))
    stat = statistici_premarket(istoric + bare_premarket(ACUM.date(), 1, 20), LIMITA)
    assert stat["volum_relativ"] == pytest.approx(2.0)


def test_fara_bare_azi_nu_exista_statistici():
    istoric = bare_premarket(zile_anterioare(1)[0], 10.0, 50)
    assert statistici_premarket(istoric, LIMITA) is None


def test_stiri_pe_simbol_grupeaza_si_ordoneaza():
    stiri = [
        {"created_at": "2026-11-16T11:00:00Z", "headline": "veche", "symbols": ["ABCD"]},
        {"created_at": "2026-11-16T12:30:00Z", "headline": "noua", "symbols": ["ABCD", "EFGH"]},
        {"created_at": "2026-11-16T12:00:00Z", "headline": "alt simbol", "symbols": ["ZZZZ"]},
    ]
    r = stiri_pe_simbol(stiri, ["ABCD", "EFGH"])
    assert [s["titlu"] for s in r["ABCD"]] == ["noua", "veche"]
    assert [s["titlu"] for s in r["EFGH"]] == ["noua"]
    assert r["ABCD"][0]["ora"] == "11-16 07:30 ET"


# ---------------------------------------------------------------- scanarea cap-coada

class ClientDateFals:
    """Raspunde ca Alpaca, din date construite in test. Filtreaza barele dupa start/end."""

    def __init__(self, active, movers, zilnic, minute5, stiri=(), eroare=None):
        self.active, self.movers = active, movers
        self.zilnic, self.minute5, self.stiri_lista = zilnic, minute5, list(stiri)
        self.eroare = eroare
        self.cereri = []

    def get(self, cale, params=None, trading=False):
        self.cereri.append((cale, dict(params or {})))
        if self.eroare:
            raise self.eroare
        if cale == "/v2/assets":
            return self.active
        if cale.endswith("/movers"):
            return {"gainers": [{"symbol": s} for s in self.movers], "losers": []}
        if cale.endswith("/most-actives"):
            return {"most_actives": []}
        if cale == "/v1beta1/news":
            return {"news": self.stiri_lista, "next_page_token": None}
        if cale == "/v2/stocks/bars":
            sursa = self.zilnic if params["timeframe"] == "1Day" else self.minute5
            start = datetime.fromisoformat(params["start"])
            end = datetime.fromisoformat(params["end"])
            rezultat = {}
            for s in params["symbols"].split(","):
                rezultat[s] = [b for b in sursa.get(s, [])
                               if start <= datetime.fromisoformat(b["t"].replace("Z", "+00:00")) <= end]
            return {"bars": rezultat, "next_page_token": None}
        raise AssertionError(f"cerere neasteptata: {cale}")


def activ(simbol, nume="Common Stock", bursa="NASDAQ"):
    return {"symbol": simbol, "name": f"{simbol} {nume}", "class": "us_equity",
            "tradable": True, "exchange": bursa}


def scenariu():
    """Sapte simboluri, fiecare construit sa fie eliminat de alt filtru (sau sa treaca)."""
    zile = zile_anterioare(10)
    # simbol: (inchidere ieri, pret azi, volum/bara azi, volum/bara istoric)
    specificatii = {
        "BUNA": (10.0, 11.5, 2000, 100),      # +15%, volum relativ 20 -> trece
        "BUNB": (20.0, 22.0, 1500, 300),      # +10%, volum relativ 5 -> trece
        "IEFT": (1.0, 1.5, 5000, 100),        # pret sub 2
        "MICG": (10.0, 10.2, 5000, 100),      # gap +2%
        "SUBV": (10.0, 12.0, 10, 1),          # volum pre-market prea mic
        "RELV": (10.0, 12.0, 2000, 1500),     # volum relativ ~1.3
        "NOUA": (10.0, 12.0, 2000, None),     # fara istoric (IPO)
    }
    zilnic, minute5 = {}, {}
    for s, (inch, pret, v_azi, v_ist) in specificatii.items():
        zilnic[s] = [bara(datetime(z.year, z.month, z.day, 0, 0, tzinfo=NY), inch, 1_000_000)
                     for z in zile]
        minute5[s] = bare_premarket(ACUM.date(), pret, v_azi)
        if v_ist is not None:
            for z in zile:
                minute5[s] = bare_premarket(z, inch, v_ist) + minute5[s]
    active = [activ(s) for s in specificatii] + [activ("ABCW", nume="Warrant")]
    stiri = [{"created_at": "2026-11-16T12:05:00Z", "headline": "BUNA wins FDA approval",
              "symbols": ["BUNA"]}]
    return ClientDateFals(active, list(specificatii) + ["ABCW", "LIPS"], zilnic, minute5, stiri)


def test_scanarea_aplica_toate_filtrele_cu_motiv():
    candidati, respinsi = scaneaza(ACUM, client=scenariu())
    # ordonati dupa valoarea pre-market: BUNB 22 x 84.000 > BUNA 11.5 x 112.000,
    # desi BUNA are volum relativ mai mare (20 vs 5)
    assert [c.simbol for c in candidati] == ["BUNB", "BUNA"]
    motive = {r["simbol"]: r["motiv"] for r in respinsi}
    assert "warrant" in motive["ABCW"]
    assert "necunoscut" in motive["LIPS"]
    assert "pret sub" in motive["IEFT"]
    assert "gap sub" in motive["MICG"]
    assert "volum pre-market sub" in motive["SUBV"]
    assert "volum relativ sub" in motive["RELV"]
    assert "fara istoric" in motive["NOUA"]


def test_candidatul_are_valorile_corecte_si_stirile():
    candidati, _ = scaneaza(ACUM, client=scenariu())
    buna, bunb = candidati[1], candidati[0]
    assert buna.gap_pct == pytest.approx(15.0)
    assert buna.pret == 11.5
    assert buna.volum_relativ == pytest.approx(20.0)
    assert buna.float_actiuni is None
    assert buna.stiri == [{"ora": "11-16 07:05 ET", "titlu": "BUNA wins FDA approval"}]
    assert bunb.stiri == []


def test_datele_cerute_nu_depasesc_limita_de_intarziere():
    client = scenariu()
    scaneaza(ACUM, client=client)
    for cale, params in client.cereri:
        if cale == "/v2/stocks/bars":
            assert datetime.fromisoformat(params["end"]) <= LIMITA
            assert params["feed"] == "sip"


def test_plafon_de_candidati(monkeypatch):
    monkeypatch.setattr(gap_scanner, "MAX_CANDIDATI", 1)
    candidati, respinsi = scaneaza(ACUM, client=scenariu())
    assert [c.simbol for c in candidati] == ["BUNB"]
    assert any(r["simbol"] == "BUNA" and "primilor" in r["motiv"] for r in respinsi)


def test_istoricul_de_volum_se_cere_doar_pentru_cei_ramasi():
    client = scenariu()
    scaneaza(ACUM, client=client)
    cereri_5m = [p for c, p in client.cereri if c == "/v2/stocks/bars" and p["timeframe"] == "5Min"]
    assert len(cereri_5m) == 2
    simboluri_istoric = set(cereri_5m[1]["symbols"].split(","))
    assert simboluri_istoric == {"BUNA", "BUNB", "RELV", "NOUA"}


# ---------------------------------------------------------------- integrarea (gap_shadow)

class ClaudeFals:
    def __init__(self):
        from types import SimpleNamespace as NS
        self.NS = NS
        self.beta = NS(messages=NS(create=self._create))

    def _create(self, **kw):
        self.cereri = getattr(self, "cereri", []) + [kw]
        # mesajul v1 continua cu <headlines>, cel v2 cu <news>
        simbol = json.loads(kw["messages"][0]["content"].split("Candidate:\n")[1]
                            .split("\n\n<")[0])["symbol"]
        text = json.dumps({"symbol": simbol, "verdict": "tradeable" if simbol == "BUNA" else "avoid",
                           "catalyst": "x", "confidence": 0.7, "size_factor": 0.5, "reason": "r"})
        return self.NS(content=[self.NS(type="text", text=text)], stop_reason="end_turn",
                       model="m", usage=self.NS(to_dict=lambda: {}))


def test_rulare_completa_scrie_jurnalul(jurnal_temporar):
    r = gap_shadow.ruleaza_analiza(ACUM, client_date=scenariu(), client_claude=ClaudeFals())
    assert r["eroare"] is None
    assert [(v["simbol"], v["verdict"], v["factor_marime"]) for v in r["verdicte"]] == \
        [("BUNB", "avoid", 0.0), ("BUNA", "tradeable", 0.5)]
    linii = (jurnal_temporar / "scanari_2026-11-16.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(linii) == 1
    inregistrare = json.loads(linii[0])
    # universul implicit e reconstruit din bare: pretul/gap-ul/volumul mic sunt eliminate
    # deja acolo, deci in scanner raman doar motivele din etapa a doua
    assert inregistrare["univers"] == 4
    assert inregistrare["respinsi_pe_motiv"] == {"volum relativ sub 2.0": 1, "fara istoric de volum": 1}


def test_universul_implicit_e_cel_reconstruit_nu_movers():
    client = scenariu()
    gap_shadow.ruleaza_analiza(ACUM, cu_claude=False, client_date=client)
    assert not any("movers" in c or "most-actives" in c for c, _ in client.cereri)


def test_esecul_scannerului_nu_opreste_programul(jurnal_temporar):
    client = scenariu()
    client.eroare = ConnectionError("Alpaca indisponibil")
    r = gap_shadow.ruleaza_analiza(ACUM, client_date=client, client_claude=ClaudeFals())
    assert r["candidati"] == [] and r["verdicte"] == []
    assert "Alpaca indisponibil" in r["eroare"]
    assert (jurnal_temporar / "scanari_2026-11-16.jsonl").exists()


def test_fara_claude_nu_se_trimite_niciun_apel():
    claude = ClaudeFals()
    claude.beta.messages.create = lambda **kw: (_ for _ in ()).throw(AssertionError("apel API"))
    r = gap_shadow.ruleaza_analiza(ACUM, cu_claude=False, client_date=scenariu(),
                                   client_claude=claude)
    assert len(r["candidati"]) == 2 and r["verdicte"] == []
