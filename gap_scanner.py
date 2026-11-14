"""Scannerul de gap-uri: ce actiuni au urcat peste noapte, cu volum, si ce stiri au.

Fluxul, la ora analizei (ex. 9:00 ET):
  1. Universul: listele Alpaca "movers" (top 50 cresteri) + "most actives" (top 100
     dupa volum). Sunt singurele surse gratuite care se actualizeaza si in pre-market.
  2. Eliminam ce nu e actiune obisnuita: warrants, rights, units, OTC, netranzactionabile.
  3. Pentru fiecare simbol ramas calculam din bare SIP: inchiderea anterioara, ultimul
     pret pre-market, gap %, volumul pre-market si volumul relativ.
  4. Filtram dupa pret, gap si volum; pastram primii MAX_CANDIDATI dupa valoarea
     tranzactionata pre-market (pret x volum).
  5. Atasam titlurile stirilor din ultimele 24h.

De ce datele sunt "vechi" cu 16 minute: planul gratuit Alpaca nu permite date SIP mai
noi de 15 minute (403). La 9:00 lucram deci cu barele pana la 8:44. Folosim aceeasi
intarziere si cand rulam pe o zi trecuta, ca rezultatul sa fie identic cu cel live.

Fiecare simbol eliminat primeste un motiv, ca sa se poata vedea unde se pierd candidatii.
Doar gap-uri in sus (long). Modulul NU plaseaza ordine.
"""
import os
import time
from dataclasses import asdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

from analyst import Candidat

AICI = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(AICI, ".env"))

NY = ZoneInfo("America/New_York")

PRET_MIN = 2.0                  # sub 2 USD: penny stocks, spread-uri mari, manipulare
GAP_MIN_PCT = 5.0               # gap minim fata de inchiderea anterioara
VOLUM_PREMARKET_MIN = 50_000    # actiuni tranzactionate pre-market pana la ora limita
VOLUM_RELATIV_MIN = 2.0         # de cate ori peste volumul pre-market obisnuit
ZILE_ISTORIC_VOLUM = 10         # cate sesiuni anterioare intra in media volumului
MAX_CANDIDATI = 20              # cati ajung la analist (fiecare = un apel Claude, ~0.4 centi)
INTARZIERE_DATE = timedelta(minutes=16)
BURSE_PERMISE = {"NYSE", "NASDAQ", "AMEX", "ARCA", "BATS"}
CUVINTE_EXCLUSE = ("warrant", "right", " unit", "units")
ORA_START_PREMARKET = (4, 0)    # sesiunea extinsa incepe la 4:00 ET

URL_DATE = "https://data.alpaca.markets"


class ClientDate:
    """Apeluri HTTP simple catre Alpaca. In teste se inlocuieste cu unul fals."""

    def __init__(self, pauza_intre_cereri_s: float = 0.35):
        # Planul gratuit permite ~200 cereri/minut. O scanare cu universul complet face
        # ~80 de cereri in ~27 s fara pauze (~185/min, masurat 14 nov 2026) — prea aproape
        # de limita intr-o zi cu mai multe gap-uri. 0.35 s intre cereri = maxim ~170/min.
        self.pauza_intre_cereri_s = pauza_intre_cereri_s
        self._ultima_cerere = 0.0
        self.sesiune = requests.Session()
        self.sesiune.headers.update({
            "APCA-API-KEY-ID": os.getenv("ALPACA_API_KEY", ""),
            "APCA-API-SECRET-KEY": os.getenv("ALPACA_SECRET_KEY", ""),
        })
        self.url_trading = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

    def get(self, cale: str, params: dict | None = None, trading: bool = False,
            incercari: int = 3, pauza_s: float = 3.0):
        """GET cu reincercare la erori trecatoare (retea, 429, 5xx).

        Alpaca intoarce din cand in cand "Internal Server Error" (vezi istoricul v3);
        o eroare 4xx (cerere gresita, plan nepermis) nu se repara reincercand, deci iese direct.
        """
        baza = self.url_trading if trading else URL_DATE
        for i in range(incercari):
            asteptare = self._ultima_cerere + self.pauza_intre_cereri_s - time.monotonic()
            if asteptare > 0:
                time.sleep(asteptare)
            self._ultima_cerere = time.monotonic()
            try:
                r = self.sesiune.get(baza + cale, params=params or {}, timeout=30)
                r.raise_for_status()
                return r.json()
            except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as e:
                trecatoare = not isinstance(e, requests.HTTPError) or \
                    e.response is None or e.response.status_code == 429 or e.response.status_code >= 500
                if not trecatoare or i == incercari - 1:
                    raise
                time.sleep(pauza_s * (i + 1))


# ---------------------------------------------------------------- calcule pure (testabile)

def simbol_eligibil(activ: dict | None) -> tuple[bool, str]:
    """E o actiune obisnuita, tranzactionabila, pe o bursa mare?"""
    if activ is None:
        return False, "activ necunoscut la broker"
    if activ.get("class") != "us_equity" or not activ.get("tradable"):
        return False, "netranzactionabil"
    if activ.get("exchange") not in BURSE_PERMISE:
        return False, f"bursa {activ.get('exchange')}"
    simbol, nume = activ.get("symbol", ""), activ.get("name", "").lower()
    if "." in simbol or "/" in simbol:
        return False, "clasa speciala (simbol cu punct)"
    if any(c in nume for c in CUVINTE_EXCLUSE):
        return False, "warrant/right/unit"
    return True, ""


def _ora_et(bara: dict) -> datetime:
    return datetime.fromisoformat(bara["t"].replace("Z", "+00:00")).astimezone(NY)


def inchidere_anterioara(bare_zilnice: list[dict], zi) -> float | None:
    """Inchiderea ultimei sesiuni dinaintea zilei `zi`."""
    anterioare = [b for b in bare_zilnice if _ora_et(b).date() < zi]
    return anterioare[-1]["c"] if anterioare else None


def _bare_premarket(bare_5m: list[dict], zi, limita: datetime) -> list[dict]:
    """Barele de 5 min din ziua `zi`, de la 4:00 pana la ora corespunzatoare lui `limita`.

    O bara conteaza doar daca s-a terminat inainte de limita (inceput + 5 min <= limita).
    Pentru zilele trecute se foloseste aceeasi ora din zi, ca volumele sa fie comparabile.
    """
    rezultat = []
    for b in bare_5m:
        t = _ora_et(b)
        if t.date() != zi:
            continue
        start = t.replace(hour=ORA_START_PREMARKET[0], minute=ORA_START_PREMARKET[1],
                          second=0, microsecond=0)
        sfarsit = t.replace(hour=limita.hour, minute=limita.minute, second=0, microsecond=0)
        if start <= t and t + timedelta(minutes=5) <= sfarsit:
            rezultat.append(b)
    return rezultat


def statistici_premarket(bare_5m: list[dict], limita: datetime) -> dict | None:
    """Ultimul pret, volumul de azi si volumul relativ fata de sesiunile anterioare.

    None daca azi nu exista nicio bara pre-market.
    """
    azi = limita.date()
    bare_azi = _bare_premarket(bare_5m, azi, limita)
    if not bare_azi:
        return None
    volum_azi = sum(b["v"] for b in bare_azi)

    zile_anterioare = sorted({_ora_et(b).date() for b in bare_5m if _ora_et(b).date() < azi})
    zile_anterioare = zile_anterioare[-ZILE_ISTORIC_VOLUM:]
    volume = [sum(b["v"] for b in _bare_premarket(bare_5m, z, limita)) for z in zile_anterioare]
    medie = sum(volume) / len(volume) if volume else 0
    return {
        "ultim_pret": bare_azi[-1]["c"],
        "volum_premarket": volum_azi,
        # fara istoric (ex. IPO recent) sau istoric zero: volumul relativ nu se poate calcula
        "volum_relativ": volum_azi / medie if medie > 0 else None,
    }


def stiri_pe_simbol(stiri: list[dict], simboluri: list[str]) -> dict[str, list[dict]]:
    """Grupeaza titlurile pe simbol, cele mai noi primele. O stire poate avea mai multe simboluri."""
    rezultat = {s: [] for s in simboluri}
    for st in sorted(stiri, key=lambda x: x.get("created_at", ""), reverse=True):
        ora = datetime.fromisoformat(st["created_at"].replace("Z", "+00:00")).astimezone(NY)
        for s in st.get("symbols", []):
            if s in rezultat:
                rezultat[s].append({"ora": ora.strftime("%m-%d %H:%M ET"), "titlu": st["headline"]})
    return rezultat


# ---------------------------------------------------------------- date de la Alpaca

def univers_live(client: ClientDate) -> list[str]:
    """Simbolurile de verificat: cresterile mari + cele mai active. Fara duplicate."""
    movers = client.get("/v1beta1/screener/stocks/movers", {"top": 50})
    active = client.get("/v1beta1/screener/stocks/most-actives", {"by": "volume", "top": 100})
    simboluri = [m["symbol"] for m in movers.get("gainers", [])]
    simboluri += [a["symbol"] for a in active.get("most_actives", [])]
    return list(dict.fromkeys(simboluri))


def active_tranzactionabile(client: ClientDate) -> dict[str, dict]:
    """Toate actiunile active de la broker, indexate dupa simbol (un singur apel)."""
    lista = client.get("/v2/assets", {"status": "active", "asset_class": "us_equity"}, trading=True)
    return {a["symbol"]: a for a in lista}


SIMBOLURI_PE_CERERE = 400       # lista de simboluri merge in URL; loturi ca sa nu fie prea lung


def bare(client: ClientDate, simboluri: list[str], interval: str,
         start: datetime, end: datetime, ajustare: str = "split") -> dict[str, list[dict]]:
    """Bare SIP pentru mai multe simboluri, in loturi si cu paginare.

    `ajustare`: "split" (implicit, pentru gap-uri) sau "all" (si dividende — randament total,
    pentru backtest-uri pe ETF-uri).
    """
    rezultat = {s: [] for s in simboluri}
    for i in range(0, len(simboluri), SIMBOLURI_PE_CERERE):
        params = {"symbols": ",".join(simboluri[i:i + SIMBOLURI_PE_CERERE]),
                  "timeframe": interval, "feed": "sip", "start": start.isoformat(),
                  "end": end.isoformat(), "limit": 10000, "adjustment": ajustare}
        while True:
            r = client.get("/v2/stocks/bars", params)
            for s, lista in (r.get("bars") or {}).items():
                rezultat.setdefault(s, []).extend(lista)
            if not r.get("next_page_token"):
                break
            params["page_token"] = r["next_page_token"]
    return rezultat


def univers_complet(acum: datetime, client: ClientDate, maxim: int = 150) -> list[str]:
    """Universul reconstruit din bare, pentru orice zi (si trecuta, si azi).

    Ia TOATE actiunile obisnuite tranzactionabile, calculeaza gap-ul pre-market la ora
    limita si pastreaza cele cu gap, pret si volum peste praguri, primele `maxim` dupa
    valoarea tranzactionata. Foloseste doar date de pana la limita, deci nu "stie" ce
    s-a intamplat dupa ora analizei. E sursa pentru replay, unde listele "movers" nu
    exista (sunt doar in timp real).

    Limita cunoscuta: lista de actiuni e cea de azi, deci titlurile delistate intre timp
    lipsesc din zilele trecute (de obicei falimente — o mica partinire in sus).
    """
    acum = acum.astimezone(NY)
    limita = acum - INTARZIERE_DATE
    start_azi = acum.replace(hour=ORA_START_PREMARKET[0], minute=ORA_START_PREMARKET[1],
                             second=0, microsecond=0)
    active = active_tranzactionabile(client)
    eligibile = sorted(s for s, a in active.items() if simbol_eligibil(a)[0])
    zilnic = bare(client, eligibile, "1Day", acum - timedelta(days=10), start_azi)
    azi = bare(client, eligibile, "5Min", start_azi, limita)

    gasite = []
    for s in eligibile:
        stat = statistici_premarket(azi.get(s, []), limita)
        inchidere = inchidere_anterioara(zilnic.get(s, []), acum.date())
        if stat is None or inchidere is None:
            continue
        gap = (stat["ultim_pret"] / inchidere - 1) * 100
        if (gap >= GAP_MIN_PCT and stat["ultim_pret"] >= PRET_MIN
                and stat["volum_premarket"] >= VOLUM_PREMARKET_MIN):
            gasite.append((stat["ultim_pret"] * stat["volum_premarket"], s))
    return [s for _, s in sorted(gasite, reverse=True)[:maxim]]


def stiri(client: ClientDate, simboluri: list[str], start: datetime, end: datetime,
          max_pagini: int = 5) -> list[dict]:
    params = {"symbols": ",".join(simboluri), "start": start.isoformat(),
              "end": end.isoformat(), "limit": 50, "sort": "desc"}
    rezultat = []
    for _ in range(max_pagini):
        r = client.get("/v1beta1/news", params)
        rezultat.extend(r.get("news", []))
        if not r.get("next_page_token"):
            break
        params["page_token"] = r["next_page_token"]
    return rezultat


# ---------------------------------------------------------------- scanarea

def ataseaza_stiri_v2(candidati: list[Candidat], acum: datetime,
                      client_stiri=None, client_sec=None) -> None:
    """Versiunea 2 a stirilor (news.py): Alpaca cu rezumate + documente SEC + starea surselor.

    Completeaza `stiri_v2` (ce primeste Claude) si `stiri` (titlurile pentru afisare,
    in formatul vechi). Nu arunca: o sursa cazuta apare ca stare in `stiri_v2`.
    """
    import news
    rezultate = news.get_news_lot([c.simbol for c in candidati], acum,
                                  client_alpaca=client_stiri, client_sec=client_sec)
    for c in candidati:
        r = rezultate[c.simbol]
        c.stiri_v2 = asdict(r)
        c.stiri = [{"ora": datetime.fromisoformat(i["published_at"]).astimezone(NY).strftime("%m-%d %H:%M ET"),
                    "titlu": i["headline"]} for i in r.items]


def scaneaza(acum: datetime, client: ClientDate | None = None,
             univers: list[str] | None = None, versiune_stiri: str = "v1",
             client_stiri=None, client_sec=None) -> tuple[list[Candidat], list[dict]]:
    """Candidatii de gap la momentul `acum` si lista simbolurilor eliminate (cu motiv).

    `univers` se poate da explicit (ex. pentru o zi trecuta); altfel se ia live.
    `versiune_stiri`: "v1" = doar titluri Alpaca; "v2" = news.py (rezumate + SEC).
    """
    client = client or ClientDate()
    acum = acum.astimezone(NY)
    limita = acum - INTARZIERE_DATE
    respinsi = []

    univers = univers if univers is not None else univers_live(client)
    active = active_tranzactionabile(client)
    eligibile = []
    for s in univers:
        ok, motiv = simbol_eligibil(active.get(s))
        if ok:
            eligibile.append(s)
        else:
            respinsi.append({"simbol": s, "motiv": motiv})
    if not eligibile:
        return [], respinsi

    # Etapa 1, ieftina: doar barele de azi -> pret, gap, volum. Elimina majoritatea.
    zilnic = bare(client, eligibile, "1Day", acum - timedelta(days=15), limita)
    start_azi = acum.replace(hour=ORA_START_PREMARKET[0], minute=ORA_START_PREMARKET[1],
                             second=0, microsecond=0)
    azi = bare(client, eligibile, "5Min", start_azi, limita)

    trecuti = []
    for s in eligibile:
        inchidere = inchidere_anterioara(zilnic.get(s, []), acum.date())
        stat = statistici_premarket(azi.get(s, []), limita)
        if inchidere is None or stat is None:
            respinsi.append({"simbol": s, "motiv": "fara date pre-market sau inchidere anterioara"})
            continue
        gap = (stat["ultim_pret"] / inchidere - 1) * 100
        info = {"simbol": s, "pret": stat["ultim_pret"], "gap_pct": round(gap, 2),
                "volum_premarket": stat["volum_premarket"]}
        if stat["ultim_pret"] < PRET_MIN:
            respinsi.append({**info, "motiv": f"pret sub {PRET_MIN}"})
        elif gap < GAP_MIN_PCT:
            respinsi.append({**info, "motiv": f"gap sub {GAP_MIN_PCT}%"})
        elif stat["volum_premarket"] < VOLUM_PREMARKET_MIN:
            respinsi.append({**info, "motiv": f"volum pre-market sub {VOLUM_PREMARKET_MIN}"})
        else:
            trecuti.append(info)

    # Etapa 2: istoricul de volum doar pentru cei ramasi -> volumul relativ.
    if trecuti:
        istoric = bare(client, [t["simbol"] for t in trecuti], "5Min",
                       acum - timedelta(days=ZILE_ISTORIC_VOLUM + 6), start_azi)
    ramasi = []
    for t in trecuti:
        # Alpaca include si bara de la capatul intervalului, deci bara de 4:00 de azi poate
        # veni si in istoric; pastram din istoric doar zilele anterioare, ca sa nu o dublam.
        anterioare = [b for b in istoric.get(t["simbol"], []) if _ora_et(b).date() < acum.date()]
        stat = statistici_premarket(anterioare + azi.get(t["simbol"], []), limita)
        t["volum_relativ"] = stat["volum_relativ"]
        if t["volum_relativ"] is None:
            respinsi.append({**t, "motiv": "fara istoric de volum"})
        elif t["volum_relativ"] < VOLUM_RELATIV_MIN:
            respinsi.append({**t, "motiv": f"volum relativ sub {VOLUM_RELATIV_MIN}"})
        else:
            ramasi.append(t)
    trecuti = ramasi

    # Ordonam dupa valoarea tranzactionata (pret x volum), nu dupa volumul relativ: la
    # small caps fara volum pre-market obisnuit, volumul relativ explodeaza (mii de x)
    # tocmai la titlurile cele mai ilichide, unde executia e cea mai proasta.
    for t in trecuti:
        t["valoare_premarket_usd"] = round(t["pret"] * t["volum_premarket"])
    trecuti.sort(key=lambda x: x["valoare_premarket_usd"], reverse=True)
    for t in trecuti[MAX_CANDIDATI:]:
        respinsi.append({**t, "motiv": f"in afara primilor {MAX_CANDIDATI}"})
    trecuti = trecuti[:MAX_CANDIDATI]
    if not trecuti:
        return [], respinsi

    candidati = [Candidat(simbol=t["simbol"], gap_pct=t["gap_pct"],
                          volum_relativ=round(t["volum_relativ"], 2), pret=t["pret"],
                          float_actiuni=None)
                 for t in trecuti]
    if versiune_stiri == "v2":
        ataseaza_stiri_v2(candidati, acum, client_stiri=client_stiri, client_sec=client_sec)
        return candidati, respinsi
    titluri = stiri_pe_simbol(stiri(client, [t["simbol"] for t in trecuti],
                                    acum - timedelta(hours=24), acum),
                              [t["simbol"] for t in trecuti])
    for c in candidati:
        c.stiri = titluri[c.simbol]
    return candidati, respinsi
