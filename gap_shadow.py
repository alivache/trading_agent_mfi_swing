"""Faza 1 (shadow): scanner -> analist -> jurnal. NU plaseaza ordine.

O rulare inseamna: scanarea gap-urilor la ora `acum`, verdictul lui Claude pentru
fiecare candidat si o inregistrare in `jurnal_analist/scanari_AAAA-LL-ZZ.jsonl`
cu tot ce s-a intamplat (inclusiv simbolurile eliminate si motivul). Din jurnalul
asta se compara mai tarziu verdictele cu ce a facut pretul.

Rulare manuala (in WSL, din directorul proiectului):
    ./venv/bin/python gap_shadow.py                  # acum, cu Claude
    ./venv/bin/python gap_shadow.py --fara-claude    # doar scannerul, fara cost API
    ./venv/bin/python gap_shadow.py --serviciu       # bucla zilnica (asa ruleaza pe VM)

Bucla zilnica, doar in zilele de bursa (calendarul Alpaca):
  - analiza la 9:00 ET; daca serviciul porneste dupa 9:25, ziua se sare (un verdict
    dat la 11:00 nu e comparabil cu unul de la 9:00);
  - evaluarea la 30 min dupa inchidere (inchiderea din calendar, deci corect si in
    zilele scurte, ex. dupa Thanksgiving);
  - la esec se reincearca de cel mult MAX_INCERCARI ori, cu pauza intre incercari;
  - starea sta pe disc, ca o repornire sa nu ruleze nimic de doua ori.
"""
import argparse
import collections
import json
import os
import time
from dataclasses import asdict
from datetime import datetime, timedelta

import analyst
import evalueaza_gap
import gap_scanner
from analyst import NY, analizeaza_candidati, factor_marime

ORA_ANALIZA = (9, 0)
FEREASTRA_ANALIZA = timedelta(minutes=25)
EVALUARE_DUPA_INCHIDERE = timedelta(minutes=30)
MAX_INCERCARI = 3
PAUZA_INTRE_INCERCARI = {"analiza": timedelta(minutes=2), "evaluare": timedelta(minutes=10)}
PAUZA_BUCLA_S = 30
ZILE_PASTRATE_IN_STARE = 10


def _scrie(inregistrare: dict, acum: datetime) -> None:
    os.makedirs(analyst.DIR_JURNAL, exist_ok=True)
    cale = os.path.join(analyst.DIR_JURNAL, f"scanari_{acum.astimezone(NY):%Y-%m-%d}.jsonl")
    with open(cale, "a", encoding="utf-8") as f:
        f.write(json.dumps(inregistrare, ensure_ascii=False, default=str) + "\n")


def ruleaza_analiza(acum: datetime | None = None, cu_claude: bool = True,
                    client_date=None, client_claude=None, univers: list[str] | None = None,
                    versiune_stiri: str | None = None, client_stiri=None, client_sec=None) -> dict:
    """O rulare completa. Nu arunca exceptii: un esec al scannerului inseamna zero candidati.

    `univers`: lista de simboluri de verificat. Implicit se reconstruieste din bare pentru
    toate actiunile (`univers_complet`) — aceeasi sursa ca in replay, ca rezultatele live si
    cele din replay sa fie comparabile. Listele Alpaca "movers" (top 50, jumatate warrants)
    ratau gap-uri reale: pe 13 nov 2026, 4 din 10 candidati.

    `versiune_stiri`: "v1" (doar titluri) sau "v2" (news.py: rezumate + SEC). Implicit din
    VERSIUNE_STIRI in .env, altfel "v1". Live ramane pe v1 pana cand replay-ul comparativ
    arata ca v2 e cel putin la fel de buna.
    """
    acum = (acum or datetime.now(NY)).astimezone(NY)
    versiune_stiri = versiune_stiri or os.getenv("VERSIUNE_STIRI", "v1")
    rezultat = {"timestamp": datetime.now(NY).isoformat(), "acum": acum.isoformat(),
                "versiune_stiri": versiune_stiri,
                "candidati": [], "respinsi": [], "verdicte": [], "eroare": None}
    try:
        client_date = client_date or gap_scanner.ClientDate()
        if univers is None:
            univers = gap_scanner.univers_complet(acum, client_date)
        rezultat["univers"] = len(univers)
        candidati, respinsi = gap_scanner.scaneaza(acum, client=client_date, univers=univers,
                                                   versiune_stiri=versiune_stiri,
                                                   client_stiri=client_stiri, client_sec=client_sec)
    except Exception as e:
        rezultat["eroare"] = f"scanner: {type(e).__name__}: {e}"
        _scrie(rezultat, acum)
        return rezultat

    rezultat["candidati"] = [asdict(c) for c in candidati]
    rezultat["respinsi"] = respinsi
    rezultat["respinsi_pe_motiv"] = dict(collections.Counter(r["motiv"] for r in respinsi))
    if cu_claude and candidati:
        verdicte = analizeaza_candidati(candidati, client=client_claude, acum=acum)
        rezultat["verdicte"] = [{**asdict(v), "factor_marime": factor_marime(v)} for v in verdicte]
    _scrie(rezultat, acum)
    return rezultat


def afiseaza(r: dict) -> None:
    print(f"Scanare la {r['acum']} | univers: {r.get('univers', '?')} simboluri cu gap")
    if r["eroare"]:
        print(f"  EROARE: {r['eroare']}")
        return
    for motiv, n in sorted(r.get("respinsi_pe_motiv", {}).items(), key=lambda x: -x[1]):
        print(f"  eliminati: {n:3d}  {motiv}")
    print(f"  candidati: {len(r['candidati'])}")
    verdicte = {v["simbol"]: v for v in r["verdicte"]}
    for c in r["candidati"]:
        print(f"\n  {c['simbol']:6s} gap {c['gap_pct']:+6.2f}%  pret {c['pret']:8.2f}  "
              f"vol. relativ {c['volum_relativ']:6.1f}  stiri: {len(c['stiri'])}")
        for s in c["stiri"][:3]:
            print(f"         [{s['ora']}] {s['titlu'][:100]}")
        v = verdicte.get(c["simbol"])
        if v:
            print(f"    -> {v['verdict'].upper()}  factor {v['factor_marime']:.2f}  "
                  f"catalizator: {v['catalyst']}")
            print(f"       {v['reason']}")


# ---------------------------------------------------------------- bucla zilnica

def _fisier_stare() -> str:
    return os.path.join(analyst.DIR_JURNAL, "stare_shadow.json")


def citeste_stare() -> dict:
    try:
        with open(_fisier_stare(), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def scrie_stare(stare: dict) -> None:
    zile = sorted(stare)[-ZILE_PASTRATE_IN_STARE:]       # nu lasam fisierul sa creasca la infinit
    os.makedirs(analyst.DIR_JURNAL, exist_ok=True)
    tmp = _fisier_stare() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({z: stare[z] for z in zile}, f, indent=1)
    os.replace(tmp, _fisier_stare())


def sesiune_bursa(client, zi: str) -> dict | None:
    """Deschiderea si inchiderea sesiunii din `zi`, sau None daca bursa e inchisa."""
    cal = client.get("/v2/calendar", {"start": zi, "end": zi}, trading=True)
    if not cal or cal[0]["date"] != zi:
        return None
    return {"open": datetime.fromisoformat(f"{zi}T{cal[0]['open']}").replace(tzinfo=NY),
            "close": datetime.fromisoformat(f"{zi}T{cal[0]['close']}").replace(tzinfo=NY)}


def _poate_incerca(s: dict, actiune: str, acum: datetime) -> bool:
    if s.get(f"{actiune}_facuta") or s.get(f"incercari_{actiune}", 0) >= MAX_INCERCARI:
        return False
    ultima = s.get(f"ultima_{actiune}")
    return ultima is None or acum >= datetime.fromisoformat(ultima) + PAUZA_INTRE_INCERCARI[actiune]


def ce_de_facut(acum: datetime, sesiune: dict | None, stare: dict) -> str | None:
    """"analiza", "evaluare" sau None. Functie pura: nu citeste ceasul, nu face apeluri."""
    if sesiune is None:
        return None
    s = stare.get(acum.date().isoformat(), {})
    t_analiza = acum.replace(hour=ORA_ANALIZA[0], minute=ORA_ANALIZA[1], second=0, microsecond=0)
    if t_analiza <= acum < t_analiza + FEREASTRA_ANALIZA and _poate_incerca(s, "analiza", acum):
        return "analiza"
    if (s.get("analiza_facuta") and acum >= sesiune["close"] + EVALUARE_DUPA_INCHIDERE
            and _poate_incerca(s, "evaluare", acum)):
        return "evaluare"
    return None


def pas(acum: datetime, client_date, sesiuni: dict, client_claude=None) -> str | None:
    """O trecere prin bucla: decide si executa cel mult o actiune."""
    zi = acum.date().isoformat()
    if zi not in sesiuni:
        sesiuni.clear()
        sesiuni[zi] = sesiune_bursa(client_date, zi)
    stare = citeste_stare()
    actiune = ce_de_facut(acum, sesiuni[zi], stare)
    if actiune is None:
        return None

    s = stare.setdefault(zi, {})
    s[f"incercari_{actiune}"] = s.get(f"incercari_{actiune}", 0) + 1
    s[f"ultima_{actiune}"] = acum.isoformat()
    scrie_stare(stare)   # marcam incercarea inainte de executie: un crash nu duce la bucla infinita

    if actiune == "analiza":
        r = ruleaza_analiza(acum, client_date=client_date, client_claude=client_claude)
        afiseaza(r)
        reusit = r["eroare"] is None
    else:
        try:
            evaluari = evalueaza_gap.evalueaza_zi(zi, client=client_date)
            print(f"Evaluare {zi}: {len(evaluari)} candidati", flush=True)
            evalueaza_gap.afiseaza_rezumat(evalueaza_gap.toate_evaluarile())
            reusit = True
        except Exception as e:
            print(f"Eroare evaluare {zi}: {type(e).__name__}: {e}", flush=True)
            reusit = False

    s[f"{actiune}_facuta"] = reusit
    scrie_stare(stare)
    return actiune


def bucla() -> None:
    print(f"GAP SHADOW pornit | analiza la {ORA_ANALIZA[0]}:{ORA_ANALIZA[1]:02d} ET | "
          f"model {analyst.MODEL} | fara ordine", flush=True)
    client_date = gap_scanner.ClientDate()
    sesiuni = {}
    while True:
        try:
            pas(datetime.now(NY), client_date, sesiuni)
        except Exception as e:   # ex. calendarul Alpaca indisponibil: reincercam la urmatoarea trecere
            print(f"Eroare in bucla: {type(e).__name__}: {e}", flush=True)
        time.sleep(PAUZA_BUCLA_S)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Scanare de gap-uri + verdicte Claude (fara ordine)")
    p.add_argument("--fara-claude", action="store_true", help="doar scannerul, fara apeluri API")
    p.add_argument("--acum", help='ora analizei in ET, ex. "2026-11-16 09:00" (implicit: acum)')
    p.add_argument("--serviciu", action="store_true", help="bucla zilnica (pentru systemd)")
    a = p.parse_args()
    if a.serviciu:
        bucla()
    else:
        acum = datetime.strptime(a.acum, "%Y-%m-%d %H:%M").replace(tzinfo=NY) if a.acum else None
        afiseaza(ruleaza_analiza(acum, cu_claude=not a.fara_claude))
