import csv, os, collections
os.chdir(os.path.dirname(os.path.abspath(__file__)))

rows = list(csv.DictReader(open("ofi_shadow_bars.csv")))
print("total randuri:", len(rows))
print("motive:", dict(collections.Counter(r["motiv"] for r in rows)))

# Reproducem exact ordinea de decizie din proceseaza_bar (ofi_vwap_shadow.py:143-150)
MIN_QUOTES = 10
elig = [r for r in rows if r["motiv"] not in ("in afara sesiunii",)
        and int(r["quotes"]) >= MIN_QUOTES]
print(f"bare in sesiune cu quotes>={MIN_QUOTES}: {len(elig)}")

vals = sorted(float(r["ofi_ratio"]) for r in elig)
n = len(vals)
def pct(p): return vals[min(n - 1, int(n * p / 100))]
print("percentile ofi_ratio CU SEMN:")
for p in (1, 5, 10, 25, 50, 75, 90, 95, 97, 98, 99, 99.5):
    print(f"   p{p:<5} = {pct(p):+.4f}")
print(f"   min = {vals[0]:+.4f}   max = {vals[-1]:+.4f}")

# De ce doar 8 candidati daca 419 bare par sa treaca de 0.25?
peste = [r for r in elig if float(r["ofi_ratio"]) >= 0.25]
print(f"\nbare eligibile cu ofi_ratio>=0.25: {len(peste)}")
print("   motivele lor:", dict(collections.Counter(r["motiv"] for r in peste)))
ex = [r for r in peste if r["motiv"] != "candidat"][:5]
for r in ex:
    print("   ex:", {k: r[k] for k in ("timestamp", "simbol", "quotes", "ofi_ratio", "motiv")})

print("\nrandament pe prag (bare eligibile, 8 zile de colectare):")
for t in (0.02, 0.03, 0.04, 0.05, 0.06, 0.08, 0.10, 0.15, 0.25):
    c = sum(1 for v in vals if v >= t)
    print(f"   prag {t:<5} -> {c:6d} bare ({100*c/n:5.2f}%), ~{c/8:6.0f}/zi")
