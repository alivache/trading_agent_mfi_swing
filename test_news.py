"""Teste pentru news.py si sec_edgar.py (pasii 1, 2, 3 si 4 din specificatia de stiri).

Fara retea: clienti falsi pentru Alpaca si SEC.
"""
from datetime import datetime, timedelta, timezone

import pytest

import news
import sec_edgar
from analyst import NY
from news import get_news, get_news_lot, in_fereastra, normalizeaza_alpaca

AS_OF = datetime(2026, 11, 16, 9, 0, tzinfo=NY)          # 14:00 UTC
AS_OF_UTC = AS_OF.astimezone(timezone.utc)


@pytest.fixture(autouse=True)
def cache_temporar(tmp_path, monkeypatch):
    monkeypatch.setattr(news, "DIR_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(sec_edgar, "DIR_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(sec_edgar, "_harta_cik", None)
    monkeypatch.setattr(sec_edgar, "_harta_zi", None)


def stire(titlu, ore_inainte, simboluri=("ABCD",), summary="rezumat", minute=0):
    t = AS_OF_UTC - timedelta(hours=ore_inainte, minutes=minute)
    return {"headline": titlu, "summary": summary, "created_at": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "updated_at": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "url": "https://x", "source": "benzinga",
            "symbols": list(simboluri)}


class AlpacaFals:
    def __init__(self, stiri=(), eroare=None):
        self.lista, self.eroare, self.apeluri = list(stiri), eroare, []

    def stiri(self, simboluri, start, end):
        self.apeluri.append((list(simboluri), start, end))
        if self.eroare:
            raise self.eroare
        return self.lista


class SecFals:
    """Raspunsuri SEC construite de mana: harta de tickere + submissions pe coloane."""

    def __init__(self, depuneri=None, eroare=None):
        self.depuneri = depuneri or {}       # cik -> lista de (forma, ore_inainte, iteme, descriere)
        self.eroare, self.cereri = eroare, []

    def get_json(self, url):
        self.cereri.append(url)
        if self.eroare:
            raise self.eroare
        if url == sec_edgar.URL_TICKERE:
            return {"0": {"cik_str": 111, "ticker": "ABCD", "title": "Abcd Inc"},
                    "1": {"cik_str": 222, "ticker": "EFGH", "title": "Efgh Corp"}}
        cik = int(url.split("CIK")[1].split(".")[0])
        lista = self.depuneri.get(cik, [])
        col = {"form": [], "acceptanceDateTime": [], "accessionNumber": [], "primaryDocument": [],
               "items": [], "primaryDocDescription": []}
        for n, (forma, ore, iteme, descr) in enumerate(lista):
            t = AS_OF_UTC - timedelta(hours=ore)
            col["form"].append(forma)
            col["acceptanceDateTime"].append(t.strftime("%Y-%m-%dT%H:%M:%S.000Z"))
            col["accessionNumber"].append(f"0001-26-{n:06d}")
            col["primaryDocument"].append("doc.htm")
            col["items"].append(iteme)
            col["primaryDocDescription"].append(descr)
        return {"cik": str(cik), "filings": {"recent": col}}


# ---------------------------------------------------------------- pasul 1: Alpaca + normalizare

def test_normalizare_alpaca():
    i = normalizeaza_alpaca(stire("ABCD wins FDA approval", 2, summary="x" * 900))
    assert i["source"] == "alpaca/benzinga" and i["type"] == "news"
    assert i["headline"] == "ABCD wins FDA approval"
    assert len(i["summary"]) == news.MAX_SUMAR and i["summary"].endswith("…")
    assert i["published_at"] == "2026-11-16T12:00:00+00:00"       # UTC


def test_get_news_doar_cu_alpaca():
    r = get_news("ABCD", AS_OF, client_alpaca=AlpacaFals([stire("veche", 5), stire("noua", 1)]),
                 cu_filings=False)
    assert r.stare_stiri == "ok" and r.stare_filings == "dezactivat"
    assert [i["headline"] for i in r.items] == ["noua", "veche"]      # cele mai noi primele


def test_stirea_cu_mai_multe_simboluri_ajunge_la_fiecare():
    alp = AlpacaFals([stire("comuna", 1, simboluri=("ABCD", "EFGH")), stire("doar efgh", 2, ("EFGH",))])
    r = get_news_lot(["ABCD", "EFGH"], AS_OF, client_alpaca=alp, cu_filings=False)
    assert [i["headline"] for i in r["ABCD"].items] == ["comuna"]
    assert [i["headline"] for i in r["EFGH"].items] == ["comuna", "doar efgh"]
    assert len(alp.apeluri) == 1                                     # un singur apel pentru lot


# ---------------------------------------------------------------- pasul 2: as_of strict, fereastra, dedup, cache

def test_nu_intoarce_niciodata_ceva_dupa_as_of():
    # API-ul ar trebui sa filtreze singur, dar nu ne bazam pe asta
    viitor = stire("din viitor", -1)                 # publicata la o ora DUPA as_of
    r = get_news("ABCD", AS_OF, client_alpaca=AlpacaFals([viitor, stire("buna", 1)]), cu_filings=False)
    assert [i["headline"] for i in r.items] == ["buna"]


def test_fereastra_de_24h_inclusiv_capetele():
    items = [normalizeaza_alpaca(stire(t, o)) for t, o in
             [("exact as_of", 0), ("exact 24h", 24), ("peste 24h", 24.01)]]
    assert [i["headline"] for i in in_fereastra(items, AS_OF)] == ["exact as_of", "exact 24h"]


def test_deduplicare_dupa_titlu_normalizat():
    items = [normalizeaza_alpaca(stire("ABCD Wins FDA Approval!", 1)),
             normalizeaza_alpaca(stire("abcd wins fda approval", 2)),
             normalizeaza_alpaca(stire("Alt titlu", 3))]
    assert [i["headline"] for i in in_fereastra(items, AS_OF)] == ["ABCD Wins FDA Approval!", "Alt titlu"]


def test_maxim_10_stiri():
    alp = AlpacaFals([stire(f"titlu {n}", 1, minute=n) for n in range(25)])
    r = get_news("ABCD", AS_OF, client_alpaca=alp, cu_filings=False)
    assert len(r.items) == news.MAX_STIRI
    assert r.items[0]["headline"] == "titlu 0"


def test_plafonul_de_10_e_total_cu_prioritate_pentru_documentele_sec():
    sec = SecFals({111: [("424B5", 20, "", "Prospectus"), ("8-K", 21, "2.02", "")]})
    alp = AlpacaFals([stire(f"stire {n}", 1, minute=n) for n in range(15)])
    r = get_news("ABCD", AS_OF, client_alpaca=alp, client_sec=sec)
    assert len(r.items) == news.MAX_STIRI
    assert sum(i["type"] == "filing" for i in r.items) == 2      # documentele mai vechi raman
    assert r.items[-1]["type"] == "filing"                        # tot cronologic


def test_entitatile_html_sunt_decodate():
    i = normalizeaza_alpaca(stire("S&amp;P futures", 1, summary="Thursday&#39;s close"))
    assert i["headline"] == "S&P futures" and i["summary"] == "Thursday's close"


def test_cache_pe_simbol_si_ora_evita_apelul_al_doilea():
    alp = AlpacaFals([stire("o stire", 1)])
    get_news("ABCD", AS_OF, client_alpaca=alp, cu_filings=False)
    r2 = get_news("ABCD", AS_OF, client_alpaca=alp, cu_filings=False)
    assert len(alp.apeluri) == 1 and r2.items[0]["headline"] == "o stire"
    # alta ora din aceeasi zi = alta intrare in cache
    get_news("ABCD", AS_OF + timedelta(hours=3), client_alpaca=alp, cu_filings=False)
    assert len(alp.apeluri) == 2


def test_as_of_fara_fus_orar_e_refuzat():
    with pytest.raises(ValueError):
        get_news("ABCD", datetime(2026, 11, 16, 9, 0), client_alpaca=AlpacaFals(), cu_filings=False)


# ---------------------------------------------------------------- pasul 3: esecuri

def test_alpaca_cazut_e_news_unavailable_nu_lista_goala():
    r = get_news("ABCD", AS_OF, client_alpaca=AlpacaFals(eroare=TimeoutError("10 s")), cu_filings=False)
    assert r.stare_stiri == "news_unavailable" and r.items == []
    assert "TimeoutError" in r.erori[0]


def test_fara_stiri_e_ok_cu_lista_goala():
    r = get_news("ABCD", AS_OF, client_alpaca=AlpacaFals([]), cu_filings=False)
    assert r.stare_stiri == "ok" and r.items == []


def test_esecul_nu_se_pune_in_cache():
    get_news("ABCD", AS_OF, client_alpaca=AlpacaFals(eroare=ConnectionError("x")), cu_filings=False)
    r = get_news("ABCD", AS_OF, client_alpaca=AlpacaFals([stire("revenit", 1)]), cu_filings=False)
    assert r.stare_stiri == "ok" and r.items[0]["headline"] == "revenit"


# ---------------------------------------------------------------- pasul 4: EDGAR

def test_filings_relevante_din_fereastra():
    sec = SecFals({111: [("8-K", 3, "2.02,9.01", "Current report"),
                         ("4", 2, "", "insider"),                      # ignorat: tranzactie insider
                         ("424B5", 1, "", "Prospectus supplement"),
                         ("10-Q", 30, "", "Quarterly report")]})       # in afara ferestrei
    r = get_news("ABCD", AS_OF, client_alpaca=AlpacaFals([]), client_sec=sec)
    assert r.stare_filings == "ok"
    titluri = [i["headline"] for i in r.items]
    assert titluri == ["SEC filing 424B5 (registration/prospectus: possible share offering)",
                       "SEC filing 8-K - results of operations (earnings); financial statements and exhibits"]
    assert all(i["type"] == "filing" and i["source"] == "sec/edgar" for i in r.items)
    assert r.items[0]["url"].startswith("https://www.sec.gov/Archives/edgar/data/111/")


def test_item_3_02_marcat_ca_diluare():
    sec = SecFals({111: [("8-K", 1, "3.02", "")]})
    r = get_news("ABCD", AS_OF, client_alpaca=AlpacaFals([]), client_sec=sec)
    assert "dilution" in r.items[0]["headline"]


def test_filing_dupa_as_of_e_ignorat():
    sec = SecFals({111: [("8-K", -2, "8.01", "")]})
    r = get_news("ABCD", AS_OF, client_alpaca=AlpacaFals([]), client_sec=sec)
    assert r.items == []


def test_simbol_necunoscut_la_sec():
    r = get_news("ZZZZ", AS_OF, client_alpaca=AlpacaFals([]), client_sec=SecFals())
    assert r.stare_filings == "fara_cik"


def test_sec_cazut_continua_cu_stirile_si_noteaza():
    r = get_news("ABCD", AS_OF, client_alpaca=AlpacaFals([stire("stire", 1)]),
                 client_sec=SecFals(eroare=ConnectionError("sec jos")))
    assert r.stare_stiri == "ok" and r.stare_filings == "filings_unavailable"
    assert [i["headline"] for i in r.items] == ["stire"]
    assert any("sec jos" in e for e in r.erori)


def test_stiri_si_filings_amestecate_cronologic():
    sec = SecFals({111: [("8-K", 2, "1.01", "")]})
    r = get_news("ABCD", AS_OF, client_alpaca=AlpacaFals([stire("noua", 1), stire("veche", 3)]),
                 client_sec=sec)
    assert [i["type"] for i in r.items] == ["news", "filing", "news"]


def test_harta_cik_se_descarca_o_data(monkeypatch):
    sec = SecFals()
    get_news_lot(["ABCD", "EFGH"], AS_OF, client_alpaca=AlpacaFals([]), client_sec=sec)
    assert sec.cereri.count(sec_edgar.URL_TICKERE) == 1


def test_fara_user_agent_clientul_sec_refuza(monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    with pytest.raises(RuntimeError, match="SEC_USER_AGENT"):
        sec_edgar.ClientSec()
