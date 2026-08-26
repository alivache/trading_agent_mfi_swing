import csv
from datetime import datetime, timezone
from types import SimpleNamespace

from ofi_vwap_shadow import OfiAggregator, contributie_ofi, proceseaza_bar, ratio_ofi


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
    bucket = datetime(2026, 8, 26, 13, 0, tzinfo=timezone.utc)
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
    bucket = datetime(2026, 8, 26, 13, 0, tzinfo=timezone.utc)
    proceseaza_bar(None, "AAPL", bucket, {"ofi": 1.0, "bid_volume": 1.0, "ask_volume": 1.0, "quotes": 1})
    assert (tmp_path / "bars.csv.bak").exists()
    assert path.read_text(encoding="utf-8").splitlines()[0] == ",".join(module.OFI_FIELDS)
