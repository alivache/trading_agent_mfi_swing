#!/usr/bin/env python3
"""
Genereaza raportul CSV pentru ziua curenta.
Rulat de cron la 16:15 ET (15 min dupa inchiderea bursei).
VM-ul e pe America/New_York, deci date.today() = ziua de tranzactionare.
"""
import os
import json
import csv
from datetime import date, datetime

FOLDER = "/home/liviu_anton/trading"
MEMORIE_FILE = os.path.join(FOLDER, "memorie_multitf.json")


def main():
    zi = date.today().isoformat()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if not os.path.exists(MEMORIE_FILE):
        print(f"[{timestamp}] Memoria nu exista. Skip.")
        return

    with open(MEMORIE_FILE, "r") as f:
        memorie = json.load(f)

    tranzactii = memorie.get("tranzactii", [])
    inchideri = [
        t for t in tranzactii
        if t["tip"] == "close_long"
        and t.get("profit") is not None
        and t["data"].startswith(zi)
    ]

    if not inchideri:
        print(f"[{timestamp}] Nicio inchidere pentru {zi}. Skip.")
        return

    CSV_FILE = os.path.join(FOLDER, f"multitf_trades_{zi}.csv")
    campuri = [
        "data_iesire", "simbol", "cantitate",
        "pret_intrare", "pret_iesire",
        "profit_usd", "profit_pct",
        "motiv_exit", "rezultat"
    ]

    rows = []
    for t in inchideri:
        simbol = t["simbol"]
        pret_intrare = 0
        for d in reversed(tranzactii):
            if d["simbol"] == simbol and d["tip"] == "open_long" and d["data"] < t["data"]:
                pret_intrare = d["pret"]
                break
        profit = t["profit"]
        profit_pct = (profit / (pret_intrare * t["cantitate"]) * 100) if pret_intrare > 0 else 0
        rows.append({
            "data_iesire": t["data"],
            "simbol": simbol,
            "cantitate": t["cantitate"],
            "pret_intrare": round(pret_intrare, 4),
            "pret_iesire": round(t["pret"], 4),
            "profit_usd": round(profit, 2),
            "profit_pct": round(profit_pct, 2),
            "motiv_exit": t.get("motiv", ""),
            "rezultat": "WIN" if profit > 0 else "LOSS"
        })

    with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=campuri)
        w.writeheader()
        w.writerows(rows)

    wins = sum(1 for r in rows if r["rezultat"] == "WIN")
    profit_total = sum(r["profit_usd"] for r in rows)
    print(f"[{timestamp}] Raport {zi}: {len(rows)} trades | "
          f"{wins} wins ({wins/len(rows)*100:.0f}%) | Profit ${profit_total:.2f}")


if __name__ == "__main__":
    main()
