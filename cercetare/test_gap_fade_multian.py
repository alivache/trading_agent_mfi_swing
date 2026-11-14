"""Teste pentru gap_fade_multian.py: capcanele care ar falsifica rezultatul H4."""
from datetime import date, datetime, timedelta, timezone

import pytest

from analyst import NY
from gap_fade_multian import candidati_simbol, e_actiune_obisnuita, simuleaza_short


def zilnice(preturi, start=date(2020, 1, 2), volum=1_000_000, zile=None):
    """preturi: lista de (deschidere, inchidere); zile lucratoare consecutive daca `zile` lipseste."""
    rez, z = [], start
    for i, (o, c) in enumerate(preturi):
        if zile:
            z = zile[i]
        else:
            while z.weekday() >= 5:
                z += timedelta(days=1)
        rez.append({"t": f"{z.isoformat()}T05:00:00Z", "o": o, "c": c, "v": volum})
        z += timedelta(days=1)
    return rez


def test_gap_valid_e_gasit():
    p = [(10, 10)] * 25 + [(11, 11)]                 # +10% la deschiderea zilei 26
    c = candidati_simbol("ABCD", zilnice(p), zilnice(p))
    assert len(c) == 1 and c[0]["gap"] == pytest.approx(0.10)
    assert c[0]["valoare20"] == pytest.approx(10 * 1_000_000)


def test_pret_sub_5_sau_lichiditate_mica_sunt_excluse():
    p = [(4, 4)] * 25 + [(4.8, 4.8)]
    assert candidati_simbol("IEFT", zilnice(p), zilnice(p)) == []
    p = [(10, 10)] * 25 + [(11, 11)]
    assert candidati_simbol("ILIC", zilnice(p, volum=1000), zilnice(p, volum=1000)) == []


def test_split_nu_pare_gap():
    # split invers 1:10 in ziua 26: brut sare de la 10 la 100, ajustat ramane ~100 -> 100
    brut = [(10, 10)] * 25 + [(100, 100)]
    ajustat = [(100, 100)] * 26
    assert candidati_simbol("REVS", zilnice(brut), zilnice(ajustat)) == []


def test_simbol_refolosit_nu_produce_gap_fals():
    # ultima bara a vechii companii, apoi peste 2 ani prima bara a celei noi, la alt pret
    zile = [date(2018, 1, 2) + timedelta(days=i) for i in range(25)] + [date(2020, 6, 1)]
    p = [(10, 10)] * 25 + [(30, 30)]
    assert candidati_simbol("ULTI", zilnice(p, zile=zile), zilnice(p, zile=zile)) == []


@pytest.mark.parametrize("activ, ok", [
    ({"symbol": "ABCD", "name": "Abcd Inc", "class": "us_equity", "exchange": "NASDAQ"}, True),
    ({"symbol": "OLD", "name": "", "class": "us_equity", "exchange": "NYSE"}, True),        # delistat, fara nume
    ({"symbol": "ABCDW", "name": "", "class": "us_equity", "exchange": "NASDAQ"}, False),
    ({"symbol": "BRAC_DELISTED", "name": "", "class": "us_equity", "exchange": "NASDAQ"}, False),
    ({"symbol": "804CVR017", "name": "", "class": "us_equity", "exchange": "NASDAQ"}, False),
    ({"symbol": "SPY", "name": "SPDR S&P 500 ETF", "class": "us_equity", "exchange": "ARCA"}, False),
    ({"symbol": "QQQX", "name": "Some ETF", "class": "us_equity", "exchange": "NASDAQ"}, False),
])
def test_filtrul_de_univers(activ, ok):
    assert e_actiune_obisnuita(activ) is ok


# ---------------------------------------------------------------- simularea pe minute

DESCHIDERE = datetime(2024, 3, 5, 9, 30, tzinfo=NY)
IESIRE = datetime(2024, 3, 5, 15, 49, tzinfo=NY)


def minute(serii):
    """serii: lista de (minut_de_la_deschidere, o, h, l, c)."""
    return [{"t": (DESCHIDERE + timedelta(minutes=m)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
             "o": o, "h": h, "l": l, "c": c} for m, o, h, l, c in serii]


def test_short_fara_stop_atins_iese_la_15_49():
    b = minute([(0, 100, 101, 99, 100), (100, 98, 99, 95, 96), (379, 95, 95, 94, 94), (385, 50, 50, 50, 50)])
    r = simuleaza_short(b, DESCHIDERE, IESIRE)
    assert r["intrare"] == 100 and r["final"] == 94          # bara de 15:55 (50) e ignorata
    assert r["stop10"] == pytest.approx(0.06)                # short de la 100 la 94 = +6%


def test_stop_atins_pierde_exact_procentul_stopului():
    b = minute([(0, 100, 101, 99, 100), (10, 105, 111, 104, 106), (379, 90, 90, 90, 90)])
    r = simuleaza_short(b, DESCHIDERE, IESIRE)
    assert r["stop10"] == pytest.approx(-0.10)
    assert r["stop20"] == pytest.approx(0.10)                # stopul de 20% nu e atins: 100 -> 90
    assert r["stop5"] == pytest.approx(-0.05)                # bara deschide exact la 105 -> executie la 105


def test_bara_care_sare_peste_stop_se_executa_la_deschiderea_ei():
    # dupa o oprire, tranzactionarea reia la 130: stopul de 10% (110) se executa la 130, nu la 110
    b = minute([(0, 100, 101, 99, 100), (60, 130, 135, 128, 131), (379, 120, 120, 120, 120)])
    r = simuleaza_short(b, DESCHIDERE, IESIRE)
    assert r["stop10"] == pytest.approx(-0.30)


def test_castigul_unui_short_nu_poate_depasi_100_la_suta():
    # prabusire de 92% (ca HOLO pe 19 sep 2022): short-ul castiga 92%, nu +1085%
    b = minute([(0, 33.56, 33.56, 2.8, 30), (379, 2.83, 2.83, 2.83, 2.83)])
    r = simuleaza_short(b, DESCHIDERE, IESIRE)
    assert r["fara_stop"] == pytest.approx(1 - 2.83 / 33.56)
    assert r["fara_stop"] < 1


def test_fara_deschidere_normala_nu_se_tranzactioneaza():
    b = minute([(20, 100, 101, 99, 100), (379, 95, 95, 95, 95)])     # prima bara la 9:50
    assert simuleaza_short(b, DESCHIDERE, IESIRE) is None
