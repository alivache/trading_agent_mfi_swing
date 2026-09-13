"""Ipoteza: |ofi_ratio| scade mecanic cu numarul de quote-uri, deci pragul fix de 0.08
selecteaza minutele subtiri, nu presiunea reala de cumparare."""
import csv, os, collections, statistics as st
os.chdir(os.path.dirname(os.path.abspath(__file__)))
BUCKETS = [(10, 25), (25, 50), (50, 100), (100, 200), (200, 400), (400, 10**9)]
d = collections.defaultdict(list)
with open("ofi_shadow_bars.csv") as f:
    for r in csv.DictReader(f):
        if r["timestamp"][:10] < "2026-08-31" or r["motiv"] not in ("ofi sub prag", "candidat"):
            continue
        q, x = int(r["quotes"]), float(r["ofi_ratio"])
        for lo, hi in BUCKETS:
            if lo <= q < hi:
                d[(lo, hi)].append(x); break
print(f"{'quote-uri':>12s} {'n':>7s} {'|ratio| median':>15s} {'p95 |ratio|':>12s} {'% >=0.08':>9s}")
for b in BUCKETS:
    v = d[b]
    if not v: continue
    a = sorted(abs(x) for x in v)
    print(f"{str(b[0])+'-'+(str(b[1]) if b[1]<10**9 else 'inf'):>12s} {len(v):7d} "
          f"{st.median(a):15.4f} {a[int(0.95*(len(a)-1))]:12.4f} "
          f"{100*sum(1 for x in v if x>=0.08)/len(v):8.2f}%")
