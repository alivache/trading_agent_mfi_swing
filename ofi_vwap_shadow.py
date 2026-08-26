#!/usr/bin/env python3
"""OFI + VWAP shadow scanner. Never submits broker orders."""
import csv
import os
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import alpaca_trade_api as tradeapi
from dotenv import load_dotenv

from vwap_pullback_strategy import ACTIUNI, analizeaza_pullback, descarca_bare

FOLDER = os.path.dirname(os.path.abspath(__file__))
NY_TZ = ZoneInfo("America/New_York")
SIGNALS_FILE = os.path.join(FOLDER, "ofi_vwap_shadow_signals.csv")
OFI_FILE = os.path.join(FOLDER, "ofi_shadow_bars.csv")
OFI_MIN_RATIO = float(os.getenv("OFI_MIN_RATIO", "0.25"))
MIN_QUOTES = int(os.getenv("OFI_MIN_QUOTES", "10"))


def contributie_ofi(previous, quote):
    """Returns the Cont-Kukanov OFI contribution for one quote update."""
    bid = float(quote.bid_price)
    ask = float(quote.ask_price)
    bid_size = float(quote.bid_size)
    ask_size = float(quote.ask_size)
    if previous is None:
        return 0.0, {"bid": bid, "ask": ask, "bid_size": bid_size, "ask_size": ask_size}
    bid_flow = (bid_size if bid > previous["bid"] else
                -previous["bid_size"] if bid < previous["bid"] else
                bid_size - previous["bid_size"])
    ask_flow = (-ask_size if ask < previous["ask"] else
                previous["ask_size"] if ask > previous["ask"] else
                -(ask_size - previous["ask_size"]))
    state = {"bid": bid, "ask": ask, "bid_size": bid_size, "ask_size": ask_size}
    return bid_flow + ask_flow, state


def ratio_ofi(ofi, bid_volume, ask_volume):
    denominator = bid_volume + ask_volume
    return ofi / denominator if denominator else 0.0


def minutul_quote(quote):
    timestamp = getattr(quote, "timestamp", None)
    if timestamp is None:
        timestamp = datetime.now(timezone.utc)
    return timestamp.astimezone(NY_TZ).replace(second=0, microsecond=0)


class OfiAggregator:
    def __init__(self):
        self.previous = {}
        self.bars = defaultdict(self._new_bar)

    @staticmethod
    def _new_bar():
        return {"ofi": 0.0, "bid_volume": 0.0, "ask_volume": 0.0, "quotes": 0}

    def update(self, symbol, quote):
        bucket = minutul_quote(quote)
        key = (symbol, bucket)
        flow, state = contributie_ofi(self.previous.get(symbol), quote)
        self.previous[symbol] = state
        bar = self.bars[key]
        bar["ofi"] += flow
        bar["bid_volume"] += float(quote.bid_size)
        bar["ask_volume"] += float(quote.ask_size)
        bar["quotes"] += 1
        return bucket

    def finalize(self, symbol, bucket):
        return self.bars.pop((symbol, bucket), None)


def scrie_bar_ofi(symbol, bucket, bar):
    exists = os.path.exists(OFI_FILE)
    with open(OFI_FILE, "a", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=["timestamp", "simbol", "ofi", "bid_volume", "ask_volume", "quotes", "ofi_ratio"])
        if not exists:
            writer.writeheader()
        writer.writerow({"timestamp": bucket.isoformat(), "simbol": symbol, **bar,
                         "ofi_ratio": round(ratio_ofi(bar["ofi"], bar["bid_volume"], bar["ask_volume"]), 6)})


def proceseaza_bar(api, symbol, bucket, bar):
    if bar is None or bar["quotes"] < MIN_QUOTES:
        return False
    ofi_ratio = ratio_ofi(bar["ofi"], bar["bid_volume"], bar["ask_volume"])
    scrie_bar_ofi(symbol, bucket, bar)
    if ofi_ratio < OFI_MIN_RATIO:
        return False
    data = descarca_bare(api, symbol)
    ok, _, details = analizeaza_pullback(data)
    if not ok:
        return False
    exists = os.path.exists(SIGNALS_FILE)
    with open(SIGNALS_FILE, "a", newline="", encoding="utf-8") as file:
        fields = ["timestamp", "simbol", "ofi_ratio", *details.keys()]
        writer = csv.DictWriter(file, fieldnames=fields)
        if not exists:
            writer.writeheader()
        writer.writerow({"timestamp": bucket.isoformat(), "simbol": symbol,
                         "ofi_ratio": round(ofi_ratio, 6), **details})
    print(f"OFI+VWAP SHADOW SIGNAL {symbol}: ratio={ofi_ratio:.3f}", flush=True)
    return True


def main():
    load_dotenv(os.path.join(FOLDER, ".env"))
    api = tradeapi.REST(os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY"),
                        os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets"), api_version="v2")
    stream = tradeapi.Stream(os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY"),
                             data_feed="iex")
    aggregator = OfiAggregator()

    async def on_quote(quote):
        symbol = quote.symbol
        current = aggregator.update(symbol, quote)
        for key in list(aggregator.bars):
            if key[0] == symbol and key[1] < current:
                old_bucket = key[1]
                proceseaza_bar(api, symbol, old_bucket, aggregator.finalize(symbol, old_bucket))

    stream.subscribe_quotes(on_quote, *ACTIUNI)
    print(f"OFI + VWAP SHADOW | {len(ACTIUNI)} simboluri | feed IEX", flush=True)
    stream.run()


if __name__ == "__main__":
    main()
