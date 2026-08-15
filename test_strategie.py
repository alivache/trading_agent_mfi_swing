"""Teste pentru logica pura din multi_tf_strategy.

Acopera zona modificata cel mai des: conditiile de iesire, indicatorii si
persistenta. Nu se face niciun apel catre Alpaca.
"""
import os
from datetime import datetime, timedelta

import pytest

import multi_tf_strategy as m


def poz(pret_intrare=100.0, pret_max=None, trailing_activ=False, cantitate=10):
    return {
        "pret_intrare": pret_intrare,
        "pret_max": pret_intrare if pret_max is None else pret_max,
        "trailing_activ": trailing_activ,
        "cantitate": cantitate,
    }


# ─────────────────────────────────────────────────────────────
# verifica_iesire — cele 5 conditii
# ─────────────────────────────────────────────────────────────
def test_fara_iesire_cand_nimic_nu_se_declanseaza():
    iesire, motiv = m.verifica_iesire("X", poz(), 100.5, ema9=11, ema21=10, rsi_5m=50)
    assert iesire is False
    assert motiv is None


def test_stop_loss_la_minus_1_5_la_suta():
    iesire, motiv = m.verifica_iesire("X", poz(), 98.5, ema9=11, ema21=10, rsi_5m=50)
    assert iesire is True
    assert "STOP LOSS" in motiv


def test_stop_loss_nu_se_declanseaza_cu_o_fractiune_inainte():
    iesire, _ = m.verifica_iesire("X", poz(), 98.6, ema9=11, ema21=10, rsi_5m=50)
    assert iesire is False


def test_take_profit_la_plus_4_la_suta():
    iesire, motiv = m.verifica_iesire("X", poz(), 104.0, ema9=11, ema21=10, rsi_5m=50)
    assert iesire is True
    assert "TAKE PROFIT" in motiv


def test_rsi_supracumparat_peste_pragul_de_trailing():
    iesire, motiv = m.verifica_iesire("X", poz(), 101.6, ema9=11, ema21=10, rsi_5m=79)
    assert iesire is True
    assert "RSI OVERBOUGHT" in motiv


def test_rsi_exact_pe_prag_nu_declanseaza():
    iesire, _ = m.verifica_iesire("X", poz(), 101.6, ema9=11, ema21=10,
                                  rsi_5m=m.RSI_5M_EXIT)
    assert iesire is False


def test_rsi_nu_taie_pozitia_sub_pragul_de_trailing():
    """Cazul CSCO din 3 august: RSI 86 la +0.2%, iesirea ar fi trunchiat trade-ul."""
    iesire, _ = m.verifica_iesire("X", poz(), 100.2, ema9=11, ema21=10, rsi_5m=86)
    assert iesire is False


def test_rsi_nu_se_declanseaza_cu_o_fractiune_sub_prag():
    """+1.4% e sub pragul de trailing de 1.5%, deci RSI-ul inca nu are voie."""
    iesire, _ = m.verifica_iesire("X", poz(), 101.4, ema9=11, ema21=10, rsi_5m=90)
    assert iesire is False


def test_stop_loss_are_prioritate_fata_de_rsi():
    """RSI mare pe pierdere nu trebuie sa mascheze stop loss-ul."""
    iesire, motiv = m.verifica_iesire("X", poz(), 98.5, ema9=11, ema21=10, rsi_5m=90)
    assert iesire is True
    assert "STOP LOSS" in motiv


def test_ema_cross_ramane_plasa_de_siguranta_sub_prag():
    """Sub pragul de trailing, momentumul pierdut il prinde EMA cross, nu RSI."""
    iesire, motiv = m.verifica_iesire("X", poz(), 100.5, ema9=9, ema21=10, rsi_5m=86)
    assert iesire is True
    assert "EMA CROSS" in motiv


def test_ema_cross_iese_doar_pe_profit():
    iesire, motiv = m.verifica_iesire("X", poz(), 100.5, ema9=9, ema21=10, rsi_5m=50)
    assert iesire is True
    assert "EMA CROSS" in motiv


def test_ema_cross_nu_iese_pe_pierdere():
    """Pe pierdere, iesirea trebuie lasata in seama stop loss-ului."""
    iesire, _ = m.verifica_iesire("X", poz(), 99.5, ema9=9, ema21=10, rsi_5m=50)
    assert iesire is False


def test_ema_cross_nu_iese_la_pret_egal_cu_intrarea():
    iesire, _ = m.verifica_iesire("X", poz(), 100.0, ema9=9, ema21=10, rsi_5m=50)
    assert iesire is False


def test_ema_cross_nu_iese_sub_pragul_care_acopera_costul():
    """La +0.2% ordinul market inchidea in pierdere dupa spread."""
    iesire, _ = m.verifica_iesire("X", poz(), 100.2, ema9=9, ema21=10, rsi_5m=50)
    assert iesire is False


def test_ema_cross_iese_peste_pragul_care_acopera_costul():
    iesire, motiv = m.verifica_iesire("X", poz(), 100.4, ema9=9, ema21=10, rsi_5m=50)
    assert iesire is True
    assert "EMA CROSS" in motiv


def test_verifica_iesire_retine_si_minimul_parcurs():
    p = poz()
    m.verifica_iesire("X", p, 99.0, ema9=11, ema21=10, rsi_5m=50)
    m.verifica_iesire("X", p, 100.8, ema9=11, ema21=10, rsi_5m=50)
    assert p["pret_min"] == 99.0
    assert p["pret_max"] == 100.8


# ─────────────────────────────────────────────────────────────
# metrici_pozitie — MFE / MAE / durata
# ─────────────────────────────────────────────────────────────
def test_metrici_masoara_excursia_maxima_in_ambele_directii():
    p = poz(pret_max=103.0)
    p["pret_min"] = 98.5
    met = m.metrici_pozitie(p, 101.0)
    assert met["mfe_pct"] == 3.0
    assert met["mae_pct"] == -1.5


def test_metrici_includ_pretul_de_iesire_in_excursie():
    """Iesirea poate fi ea insasi extrema, daca a picat intre doua cicluri."""
    met = m.metrici_pozitie(poz(), 104.0)
    assert met["mfe_pct"] == 4.0
    met = m.metrici_pozitie(poz(), 97.0)
    assert met["mae_pct"] == -3.0


def test_metrici_calculeaza_durata_din_ora_de_intrare():
    p = poz()
    p["ora_intrare"] = (m.acum_ny() - timedelta(minutes=42)).isoformat()
    assert m.metrici_pozitie(p, 100.0)["durata_min"] == 42


def test_metrici_omit_durata_pentru_pozitii_adoptate():
    """Fara ora de intrare (pozitie preluata la reconciliere) durata lipseste."""
    assert "durata_min" not in m.metrici_pozitie(poz(), 100.0)


def test_metrici_goale_fara_pret_de_intrare():
    assert m.metrici_pozitie({}, 100.0) == {}


# ─────────────────────────────────────────────────────────────
# Trailing stop
# ─────────────────────────────────────────────────────────────
def test_trailing_se_activeaza_la_plus_1_5():
    p = poz()
    m.verifica_iesire("X", p, 101.5, ema9=11, ema21=10, rsi_5m=50)
    assert p["trailing_activ"] is True


def test_trailing_inactiv_sub_pragul_de_activare():
    p = poz()
    m.verifica_iesire("X", p, 101.0, ema9=11, ema21=10, rsi_5m=50)
    assert p["trailing_activ"] is False


def test_pret_max_se_actualizeaza_in_sus():
    p = poz()
    m.verifica_iesire("X", p, 103.0, ema9=11, ema21=10, rsi_5m=50)
    assert p["pret_max"] == 103.0


def test_pret_max_nu_scade():
    p = poz(pret_max=105.0)
    m.verifica_iesire("X", p, 102.0, ema9=11, ema21=10, rsi_5m=50)
    assert p["pret_max"] == 105.0


def test_trailing_declanseaza_la_1_la_suta_sub_maxim():
    """Profit sub pragul de take profit, ca sa se testeze doar trailing-ul."""
    p = poz(pret_max=103.5, trailing_activ=True)
    iesire, motiv = m.verifica_iesire("X", p, 102.4, ema9=11, ema21=10, rsi_5m=50)
    assert iesire is True
    assert "TRAILING STOP" in motiv


def test_trailing_nu_declanseaza_la_scadere_mica():
    p = poz(pret_max=103.5, trailing_activ=True)
    iesire, _ = m.verifica_iesire("X", p, 103.0, ema9=11, ema21=10, rsi_5m=50)
    assert iesire is False


def test_trailing_are_prioritate_fata_de_take_profit():
    """Ambele conditii sunt adevarate; trailing e verificat primul."""
    p = poz(pret_max=110.0, trailing_activ=True)
    iesire, motiv = m.verifica_iesire("X", p, 104.5, ema9=11, ema21=10, rsi_5m=50)
    assert iesire is True
    assert "TRAILING STOP" in motiv


def test_stop_loss_are_prioritate_fata_de_ema_cross():
    _, motiv = m.verifica_iesire("X", poz(), 98.0, ema9=9, ema21=10, rsi_5m=50)
    assert "STOP LOSS" in motiv


# ─────────────────────────────────────────────────────────────
# Stopul folosit la iesire = stopul folosit la dimensionare
# ─────────────────────────────────────────────────────────────
def test_stop_din_pozitie_are_prioritate_fata_de_cel_fix():
    """O pozitie dimensionata cu stop de 3% nu iese la -1.5%."""
    p = poz()
    p["stop_loss_pct"] = 0.03
    assert m.verifica_iesire("X", p, 98.5, ema9=11, ema21=10, rsi_5m=50)[0] is False
    iesire, motiv = m.verifica_iesire("X", p, 97.0, ema9=11, ema21=10, rsi_5m=50)
    assert iesire is True
    assert "STOP LOSS" in motiv


def test_pozitie_fara_stop_salvat_cade_pe_valoarea_fixa():
    """Pozitiile vechi si cele adoptate la reconciliere nu au campul."""
    p = poz()
    assert "stop_loss_pct" not in p
    assert m.verifica_iesire("X", p, 98.5, ema9=11, ema21=10, rsi_5m=50)[0] is True


def test_cantitatea_si_stopul_folosesc_acelasi_procent(monkeypatch):
    """Riscul realizat trebuie sa fie cel planificat: 1% din portofoliu."""
    monkeypatch.setattr(m, "get_portofoliu", lambda: 100_000.0)
    pret, atr = 200.0, 8.0  # 1.5×ATR/pret = 6%, peste minimul de 1.5%
    cantitate, stop_loss_pct = m.calculeaza_cantitate(pret, atr)
    assert stop_loss_pct == pytest.approx(0.06)
    risc = cantitate * pret * stop_loss_pct
    assert risc <= 100_000.0 * m.RISC_PORTOFOLIU_PCT


def test_stopul_nu_coboara_sub_minim(monkeypatch):
    monkeypatch.setattr(m, "get_portofoliu", lambda: 100_000.0)
    _, stop_loss_pct = m.calculeaza_cantitate(200.0, 0.1)
    assert stop_loss_pct == pytest.approx(m.STOP_LOSS_MIN_PCT)


# ─────────────────────────────────────────────────────────────
# Limita de corelatie pe cluster
# ─────────────────────────────────────────────────────────────
def test_simbol_neincadrat_nu_e_blocat_niciodata():
    pozitii = {"NVDA": poz(), "AMD": poz(), "MU": poz()}
    assert m.cluster_pentru("CSCO") is None
    assert m.cluster_plin(pozitii, "CSCO") is False


def test_clusterul_gol_permite_intrarea():
    assert m.cluster_plin({}, "NVDA") is False


def test_clusterul_sub_limita_permite_intrarea():
    assert m.cluster_plin({"NVDA": poz()}, "AMD") is False


def test_clusterul_la_limita_blocheaza_intrarea():
    assert m.cluster_plin({"NVDA": poz(), "AMD": poz()}, "MU") is True


def test_pozitiile_din_alt_cluster_nu_blocheaza():
    """Trei megacap deschise nu trebuie sa inchida usa semiconductoarelor."""
    pozitii = {"AAPL": poz(), "MSFT": poz(), "META": poz()}
    assert m.cluster_plin(pozitii, "NVDA") is False


def test_clusterele_nu_se_suprapun():
    """Un simbol intr-un singur cluster — altfel cluster_pentru ar fi ambigua."""
    membri = [s for grup in m.CLUSTERE.values() for s in grup]
    assert len(membri) == len(set(membri))


# ─────────────────────────────────────────────────────────────
# Indicatori
# ─────────────────────────────────────────────────────────────
def test_ema_pe_serie_constanta_este_constanta():
    assert m.calculeaza_ema([50.0] * 30, 9) == pytest.approx(50.0)


def test_ema_scurta_reactioneaza_mai_repede_decat_cea_lunga():
    preturi = [100.0] * 30 + [110.0] * 5
    assert m.calculeaza_ema(preturi, 9) > m.calculeaza_ema(preturi, 21)


def test_rsi_100_cand_nu_exista_pierderi():
    """Media pierderilor 0 — evita impartirea la zero."""
    assert m.calculeaza_rsi([100 + i for i in range(30)], 14) == 100.0


def test_rsi_sub_50_pe_trend_descendent():
    assert m.calculeaza_rsi([100 - i for i in range(30)], 14) < 50


def test_rsi_in_interval_valid():
    preturi = [100, 102, 101, 105, 103, 107, 106, 110, 108, 112,
               111, 115, 113, 117, 116, 120]
    assert 0 <= m.calculeaza_rsi(preturi, 14) <= 100


def test_atr_pozitiv_si_creste_cu_volatilitatea():
    import pandas as pd

    def df(amplitudine):
        n = 30
        return pd.DataFrame({
            "High": [100 + amplitudine] * n,
            "Low": [100 - amplitudine] * n,
            "Close": [100.0] * n,
            "Open": [100.0] * n,
        })

    calm = m.calculeaza_atr(df(1), 14)
    agitat = m.calculeaza_atr(df(5), 14)
    assert calm > 0
    assert agitat > calm


# ─────────────────────────────────────────────────────────────
# Persistenta
# ─────────────────────────────────────────────────────────────
def test_salvare_si_citire(tmp_path):
    cale = str(tmp_path / "stare.json")
    m.salveaza_json(cale, {"AAPL": {"cantitate": 10}})
    assert m.incarca_json(cale, {}) == {"AAPL": {"cantitate": 10}}


def test_nu_lasa_fisiere_temporare(tmp_path):
    cale = str(tmp_path / "stare.json")
    m.salveaza_json(cale, {"a": 1})
    assert not os.path.exists(cale + ".tmp")


def test_fisier_lipsa_returneaza_valoarea_implicita(tmp_path):
    cale = str(tmp_path / "nu_exista.json")
    assert m.incarca_json(cale, {"gol": True}) == {"gol": True}


def test_json_corupt_este_pastrat_pentru_inspectie(tmp_path):
    cale = str(tmp_path / "stare.json")
    trunchiat = '{"AAPL": {"canti'
    (tmp_path / "stare.json").write_text(trunchiat)

    rezultat = m.incarca_json(cale, {})

    assert rezultat == {}
    assert os.path.exists(cale + ".corupt"), "fisierul corupt trebuie pastrat"
    assert not os.path.exists(cale), "originalul corupt nu trebuie sa ramana"
    with open(cale + ".corupt") as f:
        assert f.read() == trunchiat


def test_scriere_esuata_nu_distruge_fisierul_existent(tmp_path):
    """Miezul scrierii atomice: o eroare la serializare lasa datele vechi intacte."""
    cale = str(tmp_path / "stare.json")
    m.salveaza_json(cale, {"pozitii": "importante"})

    class NuSePoateSerializa:
        pass

    m.salveaza_json(cale, {"rau": NuSePoateSerializa()})

    assert m.incarca_json(cale, "PIERDUT") == {"pozitii": "importante"}
    assert not os.path.exists(cale + ".tmp")


# ─────────────────────────────────────────────────────────────
# Filtrul de corp pe 5m
#
# Seria e calibrata astfel incat verde, EMA9>EMA21 si RSI in 45-70 sa fie
# toate adevarate; singura variabila ramane ultima lumanare, deci rezultatul
# lui verifica_5m depinde exclusiv de filtrul de corp.
# ─────────────────────────────────────────────────────────────
def _serie_5m(ultima_open, ultima_close, ultima_high, ultima_low):
    import pandas as pd

    o, h, l, c = [], [], [], []
    pret = 100.0
    for i in range(40):
        deschidere = pret
        pret += 0.5 if i % 2 == 0 else -0.35
        o.append(deschidere)
        c.append(pret)
        h.append(max(deschidere, pret) + 0.1)
        l.append(min(deschidere, pret) - 0.1)
    o[-1], c[-1], h[-1], l[-1] = ultima_open, ultima_close, ultima_high, ultima_low
    return pd.DataFrame({"Open": o, "High": h, "Low": l, "Close": c,
                         "Volume": [1000] * 40})


BAZA_5M = 103.35   # pretul dinaintea ultimei bare, pentru seria de mai sus


def test_5m_accepta_lumanare_cu_corp_solid(monkeypatch):
    df = _serie_5m(BAZA_5M, BAZA_5M + 1.0, BAZA_5M + 1.1, BAZA_5M - 0.1)
    monkeypatch.setattr(m, "get_date", lambda *a, **k: df)
    ok, info = m.verifica_5m("X")
    assert ok, "lumanarea cu corp solid trebuie acceptata"
    assert info["corp"] == pytest.approx(1.0)


def test_5m_respinge_corp_mic_cu_umbre_lungi(monkeypatch):
    """Range-ul e urias (4.0) dar corpul e 0.02: indecizie, nu impuls.
    Filtrul vechi, pe high-low, lasa asta sa treaca."""
    df = _serie_5m(BAZA_5M, BAZA_5M + 0.02, BAZA_5M + 2.0, BAZA_5M - 2.0)
    monkeypatch.setattr(m, "get_date", lambda *a, **k: df)
    ok, info = m.verifica_5m("X")
    assert not ok, "corpul sub prag trebuie respins, oricat de mare ar fi range-ul"
    assert info["corp"] == pytest.approx(0.02)


def test_5m_date_insuficiente(monkeypatch):
    monkeypatch.setattr(m, "get_date", lambda *a, **k: None)
    ok, info = m.verifica_5m("X")
    assert not ok
    assert "insuficiente" in info["motiv"]


# ─────────────────────────────────────────────────────────────
# Reconciliere cu brokerul
# ─────────────────────────────────────────────────────────────
def test_reconciliere_stare_deja_corecta():
    locale = {"AAPL": poz(pret_intrare=150.0, cantitate=10)}
    corectate, mesaje = m.reconciliaza(locale, [("AAPL", 10, 150.0)])
    assert mesaje == []
    assert corectate["AAPL"]["cantitate"] == 10


def test_reconciliere_adopta_pozitie_necunoscuta_local():
    """Pozitie reala la broker pe care agentul nu o urmarea — altfel n-ar fi inchis-o."""
    corectate, mesaje = m.reconciliaza({}, [("TSLA", 5, 200.0)])
    assert corectate["TSLA"]["cantitate"] == 5
    assert corectate["TSLA"]["pret_intrare"] == 200.0
    assert corectate["TSLA"]["pret_max"] == 200.0
    assert corectate["TSLA"]["trailing_activ"] is False
    assert len(mesaje) == 1


def test_reconciliere_elimina_pozitia_fantoma():
    """Pozitie locala inexistenta la broker — bloca un slot din MAX_POZITII."""
    locale = {"NVDA": poz(cantitate=3)}
    corectate, mesaje = m.reconciliaza(locale, [])
    assert corectate == {}
    assert len(mesaje) == 1


def test_reconciliere_aliniaza_cantitatea_la_broker():
    locale = {"AAPL": poz(pret_intrare=150.0, cantitate=10)}
    corectate, _ = m.reconciliaza(locale, [("AAPL", 7, 150.0)])
    assert corectate["AAPL"]["cantitate"] == 7


def test_reconciliere_pastreaza_pret_intrare_si_trailing_local():
    """La aliniere de cantitate nu se pierde istoricul pozitiei."""
    locale = {"AAPL": poz(pret_intrare=150.0, pret_max=160.0,
                          trailing_activ=True, cantitate=10)}
    corectate, _ = m.reconciliaza(locale, [("AAPL", 7, 155.0)])
    assert corectate["AAPL"]["pret_intrare"] == 150.0
    assert corectate["AAPL"]["pret_max"] == 160.0
    assert corectate["AAPL"]["trailing_activ"] is True


def test_reconciliere_nu_modifica_dictionarul_primit():
    locale = {"AAPL": poz(cantitate=10)}
    m.reconciliaza(locale, [("AAPL", 7, 150.0)])
    assert locale["AAPL"]["cantitate"] == 10


def test_reconciliere_mai_multe_diferente_simultan():
    locale = {"AAPL": poz(cantitate=10), "NVDA": poz(cantitate=3)}
    corectate, mesaje = m.reconciliaza(locale, [("AAPL", 10, 100.0), ("TSLA", 5, 200.0)])
    assert set(corectate) == {"AAPL", "TSLA"}
    assert len(mesaje) == 2


# ─────────────────────────────────────────────────────────────
# Cooldown
# ─────────────────────────────────────────────────────────────
def memorie_goala():
    return {"tranzactii": [], "performanta": {}, "cooldown": {},
            "stats": {"total_profit": 0, "wins": 0, "losses": 0}}


def minute_pana_la_expirare(memorie, simbol):
    expira = datetime.fromisoformat(memorie["cooldown"][simbol])
    return (expira - m.acum_ny()).total_seconds() / 60


def test_cooldown_activ_pana_la_expirare():
    memorie = {"cooldown": {"AAPL": (m.acum_ny() + timedelta(hours=1)).isoformat()}}
    assert m.in_cooldown(memorie, "AAPL") is True


def test_cooldown_expira_si_intrarea_este_stearsa():
    vechi = (m.acum_ny() - timedelta(minutes=1)).isoformat()
    memorie = {"cooldown": {"AAPL": vechi}}
    assert m.in_cooldown(memorie, "AAPL") is False
    assert "AAPL" not in memorie["cooldown"]


def test_cooldown_accepta_timestamp_fara_fus_orar():
    """Compatibilitate cu memorie_multitf.json scris inainte de orele aware."""
    naiv = (m.acum_ny() + timedelta(hours=1)).replace(tzinfo=None).isoformat()
    memorie = {"cooldown": {"AAPL": naiv}}
    assert m.in_cooldown(memorie, "AAPL") is True


def test_cooldown_cu_timestamp_corupt_este_curatat():
    memorie = {"cooldown": {"AAPL": "nu-e-o-data"}}
    assert m.in_cooldown(memorie, "AAPL") is False
    assert "AAPL" not in memorie["cooldown"]


def test_cooldown_absent_pentru_simbol_necunoscut():
    assert m.in_cooldown({"cooldown": {}}, "AAPL") is False


def test_pierderea_da_cooldown_de_patru_ore():
    memorie = memorie_goala()
    m.log_tranzactie(memorie, "AAPL", "close_long", 100.0, 10, profit=-50.0)
    assert m.in_cooldown(memorie, "AAPL") is True
    assert minute_pana_la_expirare(memorie, "AAPL") == pytest.approx(
        m.COOLDOWN_ORE * 60, abs=1)


def test_iesirea_pe_plus_da_cooldown_de_reintrare():
    memorie = memorie_goala()
    m.log_tranzactie(memorie, "CSCO", "close_long", 115.52, 21, profit=1.26)
    assert m.in_cooldown(memorie, "CSCO") is True
    assert minute_pana_la_expirare(memorie, "CSCO") == pytest.approx(
        m.COOLDOWN_REINTRARE_MIN, abs=1)


def test_reintrarea_imediata_pe_acelasi_simbol_este_blocata():
    """Cazul din 3 august: iesire pe +0.1%, reintrare 6 minute mai tarziu."""
    memorie = memorie_goala()
    m.log_tranzactie(memorie, "CSCO", "close_long", 115.52, 21, profit=1.26)
    assert m.in_cooldown(memorie, "CSCO") is True


def test_deschiderea_pozitiei_nu_pune_cooldown():
    memorie = memorie_goala()
    m.log_tranzactie(memorie, "AAPL", "open_long", 339.46, 7)
    assert memorie["cooldown"] == {}


def test_cooldownul_nu_afecteaza_alte_simboluri():
    memorie = memorie_goala()
    m.log_tranzactie(memorie, "CSCO", "close_long", 115.52, 21, profit=1.26)
    assert m.in_cooldown(memorie, "AVGO") is False
