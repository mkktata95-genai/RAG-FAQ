"""Measure how much text each cleanup rule removes from every fixture page (diagnostic only, changes nothing).
Run from the folder containing content_freshness_httpV1.py:
  python measure_cleanup_loss.py --fixtures <fixtures_base> [--out cleanup_loss.csv]
Rules are replayed in the same order as clean_scraped_content(); the final text is checked against the real function."""
import argparse, csv, logging, re, sys
from pathlib import Path
sys.path.insert(0, ".")
import content_freshness_httpV1 as cf

M, D, I = re.MULTILINE, re.DOTALL, re.IGNORECASE
RULES = [  # (name, pattern, flags, replacement) - mirrors clean_scraped_content()
    ("breadcrumb_block", r'(?:^\s*\d+\.\s*\[.*?\]\(.*?\)\s*>\s*$\s*)+^\s*\d+\.\s+[A-Z][^\n]{3,60}$', M),
    ("numbered_link_arrow", r'^\s*\d+\.\s*\[.*?\]\(.*?\)\s*>\s*$', M),
    ("share_block", r'Share\s*\n(\s*\*\s*(\[?\s*\]?\([^\)]*\))?\s*\n)+', 0),
    ("empty_bullet_link", r'^\s*\*\s*\[?\s*\]?\(\s*[^\)]{0,10}\)\s*$', M),
    ("share_line", r'^Share\s*$', M),
    ("tweet_link", r'\[?\s*\]?\(https://twitter\.com/intent/tweet[^\)]*\)\s*', 0),
    ("social_urls", r'https://www\.(facebook|instagram|linkedin|x|youtube|twitter)\.com/\S+', 0),
    ("empty_link", r'\[\s*\]\(\s*\)', 0),
    ("empty_bullet", r'^\s*\*\s*\[\s*\]\s*$', M),
    ("prev_next_item", r'^(Previous Item|Next Item)\s*$', M | I),
    ("browser_not_supported", r'Your browser is not supported\..*?×\s*', D),
    ("CUT_RL_mutual_insurance", r'\*\*The Royal London Mutual Insurance.*$', D | M),
    ("back_to_top", r'\[Back to top\].*?\n', 0),
]

def measure(md):
    out = {}
    before = len(md)
    t = cf._remove_duplicate_content(md)
    out["CUT_duplicate_h1_dedupe"] = before - len(t)
    for name, pat, fl in RULES:
        n = len(t); t = re.sub(pat, "", t, flags=fl); out[name] = n - len(t)
    return out, before, t

ap = argparse.ArgumentParser()
ap.add_argument("--fixtures", required=True); ap.add_argument("--out", default="cleanup_loss.csv")
a = ap.parse_args()
logging.disable(logging.CRITICAL)
names = ["CUT_duplicate_h1_dedupe"] + [r[0] for r in RULES]
rows, mismatch = [], 0
for f in sorted(Path(a.fixtures).glob("*.html")):
    md = cf.html_fragment_to_markdown(cf.extract_main_html(f.read_text(encoding="utf-8", errors="replace")))
    loss, n0, t = measure(md)
    # small whitespace-only tidy after rules is ignored; compare content ignoring whitespace
    if re.sub(r"\s+", "", t) != re.sub(r"\s+", "", cf.clean_scraped_content(md)):
        mismatch += 1
    total = sum(loss.values())
    rows.append({"page": f.stem, "chars_before": n0, "chars_removed": total,
                 "pct_removed": round(100 * total / n0, 1) if n0 else 0, **loss})

with open(a.out, "w", newline="", encoding="utf-8") as fh:
    w = csv.DictWriter(fh, fieldnames=["page", "chars_before", "chars_removed", "pct_removed"] + names)
    w.writeheader(); w.writerows(sorted(rows, key=lambda r: -r["pct_removed"]))

print(f"{len(rows)} pages | replay-vs-real mismatches: {mismatch}\n")
print(f"{'rule':32} {'pages hit':>9} {'chars removed':>14} {'worst page %':>12}")
for n in names:
    hit = [r for r in rows if r[n] > 0]
    if hit:
        worst = max(100 * r[n] / r["chars_before"] for r in hit if r["chars_before"])
        print(f"{n:32} {len(hit):9d} {sum(r[n] for r in hit):14d} {worst:11.1f}%")
print("\nTop 15 pages by % removed:")
for r in sorted(rows, key=lambda r: -r["pct_removed"])[:15]:
    top = max(names, key=lambda n: r[n])
    print(f"{r['pct_removed']:5.1f}%  {r['chars_removed']:6d}/{r['chars_before']:<6d} {top:26} {r['page'][-70:]}")
print(f"\nFull table: {a.out}")