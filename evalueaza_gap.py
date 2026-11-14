"""Evaluarea de seara: ce a facut pretul fiecarui candidat, comparat cu verdictul.

Pentru fiecare candidat din scanarea de dimineata masuram un trade ipotetic simplu,
identic pentru toti, ca verdictele sa poata fi comparate corect:
  intrare = deschiderea primei bare de 1 min a sesiunii (9:30)
  iesire  = inchiderea barei de la "inchidere - 10 min" (15:50; 12:50 in zilele scurte)
Masuram randamentul, excursiile maxime (MFE/MAE) si randamentul QQQ pe exact acelasi
interval. Excesul fata de QQQ e cifra care conteaza: lectia din v3 a fost ca o
strategie poate parea buna doar pentru ca piata a urcat.

Nu exista stop sau dimensionare aici — e masurarea selectiei, nu o simulare de cont.
Intrebarea la care raspunde, dupa cateva saptamani: candidatii "tradeable" fac mai bine
decat cei "avoid"? Daca nu, analistul nu adauga nimic.

Rulare (in WSL):
    ./venv/bin/python evalueaza_gap.py                # rezumatul tuturor zilelor evaluate
    ./venv/bin/python evalueaza_gap.py --zi 2026-11-16   # evalueaza (sau re-evalueaza) o zi
"""
import argparse
import glob
import json
import math
import os
import statistics
from datetime import datetime, timedelta

import analyst
from analyst import NY
from gap_scanner import ClientDate, bare

MINUTE_INAINTE_DE_INCHIDERE = 10
SIMBOL_PIATA = "QQQ"


def _ora(b: dict) -> datetime:
    return datetime.fromisoformat(b["t"].replace("Z", "+00:00")).astimezone(NY)


def ultima_scanare(zi: str, director: str | None = None) -> dict | None:
    """Ultima rulare fara eroare din ziua `zi` (pot exista si rulari manuale)."""
    cale = os.path.join(director or analyst.DIR_JURNAL, f"scanari_{zi}.jsonl")
    if not os.path.exists(cale):
        return None
    rulari = [json.loads(l) for l in open(cale, encoding="utf-8") if l.strip()]
    bune = [r for r in rulari if not r.get("eroare") and r.get("candidati")]
    return bune[-1] if bune else None


def masoara(bare_1m: list[dict], deschidere: datetime, iesire: datetime) -> dict | None:
    """Randament, MFE si MAE intre deschidere si iesire, din bare de 1 minut."""
    sesiune = [b for b in bare_1m if deschidere <= _ora(b) < iesire]
    if not sesiune:
        return None
    intrare, final = sesiune[0]["o"], sesiune[-1]["c"]
    return {
        "intrare": intrare,
        "iesire": final,
        "randament_pct": round((final / intrare - 1) * 100, 3),
        "mfe_pct": round((max(b["h"] for b in sesiune) / intrare - 1) * 100, 3),
        "mae_pct": round((min(b["l"] for b in sesiune) / intrare - 1) * 100, 3),
        "minim": min(b["l"] for b in sesiune),
    }


def evalueaza_zi(zi: str, client: ClientDate | None = None) -> list[dict]:
    """Evalueaza candidatii din `zi` si scrie `evaluare_<zi>.jsonl` (suprascrie)."""
    scanare = ultima_scanare(zi)
    if scanare is None:
        return []
    client = client or ClientDate()
    cal = client.get("/v2/calendar", {"start": zi, "end": zi}, trading=True)
    if not cal:
        return []
    deschidere = datetime.fromisoformat(f"{zi}T{cal[0]['open']}").replace(tzinfo=NY)
    inchidere = datetime.fromisoformat(f"{zi}T{cal[0]['close']}").replace(tzinfo=NY)
    iesire = inchidere - timedelta(minutes=MINUTE_INAINTE_DE_INCHIDERE)

    simboluri = [c["simbol"] for c in scanare["candidati"]]
    date = bare(client, simboluri + [SIMBOL_PIATA], "1Min", deschidere, inchidere)
    piata = masoara(date.get(SIMBOL_PIATA, []), deschidere, iesire)
    verdicte = {v["simbol"]: v for v in scanare.get("verdicte", [])}

    rezultate = []
    for c in scanare["candidati"]:
        v = verdicte.get(c["simbol"], {})
        m = masoara(date.get(c["simbol"], []), deschidere, iesire)
        inchidere_ieri = c["pret"] / (1 + c["gap_pct"] / 100)
        r = {
            "zi": zi, "simbol": c["simbol"], "gap_pct": c["gap_pct"], "pret_analiza": c["pret"],
            "nr_stiri": len(c["stiri"]),
            "verdict": v.get("verdict", "fara verdict"), "valid": v.get("valid", False),
            "factor_marime": v.get("factor_marime", 0.0), "confidence": v.get("confidence"),
            "catalyst": v.get("catalyst"),
        }
        if m is None:
            r["eroare"] = "fara bare in sesiune (oprit de la tranzactionare?)"
        else:
            r.update({k: m[k] for k in ("intrare", "iesire", "randament_pct", "mfe_pct", "mae_pct")})
            r["gap_pana_la_deschidere_pct"] = round((m["intrare"] / c["pret"] - 1) * 100, 3)
            r["gap_umplut"] = m["minim"] <= inchidere_ieri
            if piata:
                r["qqq_pct"] = piata["randament_pct"]
                r["exces_pct"] = round(m["randament_pct"] - piata["randament_pct"], 3)
        rezultate.append(r)

    cale = os.path.join(analyst.DIR_JURNAL, f"evaluare_{zi}.jsonl")
    with open(cale, "w", encoding="utf-8") as f:
        for r in rezultate:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return rezultate


def toate_evaluarile(director: str | None = None) -> list[dict]:
    rezultate = []
    for cale in sorted(glob.glob(os.path.join(director or analyst.DIR_JURNAL, "evaluare_*.jsonl"))):
        rezultate += [json.loads(l) for l in open(cale, encoding="utf-8") if l.strip()]
    return rezultate


def rezumat(evaluari: list[dict]) -> dict:
    """Pe grupe de verdict: n, randament mediu, exces mediu vs QQQ, rata de castig, t."""
    grupe = {}
    for e in evaluari:
        if "exces_pct" not in e:
            continue
        if e["valid"]:
            grupa = e["verdict"]
        elif e["verdict"] == "fara verdict":
            grupa = "fara verdict (doar scanner)"
        else:
            grupa = "invalid (esec analist)"
        grupe.setdefault(grupa, []).append(e)
    rezultat = {}
    for g, lista in grupe.items():
        exces = [e["exces_pct"] for e in lista]
        n = len(exces)
        t = (statistics.mean(exces) / (statistics.stdev(exces) / math.sqrt(n))
             if n > 2 and statistics.stdev(exces) > 0 else None)
        rezultat[g] = {
            "n": n,
            "randament_mediu_pct": round(statistics.mean(e["randament_pct"] for e in lista), 3),
            "exces_mediu_pct": round(statistics.mean(exces), 3),
            "rata_castig_pct": round(100 * sum(e["randament_pct"] > 0 for e in lista) / n, 1),
            "t_exces": round(t, 2) if t is not None else None,
            "gap_umplut_pct": round(100 * sum(e["gap_umplut"] for e in lista) / n, 1),
        }
    return rezultat


def afiseaza_rezumat(evaluari: list[dict]) -> None:
    zile = sorted({e["zi"] for e in evaluari})
    print(f"Evaluari: {len(evaluari)} candidati in {len(zile)} zile"
          + (f" ({zile[0]} .. {zile[-1]})" if zile else ""))
    for g, s in sorted(rezumat(evaluari).items()):
        print(f"  {g:24s} n={s['n']:3d}  randament {s['randament_mediu_pct']:+7.3f}%  "
              f"exces vs QQQ {s['exces_mediu_pct']:+7.3f}%  (t={s['t_exces']})  "
              f"castig {s['rata_castig_pct']:5.1f}%  gap umplut {s['gap_umplut_pct']:5.1f}%")
    print("  (sub ~30 de candidati pe grupa, diferentele sunt zgomot)")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Evaluarea verdictelor fata de pret")
    p.add_argument("--zi", help="AAAA-LL-ZZ: evalueaza ziua asta inainte de rezumat")
    a = p.parse_args()
    if a.zi:
        for r in evalueaza_zi(a.zi):
            print(f"  {r['simbol']:6s} {r['verdict']:10s} "
                  + (f"{r['randament_pct']:+7.2f}%  exces {r.get('exces_pct', float('nan')):+7.2f}%"
                     if "randament_pct" in r else r.get("eroare", "")))
    afiseaza_rezumat(toate_evaluarile())
