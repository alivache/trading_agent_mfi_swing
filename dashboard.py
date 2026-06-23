from flask import Flask, render_template_string, jsonify
import os
import json
import csv
import glob
from dotenv import load_dotenv
import alpaca_trade_api as tradeapi
from datetime import datetime, date

load_dotenv()

API_KEY = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
BASE_URL = os.getenv("ALPACA_BASE_URL")

ACTIUNI_RAW = os.getenv("ACTIUNI", "AAPL,MSFT,NVDA,GOOGL,AMZN,NFLX")
ACTIUNI = [s.strip().upper() for s in ACTIUNI_RAW.split(",")]

api = tradeapi.REST(API_KEY, SECRET_KEY, BASE_URL)
app = Flask(__name__)

FOLDER = "/home/liviu_anton/trading"
MEMORIE_FILE = os.path.join(FOLDER, "memorie_multitf.json")
LOG_FILE = os.path.join(FOLDER, "agent.log")
GRAFICE_CACHE_FILE = os.path.join(FOLDER, "grafice_cache.json")

STOP_LOSS_PCT = 0.008
TAKE_PROFIT_PCT = 0.024

GOL = {"dates": [], "open": [], "high": [], "low": [], "close": [],
       "ema9": [], "ema21": [], "rsi": []}


# ═══════════════════════════════════════
# CITESTE CACHE GRAFICE (scris de agent)
# ═══════════════════════════════════════
def incarca_grafice_cache():
    if os.path.exists(GRAFICE_CACHE_FILE):
        try:
            with open(GRAFICE_CACHE_FILE, "r") as f:
                return json.load(f)
        except:
            return {"actiuni": {}, "_updated": None}
    return {"actiuni": {}, "_updated": None}


def date_grafic(simbol, cache):
    """Returneaza (chart_data, status_1d, change) din cache-ul agentului"""
    a = cache.get("actiuni", {}).get(simbol)
    if not a:
        return GOL, "fara date", 0
    chart = {
        "dates": a.get("dates", []), "open": a.get("open", []),
        "high": a.get("high", []), "low": a.get("low", []),
        "close": a.get("close", []), "ema9": a.get("ema9", []),
        "ema21": a.get("ema21", []), "rsi": a.get("rsi", [])
    }
    return chart, a.get("status_1d", "?"), a.get("change", 0)


# ═══════════════════════════════════════
# MEMORIE & STATISTICI
# ═══════════════════════════════════════
def incarca_memorie():
    if os.path.exists(MEMORIE_FILE):
        with open(MEMORIE_FILE, "r") as f:
            return json.load(f)
    return {"tranzactii": [], "performanta": {}, "stats": {"total_profit": 0, "wins": 0, "losses": 0}}


def tranzactii_azi(memorie):
    azi = date.today().isoformat()
    toate = [t for t in memorie.get("tranzactii", []) if t.get("data", "").startswith(azi)]
    toate.sort(key=lambda x: x.get("data", ""), reverse=True)
    profit_azi = sum(t["profit"] for t in toate if t.get("profit") is not None)
    inchise_azi = len([t for t in toate if t["tip"] == "close_long"])
    return toate, profit_azi, inchise_azi


def _f(val):
    try:
        return float(val)
    except:
        return 0.0


def calculeaza_statistici():
    pattern = os.path.join(FOLDER, "multitf_trades_*.csv")
    fisiere = sorted(glob.glob(pattern))
    trades = []
    for f in fisiere:
        zi = os.path.basename(f).replace("multitf_trades_", "").replace(".csv", "")
        try:
            with open(f, "r", encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    row["_zi"] = zi
                    trades.append(row)
        except:
            pass
    if not trades:
        return None
    total = len(trades)
    wins = [t for t in trades if t.get("rezultat") == "WIN"]
    losses = [t for t in trades if t.get("rezultat") == "LOSS"]
    profit_total = sum(_f(t["profit_usd"]) for t in trades)
    castig_brut = sum(_f(t["profit_usd"]) for t in wins)
    pierdere_bruta = abs(sum(_f(t["profit_usd"]) for t in losses))
    win_rate = len(wins) / total * 100 if total else 0
    profit_factor = (castig_brut / pierdere_bruta) if pierdere_bruta > 0 else 999
    castig_mediu = castig_brut / len(wins) if wins else 0
    pierdere_medie = pierdere_bruta / len(losses) if losses else 0
    best = max(trades, key=lambda t: _f(t["profit_usd"]))
    worst = min(trades, key=lambda t: _f(t["profit_usd"]))
    per_zi, per_simbol, per_motiv = {}, {}, {}
    for t in trades:
        z = t["_zi"]
        per_zi.setdefault(z, {"trades": 0, "wins": 0, "profit": 0.0})
        per_zi[z]["trades"] += 1; per_zi[z]["profit"] += _f(t["profit_usd"])
        if t.get("rezultat") == "WIN": per_zi[z]["wins"] += 1
        s = t["simbol"]
        per_simbol.setdefault(s, {"trades": 0, "wins": 0, "profit": 0.0})
        per_simbol[s]["trades"] += 1; per_simbol[s]["profit"] += _f(t["profit_usd"])
        if t.get("rezultat") == "WIN": per_simbol[s]["wins"] += 1
        m = t.get("motiv_exit", "?").split("(")[0].strip()
        per_motiv.setdefault(m, {"count": 0, "profit": 0.0})
        per_motiv[m]["count"] += 1; per_motiv[m]["profit"] += _f(t["profit_usd"])
    if profit_factor >= 2.0:
        verdict = ("✅ Profit factor EXCELENT", "green")
    elif profit_factor >= 1.5:
        verdict = ("✅ Profit factor BUN (>1.5 = sustenabil)", "green")
    elif profit_factor >= 1.0:
        verdict = ("🟡 Profit factor SLAB (profitabil dar fragil)", "yellow")
    else:
        verdict = ("🔴 Profit factor SUB 1.0 (pierde bani)", "red")
    return {
        "zile": len(per_zi), "total": total, "wins": len(wins), "losses": len(losses),
        "win_rate": win_rate, "profit_total": profit_total,
        "profit_factor": profit_factor, "castig_mediu": castig_mediu,
        "pierdere_medie": pierdere_medie,
        "rr": (castig_mediu / pierdere_medie) if pierdere_medie > 0 else 0,
        "best": {"simbol": best["simbol"], "profit": _f(best["profit_usd"]), "zi": best["_zi"]},
        "worst": {"simbol": worst["simbol"], "profit": _f(worst["profit_usd"]), "zi": worst["_zi"]},
        "verdict": verdict,
        "per_zi": dict(sorted(per_zi.items())),
        "per_simbol": dict(sorted(per_simbol.items(), key=lambda x: x[1]["profit"], reverse=True)),
        "per_motiv": dict(sorted(per_motiv.items(), key=lambda x: x[1]["profit"], reverse=True)),
    }


def citeste_log():
    try:
        if not os.path.exists(LOG_FILE):
            return ["Log indisponibil..."]
        with open(LOG_FILE, "r") as f:
            lines = f.readlines()[-30:]
            lines.reverse()
            return [l.rstrip() for l in lines if l.strip()]
    except Exception as e:
        return [f"Eroare: {e}"]


# ═══════════════════════════════════════
# HTML
# ═══════════════════════════════════════
HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Trading Agent Dashboard</title>
    <meta http-equiv="refresh" content="120">
    <script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: #0f1419; color: #e6edf3; padding: 20px; min-height: 100vh; }
        .container { max-width: 1400px; margin: 0 auto; }
        h1 { color: #58a6ff; margin-bottom: 15px; font-size: 28px; }
        h2 { color: #58a6ff; margin: 25px 0 15px; font-size: 20px; }
        .tabs { display: flex; gap: 8px; margin-bottom: 25px; border-bottom: 1px solid #30363d; }
        .tab { padding: 12px 24px; cursor: pointer; color: #8b949e; font-size: 15px;
            font-weight: 500; border-bottom: 2px solid transparent; user-select: none; }
        .tab:hover { color: #e6edf3; }
        .tab.active { color: #58a6ff; border-bottom-color: #58a6ff; }
        .tab-content { display: none; }
        .tab-content.active { display: block; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 15px; margin-bottom: 25px; }
        .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 20px; }
        .card h3 { color: #8b949e; font-size: 12px; text-transform: uppercase;
            margin-bottom: 10px; letter-spacing: 1px; }
        .card .value { font-size: 24px; font-weight: bold; }
        .green { color: #3fb950; } .red { color: #f85149; } .yellow { color: #d29922; }
        table { width: 100%; border-collapse: collapse; background: #161b22;
            border-radius: 8px; overflow: hidden; margin-bottom: 25px; }
        th, td { padding: 12px 15px; text-align: left; border-bottom: 1px solid #30363d; }
        th { background: #21262d; color: #8b949e; font-size: 12px;
            text-transform: uppercase; letter-spacing: 1px; }
        tr:last-child td { border-bottom: none; }
        .badge { display: inline-block; padding: 3px 8px; border-radius: 4px;
            font-size: 11px; font-weight: bold; }
        .badge-win { background: #1a3a1f; color: #3fb950; }
        .badge-loss { background: #3a1a1a; color: #f85149; }
        .badge-open { background: #1a2a3a; color: #58a6ff; }
        .timestamp { text-align: right; color: #8b949e; font-size: 12px; margin-top: 20px; }
        .status-dot { display: inline-block; width: 10px; height: 10px;
            border-radius: 50%; margin-right: 8px; }
        .status-online { background: #3fb950; } .status-offline { background: #f85149; }
        .chart-container { background: #161b22; border: 1px solid #30363d;
            border-radius: 8px; padding: 15px; margin-bottom: 20px; }
        .chart-title { color: #e6edf3; font-size: 18px; font-weight: bold; margin-bottom: 10px; }
        .chart-info { color: #8b949e; font-size: 13px; margin-bottom: 10px; }
        .watchlist-grid { display: grid; grid-template-columns: repeat(2, 1fr);
            gap: 15px; margin-bottom: 25px; }
        .watch-card { background: #161b22; border: 1px solid #30363d;
            border-radius: 8px; padding: 12px; }
        .watch-header { display: flex; justify-content: space-between;
            align-items: center; margin-bottom: 8px; }
        .watch-symbol { font-size: 16px; font-weight: bold; }
        .watch-status { font-size: 12px; padding: 2px 8px; border-radius: 4px; background: #21262d; }
        @media (max-width: 800px) { .watchlist-grid { grid-template-columns: 1fr; } }
        .log-container { background: #0d1117; border: 1px solid #30363d; border-radius: 8px;
            padding: 15px; max-height: 400px; overflow-y: auto;
            font-family: 'Courier New', monospace; font-size: 13px; margin-bottom: 25px; }
        .log-line { color: #c9d1d9; padding: 3px 0; border-bottom: 1px solid #21262d;
            white-space: pre-wrap; word-break: break-word; }
        .log-line:last-child { border-bottom: none; }
        .log-line.green-line { color: #3fb950; } .log-line.red-line { color: #f85149; }
        .log-line.yellow-line { color: #d29922; } .log-line.blue-line { color: #58a6ff; }
        .verdict-box { padding: 15px 20px; border-radius: 8px; margin-bottom: 25px;
            font-size: 16px; font-weight: 500; border: 1px solid #30363d; background: #161b22; }
        .cache-info { color: #8b949e; font-size: 12px; margin-bottom: 15px; }
    </style>
</head>
<body>
    <div class="container">
        <h1>🤖 Trading Agent Dashboard</h1>

        <div class="tabs">
            <div class="tab active" onclick="switchTab('dashboard', this)">📊 Dashboard</div>
            <div class="tab" onclick="switchTab('statistici', this)">📈 Statistici</div>
        </div>

        <!-- TAB DASHBOARD -->
        <div id="tab-dashboard" class="tab-content active">
            <div class="grid">
                <div class="card"><h3>Sold Cash</h3>
                    <div class="value">${{ "%.2f"|format(account.cash) }}</div></div>
                <div class="card"><h3>Portofoliu</h3>
                    <div class="value">${{ "%.2f"|format(account.portfolio_value) }}</div></div>
                <div class="card"><h3>Profit AZI</h3>
                    <div class="value {{ 'green' if profit_azi >= 0 else 'red' }}">
                        ${{ "%.2f"|format(profit_azi) }}</div>
                    <div style="color:#8b949e;font-size:12px;margin-top:4px;">{{ inchise_azi }} trades inchise</div></div>
                <div class="card"><h3>P&L Total</h3>
                    <div class="value {{ 'green' if stats.total_profit >= 0 else 'red' }}">
                        ${{ "%.2f"|format(stats.total_profit) }}</div></div>
                <div class="card"><h3>Win Rate</h3>
                    <div class="value {{ 'green' if win_rate >= 50 else 'yellow' if win_rate >= 30 else 'red' }}">
                        {{ "%.1f"|format(win_rate) }}%</div></div>
                <div class="card"><h3>Bursa</h3>
                    <div class="value">
                        <span class="status-dot {{ 'status-online' if bursa_deschisa else 'status-offline' }}"></span>
                        {{ 'DESCHISA' if bursa_deschisa else 'INCHISA' }}</div></div>
            </div>

            <div class="cache-info">📊 Grafice actualizate de agent: {{ cache_updated }}</div>

            <h2>💼 Tranzactii AZI ({{ tranz_azi|length }})</h2>
            {% if tranz_azi|length == 0 %}
            <div class="card" style="text-align:center;color:#8b949e;">Nicio tranzactie azi inca</div>
            {% else %}
            <table>
                <thead><tr><th>Ora</th><th>Simbol</th><th>Actiune</th><th>Pret</th><th>Cant.</th><th>Profit</th><th>Motiv</th></tr></thead>
                <tbody>
                    {% for t in tranz_azi %}
                    <tr>
                        <td>{{ t.data[11:19] }}</td>
                        <td><strong>{{ t.simbol }}</strong></td>
                        <td>{% if t.tip == 'open_long' %}
                                <span class="badge badge-open">CUMPARARE</span>
                            {% else %}
                                <span class="badge {{ 'badge-win' if t.profit and t.profit > 0 else 'badge-loss' }}">VANZARE</span>
                            {% endif %}</td>
                        <td>${{ "%.2f"|format(t.pret) }}</td>
                        <td>{{ t.cantitate }}</td>
                        <td>{% if t.profit is not none %}
                                <span class="{{ 'green' if t.profit > 0 else 'red' }}">${{ "%.2f"|format(t.profit) }}</span>
                            {% else %} - {% endif %}</td>
                        <td style="font-size:12px;color:#8b949e;">{{ t.motiv or '-' }}</td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
            {% endif %}

            <h2>📡 Activitate Agent (Live Log)</h2>
            <div class="log-container">
                {% for line in log_lines %}
                <div class="log-line 
                    {% if '✅' in line or '🟢' in line or 'TAKE PROFIT' in line %}green-line
                    {% elif '❌' in line or '🔴' in line or 'STOP LOSS' in line or 'Eroare' in line %}red-line
                    {% elif '⏳' in line or '🟡' in line or '🟠' in line or '📅' in line %}yellow-line
                    {% elif '⭐' in line or '🎯' in line or '🔔' in line %}blue-line
                    {% endif %}">{{ line }}</div>
                {% endfor %}
            </div>

            <h2>📂 Pozitii Deschise ({{ pozitii|length }})</h2>
            {% if pozitii|length == 0 %}
            <div class="card" style="text-align: center; color: #8b949e;">Nicio pozitie deschisa</div>
            {% endif %}
            {% for p in pozitii %}
            <div class="chart-container">
                <div class="chart-title">{{ p.symbol }} - 
                    <span class="{{ 'green' if p.unrealized_pl >= 0 else 'red' }}">
                        {{ "%+.2f"|format(p.unrealized_plpc * 100) }}% ({{ "%+.2f"|format(p.unrealized_pl) }}$)
                    </span></div>
                <div class="chart-info">Cantitate: {{ p.qty }} | Intrare: ${{ "%.2f"|format(p.avg_entry_price) }} | 
                    Curent: ${{ "%.2f"|format(p.current_price) }} | 
                    <span class="red">SL: ${{ "%.2f"|format(p.sl_price) }}</span> | 
                    <span class="green">TP: ${{ "%.2f"|format(p.tp_price) }}</span></div>
                <div id="chart_{{ p.symbol }}" style="height: 480px;"></div>
                <script>
                    (function() {
                        var d = {{ p.chart_data|safe }};
                        if (!d.dates.length) { document.getElementById('chart_{{ p.symbol }}').innerHTML =
                            '<p style="color:#8b949e;text-align:center;padding:40px;">Date indisponibile (agentul actualizeaza in cateva minute)</p>'; return; }
                        var entry = {{ p.avg_entry_price }}, sl = {{ p.sl_price }}, tp = {{ p.tp_price }};
                        var n = d.dates.length;
                        var candle = { x: d.dates, open: d.open, high: d.high, low: d.low, close: d.close,
                            type: 'candlestick', xaxis: 'x', yaxis: 'y',
                            increasing: {line: {color: '#3fb950'}, fillcolor: '#3fb950'},
                            decreasing: {line: {color: '#f85149'}, fillcolor: '#f85149'}, name: 'Pret' };
                        var ema9 = { x: d.dates, y: d.ema9, type: 'scatter', mode: 'lines',
                            line: {color: '#d29922', width: 1.5}, name: 'EMA9', xaxis: 'x', yaxis: 'y' };
                        var ema21 = { x: d.dates, y: d.ema21, type: 'scatter', mode: 'lines',
                            line: {color: '#a371f7', width: 1.5}, name: 'EMA21', xaxis: 'x', yaxis: 'y' };
                        var le = { x: d.dates, y: Array(n).fill(entry), type: 'scatter', mode: 'lines',
                            line: {color: '#58a6ff', width: 2, dash: 'dash'}, name: 'Intrare', xaxis: 'x', yaxis: 'y' };
                        var lsl = { x: d.dates, y: Array(n).fill(sl), type: 'scatter', mode: 'lines',
                            line: {color: '#f85149', width: 1.5, dash: 'dot'}, name: 'SL', xaxis: 'x', yaxis: 'y' };
                        var ltp = { x: d.dates, y: Array(n).fill(tp), type: 'scatter', mode: 'lines',
                            line: {color: '#3fb950', width: 1.5, dash: 'dot'}, name: 'TP', xaxis: 'x', yaxis: 'y' };
                        var rsi = { x: d.dates, y: d.rsi, type: 'scatter', mode: 'lines',
                            line: {color: '#58a6ff', width: 1.5}, name: 'RSI', xaxis: 'x2', yaxis: 'y2' };
                        var layout = { paper_bgcolor: '#161b22', plot_bgcolor: '#161b22', font: {color: '#e6edf3'},
                            xaxis: {gridcolor: '#30363d', rangeslider: {visible: false}, domain: [0,1], anchor: 'y'},
                            yaxis: {gridcolor: '#30363d', domain: [0.32, 1]},
                            xaxis2: {gridcolor: '#30363d', rangeslider: {visible: false}, domain: [0,1], anchor: 'y2'},
                            yaxis2: {gridcolor: '#30363d', domain: [0, 0.24], range: [0,100], tickvals: [30,50,70]},
                            margin: {t: 10, b: 40, l: 55, r: 20}, legend: {orientation: 'h', y: -0.12},
                            shapes: [{type:'line', xref:'paper', x0:0, x1:1, yref:'y2', y0:70, y1:70,
                                    line:{color:'#f85149', width:1, dash:'dot'}},
                                {type:'line', xref:'paper', x0:0, x1:1, yref:'y2', y0:30, y1:30,
                                    line:{color:'#3fb950', width:1, dash:'dot'}}] };
                        Plotly.newPlot('chart_{{ p.symbol }}', [candle, ema9, ema21, le, lsl, ltp, rsi], layout, {responsive: true});
                    })();
                </script>
            </div>
            {% endfor %}

            <h2>📈 Watchlist — Toate Actiunile ({{ watchlist|length }})</h2>
            <div class="watchlist-grid">
                {% for w in watchlist %}
                <div class="watch-card">
                    <div class="watch-header">
                        <span class="watch-symbol">{{ w.symbol }}
                            <span class="{{ 'green' if w.change >= 0 else 'red' }}" style="font-size:13px;">
                                {{ "%+.2f"|format(w.change) }}%</span></span>
                        <span class="watch-status">{{ w.status }}</span>
                    </div>
                    <div id="watch_{{ w.symbol }}" style="height: 300px;"></div>
                    <script>
                        (function() {
                            var d = {{ w.chart_data|safe }};
                            if (!d.dates.length) { document.getElementById('watch_{{ w.symbol }}').innerHTML =
                                '<p style="color:#8b949e;text-align:center;padding:30px;font-size:11px;">Date indisponibile</p>'; return; }
                            var candle = { x: d.dates, open: d.open, high: d.high, low: d.low, close: d.close,
                                type: 'candlestick', xaxis: 'x', yaxis: 'y',
                                increasing: {line: {color: '#3fb950'}, fillcolor: '#3fb950'},
                                decreasing: {line: {color: '#f85149'}, fillcolor: '#f85149'} };
                            var ema9 = { x: d.dates, y: d.ema9, type: 'scatter', mode: 'lines',
                                line: {color: '#d29922', width: 1.2}, xaxis: 'x', yaxis: 'y' };
                            var ema21 = { x: d.dates, y: d.ema21, type: 'scatter', mode: 'lines',
                                line: {color: '#a371f7', width: 1.2}, xaxis: 'x', yaxis: 'y' };
                            var rsi = { x: d.dates, y: d.rsi, type: 'scatter', mode: 'lines',
                                line: {color: '#58a6ff', width: 1.2}, xaxis: 'x2', yaxis: 'y2' };
                            var layout = { paper_bgcolor: '#161b22', plot_bgcolor: '#161b22',
                                font: {color: '#e6edf3', size: 9}, showlegend: false,
                                xaxis: {gridcolor: '#30363d', rangeslider: {visible: false}, domain: [0,1], anchor: 'y'},
                                yaxis: {gridcolor: '#30363d', domain: [0.34, 1]},
                                xaxis2: {gridcolor: '#30363d', rangeslider: {visible: false}, domain: [0,1], anchor: 'y2'},
                                yaxis2: {gridcolor: '#30363d', domain: [0, 0.26], range: [0,100], tickvals: [30,70]},
                                margin: {t: 5, b: 25, l: 38, r: 8},
                                shapes: [{type:'line', xref:'paper', x0:0, x1:1, yref:'y2', y0:70, y1:70,
                                        line:{color:'#f85149', width:0.8, dash:'dot'}},
                                    {type:'line', xref:'paper', x0:0, x1:1, yref:'y2', y0:30, y1:30,
                                        line:{color:'#3fb950', width:0.8, dash:'dot'}}] };
                            Plotly.newPlot('watch_{{ w.symbol }}', [candle, ema9, ema21, rsi], layout, {responsive: true, displayModeBar: false});
                        })();
                    </script>
                </div>
                {% endfor %}
            </div>
        </div>

        <!-- TAB STATISTICI -->
        <div id="tab-statistici" class="tab-content">
            {% if st %}
            <div class="verdict-box {{ st.verdict[1] }}">{{ st.verdict[0] }}</div>
            <div class="grid">
                <div class="card"><h3>Zile tranzactionate</h3><div class="value">{{ st.zile }}</div></div>
                <div class="card"><h3>Total Trades</h3><div class="value">{{ st.total }}</div></div>
                <div class="card"><h3>Win Rate</h3>
                    <div class="value {{ 'green' if st.win_rate >= 50 else 'yellow' }}">
                        {{ "%.1f"|format(st.win_rate) }}%</div></div>
                <div class="card"><h3>Profit Total</h3>
                    <div class="value {{ 'green' if st.profit_total >= 0 else 'red' }}">
                        ${{ "%.2f"|format(st.profit_total) }}</div></div>
                <div class="card"><h3>Profit Factor</h3>
                    <div class="value {{ 'green' if st.profit_factor >= 1.5 else 'yellow' if st.profit_factor >= 1 else 'red' }}">
                        {{ "%.2f"|format(st.profit_factor) if st.profit_factor < 999 else "∞" }}</div></div>
                <div class="card"><h3>Castig Mediu</h3>
                    <div class="value green">${{ "%.2f"|format(st.castig_mediu) }}</div></div>
                <div class="card"><h3>Pierdere Medie</h3>
                    <div class="value red">${{ "%.2f"|format(st.pierdere_medie) }}</div></div>
                <div class="card"><h3>Raport C/P</h3>
                    <div class="value">{{ "%.2f"|format(st.rr) if st.rr > 0 else "—" }}</div></div>
            </div>
            <div class="grid">
                <div class="card"><h3>🏆 Cel mai bun trade</h3>
                    <div class="value green">{{ st.best.simbol }} +${{ "%.2f"|format(st.best.profit) }}</div>
                    <div style="color:#8b949e;font-size:13px;margin-top:5px;">{{ st.best.zi }}</div></div>
                <div class="card"><h3>🔻 Cel mai prost trade</h3>
                    <div class="value {{ 'red' if st.worst.profit < 0 else 'green' }}">
                        {{ st.worst.simbol }} ${{ "%.2f"|format(st.worst.profit) }}</div>
                    <div style="color:#8b949e;font-size:13px;margin-top:5px;">{{ st.worst.zi }}</div></div>
            </div>
            <h2>📅 Performanta pe Zi</h2>
            <table>
                <thead><tr><th>Zi</th><th>Trades</th><th>Win Rate</th><th>Profit</th></tr></thead>
                <tbody>
                    {% for zi, d in st.per_zi.items() %}
                    <tr><td>{{ zi }}</td><td>{{ d.trades }}</td>
                        <td>{{ "%.0f"|format(d.wins / d.trades * 100 if d.trades else 0) }}%</td>
                        <td class="{{ 'green' if d.profit >= 0 else 'red' }}">${{ "%.2f"|format(d.profit) }}</td></tr>
                    {% endfor %}
                </tbody>
            </table>
            <h2>🏆 Performanta pe Simbol</h2>
            <table>
                <thead><tr><th>Simbol</th><th>Trades</th><th>Win Rate</th><th>Profit</th></tr></thead>
                <tbody>
                    {% for s, d in st.per_simbol.items() %}
                    <tr><td><strong>{{ s }}</strong></td><td>{{ d.trades }}</td>
                        <td>{{ "%.0f"|format(d.wins / d.trades * 100 if d.trades else 0) }}%</td>
                        <td class="{{ 'green' if d.profit >= 0 else 'red' }}">${{ "%.2f"|format(d.profit) }}</td></tr>
                    {% endfor %}
                </tbody>
            </table>
            <h2>🚪 Performanta pe Motiv de Iesire</h2>
            <table>
                <thead><tr><th>Motiv</th><th>Count</th><th>Profit</th></tr></thead>
                <tbody>
                    {% for m, d in st.per_motiv.items() %}
                    <tr><td>{{ m }}</td><td>{{ d.count }}</td>
                        <td class="{{ 'green' if d.profit >= 0 else 'red' }}">${{ "%.2f"|format(d.profit) }}</td></tr>
                    {% endfor %}
                </tbody>
            </table>
            {% else %}
            <div class="card" style="text-align: center; color: #8b949e; padding: 40px;">
                Nu exista inca date de statistici.<br>
                Fisierele CSV se genereaza automat la inchiderea bursei.
            </div>
            {% endif %}
        </div>

        <div class="timestamp">Actualizat: {{ now }} | Auto-refresh: 120s | Date grafice: din agent (zero yfinance)</div>
    </div>

    <script>
        function switchTab(name, el) {
            document.querySelectorAll('.tab-content').forEach(function(t) { t.classList.remove('active'); });
            document.querySelectorAll('.tab').forEach(function(t) { t.classList.remove('active'); });
            document.getElementById('tab-' + name).classList.add('active');
            el.classList.add('active');
            window.dispatchEvent(new Event('resize'));
        }
    </script>
</body>
</html>
"""


@app.route("/")
def dashboard():
    memorie = incarca_memorie()
    stats = memorie.get("stats", {"total_profit": 0, "wins": 0, "losses": 0})
    total = stats["wins"] + stats["losses"]
    win_rate = (stats["wins"] / total * 100) if total > 0 else 0

    tranz_azi, profit_azi, inchise_azi = tranzactii_azi(memorie)

    grafice = incarca_grafice_cache()
    cache_updated = grafice.get("_updated", "niciodata")
    if cache_updated and cache_updated != "niciodata":
        cache_updated = cache_updated[11:19]

    try:
        account = api.get_account()
        account_data = {"cash": float(account.cash), "portfolio_value": float(account.portfolio_value)}
    except:
        account_data = {"cash": 0, "portfolio_value": 0}

    try:
        bursa_deschisa = api.get_clock().is_open
    except:
        bursa_deschisa = False

    pozitii_simboluri = []
    try:
        pozitii_raw = api.list_positions()
        pozitii = []
        for p in pozitii_raw:
            pozitii_simboluri.append(p.symbol)
            entry = float(p.avg_entry_price)
            chart_data, _, _ = date_grafic(p.symbol, grafice)
            pozitii.append({
                "symbol": p.symbol, "qty": int(p.qty), "avg_entry_price": entry,
                "current_price": float(p.current_price),
                "unrealized_pl": float(p.unrealized_pl),
                "unrealized_plpc": float(p.unrealized_plpc),
                "sl_price": entry * (1 - STOP_LOSS_PCT),
                "tp_price": entry * (1 + TAKE_PROFIT_PCT),
                "chart_data": json.dumps(chart_data)
            })
    except:
        pozitii = []

    watchlist = []
    for simbol in ACTIUNI:
        if simbol in pozitii_simboluri:
            continue
        chart_data, stt, change = date_grafic(simbol, grafice)
        watchlist.append({"symbol": simbol, "status": stt, "change": change,
                          "chart_data": json.dumps(chart_data)})

    st = calculeaza_statistici()

    return render_template_string(
        HTML, account=account_data, stats=stats, win_rate=win_rate,
        profit_azi=profit_azi, inchise_azi=inchise_azi, tranz_azi=tranz_azi,
        pozitii=pozitii, watchlist=watchlist, st=st, cache_updated=cache_updated,
        bursa_deschisa=bursa_deschisa, log_lines=citeste_log(),
        now=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    )


@app.route("/api/stats")
def api_stats():
    return jsonify(incarca_memorie())


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)