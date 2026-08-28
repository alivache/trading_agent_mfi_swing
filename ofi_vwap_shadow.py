#!/usr/bin/env python3
"""OFI + VWAP shadow scanner. Never submits broker orders."""
import asyncio
import csv
import logging
import os
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import alpaca_trade_api as tradeapi
from dotenv import load_dotenv

from vwap_pullback_strategy import (ACTIUNI, SESIUNE_START, SESIUNE_STOP,
                                    analizeaza_pullback, descarca_bare)

FOLDER = os.path.dirname(os.path.abspath(__file__))
NY_TZ = ZoneInfo("America/New_York")
SIGNALS_FILE = os.path.join(FOLDER, "ofi_vwap_shadow_signals.csv")
OFI_FILE = os.path.join(FOLDER, "ofi_shadow_bars.csv")
OFI_FIELDS = ["timestamp", "simbol", "ofi", "bid_volume", "ask_volume", "quotes", "ofi_ratio", "motiv"]

load_dotenv(os.path.join(FOLDER, ".env"))
OFI_MIN_RATIO = float(os.getenv("OFI_MIN_RATIO", "0.25"))
MIN_QUOTES = int(os.getenv("OFI_MIN_QUOTES", "10"))
STREAM_ERORI_MAX = int(os.getenv("OFI_STREAM_ERORI_MAX", "20"))
STREAM_FEREASTRA_SEC = float(os.getenv("OFI_STREAM_FEREASTRA_SEC", "60"))


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


def in_sesiune_regulara(bucket):
    """True doar pentru minutele 09:30-16:00 NY.

    Pre-market si after-hours pe IEX au cotatii atat de rare incat o bara intreaga poate avea
    ask_size 0 pe toata durata ei; ratio_ofi imparte atunci doar la bid_volume si iese mecanic
    peste prag. Toti candidatii inregistrati pana acum erau exact asta -- artefacte din afara
    sesiunii, niciunul confirmat vreodata de VWAP.
    """
    return SESIUNE_START <= bucket.astimezone(NY_TZ).time() < SESIUNE_STOP


def ratio_ofi(ofi, bid_volume, ask_volume):
    denominator = bid_volume + ask_volume
    return ofi / denominator if denominator else 0.0


def minutul_quote(quote):
    """Floors the quote timestamp to the New York minute.

    Alpaca trimite Timestamp-uri pandas cu precizie de nanosecunda, iar `replace(microsecond=0)`
    nu atinge campul `nanosecond`: fiecare quote primea propriul bucket, deci agregarea pe minut
    nu se intampla deloc. Construim un datetime curat din campurile calendaristice.
    """
    timestamp = getattr(quote, "timestamp", None)
    if timestamp is None:
        timestamp = datetime.now(timezone.utc)
    local = timestamp.astimezone(NY_TZ)
    return datetime(local.year, local.month, local.day, local.hour, local.minute, tzinfo=NY_TZ)


class OfiAggregator:
    def __init__(self):
        self.previous = {}
        self.bars = defaultdict(self._new_bar)
        self.inchise = {}

    @staticmethod
    def _new_bar():
        return {"ofi": 0.0, "bid_volume": 0.0, "ask_volume": 0.0, "quotes": 0}

    def update(self, symbol, quote):
        bucket = minutul_quote(quote)
        ultima_inchisa = self.inchise.get(symbol)
        if ultima_inchisa is not None and bucket <= ultima_inchisa:
            return ultima_inchisa  # quote intarziat: bara lui e deja scrisa, n-o redeschidem
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

    def expira(self, curent):
        """Closes every bar older than the current minute, on all symbols.

        Flushing only the symbol that just ticked left quiet symbols' bars stranded in
        memory — never written, never evaluated.
        """
        vechi = [key for key in self.bars if key[1] < curent]
        expirate = []
        for symbol, bucket in vechi:
            self.inchise[symbol] = max(bucket, self.inchise.get(symbol, bucket))
            expirate.append((symbol, bucket, self.bars.pop((symbol, bucket))))
        return expirate


def scrie_rand(path, fields, rand):
    """Appends one row, rotating the file when its header no longer matches the schema."""
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as file:
            if next(csv.reader(file), None) != fields:
                os.replace(path, path + ".bak")
    exists = os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        if not exists:
            writer.writeheader()
        writer.writerow(rand)


def scrie_bar_ofi(symbol, bucket, bar, ofi_ratio, motiv):
    scrie_rand(OFI_FILE, OFI_FIELDS, {"timestamp": bucket.isoformat(), "simbol": symbol, **bar,
                                      "ofi_ratio": round(ofi_ratio, 6), "motiv": motiv})


def proceseaza_bar(api, symbol, bucket, bar, acum=None):
    if bar is None:
        return False
    ofi_ratio = ratio_ofi(bar["ofi"], bar["bid_volume"], bar["ask_volume"])
    if not in_sesiune_regulara(bucket):
        motiv = "in afara sesiunii"
    elif bar["quotes"] < MIN_QUOTES:
        motiv = "quote-uri insuficiente"
    elif ofi_ratio < OFI_MIN_RATIO:
        motiv = "ofi sub prag"
    else:
        motiv = "candidat"
    # Toate barele se scriu, inclusiv cele respinse: altfel nu se poate distinge
    # "n-au fost date" de "a fost filtrat" si pragurile nu se pot calibra.
    scrie_bar_ofi(symbol, bucket, bar, ofi_ratio, motiv)
    if motiv != "candidat":
        return False
    data = descarca_bare(api, symbol)
    ok, _, details, _ = analizeaza_pullback(data, acum)
    if not ok:
        return False
    scrie_rand(SIGNALS_FILE, ["timestamp", "simbol", "ofi_ratio", *details.keys()],
               {"timestamp": bucket.isoformat(), "simbol": symbol,
                "ofi_ratio": round(ofi_ratio, 6), **details})
    print(f"OFI+VWAP SHADOW SIGNAL {symbol}: ratio={ofi_ratio:.3f}", flush=True)
    return True


class OpresteFurtunaDeReconectari(logging.Handler):
    """Termina procesul cand stream-ul Alpaca intra in bucla de reconectari esuate.

    `_start_ws` ridica `ValueError("connection limit exceeded")` la autentificare. Nu e
    `WebSocketException`, deci `_run_forever` din alpaca-trade-api cade pe ramura generica
    `except Exception`, care NU apeleaza `close()` si nu reseteaza `_running` -- socket-ul
    ramas deschis reintra in bucla dupa `sleep(0.01)`. Rezultatul e ~100 de conexiuni noi pe
    secunda, niciuna inchisa: bucla isi intretine singura conditia care o declanseaza si nu
    iese din ea (pe 28 august a mancat doua treimi din prima ora de sesiune).

    Iesim cu `os._exit` pentru ca suntem pe event-loop-ul stream-ului, in interiorul unui
    `except Exception` care ar inghiti orice exceptie obisnuita. systemd (Restart=always,
    RestartSec) reporneste un proces curat dupa ce brokerul a eliberat conexiunile vechi.
    """

    def __init__(self, maxim=None, fereastra=None, iesire=None):
        super().__init__(level=logging.ERROR)
        self.maxim = STREAM_ERORI_MAX if maxim is None else maxim
        self.fereastra = STREAM_FEREASTRA_SEC if fereastra is None else fereastra
        self.iesire = iesire or (lambda: os._exit(1))
        self.momente = deque()

    def emit(self, record):
        acum = time.monotonic()
        self.momente.append(acum)
        while self.momente and acum - self.momente[0] > self.fereastra:
            self.momente.popleft()
        if len(self.momente) < self.maxim:
            return
        print(f"Stream OFI: {len(self.momente)} erori in {self.fereastra:.0f}s "
              f"({record.getMessage()}) - iesim, systemd reporneste", flush=True)
        self.momente.clear()
        self.iesire()


def main():
    api = tradeapi.REST(os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY"),
                        os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets"), api_version="v2")
    stream = tradeapi.Stream(os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY"),
                             data_feed="iex")
    aggregator = OfiAggregator()
    logging.getLogger("alpaca_trade_api.stream").addHandler(OpresteFurtunaDeReconectari())

    async def on_quote(quote):
        try:
            current = aggregator.update(quote.symbol, quote)
            for symbol, bucket, bar in aggregator.expira(current):
                # descarca_bare face HTTP blocant: pe event-loop ar opri stream-ul de quote-uri.
                await asyncio.to_thread(proceseaza_bar, api, symbol, bucket, bar)
        except Exception as error:
            print(f"Eroare OFI shadow: {error}", flush=True)

    stream.subscribe_quotes(on_quote, *ACTIUNI)
    print(f"OFI + VWAP SHADOW | {len(ACTIUNI)} simboluri | feed IEX", flush=True)
    stream.run()


if __name__ == "__main__":
    main()
