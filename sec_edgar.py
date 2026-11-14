"""Documentele depuse la SEC (EDGAR) in ultimele 24h, ca stiri normalizate pentru analist.

De ce conteaza: un gap urmat de o oferta de actiuni (prospect 424B, inregistrare S-1/S-3,
8-K cu item 3.02) tinde sa se intoarca repede. Benzinga nu prinde mereu asemenea
documente la small caps; SEC le are pe toate, cu ora exacta a depunerii.

Reguli SEC (https://www.sec.gov/os/accessing-edgar-data, verificat 14 nov 2026):
  - fara cheie API, dar header-ul User-Agent trebuie sa contina nume + email de contact;
    il citim din SEC_USER_AGENT din .env (nu e in cod si nu ajunge in git);
  - maxim 10 cereri pe secunda -> pauza de 0.15 s intre cereri;
  - API-ul "submissions" se actualizeaza in timp real (intarziere tipica sub o secunda).

Endpoint-uri:
  https://www.sec.gov/files/company_tickers.json          simbol -> CIK (codul companiei)
  https://data.sec.gov/submissions/CIK##########.json     documentele recente, pe coloane
"""
import json
import os
import time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

AICI = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(AICI, ".env"))

URL_TICKERE = "https://www.sec.gov/files/company_tickers.json"
URL_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
DIR_CACHE = os.path.join(AICI, "cache_stiri")

# Doar documentele care pot explica (sau contrazice) un gap. Formularele 3/4/5 (tranzactii
# ale insiderilor) si 144 sunt foarte dese si ar ingropa restul.
FORME_RELEVANTE = {
    "8-K", "8-K/A", "6-K", "10-Q", "10-K", "20-F", "425", "DEFM14A", "SC TO-T",
    "SC 13D", "SC 13D/A",
    "S-1", "S-1/A", "S-3", "S-3/A", "S-3ASR", "F-1", "F-1/A", "F-3", "F-3/A",
    "424B1", "424B2", "424B3", "424B4", "424B5", "424B7", "424B8",
}
FORME_OFERTA = {"S-1", "S-1/A", "S-3", "S-3/A", "S-3ASR", "F-1", "F-1/A", "F-3", "F-3/A",
                "424B1", "424B2", "424B3", "424B4", "424B5", "424B7", "424B8"}
ITEME_8K = {
    "1.01": "material definitive agreement",
    "1.03": "bankruptcy or receivership",
    "2.01": "completion of acquisition or disposition",
    "2.02": "results of operations (earnings)",
    "3.01": "delisting notice or listing rule failure",
    "3.02": "unregistered sales of equity securities (dilution)",
    "3.03": "material modification of shareholder rights",
    "5.02": "director or officer change",
    "5.03": "charter or bylaw amendment (often a reverse split)",
    "5.07": "shareholder vote results",
    "7.01": "Regulation FD disclosure",
    "8.01": "other events",
    "9.01": "financial statements and exhibits",
}


class ClientSec:
    def __init__(self, pauza_s: float = 0.15, timeout_s: float = 10.0):
        agent = os.getenv("SEC_USER_AGENT", "").strip()
        if not agent:
            # SEC blocheaza cererile fara contact; mai bine un esec clar decat 403-uri
            raise RuntimeError("SEC_USER_AGENT lipseste din .env (nume + email, cerinta SEC)")
        self.pauza_s, self.timeout_s, self._ultima = pauza_s, timeout_s, 0.0
        self.sesiune = requests.Session()
        self.sesiune.headers.update({"User-Agent": agent, "Accept-Encoding": "gzip, deflate"})

    def get_json(self, url: str):
        asteptare = self._ultima + self.pauza_s - time.monotonic()
        if asteptare > 0:
            time.sleep(asteptare)
        self._ultima = time.monotonic()
        r = self.sesiune.get(url, timeout=self.timeout_s)
        r.raise_for_status()
        return r.json()


_harta_cik: dict[str, int] | None = None
_harta_zi: str | None = None


def harta_cik(client: ClientSec) -> dict[str, int]:
    """Simbol -> CIK. Fisierul SEC se descarca o data pe zi si se tine pe disc."""
    global _harta_cik, _harta_zi
    zi = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _harta_cik is not None and _harta_zi == zi:
        return _harta_cik
    cale = os.path.join(DIR_CACHE, f"sec_tickere_{zi}.json")
    try:
        with open(cale, encoding="utf-8") as f:
            date = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        date = client.get_json(URL_TICKERE)
        os.makedirs(DIR_CACHE, exist_ok=True)
        with open(cale, "w", encoding="utf-8") as f:
            json.dump(date, f)
    _harta_cik = {v["ticker"].upper(): int(v["cik_str"]) for v in date.values()}
    _harta_zi = zi
    return _harta_cik


def _descriere(forma: str, iteme: str, descriere_doc: str) -> tuple[str, str]:
    """Titlul si rezumatul documentului, in engleza (ca restul datelor pentru Claude)."""
    coduri = [c.strip() for c in (iteme or "").split(",") if c.strip()]
    explicatii = [f"Item {c}: {ITEME_8K.get(c, 'other')}" for c in coduri]
    titlu = f"SEC filing {forma}"
    if forma in FORME_OFERTA:
        titlu += " (registration/prospectus: possible share offering)"
    if explicatii:
        titlu += " - " + "; ".join(e.split(": ", 1)[1] for e in explicatii)
    rezumat = "; ".join(x for x in [descriere_doc, *explicatii] if x)
    return titlu, rezumat


def filings_din_submissions(date: dict, cik: int, start: datetime, end: datetime) -> list[dict]:
    """Documentele relevante depuse intre start si end (inclusiv), normalizate. Functie pura."""
    recente = (date.get("filings") or {}).get("recent") or {}
    forme = recente.get("form", [])
    rezultat = []
    for i, forma in enumerate(forme):
        if forma not in FORME_RELEVANTE:
            continue
        cand = recente["acceptanceDateTime"][i]
        t = datetime.fromisoformat(cand.replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        t = t.astimezone(timezone.utc)
        if not start <= t <= end:
            continue
        acc = recente["accessionNumber"][i]
        doc = (recente.get("primaryDocument") or [""] * len(forme))[i]
        titlu, rezumat = _descriere(forma, (recente.get("items") or [""] * len(forme))[i],
                                    (recente.get("primaryDocDescription") or [""] * len(forme))[i])
        rezultat.append({
            "source": "sec/edgar",
            "headline": titlu,
            "summary": rezumat[:500],
            "published_at": t.isoformat(),
            "url": f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/{doc}",
            "type": "filing",
            "form": forma,
        })
    return rezultat


def filings(simbol: str, start: datetime, end: datetime, client: ClientSec | None = None) -> list[dict] | None:
    """Documentele SEC relevante ale simbolului in [start, end]; None daca SEC nu-l cunoaste."""
    client = client or ClientSec()
    cik = harta_cik(client).get(simbol.upper())
    if cik is None:
        return None
    return filings_din_submissions(client.get_json(URL_SUBMISSIONS.format(cik=cik)), cik, start, end)
