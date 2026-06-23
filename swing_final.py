import yfinance as yf
import alpaca_trade_api as tradeapi
import os
import json
import time
import csv
import numpy as np
import pandas as pd
from dotenv import load_dotenv
from datetime import datetime, timezone, date, timedelta
import warnings
warnings.filterwarnings("ignore")

load_dotenv()

API_KEY = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
BASE_URL = os.getenv("ALPACA_BASE_URL")
MAX_TRADE_SIZE_USD = float(os.getenv("MAX_TRADE_SIZE_USD", 2500))
MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES_PER_DAY", 10))

# Lista actiuni din .env
ACTIUNI_RAW = os.getenv("ACTIUNI", "AAPL,MSFT,NVDA,GOOGL,AMZN,NFLX")
ACTIUNI = [s.strip().upper() for s in ACTIUNI_RAW.split(",")]

api = tradeapi.REST(API_KEY, SECRET_KEY, BASE_URL)

# ═══════════════════════════════════════
# SETARI
# ═══════════════════════════════════════
INTERVAL_SCANARE = 300
MEMORIE_FILE = "memorie_final.json"

EMA_SCURTA = 20
EMA_LUNGA = 50
RSI_PERIOADA = 14
RSI_OVERSOLD = 45

STOP_LOSS_PCT = 0.02
TAKE_PROFIT_INITIAL = 0.03
TRAILING_STOP_PCT = 0.015
MAX_RISC_PORTOFOLIU = 0.02
MAX_POZITII = 5
COOLDOWN_ORE = 24

HMM_STARI = 3
HMM_MIN_DATE = 100
HMM_ITERATII = 50

trades_azi = 0
data_curenta = datetime.now().date()
pozitii_deschise = {}
raport_generat_azi = False
modele_hmm = {}


# ═══════════════════════════════════════
# HMM
# ═══════════════════════════════════════
class HMM:
    def __init__(self, n_stari=3, n_iter=50):
        self.n_stari = n_stari
        self.n_iter = n_iter
        self.pi = None
        self.A = None
        self.means = None
        self.covars = None
        self.antrenat = False

    def _gaussian(self, x, mean, var):
        var = max(var, 1e-10)
        return np.exp(-0.5 * ((x - mean) ** 2) / var) / np.sqrt(2 * np.pi * var)

    def _emisie(self, obs):
        T = len(obs)
        B = np.zeros((T, self.n_stari))
        for t in range(T):
            for s in range(self.n_stari):
                prob = 1.0
                for f in range(len(obs[t])):
                    prob *= self._gaussian(obs[t][f], self.means[s][f], self.covars[s][f])
                B[t][s] = max(prob, 1e-300)
        return B

    def _forward(self, obs, B):
        T = len(obs)
        alpha = np.zeros((T, self.n_stari))
        alpha[0] = self.pi * B[0]
        alpha[0] /= alpha[0].sum() + 1e-300
        for t in range(1, T):
            for s in range(self.n_stari):
                alpha[t][s] = np.dot(alpha[t-1], self.A[:, s]) * B[t][s]
            alpha[t] /= alpha[t].sum() + 1e-300
        return alpha

    def _backward(self, obs, B):
        T = len(obs)
        beta = np.zeros((T, self.n_stari))
        beta[-1] = 1.0
        for t in range(T-2, -1, -1):
            for s in range(self.n_stari):
                beta[t][s] = np.dot(self.A[s] * B[t+1], beta[t+1])
            beta[t] /= beta[t].sum() + 1e-300
        return beta

    def fit(self, obs):
        T, n_features = obs.shape
        np.random.seed(42)
        self.pi = np.ones(self.n_stari) / self.n_stari
        self.A = np.ones((self.n_stari, self.n_stari)) / self.n_stari
        self.means = np.array([obs[np.random.choice(T)] for _ in range(self.n_stari)])
        self.covars = np.ones((self.n_stari, n_features)) * np.var(obs, axis=0)

        for _ in range(self.n_iter):
            B = self._emisie(obs)
            alpha = self._forward(obs, B)
            beta = self._backward(obs, B)
            gamma = alpha * beta
            gamma /= gamma.sum(axis=1, keepdims=True) + 1e-300
            self.pi = gamma[0] / gamma[0].sum()
            for i in range(self.n_stari):
                for j in range(self.n_stari):
                    num = sum(alpha[t][i] * self.A[i][j] * B[t+1][j] * beta[t+1][j] for t in range(T-1))
                    den = gamma[:-1, i].sum()
                    self.A[i][j] = max(num / (den + 1e-300), 1e-10)
                self.A[i] /= self.A[i].sum()
            for s in range(self.n_stari):
                w = gamma[:, s]
                w_sum = w.sum() + 1e-300
                self.means[s] = (w[:, None] * obs).sum(axis=0) / w_sum
                diff = obs - self.means[s]
                self.covars[s] = np.maximum((w[:, None] * diff**2).sum(axis=0) / w_sum, 1e-6)
        self.antrenat = True
        return self

    def predict(self, obs):
        T, _ = obs.shape
        B = self._emisie(obs)
        viterbi = np.zeros((T, self.n_stari))
        psi = np.zeros((T, self.n_stari), dtype=int)
        viterbi[0] = np.log(self.pi + 1e-300) + np.log(B[0] + 1e-300)
        for t in range(1, T):
            for s in range(self.n_stari):
                trans = viterbi[t-1] + np.log(self.A[:, s] + 1e-300)
                psi[t][s] = np.argmax(trans)
                viterbi[t][s] = trans[psi[t][s]] + np.log(B[t][s] + 1e-300)
        stari = np.zeros(T, dtype=int)
        stari[-1] = np.argmax(viterbi[-1])
        for t in range(T-2, -1, -1):
            stari[t] = psi[t+1][stari[t+1]]
        return stari

    def predict_proba(self, obs):
        B = self._emisie(obs)
        alpha = self._forward(obs, B)
        beta = self._backward(obs, B)
        gamma = alpha * beta
        gamma /= gamma.sum(axis=1, keepdims=True) + 1e-300
        return gamma


# ═══════════════════════════════════════
# MEMORIE
# ═══════════════════════════════════════
def incarca_memorie():
    if os.path.exists(MEMORIE_FILE):
        with open(MEMORIE_FILE, "r") as f:
            return json.load(f)
    return {
        "tranzactii": [],
        "performanta": {},
        "cooldown": {},
        "stats": {
            "total_profit": 0,
            "wins": 0,
            "losses": 0
        }
    }


def salveaza_memorie(memorie):
    with open(MEMORIE_FILE, "w") as f:
        json.dump(memorie, f, indent=2, default=str)


def log_tranzactie(memorie, simbol, tip, pret, cantitate, profit=None, motiv=None):
    memorie["tranzactii"].append({
        "simbol": simbol,
        "tip": tip,
        "pret": pret,
        "cantitate": cantitate,
        "profit": profit,
        "motiv": motiv,
        "ora": datetime.now().hour,
        "data": datetime.now().isoformat()
    })

    if profit is not None:
        memorie["stats"]["total_profit"] += profit
        if profit > 0:
            memorie["stats"]["wins"] += 1
        else:
            memorie["stats"]["losses"] += 1
            memorie["cooldown"][simbol] = datetime.now().isoformat()
            print(f"  ⏳ COOLDOWN {COOLDOWN_ORE}h setat pentru {simbol}")

        if simbol not in memorie["performanta"]:
            memorie["performanta"][simbol] = {
                "profit": 0, "trades": 0, "wins": 0
            }
        p = memorie["performanta"][simbol]
        p["profit"] += profit
        p["trades"] += 1
        if profit > 0:
            p["wins"] += 1

    salveaza_memorie(memorie)


# ═══════════════════════════════════════
# COOLDOWN
# ═══════════════════════════════════════
def simbol_in_cooldown(simbol, memorie):
    if simbol not in memorie.get("cooldown", {}):
        return False

    data_pierdere = datetime.fromisoformat(memorie["cooldown"][simbol])
    ore_trecute = (datetime.now() - data_pierdere).total_seconds() / 3600

    if ore_trecute < COOLDOWN_ORE:
        ore_ramase = COOLDOWN_ORE - ore_trecute
        print(f"  ⏳ {simbol} in cooldown — mai {ore_ramase:.1f}h")
        return True

    del memorie["cooldown"][simbol]
    salveaza_memorie(memorie)
    return False


# ═══════════════════════════════════════
# EXPORT CSV
# ═══════════════════════════════════════
def export_csv_automat(memorie):
    tranzactii = memorie["tranzactii"]
    azi = date.today().isoformat()

    inchideri = [
        t for t in tranzactii
        if t["tip"] == "close_long"
        and t.get("profit") is not None
        and t["data"].startswith(azi)
    ]

    if not inchideri:
        print("📊 Nicio tranzactie de exportat azi.")
        return

    CSV_FILE = f"final_trades_{azi}.csv"
    campuri = [
        "data_iesire", "simbol", "cantitate",
        "pret_intrare", "pret_iesire",
        "profit_usd", "profit_pct",
        "motiv_exit", "rezultat"
    ]

    rows = []
    for t in inchideri:
        simbol = t["simbol"]
        pret_intrare = 0
        for d in reversed(tranzactii):
            if d["simbol"] == simbol and d["tip"] == "open_long":
                if d["data"] < t["data"]:
                    pret_intrare = d["pret"]
                    break

        profit = t["profit"]
        profit_pct = (profit / (pret_intrare * t["cantitate"]) * 100) if pret_intrare > 0 else 0

        rows.append({
            "data_iesire": t["data"],
            "simbol": simbol,
            "cantitate": t["cantitate"],
            "pret_intrare": round(pret_intrare, 4),
            "pret_iesire": round(t["pret"], 4),
            "profit_usd": round(profit, 2),
            "profit_pct": round(profit_pct, 2),
            "motiv_exit": t.get("motiv", ""),
            "rezultat": "WIN" if profit > 0 else "LOSS"
        })

    with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=campuri)
        writer.writeheader()
        writer.writerows(rows)

    wins = [r for r in rows if r["rezultat"] == "WIN"]
    profit_total = sum(r["profit_usd"] for r in rows)
    win_rate = len(wins) / len(rows) if rows else 0

    print(f"\n{'=' * 50}")
    print(f"📊 RAPORT ZILNIC — {azi}")
    print(f"{'=' * 50}")
    print(f"  🔄 Total trades:  {len(rows)}")
    print(f"  ✅ Wins:          {len(wins)}")
    print(f"  ❌ Losses:        {len(rows) - len(wins)}")
    print(f"  🎯 Win rate:      {win_rate:.1%}")
    print(f"  💰 Profit total:  ${profit_total:.2f}")
    print(f"  💾 CSV:           {CSV_FILE}")
    print(f"{'=' * 50}\n")


# ═══════════════════════════════════════
# INDICATORI
# ═══════════════════════════════════════
def calculeaza_ema(preturi, perioada):
    prices = pd.Series(preturi)
    return prices.ewm(span=perioada, adjust=False).mean().iloc[-1]


def calculeaza_rsi(preturi, perioada=14):
    prices = pd.Series(preturi)
    delta = prices.diff()
    castig = delta.where(delta > 0, 0.0)
    pierdere = -delta.where(delta < 0, 0.0)
    avg_castig = castig.rolling(window=perioada).mean().iloc[-1]
    avg_pierdere = pierdere.rolling(window=perioada).mean().iloc[-1]
    if avg_pierdere == 0:
        return 100
    rs = avg_castig / avg_pierdere
    return 100 - (100 / (1 + rs))


def calculeaza_macd(preturi):
    prices = pd.Series(preturi)
    ema12 = prices.ewm(span=12, adjust=False).mean()
    ema26 = prices.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    histogram = macd - signal
    return macd.iloc[-1], signal.iloc[-1], histogram.iloc[-1]


def calculeaza_atr(df, perioada=14):
    high = df["High"]
    low = df["Low"]
    close = df["Close"].shift(1)
    tr = pd.concat([
        high - low,
        (high - close).abs(),
        (low - close).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(perioada).mean().iloc[-1]


def calculeaza_volum_ratio(volume):
    if len(volume) < 20:
        return 1.0
    vol_recent = sum(volume[-5:]) / 5
    vol_mediu = sum(volume[-20:]) / 20
    return vol_recent / vol_mediu if vol_mediu > 0 else 1.0


def get_date(simbol, interval="1h", period="60d"):
    df = yf.download(simbol, period=period, interval=interval, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df


# ═══════════════════════════════════════
# HMM DETECTARE REGIM
# ═══════════════════════════════════════
def pregateste_features(df):
    close = df["Close"]
    high = df["High"]
    low = df["Low"]
    volume = df["Volume"]
    randament = close.pct_change().fillna(0)
    volatilitate = ((high - low) / close).fillna(0)
    vol_mediu = volume.rolling(20).mean()
    vol_relativ = (volume / vol_mediu).fillna(1)
    features = np.column_stack([
        randament.values,
        volatilitate.values,
        vol_relativ.values
    ])
    mean = features.mean(axis=0)
    std = features.std(axis=0) + 1e-10
    return (features - mean) / std


def detecteaza_regim(simbol, df):
    global modele_hmm
    try:
        features = pregateste_features(df)
        if len(features) < HMM_MIN_DATE:
            return "necunoscut", {}

        if simbol not in modele_hmm:
            print(f"  🧠 Antrenez HMM {simbol}...")
            model = HMM(n_stari=HMM_STARI, n_iter=HMM_ITERATII)
            model.fit(features)
            modele_hmm[simbol] = model
        else:
            model = modele_hmm[simbol]

        stari = model.predict(features)
        probabilitati = model.predict_proba(features)
        randamente = features[:, 0]

        randament_per_stare = {}
        for s in range(HMM_STARI):
            mask = stari == s
            randament_per_stare[s] = randamente[mask].mean() if mask.sum() > 0 else 0

        stare_bullish = max(randament_per_stare, key=randament_per_stare.get)
        stare_bearish = min(randament_per_stare, key=randament_per_stare.get)
        stare_curenta = stari[-1]
        prob_bullish = float(probabilitati[-1][stare_bullish])

        if stare_curenta == stare_bullish:
            regim = "bullish"
        elif stare_curenta == stare_bearish:
            regim = "bearish"
        else:
            regim = "lateral"

        return regim, {"prob_bullish": prob_bullish}

    except Exception as e:
        print(f"  Eroare HMM {simbol}: {e}")
        return "necunoscut", {}


# ═══════════════════════════════════════
# CONFIRMARE 4H
# ═══════════════════════════════════════
def confirmare_4h(simbol):
    try:
        df_4h = get_date(simbol, interval="4h", period="60d")
        if df_4h.empty or len(df_4h) < 50:
            return True

        preturi_4h = df_4h["Close"].tolist()
        ema20_4h = calculeaza_ema(preturi_4h, EMA_SCURTA)
        ema50_4h = calculeaza_ema(preturi_4h, EMA_LUNGA)
        rsi_4h = calculeaza_rsi(preturi_4h, RSI_PERIOADA)
        macd_4h, signal_4h, _ = calculeaza_macd(preturi_4h)

        return (ema20_4h > ema50_4h and
                macd_4h > signal_4h and
                40 < rsi_4h < 75)

    except Exception as e:
        return True


# ═══════════════════════════════════════
# SEMNAL
# ═══════════════════════════════════════
def analizeaza_semnal(simbol, memorie):
    try:
        if simbol_in_cooldown(simbol, memorie):
            return None, {}

        df = get_date(simbol, interval="1h", period="60d")
        if df.empty or len(df) < HMM_MIN_DATE:
            return None, {}

        preturi = df["Close"].tolist()
        volume = df["Volume"].tolist()
        pret_curent = preturi[-1]

        ema20 = calculeaza_ema(preturi, EMA_SCURTA)
        ema50 = calculeaza_ema(preturi, EMA_LUNGA)
        ema20_prev = calculeaza_ema(preturi[:-1], EMA_SCURTA)
        ema50_prev = calculeaza_ema(preturi[:-1], EMA_LUNGA)
        rsi = calculeaza_rsi(preturi, RSI_PERIOADA)
        macd, signal, histogram = calculeaza_macd(preturi)
        atr = calculeaza_atr(df)
        volum_ratio = calculeaza_volum_ratio(volume)
        regim, info_hmm = detecteaza_regim(simbol, df)
        confirmat_4h = confirmare_4h(simbol)

        info = {
            "pret": pret_curent,
            "ema20": ema20,
            "ema50": ema50,
            "rsi": rsi,
            "macd": macd,
            "signal": signal,
            "atr": atr,
            "volum_ratio": volum_ratio,
            "regim_hmm": regim,
            "info_hmm": info_hmm,
            "confirmat_4h": confirmat_4h
        }

        trend_bullish = ema20 > ema50
        crossover_bullish = ema20_prev < ema50_prev and ema20 > ema50
        rsi_ok = RSI_OVERSOLD < rsi < 70
        macd_bullish = macd > signal
        volum_ok = volum_ratio > 0.9
        hmm_bullish = regim == "bullish"
        prob_bullish = info_hmm.get("prob_bullish", 0)

        semnal = None
        motiv = ""

        if (trend_bullish and rsi_ok and macd_bullish
                and volum_ok and hmm_bullish
                and prob_bullish > 0.5
                and confirmat_4h):
            semnal = "long"
            motiv = (
                f"HMM=BULLISH({prob_bullish:.0%}) | "
                f"4H=✅ | "
                f"EMA20({ema20:.2f})>EMA50({ema50:.2f}) | "
                f"RSI={rsi:.1f} | MACD▲"
            )
            if crossover_bullish:
                motiv += " | ⭐ CROSSOVER"
            info["motiv_intrare"] = motiv

        return semnal, info

    except Exception as e:
        print(f"  Eroare analiza {simbol}: {e}")
        return None, {}


# ═══════════════════════════════════════
# EXIT CU TRAILING
# ═══════════════════════════════════════
def verifica_exit(simbol, pret_intrare, pret_max, trailing_activ):
    try:
        df = get_date(simbol, interval="1h", period="5d")
        if df.empty:
            return False, None, pret_max, trailing_activ

        preturi = df["Close"].tolist()
        pret_curent = preturi[-1]
        rsi = calculeaza_rsi(preturi, RSI_PERIOADA)
        ema20 = calculeaza_ema(preturi, EMA_SCURTA)
        ema50 = calculeaza_ema(preturi, EMA_LUNGA)
        macd, signal, _ = calculeaza_macd(preturi)
        variatie = (pret_curent - pret_intrare) / pret_intrare

        pret_max = max(pret_max, pret_curent)

        if variatie >= TAKE_PROFIT_INITIAL and not trailing_activ:
            trailing_activ = True
            print(f"  🎯 {simbol} — Trailing activat la {variatie:.2%}!")

        if trailing_activ:
            drawdown = (pret_max - pret_curent) / pret_max
            if drawdown >= TRAILING_STOP_PCT:
                return True, f"TRAILING STOP (profit={variatie:.2%})", pret_max, trailing_activ

        if variatie <= -STOP_LOSS_PCT:
            return True, f"STOP LOSS ({variatie:.2%})", pret_max, trailing_activ

        regim, _ = detecteaza_regim(simbol, df)
        if regim == "bearish" and variatie > 0:
            return True, f"HMM BEARISH ({variatie:.2%})", pret_max, trailing_activ

        if ema20 < ema50:
            return True, f"EMA CROSSOVER BEARISH ({variatie:.2%})", pret_max, trailing_activ

        if macd < signal and variatie > 0.01:
            return True, f"MACD BEARISH ({variatie:.2%})", pret_max, trailing_activ

        if rsi > 75:
            return True, f"RSI OVERBOUGHT ({rsi:.1f})", pret_max, trailing_activ

        return False, None, pret_max, trailing_activ

    except Exception as e:
        return False, None, pret_max, trailing_activ


# ═══════════════════════════════════════
# CALCUL CANTITATE
# ═══════════════════════════════════════
def calculeaza_cantitate(pret, atr):
    try:
        account = api.get_account()
        portofoliu = float(account.portfolio_value)
    except:
        portofoliu = 100000

    risc_max = portofoliu * MAX_RISC_PORTOFOLIU
    stop_loss_dinamic = max(STOP_LOSS_PCT, atr / pret * 2)
    cantitate_risc = int(risc_max / (pret * stop_loss_dinamic))
    cantitate_size = int(MAX_TRADE_SIZE_USD / pret)
    cantitate = min(cantitate_risc, cantitate_size)
    return max(1, cantitate), stop_loss_dinamic


# ═══════════════════════════════════════
# TRANZACTIONARE
# ═══════════════════════════════════════
def deschide_pozitie(simbol, pret, cantitate, stop_loss, motiv, memorie):
    global trades_azi
    try:
        api.submit_order(
            symbol=simbol, qty=cantitate,
            side="buy", type="market", time_in_force="gtc"
        )
        print(f"  ✅ LONG {cantitate}x {simbol} @ ${pret:.2f}")
        print(f"     {motiv}")
        print(f"     SL={stop_loss:.2%} | Trailing dupa {TAKE_PROFIT_INITIAL:.0%}")

        pozitii_deschise[simbol] = {
            "pret_intrare": pret,
            "cantitate": cantitate,
            "pret_max": pret,
            "stop_loss": stop_loss,
            "trailing_activ": False
        }
        trades_azi += 1
        log_tranzactie(memorie, simbol, "open_long", pret, cantitate)

    except Exception as e:
        print(f"  Eroare deschidere {simbol}: {e}")


def inchide_pozitie(simbol, motiv, memorie):
    if simbol not in pozitii_deschise:
        return
    pozitie = pozitii_deschise[simbol]
    try:
        df = get_date(simbol, interval="1h", period="1d")
        pret_curent = df["Close"].iloc[-1]

        api.submit_order(
            symbol=simbol, qty=pozitie["cantitate"],
            side="sell", type="market", time_in_force="gtc"
        )
        profit = (pret_curent - pozitie["pret_intrare"]) * pozitie["cantitate"]

        emoji = "🟢" if profit > 0 else "🔴"
        print(f"  {emoji} INCHIS {simbol} | {motiv} | Profit: ${profit:.2f}")
        log_tranzactie(
            memorie, simbol, "close_long",
            pret_curent, pozitie["cantitate"], profit, motiv
        )
        del pozitii_deschise[simbol]

    except Exception as e:
        print(f"  Eroare inchidere {simbol}: {e}")


# ═══════════════════════════════════════
# DISPLAY
# ═══════════════════════════════════════
def afiseaza_stats(memorie):
    stats = memorie["stats"]
    total = stats["wins"] + stats["losses"]
    rata = stats["wins"] / total if total > 0 else 0
    print(f"\n📊 STATS: Profit=${stats['total_profit']:.2f} | "
          f"Win rate={rata:.1%} | Trades={total}")


def afiseaza_pozitii():
    if not pozitii_deschise:
        print("\n📂 Nicio pozitie deschisa")
        return
    print("\n📂 POZITII DESCHISE:")
    for simbol, poz in pozitii_deschise.items():
        variatie = 0
        try:
            df = get_date(simbol, interval="1h", period="1d")
            pret_curent = df["Close"].iloc[-1]
            variatie = (pret_curent - poz["pret_intrare"]) / poz["pret_intrare"]
        except:
            pass
        emoji = "🟢" if variatie > 0 else "🔴"
        trailing = "🎯 TRAILING" if poz.get("trailing_activ") else ""
        print(f"  {emoji} {simbol} LONG | "
              f"Intrare=${poz['pret_intrare']:.2f} | "
              f"P&L={variatie:.2%} | "
              f"SL={poz['stop_loss']:.2%} {trailing}")


def afiseaza_performanta(memorie):
    if not memorie["performanta"]:
        return
    print("\n🏆 PERFORMANTA:")
    print(f"  {'Simbol':<8} {'Trades':<8} {'Win%':<8} {'Profit'}")
    print(f"  {'-' * 35}")
    for simbol, p in sorted(
        memorie["performanta"].items(),
        key=lambda x: x[1]["profit"], reverse=True
    ):
        wr = p["wins"] / p["trades"] if p["trades"] > 0 else 0
        emoji = "🟢" if p["profit"] > 0 else "🔴"
        cooldown = "⏳" if simbol in memorie.get("cooldown", {}) else ""
        print(f"  {emoji} {simbol:<8} {p['trades']:<8} {wr:<8.0%} ${p['profit']:.2f} {cooldown}")


def afiseaza_regimuri(info_simboluri):
    if not info_simboluri:
        return
    print("\n🧠 REGIMURI HMM:")
    for simbol, info in info_simboluri.items():
        regim = info.get("regim_hmm", "N/A")
        info_h = info.get("info_hmm", {})
        prob = info_h.get("prob_bullish", 0) if info_h else 0
        tf4h = "✅" if info.get("confirmat_4h") else "❌"
        emoji = "🟢" if regim == "bullish" else "🔴" if regim == "bearish" else "🟡"
        print(f"  {emoji} {simbol:<8} HMM={regim:<10} prob={prob:.0%} | 4H={tf4h}")


# ═══════════════════════════════════════
# AGENT PRINCIPAL
# ═══════════════════════════════════════
def agent():
    global trades_azi, data_curenta, raport_generat_azi

    print("🤖 SWING FINAL — HMM + 2xTF + TRAILING + COOLDOWN")
    print(f"📋 Actiuni: {', '.join(ACTIUNI)}")
    print(f"🧠 HMM {HMM_STARI} stari | Confirmare 1H+4H | Cooldown {COOLDOWN_ORE}h")
    print(f"💰 Max trade: ${MAX_TRADE_SIZE_USD} | Max pozitii: {MAX_POZITII}")
    print(f"🛑 SL={STOP_LOSS_PCT:.1%} | Trailing dupa {TAKE_PROFIT_INITIAL:.0%}")
    print(f"⏱️  Scanare la fiecare {INTERVAL_SCANARE // 60} minute")
    print("-" * 60)

    memorie = incarca_memorie()
    ciclu = 0

    while True:
        try:
            if datetime.now().date() != data_curenta:
                data_curenta = datetime.now().date()
                trades_azi = 0
                raport_generat_azi = False
                modele_hmm.clear()
                print("🔄 Zi noua — reset modele HMM")

            print(f"\n⏰ {datetime.now().strftime('%H:%M:%S')} | "
                  f"Ciclu #{ciclu} | "
                  f"Trades: {trades_azi}/{MAX_TRADES_PER_DAY} | "
                  f"Pozitii: {len(pozitii_deschise)}/{MAX_POZITII}")

            clock = api.get_clock()
            if not clock.is_open:
                ora_acum = datetime.now().hour
                minut_acum = datetime.now().minute
                if ora_acum == 23 and minut_acum < 5 and not raport_generat_azi:
                    print("🔔 Bursa inchisa — generez raport...")
                    export_csv_automat(memorie)
                    raport_generat_azi = True

                print(f"❌ Bursa inchisa. Se deschide la: {clock.next_open}")
                time.sleep(300)
                continue

            # EXIT
            for simbol in list(pozitii_deschise.keys()):
                poz = pozitii_deschise[simbol]
                exit_acum, motiv, pret_max_nou, trailing_nou = verifica_exit(
                    simbol,
                    poz["pret_intrare"],
                    poz["pret_max"],
                    poz.get("trailing_activ", False)
                )
                pozitii_deschise[simbol]["pret_max"] = pret_max_nou
                pozitii_deschise[simbol]["trailing_activ"] = trailing_nou
                if exit_acum:
                    inchide_pozitie(simbol, motiv, memorie)

            # CAUTA INTRARI
            locuri_libere = MAX_POZITII - len(pozitii_deschise)
            info_simboluri = {}

            if locuri_libere > 0 and trades_azi < MAX_TRADES_PER_DAY:
                print(f"\n🔍 Scanez {len(ACTIUNI)} actiuni | Locuri: {locuri_libere}")

                candidati = []
                for simbol in ACTIUNI:
                    if simbol in pozitii_deschise:
                        continue

                    semnal, info = analizeaza_semnal(simbol, memorie)
                    info_simboluri[simbol] = info

                    if info and "rsi" in info:
                        regim = info.get("regim_hmm", "N/A")
                        emoji = "🟢" if regim == "bullish" else "🔴" if regim == "bearish" else "🟡"
                        tf4h = "✅" if info.get("confirmat_4h") else "❌"
                        print(f"  {simbol}: P=${info.get('pret', 0):.2f} | "
                              f"HMM={emoji}{regim:<8} | "
                              f"4H={tf4h} | "
                              f"RSI={info.get('rsi', 0):.1f} | "
                              f"Semnal={'🟢 LONG' if semnal == 'long' else '⏳ none'}")

                    if semnal:
                        candidati.append((simbol, info))

                candidati.sort(
                    key=lambda x: x[1].get("info_hmm", {}).get("prob_bullish", 0)
                    if x[1].get("info_hmm") else 0,
                    reverse=True
                )

                for simbol, info in candidati[:locuri_libere]:
                    if trades_azi >= MAX_TRADES_PER_DAY:
                        break
                    cantitate, stop_loss = calculeaza_cantitate(
                        info["pret"], info["atr"]
                    )
                    deschide_pozitie(
                        simbol, info["pret"],
                        cantitate, stop_loss,
                        info.get("motiv_intrare", ""),
                        memorie
                    )
            else:
                print(f"\n⏳ Portofoliu plin sau limita atinsa")

            afiseaza_regimuri(info_simboluri)
            afiseaza_pozitii()
            afiseaza_stats(memorie)
            afiseaza_performanta(memorie)

            ciclu += 1
            print(f"\n⏳ Urmatoarea scanare in {INTERVAL_SCANARE // 60} minute...")
            time.sleep(INTERVAL_SCANARE)

        except Exception as e:
            print(f"❌ Eroare generala: {e}")
            time.sleep(60)


# ═══════════════════════════════════════
# START
# ═══════════════════════════════════════
def start():
    clock = api.get_clock()
    if clock.is_open:
        print("✅ Bursa DESCHISA!")
        agent()
    else:
        print(f"❌ Bursa INCHISA — se deschide la: {clock.next_open}")
        if input("Pornesc si astept? (da/nu): ").lower() == "da":
            agent()


start()