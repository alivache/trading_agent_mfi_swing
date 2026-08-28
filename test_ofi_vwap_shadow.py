import csv
import logging
import time as module_time
from datetime import datetime, timezone
from types import SimpleNamespace

from ofi_vwap_shadow import (OfiAggregator, OpresteFurtunaDeReconectari, contributie_ofi,
                             in_sesiune_regulara, proceseaza_bar, ratio_ofi)


def quote(bid, ask, bid_size, ask_size, minute=0, symbol="AAPL"):
    return SimpleNamespace(
        bid_price=bid,
        ask_price=ask,
        bid_size=bid_size,
        ask_size=ask_size,
        symbol=symbol,
        timestamp=datetime(2026, 8, 26, 13, minute, tzinfo=timezone.utc),
    )


def test_ofi_bid_increase_is_buying_pressure():
    flow, state = contributie_ofi(None, quote(100, 100.1, 10, 12))
    assert flow == 0
    flow, _ = contributie_ofi(state, quote(100.1, 100.1, 15, 12))
    assert flow == 15


def test_ofi_ask_increase_is_buying_pressure():
    flow, state = contributie_ofi(None, quote(100, 100.1, 10, 12))
    flow, _ = contributie_ofi(state, quote(100, 100.2, 10, 15))
    assert flow == 12


def test_ratio_ofi_is_normalized():
    assert ratio_ofi(25, 75, 25) == 0.25
    assert ratio_ofi(10, 0, 0) == 0


def test_aggregator_keeps_minutes_separate():
    aggregator = OfiAggregator()
    aggregator.update("AAPL", quote(100, 100.1, 10, 10, 0))
    aggregator.update("AAPL", quote(100.1, 100.1, 20, 10, 1))
    first = aggregator.finalize("AAPL", quote(100, 100.1, 10, 10, 0).timestamp.replace(second=0))
    assert first["quotes"] == 1
    assert aggregator.bars


def test_expira_inchide_si_barele_simbolurilor_tacute():
    aggregator = OfiAggregator()
    aggregator.update("INTC", quote(100, 100.1, 10, 10, 0, symbol="INTC"))
    curent = aggregator.update("AAPL", quote(100, 100.1, 10, 10, 1))
    expirate = aggregator.expira(curent)
    assert [symbol for symbol, _, _ in expirate] == ["INTC"]
    assert not any(key[0] == "INTC" for key in aggregator.bars)


def test_barele_respinse_sunt_totusi_scrise(tmp_path, monkeypatch):
    import ofi_vwap_shadow as module

    monkeypatch.setattr(module, "OFI_FILE", str(tmp_path / "bars.csv"))
    bucket = datetime(2026, 8, 26, 14, 0, tzinfo=timezone.utc)
    bar = {"ofi": 10.0, "bid_volume": 100.0, "ask_volume": 100.0, "quotes": 2}
    assert not proceseaza_bar(None, "AAPL", bucket, bar)
    rand = list(csv.DictReader((tmp_path / "bars.csv").open(encoding="utf-8")))[0]
    assert rand["motiv"] == "quote-uri insuficiente"
    assert rand["quotes"] == "2"


def test_csv_ul_se_roteste_cand_se_schimba_schema(tmp_path, monkeypatch):
    import ofi_vwap_shadow as module

    path = tmp_path / "bars.csv"
    path.write_text("timestamp,simbol,ofi\n2026-08-26T13:00:00,AAPL,1.0\n", encoding="utf-8")
    monkeypatch.setattr(module, "OFI_FILE", str(path))
    bucket = datetime(2026, 8, 26, 14, 0, tzinfo=timezone.utc)
    proceseaza_bar(None, "AAPL", bucket, {"ofi": 1.0, "bid_volume": 1.0, "ask_volume": 1.0, "quotes": 1})
    assert (tmp_path / "bars.csv.bak").exists()
    assert path.read_text(encoding="utf-8").splitlines()[0] == ",".join(module.OFI_FIELDS)


def quote_cu_nanosecunde(nanosecunde, minute=0):
    import pandas as pd

    moment = pd.Timestamp(2026, 8, 26, 13, minute, tz="UTC") + pd.Timedelta(nanosecunde, unit="ns")
    return SimpleNamespace(bid_price=100, ask_price=100.1, bid_size=10, ask_size=10,
                           symbol="AAPL", timestamp=moment)


def test_nanosecundele_nu_creeaza_bucket_uri_separate():
    aggregator = OfiAggregator()
    primul = aggregator.update("AAPL", quote_cu_nanosecunde(146))
    al_doilea = aggregator.update("AAPL", quote_cu_nanosecunde(999))
    assert primul == al_doilea
    assert len(aggregator.bars) == 1
    assert aggregator.bars[("AAPL", primul)]["quotes"] == 2


def test_quote_intarziat_nu_redeschide_o_bara_scrisa():
    aggregator = OfiAggregator()
    aggregator.update("AAPL", quote(100, 100.1, 10, 10, 0))
    curent = aggregator.update("AAPL", quote(100, 100.1, 10, 10, 1))
    assert len(aggregator.expira(curent)) == 1
    aggregator.update("AAPL", quote(100, 100.1, 10, 10, 0))
    assert not any(bucket.minute == 0 for _, bucket in aggregator.bars)


def test_sesiunea_regulara_taie_pre_si_after_market():
    assert not in_sesiune_regulara(datetime(2026, 8, 26, 12, 1, tzinfo=timezone.utc))   # 08:01 NY
    assert in_sesiune_regulara(datetime(2026, 8, 26, 13, 30, tzinfo=timezone.utc))      # 09:30 NY
    assert in_sesiune_regulara(datetime(2026, 8, 26, 19, 59, tzinfo=timezone.utc))      # 15:59 NY
    assert not in_sesiune_regulara(datetime(2026, 8, 26, 20, 0, tzinfo=timezone.utc))   # 16:00 NY
    assert not in_sesiune_regulara(datetime(2026, 8, 26, 20, 59, tzinfo=timezone.utc))  # 16:59 NY


def test_bara_din_afara_sesiunii_nu_devine_candidat(tmp_path, monkeypatch):
    import ofi_vwap_shadow as module

    monkeypatch.setattr(module, "OFI_FILE", str(tmp_path / "bars.csv"))
    # Exact forma candidatilor falsi din productie: after-hours, ask_size 0 pe toata bara.
    bucket = datetime(2026, 8, 26, 20, 20, tzinfo=timezone.utc)
    bar = {"ofi": 1700.0, "bid_volume": 5200.0, "ask_volume": 0.0, "quotes": 52}
    assert not proceseaza_bar(None, "NVDA", bucket, bar)
    rand = list(csv.DictReader((tmp_path / "bars.csv").open(encoding="utf-8")))[0]
    assert rand["motiv"] == "in afara sesiunii"


def test_aceeasi_bara_in_sesiune_ramane_candidat(tmp_path, monkeypatch):
    import ofi_vwap_shadow as module

    monkeypatch.setattr(module, "OFI_FILE", str(tmp_path / "bars.csv"))
    monkeypatch.setattr(module, "descarca_bare", lambda api, simbol: None)
    bucket = datetime(2026, 8, 26, 15, 20, tzinfo=timezone.utc)
    bar = {"ofi": 1700.0, "bid_volume": 5200.0, "ask_volume": 0.0, "quotes": 52}
    assert not proceseaza_bar(None, "NVDA", bucket, bar)  # fara bare, VWAP nu confirma
    rand = list(csv.DictReader((tmp_path / "bars.csv").open(encoding="utf-8")))[0]
    assert rand["motiv"] == "candidat"


def eroare_de_stream():
    return logging.LogRecord("alpaca_trade_api.stream", logging.ERROR, __file__, 0,
                             "error during websocket communication: connection limit exceeded",
                             None, None)


def test_garda_iese_dupa_prea_multe_erori_de_reconectare():
    iesiri = []
    garda = OpresteFurtunaDeReconectari(maxim=3, fereastra=60, iesire=lambda: iesiri.append(1))
    for _ in range(2):
        garda.emit(eroare_de_stream())
    assert not iesiri
    garda.emit(eroare_de_stream())
    assert iesiri == [1]


def test_garda_ignora_erorile_izolate_din_ferestre_diferite(monkeypatch):
    iesiri = []
    ceas = [0.0]
    monkeypatch.setattr(module_time, "monotonic", lambda: ceas[0])
    garda = OpresteFurtunaDeReconectari(maxim=3, fereastra=60, iesire=lambda: iesiri.append(1))
    for _ in range(10):
        garda.emit(eroare_de_stream())
        ceas[0] += 61
    assert not iesiri
