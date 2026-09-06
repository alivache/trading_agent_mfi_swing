import csv, glob, collections, os
os.chdir(os.path.dirname(os.path.abspath(__file__)))
rows = []
for f in sorted(glob.glob("multitf_trades_2026-*.csv")):
    rows += list(csv.DictReader(open(f)))

def agg(rs, label):
    p = [float(r["profit_usd"]) for r in rs]
    w = [x for x in p if x > 0]; l = [x for x in p if x <= 0]
    gp, gl = sum(w), -sum(l)
    pf = gp / gl if gl else float("inf")
    print(f"{label}\n   n={len(p)}  net=${sum(p):.2f}  WR={100*len(w)/len(p):.0f}%  PF={pf:.2f}"
          f"  gp=${gp:.2f}  gl=-${gl:.2f}  avgW=${gp/len(w):.2f}  avgL=-${gl/len(l):.2f}")

last7 = [r for r in rows if r["data_iesire"][:10] >= "2026-08-31"]
agg(rows, "TOT ISTORICUL (25 aug - 4 sep)")
agg(last7, "ULTIMELE 7 ZILE (31 aug - 4 sep)")

for k in ("motiv_exit", "simbol"):
    d = collections.defaultdict(list)
    for r in last7: d[r[k]].append(float(r["profit_usd"]))
    print(f"\n-- pe {k}")
    for kk, v in sorted(d.items(), key=lambda x: -sum(x[1])):
        print(f"   {kk:35s} n={len(v)}  net=${sum(v):7.2f}")

print("\n-- excursii (7 zile)")
for r in last7:
    print(f"   {r['data_iesire'][:10]} {r['simbol']:5s} {float(r['profit_pct']):+5.2f}%"
          f"  mfe={float(r['mfe_pct']):5.2f}%  mae={float(r['mae_pct']):5.2f}%"
          f"  {int(r['durata_min']):4d}min  {r['motiv_exit']}")
mfe = [float(r["mfe_pct"]) for r in last7]
print(f"   MFE mediu={sum(mfe)/len(mfe):.2f}%   MFE>=1.5% (arma trailing): {sum(1 for x in mfe if x>=1.5)}/{len(mfe)}"
      f"   MFE>=4% (TP): {sum(1 for x in mfe if x>=4)}")
mae = [float(r["mae_pct"]) for r in last7]
print(f"   MAE mediu={sum(mae)/len(mae):.2f}%   MAE<=-0.9% (atins SL nou): {sum(1 for x in mae if x<=-0.9)}"
      f"   MAE<=-1.5% (ar fi atins SL vechi): {sum(1 for x in mae if x<=-1.5)}")
