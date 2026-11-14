"""Teste pentru replay: universul reconstruit din bare si garzile scriptului.

Fara retea: aceleasi date sintetice ca in test_gap_scanner / test_gap_shadow.
"""
import os
from datetime import date, datetime, timedelta

import pytest

import analyst
import evalueaza_gap
import gap_scanner
import replay_analist
from analyst import NY
from test_gap_scanner import ACUM, ClaudeFals, jurnal_temporar, scenariu  # noqa: F401
from test_gap_shadow import ClientCuCalendar, client_complet


@pytest.fixture(autouse=True)
def replay_temporar(tmp_path, monkeypatch):
    monkeypatch.setattr(replay_analist, "DIR_REPLAY", str(tmp_path / "replay"))


class ClientCuInterval(ClientCuCalendar):
    """Calendar pe interval: toate zilele lucratoare intre start si end."""

    def get(self, cale, params=None, trading=False):
        if cale == "/v2/calendar":
            self.cereri.append((cale, dict(params)))
            z, end = date.fromisoformat(params["start"]), date.fromisoformat(params["end"])
            zile = []
            while z <= end:
                if z.weekday() < 5:
                    zile.append({"date": z.isoformat(), "open": "09:30", "close": "16:00"})
                z += timedelta(days=1)
            return zile
        return super().get(cale, params, trading)


def client_interval():
    c = client_complet()
    return ClientCuInterval(c.active, c.movers, c.zilnic, c.minute5, c.stiri_lista,
                            minute1=c.minute1)


# ---------------------------------------------------------------- universul reconstruit

def test_univers_complet_aplica_pragurile_si_ordoneaza_dupa_valoare():
    u = gap_scanner.univers_complet(ACUM, scenariu())
    # eliminati: ABCW (warrant), IEFT (pret), MICG (gap), SUBV (volum)
    # valoare: BUNB 22x84.000 > RELV = NOUA 12x112.000 > BUNA 11.5x112.000
    assert u == ["BUNB", "RELV", "NOUA", "BUNA"]


def test_univers_complet_nu_cere_date_de_dupa_limita():
    client = scenariu()
    gap_scanner.univers_complet(ACUM, client)
    limita = ACUM - gap_scanner.INTARZIERE_DATE
    for cale, params in client.cereri:
        if cale == "/v2/stocks/bars":
            assert datetime.fromisoformat(params["end"]) <= limita


def test_univers_complet_respecta_maximul():
    assert gap_scanner.univers_complet(ACUM, scenariu(), maxim=2) == ["BUNB", "RELV"]


def test_cererile_de_bare_se_impart_in_loturi(monkeypatch):
    monkeypatch.setattr(gap_scanner, "SIMBOLURI_PE_CERERE", 3)
    client = scenariu()
    rezultat = gap_scanner.bare(client, ["BUNA", "BUNB", "IEFT", "MICG", "SUBV"], "1Day",
                                ACUM - timedelta(days=15), ACUM)
    cereri = [p for c, p in client.cereri if c == "/v2/stocks/bars"]
    assert [p["symbols"] for p in cereri] == ["BUNA,BUNB,IEFT", "MICG,SUBV"]
    assert len(rezultat["SUBV"]) == 10


# ---------------------------------------------------------------- garzile scriptului

def test_refuza_zile_dinaintea_datei_limita_a_modelului(capsys):
    client = client_interval()
    cod = replay_analist.main(["--zi", "2026-06-15"], client=client, client_claude=ClaudeFals(),
                              azi=date(2026, 11, 17))
    assert cod == 1
    assert "contaminate" in capsys.readouterr().out
    assert not any(c == "/v2/stocks/bars" for c, _ in client.cereri)


def test_fara_claude_merge_si_inainte_de_limita():
    cod = replay_analist.main(["--zi", "2026-06-15", "--fara-claude"], client=client_interval(),
                              azi=date(2026, 11, 17))
    assert cod == 0


def test_interval_scump_cere_confirmare(capsys):
    client = client_interval()
    cod = replay_analist.main(["--de-la", "2026-08-03", "--pana-la", "2026-08-31"], client=client,
                              client_claude=ClaudeFals(), azi=date(2026, 11, 17))
    assert cod == 1
    assert "--da" in capsys.readouterr().out
    assert not any(c == "/v2/stocks/bars" for c, _ in client.cereri)


def test_ziua_de_azi_si_viitorul_sunt_excluse(capsys):
    cod = replay_analist.main(["--zi", "2026-11-16"], client=client_interval(),
                              client_claude=ClaudeFals(), azi=date(2026, 11, 16))
    assert cod == 1


# ---------------------------------------------------------------- replay cap-coada

def test_replay_o_zi_scrie_in_jurnalul_separat_si_evalueaza(capsys):
    cod = replay_analist.main(["--zi", "2026-11-16"], client=client_interval(),
                              client_claude=ClaudeFals(), azi=date(2026, 11, 17))
    assert cod == 0
    iesire = capsys.readouterr().out
    assert "BUNA" in iesire and "tradeable" in iesire and "exces" in iesire
    fisiere = os.listdir(replay_analist.DIR_REPLAY)
    assert "scanari_2026-11-16.jsonl" in fisiere and "evaluare_2026-11-16.jsonl" in fisiere
    # BUNB/BUNA trec scannerul; RELV si NOUA cad la volumul relativ in etapa a doua
    evaluari = {e["simbol"] for e in evalueaza_gap.toate_evaluarile()}
    assert evaluari == {"BUNA", "BUNB"}
