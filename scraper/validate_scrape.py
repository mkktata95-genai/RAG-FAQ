# validate_scrape.py - post-scrape gate. Run BEFORE building/updating the index.
# Usage (from RAG\):
#   python scraper\validate_scrape.py <new.json> [--prev <old.json>] [--csv issues.csv]
#       [--min-chars 100] [--max-drop 30]
# Exit code 1 if any ERROR (use it to block indexing), 0 otherwise.
#
# ERROR: empty/very short content | no H1 and title not found in content | content shrank > --max-drop % vs prev
# WARN : page in prev but missing in new | identical content on different URLs | boilerplate leaked into content
import argparse, csv, json, re, sys
from collections import defaultdict

BOILERPLATE = ["skip to main content", "accept all cookies", "cookie settings", "back to top",
               "javascript is disabled", "enable javascript"]  # add more as you spot them


def load(path):
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    pages = d.get("pages", d) if isinstance(d, dict) else d
    out = {}
    for p in pages:
        url = p.get("source_url") or p.get("url")
        if not url:
            continue
        variant = p.get("dropdown_url") or p.get("dropdown_state") or p.get("dropdown_value") or ""
        out[(url, str(variant))] = p
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("new"); ap.add_argument("--prev"); ap.add_argument("--csv")
    ap.add_argument("--min-chars", type=int, default=100)
    ap.add_argument("--max-drop", type=float, default=30.0)
    a = ap.parse_args()

    new = load(a.new)
    prev = load(a.prev) if a.prev else {}
    issues = []  # (severity, url, variant, message)

    def add(sev, key, msg):
        issues.append((sev, key[0], key[1], msg))

    by_hash = defaultdict(list)
    for key, p in new.items():
        c = p.get("content") or ""
        url, variant = key
        if len(c.strip()) < a.min_chars:
            add("ERROR", key, f"content empty/too short ({len(c.strip())} chars)")
            continue
        if not variant:  # base pages only: dropdown variants have no H1 of their own
            title = (p.get("title") or "").strip().lower()
            has_h1 = re.search(r"^#\s+\S", c, re.M) is not None
            if not has_h1 and not (title and title in c.lower()):
                add("ERROR", key, "no H1 and page title not found in content (hero/heading dropped?)")
        low = c.lower()
        hits = [b for b in BOILERPLATE if b in low]
        if hits:
            add("WARN", key, "boilerplate in content: " + ", ".join(hits))
        by_hash[re.sub(r"\s+", " ", c).strip()].append(key)
        if key in prev:
            old_len = len(prev[key].get("content") or "")
            if old_len and (old_len - len(c)) * 100 / old_len > a.max_drop:
                add("ERROR", key, f"content shrank {old_len} -> {len(c)} chars "
                                  f"(> {a.max_drop:g}% drop vs prev)")
    for text, keys in by_hash.items():
        if len({k[0] for k in keys}) > 1:
            add("WARN", keys[0], "identical content on " + ", ".join(sorted({k[0] for k in keys})))
    for key in prev:
        if key not in new:
            add("WARN", key, "present in prev scrape, missing in new (removed or scrape failed?)")

    errs = sum(i[0] == "ERROR" for i in issues)
    warns = len(issues) - errs
    print(f"Validated {len(new)} pages" + (f" against {len(prev)} prev" if prev else "")
          + f": {errs} ERROR, {warns} WARN")
    for sev in ("ERROR", "WARN"):
        for s, url, variant, msg in issues:
            if s == sev:
                print(f"[{s}] {url}{' [' + variant + ']' if variant else ''}: {msg}")
    if a.csv:
        with open(a.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f); w.writerow(["severity", "url", "variant", "issue"]); w.writerows(issues)
        print(f"CSV written to {a.csv}")
    if errs:
        print("RESULT: FAIL - do not index until the ERRORs are explained.")
        sys.exit(1)
    print("RESULT: PASS")


if __name__ == "__main__":
    main()