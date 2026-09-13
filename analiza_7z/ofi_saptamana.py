"""OFI shadow, 31 aug - 4 sep: doar barele efectiv evaluate (in sesiune + quote-uri suficiente),
adica motiv in {ofi sub prag, candidat}. Restul sunt respinse inainte de comparatia cu pragul."""
import csv, collections, os, statistics as st
os.chdir(os.path.dirname(os.path.abspath(__file__)))

EVALUATE = {"ofi sub prag", "candidat"}
zile = collections.defaultdict(collections.Counter)
ratios, cand, q_all = [], [], []
per_simbol, per_ora = collections.Counter(), collections.Counter()

with open("ofi_shadow_bars.csv") as f:
    for r in csv.DictReader(f):
        zi = r["timestamp"][:10]
        zile[zi][r["motiv"]] += 1
        if zi < "2026-08-31" or r["motiv"] not in EVALUATE:
            continue
        x, q = float(r["ofi_ratio"]), int(r["quotes"])
        ratios.append(x); q_all.append(q)
        if x >= 0.08:
            cand.append((r["timestamp"], r["simbol"], x, q))
            per_simbol[r["simbol"]] += 1
            per_ora[r["timestamp"][11:13]] += 1

n = len(ratios); s = sorted(ratios)
pc = lambda p: s[int(p / 100 * (n - 1))]
print(f"bare evaluate 31 aug - 4 sep: n={n} ({n/5:.0f}/zi pe 20 simboluri)")
print(f"quotes/bara: median={st.median(q_all):.0f} p25={sorted(q_all)[n//4]} min={min(q_all)}")
print(f"ofi_ratio: p50={pc(50):+.4f} p75={pc(75):+.4f} p90={pc(90):+.4f} p95={pc(95):+.4f} "
      f"p99={pc(99):+.4f} max={s[-1]:+.4f} min={s[0]:+.4f}")
print(f"medie={st.mean(ratios):+.5f}  pozitive={100*sum(1 for x in ratios if x>0)/n:.1f}%")
for prag in (0.08, 0.15, 0.25):
    k = sum(1 for x in ratios if x >= prag)
    print(f"  prag {prag:.2f}: {k:4d} bare ({k/5:.1f}/zi, {100*k/n:.2f}% din evaluate)")

print(f"\ncandidati la 0.08: n={len(cand)}; cu <20 quote-uri: "
      f"{sum(1 for c in cand if c[3] < 20)}; cu >=100 quote-uri: {sum(1 for c in cand if c[3] >= 100)}")
print("pe ora (NY):", "  ".join(f"{h}={v}" for h, v in sorted(per_ora.items())))
print("pe simbol:", "  ".join(f"{k}={v}" for k, v in per_simbol.most_common()))
print("\ntop 10 candidati dupa ofi_ratio:")
for t, sim, x, q in sorted(cand, key=lambda r: -r[2])[:10]:
    print(f"   {t[:16]} {sim:6s} ratio={x:+.3f} quotes={q}")
print("\ntop 10 candidati dupa numarul de quote-uri (cei mai 'reali'):")
for t, sim, x, q in sorted(cand, key=lambda r: -r[3])[:10]:
    print(f"   {t[:16]} {sim:6s} ratio={x:+.3f} quotes={q}")
