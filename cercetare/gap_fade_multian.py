"""H4 din IPOTEZE.md: short pe gap-urile in sus, 2016 - nov 2026, cu stop simulat minut cu minut.

Etape (fiecare salvata pe disc in cercetare/cache/h4/, ca o intrerupere sa se reia de unde a ramas):
  1. universul: actiuni obisnuite NYSE/NASDAQ/AMEX, active SI delistate (fara partinirea
     supravietuitorilor);
  2. bare zilnice (brute + ajustate pentru split-uri) -> candidatii fiecarei zile:
       gap = deschidere / inchiderea anterioara - 1 >= 5% (pe preturi ajustate, ca un split sa nu
       para gap), inchiderea anterioara bruta >= 5 USD, valoarea tranzactionata medie pe 20 de zile
       (anterioare) >= 5 mil. USD, maxim 10 pe zi dupa aceasta valoare;
       inchiderea anterioara trebuie sa fie la cel mult 5 zile calendaristice (un simbol refolosit de
       alta companie, ex. ULTI, ar produce altfel un "gap" fals intre doua firme);
  3. bare de 1 minut pentru candidatii fiecarei zile -> tranzactia simulata, pentru stopuri de
     5%, 10% (regula decisiva) si 20% (sensibilitate);
  4. raportul si criteriul pre-inregistrat.

Rulare (in WSL, din radacina proiectului; ~40 min prima data, apoi secunde din cache):
    ./venv/bin/python cercetare/gap_fade_multian.py
"""
import json
import math
import os
import pickle
import re
import statistics as st
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

AICI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(AICI))

import gap_scanner                                         # noqa: E402
from analyst import NY                                     # noqa: E402

DIR = os.path.join(AICI, "cache", "h4")
START, END = date(2016, 1, 4), date(2026, 11, 13)
BURSE = {"NYSE", "NASDAQ", "AMEX"}
GAP_MIN = 0.05
PRET_MIN = 5.0
VALOARE_MIN = 5_000_000
MAX_PE_ZI = 10
STOPURI = (0.05, 0.10, 0.20)
STOP_DECISIV = 0.10
COST = 0.005                                               # dus-intors
VERIFICARE = (date(2022, 1, 1), END)
MAX_ZILE_INTRE_BARE = 5


def _log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# ---------------------------------------------------------------- 1. universul

def e_actiune_obisnuita(a: dict) -> bool:
    """Filtru pe lista de active (si cele delistate, care adesea n-au nume)."""
    s, nume = a.get("symbol", ""), (a.get("name") or "").lower()
    if a.get("class") != "us_equity" or a.get("exchange") not in BURSE:
        return False
    if not re.fullmatch(r"[A-Z]{1,5}", s):                 # CUSIP-uri, "_DELISTED", clase cu punct
        return False
    if len(s) == 5 and s[-1] in "WUR":                     # conventia NASDAQ: warrants/units/rights
        return False
    return not any(c in nume for c in ("warrant", "right", " unit", "units", " etf", "etn", " fund"))


def univers(client) -> list[str]:
    cale = os.path.join(DIR, "univers.json")
    if os.path.exists(cale):
        return json.load(open(cale))
    toate = []
    for stare in ("active", "inactive"):
        toate += client.get("/v2/assets", {"status": stare, "asset_class": "us_equity"}, trading=True)
    simboluri = sorted({a["symbol"] for a in toate if e_actiune_obisnuita(a)})
    json.dump(simboluri, open(cale, "w"))
    return simboluri


# ---------------------------------------------------------------- 2. candidatii din bare zilnice

def _df(lista: list[dict]) -> pd.DataFrame:
    if not lista:
        return pd.DataFrame(columns=["zi", "o", "c", "v"])
    d = pd.DataFrame(lista)
    d["zi"] = pd.to_datetime(d["t"]).dt.tz_convert("America/New_York").dt.date
    return d[["zi", "o", "c", "v"]]


def candidati_simbol(simbol: str, brute: list[dict], ajustate: list[dict]) -> list[dict]:
    """Zilele in care simbolul trece filtrele H4. Functie pura."""
    a, b = _df(ajustate), _df(brute)
    if len(a) < 22:
        return []
    d = a.merge(b, on="zi", how="inner", suffixes=("", "_brut")).sort_values("zi").reset_index(drop=True)
    d["c_ant"] = d["c"].shift(1)
    d["c_brut_ant"] = d["c_brut"].shift(1)
    d["zi_ant"] = d["zi"].shift(1)
    d["valoare20"] = (d["c_brut"] * d["v_brut"]).rolling(20).mean().shift(1)
    d["gap"] = d["o"] / d["c_ant"] - 1
    zile_dif = (pd.to_datetime(d["zi"]) - pd.to_datetime(d["zi_ant"])).dt.days
    ok = ((d["gap"] >= GAP_MIN) & (d["c_brut_ant"] >= PRET_MIN) & (d["valoare20"] >= VALOARE_MIN)
          & (zile_dif <= MAX_ZILE_INTRE_BARE))
    return [{"zi": r.zi, "simbol": simbol, "gap": float(r.gap), "valoare20": float(r.valoare20),
             "c_brut_ant": float(r.c_brut_ant)} for r in d[ok].itertuples()]


def toti_candidatii(client, simboluri: list[str], lot: int = 400) -> pd.DataFrame:
    os.makedirs(os.path.join(DIR, "loturi"), exist_ok=True)
    rezultat = []
    t0, t1 = datetime(2015, 11, 1, tzinfo=NY), datetime(END.year, END.month, END.day, 20, tzinfo=NY)
    for i in range(0, len(simboluri), lot):
        cale = os.path.join(DIR, "loturi", f"lot_{i // lot:03d}.pkl")
        if os.path.exists(cale):
            rezultat += pickle.load(open(cale, "rb"))
            continue
        grup = simboluri[i:i + lot]
        brute = gap_scanner.bare(client, grup, "1Day", t0, t1, ajustare="raw")
        ajustate = gap_scanner.bare(client, grup, "1Day", t0, t1, ajustare="split")
        gasiti = []
        for s in grup:
            gasiti += candidati_simbol(s, brute.get(s, []), ajustate.get(s, []))
        pickle.dump(gasiti, open(cale, "wb"))
        rezultat += gasiti
        _log(f"bare zilnice: lot {i // lot + 1}/{math.ceil(len(simboluri) / lot)}, "
             f"{len(gasiti)} zile-candidat in lot")
    df = pd.DataFrame(rezultat)
    df = df[(df["zi"] >= START) & (df["zi"] <= END)]
    # maxim MAX_PE_ZI pe zi, dupa valoarea tranzactionata medie
    return (df.sort_values(["zi", "valoare20"], ascending=[True, False])
              .groupby("zi").head(MAX_PE_ZI).reset_index(drop=True))


# ---------------------------------------------------------------- 3. simularea pe bare de 1 minut

def simuleaza_short(bare_1m: list[dict], deschidere: datetime, iesire: datetime,
                    stopuri=STOPURI) -> dict | None:
    """Short la deschiderea primei bare a sesiunii; stop verificat minut cu minut; acoperire la
    inchiderea barei de la `iesire` (inclusiv). Intoarce randamentul short-ului (fara costuri)
    pentru fiecare stop. Functie pura."""
    sesiune = []
    for b in bare_1m:
        t = datetime.fromisoformat(b["t"].replace("Z", "+00:00")).astimezone(NY)
        if deschidere <= t <= iesire:
            sesiune.append((t, b))
    if not sesiune or sesiune[0][0] > deschidere + timedelta(minutes=5):
        return None                                        # fara deschidere normala (oprire, lipsa date)
    intrare = sesiune[0][1]["o"]
    final = sesiune[-1][1]["c"]
    rezultat = {"intrare": intrare, "final": final}
    for stop in stopuri:
        nivel = intrare * (1 + stop)
        pret_iesire = final
        for _, b in sesiune:
            if b["o"] >= nivel:                            # a sarit peste stop: executie la deschiderea barei
                pret_iesire = b["o"]
                break
            if b["h"] >= nivel:
                pret_iesire = nivel
                break
        rezultat[f"stop{int(stop * 100)}"] = randament_short(intrare, pret_iesire)
    rezultat["fara_stop"] = randament_short(intrare, final)
    return rezultat


def randament_short(intrare: float, iesire: float) -> float:
    """Castigul unui short ca fractie din valoarea la intrare: maxim +100% (pretul la zero),
    iar la un stop de +10% pierderea e exact -10%.

    (Versiunea initiala calcula `intrare / iesire - 1`, care umfla castigurile la prabusiri —
    +1085% in loc de +91.6% la HOLO — si micsora pierderile la stop. Gasit si reparat
    pe 14 nov 2026, inainte de orice decizie pe baza rezultatelor.)
    """
    return 1 - iesire / intrare


def calendar(client) -> dict[date, tuple[datetime, datetime]]:
    cale = os.path.join(DIR, "calendar.pkl")
    if os.path.exists(cale):
        return pickle.load(open(cale, "rb"))
    cal = client.get("/v2/calendar", {"start": START.isoformat(), "end": END.isoformat()}, trading=True)
    rez = {}
    for z in cal:
        d = date.fromisoformat(z["date"])
        rez[d] = (datetime.fromisoformat(f"{z['date']}T{z['open']}").replace(tzinfo=NY),
                  datetime.fromisoformat(f"{z['date']}T{z['close']}").replace(tzinfo=NY))
    pickle.dump(rez, open(cale, "wb"))
    return rez


def toate_tranzactiile(client, candidati: pd.DataFrame, cal) -> pd.DataFrame:
    cale = os.path.join(DIR, "tranzactii.jsonl")
    facute = set()
    if os.path.exists(cale):
        for l in open(cale):
            facute.add(json.loads(l)["zi"])
    zile = sorted(candidati["zi"].unique())
    with open(cale, "a") as f:
        for n, zi in enumerate(zile):
            if zi.isoformat() in facute or zi not in cal:
                continue
            deschidere, inchidere = cal[zi]
            iesire = inchidere - timedelta(minutes=11)     # bara de 15:49 (12:49 in zilele scurte)
            grup = candidati[candidati["zi"] == zi]
            b = gap_scanner.bare(client, list(grup["simbol"]), "1Min", deschidere,
                                 inchidere - timedelta(minutes=1), ajustare="raw")
            linii = []
            for r in grup.itertuples():
                sim = simuleaza_short(b.get(r.simbol, []), deschidere, iesire)
                linii.append({"zi": zi.isoformat(), "simbol": r.simbol, "gap": r.gap,
                              "valoare20": r.valoare20, **(sim or {"fara_deschidere": True})})
            if not linii:
                linii = [{"zi": zi.isoformat(), "gol": True}]
            for l in linii:
                f.write(json.dumps(l) + "\n")
            f.flush()
            if n % 100 == 0:
                _log(f"bare de 1 minut: ziua {n + 1}/{len(zile)} ({zi})")
    return pd.DataFrame([json.loads(l) for l in open(cale)])


# ---------------------------------------------------------------- 4. raport

def raport(tr: pd.DataFrame) -> bool:
    tr = tr[tr.get("intrare").notna()] if "intrare" in tr else tr
    tr = tr.copy()
    tr["zi"] = pd.to_datetime(tr["zi"]).dt.date
    tr["an"] = [z.year for z in tr["zi"]]
    _log(f"{len(tr)} tranzactii simulate in {tr['zi'].nunique()} zile")
    trece = False
    for col, nume in [("stop5", "stop 5%"), ("stop10", "stop 10% (DECISIV)"), ("stop20", "stop 20%"),
                      ("fara_stop", "fara stop")]:
        x = tr[col] - COST
        print(f"\n== {nume}, cost {COST * 100:.1f}% dus-intors")
        for eticheta, masca in (("calibrare 2016-2021", tr["zi"] < VERIFICARE[0]),
                                ("VERIFICARE 2022-2026", tr["zi"] >= VERIFICARE[0])):
            s = x[masca]
            pe_zi = s.groupby(tr.loc[masca, "zi"]).mean()
            t = pe_zi.mean() / (pe_zi.std() / math.sqrt(len(pe_zi)))
            print(f"  {eticheta}: n={len(s)}  medie {s.mean() * 100:+.2f}%  mediana {s.median() * 100:+.2f}%  "
                  f"castig {(s > 0).mean() * 100:.0f}%  | pe zile: medie {pe_zi.mean() * 100:+.2f}% "
                  f"t={t:+.2f}  zile+ {(pe_zi > 0).mean() * 100:.0f}%  cea mai rea zi {pe_zi.min() * 100:+.1f}%")
        ani = x.groupby(tr["an"]).agg(["count", "mean"])
        print("  pe ani: " + "  ".join(f"{a}: {m * 100:+.2f}% (n={n})" for a, (n, m) in ani.iterrows()))
        if col == "stop10":
            v = tr["zi"] >= VERIFICARE[0]
            pe_zi = x[v].groupby(tr.loc[v, "zi"]).mean()
            t = pe_zi.mean() / (pe_zi.std() / math.sqrt(len(pe_zi)))
            ani_v = [a for a in range(2022, 2027)]
            pozitivi = sum(1 for a in ani_v if a in ani.index and ani.loc[a, "mean"] > 0)
            trece = pe_zi.mean() > 0 and t > 2 and pozitivi >= 4
            print(f"  CRITERIU: media zilnica {pe_zi.mean() * 100:+.2f}% > 0 cu t={t:.2f} > 2 si "
                  f"{pozitivi}/5 ani pozitivi (>= 4) -> {'TRECE' if trece else 'NU trece'}")
    return trece


def main():
    os.makedirs(DIR, exist_ok=True)
    client = gap_scanner.ClientDate()
    simboluri = univers(client)
    _log(f"univers: {len(simboluri)} actiuni obisnuite (active + delistate)")
    cand = toti_candidatii(client, simboluri)
    _log(f"candidati: {len(cand)} in {cand['zi'].nunique()} zile")
    cal = calendar(client)
    tr = toate_tranzactiile(client, cand, cal)
    raport(tr)


if __name__ == "__main__":
    main()
