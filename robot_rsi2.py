"""Robotul H3: revenire pe termen scurt pe SPY si QQQ (RSI pe 2 zile). DOAR PAPER.

Regula e exact cea testata in cercetare/IPOTEZE.md (H3, singura care a trecut pe 2016-2026):
  - intrare: RSI(2) < 10 si pretul peste media pe 200 de zile -> cumparare la inchidere;
  - iesire:  pretul peste media pe 5 zile                     -> vanzare la inchidere;
  - o singura pozitie pe simbol, fara stop (cea mai proasta tranzactie din test: -7.5%).

Cum ruleaza o zi de bursa:
  1. la inchidere - 16 min (15:44 ET; 12:44 in zilele scurte) calculeaza semnalul cu pretul
     curent pe post de "inchidere" si trimite ordine `cls` (market on close). Alpaca le
     accepta pana la inchidere - 10 min; dupa aceea le respinge;
  2. la inchidere + 20 min verifica executiile, actualizeaza pozitiile si recalculeaza
     semnalul cu inchiderea oficiala, ca sa masuram cat difera live de test.

Siguranta:
  - adresa de paper e scrisa aici, nu citita din configurare; daca .env indica alt cont,
    robotul se opreste inainte de orice ordin;
  - robotul gestioneaza doar actiunile pe care le-a cumparat el (starea pe disc) si
    verifica la broker ca exista; la nepotrivire nu tranzactioneaza simbolul respectiv;
  - fiecare ordin are un identificator unic pe zi si simbol: o repornire nu trimite dublu.

Rulare (in WSL):
    ./venv/bin/python robot_rsi2.py --serviciu      # bucla zilnica (asa ruleaza pe VM)
    ./venv/bin/python robot_rsi2.py --stare         # starea curenta si semnalul de azi, fara ordine
"""
import argparse
import json
import math
import os
import sys
import time
from datetime import date, datetime, timedelta

import pandas as pd
from dotenv import load_dotenv

AICI = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(AICI, ".env"))

from analyst import NY                                   # noqa: E402

URL_PAPER = "https://paper-api.alpaca.markets"           # fixat in cod, intentionat
SIMBOLURI = ["SPY", "QQQ"]
SUMA_PE_SIMBOL_USD = 10_000                               # ales de user pe 14 nov 2026
RSI_PRAG = 10
SMA_LUNG, SMA_SCURT = 200, 5
DECIZIE_INAINTE = timedelta(minutes=16)                   # 15:44 ET
LIMITA_CLS_INAINTE = timedelta(minutes=10)                # Alpaca respinge `cls` dupa 15:50 ET
VERIFICARE_DUPA = timedelta(minutes=20)                   # SIP intarziat 15 min -> bara zilnica completa
ZILE_ISTORIC = 420                                        # calendaristice: ~290 zile de bursa > 200
INTARZIERE_SIP = timedelta(minutes=16)                    # planul gratuit: SIP doar mai vechi de 15 min
DIR_JURNAL = os.path.join(AICI, "jurnal_robot_rsi2")
PAUZA_BUCLA_S = 20


class EroareCont(RuntimeError):
    pass


def verifica_paper() -> None:
    """Oprire inainte de orice ordin daca configurarea indica alt cont decat paper."""
    configurat = os.getenv("ALPACA_BASE_URL", "").strip().rstrip("/")
    if configurat and configurat != URL_PAPER:
        raise EroareCont(f"ALPACA_BASE_URL nu e contul paper ({configurat!r}); robotul refuza sa porneasca")


# ---------------------------------------------------------------- regula (functii pure)

def rsi_wilder(inchideri: pd.Series, n: int = 2) -> pd.Series:
    d = inchideri.diff()
    castig = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    pierdere = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    return 100 - 100 / (1 + castig / pierdere)


def semnal(inchideri: list[float], in_pozitie: bool) -> dict:
    """Decizia pentru ultima valoare din `inchideri` (care poate fi pretul curent).

    Intoarce {"actiune": "cumpara" | "vinde" | "nimic", plus indicatorii}. Aceeasi logica,
    pas cu pas, ca `h3_rsi2` din cercetare/backtest_zilnic.py (verificat in teste).
    """
    s = pd.Series(inchideri, dtype=float)
    if len(s) < SMA_LUNG:
        return {"actiune": "nimic", "motiv": f"istoric insuficient ({len(s)} < {SMA_LUNG} zile)"}
    rsi = float(rsi_wilder(s).iloc[-1])
    sma_lung = float(s.iloc[-SMA_LUNG:].mean())
    sma_scurt = float(s.iloc[-SMA_SCURT:].mean())
    pret = float(s.iloc[-1])
    ind = {"pret": round(pret, 4), "rsi2": round(rsi, 2), "sma200": round(sma_lung, 4),
           "sma5": round(sma_scurt, 4)}
    if not in_pozitie and rsi < RSI_PRAG and pret > sma_lung:
        return {"actiune": "cumpara", **ind}
    if in_pozitie and pret > sma_scurt:
        return {"actiune": "vinde", **ind}
    return {"actiune": "nimic", **ind}


def cantitate(pret: float, suma: float = SUMA_PE_SIMBOL_USD) -> int:
    return int(suma // pret) if pret > 0 else 0


# ---------------------------------------------------------------- brokerul (izolat, ca testele sa-l poata inlocui)

class BrokerAlpaca:
    def __init__(self):
        verifica_paper()
        import alpaca_trade_api as tradeapi
        import gap_scanner
        self.api = tradeapi.REST(os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY"), URL_PAPER)
        self.date = gap_scanner.ClientDate()
        self._bare = gap_scanner.bare

    def sesiune(self, zi: date) -> tuple[datetime, datetime] | None:
        cal = self.date.get("/v2/calendar", {"start": zi.isoformat(), "end": zi.isoformat()}, trading=True)
        if not cal or cal[0]["date"] != zi.isoformat():
            return None
        return (datetime.fromisoformat(f"{zi}T{cal[0]['open']}").replace(tzinfo=NY),
                datetime.fromisoformat(f"{zi}T{cal[0]['close']}").replace(tzinfo=NY))

    def inchideri_zilnice(self, simbol: str, pana_la: datetime) -> pd.Series:
        """Inchideri ajustate complet (split + dividende), ca in backtest.

        Planul gratuit refuza (403) datele SIP mai noi de 15 minute, deci cererea se opreste
        cu 16 minute in urma (gasit la prima rulare reala, 14 nov 2026).
        """
        sfarsit = min(pana_la, datetime.now(NY) - INTARZIERE_SIP)
        b = self._bare(self.date, [simbol], "1Day", pana_la - timedelta(days=ZILE_ISTORIC), sfarsit,
                       ajustare="all")[simbol]
        idx = [datetime.fromisoformat(x["t"].replace("Z", "+00:00")).astimezone(NY).date() for x in b]
        return pd.Series([x["c"] for x in b], index=idx, dtype=float)

    def pret_curent(self, simbol: str) -> float:
        """Ultima tranzactie IEX (timp real pe planul gratuit; SIP e intarziat 15 min)."""
        return float(self.api.get_latest_trade(simbol, feed="iex").price)

    def cantitate_la_broker(self, simbol: str) -> float:
        try:
            return float(self.api.get_position(simbol).qty)
        except Exception:
            return 0.0

    def trimite(self, simbol: str, parte: str, qty: int, cid: str, tif: str) -> dict:
        o = self.api.submit_order(symbol=simbol, qty=qty, side=parte, type="market",
                                  time_in_force=tif, client_order_id=cid)
        return {"id": o.id, "status": o.status, "tif": o.time_in_force}

    def ordin(self, cid: str) -> dict | None:
        try:
            o = self.api.get_order_by_client_order_id(cid)
        except Exception:
            return None
        return {"id": o.id, "status": o.status, "filled_qty": float(o.filled_qty or 0),
                "pret_mediu": float(o.filled_avg_price) if o.filled_avg_price else None}


# ---------------------------------------------------------------- starea pe disc

def _cale(nume: str) -> str:
    return os.path.join(DIR_JURNAL, nume)


def citeste_stare() -> dict:
    try:
        with open(_cale("stare.json"), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"pozitii": {}, "in_asteptare": {}, "zile": {}}


def scrie_stare(stare: dict) -> None:
    os.makedirs(DIR_JURNAL, exist_ok=True)
    stare["zile"] = {z: stare["zile"][z] for z in sorted(stare["zile"])[-15:]}
    tmp = _cale("stare.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(stare, f, indent=1, default=str)
    os.replace(tmp, _cale("stare.json"))


def jurnal(nume: str, inregistrare: dict) -> None:
    os.makedirs(DIR_JURNAL, exist_ok=True)
    with open(_cale(nume), "a", encoding="utf-8") as f:
        f.write(json.dumps(inregistrare, ensure_ascii=False, default=str) + "\n")


# ---------------------------------------------------------------- pasii zilei

def decide(broker, acum: datetime, inchidere: datetime, stare: dict) -> list[dict]:
    """Pasul 1: semnalul cu pretul curent si ordinele `cls` (cu rezerva `day` daca sunt respinse)."""
    zi = acum.date().isoformat()
    rezultate = []
    stare["in_pozitie_la_decizie"] = {s: s in stare["pozitii"] for s in SIMBOLURI}
    for s in SIMBOLURI:
        poz = stare["pozitii"].get(s)
        r = {"timestamp": acum.isoformat(), "zi": zi, "simbol": s, "pas": "decizie"}
        try:
            la_broker = broker.cantitate_la_broker(s)
            if poz and la_broker < poz["qty"]:
                # nu vindem ce nu exista si nu cumparam peste o pozitie necunoscuta
                r.update(actiune="nimic", alarma=f"broker are {la_broker}, robotul crede {poz['qty']}")
                rezultate.append(r)
                continue
            istoric = broker.inchideri_zilnice(s, acum)
            istoric = istoric[[d < acum.date() for d in istoric.index]]       # doar zilele incheiate
            pret = broker.pret_curent(s)
            sem = semnal(list(istoric.values) + [pret], in_pozitie=poz is not None)
            r.update(sem)
            if sem["actiune"] == "nimic":
                rezultate.append(r)
                continue
            parte = "buy" if sem["actiune"] == "cumpara" else "sell"
            qty = cantitate(pret) if parte == "buy" else int(poz["qty"])
            if qty <= 0:
                r.update(actiune="nimic", motiv="cantitate zero")
                rezultate.append(r)
                continue
            cid = f"rsi2-{s}-{zi}-{parte}"
            existent = next((c for c in (cid, cid + "-day") if broker.ordin(c) is not None), None)
            if existent:                                                       # repornire: deja trimis
                r.update(ordin={"cid": existent, "deja_trimis": True})
            else:
                tif = "cls" if acum < inchidere - LIMITA_CLS_INAINTE else "day"
                try:
                    r["ordin"] = {"cid": cid, "qty": qty, **broker.trimite(s, parte, qty, cid, tif)}
                except Exception as e:
                    if tif != "cls":
                        raise
                    # `cls` respins (ex. trimis prea tarziu): ordin market obisnuit, imediat
                    r["respins_cls"] = f"{type(e).__name__}: {e}"
                    cid += "-day"
                    r["ordin"] = {"cid": cid, "qty": qty, **broker.trimite(s, parte, qty, cid, "day")}
            stare["in_asteptare"][s] = {"cid": r["ordin"]["cid"], "parte": parte, "zi": zi,
                                         "pret_decizie": pret, "indicatori": sem}
        except Exception as e:
            r["eroare"] = f"{type(e).__name__}: {e}"
        rezultate.append(r)
    for r in rezultate:
        jurnal(f"{zi}.jsonl", r)
    return rezultate


def verifica(broker, acum: datetime, stare: dict) -> list[dict]:
    """Pasul 2: executiile de azi, pozitiile si semnalul recalculat cu inchiderea oficiala."""
    zi = acum.date().isoformat()
    rezultate = []
    for s in SIMBOLURI:
        r = {"timestamp": acum.isoformat(), "zi": zi, "simbol": s, "pas": "verificare"}
        asteptare = stare["in_asteptare"].get(s)
        if asteptare:
            o = broker.ordin(asteptare["cid"])
            r["ordin"] = o
            if o and o["status"] == "filled":
                if asteptare["parte"] == "buy":
                    stare["pozitii"][s] = {"qty": o["filled_qty"], "pret_intrare": o["pret_mediu"],
                                           "zi_intrare": zi, "cid": asteptare["cid"]}
                else:
                    poz = stare["pozitii"].pop(s, None)
                    if poz:
                        tranzactie = {"simbol": s, "zi_intrare": poz["zi_intrare"], "zi_iesire": zi,
                                      "pret_intrare": poz["pret_intrare"], "pret_iesire": o["pret_mediu"],
                                      "qty": poz["qty"],
                                      "randament_pct": round((o["pret_mediu"] / poz["pret_intrare"] - 1) * 100, 3),
                                      "profit_usd": round((o["pret_mediu"] - poz["pret_intrare"]) * poz["qty"], 2)}
                        jurnal("tranzactii.jsonl", tranzactie)
                        r["tranzactie"] = tranzactie
                del stare["in_asteptare"][s]
            elif o and o["status"] in ("canceled", "expired", "rejected"):
                r["neexecutat"] = o["status"]
                del stare["in_asteptare"][s]
            # altfel (inca "accepted"/"new"): lasam in asteptare, reverificam la urmatoarea trecere
        try:
            # Ce ar fi decis testul cu inchiderea oficiala (nu cu pretul de la 15:44), pornind din
            # aceeasi stare a pozitiei. Diferentele arata cat costa decizia cu 16 minute inainte.
            istoric = broker.inchideri_zilnice(s, acum)
            if not len(istoric) or istoric.index[-1] != acum.date():
                r["semnal_cu_inchiderea_oficiala"] = {"lipsa": "bara zilnica de azi nu e inca disponibila"}
            else:
                in_poz = stare.get("in_pozitie_la_decizie", {}).get(s, False)
                r["semnal_cu_inchiderea_oficiala"] = semnal(list(istoric.values), in_pozitie=in_poz)
        except Exception as e:
            r["eroare_semnal_oficial"] = f"{type(e).__name__}: {e}"
        rezultate.append(r)
        jurnal(f"{zi}.jsonl", r)
    return rezultate


def ce_de_facut(acum: datetime, sesiune, stare: dict) -> str | None:
    if sesiune is None:
        return None
    _, inchidere = sesiune
    z = stare["zile"].get(acum.date().isoformat(), {})
    if inchidere - DECIZIE_INAINTE <= acum < inchidere - LIMITA_CLS_INAINTE + timedelta(minutes=8) \
            and not z.get("decizie"):
        return "decizie"
    # verificarea ruleaza si daca decizia a avut erori: un ordin trimis pentru celalalt simbol
    # trebuie oricum urmarit
    if acum >= inchidere + VERIFICARE_DUPA and z.get("incercari_decizie") and not z.get("verificare"):
        return "verificare"
    return None


def pas(broker, acum: datetime, sesiuni: dict) -> str | None:
    zi = acum.date()
    if zi not in sesiuni:
        sesiuni.clear()
        sesiuni[zi] = broker.sesiune(zi)
    stare = citeste_stare()
    actiune = ce_de_facut(acum, sesiuni[zi], stare)
    if actiune is None:
        return None
    z = stare["zile"].setdefault(zi.isoformat(), {})
    if actiune == "decizie":
        rez = decide(broker, acum, sesiuni[zi][1], stare)
        # la eroare reincercam la urmatoarea trecere (20 s), cat timp suntem in fereastra;
        # ordinele deja trimise nu se dubleaza (identificator unic pe zi si simbol)
        z["decizie"] = not any("eroare" in r for r in rez)
        z["incercari_decizie"] = z.get("incercari_decizie", 0) + 1
    else:
        rez = verifica(broker, acum, stare)
        z["incercari_verificare"] = z.get("incercari_verificare", 0) + 1
        # reincercam daca un ordin inca nu s-a lamurit, dar nu la nesfarsit (~10 minute)
        z["verificare"] = not stare["in_asteptare"] or z["incercari_verificare"] >= 30
        if stare["in_asteptare"] and z["verificare"]:
            jurnal(f"{zi.isoformat()}.jsonl", {"timestamp": acum.isoformat(), "pas": "verificare",
                                                "alarma": f"ordine nelamurite: {stare['in_asteptare']}"})
    scrie_stare(stare)
    for r in rez:
        print(json.dumps(r, ensure_ascii=False, default=str), flush=True)
    return actiune


def bucla() -> None:
    broker = BrokerAlpaca()
    print(f"ROBOT RSI2 pornit | PAPER | {', '.join(SIMBOLURI)} | {SUMA_PE_SIMBOL_USD} USD/simbol", flush=True)
    sesiuni = {}
    while True:
        try:
            pas(broker, datetime.now(NY), sesiuni)
        except EroareCont:
            raise
        except Exception as e:
            print(f"Eroare in bucla: {type(e).__name__}: {e}", flush=True)
        time.sleep(PAUZA_BUCLA_S)


def afiseaza_stare() -> None:
    broker = BrokerAlpaca()
    stare = citeste_stare()
    acum = datetime.now(NY)
    print(f"Pozitii robot: {stare['pozitii'] or 'niciuna'} | in asteptare: {stare['in_asteptare'] or 'nimic'}")
    for s in SIMBOLURI:
        istoric = broker.inchideri_zilnice(s, acum)
        istoric = istoric[[d < acum.date() for d in istoric.index]]
        pret = broker.pret_curent(s)
        sem = semnal(list(istoric.values) + [pret], in_pozitie=s in stare["pozitii"])
        print(f"  {s}: {sem}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Robotul RSI2 pe SPY/QQQ (doar paper)")
    p.add_argument("--serviciu", action="store_true", help="bucla zilnica")
    p.add_argument("--stare", action="store_true", help="starea si semnalul curent, fara ordine")
    a = p.parse_args()
    try:
        if a.serviciu:
            bucla()
        else:
            afiseaza_stare()
    except EroareCont as e:
        print(f"OPRIT: {e}", flush=True)
        sys.exit(2)
