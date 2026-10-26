# diff_page.py — run from RAG\ : python scraper\diff_page.py <old.json> <new.json> <url>
import json, sys
old, new = (json.load(open(f, encoding="utf-8")) for f in sys.argv[1:3])
url = sys.argv[3]
g = lambda pages: next(p["content"] for p in pages if p["source_url"] == url and not p.get("dropdown_url"))
o, n = g(old), g(new)
print("=== NEW content (%d chars) ===\n%s\n" % (len(n), n[:2500]))
ns = set(n.splitlines())
gone = [l for l in o.splitlines() if l.strip() and l not in ns]
print("=== lines in OLD not in NEW: %d lines, %d chars ===" % (len(gone), sum(map(len, gone))))
for l in gone[:25]:
    print("-", l[:130])