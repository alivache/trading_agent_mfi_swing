"""Stirile pentru analist (versiunea 2): Alpaca News + documente SEC EDGAR.

`get_news(simbol, as_of)` intoarce un `RezultatStiri` cu stiri normalizate, oricare
ar fi sursa:
    source, headline, summary (max ~500 caractere), published_at (UTC, ISO),
    url, type ("news" sau "filing")

Reguli (de ce arata codul asa):

1. `as_of` e obligatoriu si strict. Nu intoarcem NICIODATA ceva publicat dupa `as_of`,
   chiar daca API-ul ar trimite. Altfel un replay pe zile trecute ar "vedea viitorul"
   si ar arata mai bine decat realitatea. Verificam in cod, nu ne bazam doar pe
   parametrul `end` al API-ului.

2. Doua stari diferite, ca sa le putem deosebi in jurnal:
     - lista goala + stare_stiri "ok"            = am cautat si nu e nimic;
     - stare_stiri "news_unavailable"            = nu stim (sursa a cazut).
   La fel pentru documentele SEC ("filings_unavailable"). In ambele cazuri analistul
   da "avoid" daca nu are altceva, dar jurnalul arata de ce.

3. Cache pe disc pe (zi, simbol, ora as_of), ca un replay repetat sa nu refaca
   apelurile si sa vada exact aceleasi stiri. Cheia include ora: la 9:00 si la 12:00
   pot exista stiri diferite. Esecurile NU se pun in cache, ca o reincercare sa poata reusi.

4. Finnhub (rezerva din specificatia initiala) lipseste deliberat: Alpaca n-a cazut
   pana acum, iar o a doua sursa ar putea da stiri diferite live fata de replay.
   Se poate adauga in `_stiri_alpaca` daca apar esecuri reale.
"""
import html
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

AICI = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(AICI, ".env"))

FEREASTRA = timedelta(hours=24)
MAX_STIRI = 10
MAX_SUMAR = 500
DIR_CACHE = os.path.join(AICI, "cache_stiri")


@dataclass
class RezultatStiri:
    simbol: str
    as_of: str                                   # ISO, UTC
    items: list[dict] = field(default_factory=list)
    stare_stiri: str = "ok"                      # "ok" sau "news_unavailable"
    stare_filings: str = "ok"                    # "ok", "filings_unavailable", "fara_cik", "dezactivat"
    erori: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- normalizare (pura)

def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


def _trunchiaza(text: str, n: int = MAX_SUMAR) -> str:
    # Benzinga trimite uneori text cu entitati HTML ("S&amp;P", "Thursday&#39;s")
    text = " ".join(html.unescape(text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def normalizeaza_alpaca(stire: dict) -> dict:
    return {
        "source": f"alpaca/{stire.get('source') or 'benzinga'}",
        "headline": _trunchiaza(stire.get("headline", ""), 300),
        "summary": _trunchiaza(stire.get("summary", "")),
        "published_at": _utc(stire["created_at"]).isoformat(),
        "url": stire.get("url", ""),
        "type": "news",
    }


def _cheie_dedup(item: dict) -> str:
    """Acelasi titlu de la surse diferite: litere si cifre, fara majuscule/punctuatie."""
    return re.sub(r"[^a-z0-9]", "", item["headline"].lower())[:120]


def in_fereastra(items: list[dict], as_of: datetime) -> list[dict]:
    """Doar ce e publicat in ultimele 24h inainte de `as_of` (inclusiv), deduplicat,
    cele mai noi primele, maxim MAX_STIRI in total.

    Documentele SEC au prioritate la plafon: un prospect de oferta (motiv automat de
    "avoid") nu trebuie impins afara de articole generale de tip "stocks moving".
    """
    as_of = as_of.astimezone(timezone.utc)
    start = as_of - FEREASTRA
    bune = [i for i in items if start <= _utc(i["published_at"]) <= as_of]
    bune.sort(key=lambda i: i["published_at"], reverse=True)
    vazute, unice = set(), []
    for i in bune:
        k = _cheie_dedup(i)
        if k and k not in vazute:
            vazute.add(k)
            unice.append(i)
    filings = [i for i in unice if i["type"] == "filing"][:MAX_STIRI]
    stiri = [i for i in unice if i["type"] == "news"][:MAX_STIRI - len(filings)]
    return sorted(stiri + filings, key=lambda i: i["published_at"], reverse=True)


# ---------------------------------------------------------------- surse

class ClientAlpacaStiri:
    URL = "https://data.alpaca.markets/v1beta1/news"

    def __init__(self, timeout_s: float = 10.0, pauza_s: float = 0.35):
        self.timeout_s, self.pauza_s, self._ultima = timeout_s, pauza_s, 0.0
        self.sesiune = requests.Session()
        self.sesiune.headers.update({
            "APCA-API-KEY-ID": os.getenv("ALPACA_API_KEY", ""),
            "APCA-API-SECRET-KEY": os.getenv("ALPACA_SECRET_KEY", ""),
        })

    def stiri(self, simboluri: list[str], start: datetime, end: datetime) -> list[dict]:
        """Toate stirile brute pentru simboluri in [start, end], cu paginare."""
        params = {"symbols": ",".join(simboluri), "start": start.isoformat(),
                  "end": end.isoformat(), "limit": 50, "sort": "desc", "include_content": "false"}
        rezultat = []
        for _ in range(10):
            asteptare = self._ultima + self.pauza_s - time.monotonic()
            if asteptare > 0:
                time.sleep(asteptare)
            self._ultima = time.monotonic()
            r = self.sesiune.get(self.URL, params=params, timeout=self.timeout_s)
            r.raise_for_status()
            j = r.json()
            rezultat += j.get("news", [])
            if not j.get("next_page_token"):
                break
            params["page_token"] = j["next_page_token"]
        return rezultat


# ---------------------------------------------------------------- cache

# Se mareste cand se schimba normalizarea (ex. decodarea HTML din 14 nov 2026), ca un
# cache vechi sa nu mai fie citit: altfel un replay repetat ar vedea textul vechi.
VERSIUNE_FORMAT = 2


def _cale_cache(simbol: str, as_of: datetime) -> str:
    t = as_of.astimezone(timezone.utc)
    return os.path.join(DIR_CACHE, f"format{VERSIUNE_FORMAT}", f"{t:%Y-%m-%d}", f"{simbol}_{t:%H%M}.json")


def citeste_cache(simbol: str, as_of: datetime) -> RezultatStiri | None:
    try:
        with open(_cale_cache(simbol, as_of), encoding="utf-8") as f:
            return RezultatStiri(**json.load(f))
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        return None


def scrie_cache(r: RezultatStiri, as_of: datetime) -> None:
    if r.stare_stiri != "ok" or r.stare_filings == "filings_unavailable":
        return      # un esec nu se memoreaza: data viitoare reincercam
    cale = _cale_cache(r.simbol, as_of)
    os.makedirs(os.path.dirname(cale), exist_ok=True)
    tmp = cale + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(asdict(r), f, ensure_ascii=False, indent=1)
    os.replace(tmp, cale)


# ---------------------------------------------------------------- functia principala

def get_news_lot(simboluri: list[str], as_of: datetime, client_alpaca=None, client_sec=None,
                 cu_filings: bool = True, cache: bool = True) -> dict[str, RezultatStiri]:
    """Stirile pentru mai multi simboluri deodata (un singur apel Alpaca pentru toti)."""
    if as_of.tzinfo is None:
        raise ValueError("as_of trebuie sa aiba fus orar (ex. America/New_York)")
    rezultat, de_cautat = {}, []
    for s in simboluri:
        din_cache = citeste_cache(s, as_of) if cache else None
        if din_cache is not None:
            rezultat[s] = din_cache
        else:
            de_cautat.append(s)
    if not de_cautat:
        return rezultat

    as_of_utc = as_of.astimezone(timezone.utc)
    brute_pe_simbol = {s: [] for s in de_cautat}
    eroare_stiri = None
    try:
        client_alpaca = client_alpaca or ClientAlpacaStiri()
        for st in client_alpaca.stiri(de_cautat, as_of_utc - FEREASTRA, as_of_utc):
            item = normalizeaza_alpaca(st)
            for s in st.get("symbols", []):
                if s in brute_pe_simbol:
                    brute_pe_simbol[s].append(item)
    except Exception as e:
        eroare_stiri = f"alpaca: {type(e).__name__}: {e}"

    for s in de_cautat:
        r = RezultatStiri(simbol=s, as_of=as_of_utc.isoformat())
        if eroare_stiri:
            r.stare_stiri = "news_unavailable"
            r.erori.append(eroare_stiri)
        items = list(brute_pe_simbol[s])
        if not cu_filings:
            r.stare_filings = "dezactivat"
        else:
            try:
                import sec_edgar
                filings = sec_edgar.filings(s, as_of_utc - FEREASTRA, as_of_utc, client=client_sec)
                if filings is None:
                    r.stare_filings = "fara_cik"
                else:
                    items += filings
            except Exception as e:
                r.stare_filings = "filings_unavailable"
                r.erori.append(f"sec: {type(e).__name__}: {e}")
        r.items = in_fereastra(items, as_of_utc)
        if cache:
            scrie_cache(r, as_of)
        rezultat[s] = r
    return rezultat


def get_news(simbol: str, as_of: datetime, **kw) -> RezultatStiri:
    """Stirile unui singur simbol din ultimele 24h dinainte de `as_of`."""
    return get_news_lot([simbol], as_of, **kw)[simbol]
