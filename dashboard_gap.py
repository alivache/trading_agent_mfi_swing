"""Dashboard pentru versiunea gap + analist Claude (port 8080, READ-ONLY).

Inlocuieste dashboard.py (v3) pe acelasi port si in acelasi serviciu systemd
(`dashboard-v3`). Nu vorbeste cu brokerul si nu cheama Claude: doar citeste
jurnalele scrise de gap_shadow.py (live) si replay_analist.py (replay).

Pagini:
  /              ziua cea mai recenta cu scanare: candidati, verdicte, rezultate
  /zi/AAAA-LL-ZZ o zi anume
  /rezultate     tradeable vs avoid, cumulat (?sursa=live sau ?sursa=replay)
  /api/stare     acelasi lucru ca JSON, pentru verificari rapide
"""
import collections
import json
import os
import re
import statistics
import subprocess
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from flask import Flask, abort, jsonify, render_template_string, request

import analyst
import evalueaza_gap
from analyst import NY

RO = ZoneInfo("Europe/Bucharest")
DIR_LIVE = analyst.DIR_JURNAL
DIR_REPLAY = os.path.join(analyst.AICI, "jurnal_replay")
DIR_REPLAY_V2 = os.path.join(analyst.AICI, "jurnal_replay_v2")
SURSE = {"live": DIR_LIVE, "replay": DIR_REPLAY, "replay_v2": DIR_REPLAY_V2}
PRET_INPUT_USD_PE_MTOK = 2.0      # claude-sonnet-5-5
PRET_OUTPUT_USD_PE_MTOK = 10.0    # include si tokenii de gandire
FORMAT_ZI = re.compile(r"^\d{4}-\d{2}-\d{2}$")

app = Flask(__name__)


# ---------------------------------------------------------------- citirea jurnalelor

def citeste_jsonl(cale: str) -> list[dict]:
    if not os.path.exists(cale):
        return []
    rezultat = []
    with open(cale, encoding="utf-8") as f:
        for linie in f:
            linie = linie.strip()
            if linie:
                try:
                    rezultat.append(json.loads(linie))
                except json.JSONDecodeError:
                    pass      # o linie scrisa pe jumatate (crash) nu strica pagina
    return rezultat


def zile_cu_scanari(director: str) -> list[str]:
    """Zilele cu cel putin o scanare, cele mai noi primele."""
    if not os.path.isdir(director):
        return []
    zile = [f[len("scanari_"):-len(".jsonl")] for f in os.listdir(director)
            if f.startswith("scanari_") and f.endswith(".jsonl")]
    return sorted(zile, reverse=True)


def stare_serviciu(nume: str = "gap-shadow") -> str:
    try:
        r = subprocess.run(["systemctl", "is-active", nume], capture_output=True, text=True, timeout=3)
        return r.stdout.strip() or "necunoscut"
    except (OSError, subprocess.TimeoutExpired):
        return "necunoscut"


def cost_zi(director: str, zi: str) -> dict:
    """Apeluri Claude si cost estimat din jurnalul analistului pentru ziua `zi`."""
    apeluri = [a for a in citeste_jsonl(os.path.join(director, f"{zi}.jsonl")) if a.get("apel_trimis")]
    intrare = sum((a.get("usage") or {}).get("input_tokens", 0) for a in apeluri)
    iesire = sum((a.get("usage") or {}).get("output_tokens", 0) for a in apeluri)
    cost = intrare / 1e6 * PRET_INPUT_USD_PE_MTOK + iesire / 1e6 * PRET_OUTPUT_USD_PE_MTOK
    return {"apeluri": len(apeluri), "tokeni_intrare": intrare, "tokeni_iesire": iesire,
            "cost_usd": round(cost, 4), "plafon": analyst.MAX_APELURI_PE_ZI}


def urmatoarea_analiza(acum: datetime) -> datetime:
    """Urmatoarea zi lucratoare la 9:00 ET (sarbatorile le sare serviciul, nu pagina)."""
    t = acum.astimezone(NY).replace(hour=9, minute=0, second=0, microsecond=0)
    if acum.astimezone(NY) >= t:
        t += timedelta(days=1)
    while t.weekday() >= 5:
        t += timedelta(days=1)
    return t


def date_zi(director: str, zi: str) -> dict:
    """Tot ce arata pagina unei zile: ultima rulare, randurile pe candidat, palnia, costul."""
    rulari = citeste_jsonl(os.path.join(director, f"scanari_{zi}.jsonl"))
    ultima = rulari[-1] if rulari else None
    evaluari = {e["simbol"]: e for e in citeste_jsonl(os.path.join(director, f"evaluare_{zi}.jsonl"))}
    randuri = []
    if ultima:
        verdicte = {v["simbol"]: v for v in ultima.get("verdicte", [])}
        for c in ultima.get("candidati", []):
            randuri.append({"c": c, "v": verdicte.get(c["simbol"]), "e": evaluari.get(c["simbol"])})
    return {
        "zi": zi,
        "ultima": ultima,
        "nr_rulari": len(rulari),
        "randuri": randuri,
        "palnie": sorted((ultima or {}).get("respinsi_pe_motiv", {}).items(), key=lambda x: -x[1]),
        "nr_tradeable": sum(1 for r in randuri if r["v"] and r["v"]["valid"] and r["v"]["verdict"] == "tradeable"),
        "nr_avoid": sum(1 for r in randuri if r["v"] and r["v"]["verdict"] == "avoid"),
        "cost": cost_zi(director, zi),
        "evaluat": bool(evaluari),
    }


def _grupa(e: dict) -> str:
    if e.get("valid"):
        return e["verdict"]
    return "fara verdict" if e.get("verdict") == "fara verdict" else "invalid"


def serie_cumulata(evaluari: list[dict]) -> dict:
    """Excesul vs QQQ cumulat pe zile, pe grupe.

    Pentru fiecare zi luam media excesului candidatilor din grupa (ca un portofoliu
    egal ponderat in ziua aceea), 0 daca grupa n-a avut candidati, si adunam zilele.
    """
    pe_zi = collections.defaultdict(lambda: collections.defaultdict(list))
    for e in evaluari:
        if "exces_pct" not in e:
            continue
        pe_zi[e["zi"]]["toti"].append(e["exces_pct"])
        pe_zi[e["zi"]][_grupa(e)].append(e["exces_pct"])
    zile = sorted(pe_zi)
    serii = {"zile": zile}
    for g in ("toti", "tradeable", "avoid"):
        total, valori = 0.0, []
        for z in zile:
            lista = pe_zi[z].get(g, [])
            total += statistics.mean(lista) if lista else 0.0
            valori.append(round(total, 3))
        serii[g] = valori
    return serii


def tabel_pe_zile(evaluari: list[dict]) -> list[dict]:
    pe_zi = collections.defaultdict(list)
    for e in evaluari:
        if "exces_pct" in e:
            pe_zi[e["zi"]].append(e)

    def medie(lista):
        return round(statistics.mean(x["exces_pct"] for x in lista), 2) if lista else None

    randuri = []
    for z in sorted(pe_zi, reverse=True):
        lista = pe_zi[z]
        t = [e for e in lista if _grupa(e) == "tradeable"]
        a = [e for e in lista if _grupa(e) == "avoid"]
        randuri.append({"zi": z, "n": len(lista), "nt": len(t), "na": len(a),
                        "toti": medie(lista), "t": medie(t), "a": medie(a)})
    return randuri


# ---------------------------------------------------------------- pagina

PAGINA = """<!DOCTYPE html>
<html lang="ro">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Gap Shadow</title>
<meta http-equiv="refresh" content="120">
{% if tab == 'rezultate' %}<script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>{% endif %}
<style>
  :root { color-scheme: dark;
    --plan:#0d1117; --suprafata:#161b22; --chenar:#30363d; --linie:#21262d;
    --text:#c9d1d9; --text2:#8b949e; --verde:#3fb950; --rosu:#f85149;
    --s-tradeable:#3987e5; --s-avoid:#d95926; --s-toti:#199e70; }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { background:var(--plan); color:var(--text); font-family:system-ui,-apple-system,"Segoe UI",sans-serif;
         padding:16px; max-width:1300px; margin:0 auto; }
  h1 { font-size:20px; margin-bottom:4px; }
  h2 { font-size:16px; margin:24px 0 12px; }
  .sub { color:var(--text2); font-size:13px; margin-bottom:16px; }
  .tabs { display:flex; gap:8px; margin-bottom:16px; flex-wrap:wrap; }
  .tab { padding:8px 16px; background:var(--suprafata); border:1px solid var(--chenar); border-radius:6px;
         text-decoration:none; color:var(--text); font-size:14px; }
  .tab.activ { background:#1f6feb; border-color:#1f6feb; color:#fff; }
  .banda { padding:10px 14px; border-radius:8px; margin-bottom:16px; font-size:14px;
           background:var(--suprafata); border:1px solid var(--chenar); }
  .banda b { color:#fff; } .banda b.verde { color:var(--verde); } .banda b.rosu { color:var(--rosu); }
  .carduri { display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:12px; margin-bottom:8px; }
  .card { background:var(--suprafata); border:1px solid var(--chenar); border-radius:8px; padding:14px; }
  .card .eticheta { color:var(--text2); font-size:12px; }
  .card .valoare { font-size:22px; font-weight:600; margin-top:4px; }
  .card .detaliu { color:var(--text2); font-size:12px; margin-top:2px; }
  .verde { color:var(--verde); } .rosu { color:var(--rosu); } .gri { color:var(--text2); }
  .tabel-box { overflow-x:auto; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th,td { text-align:left; padding:8px; border-bottom:1px solid var(--linie); vertical-align:top; }
  th { color:var(--text2); font-weight:500; white-space:nowrap; }
  td.num, th.num { text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }
  .badge { display:inline-block; padding:2px 8px; border-radius:10px; font-size:11px; font-weight:600; color:#fff; }
  .b-tradeable { background:var(--s-tradeable); } .b-avoid { background:var(--s-avoid); }
  .b-invalid { background:#484f58; }
  details { margin-top:6px; } summary { cursor:pointer; color:var(--text2); font-size:12px; }
  .motiv { color:var(--text); font-size:13px; margin:6px 0; max-width:780px; line-height:1.45; }
  .stiri { list-style:none; font-size:12px; color:var(--text2); line-height:1.6; }
  .grafic-box { background:var(--suprafata); border:1px solid var(--chenar); border-radius:8px; padding:8px; }
  .gol { color:var(--text2); padding:24px; text-align:center; background:var(--suprafata);
         border:1px dashed var(--chenar); border-radius:8px; }
  .zile { display:flex; flex-wrap:wrap; gap:6px; }
  .zile a { color:var(--text); font-size:12px; padding:4px 8px; border:1px solid var(--chenar);
            border-radius:6px; text-decoration:none; }
  .zile a.activ { border-color:#1f6feb; color:#fff; }
  .nota { color:var(--text2); font-size:12px; margin-top:8px; line-height:1.5; }
</style>
</head>
<body>
  <h1>Gap Shadow — analist Claude</h1>
  <div class="sub">Actualizat {{ acum_et }} ET ({{ acum_ro }} ora RO) · reîmprospătare la 120 s</div>

  <div class="banda">
    <b>Mod shadow: fără ordine.</b>
    Serviciul <code>gap-shadow</code>: <b class="{{ 'verde' if serviciu == 'active' else 'rosu' }}">{{ serviciu }}</b>
    · următoarea analiză: {{ urmatoarea_et }} ET ({{ urmatoarea_ro }} ora RO)
    · model {{ model }}
  </div>

  <div class="tabs">
    <a href="/" class="tab {{ 'activ' if tab == 'zi' else '' }}">Ziua</a>
    <a href="/rezultate?sursa=live" class="tab {{ 'activ' if tab == 'rezultate' and sursa == 'live' else '' }}">Rezultate live</a>
    <a href="/rezultate?sursa=replay" class="tab {{ 'activ' if tab == 'rezultate' and sursa == 'replay' else '' }}">Rezultate replay</a>
    <a href="/rezultate?sursa=replay_v2" class="tab {{ 'activ' if tab == 'rezultate' and sursa == 'replay_v2' else '' }}">Replay v2 (știri + SEC)</a>
  </div>

{% if tab == 'zi' %}
  {% if not d.ultima %}
    <div class="gol">Nicio scanare încă. Prima analiză rulează la {{ urmatoarea_et }} ET ({{ urmatoarea_ro }} ora României).</div>
  {% else %}
    <h2>{{ d.zi }}{% if d.nr_rulari > 1 %} <span class="gri">· ultima din {{ d.nr_rulari }} rulări</span>{% endif %}</h2>
    {% if d.ultima.eroare %}<div class="banda rosu">Scanarea a eșuat: {{ d.ultima.eroare }}</div>{% endif %}
    <div class="carduri">
      <div class="card"><div class="eticheta">Gap-uri găsite</div><div class="valoare">{{ d.ultima.get('univers', '–') }}</div><div class="detaliu">gap ≥ 5%, preț, volum</div></div>
      <div class="card"><div class="eticheta">Candidați</div><div class="valoare">{{ d.randuri|length }}</div><div class="detaliu">după toate filtrele</div></div>
      <div class="card"><div class="eticheta">Tradeable</div><div class="valoare">{{ d.nr_tradeable }}</div></div>
      <div class="card"><div class="eticheta">Avoid</div><div class="valoare">{{ d.nr_avoid }}</div></div>
      <div class="card"><div class="eticheta">Apeluri Claude</div><div class="valoare">{{ d.cost.apeluri }}<span class="gri" style="font-size:14px"> / {{ d.cost.plafon }}</span></div><div class="detaliu">plafon zilnic</div></div>
      <div class="card"><div class="eticheta">Cost estimat</div><div class="valoare">${{ "%.3f"|format(d.cost.cost_usd) }}</div><div class="detaliu">{{ d.cost.tokeni_intrare }} in · {{ d.cost.tokeni_iesire }} out</div></div>
    </div>

    <h2>Candidați și verdicte</h2>
    {% if not d.randuri %}
      <div class="gol">Niciun candidat nu a trecut de filtre în ziua asta.</div>
    {% else %}
    <div class="tabel-box"><table>
      <thead><tr>
        <th>Simbol</th><th class="num">Gap</th><th class="num">Preț</th><th class="num">Vol. rel.</th>
        <th class="num">Știri</th><th>Verdict</th><th>Catalizator și motiv</th>
        <th class="num">Rezultat</th><th class="num">Exces vs QQQ</th><th class="num">MFE / MAE</th>
      </tr></thead>
      <tbody>
      {% for r in d.randuri %}
        <tr>
          <td><b>{{ r.c.simbol }}</b></td>
          <td class="num">{{ "%+.1f"|format(r.c.gap_pct) }}%</td>
          <td class="num">{{ "%.2f"|format(r.c.pret) }}</td>
          <td class="num">{{ ("%.1f"|format(r.c.volum_relativ) ~ "×") if r.c.volum_relativ < 1000 else ">999×" }}</td>
          <td class="num">{{ r.c.stiri|length }}</td>
          <td>
            {% if r.v %}
              {% set cls = 'b-' ~ (r.v.verdict if r.v.valid else 'invalid') %}
              <span class="badge {{ cls }}">{{ r.v.verdict if r.v.valid else 'avoid (eșec)' }}</span>
              {% if r.v.valid and r.v.verdict == 'tradeable' %}<div class="gri" style="font-size:12px;margin-top:4px">mărime ×{{ "%.2f"|format(r.v.factor_marime) }} · încredere {{ "%.2f"|format(r.v.confidence) }}</div>{% endif %}
            {% else %}<span class="gri">fără verdict</span>{% endif %}
          </td>
          <td>
            {% if r.v %}<div>{{ r.v.catalyst }}</div>
              <details><summary>motiv și știri</summary>
                <div class="motiv">{{ r.v.reason }}</div>
                {% if r.c.stiri %}<ul class="stiri">{% for s in r.c.stiri %}<li>[{{ s.ora }}] {{ s.titlu }}</li>{% endfor %}</ul>
                {% else %}<div class="gri" style="font-size:12px">fără știri în ultimele 24h</div>{% endif %}
              </details>
            {% endif %}
          </td>
          {% if r.e and r.e.exces_pct is defined %}
            <td class="num {{ 'verde' if r.e.randament_pct > 0 else 'rosu' }}">{{ "%+.2f"|format(r.e.randament_pct) }}%</td>
            <td class="num {{ 'verde' if r.e.exces_pct > 0 else 'rosu' }}">{{ "%+.2f"|format(r.e.exces_pct) }}%</td>
            <td class="num gri">{{ "%+.1f"|format(r.e.mfe_pct) }} / {{ "%+.1f"|format(r.e.mae_pct) }}</td>
          {% else %}
            <td class="num gri" colspan="3">{{ 'fără date' if d.evaluat else 'se evaluează după închidere' }}</td>
          {% endif %}
        </tr>
      {% endfor %}
      </tbody>
    </table></div>
    <div class="nota">Rezultat = intrare la deschidere (9:30), ieșire la închidere − 10 min, la fel pentru toți candidații.
      Exces = rezultatul minus QQQ pe același interval. MFE / MAE = cea mai bună / cea mai proastă excursie de la intrare.</div>
    {% endif %}

    {% if d.palnie %}
    <h2>Eliminați de filtre</h2>
    <div class="tabel-box"><table style="max-width:520px">
      <tbody>{% for motiv, n in d.palnie %}<tr><td>{{ motiv }}</td><td class="num">{{ n }}</td></tr>{% endfor %}</tbody>
    </table></div>
    {% endif %}
  {% endif %}

  {% if zile %}
    <h2>Alte zile</h2>
    <div class="zile">{% for z in zile %}<a href="/zi/{{ z }}" class="{{ 'activ' if z == d.zi else '' }}">{{ z }}</a>{% endfor %}</div>
  {% endif %}
{% endif %}

{% if tab == 'rezultate' %}
  {% if not rezumat %}
    <div class="gol">Nicio evaluare încă pentru sursa „{{ sursa }}”.
      {% if sursa != 'live' %}Replay-ul rulează local; rezultatele apar aici după ce sunt copiate în <code>jurnal_replay/</code> pe VM.{% else %}Prima evaluare vine la 30 de minute după închiderea primei zile analizate.{% endif %}</div>
  {% else %}
    <h2>Tradeable vs avoid — {{ nr_zile }} zile, {{ nr_candidati }} candidați</h2>
    <div class="tabel-box"><table>
      <thead><tr><th>Grupă</th><th class="num">n</th><th class="num">Rezultat mediu</th><th class="num">Exces mediu vs QQQ</th>
        <th class="num">t (exces)</th><th class="num">Câștigători</th><th class="num">Gap umplut</th></tr></thead>
      <tbody>{% for g, s in rezumat %}
        <tr><td>{% if g in ('tradeable', 'avoid') %}<span class="badge b-{{ g }}">{{ g }}</span>{% else %}<span class="badge b-invalid">{{ g }}</span>{% endif %}</td>
          <td class="num">{{ s.n }}</td>
          <td class="num {{ 'verde' if s.randament_mediu_pct > 0 else 'rosu' }}">{{ "%+.2f"|format(s.randament_mediu_pct) }}%</td>
          <td class="num {{ 'verde' if s.exces_mediu_pct > 0 else 'rosu' }}">{{ "%+.2f"|format(s.exces_mediu_pct) }}%</td>
          <td class="num">{{ s.t_exces if s.t_exces is not none else '–' }}</td>
          <td class="num">{{ "%.0f"|format(s.rata_castig_pct) }}%</td>
          <td class="num">{{ "%.0f"|format(s.gap_umplut_pct) }}%</td></tr>
      {% endfor %}</tbody>
    </table></div>
    <div class="nota">Sub ~30 de candidați pe grupă, diferențele sunt zgomot. |t| peste ~2 începe să conteze.
      Costurile reale (spread, slippage la deschidere) nu sunt scăzute — la small caps pot fi 0.2–0.5% dus-întors.</div>

    <h2>Exces cumulat vs QQQ</h2>
    <div class="grafic-box"><div id="grafic" style="height:360px"></div></div>
    <div class="nota">Pentru fiecare zi: media excesului candidaților din grupă (portofoliu egal ponderat în ziua aceea), adunată de la o zi la alta.
      O linie tradeable deasupra liniei avoid înseamnă că verdictele lui Claude separă bine.</div>

    <h2>Pe zile</h2>
    <div class="tabel-box"><table>
      <thead><tr><th>Zi</th><th class="num">Candidați</th><th class="num">T / A</th><th class="num">Exces toți</th>
        <th class="num">Exces tradeable</th><th class="num">Exces avoid</th></tr></thead>
      <tbody>{% for r in pe_zile %}
        <tr><td>{% if sursa == 'live' %}<a href="/zi/{{ r.zi }}" style="color:var(--text)">{{ r.zi }}</a>{% else %}{{ r.zi }}{% endif %}</td>
          <td class="num">{{ r.n }}</td><td class="num">{{ r.nt }} / {{ r.na }}</td>
          {% for v in (r.toti, r.t, r.a) %}
            <td class="num {{ '' if v is none else ('verde' if v > 0 else 'rosu') }}">{{ '–' if v is none else ("%+.2f"|format(v) ~ '%') }}</td>
          {% endfor %}</tr>
      {% endfor %}</tbody>
    </table></div>

    <script>
      const s = {{ serie|tojson }};
      const stil = getComputedStyle(document.documentElement);
      const cul = n => stil.getPropertyValue(n).trim();
      const serii = [["toti", "toți candidații", "--s-toti"], ["tradeable", "tradeable", "--s-tradeable"],
                     ["avoid", "avoid", "--s-avoid"]];
      const urme = serii.map(([k, nume, c]) => ({
        x: s.zile, y: s[k], name: nume, type: "scatter", mode: "lines",
        line: { color: cul(c), width: 2 },
        hovertemplate: nume + ": %{y:+.2f}%<extra></extra>" }));
      // Etichete directe la capatul liniilor; cand capetele sunt apropiate le departam
      // vertical (doar eticheta, nu linia), ca sa nu se suprapuna.
      const ultim = s.zile[s.zile.length - 1];
      const toate = serii.flatMap(([k]) => s[k]);
      const distanta = (Math.max(...toate, 0) - Math.min(...toate, 0)) * 0.07 || 1;
      const capete = serii.map(([k, nume]) => ({ nume, y: s[k][s[k].length - 1] }))
                          .sort((a, b) => b.y - a.y);
      for (let i = 1; i < capete.length; i++)
        capete[i].y = Math.min(capete[i].y, capete[i - 1].y - distanta);
      const etichete = capete.map(c => ({
        x: ultim, y: c.y, text: c.nume, showarrow: false, xanchor: "left", xshift: 6,
        font: { color: cul("--text"), size: 12 } }));
      Plotly.newPlot("grafic", urme, {
        paper_bgcolor: cul("--suprafata"), plot_bgcolor: cul("--suprafata"),
        font: { color: cul("--text2"), family: "system-ui, sans-serif", size: 12 },
        margin: { l: 56, r: 120, t: 16, b: 40 }, hovermode: "x unified",
        legend: { orientation: "h", y: 1.12, x: 0, font: { color: cul("--text") } },
        xaxis: { gridcolor: cul("--linie"), linecolor: cul("--chenar"), type: "category", nticks: 10 },
        yaxis: { gridcolor: cul("--linie"), zerolinecolor: cul("--chenar"), ticksuffix: "%", title: { text: "exces cumulat" } },
        annotations: etichete,
      }, { displayModeBar: false, responsive: true });
    </script>
  {% endif %}
{% endif %}
</body>
</html>
"""


ZILE_RO = ["lun", "mar", "mie", "joi", "vin", "sâm", "dum"]
LUNI_RO = ["ian", "feb", "mar", "apr", "mai", "iun", "iul", "aug", "sep", "oct", "nov", "dec"]


def format_ro(t: datetime) -> str:
    """ex. "lun 16 nov, 09:00" (strftime ar da numele in engleza)."""
    return f"{ZILE_RO[t.weekday()]} {t.day} {LUNI_RO[t.month - 1]}, {t:%H:%M}"


def _comune(tab: str, sursa: str = "live") -> dict:
    acum = datetime.now(NY)
    urm = urmatoarea_analiza(acum)
    return {"tab": tab, "sursa": sursa, "model": analyst.MODEL, "serviciu": stare_serviciu(),
            "acum_et": acum.strftime("%Y-%m-%d %H:%M"), "acum_ro": acum.astimezone(RO).strftime("%H:%M"),
            "urmatoarea_et": format_ro(urm), "urmatoarea_ro": urm.astimezone(RO).strftime("%H:%M")}


@app.route("/")
def pagina_principala():
    zile = zile_cu_scanari(DIR_LIVE)
    zi = zile[0] if zile else datetime.now(NY).date().isoformat()
    return render_template_string(PAGINA, d=date_zi(DIR_LIVE, zi), zile=zile, **_comune("zi"))


@app.route("/zi/<zi>")
def pagina_zi(zi):
    if not FORMAT_ZI.match(zi):
        abort(404)
    return render_template_string(PAGINA, d=date_zi(DIR_LIVE, zi), zile=zile_cu_scanari(DIR_LIVE),
                                  **_comune("zi"))


@app.route("/rezultate")
def pagina_rezultate():
    sursa = request.args.get("sursa", "live")
    if sursa not in SURSE:
        abort(404)
    evaluari = evalueaza_gap.toate_evaluarile(SURSE[sursa])
    rezumat = evalueaza_gap.rezumat(evaluari)
    ordine = ["tradeable", "avoid", "invalid (esec analist)", "fara verdict (doar scanner)"]
    rezumat = sorted(rezumat.items(), key=lambda x: ordine.index(x[0]) if x[0] in ordine else 99)
    cu_rezultat = [e for e in evaluari if "exces_pct" in e]
    return render_template_string(
        PAGINA, rezumat=rezumat, serie=serie_cumulata(evaluari), pe_zile=tabel_pe_zile(evaluari),
        nr_zile=len({e["zi"] for e in cu_rezultat}), nr_candidati=len(cu_rezultat),
        **_comune("rezultate", sursa))


@app.route("/api/stare")
def api_stare():
    zile = zile_cu_scanari(DIR_LIVE)
    d = date_zi(DIR_LIVE, zile[0]) if zile else None
    return jsonify({
        "serviciu": stare_serviciu(),
        "ultima_zi": zile[0] if zile else None,
        "candidati": len(d["randuri"]) if d else 0,
        "tradeable": d["nr_tradeable"] if d else 0,
        "cost_azi": d["cost"] if d else None,
        "rezumat_live": evalueaza_gap.rezumat(evalueaza_gap.toate_evaluarile(DIR_LIVE)),
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
