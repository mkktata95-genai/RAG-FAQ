"""
Schema Type Audit — v3
Checks field assignments in scraper, indexer, freshness against Azure AI Search schema.
Excludes run_pipeline() return dicts (internal counters, not uploaded to Azure).

CHANGELOG
---------
v3 — Fixed two bugs found during v5 rebuild rollout:
      1. base path was hardcoded to "/mnt/project" (a sandbox-only
         path) — every file lookup silently failed on VDI/Windows.
         Now resolves relative to this script's own location via
         Path(__file__).parent, so it works regardless of cwd.
      2. "All clear" was printed whenever ISSUES was empty — which
         is also true when ALL files failed to be found (zero
         fields ever checked). Script now aborts loudly instead of
         reporting false success when any file is missing.
      Also: FILES dict updated to the renamed v5/V1 filenames, and
      SCHEMA extended with parent_url (v4.8.0) and element_type
      (v5.10.0) — both were missing from v2's coverage.
v2 — Original two-bug-affected version (superseded).
"""
import re
from pathlib import Path

SCHEMA = {
    "chunk_id":            (str,   "Edm.String"),
    "content":             (str,   "Edm.String"),
    "augmented_questions": (str,   "Edm.String"),
    "title_questions":     (str,   "Edm.String"),
    "title":               (str,   "Edm.String"),
    "source_url":          (str,   "Edm.String"),
    "section":             (str,   "Edm.String"),
    "audience":            (str,   "Edm.String"),
    "scraped_at":          (str,   "Edm.String"),
    "chunk_index":         (int,   "Edm.Int32"),
    "total_chunks":        (int,   "Edm.Int32"),
    "content_hash":        (str,   "Edm.String"),
    "pipeline_version":    (str,   "Edm.String"),
    "index_run_id":        (str,   "Edm.String"),
    "indexed_at":          (str,   "Edm.String"),
    "scraper_version":     (str,   "Edm.String"),
    "metadata_version":    (str,   "Edm.String"),
    "scrape_run_id":       (str,   "Edm.String"),
    "refresh_count":       (int,   "Edm.Int32"),
    "has_video":           (bool,  "Edm.Boolean"),
    "content_type":        (str,   "Edm.String"),
    "product_category":    (str,   "Edm.String"),
    "description":         (str,   "Edm.String"),
    "thumbnail_url":       (str,   "Edm.String"),
    "publish_date":        (str,   "Edm.String"),
    "collection_name":     (str,   "Edm.String"),
    "read_time_mins":      (str,   "Edm.String"),
    "parent_url":          (str,   "Edm.String"),   # v4.8.0 — clean citation URL for dropdown_state chunks
    "element_type":        (str,   "Edm.String"),   # v5.10.0 — "prose" | "table" | "dropdown_state"
}

# Functions whose return dicts are internal (not uploaded to Azure Search)
EXCLUDED_FUNCTIONS = {"run_pipeline", "main"}

FILES = {
    "scraper":   "scrape_approved_urls_updatedV5.py",
    "indexer":   "chunk_and_index_hqaV5.py",
    "freshness": "content_freshnessV1.py",
}

ISSUES = []

# Classify the Python type a field-assignment expression will produce at runtime.
def classify_expr(expr):
    """Classify the Python type a field-assignment expression will produce at runtime."""
    expr = expr.strip()
    if expr.startswith('"') or expr.startswith("'") or expr.startswith('f"') or expr.startswith("f'"):
        return str
    if expr.startswith("str("):
        return str
    if expr in ("True", "False"):
        return bool
    if expr == "0" or expr == "1":
        return int
    try:
        int(expr)
        return int
    except (ValueError, TypeError):
        pass
    if expr.startswith("max(") and "str(" not in expr:
        return int
    if expr.startswith("int("):
        return int
    if expr.startswith("round("):
        return int
    if expr.startswith("bool("):
        return bool
    m = re.match(r'(?:page|metadata|base_page_data|chunk)\.get\(["\'][^"\']+["\'],\s*(.+?)\)\s*$', expr)
    if m:
        return classify_expr(m.group(1).strip())
    if re.match(r'str\(.+\)', expr):
        return str
    if "isoformat()" in expr or expr.startswith("datetime"):
        return str
    return None

# Scan one file for SCHEMA-field assignments and flag any type mismatches.
def audit_file(filepath, label):
    """Scan one file for SCHEMA-field assignments and flag any type mismatches."""
    src = Path(filepath).read_text(encoding="utf-8", errors="ignore")
    lines = src.splitlines()

    # Track current function to exclude run_pipeline/main return dicts
    current_func = None
    func_pattern = re.compile(r'^def (\w+)\(')
    field_pattern = re.compile(r'"([a-z_]+)"\s*:\s*(.+?)(?:,\s*(?:#.*)?)?$')

    for i, line in enumerate(lines, 1):
        stripped = line.strip()

        # Track function context
        fm = func_pattern.match(stripped)
        if fm:
            current_func = fm.group(1)

        # Skip comments, logs, prints, docstrings
        if (stripped.startswith("#") or stripped.startswith('"""') or
            stripped.startswith("log.") or stripped.startswith("print") or
            stripped.startswith("*")):
            continue

        # Skip if inside excluded functions
        if current_func in EXCLUDED_FUNCTIONS:
            continue

        m = field_pattern.search(stripped)
        if not m:
            continue

        field = m.group(1)
        val_expr = m.group(2).strip().rstrip(",").strip()

        if field not in SCHEMA:
            continue

        expected_type, edm = SCHEMA[field]
        inferred = classify_expr(val_expr)
        if inferred is None:
            continue

        if inferred != expected_type:
            ISSUES.append({
                "file": label, "line": i, "field": field,
                "expected": expected_type.__name__,
                "got": inferred.__name__,
                "edm": edm,
                "raw": stripped[:90]
            })

# v3: resolve relative to this script's own location, not a hardcoded
# sandbox path. Works from any cwd — RAG\, RAG\scraper\, wherever.
base = Path(__file__).parent

files_found = 0
for label, fname in FILES.items():
    fpath = base / fname
    if fpath.exists():
        audit_file(fpath, label)
        files_found += 1
    else:
        print(f"⚠️  Not found: {fpath}")

print("\n" + "="*70)
print("SCHEMA TYPE AUDIT REPORT v3")
print("="*70)

# v3: fail loudly if any file was missing — "no issues found" and
# "no files were ever read" must never look identical to the reader.
if files_found < len(FILES):
    print(f"\n🛑 ABORTED — only {files_found}/{len(FILES)} files found.")
    print("   Fix the paths above before trusting any result from this run.")
elif not ISSUES:
    print(f"\n✅ All clear — no type mismatches found across all {files_found} files.")
else:
    print(f"\n❌ {len(ISSUES)} type mismatch(es) found:\n")
    current_file = None
    for issue in ISSUES:
        if issue["file"] != current_file:
            current_file = issue["file"]
            print(f"\n── {current_file.upper()} ──────────────────────────")
        print(f"  Line {issue['line']:4d} | '{issue['field']}' → expected {issue['expected']} ({issue['edm']}), got {issue['got']}")
        print(f"           {issue['raw']}")

print("\n" + "="*70)
print(f"Schema fields audited: {len(SCHEMA)}")
print(f"Files found:           {files_found}/{len(FILES)}")
print(f"Files audited:         {', '.join(FILES.keys())}")
print("="*70)