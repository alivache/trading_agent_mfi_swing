"""Analiza pe memorie_multitf.json de pe VM (sursa completa), nu pe CSV-urile zilnice —
acestea pierd zilele fara nicio inchidere END OF DAY (vezi raport_facut consumat premarket)."""
import json, os, collections, statistics as st
os.chdir(os.path.dirname(os.path.abspath(__file__)))

t = json.load(open("memorie_vm_2026-09-13.json"))["tranzactii"]
inchideri = [x for x in t if x["tip"] == "close_long"]
# pretul de intrare = ultimul open_long anterior pe acelasi simbol
for i, x in enumerate(t):
    if x["tip"] != "close_long":
        continue
    for k in range(i - 1, -1, -1):
        if t[k]["simbol"] == x["simbol"] and t[k]["tip"] == "open_long":
            x["pret_intrare"] = t[k]["pret"]
            x["ora_intrare"] = t[k]["ora"]
            break

def agg(rs, label):
    p = [x["profit"] for x in rs]
    w = [v for v in p if v > 0]; l = [v for v in p if v <= 0]
    gp, gl = sum(w), -sum(l)
    print(f"{label}\n   n={len(p)}  net=${sum(p):+.2f}  WR={100*len(w)/len(p):.0f}%  "
          f"PF={gp/gl if gl else float('inf'):.2f}  avgW=${gp/len(w):.2f}  avgL=-${gl/len(l):.2f}")

SAPT = [x for x in inchideri if x["data"] >= "2026-09-08"]
agg(inchideri, "TOT (25 aug - 11 sep)")
agg(SAPT, "ULTIMA SAPTAMANA (8-11 sep, 4 sesiuni)")
agg([x for x in inchideri if "2026-08-31" <= x["data"] <= "2026-09-04"],
    "SAPTAMANA ANTERIOARA (31 aug - 4 sep)")

print("\n-- ultima saptamana, tranzactie cu tranzactie")
for x in sorted(SAPT, key=lambda r: (r["data"], r["ora"])):
    pi = x.get("pret_intrare")
    pct = (x["pret"] - pi) / pi * 100 if pi else float("nan")
    print(f"   {x['data']} {x['simbol']:5s} {pct:+5.2f}%  ${x['profit']:+7.2f}  "
          f"mfe={x.get('mfe_pct',0):5.2f}%  mae={x.get('mae_pct',0):6.2f}%  "
          f"{x.get('durata_min',0):4d}min  intrare {x.get('ora_intrare','?')[:5]}  {x['motiv']}")

for eticheta, rs in (("ultima saptamana", SAPT), ("tot istoricul", inchideri)):
    m = [x["mfe_pct"] for x in rs if x.get("mfe_pct") is not None]
    a = [x["mae_pct"] for x in rs if x.get("mae_pct") is not None]
    print(f"\n-- excursii, {eticheta} (n={len(m)})")
    print(f"   MFE: medie={st.mean(m):.2f}% max={max(m):.2f}%  >=1.5% (arma trailing): "
          f"{sum(1 for v in m if v >= 1.5)}  >=4% (TP): {sum(1 for v in m if v >= 4)}")
    print(f"   MAE: medie={st.mean(a):.2f}% min={min(a):.2f}%  <=-0.9%: "
          f"{sum(1 for v in a if v <= -0.9)}  <=-1.5%: {sum(1 for v in a if v <= -1.5)}")

print("\n-- pe motiv de iesire (tot istoricul)")
d = collections.defaultdict(list)
for x in inchideri:
    d[x["motiv"].split(" (")[0]].append(x["profit"])
for k, v in sorted(d.items(), key=lambda r: -sum(r[1])):
    print(f"   {k:20s} n={len(v):2d}  net=${sum(v):+8.2f}  mediu=${st.mean(v):+7.2f}")
