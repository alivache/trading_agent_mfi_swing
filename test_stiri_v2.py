"""Teste pentru pasul 5 din specificatia de stiri: integrarea news.py cu analistul (versiunea 2),
replay-ul comparativ v1/v2 si scriptul stiri_analist.py. Fara retea."""
import json
import os
from datetime import date
from types import SimpleNamespace

import pytest

import analyst
import gap_scanner
import news
import replay_analist
import sec_edgar
import stiri_analist
from analyst import Candidat, construieste_mesaj, system_prompt
from test_gap_scanner import ACUM, ClaudeFals, jurnal_temporar, scenariu  # noqa: F401
from test_news import AlpacaFals, SecFals, stire
from test_replay import client_interval


@pytest.fixture(autouse=True)
def directoare_temporare(tmp_path, monkeypatch):
    monkeypatch.setattr(news, "DIR_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(sec_edgar, "DIR_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(sec_edgar, "_harta_cik", None)
    monkeypatch.setattr(replay_analist, "DIR_REPLAY", str(tmp_path / "replay"))
    monkeypatch.setattr(replay_analist, "DIR_REPLAY_V2", str(tmp_path / "replay_v2"))
    monkeypatch.setattr(stiri_analist, "DIR_TEST", str(tmp_path / "test_stiri"))


def rezultat_stiri(items=(), stare_stiri="ok", stare_filings="ok"):
    return {"simbol": "ABCD", "as_of": "x", "items": list(items), "stare_stiri": stare_stiri,
            "stare_filings": stare_filings, "erori": []}


def item(tip="news", titlu="ABCD wins FDA approval", form=None):
    i = {"source": "alpaca/benzinga" if tip == "news" else "sec/edgar", "headline": titlu,
         "summary": "un rezumat", "published_at": "2026-11-16T12:00:00+00:00", "url": "", "type": tip}
    if form:
        i["form"] = form
    return i


def cand(stiri_v2=None):
    return Candidat(simbol="ABCD", gap_pct=12.0, volum_relativ=5.0, pret=10.0, stiri_v2=stiri_v2)


# ---------------------------------------------------------------- mesajul si promptul v2

def test_v1_ramane_neschimbat():
    c = cand()
    assert analyst.versiune(c) == "v1"
    assert system_prompt(c) == analyst.SYSTEM_PROMPT
    assert "<headlines>" in construieste_mesaj(c, ACUM)


def test_promptul_v2_are_sectiunea_news_handling():
    p = analyst.SYSTEM_PROMPT_V2
    assert "News handling" in p and "cite the specific headline or filing" in p
    assert "<headlines>" not in p
    assert "Fields:" in p and "size_factor" in p          # restul promptului e pastrat


def test_mesaj_v2_cu_stiri_si_filing():
    c = cand(rezultat_stiri([item(), item("filing", "SEC filing 424B5 (possible share offering)", "424B5")]))
    m = construieste_mesaj(c, ACUM)
    assert system_prompt(c) == analyst.SYSTEM_PROMPT_V2
    assert "<news>" in m and "</news>" in m and "<headlines>" not in m
    assert "NEWS (alpaca/benzinga): ABCD wins FDA approval" in m
    assert "FILING 424B5 (sec/edgar): SEC filing 424B5" in m
    assert "un rezumat" in m
    assert "[11-16 07:00 ET]" in m


@pytest.mark.parametrize("stari, text", [
    (("news_unavailable", "ok"), "news_unavailable: the news source failed"),
    (("ok", "filings_unavailable"), "SEC filings could not be checked"),
    (("ok", "fara_cik"), "No SEC registrant"),
])
def test_mesaj_v2_spune_starea_surselor(stari, text):
    assert text in construieste_mesaj(cand(rezultat_stiri([], *stari)), ACUM)


def test_mesaj_v2_cautat_si_gol_e_diferit_de_indisponibil():
    gol = construieste_mesaj(cand(rezultat_stiri([])), ACUM)
    indisponibil = construieste_mesaj(cand(rezultat_stiri([], "news_unavailable")), ACUM)
    assert "searched: no news" in gol and "news_unavailable" not in gol
    assert "news_unavailable" in indisponibil and "searched: no news" not in indisponibil


def test_jurnalul_noteaza_versiunea(jurnal_temporar):
    claude = ClaudeFals()
    analyst.analizeaza_candidat(cand(rezultat_stiri([item()])), client=claude, acum=ACUM)
    (linie,) = [json.loads(l) for l in (jurnal_temporar / "2026-11-16.jsonl").read_text(encoding="utf-8").splitlines()]
    assert linie["versiune"] == "v2"
    assert claude.cereri[0]["system"] == analyst.SYSTEM_PROMPT_V2


# ---------------------------------------------------------------- scannerul pe v2

def test_scanner_v2_ataseaza_stirile_noi_si_nu_mai_cere_titlurile_vechi():
    client = scenariu()
    alp = AlpacaFals([stire("BUNA wins FDA approval", 2, simboluri=("BUNA",))])
    candidati, _ = gap_scanner.scaneaza(ACUM, client=client, versiune_stiri="v2",
                                        client_stiri=alp, client_sec=SecFals())
    buna = next(c for c in candidati if c.simbol == "BUNA")
    assert buna.stiri_v2["stare_stiri"] == "ok"
    assert buna.stiri_v2["items"][0]["headline"] == "BUNA wins FDA approval"
    assert buna.stiri == [{"ora": "11-16 07:00 ET", "titlu": "BUNA wins FDA approval"}]
    assert not any(c == "/v1beta1/news" for c, _ in client.cereri)


def test_scanner_v2_cu_alpaca_cazut_nu_pica_scanarea():
    candidati, _ = gap_scanner.scaneaza(ACUM, client=scenariu(), versiune_stiri="v2",
                                        client_stiri=AlpacaFals(eroare=ConnectionError("x")),
                                        client_sec=SecFals())
    assert candidati and all(c.stiri_v2["stare_stiri"] == "news_unavailable" for c in candidati)


# ---------------------------------------------------------------- replay comparativ v1 / v2

def test_replay_v2_foloseste_aceiasi_candidati_ca_v1():
    client = client_interval()
    assert replay_analist.main(["--zi", "2026-11-16"], client=client, client_claude=ClaudeFals(),
                               azi=date(2026, 11, 17)) == 0
    claude_v2 = ClaudeFals()
    cod = replay_analist.main(["--zi", "2026-11-16", "--versiune", "v2"], client=client_interval(),
                              client_claude=claude_v2, azi=date(2026, 11, 17),
                              client_stiri=AlpacaFals([]), client_sec=SecFals())
    assert cod == 0
    v1 = [json.loads(l) for l in open(os.path.join(replay_analist.DIR_REPLAY, "scanari_2026-11-16.jsonl"))]
    v2 = [json.loads(l) for l in open(os.path.join(replay_analist.DIR_REPLAY_V2, "scanari_2026-11-16.jsonl"))]
    assert [c["simbol"] for c in v2[-1]["candidati"]] == [c["simbol"] for c in v1[-1]["candidati"]]
    assert v2[-1]["versiune_stiri"] == "v2"
    assert all(k["system"] == analyst.SYSTEM_PROMPT_V2 for k in claude_v2.cereri)
    assert os.path.exists(os.path.join(replay_analist.DIR_REPLAY_V2, "evaluare_2026-11-16.jsonl"))


def test_replay_v2_fara_v1_sare_ziua(capsys):
    cod = replay_analist.main(["--zi", "2026-11-16", "--versiune", "v2"], client=client_interval(),
                              client_claude=ClaudeFals(), azi=date(2026, 11, 17),
                              client_stiri=AlpacaFals([]), client_sec=SecFals())
    assert cod == 1          # nicio zi cu scanare v1 -> nimic de comparat


# ---------------------------------------------------------------- scriptul stiri_analist

def test_stiri_analist_afiseaza_stirile_si_verdictul(capsys):
    cod = stiri_analist.main(["--data", "2026-11-16", "--simboluri", "BUNA,LIPS"],
                             client=scenariu(), client_claude=ClaudeFals(),
                             client_stiri=AlpacaFals([stire("BUNA wins FDA approval", 2, ("BUNA",))]),
                             client_sec=SecFals())
    iesire = capsys.readouterr().out
    assert cod == 0
    assert "BUNA  gap +15.0%" in iesire and "BUNA wins FDA approval" in iesire
    assert "TRADEABLE" in iesire
    assert "LIPS: fara date pre-market" in iesire


def test_stiri_analist_refuza_zile_contaminate():
    assert stiri_analist.main(["--data", "2026-05-04", "--simboluri", "BUNA"], client=scenariu(),
                              client_claude=ClaudeFals()) == 1
