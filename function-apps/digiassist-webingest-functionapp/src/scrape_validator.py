"""Post-scrape content validation. Plain functions, standard library only.

Used by scraper_job.py (Function App) and runnable by hand:
    python scrape_validator.py <new.json> [--prev <old.json>] [--csv issues.csv]
        [--min-chars 100] [--max-drop 30]      (exit code 1 if any ERROR)

Issues are (severity, url, variant, message) tuples.
ERROR: empty/very short content (unless equally short in prev) | no H1 and title not found in content
       | content shrank > max_drop % vs prev
WARN : short in both runs | page in prev but missing in new | identical content on different URLs
       | boilerplate leaked into content
"""
import argparse
import csv
import json
import re
import sys
from collections import defaultdict

# Add phrases here as you spot them leaking into scraped content.
BOILERPLATE = ["skip to main content", "accept all cookies", "cookie settings", "back to top",
               "javascript is disabled", "enable javascript"]


def load_pages(path) -> dict:
    """Scrape JSON (list, or {"pages": [...]}) -> {(url, variant): page}."""
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


def validate_pages(new: dict, prev: dict | None = None, min_chars: int = 100, max_drop: float = 30.0) -> list:
    prev = prev or {}
    issues = []

    def add(sev, key, msg):
        issues.append((sev, key[0], key[1], msg))

    by_text = defaultdict(list)
    for key, p in new.items():
        c = p.get("content") or ""
        variant = key[1]
        if len(c.strip()) < min_chars:
            if key in prev and len((prev[key].get("content") or "").strip()) < min_chars:
                add("WARN", key, f"short in both runs ({len(c.strip())} chars) - likely genuine (widget/short dropdown option)")
            else:
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
        by_text[re.sub(r"\s+", " ", c).strip()].append(key)
        if key in prev:
            old_len = len(prev[key].get("content") or "")
            if old_len and (old_len - len(c)) * 100 / old_len > max_drop:
                add("ERROR", key, f"content shrank {old_len} -> {len(c)} chars (> {max_drop:g}% drop vs prev)")
    for keys in by_text.values():
        if len({k[0] for k in keys}) > 1:
            add("WARN", keys[0], "identical content on " + ", ".join(sorted({k[0] for k in keys})))
    for key in prev:
        if key not in new:
            add("WARN", key, "present in prev scrape, missing in new (removed or scrape failed?)")
    return issues


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("new")
    ap.add_argument("--prev")
    ap.add_argument("--csv")
    ap.add_argument("--min-chars", type=int, default=100)
    ap.add_argument("--max-drop", type=float, default=30.0)
    a = ap.parse_args()

    new = load_pages(a.new)
    prev = load_pages(a.prev) if a.prev else {}
    issues = validate_pages(new, prev, a.min_chars, a.max_drop)
    errs = sum(i[0] == "ERROR" for i in issues)
    print(f"Validated {len(new)} pages" + (f" against {len(prev)} prev" if prev else "")
          + f": {errs} ERROR, {len(issues) - errs} WARN")
    for sev in ("ERROR", "WARN"):
        for s, url, variant, msg in issues:
            if s == sev:
                print(f"[{s}] {url}{' [' + variant + ']' if variant else ''}: {msg}")
    if a.csv:
        with open(a.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["severity", "url", "variant", "issue"])
            w.writerows(issues)
        print(f"CSV written to {a.csv}")
    if errs:
        print("RESULT: FAIL - do not index until the ERRORs are explained.")
        sys.exit(1)
    print("RESULT: PASS")


if __name__ == "__main__":
    main()
