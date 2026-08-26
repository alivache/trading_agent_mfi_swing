import pandas as pd

from vwap_pullback_strategy import analizeaza_pullback, calculeaza_vwap, pregateste_bare, scrie_semnal


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


def test_vwap_se_reseteaza_la_sesiunea_new_york():
    index = pd.to_datetime(["2026-08-25 19:55Z", "2026-08-25 20:00Z", "2026-08-26 13:30Z"])
    frame = bare(index, [100, 102, 200], volume=100)
    vwap = calculeaza_vwap(frame)
    assert vwap.iloc[1] == 101.1
    assert vwap.iloc[2] == 200.1


def test_pullback_confirmat_genereaza_semnal():
    index = pd.date_range("2026-08-26 13:30Z", periods=55, freq="5min")
    closes = [100 + i * 0.05 for i in range(54)] + [101.6]
    lows = [value - 0.1 for value in closes[:-1]] + [101.2]
    frame = bare(index, closes, volume=1000, low=lows)
    frame.iloc[-1, frame.columns.get_loc("Open")] = 101.4
    frame.iloc[-1, frame.columns.get_loc("Volume")] = 3000
    ok, reason, details = analizeaza_pullback(frame)
    assert ok
    assert reason == "VWAP pullback confirmat"
    assert details["volume"] == 3000


def test_pullback_respins_fara_confirmare():
    index = pd.date_range("2026-08-26 13:30Z", periods=55, freq="5min")
    closes = [100 + i * 0.05 for i in range(55)]
    frame = bare(index, closes, volume=1000)
    frame.iloc[-1, frame.columns.get_loc("Open")] = closes[-1] + 1
    ok, _, _ = analizeaza_pullback(frame)
    assert not ok


def test_semnalul_se_deduplicateaza(tmp_path, monkeypatch):
    import vwap_pullback_strategy as module

    monkeypatch.setattr(module, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(module, "SIGNALS_FILE", str(tmp_path / "signals.csv"))
    assert scrie_semnal("AAPL", "bar-1", {"close": 100})
    assert not scrie_semnal("AAPL", "bar-1", {"close": 100})
    assert len((tmp_path / "signals.csv").read_text().splitlines()) == 2
