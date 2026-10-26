import json, sys
old, new = (json.load(open(f, encoding="utf-8")) for f in sys.argv[1:3])
m = lambda pages: {p["source_url"]: len(p["content"]) for p in pages if not p.get("dropdown_url")}
o, n = m(old), m(new)
print(f"old={len(o)} new={len(n)} missing_in_new={sorted(set(o)-set(n))}")
rows = sorted(((n[u] - o[u]) / max(o[u], 1), u, o[u], n[u]) for u in o.keys() & n.keys())
for d, u, a, b in rows[:15]:
    print(f"{d:+.0%}  {a}->{b}  {u}")