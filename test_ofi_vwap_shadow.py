from datetime import datetime, timezone
from types import SimpleNamespace

from ofi_vwap_shadow import OfiAggregator, contributie_ofi, ratio_ofi


def quote(bid, ask, bid_size, ask_size, minute=0):
    return SimpleNamespace(
        bid_price=bid,
        ask_price=ask,
        bid_size=bid_size,
        ask_size=ask_size,
        symbol="AAPL",
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
