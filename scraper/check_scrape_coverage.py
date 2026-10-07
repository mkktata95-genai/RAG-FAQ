"""Which Excel URLs are missing from a scrape JSON?  Usage:
   python check_scrape_coverage.py <approved_urls.xlsx> [scraped.json]   (default: newest scraper/data/*_httpV1_*.json)"""
import sys, json, glob, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scrape_approved_urls_httpV1 import load_approved_pages, normalize_url

excel = sys.argv[1]
files = [f for f in glob.glob("scraper/data/*_httpV1_*.json") if not f.endswith("_failures.json")]
jpath = sys.argv[2] if len(sys.argv) > 2 else max(files, key=os.path.getmtime)
want = {p["url"] for p in load_approved_pages(excel)}
got = {normalize_url(e["source_url"]) for e in json.load(open(jpath, encoding="utf-8"))}
missing = sorted(want - got)
print(f"Excel URLs: {len(want)} | scraped distinct pages: {len(got & want)} | JSON file: {jpath}")
print(f"Missing from JSON: {len(missing)}")
for u in missing: print("  -", u)
extra = sorted(got - want)
if extra: print(f"In JSON but not in Excel: {len(extra)}"); [print("  +", u) for u in extra]