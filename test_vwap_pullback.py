from datetime import timedelta

import pandas as pd

from vwap_pullback_strategy import (analizeaza_pullback, calculeaza_vwap, doar_sesiune_regulara,
                                    elimina_bara_incompleta, scrie_semnal)


def bare(index, close, volume=1000, low=None):
    close = list(close)
    low = low or close
    return pd.DataFrame({
        "Open": [value - 0.2 for value in close],
        "High": [value + 0.3 for value in close],
        "Low": low,
        "Close": close,
        "Volume": [volume] * len(close),
    }, index=index)


def sesiune_confirmata(periods=55):
    """Session ending in a bar that satisfies every pullback filter."""
    index = pd.date_range("2026-08-26 13:30Z", periods=periods, freq="5min")
    closes = [100 + i * 0.05 for i in range(periods - 1)] + [101.6]
    lows = [value - 0.1 for value in closes[:-1]] + [101.2]
    frame = bare(index, closes, volume=1000, low=lows)
    frame.iloc[-1, frame.columns.get_loc("Open")] = 101.4
    frame.iloc[-1, frame.columns.get_loc("Volume")] = 3000
    return frame


def dupa_inchiderea_barei(frame, pozitie=-1):
    """The moment the given bar is complete — the earliest time it may be analyzed."""
    return frame.index[pozitie] + timedelta(minutes=5)


def test_vwap_se_reseteaza_la_sesiunea_new_york():
    index = pd.to_datetime(["2026-08-25 19:55Z", "2026-08-25 20:00Z", "2026-08-26 13:30Z"])
    frame = bare(index, [100, 102, 200], volume=100)
    vwap = calculeaza_vwap(frame)
    assert vwap.iloc[1] == 101.1
    assert vwap.iloc[2] == 200.1


def test_pullback_confirmat_genereaza_semnal():
    frame = sesiune_confirmata()
    ok, reason, details, bar_time = analizeaza_pullback(frame, dupa_inchiderea_barei(frame))
    assert ok
    assert reason == "VWAP pullback confirmat"
    assert details["volume"] == 3000
    assert bar_time == frame.index[-1]


def test_pullback_respins_fara_confirmare():
    index = pd.date_range("2026-08-26 13:30Z", periods=55, freq="5min")
    closes = [100 + i * 0.05 for i in range(55)]
    frame = bare(index, closes, volume=1000)
    frame.iloc[-1, frame.columns.get_loc("Open")] = closes[-1] + 1
    ok, _, _, _ = analizeaza_pullback(frame, dupa_inchiderea_barei(frame))
    assert not ok


def test_barele_din_pre_market_sunt_excluse():
    index = pd.to_datetime(["2026-08-26 12:50Z", "2026-08-26 13:30Z", "2026-08-26 20:05Z"])
    frame = bare(index, [100, 101, 102])
    ramase = doar_sesiune_regulara(frame)
    assert list(ramase.index) == [pd.Timestamp("2026-08-26 13:30Z")]


def test_bara_in_formare_este_eliminata():
    frame = sesiune_confirmata()
    inchisa = dupa_inchiderea_barei(frame, -2)
    assert len(elimina_bara_incompleta(frame, inchisa)) == len(frame) - 1
    assert len(elimina_bara_incompleta(frame, dupa_inchiderea_barei(frame))) == len(frame)


def test_semnalul_ignora_bara_in_formare():
    frame = sesiune_confirmata()
    # Bara care abia s-a deschis, cu un pret aberant: nu trebuie sa influenteze decizia.
    in_formare = bare(pd.DatetimeIndex([frame.index[-1] + timedelta(minutes=5)]), [90.0])
    ok, _, details, bar_time = analizeaza_pullback(pd.concat([frame, in_formare]),
                                                  dupa_inchiderea_barei(frame))
    assert ok
    assert bar_time == frame.index[-1]
    assert details["volume"] == 3000


def test_bara_invechita_este_respinsa():
    frame = sesiune_confirmata()
    ok, motiv, _, _ = analizeaza_pullback(frame, frame.index[-1] + timedelta(minutes=60))
    assert not ok
    assert motiv == "bara invechita"


def test_warm_up_respinge_primele_minute_de_sesiune():
    zi_precedenta = pd.date_range("2026-08-25 13:30Z", periods=78, freq="5min")
    deschidere = pd.date_range("2026-08-26 13:30Z", periods=6, freq="5min")
    index = zi_precedenta.append(deschidere)
    frame = bare(index, [100 + i * 0.05 for i in range(len(index))])
    ok, motiv, _, _ = analizeaza_pullback(frame, dupa_inchiderea_barei(frame))
    assert not ok
    assert motiv == "warm-up sesiune"


def test_semnalul_se_deduplicateaza(tmp_path, monkeypatch):
    import vwap_pullback_strategy as module

    monkeypatch.setattr(module, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(module, "SIGNALS_FILE", str(tmp_path / "signals.csv"))
    assert scrie_semnal("AAPL", "bar-1", {"close": 100})
    assert not scrie_semnal("AAPL", "bar-1", {"close": 100})
    assert len((tmp_path / "signals.csv").read_text().splitlines()) == 2
