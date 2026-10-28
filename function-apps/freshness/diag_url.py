"""Diagnose a 'changed' URL: fetch it 3 times, compare hashes with each other and with the index."""
import difflib, json, os, sys, tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.environ.setdefault("FRESHNESS_WORK_DIR", tempfile.mkdtemp(prefix="diag_"))
for k, v in json.load(open("local.settings.json"))["Values"].items():
    os.environ.setdefault(k, v)
sys.path.insert(0, "src")
import content_freshness_httpV1 as m

url = sys.argv[1]
entry = {"url": url, "title": "", "excel_category": ""}
pages = [m.scrape_url_for_freshness(entry) for _ in range(3)]
fresh = [p[0] for p in pages if p]
hashes = [p["content_hash"] for p in fresh]
print("fresh hashes :", hashes)
state = m.fetch_current_state_from_index()
idx = state.get(m.normalise_url(m.get_base_url(url)))
print("indexed hash :", idx and idx["content_hash"])
print("verdict      :", "UNSTABLE (differs between fetches)" if len(set(hashes)) > 1
      else "STABLE and " + ("SAME as index" if idx and hashes[0] == idx["content_hash"] else "DIFFERENT from index"))

def show(a, b, na, nb):
    d = list(difflib.unified_diff(a.splitlines(), b.splitlines(), na, nb, lineterm="", n=0))
    print("\n".join(d[:30]) or "(no line-level difference)")

if len(set(hashes)) > 1:
    show(fresh[0]["content"], next(p["content"] for p in fresh if p["content_hash"] != hashes[0]), "fetch 1", "fetch 2")
elif idx:
    r = m.get_search_client().search("*", filter=f"source_url eq '{idx['source_url']}' and chunk_index eq 0",
                                     select=["content"], top=1)
    old = next(iter(r), {}).get("content", "")
    show(old, fresh[0]["content"][:len(old)], "indexed chunk 0", "fresh (same length)")