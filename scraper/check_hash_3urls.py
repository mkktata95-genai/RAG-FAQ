"""
Hash diagnostic: runs on N base pages from the scraped JSON.
Place next to content_freshness_httpV1.py and run:
    python check_hash_3urls.py --file scraper/data/<file_used_for_index>.json
Omit --file to auto-pick the newest scraped JSON.
"""
import argparse
import difflib
import json
from pathlib import Path

import content_freshness_httpV1 as cf

parser = argparse.ArgumentParser()
parser.add_argument("--file", default=None)
parser.add_argument("--n", type=int, default=3)
parser.add_argument("--urls", nargs="*", default=None, help="Specific source_urls to test")
args = parser.parse_args()

if args.file:
    path = Path(args.file)
else:
    cands = [p for p in Path("scraper/data").glob("royal_london_faq_approved_*.json")
             if not p.name.endswith("_failures.json")]
    path = max(cands, key=lambda p: p.stat().st_mtime)
print(f"Using scraped file: {path}")
print(f"Using index:        {cf.INDEX_NAME}\n")

pages = json.load(open(path, encoding="utf-8"))
if args.urls:
    wanted = {cf.normalize_url(u) for u in args.urls}
    base_pages = [p for p in pages if not p.get("dropdown_url") and cf.normalize_url(p["source_url"]) in wanted]
else:
    base_pages = [p for p in pages if not p.get("dropdown_url")][: args.n]
h = cf.compute_content_hash


def fresh_text(url):
    html, status, err = cf.fetch_html(url)
    if err or not html:
        raise RuntimeError(f"fetch failed: {status} {err}")
    md = cf.html_fragment_to_markdown(cf.extract_main_html(html))
    return cf.clean_content(cf.clean_scraped_content(md))


def index_hash(url):
    try:
        r = list(cf.get_search_client().search(
            search_text="*", filter=f"source_url eq '{url}'",
            select=["content_hash"], top=1))
        return r[0]["content_hash"] if r else None
    except Exception as e:
        return f"lookup failed: {e}"


def show_diff(x, y, label):
    print(f"   --- diff {label} (first 25 lines) ---")
    d = difflib.unified_diff(x.splitlines(), y.splitlines(), lineterm="", n=0)
    for i, line in enumerate(d):
        if i >= 25:
            break
        print("   " + line)


for p in base_pages:
    url = p["source_url"]
    print("=" * 70)
    print(url)
    try:
        a, b = fresh_text(url), fresh_text(url)
    except Exception as e:
        print("   ERROR:", e)
        continue
    j = cf.clean_content(p["content"].strip())
    print("   fresh1 == fresh2 (page stable):          ", h(a) == h(b))
    print("   JSON   == fresh1 (scrape vs freshness):  ", h(j) == h(a))
    ih = index_hash(url)
    print("   index hash == JSON hash:                 ", ih == h(j))
    print("   index hash == fresh1 hash:               ", ih == h(a))
    if h(a) != h(b):
        show_diff(a, b, "fresh1 vs fresh2")
    elif h(j) != h(a):
        show_diff(j, a, "JSON vs fresh")