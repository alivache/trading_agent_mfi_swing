"""
Backtest pentru swing_final.py — EMA20/50 + RSI + MACD + HMM + confirmare 4H.
Timeframe 1H. HMM antrenat o data pe zi per simbol (ca in agentul real).
ATENTIE: poate dura 30-60 min pe e2-micro.
"""
import os
import csv
import yfinance as yf
import pandas as pd
import numpy as np
from dotenv import load_dotenv
from datetime import datetime
import warnings
warnings.filterwarnings("ignore")

load_dotenv()

ACTIUNI_RAW = os.getenv("ACTIUNI", "AAPL,MSFT,NVDA,GOOGL,AMZN,NFLX")
ACTIUNI = [s.strip().upper() for s in ACTIUNI_RAW.split(",")]
MAX_TRADE_SIZE_USD = float(os.getenv("MAX_TRADE_SIZE_USD", 2500))

PORTOFOLIU_INITIAL = 100000
MAX_RISC_PORTOFOLIU = 0.02
MAX_POZITII = 5
COOLDOWN_ORE = 24

EMA_SCURTA = 20
EMA_LUNGA = 50
RSI_PERIOADA = 14
RSI_OVERSOLD = 45
STOP_LOSS_PCT = 0.02
TAKE_PROFIT_INITIAL = 0.03
TRAILING_STOP_PCT = 0.015

HMM_STARI = 3
HMM_MIN_DATE = 100
HMM_ITERATII = 30  # redus de la 50 pentru viteza


# ═══════════════════════════════════════
# HMM (identic cu swing_final.py)
# ═══════════════════════════════════════
class HMM:
    def __init__(self, n_stari=3, n_iter=30):
        self.n_stari = n_stari
        self.n_iter = n_iter
        self.pi = None
        self.A = None
        self.means = None
        self.covars = None

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
# INDICATORI
# ═══════════════════════════════════════
def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def rsi(s, n=14):
    delta = s.diff()
    c = delta.where(delta > 0, 0.0)
    p = -delta.where(delta < 0, 0.0)
    ac = c.rolling(n).mean()
    ap = p.rolling(n).mean()
    rs = ac / ap.replace(0, 0.0001)
    return (100 - (100 / (1 + rs))).fillna(50)


def macd_calc(s):
    e12 = s.ewm(span=12, adjust=False).mean()
    e26 = s.ewm(span=26, adjust=False).mean()
    macd = e12 - e26
    signal = macd.ewm(span=9, adjust=False).mean()
    return macd, signal


def atr_calc(df, n=14):
    high = df["High"]
    low = df["Low"]
    close = df["Close"].shift(1)
    tr = pd.concat([high - low, (high - close).abs(), (low - close).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def features_hmm(df):
    close = df["Close"]
    high = df["High"]
    low = df["Low"]
    volume = df["Volume"]
    randament = close.pct_change().fillna(0)
    volatilitate = ((high - low) / close).fillna(0)
    vol_mediu = volume.rolling(20).mean()
    vol_relativ = (volume / vol_mediu).fillna(1)
    feat = np.column_stack([randament.values, volatilitate.values, vol_relativ.values])
    mean = feat.mean(axis=0)
    std = feat.std(axis=0) + 1e-10
    return (feat - mean) / std


def fetch(simbol, interval, period):
    df = yf.download(simbol, period=period, interval=interval, progress=False, auto_adjust=False)
    if df.empty:
        return df
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df


def pregateste(simbol):
    print(f"  Descarc {simbol}...", end=" ", flush=True)
    try:
        df_1h = fetch(simbol, "1h", "60d")
        df_4h = fetch(simbol, "4h", "60d")
        if df_1h.empty or len(df_1h) < HMM_MIN_DATE:
            print("date insuficiente")
            return None
        df_1h["ema20"] = ema(df_1h["Close"], EMA_SCURTA)
        df_1h["ema50"] = ema(df_1h["Close"], EMA_LUNGA)
        df_1h["rsi"] = rsi(df_1h["Close"], RSI_PERIOADA)
        m, sig = macd_calc(df_1h["Close"])
        df_1h["macd"] = m
        df_1h["macd_signal"] = sig
        df_1h["atr"] = atr_calc(df_1h, 14)
        if not df_4h.empty and len(df_4h) >= 50:
            df_4h["ema20"] = ema(df_4h["Close"], EMA_SCURTA)
            df_4h["ema50"] = ema(df_4h["Close"], EMA_LUNGA)
            df_4h["rsi"] = rsi(df_4h["Close"], RSI_PERIOADA)
            m4, s4 = macd_calc(df_4h["Close"])
            df_4h["macd"] = m4
            df_4h["macd_signal"] = s4
        for d in [df_1h, df_4h]:
            if not d.empty:
                if d.index.tz is None:
                    d.index = d.index.tz_localize("UTC")
                else:
                    d.index = d.index.tz_convert("UTC")
        print(f"OK ({len(df_1h)} ore)")
        return {"1h": df_1h, "4h": df_4h}
    except Exception as e:
        print(f"eroare: {e}")
        return None


def detecteaza_regim(features, model):
    try:
        if len(features) < HMM_MIN_DATE:
            return "necunoscut", 0
        stari = model.predict(features)
        proba = model.predict_proba(features)
        randamente = features[:, 0]
        rand_stare = {}
        for s in range(HMM_STARI):
            mask = stari == s
            rand_stare[s] = randamente[mask].mean() if mask.sum() > 0 else 0
        stare_bull = max(rand_stare, key=rand_stare.get)
        stare_bear = min(rand_stare, key=rand_stare.get)
        sc = stari[-1]
        prob_bull = float(proba[-1][stare_bull])
        if sc == stare_bull:
            return "bullish", prob_bull
        elif sc == stare_bear:
            return "bearish", prob_bull
        return "lateral", prob_bull
    except:
        return "necunoscut", 0


def confirmare_4h(df_4h, t):
    if df_4h.empty:
        return True
    df = df_4h[df_4h.index <= t]
    if len(df) < 50:
        return True
    last = df.iloc[-1]
    if pd.isna(last["ema50"]) or pd.isna(last["rsi"]) or pd.isna(last["macd_signal"]):
        return True
    return (last["ema20"] > last["ema50"]
            and last["macd"] > last["macd_signal"]
            and 40 < last["rsi"] < 75)


def main():
    print("\n" + "=" * 65)
    print("BACKTEST swing_final — EMA+RSI+MACD+HMM+4H (timeframe 1H)")
    print(f"SL={STOP_LOSS_PCT:.1%} | TP trailing dupa {TAKE_PROFIT_INITIAL:.0%}")
    print("ATENTIE: poate dura 30-60 min")
    print("=" * 65 + "\n")

    print("Descarc datele...")
    date_s = {}
    for s in ACTIUNI:
        d = pregateste(s)
        if d:
            date_s[s] = d
    if not date_s:
        return

    timestamps = set()
    for d in date_s.values():
        timestamps.update(d["1h"].index)
    timestamps = sorted(timestamps)
    print(f"\nTimeline: {len(timestamps)} ore. Incep simularea...\n")

    portofoliu = PORTOFOLIU_INITIAL
    pozitii = {}
    cooldown = {}
    trades = []
    modele_hmm = {}
    zi_curenta = None

    for idx, t in enumerate(timestamps):
        zi_t = t.date()
        # Reantreneaza HMM o data pe zi (ca agentul real care reseteaza la zi noua)
        if zi_t != zi_curenta:
            zi_curenta = zi_t
            modele_hmm = {}

        # Doar ore de tranzactionare (13:30-20:00 UTC = 9:30-16:00 ET)
        if t.hour < 13 or (t.hour == 13 and t.minute < 30) or t.hour >= 20:
            continue

        # EXIT
        for s in list(pozitii.keys()):
            poz = pozitii[s]
            df = date_s[s]["1h"]
            df_t = df[df.index <= t]
            if df_t.empty:
                continue
            last = df_t.iloc[-1]
            pret = last["Close"]
            variatie = (pret - poz["pret_intrare"]) / poz["pret_intrare"]
            poz["pret_max"] = max(poz["pret_max"], pret)

            iesire, motiv = False, None
            if variatie >= TAKE_PROFIT_INITIAL and not poz["trailing"]:
                poz["trailing"] = True
            if poz["trailing"]:
                dd = (poz["pret_max"] - pret) / poz["pret_max"]
                if dd >= TRAILING_STOP_PCT:
                    iesire, motiv = True, "TRAILING"
            if not iesire and variatie <= -STOP_LOSS_PCT:
                iesire, motiv = True, "STOP_LOSS"
            if not iesire and not pd.isna(last["ema50"]) and last["ema20"] < last["ema50"]:
                iesire, motiv = True, "EMA_BEARISH"
            if not iesire and not pd.isna(last["macd_signal"]) and last["macd"] < last["macd_signal"] and variatie > 0.01:
                iesire, motiv = True, "MACD_BEARISH"
            if not iesire and not pd.isna(last["rsi"]) and last["rsi"] > 75:
                iesire, motiv = True, "RSI_OB"

            if iesire:
                profit = (pret - poz["pret_intrare"]) * poz["cantitate"]
                trades.append({"simbol": s, "profit_usd": profit, "motiv": motiv,
                               "rezultat": "WIN" if profit > 0 else "LOSS"})
                portofoliu += profit
                if profit < 0:
                    cooldown[s] = t
                del pozitii[s]

        # INTRARI
        if len(pozitii) >= MAX_POZITII:
            continue

        for s in date_s.keys():
            if s in pozitii:
                continue
            if s in cooldown and (t - cooldown[s]).total_seconds() / 3600 < COOLDOWN_ORE:
                continue

            df = date_s[s]["1h"]
            df_t = df[df.index <= t]
            if len(df_t) < HMM_MIN_DATE:
                continue
            last = df_t.iloc[-1]
            if pd.isna(last["ema50"]) or pd.isna(last["rsi"]) or pd.isna(last["macd_signal"]) or pd.isna(last["atr"]):
                continue

            # Conditii EMA/RSI/MACD
            trend = last["ema20"] > last["ema50"]
            rsi_ok = RSI_OVERSOLD < last["rsi"] < 70
            macd_bull = last["macd"] > last["macd_signal"]
            if not (trend and rsi_ok and macd_bull):
                continue

            # HMM (antrenat o data pe zi per simbol)
            if s not in modele_hmm:
                feat = features_hmm(df_t)
                if len(feat) < HMM_MIN_DATE:
                    continue
                model = HMM(HMM_STARI, HMM_ITERATII)
                model.fit(feat)
                modele_hmm[s] = model
            feat = features_hmm(df_t)
            regim, prob_bull = detecteaza_regim(feat, modele_hmm[s])
            if regim != "bullish" or prob_bull < 0.5:
                continue

            # Confirmare 4H
            if not confirmare_4h(date_s[s]["4h"], t):
                continue

            # Intra
            pret = last["Close"]
            atr_v = last["atr"]
            risc_max = portofoliu * MAX_RISC_PORTOFOLIU
            sl_din = max(STOP_LOSS_PCT, atr_v / pret * 2)
            cant_risc = int(risc_max / (pret * sl_din))
            cant_size = int(MAX_TRADE_SIZE_USD / pret)
            cantitate = max(1, min(cant_risc, cant_size))
            pozitii[s] = {"pret_intrare": pret, "cantitate": cantitate,
                          "pret_max": pret, "trailing": False}
            if len(pozitii) >= MAX_POZITII:
                break

        if idx % 200 == 0:
            print(f"  {t.date()} {t.strftime('%H:%M')} | Pozitii: {len(pozitii)} | "
                  f"Trades: {len(trades)} | ${portofoliu:.0f}")

    # RAPORT
    print("\n" + "=" * 65)
    print("REZULTATE BACKTEST swing_final")
    print("=" * 65)
    total = len(trades)
    if total == 0:
        print("Nicio tranzactie.")
        return
    wins = [x for x in trades if x["rezultat"] == "WIN"]
    losses = [x for x in trades if x["rezultat"] == "LOSS"]
    cb = sum(x["profit_usd"] for x in wins)
    pb = abs(sum(x["profit_usd"] for x in losses))
    wr = len(wins) / total * 100
    pf = cb / pb if pb > 0 else 999
    cm = cb / len(wins) if wins else 0
    pm = pb / len(losses) if losses else 0

    print(f"\n  Portofoliu final:     ${portofoliu:,.2f}")
    print(f"  Randament:            {(portofoliu/PORTOFOLIU_INITIAL-1)*100:+.2f}%")
    print(f"  Total trades:         {total}")
    print(f"  Win rate:             {wr:.1f}%")
    print(f"  Profit factor:        {pf:.2f}")
    print(f"  Castig mediu:         ${cm:.2f}")
    print(f"  Pierdere medie:       ${pm:.2f}")
    print(f"  Raport C/P:           {cm/pm:.2f}" if pm > 0 else "")

    if pf >= 1.5:
        print(f"\n  ✅ Profit factor BUN")
    elif pf >= 1.0:
        print(f"\n  🟡 Profit factor SLAB")
    else:
        print(f"\n  🔴 Profit factor SUB 1.0")

    per_motiv = {}
    for x in trades:
        m = x["motiv"]
        per_motiv.setdefault(m, {"n": 0, "p": 0.0})
        per_motiv[m]["n"] += 1
        per_motiv[m]["p"] += x["profit_usd"]
    print(f"\n  Pe motiv de iesire:")
    for m, d in sorted(per_motiv.items(), key=lambda x: x[1]["p"], reverse=True):
        print(f"    {m:<14} {d['n']:>3}   ${d['p']:>9.2f}")
    print("=" * 65 + "\n")


if __name__ == "__main__":
    main()
