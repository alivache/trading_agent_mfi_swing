import dashboard


def cumparare(simbol, pret, cantitate, ora):
    return {"simbol": simbol, "tip": "open_long", "pret": pret, "cantitate": cantitate,
            "profit": None, "motiv": "", "ora": ora, "data": "2026-08-26"}


def vanzare(simbol, pret, cantitate, ora, profit):
    return {"simbol": simbol, "tip": "close_long", "pret": pret, "cantitate": cantitate,
            "profit": profit, "motiv": "STOP LOSS", "ora": ora, "data": "2026-08-26"}


def test_pnl_din_broker_cand_api_ul_raspunde(monkeypatch):
    monkeypatch.setattr(dashboard, "pozitii_broker",
                        lambda: {"MU": {"pret_curent": 940.0, "profit": 11.1, "profit_pct": 0.59}})
    pnl = dashboard.pnl_pozitii({"MU": {"pret_intrare": 934.45, "cantitate": 2}}, {})
    assert pnl["MU"] == {"pret_curent": 940.0, "profit": 11.1, "profit_pct": 0.59,
                         "aproximativ": False}


def test_pnl_cade_pe_cache_si_se_marcheaza_aproximativ(monkeypatch):
    monkeypatch.setattr(dashboard, "pozitii_broker", dict)
    grafice = {"MU": {"close": [930.0, 944.45]}}
    pnl = dashboard.pnl_pozitii({"MU": {"pret_intrare": 934.45, "cantitate": 2}}, grafice)
    assert pnl["MU"]["profit"] == 20.0
    assert pnl["MU"]["aproximativ"] is True


def test_pozitia_fara_pret_disponibil_este_omisa(monkeypatch):
    monkeypatch.setattr(dashboard, "pozitii_broker", dict)
    assert dashboard.pnl_pozitii({"MU": {"pret_intrare": 934.45, "cantitate": 2}}, {}) == {}


def test_cumpararea_deschisa_primeste_pnl_ul_curent():
    tranzactii = [cumparare("MU", 934.45, 2, "09:51")]
    pnl = {"MU": {"profit": 11.1, "profit_pct": 0.59, "aproximativ": False}}
    dashboard.adauga_rezultat_cumpararilor(tranzactii, pnl)
    assert tranzactii[0]["pnl_curent"] == 11.1
    assert tranzactii[0]["pnl_pct"] == 0.59
    assert tranzactii[0]["pnl_stare"] == "deschis"


def test_cumpararea_inchisa_primeste_profitul_realizat():
    tranzactii = [cumparare("NVDA", 212.93, 11, "09:45"),
                  vanzare("NVDA", 209.77, 11, "12:38", -34.73)]
    dashboard.adauga_rezultat_cumpararilor(tranzactii, {})
    assert tranzactii[0]["pnl_curent"] == -34.73
    assert tranzactii[0]["pnl_stare"] == "inchis"
    assert tranzactii[1]["pnl_curent"] is None


def test_reintrarea_in_aceeasi_zi_nu_amesteca_rezultatele():
    tranzactii = [cumparare("MU", 900.0, 1, "09:30"),
                  vanzare("MU", 910.0, 1, "10:00", 10.0),
                  cumparare("MU", 934.45, 2, "11:00")]
    pnl = {"MU": {"profit": 11.1, "profit_pct": 0.59, "aproximativ": True}}
    dashboard.adauga_rezultat_cumpararilor(tranzactii, pnl)
    assert tranzactii[0]["pnl_stare"] == "inchis" and tranzactii[0]["pnl_curent"] == 10.0
    assert tranzactii[2]["pnl_stare"] == "aproximativ" and tranzactii[2]["pnl_curent"] == 11.1
