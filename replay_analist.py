"""Ruleaza analistul pe zile trecute, fara ordine, si arata verdictele langa ce a facut pretul.

Ce face, pentru fiecare zi de bursa ceruta:
  1. reconstruieste universul de gap-uri din barele istorice, cu datele de pana la 8:44
     (`gap_scanner.univers_complet`), pentru ca listele "movers" exista doar live;
  2. ruleaza exact acelasi scanner si acelasi analist ca serviciul de pe VM;
  3. evalueaza fiecare candidat ca evaluarea de seara (intrare 9:30, iesire la
     inchidere - 10 min, exces fata de QQQ).

Jurnalul merge in `jurnal_replay/`, separat de cel live, cu propriul plafon zilnic.

DE CE CONTEAZA DATA: Claude Sonnet 5.5 are data-limita a cunostintelor iunie 2026.
Pentru o zi de dinainte, modelul ar putea sti deja ce s-a intamplat cu titlul
(achizitia, offering-ul, prabusirea) si verdictele ar parea mai bune decat sunt.
De aceea zilele de pana la DATA_LIMITA_MODEL sunt refuzate implicit.

Exemple (in WSL):
    ./venv/bin/python replay_analist.py --zi 2026-11-13
    ./venv/bin/python replay_analist.py --zi 2026-11-13 --fara-claude
    ./venv/bin/python replay_analist.py --de-la 2026-08-03 --pana-la 2026-08-28 --da
"""
import argparse
import os
import sys
import dataclasses
from dataclasses import asdict
from datetime import date, datetime, timedelta

import analyst
import evalueaza_gap
import gap_scanner
import gap_shadow
from analyst import NY, Candidat, analizeaza_candidati, factor_marime

DATA_LIMITA_MODEL = date(2026, 6, 30)     # Sonnet 5.5: "Jun 2026" (docs, verificat 14 nov 2026)
COST_ESTIMAT_PE_CANDIDAT_USD = 0.004      # masurat: ~1.150 tokeni in + ~150 out cu Sonnet 5.5
PRAG_CONFIRMARE_USD = 0.50                # peste atat, cerem --da explicit
DIR_REPLAY = os.path.join(analyst.AICI, "jurnal_replay")
DIR_REPLAY_V2 = os.path.join(analyst.AICI, "jurnal_replay_v2")


def zile_de_bursa(client, de_la: date, pana_la: date) -> list[date]:
    cal = client.get("/v2/calendar", {"start": de_la.isoformat(), "end": pana_la.isoformat()},
                     trading=True)
    return [date.fromisoformat(z["date"]) for z in cal]


def ruleaza_zi(zi: date, client, cu_claude: bool, ora: tuple[int, int], detaliat: bool,
               client_claude=None) -> None:
    acum = datetime(zi.year, zi.month, zi.day, ora[0], ora[1], tzinfo=NY)
    univers = gap_scanner.univers_complet(acum, client)
    r = gap_shadow.ruleaza_analiza(acum, cu_claude=cu_claude, client_date=client,
                                   client_claude=client_claude, univers=univers,
                                   versiune_stiri="v1")
    if r["eroare"]:
        print(f"{zi}: EROARE {r['eroare']}", flush=True)
        return
    afiseaza_zi(zi, r, client, detaliat)


def ruleaza_zi_v2(zi: date, client, detaliat: bool, client_claude=None,
                  client_stiri=None, client_sec=None) -> None:
    """Comparatia A/B: ACEIASI candidati ca in replay-ul v1, doar stirile si promptul difera.

    Candidatii se iau din `jurnal_replay/` (nu se rescaneaza), ca diferenta dintre v1 si v2
    sa vina numai din stiri (rezumate + documente SEC) si din promptul de sistem v2.
    """
    sc = evalueaza_gap.ultima_scanare(zi.isoformat(), director=DIR_REPLAY)
    if sc is None:
        print(f"{zi}: nu exista scanare v1 in {DIR_REPLAY} — ruleaza intai replay-ul v1", flush=True)
        return
    acum = datetime.fromisoformat(sc["acum"]).astimezone(NY)
    campuri = {f.name for f in dataclasses.fields(Candidat)} - {"stiri", "stiri_v2"}
    candidati = [Candidat(**{k: v for k, v in c.items() if k in campuri}) for c in sc["candidati"]]
    gap_scanner.ataseaza_stiri_v2(candidati, acum, client_stiri=client_stiri, client_sec=client_sec)
    verdicte = analizeaza_candidati(candidati, client=client_claude, acum=acum)
    r = {"timestamp": datetime.now(NY).isoformat(), "acum": acum.isoformat(),
         "versiune_stiri": "v2", "sursa_candidati": "jurnal_replay (v1)",
         "univers": sc.get("univers"), "respinsi_pe_motiv": sc.get("respinsi_pe_motiv", {}),
         "candidati": [asdict(c) for c in candidati],
         "verdicte": [{**asdict(v), "factor_marime": factor_marime(v)} for v in verdicte],
         "eroare": None}
    gap_shadow._scrie(r, acum)
    afiseaza_zi(zi, r, client, detaliat)


def afiseaza_zi(zi: date, r: dict, client, detaliat: bool) -> None:
    evaluari = {e["simbol"]: e for e in evalueaza_gap.evalueaza_zi(zi.isoformat(), client=client)} \
        if r["candidati"] else {}
    print(f"\n{zi} [{r.get('versiune_stiri', 'v1')}]: univers {r.get('univers')} cu gap >= "
          f"{gap_scanner.GAP_MIN_PCT}% -> {len(r['candidati'])} candidati", flush=True)
    verdicte = {v["simbol"]: v for v in r["verdicte"]}
    for c in r["candidati"]:
        v, e = verdicte.get(c["simbol"], {}), evaluari.get(c["simbol"], {})
        rezultat = (f"{e['randament_pct']:+7.2f}%  exces {e['exces_pct']:+7.2f}%"
                    if "exces_pct" in e else "   (fara evaluare)")
        verdict = (f"{v['verdict']:9s} x{v['factor_marime']:.2f}" if v else "(fara Claude)  ")
        print(f"  {c['simbol']:6s} gap {c['gap_pct']:+6.1f}%  stiri {len(c['stiri']):2d}  "
              f"{verdict}  {rezultat}", flush=True)
        if detaliat and v:
            print(f"         {v['catalyst']} | {v['reason']}", flush=True)


def main(argv=None, client=None, client_claude=None, azi: date | None = None,
         client_stiri=None, client_sec=None) -> int:
    """Clientii si `azi` se dau doar in teste; normal raman None."""
    p = argparse.ArgumentParser(description="Analistul pe zile trecute (fara ordine)")
    p.add_argument("--zi", type=date.fromisoformat, help="o singura zi, AAAA-LL-ZZ")
    p.add_argument("--de-la", type=date.fromisoformat, help="inceputul intervalului")
    p.add_argument("--pana-la", type=date.fromisoformat, help="sfarsitul intervalului (inclusiv)")
    p.add_argument("--ora", default="09:00", help="ora analizei in ET (implicit 09:00)")
    p.add_argument("--fara-claude", action="store_true", help="doar scannerul + evaluarea, fara cost")
    p.add_argument("--da", action="store_true", help="confirma costul estimat al unui interval lung")
    p.add_argument("--permite-inainte-de-limita", action="store_true",
                   help="ruleaza si zile dinaintea datei-limita a modelului (verdicte contaminate)")
    p.add_argument("--versiune", choices=["v1", "v2"], default="v1",
                   help="v1 = scanare + titluri (implicit); v2 = aceiasi candidati ca v1, cu news.py")
    a = p.parse_args(argv)
    if a.versiune == "v2" and a.fara_claude:
        p.error("--versiune v2 compara verdictele lui Claude; nu are sens cu --fara-claude")

    if a.zi:
        de_la = pana_la = a.zi
    elif a.de_la and a.pana_la:
        de_la, pana_la = a.de_la, a.pana_la
    else:
        p.error("da --zi sau --de-la impreuna cu --pana-la")
    ora = tuple(int(x) for x in a.ora.split(":"))

    # Jurnal separat de cel live (si plafon de apeluri separat), iar v2 separat de v1.
    director = DIR_REPLAY_V2 if a.versiune == "v2" else DIR_REPLAY
    analyst.DIR_JURNAL = director
    analyst.FISIER_CONTOR = os.path.join(director, "contor_apeluri.json")

    client = client or gap_scanner.ClientDate(pauza_intre_cereri_s=0.35)
    azi = azi or datetime.now(NY).date()
    zile = [z for z in zile_de_bursa(client, de_la, pana_la) if z < azi]
    if a.versiune == "v2":
        zile = [z for z in zile if evalueaza_gap.ultima_scanare(z.isoformat(), director=DIR_REPLAY)]
    if not zile:
        print("Nicio zi de bursa incheiata in interval.")
        return 1
    contaminate = [z for z in zile if z <= DATA_LIMITA_MODEL]
    if contaminate and not a.fara_claude and not a.permite_inainte_de_limita:
        print(f"Refuz: {len(contaminate)} zile sunt pana la {DATA_LIMITA_MODEL}, data-limita a "
              f"cunostintelor modelului. Verdictele ar fi contaminate (modelul poate sti "
              f"deznodamantul). Alege zile de dupa sau foloseste --fara-claude.")
        return 1

    if not a.fara_claude:
        # v2 trimite si rezumate + documente SEC: estimam ~1.5x tokeni de intrare
        pe_candidat = COST_ESTIMAT_PE_CANDIDAT_USD * (1.5 if a.versiune == "v2" else 1.0)
        cost = len(zile) * gap_scanner.MAX_CANDIDATI * pe_candidat
        print(f"{len(zile)} zile de bursa, cost maxim estimat ~{cost:.2f} USD "
              f"(cel mult {gap_scanner.MAX_CANDIDATI} candidati/zi)")
        if cost > PRAG_CONFIRMARE_USD and not a.da:
            print(f"Peste {PRAG_CONFIRMARE_USD:.2f} USD: ruleaza din nou cu --da ca sa confirmi.")
            return 1

    for z in zile:
        try:
            if a.versiune == "v2":
                ruleaza_zi_v2(z, client, detaliat=len(zile) == 1, client_claude=client_claude,
                              client_stiri=client_stiri, client_sec=client_sec)
            else:
                ruleaza_zi(z, client, cu_claude=not a.fara_claude, ora=ora, detaliat=len(zile) == 1,
                           client_claude=client_claude)
        except Exception as e:      # o zi stricata nu opreste restul intervalului
            print(f"{z}: EROARE {type(e).__name__}: {e}", flush=True)

    print()
    evaluari = [e for e in evalueaza_gap.toate_evaluarile(director)
                if de_la.isoformat() <= e["zi"] <= pana_la.isoformat()]
    evalueaza_gap.afiseaza_rezumat(evaluari)
    return 0


if __name__ == "__main__":
    sys.exit(main())
