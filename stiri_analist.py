"""Stirile (v2) si verdictul analistului pentru simbolurile alese, la o data trecuta. Fara ordine.

Pentru fiecare simbol calculeaza gap-ul, pretul si volumul pre-market la ora analizei
(cu aceeasi intarziere de 16 min ca live), aduce stirile Alpaca + documentele SEC din
ultimele 24h si cere verdictul analistului v2. Spre deosebire de replay, nu aplica
filtrele scannerului: poti intreba despre orice simbol, si cu gap mic.

Exemple (in WSL):
    ./venv/bin/python stiri_analist.py --data 2026-11-13 --simboluri CBRS,FCUV,MOD
    ./venv/bin/python stiri_analist.py --data 2026-11-13 --simboluri VTSI --fara-claude

Jurnalul merge in `jurnal_stiri_test/`, separat de live si de replay.
"""
import argparse
import os
import sys
from datetime import date, datetime, timedelta

import analyst
import gap_scanner
from analyst import NY, Candidat, analizeaza_candidat, factor_marime
from replay_analist import DATA_LIMITA_MODEL

DIR_TEST = os.path.join(analyst.AICI, "jurnal_stiri_test")


def candidati_fara_filtre(simboluri: list[str], acum: datetime, client) -> tuple[list[Candidat], list[str]]:
    """Datele de pret/volum pentru fiecare simbol, fara pragurile scannerului."""
    limita = acum - gap_scanner.INTARZIERE_DATE
    start_azi = acum.replace(hour=4, minute=0, second=0, microsecond=0)
    zilnic = gap_scanner.bare(client, simboluri, "1Day", acum - timedelta(days=15), limita)
    minute5 = gap_scanner.bare(client, simboluri, "5Min",
                               acum - timedelta(days=gap_scanner.ZILE_ISTORIC_VOLUM + 6), limita)
    candidati, lipsa = [], []
    for s in simboluri:
        inchidere = gap_scanner.inchidere_anterioara(zilnic.get(s, []), acum.date())
        stat = gap_scanner.statistici_premarket(minute5.get(s, []), limita)
        if inchidere is None or stat is None:
            lipsa.append(s)
            continue
        candidati.append(Candidat(
            simbol=s, gap_pct=round((stat["ultim_pret"] / inchidere - 1) * 100, 2),
            volum_relativ=round(stat["volum_relativ"], 2) if stat["volum_relativ"] else 0.0,
            pret=stat["ultim_pret"]))
    return candidati, lipsa


def main(argv=None, client=None, client_claude=None, client_stiri=None, client_sec=None) -> int:
    p = argparse.ArgumentParser(description="Stirile si verdictul analistului pentru simboluri alese")
    p.add_argument("--data", type=date.fromisoformat, required=True, help="AAAA-LL-ZZ (zi trecuta)")
    p.add_argument("--simboluri", required=True, help="lista separata prin virgula, ex. CBRS,FCUV")
    p.add_argument("--ora", default="09:00", help="ora analizei in ET (implicit 09:00)")
    p.add_argument("--fara-claude", action="store_true", help="doar stirile, fara verdict (gratuit)")
    a = p.parse_args(argv)

    simboluri = [s.strip().upper() for s in a.simboluri.split(",") if s.strip()]
    if len(simboluri) > 20:
        p.error("maxim 20 de simboluri o data (fiecare verdict e un apel Claude)")
    if a.data <= DATA_LIMITA_MODEL and not a.fara_claude:
        print(f"Refuz: {a.data} e inainte de data-limita a modelului ({DATA_LIMITA_MODEL}); "
              f"verdictul ar putea fi influentat de ce stie modelul. Foloseste --fara-claude.")
        return 1
    ora = tuple(int(x) for x in a.ora.split(":"))
    acum = datetime(a.data.year, a.data.month, a.data.day, ora[0], ora[1], tzinfo=NY)

    analyst.DIR_JURNAL = DIR_TEST
    analyst.FISIER_CONTOR = os.path.join(DIR_TEST, "contor_apeluri.json")
    client = client or gap_scanner.ClientDate()

    candidati, lipsa = candidati_fara_filtre(simboluri, acum, client)
    for s in lipsa:
        print(f"\n{s}: fara date pre-market sau fara inchidere anterioara la {acum:%Y-%m-%d %H:%M} ET")
    gap_scanner.ataseaza_stiri_v2(candidati, acum, client_stiri=client_stiri, client_sec=client_sec)

    for c in candidati:
        r = c.stiri_v2
        print(f"\n{'=' * 90}\n{c.simbol}  gap {c.gap_pct:+.1f}%  pret {c.pret:.2f}  "
              f"vol. relativ {c.volum_relativ:.1f}x  |  stiri: {r['stare_stiri']}  SEC: {r['stare_filings']}")
        for e in r["erori"]:
            print(f"  ! {e}")
        if not r["items"]:
            print("  (nicio stire sau document in ultimele 24h)")
        for i in r["items"]:
            t = datetime.fromisoformat(i["published_at"]).astimezone(NY)
            tip = "SEC " + i.get("form", "") if i["type"] == "filing" else "stire"
            print(f"  [{t:%m-%d %H:%M} ET] {tip:9s} {i['headline'][:110]}")
            if i.get("summary"):
                print(f"  {'':24s}{i['summary'][:150]}")
        if not a.fara_claude:
            v = analizeaza_candidat(c, client=client_claude, acum=acum)
            print(f"  -> {v.verdict.upper()}  factor {factor_marime(v):.2f}"
                  + ("" if v.valid else f"  (esec: {v.eroare})"))
            print(f"     catalizator: {v.catalyst}")
            print(f"     motiv: {v.reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
