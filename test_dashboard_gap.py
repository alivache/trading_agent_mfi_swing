"""Teste pentru dashboard_gap.py: paginile se incarca din jurnale si nu au incredere in ele."""
import json
from datetime import datetime

import pytest

import analyst
import dashboard_gap
from analyst import NY

ZI = "2026-11-16"


def scrie(director, nume, linii):
    director.mkdir(parents=True, exist_ok=True)
    (director / nume).write_text("".join(json.dumps(l) + "\n" for l in linii), encoding="utf-8")


def candidat(simbol, stiri=None):
    return {"simbol": simbol, "gap_pct": 12.0, "volum_relativ": 5.0, "pret": 10.0,
            "float_actiuni": None, "stiri": stiri or []}


def verdict(simbol, v="tradeable", factor=0.5, valid=True):
    return {"simbol": simbol, "verdict": v, "catalyst": f"catalizator {simbol}", "confidence": 0.7,
            "size_factor": factor, "reason": f"motiv {simbol}", "valid": valid, "eroare": None,
            "factor_marime": factor if v == "tradeable" and valid else 0.0}


def evaluare(simbol, zi, v, exces, valid=True):
    return {"zi": zi, "simbol": simbol, "verdict": v, "valid": valid, "factor_marime": 0.5,
            "randament_pct": exces + 1, "exces_pct": exces, "mfe_pct": 3.0, "mae_pct": -2.0,
            "gap_umplut": False, "nr_stiri": 1}


@pytest.fixture
def jurnale(tmp_path, monkeypatch):
    live, replay = tmp_path / "live", tmp_path / "replay"
    monkeypatch.setattr(dashboard_gap, "DIR_LIVE", str(live))
    monkeypatch.setattr(dashboard_gap, "DIR_REPLAY", str(replay))
    monkeypatch.setattr(dashboard_gap, "SURSE", {"live": str(live), "replay": str(replay)})
    monkeypatch.setattr(dashboard_gap, "stare_serviciu", lambda nume="gap-shadow": "active")
    return live, replay


@pytest.fixture
def client():
    return dashboard_gap.app.test_client()


def test_pagina_goala_inainte_de_prima_scanare(jurnale, client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Nicio scanare încă" in r.get_data(as_text=True)


def test_ziua_arata_candidatii_verdictele_si_costul(jurnale, client):
    live, _ = jurnale
    scrie(live, f"scanari_{ZI}.jsonl", [{
        "acum": f"{ZI}T09:00:00-05:00", "univers": 23, "eroare": None,
        "candidati": [candidat("ABCD", [{"ora": "11-16 07:05 ET", "titlu": "ABCD wins FDA approval"}]),
                      candidat("EFGH")],
        "verdicte": [verdict("ABCD"), verdict("EFGH", "avoid", 0.0)],
        "respinsi_pe_motiv": {"gap sub 5.0%": 7},
    }])
    scrie(live, f"{ZI}.jsonl", [
        {"apel_trimis": True, "usage": {"input_tokens": 1000, "output_tokens": 200}},
        {"apel_trimis": True, "usage": {"input_tokens": 1000, "output_tokens": 200}},
        {"apel_trimis": False},
    ])
    html = client.get("/").get_data(as_text=True)
    assert "ABCD" in html and "EFGH" in html
    assert "catalizator ABCD" in html and "motiv ABCD" in html
    assert "ABCD wins FDA approval" in html
    assert "se evaluează după închidere" in html
    assert "gap sub 5.0%" in html
    # 2 apeluri x (1000 in x $2/M + 200 out x $10/M) = $0.008
    assert "$0.008" in html


def test_rezultatul_apare_dupa_evaluare(jurnale, client):
    live, _ = jurnale
    scrie(live, f"scanari_{ZI}.jsonl", [{"acum": ZI, "eroare": None, "candidati": [candidat("ABCD")],
                                        "verdicte": [verdict("ABCD")]}])
    scrie(live, f"evaluare_{ZI}.jsonl", [evaluare("ABCD", ZI, "tradeable", 4.25)])
    html = client.get(f"/zi/{ZI}").get_data(as_text=True)
    assert "+4.25%" in html and "+5.25%" in html


def test_titlurile_si_motivele_sunt_escapate(jurnale, client):
    """Titlurile de stiri vin de la terti: un <script> in titlu nu trebuie sa ruleze."""
    live, _ = jurnale
    rau = "<script>alert('x')</script>"
    v = verdict("ABCD")
    v["reason"] = rau
    scrie(live, f"scanari_{ZI}.jsonl", [{"acum": ZI, "eroare": None,
                                        "candidati": [candidat("ABCD", [{"ora": "x", "titlu": rau}])],
                                        "verdicte": [v]}])
    html = client.get("/").get_data(as_text=True)
    assert rau not in html
    assert "&lt;script&gt;" in html


def test_scanare_esuata_se_vede(jurnale, client):
    live, _ = jurnale
    scrie(live, f"scanari_{ZI}.jsonl", [{"acum": ZI, "eroare": "scanner: ConnectionError: x",
                                        "candidati": [], "verdicte": []}])
    assert "Scanarea a eșuat" in client.get("/").get_data(as_text=True)


def test_linie_stricata_in_jurnal_nu_strica_pagina(jurnale, client):
    live, _ = jurnale
    live.mkdir(parents=True)
    (live / f"scanari_{ZI}.jsonl").write_text(
        json.dumps({"acum": ZI, "eroare": None, "candidati": [candidat("ABCD")], "verdicte": []})
        + "\n{\"acum\": \"jumatate", encoding="utf-8")
    r = client.get("/")
    assert r.status_code == 200 and "ABCD" in r.get_data(as_text=True)


def test_zi_cu_format_gresit_da_404(jurnale, client):
    assert client.get("/zi/../../etc").status_code == 404
    assert client.get("/zi/2026-1-1").status_code == 404
    assert client.get("/rezultate?sursa=altceva").status_code == 404


def test_rezultate_replay_si_live_sunt_separate(jurnale, client):
    _, replay = jurnale
    scrie(replay, "evaluare_2026-08-03.jsonl", [evaluare("ABCD", "2026-08-03", "tradeable", 2.0),
                                                evaluare("EFGH", "2026-08-03", "avoid", -1.0)])
    html_replay = client.get("/rezultate?sursa=replay").get_data(as_text=True)
    assert "1 zile, 2 candidați" in html_replay and "Plotly.newPlot" in html_replay
    assert "Nicio evaluare încă" in client.get("/rezultate?sursa=live").get_data(as_text=True)


def test_serie_cumulata_media_zilnica_adunata():
    e = [evaluare("A", "2026-08-03", "tradeable", 2.0), evaluare("B", "2026-08-03", "tradeable", 4.0),
         evaluare("C", "2026-08-03", "avoid", -1.0),
         evaluare("D", "2026-08-04", "avoid", 3.0)]          # nicio tradeable pe 4 aug -> +0
    s = dashboard_gap.serie_cumulata(e)
    assert s["zile"] == ["2026-08-03", "2026-08-04"]
    assert s["tradeable"] == [3.0, 3.0]
    assert s["avoid"] == [-1.0, 2.0]
    assert s["toti"] == [pytest.approx(5 / 3, abs=1e-3), pytest.approx(5 / 3 + 3, abs=1e-3)]


def test_urmatoarea_analiza_sare_weekendul():
    vineri_seara = datetime(2026, 11, 13, 17, 0, tzinfo=NY)
    assert dashboard_gap.urmatoarea_analiza(vineri_seara) == datetime(2026, 11, 16, 9, 0, tzinfo=NY)
    luni_dimineata = datetime(2026, 11, 16, 8, 0, tzinfo=NY)
    assert dashboard_gap.urmatoarea_analiza(luni_dimineata) == datetime(2026, 11, 16, 9, 0, tzinfo=NY)


def test_api_stare(jurnale, client):
    j = client.get("/api/stare").get_json()
    assert j["serviciu"] == "active" and j["ultima_zi"] is None
