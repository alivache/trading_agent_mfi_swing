"""Teste pentru analyst.py — pasul 1: apelul si validarea.

Niciun test nu cheama API-ul Anthropic: folosim un client fals care intoarce
raspunsuri construite de mana (sau arunca erori), ca sa verificam ce face codul
cu fiecare tip de raspuns.
"""
import json
from datetime import datetime
from types import SimpleNamespace

import anthropic
import pytest

import analyst
from analyst import (NY, Candidat, Verdict, analizeaza_candidat, analizeaza_candidati,
                     construieste_mesaj, factor_marime, valideaza_raspuns)

ACUM = datetime(2026, 11, 16, 9, 0, tzinfo=NY)


@pytest.fixture(autouse=True)
def jurnal_temporar(tmp_path, monkeypatch):
    """Fiecare test scrie jurnalul si contorul intr-un director temporar."""
    monkeypatch.setattr(analyst, "DIR_JURNAL", str(tmp_path))
    monkeypatch.setattr(analyst, "FISIER_CONTOR", str(tmp_path / "contor_apeluri.json"))
    return tmp_path


def candidat(simbol="ABCD", stiri=None):
    return Candidat(simbol=simbol, gap_pct=14.2, volum_relativ=6.5, pret=8.40,
                    stiri=stiri if stiri is not None else
                    [{"ora": "07:02", "titlu": "ABCD wins FDA approval for lead drug"}])


def json_raspuns(**suprascrie):
    d = {"symbol": "ABCD", "verdict": "tradeable", "catalyst": "FDA approval",
         "confidence": 0.8, "size_factor": 0.6, "reason": "Clear binary catalyst."}
    d.update(suprascrie)
    return json.dumps(d)


class ClientFals:
    """Imita client.beta.messages.create. Tine minte argumentele primite."""

    def __init__(self, text=None, stop_reason="end_turn", eroare=None):
        self.text, self.stop_reason, self.eroare = text, stop_reason, eroare
        self.apeluri = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.apeluri.append(kwargs)
        if self.eroare is not None:
            raise self.eroare
        continut = [] if self.text is None else [SimpleNamespace(type="text", text=self.text)]
        return SimpleNamespace(content=continut, stop_reason=self.stop_reason,
                               model="claude-sonnet-5-5",
                               usage=SimpleNamespace(to_dict=lambda: {"input_tokens": 900,
                                                                      "output_tokens": 120}))


def linii_jurnal(director):
    cale = director / "2026-11-16.jsonl"
    return [json.loads(l) for l in cale.read_text(encoding="utf-8").splitlines()]


# ---------------------------------------------------------------- raspunsuri valide

def test_raspuns_valid_tradeable():
    client = ClientFals(json_raspuns())
    v = analizeaza_candidat(candidat(), client=client, acum=ACUM)
    assert v.valid and v.verdict == "tradeable"
    assert v.catalyst == "FDA approval"
    assert factor_marime(v) == 0.6


def test_cererea_trimisa_contine_ce_trebuie():
    client = ClientFals(json_raspuns())
    analizeaza_candidat(candidat(), client=client, acum=ACUM)
    cerere = client.apeluri[0]
    assert cerere["model"] == "claude-sonnet-5-5"
    assert cerere["system"] == analyst.SYSTEM_PROMPT
    assert cerere["output_config"]["format"]["schema"] == analyst.SCHEMA
    mesaj = cerere["messages"][0]["content"]
    assert '"symbol": "ABCD"' in mesaj
    assert "<headlines>" in mesaj and "FDA approval" in mesaj
    assert "2026-11-16 09:00 ET" in mesaj


def test_avoid_cu_marime_pozitiva_devine_marime_zero():
    v = valideaza_raspuns(json_raspuns(verdict="avoid", size_factor=0.7), "ABCD")
    assert v.valid and v.verdict == "avoid"
    assert v.size_factor == 0.0
    assert factor_marime(v) == 0.0


def test_simbolul_se_compara_fara_majuscule_si_spatii():
    v = valideaza_raspuns(json_raspuns(symbol=" abcd "), "ABCD")
    assert v.valid


# ---------------------------------------------------------------- raspunsuri invalide -> avoid

@pytest.mark.parametrize("text, motiv", [
    ("nu e json", "JSON valid"),
    ("[1, 2]", "obiect JSON"),
    (json.dumps({"symbol": "ABCD", "verdict": "tradeable"}), "campuri lipsa"),
    (json_raspuns(symbol="XYZ"), "simbol gresit"),
    (json_raspuns(verdict="buy"), "verdict necunoscut"),
    (json_raspuns(size_factor=1.5), "size_factor"),
    (json_raspuns(size_factor=-0.1), "size_factor"),
    (json_raspuns(size_factor=True), "size_factor"),
    (json_raspuns(size_factor="0.5"), "size_factor"),
    (json_raspuns(confidence=2), "confidence"),
    (json_raspuns(reason=None), "reason"),
])
def test_raspuns_invalid_devine_avoid(text, motiv):
    v = analizeaza_candidat(candidat(), client=ClientFals(text), acum=ACUM)
    assert not v.valid and v.verdict == "avoid"
    assert motiv in v.eroare
    assert factor_marime(v) == 0.0


@pytest.mark.parametrize("stop_reason, motiv", [
    ("refusal", "refuzat"),
    ("max_tokens", "trunchiat"),
])
def test_refuz_sau_trunchiere_devine_avoid(stop_reason, motiv):
    # chiar daca textul ar fi valid, nu avem incredere intr-un raspuns oprit anormal
    v = analizeaza_candidat(candidat(), client=ClientFals(json_raspuns(), stop_reason=stop_reason),
                            acum=ACUM)
    assert not v.valid and motiv in v.eroare


def test_raspuns_fara_text_devine_avoid():
    v = analizeaza_candidat(candidat(), client=ClientFals(text=None), acum=ACUM)
    assert not v.valid and "fara text" in v.eroare


@pytest.mark.parametrize("eroare, motiv", [
    (anthropic.APITimeoutError(request=None), "timeout"),
    (anthropic.APIConnectionError(request=None), "conexiune"),
    (anthropic.APIStatusError("supraincarcat", body=None,
                              response=SimpleNamespace(status_code=529, headers={},
                                                       request=None)), "529"),
    (RuntimeError("bug"), "neasteptata"),
])
def test_erori_de_apel_devin_avoid_fara_exceptie(eroare, motiv):
    v = analizeaza_candidat(candidat(), client=ClientFals(eroare=eroare), acum=ACUM)
    assert not v.valid and v.verdict == "avoid"
    assert motiv in v.eroare


def test_factor_marime_nu_depaseste_niciodata_1():
    # chiar daca cineva construieste un Verdict gresit de mana, plafonul tine
    v = Verdict(simbol="ABCD", verdict="tradeable", size_factor=5.0, valid=True)
    assert factor_marime(v) == 1.0


# ---------------------------------------------------------------- plafon zilnic

def test_plafon_zilnic_opreste_apelurile(monkeypatch):
    monkeypatch.setattr(analyst, "MAX_APELURI_PE_ZI", 2)
    client = ClientFals(json_raspuns())
    verdicte = [analizeaza_candidat(candidat(), client=client, acum=ACUM) for _ in range(3)]
    assert [v.valid for v in verdicte] == [True, True, False]
    assert "plafon zilnic" in verdicte[2].eroare
    assert len(client.apeluri) == 2          # al treilea nu a ajuns la API


def test_plafonul_se_reseteaza_a_doua_zi(monkeypatch):
    monkeypatch.setattr(analyst, "MAX_APELURI_PE_ZI", 1)
    client = ClientFals(json_raspuns())
    analizeaza_candidat(candidat(), client=client, acum=ACUM)
    maine = datetime(2026, 11, 17, 9, 0, tzinfo=NY)
    assert analizeaza_candidat(candidat(), client=client, acum=maine).valid


# ---------------------------------------------------------------- jurnal

def test_fiecare_apel_e_jurnalizat(jurnal_temporar):
    analizeaza_candidat(candidat(), client=ClientFals(json_raspuns()), acum=ACUM)
    analizeaza_candidat(candidat(), client=ClientFals("stricat"), acum=ACUM)
    linii = linii_jurnal(jurnal_temporar)
    assert len(linii) == 2
    assert linii[0]["apel_trimis"] and linii[0]["verdict"]["verdict"] == "tradeable"
    assert linii[0]["raspuns_text"] == json_raspuns()
    assert linii[0]["usage"]["input_tokens"] == 900
    assert linii[0]["candidat"]["simbol"] == "ABCD"
    assert linii[1]["raspuns_text"] == "stricat"
    assert not linii[1]["verdict"]["valid"]


def test_apelul_sarit_din_plafon_e_si_el_jurnalizat(jurnal_temporar, monkeypatch):
    monkeypatch.setattr(analyst, "MAX_APELURI_PE_ZI", 0)
    analizeaza_candidat(candidat(), client=ClientFals(json_raspuns()), acum=ACUM)
    (linie,) = linii_jurnal(jurnal_temporar)
    assert linie["apel_trimis"] is False


# ---------------------------------------------------------------- mesaj si lot

def test_mesaj_fara_stiri_spune_explicit():
    mesaj = construieste_mesaj(candidat(stiri=[]), ACUM)
    assert "no headlines" in mesaj


def test_mesaj_limiteaza_numarul_de_stiri():
    stiri = [{"ora": "06:00", "titlu": f"titlu {i}"} for i in range(40)]
    mesaj = construieste_mesaj(candidat(stiri=stiri), ACUM)
    assert mesaj.count("titlu ") == analyst.MAX_STIRI


def test_lot_pastreaza_ordinea_si_izoleaza_esecurile():
    raspunsuri = iter([json_raspuns(), "stricat", json_raspuns(symbol="EFGH")])
    client = ClientFals()
    client.beta.messages.create = lambda **kw: SimpleNamespace(
        content=[SimpleNamespace(type="text", text=next(raspunsuri))], stop_reason="end_turn",
        model="m", usage=SimpleNamespace(to_dict=lambda: {}))
    verdicte = analizeaza_candidati([candidat("ABCD"), candidat("XXXX"), candidat("EFGH")],
                                    client=client, acum=ACUM)
    assert [v.simbol for v in verdicte] == ["ABCD", "XXXX", "EFGH"]
    assert [v.valid for v in verdicte] == [True, False, True]


def test_clientul_real_are_timeout_30s_si_fara_reincercari(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    c = analyst.creeaza_client()
    assert c.timeout == 30
    assert c.max_retries == 0
