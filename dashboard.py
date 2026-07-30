#!/usr/bin/env python3
# dashboard.py — Dashboard Flask (port 8080), READ-ONLY.
# Citeste doar din JSON-urile de pe disc, niciodata din yfinance.
import os
import json
import glob
import csv
from datetime import datetime
from flask import Flask, render_template_string, jsonify

FOLDER = os.path.dirname(os.path.abspath(__file__))

# Conexiune Alpaca DOAR pentru soldul contului (nu yfinance — fara rate-limit)
from dotenv import load_dotenv
load_dotenv(os.path.join(FOLDER, ".env"))
try:
    import alpaca_trade_api as tradeapi
    _api = tradeapi.REST(
        os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY"),
        os.getenv("ALPACA_BASE_URL"), api_version="v2"
    )
except Exception:
    _api = None

POZITII_FILE = os.path.join(FOLDER, "pozitii_active.json")
MEMORIE_FILE = os.path.join(FOLDER, "memorie_multitf.json")
GRAFICE_FILE = os.path.join(FOLDER, "grafice_cache.json")
LOG_FILE = os.path.join(FOLDER, "agent.log")

app = Flask(__name__)


def incarca_json(cale, implicit):
    if os.path.exists(cale):
        try:
            with open(cale, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return implicit
    return implicit


def ultimele_linii_log(n=30):
    if not os.path.exists(LOG_FILE):
        return []
    try:
        with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
            linii = f.readlines()
        return [l.rstrip() for l in linii[-n:]]
    except Exception:
        return []


def calculeaza_statistici(memorie):
    tranzactii = memorie.get("tranzactii", [])
    inchideri = [t for t in tranzactii if t["tip"] == "close_long" and t.get("profit") is not None]
    total = len(inchideri)
    wins = [t for t in inchideri if t["profit"] >= 0]
    losses = [t for t in inchideri if t["profit"] < 0]
    profit_brut = sum(t["profit"] for t in wins)
    pierdere_bruta = abs(sum(t["profit"] for t in losses))
    pf = (profit_brut / pierdere_bruta) if pierdere_bruta > 0 else 0
    win_rate = (len(wins) / total * 100) if total > 0 else 0
    castig_mediu = (profit_brut / len(wins)) if wins else 0
    pierdere_medie = (pierdere_bruta / len(losses)) if losses else 0
    return {
        "total": total, "wins": len(wins), "losses": len(losses),
        "win_rate": round(win_rate, 1), "profit_factor": round(pf, 2),
        "profit_total": round(sum(t["profit"] for t in inchideri), 2),
        "castig_mediu": round(castig_mediu, 2),
        "pierdere_medie": round(pierdere_medie, 2),
    }


def stats_pe_zi(memorie):
    pe_zi = {}
    for t in memorie.get("tranzactii", []):
        if t["tip"] == "close_long" and t.get("profit") is not None:
            z = t["data"]
            if z not in pe_zi:
                pe_zi[z] = {"trades": 0, "wins": 0, "profit": 0}
            pe_zi[z]["trades"] += 1
            if t["profit"] >= 0:
                pe_zi[z]["wins"] += 1
            pe_zi[z]["profit"] += t["profit"]
    for z in pe_zi:
        pe_zi[z]["profit"] = round(pe_zi[z]["profit"], 2)
    return dict(sorted(pe_zi.items(), reverse=True))


def stats_pe_simbol(memorie):
    return memorie.get("performanta", {})


def stats_pe_motiv(memorie):
    pe_motiv = {}
    for t in memorie.get("tranzactii", []):
        if t["tip"] == "close_long" and t.get("motiv"):
            m = t["motiv"].split("(")[0].strip()
            if m not in pe_motiv:
                pe_motiv[m] = {"count": 0, "profit": 0}
            pe_motiv[m]["count"] += 1
            pe_motiv[m]["profit"] += t.get("profit", 0) or 0
    for m in pe_motiv:
        pe_motiv[m]["profit"] = round(pe_motiv[m]["profit"], 2)
    return pe_motiv


PAGINA = """
<!DOCTYPE html>
<html lang="ro">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Trading Dashboard — Multi-TF</title>
<meta http-equiv="refresh" content="120">
<script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background:#0d1117; color:#c9d1d9; font-family:-apple-system,Segoe UI,Roboto,sans-serif; padding:16px; }
  h1 { font-size:20px; margin-bottom:4px; }
  .sub { color:#8b949e; font-size:13px; margin-bottom:16px; }
  .tabs { display:flex; gap:8px; margin-bottom:16px; }
  .tab { padding:8px 16px; background:#161b22; border:1px solid #30363d; border-radius:6px;
         cursor:pointer; text-decoration:none; color:#c9d1d9; }
  .tab.activ { background:#1f6feb; border-color:#1f6feb; color:#fff; }
  .carduri { display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:12px; margin-bottom:20px; }
  .card { background:#161b22; border:1px solid #30363d; border-radius:8px; padding:14px; }
  .card .eticheta { color:#8b949e; font-size:12px; }
  .card .valoare { font-size:22px; font-weight:600; margin-top:4px; }
  .verde { color:#3fb950; } .rosu { color:#f85149; } .gri { color:#8b949e; }
  table { width:100%; border-collapse:collapse; margin-bottom:20px; font-size:13px; }
  th,td { text-align:left; padding:8px; border-bottom:1px solid #21262d; }
  th { color:#8b949e; font-weight:500; }
  .badge { padding:2px 8px; border-radius:10px; font-size:11px; }
  h2 { font-size:16px; margin:20px 0 12px; }
  .log { background:#010409; border:1px solid #30363d; border-radius:8px; padding:12px;
         font-family:monospace; font-size:12px; line-height:1.6; max-height:320px; overflow-y:auto; }
  .grafice { display:grid; grid-template-columns:repeat(auto-fit,minmax(400px,1fr)); gap:12px; }
  .grafic-box { background:#161b22; border:1px solid #30363d; border-radius:8px; padding:8px; }
  .banda { padding:12px; border-radius:8px; margin-bottom:16px; font-weight:600; text-align:center; }
</style>
</head>
<body>
  <h1>🤖 Trading Dashboard — Multi-Timeframe</h1>
  <div class="sub">{{ status_bursa }} · Actualizat: {{ acum }} · auto-refresh 120s</div>

  <div class="tabs">
    <a href="/" class="tab {{ 'activ' if tab=='dashboard' else '' }}">Dashboard</a>
    <a href="/statistici" class="tab {{ 'activ' if tab=='statistici' else '' }}">Statistici</a>
  </div>

  {% if tab == 'dashboard' %}
  <div class="carduri">
    <div class="card"><div class="eticheta">Cash</div><div class="valoare">${{ "%.0f"|format(cash) }}</div></div>
    <div class="card"><div class="eticheta">Valoare Portofoliu</div><div class="valoare">${{ "%.0f"|format(valoare) }}</div></div>
    <div class="card"><div class="eticheta">Profit Azi</div><div class="valoare {{ 'verde' if profit_azi>=0 else 'rosu' }}">${{ "%.2f"|format(profit_azi) }}</div></div>
    <div class="card"><div class="eticheta">P&L Total</div><div class="valoare {{ 'verde' if stats.profit_total>=0 else 'rosu' }}">${{ "%.2f"|format(stats.profit_total) }}</div></div>
    <div class="card"><div class="eticheta">Win Rate</div><div class="valoare">{{ stats.win_rate }}%</div></div>
    <div class="card"><div class="eticheta">Pozitii</div><div class="valoare">{{ pozitii|length }}/5</div></div>
  </div>

  <h2>💱 Tranzactii Azi ({{ tranzactii_azi|length }})</h2>
  {% if tranzactii_azi %}
  <table>
    <thead><tr><th>Ora</th><th>Simbol</th><th>Tip</th><th>Pret</th><th>Cant.</th><th>Profit</th><th>Motiv</th></tr></thead>
    <tbody>
      {% for t in tranzactii_azi %}
      <tr>
        <td class="gri">{{ t.ora }}</td>
        <td><strong>{{ t.simbol }}</strong></td>
        <td>{% if t.tip=='open_long' %}<span class="badge" style="background:#1a3a1f;color:#3fb950;">BUY</span>{% else %}<span class="badge" style="background:#3a1a1a;color:#f85149;">SELL</span>{% endif %}</td>
        <td>${{ "%.2f"|format(t.pret) }}</td>
        <td>{{ t.cantitate }}</td>
        <td>{% if t.profit is not none %}<span class="{{ 'verde' if t.profit>=0 else 'rosu' }}">${{ "%.2f"|format(t.profit) }}</span>{% else %}<span class="gri">—</span>{% endif %}</td>
        <td class="gri">{{ t.motiv or '' }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
  {% else %}<p class="gri">Nicio tranzactie azi.</p>{% endif %}

  <h2>📋 Log Agent (ultimele 30)</h2>
  <div class="log">
    {% for l in log_linii %}<div>{{ l }}</div>{% endfor %}
  </div>

  <h2>📈 Pozitii Deschise</h2>
  {% if pozitii %}
  <div class="grafice">
    {% for sim, poz in pozitii.items() %}
    <div class="grafic-box"><div id="chart_{{ sim }}" style="height:300px;"></div></div>
    {% endfor %}
  </div>
  {% else %}<p class="gri">Nicio pozitie deschisa.</p>{% endif %}

  <h2>👁️ Watchlist</h2>
  <div class="grafice">
    {% for sim, g in grafice.items() %}
    <div class="grafic-box">
      <div style="display:flex;justify-content:space-between;padding:4px 8px;">
        <strong>{{ sim }}</strong>
        <span>{{ g.status_1d }} · <span class="{{ 'verde' if g.var_zi>=0 else 'rosu' }}">{{ g.var_zi }}%</span></span>
      </div>
      <div id="watch_{{ sim }}" style="height:220px;"></div>
    </div>
    {% endfor %}
  </div>

  <script>
    var grafice = {{ grafice_json|safe }};
    var pozitii = {{ pozitii_json|safe }};
    var layout_base = {
      paper_bgcolor:'#161b22', plot_bgcolor:'#161b22', font:{color:'#c9d1d9',size:10},
      margin:{l:40,r:10,t:10,b:30}, showlegend:false,
      xaxis:{gridcolor:'#21262d'}, yaxis:{gridcolor:'#21262d'}
    };
    for (var sim in grafice) {
      var d = grafice[sim];
      var candle = { x:d.dates, open:d.open, high:d.high, low:d.low, close:d.close,
                     type:'candlestick', name:sim };
      var ema9 = { x:d.dates, y:d.ema9, type:'scatter', mode:'lines', line:{color:'#f0883e',width:1}, name:'EMA9' };
      var ema21 = { x:d.dates, y:d.ema21, type:'scatter', mode:'lines', line:{color:'#58a6ff',width:1}, name:'EMA21' };
      if (document.getElementById('watch_'+sim)) {
        Plotly.newPlot('watch_'+sim, [candle,ema9,ema21], layout_base, {responsive:true,displayModeBar:false});
      }
      if (document.getElementById('chart_'+sim)) {
        var extra = [candle,ema9,ema21];
        if (pozitii[sim]) {
          var pi = pozitii[sim].pret_intrare;
          extra.push({ x:d.dates, y:d.dates.map(function(){return pi*1.04;}), type:'scatter', mode:'lines', line:{color:'#3fb950',width:1,dash:'dot'}, name:'TP' });
          extra.push({ x:d.dates, y:d.dates.map(function(){return pi*0.985;}), type:'scatter', mode:'lines', line:{color:'#f85149',width:1,dash:'dot'}, name:'SL' });
        }
        Plotly.newPlot('chart_'+sim, extra, layout_base, {responsive:true,displayModeBar:false});
      }
    }
  </script>

  {% else %}
  <!-- TAB STATISTICI -->
  {% set pf = stats.profit_factor %}
  <div class="banda" style="background:{{ '#1a3a1f' if pf>=1.5 else ('#3a3a1a' if pf>=1.0 else '#3a1a1a') }};
       color:{{ '#3fb950' if pf>=1.5 else ('#d29922' if pf>=1.0 else '#f85149') }};">
    {% if pf>=1.5 %}✅ Profit Factor {{ pf }} — strategie profitabila
    {% elif pf>=1.0 %}⚠️ Profit Factor {{ pf }} — marginal
    {% else %}🔴 Profit Factor {{ pf }} — sub break-even{% endif %}
  </div>

  <div class="carduri">
    <div class="card"><div class="eticheta">Total Trades</div><div class="valoare">{{ stats.total }}</div></div>
    <div class="card"><div class="eticheta">Win Rate</div><div class="valoare">{{ stats.win_rate }}%</div></div>
    <div class="card"><div class="eticheta">Profit Factor</div><div class="valoare">{{ stats.profit_factor }}</div></div>
    <div class="card"><div class="eticheta">Profit Total</div><div class="valoare {{ 'verde' if stats.profit_total>=0 else 'rosu' }}">${{ "%.2f"|format(stats.profit_total) }}</div></div>
    <div class="card"><div class="eticheta">Castig Mediu</div><div class="valoare verde">${{ "%.2f"|format(stats.castig_mediu) }}</div></div>
    <div class="card"><div class="eticheta">Pierdere Medie</div><div class="valoare rosu">${{ "%.2f"|format(stats.pierdere_medie) }}</div></div>
  </div>

  <h2>📅 Performanta pe Zi</h2>
  <table>
    <thead><tr><th>Data</th><th>Trades</th><th>Win Rate</th><th>Profit</th></tr></thead>
    <tbody>
      {% for zi, s in pe_zi.items() %}
      <tr>
        <td>{{ zi }}</td><td>{{ s.trades }}</td>
        <td>{{ "%.0f"|format(s.wins/s.trades*100 if s.trades else 0) }}%</td>
        <td class="{{ 'verde' if s.profit>=0 else 'rosu' }}">${{ "%.2f"|format(s.profit) }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>

  <h2>🎯 Performanta pe Simbol</h2>
  <table>
    <thead><tr><th>Simbol</th><th>Trades</th><th>Wins</th><th>Profit</th></tr></thead>
    <tbody>
      {% for sim, s in pe_simbol.items() %}
      <tr><td><strong>{{ sim }}</strong></td><td>{{ s.trades }}</td><td>{{ s.wins }}</td>
        <td class="{{ 'verde' if s.profit>=0 else 'rosu' }}">${{ "%.2f"|format(s.profit) }}</td></tr>
      {% endfor %}
    </tbody>
  </table>

  <h2>🚪 Performanta pe Motiv de Iesire</h2>
  <table>
    <thead><tr><th>Motiv</th><th>Count</th><th>Profit</th></tr></thead>
    <tbody>
      {% for m, s in pe_motiv.items() %}
      <tr><td>{{ m }}</td><td>{{ s.count }}</td>
        <td class="{{ 'verde' if s.profit>=0 else 'rosu' }}">${{ "%.2f"|format(s.profit) }}</td></tr>
      {% endfor %}
    </tbody>
  </table>
  {% endif %}
</body>
</html>
"""


def date_comune():
    memorie = incarca_json(MEMORIE_FILE, {"tranzactii": [], "performanta": {}, "stats": {}})
    pozitii = incarca_json(POZITII_FILE, {})
    stats = calculeaza_statistici(memorie)
    return memorie, pozitii, stats


def get_cash_valoare():
    """Citeste soldul REAL din Alpaca (nu yfinance — fara rate-limit)."""
    if _api is not None:
        try:
            acc = _api.get_account()
            return float(acc.portfolio_value), float(acc.cash)
        except Exception:
            pass
    # Fallback daca Alpaca nu raspunde
    memorie = incarca_json(MEMORIE_FILE, {"stats": {}})
    profit_total = memorie.get("stats", {}).get("total_profit", 0)
    return 100000 + profit_total, 100000 + profit_total


@app.route("/")
def dashboard():
    memorie, pozitii, stats = date_comune()
    grafice = incarca_json(GRAFICE_FILE, {})
    azi = datetime.now().strftime("%Y-%m-%d")
    tranzactii_azi = [t for t in memorie.get("tranzactii", []) if t["data"] == azi]
    profit_azi = sum(t["profit"] for t in tranzactii_azi
                     if t["tip"] == "close_long" and t.get("profit") is not None)
    valoare, cash = get_cash_valoare()
    return render_template_string(
        PAGINA, tab="dashboard", acum=datetime.now().strftime("%H:%M:%S"),
        status_bursa="🟢 Bursa deschisa" if _bursa_pare_deschisa(memorie) else "🔴 Bursa inchisa",
        cash=cash, valoare=valoare, profit_azi=profit_azi, stats=stats,
        pozitii=pozitii, tranzactii_azi=list(reversed(tranzactii_azi)),
        log_linii=ultimele_linii_log(30), grafice=grafice,
        grafice_json=json.dumps(grafice), pozitii_json=json.dumps(pozitii),
    )


@app.route("/statistici")
def statistici():
    memorie, pozitii, stats = date_comune()
    return render_template_string(
        PAGINA, tab="statistici", acum=datetime.now().strftime("%H:%M:%S"),
        status_bursa="", stats=stats,
        pe_zi=stats_pe_zi(memorie), pe_simbol=stats_pe_simbol(memorie),
        pe_motiv=stats_pe_motiv(memorie),
        pozitii={}, grafice={}, grafice_json="{}", pozitii_json="{}",
        cash=0, valoare=0, profit_azi=0, tranzactii_azi=[], log_linii=[],
    )


@app.route("/api/stats")
def api_stats():
    memorie, _, stats = date_comune()
    return jsonify({
        "general": stats, "pe_zi": stats_pe_zi(memorie),
        "pe_simbol": stats_pe_simbol(memorie), "pe_motiv": stats_pe_motiv(memorie),
    })


def _bursa_pare_deschisa(memorie):
    tranzactii = memorie.get("tranzactii", [])
    if not tranzactii:
        return False
    ultima = tranzactii[-1]
    return ultima.get("data") == datetime.now().strftime("%Y-%m-%d")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
