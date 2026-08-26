#!/usr/bin/env python3
"""VWAP Pullback in shadow mode. Never submits broker orders."""
import argparse
import csv
import json
import os
import time
from datetime import datetime, time as ora_zi, timedelta, timezone
from zoneinfo import ZoneInfo

import alpaca_trade_api as tradeapi
import pandas as pd
from dotenv import load_dotenv

FOLDER = os.path.dirname(os.path.abspath(__file__))
NY_TZ = ZoneInfo("America/New_York")
SIGNALS_FILE = os.path.join(FOLDER, "vwap_shadow_signals.csv")
STATE_FILE = os.path.join(FOLDER, "vwap_shadow_state.json")

load_dotenv(os.path.join(FOLDER, ".env"))
API_KEY = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
ACTIUNI = [s.strip().upper() for s in os.getenv("ACTIUNI", "AAPL,MSFT,NVDA").split(",") if s.strip()]
SCAN_INTERVAL_SEC = int(os.getenv("VWAP_SHADOW_INTERVAL_SEC", "300"))
PROXIMITATE_VWAP_PCT = float(os.getenv("VWAP_PROXIMITATE_PCT", "0.005"))
VOLUM_MIN_MULTIPLIER = float(os.getenv("VWAP_VOLUM_MIN_MULTIPLIER", "1.2"))
DURATA_BARA_MIN = int(os.getenv("VWAP_DURATA_BARA_MIN", "5"))
VECHIME_MAXIMA_MIN = float(os.getenv("VWAP_VECHIME_MAXIMA_MIN", "15"))
WARMUP_SESIUNE_MIN = float(os.getenv("VWAP_WARMUP_MIN", "30"))
MIN_BARE = 50
SESIUNE_START = ora_zi(9, 30)
SESIUNE_STOP = ora_zi(16, 0)


def index_utc(df):
    """Normalizes the bar index to tz-aware UTC; Alpaca sometimes returns naive stamps."""
    index = pd.DatetimeIndex(df.index)
    return index.tz_localize(timezone.utc) if index.tz is None else index.tz_convert(timezone.utc)


def doar_sesiune_regulara(df):
    """Keeps only 09:30-16:00 NY bars.

    Pre-market bars on IEX are thin and would anchor the session VWAP on a handful of
    near-zero-volume prints, which made every bar right after the open look like a pullback.
    """
    if df.empty:
        return df
    local = index_utc(df).tz_convert(NY_TZ)
    return df[(local.time >= SESIUNE_START) & (local.time < SESIUNE_STOP)]


def elimina_bara_incompleta(df, acum):
    """Drops the still-forming bar — its Close and Volume keep changing after we read them."""
    if df.empty:
        return df
    if index_utc(df)[-1] + timedelta(minutes=DURATA_BARA_MIN) > acum:
        return df.iloc[:-1]
    return df


def minute_de_la_deschidere(moment):
    local = moment.astimezone(NY_TZ)
    return (local.hour - SESIUNE_START.hour) * 60 + local.minute - SESIUNE_START.minute


def calculeaza_vwap(df):
    """Calculates session VWAP, resetting at each New York trading date."""
    if df.empty:
        return pd.Series(dtype=float, index=df.index)
    prices = (df["High"] + df["Low"] + df["Close"]) / 3
    sessions = index_utc(df).tz_convert(NY_TZ).date
    volume = df["Volume"].astype(float).clip(lower=0)
    value = prices * volume
    return value.groupby(sessions).cumsum() / volume.groupby(sessions).cumsum().replace(0, float("nan"))


def pregateste_bare(df):
    """Adds VWAP and confirmation indicators without mutating the input."""
    result = df.copy()
    result["VWAP"] = calculeaza_vwap(result)
    result["EMA20"] = result["Close"].ewm(span=20, adjust=False).mean()
    result["EMA50"] = result["Close"].ewm(span=50, adjust=False).mean()
    result["VolumeMA20"] = result["Volume"].rolling(20).mean()
    return result


def analizeaza_pullback(df, acum=None):
    """Returns (accepted, reason, details, bar_time) for the last completed session bar."""
    if df is None or df.empty:
        return False, "date insuficiente", {}, None
    acum = acum or datetime.now(timezone.utc)
    sesiune = elimina_bara_incompleta(doar_sesiune_regulara(df), acum)
    if len(sesiune) < MIN_BARE:
        return False, "date insuficiente", {}, None
    bar_time = index_utc(sesiune)[-1]
    if acum - bar_time > timedelta(minutes=VECHIME_MAXIMA_MIN):
        return False, "bara invechita", {}, bar_time
    if minute_de_la_deschidere(bar_time) < WARMUP_SESIUNE_MIN:
        return False, "warm-up sesiune", {}, bar_time
    data = pregateste_bare(sesiune)
    ultima = data.iloc[-1]
    if pd.isna(ultima[["VWAP", "EMA20", "EMA50", "VolumeMA20"]]).any():
        return False, "indicatori insuficienti", {}, bar_time
    proximity = abs(ultima["Close"] - ultima["VWAP"]) / ultima["VWAP"]
    volume_ok = ultima["Volume"] >= ultima["VolumeMA20"] * VOLUM_MIN_MULTIPLIER
    trend_ok = ultima["Close"] > ultima["VWAP"] and ultima["EMA20"] > ultima["EMA50"]
    confirmation_ok = ultima["Close"] > ultima["Open"] and ultima["Low"] <= ultima["VWAP"] * (1 + PROXIMITATE_VWAP_PCT)
    accepted = trend_ok and proximity <= PROXIMITATE_VWAP_PCT and volume_ok and confirmation_ok
    details = {
        "close": round(float(ultima["Close"]), 4),
        "vwap": round(float(ultima["VWAP"]), 4),
        "ema20": round(float(ultima["EMA20"]), 4),
        "ema50": round(float(ultima["EMA50"]), 4),
        "volume": int(ultima["Volume"]),
        "volume_ma20": round(float(ultima["VolumeMA20"]), 2),
        "proximity_pct": round(proximity * 100, 3),
    }
    return accepted, "VWAP pullback confirmat" if accepted else "filtre neconfirmate", details, bar_time


def incarca_stare():
    try:
        with open(STATE_FILE, encoding="utf-8") as file:
            return json.load(file)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def salveaza_stare(state):
    temporary = STATE_FILE + ".tmp"
    with open(temporary, "w", encoding="utf-8") as file:
        json.dump(state, file, indent=2)
    os.replace(temporary, STATE_FILE)


def scrie_semnal(simbol, bar_time, details):
    state = incarca_stare()
    marker = str(bar_time)
    if state.get(simbol) == marker:
        return False
    exists = os.path.exists(SIGNALS_FILE)
    with open(SIGNALS_FILE, "a", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=["timestamp", "simbol", *details.keys()])
        if not exists:
            writer.writeheader()
        writer.writerow({"timestamp": marker, "simbol": simbol, **details})
    state[simbol] = marker
    salveaza_stare(state)
    return True


def descarca_bare(api, simbol):
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    bars = api.get_bars(simbol, "5Min", start=start, end=now.strftime("%Y-%m-%dT%H:%M:%SZ"), feed="iex").df
    if bars is None or bars.empty:
        return None
    return bars.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"})[["Open", "High", "Low", "Close", "Volume"]]


def ruleaza_scanare(api, simboluri):
    semnale = 0
    acum = datetime.now(timezone.utc)
    for simbol in simboluri:
        try:
            df = descarca_bare(api, simbol)
            ok, motiv, details, bar_time = analizeaza_pullback(df, acum)
            if ok and scrie_semnal(simbol, bar_time, details):
                print(f"SHADOW SIGNAL {simbol}: {motiv} | {details}", flush=True)
                semnale += 1
        except Exception as error:
            print(f"Eroare shadow {simbol}: {error}", flush=True)
    return semnale


def main():
    parser = argparse.ArgumentParser(description="VWAP Pullback shadow scanner")
    parser.add_argument("--once", action="store_true", help="ruleaza un singur ciclu")
    args = parser.parse_args()
    api = tradeapi.REST(API_KEY, SECRET_KEY, BASE_URL, api_version="v2")
    print(f"VWAP PULLBACK SHADOW | {len(ACTIUNI)} simboluri | interval {SCAN_INTERVAL_SEC}s", flush=True)
    while True:
        if api.get_clock().is_open:
            ruleaza_scanare(api, ACTIUNI)
        else:
            print("Bursa inchisa - shadow in asteptare", flush=True)
        if args.once:
            return
        time.sleep(SCAN_INTERVAL_SEC)


if __name__ == "__main__":
    main()
