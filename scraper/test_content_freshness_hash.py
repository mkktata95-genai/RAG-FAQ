"""
Validates the content_freshness_httpV1.py hash chain BEFORE the full
pipeline is built, using a real Royal London page (user-supplied) plus a
hand-edited duplicate. Fully offline — no network, no live site touched.

Run: python3 test_content_freshness_hash.py
"""

import content_freshness_httpV1 as m

REAL_HTML = "scraper/tests/fixtures/real_page.html"
EDITED_HTML = "scraper/tests/fixtures/real_page_edited.html"

passed = 0
failed = 0


def check(label, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}  {detail}")


def load(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


print("=" * 60)
print("1. Determinism — same input, hashed twice, must match")
print("=" * 60)
html = load(REAL_HTML)
hash_a = m.hash_from_raw_html(html)
hash_b = m.hash_from_raw_html(html)
check("hash is a 64-char sha256 hex digest", len(hash_a) == 64)
check("same HTML -> same hash on repeat run", hash_a == hash_b,
      f"{hash_a} != {hash_b}")

print()
print("=" * 60)
print("2. Real content change -> hash must flip")
print("=" * 60)
edited_html = load(EDITED_HTML)
hash_edited = m.hash_from_raw_html(edited_html)
check("edited page produces a different hash", hash_edited != hash_a,
      f"both hashed to {hash_a} — edit did not register")

print()
print("=" * 60)
print("3. Two-stage clean matters (regression guard for the v1.7.9-class bug)")
print("=" * 60)
main_html = m.extract_main_html(html)
md = m.html_fragment_to_markdown(main_html)
scraped_clean = m.clean_scraped_content(md)
single_clean_hash = m.compute_content_hash(scraped_clean)
double_clean_hash = m.hash_from_raw_html(html)
check(
    "hashing after only clean_scraped_content() differs from the real "
    "(double-clean) chain — proves the second clean_content() pass is "
    "not a no-op, so skipping it would silently produce the wrong hash",
    single_clean_hash != double_clean_hash,
)

print()
print("=" * 60)
print("4. Whitespace-only difference must NOT cause a false 'changed'")
print("=" * 60)
noisy_html = html.replace(
    "Compounding can be a powerful way to grow your wealth over time.",
    "Compounding can be a powerful way to grow  your wealth over time.",  # double space only
)
hash_noisy = m.hash_from_raw_html(noisy_html)
check("double-space-only diff collapses to the same hash (no false positive)",
      hash_noisy == hash_a, f"{hash_noisy} != {hash_a}")

print()
print("=" * 60)
print(f"RESULT: {passed} passed, {failed} failed")
print("=" * 60)

if failed:
    raise SystemExit(1)