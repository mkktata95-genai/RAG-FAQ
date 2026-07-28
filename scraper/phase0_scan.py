"""
phase0_scan.py  v1.0.0
======================
Phase 0 — Extended page structure audit.

Three checks in one pass over ALL pages in the scraper JSON:

  1. RANDOM OK SAMPLE (~30 pages, est_chunks < 50)
     - Confirms whether |pipe| markdown table detection holds on "clean" pages
     - Flags any OK page that actually has significant tables (hidden complexity)

  2. TABLE INTEGRITY CHECK (all 364 pages)
     - Simulates flat RecursiveCharacterTextSplitter at CHUNK_SIZE=1600
     - Detects any table that would be split mid-body across chunk boundaries
     - Reports: url, table #, rows split, severity

  3. NON-PIPE TABLE DETECTION (all pages)
     - Looks for HTML table artefacts crawl4ai may NOT convert to | markdown:
         - Raw <table>/<tr>/<td> tags surviving in markdown output
         - Dense whitespace-aligned columns (heuristic: 3+ columns, consistent spacing)
     - These would be INVISIBLE to the current element detector

OUTPUTS
-------
  - Console: per-check summary
  - phase0_report.json: machine-readable results for go/no-go decision

USAGE
-----
  python phase0_scan.py --json scraper/data/<file>.json
  python phase0_scan.py --json scraper/data/<file>.json --sample 30 --seed 42

CHANGELOG
---------
v1.0.0 — Initial Phase 0 scanner. Three-check audit: random OK sample,
          table integrity, non-pipe table detection. JSON report output.
"""

import argparse
import json
import random
import re
import sys
from pathlib import Path

# ── Constants ─────────────────────────────────────────────────────────────────

CHUNK_SIZE    = 1600
CHUNK_OVERLAP = 200
EFFECTIVE     = CHUNK_SIZE - CHUNK_OVERLAP
OVERSIZED_THRESHOLD = 50   # est_chunks >= this → "oversized"
DEFAULT_SAMPLE      = 30
DEFAULT_SEED        = 42
TABLE_ROWS_PER_CHUNK = 30  # agreed atomic table chunk size


# ── Reuse core detector from analyse_page_structure.py ────────────────────────

def parse_table_block(lines: list) -> dict:
    header_row = None
    separator_row = None
    data_rows = []
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if all(re.match(r'^[-:]+$', c) for c in cells if c):
            separator_row = cells
        elif header_row is None and separator_row is None:
            header_row = cells
        else:
            data_rows.append(cells)
    all_rows = [r for r in [header_row, separator_row] + data_rows if r]
    col_count = max((len(r) for r in all_rows), default=0)
    return {
        "has_header":    header_row is not None,
        "has_separator": separator_row is not None,
        "col_count":     col_count,
        "data_rows":     len(data_rows),
        "total_lines":   len(lines),
        "header":        header_row,
    }


def analyse_content(content: str) -> list:
    """Return list of typed element dicts."""
    lines = content.splitlines()
    elements = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith("```"):
            block = [line]; i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                block.append(lines[i]); i += 1
            if i < len(lines):
                block.append(lines[i]); i += 1
            elements.append({"type": "code_block", "lines": block,
                             "char_count": sum(len(l) for l in block)})
            continue

        if stripped.startswith("|") and "|" in stripped[1:]:
            block = [line]; i += 1
            while i < len(lines) and lines[i].strip().startswith("|"):
                block.append(lines[i]); i += 1
            parsed = parse_table_block(block)
            elements.append({"type": "table", "lines": block,
                             "char_count": sum(len(l) for l in block), **parsed})
            continue

        if re.match(r'^#{1,4}\s', stripped):
            elements.append({"type": "header", "lines": [line],
                             "char_count": len(line),
                             "level": len(re.match(r'^(#+)', stripped).group(1)),
                             "text": stripped.lstrip("#").strip()})
            i += 1; continue

        if re.match(r'^[-*+]\s|^\d+\.\s', stripped):
            block = [line]; i += 1
            while i < len(lines) and re.match(r'^[-*+]\s|^\d+\.\s|^\s+',
                                               lines[i]) and lines[i].strip():
                block.append(lines[i]); i += 1
            elements.append({"type": "list", "lines": block,
                             "char_count": sum(len(l) for l in block)})
            continue

        if not stripped:
            elements.append({"type": "blank", "lines": [line], "char_count": 0})
            i += 1; continue

        block = [line]; i += 1
        while i < len(lines):
            ns = lines[i].strip()
            if (ns.startswith("|") or ns.startswith("#") or
                    ns.startswith("```") or re.match(r'^[-*+]\s|^\d+\.\s', ns)
                    or not ns):
                break
            block.append(lines[i]); i += 1
        elements.append({"type": "prose", "lines": block,
                         "char_count": sum(len(l) for l in block)})

    return elements


def est_chunks(content_len: int) -> int:
    return max(1, (content_len - CHUNK_OVERLAP) // EFFECTIVE)


# ── Check 1: Random OK sample ─────────────────────────────────────────────────

def check_ok_sample(pages: list, sample_n: int, seed: int) -> dict:
    """
    Sample ~sample_n OK pages (est_chunks < OVERSIZED_THRESHOLD).
    For each: run element detection, flag if tables found.
    Returns summary + per-page results.
    """
    ok_pages = [p for p in pages if est_chunks(len(p.get("content", ""))) < OVERSIZED_THRESHOLD]

    rng = random.Random(seed)
    sample = rng.sample(ok_pages, min(sample_n, len(ok_pages)))

    results = []
    tables_detected   = 0
    no_tables         = 0
    hidden_complexity = []  # OK pages with significant tables

    for page in sample:
        content  = page.get("content", "")
        elements = analyse_content(content)
        tables   = [e for e in elements if e["type"] == "table"]
        total_rows = sum(t["data_rows"] for t in tables)

        has_pipe_table = len(tables) > 0
        if has_pipe_table:
            tables_detected += 1
        else:
            no_tables += 1

        # Flag if table content is substantial (>5 rows) — hidden complexity
        if total_rows > 5:
            hidden_complexity.append({
                "url":        page["url"],
                "tables":     len(tables),
                "total_rows": total_rows,
                "est_chunks": est_chunks(len(content)),
            })

        results.append({
            "url":           page["url"],
            "content_len":   len(content),
            "est_chunks":    est_chunks(len(content)),
            "pipe_tables":   len(tables),
            "total_rows":    total_rows,
        })

    pipe_detection_rate = tables_detected / len(sample) * 100 if sample else 0

    return {
        "ok_pages_total":       len(ok_pages),
        "sample_size":          len(sample),
        "with_pipe_tables":     tables_detected,
        "without_pipe_tables":  no_tables,
        "pipe_detection_rate_pct": round(pipe_detection_rate, 1),
        "hidden_complexity":    hidden_complexity,
        "per_page":             results,
    }


# ── Check 2: Table integrity (mid-table split detection) ──────────────────────

def simulate_flat_chunks(content: str) -> list:
    """
    Simulate flat RecursiveCharacterTextSplitter: fixed-size character windows.
    Returns list of (start_char, end_char) tuples.
    """
    chunks = []
    start = 0
    length = len(content)
    while start < length:
        end = min(start + CHUNK_SIZE, length)
        chunks.append((start, end))
        start += EFFECTIVE
    return chunks


def check_table_integrity(pages: list) -> dict:
    """
    For every page, simulate flat chunking and detect tables split mid-body.
    A split occurs when a chunk boundary falls inside a table's content
    (between its first and last line).
    """
    split_violations = []
    clean_pages      = 0
    total_tables_checked = 0

    for page in pages:
        content  = page.get("content", "")
        if not content:
            continue

        elements = analyse_content(content)
        tables   = [e for e in elements if e["type"] == "table"]
        if not tables:
            clean_pages += 1
            continue

        total_tables_checked += len(tables)

        # Build char offset for each table
        lines    = content.splitlines(keepends=True)
        line_offsets = []
        pos = 0
        for line in lines:
            line_offsets.append(pos)
            pos += len(line)

        # Map each element to its char range
        elem_offsets = []
        line_idx = 0
        for elem in elements:
            n = len(elem["lines"])
            start_char = line_offsets[line_idx] if line_idx < len(line_offsets) else len(content)
            end_line   = line_idx + n - 1
            end_char   = (line_offsets[end_line] + len(lines[end_line])
                          if end_line < len(lines) else len(content))
            elem_offsets.append((start_char, end_char, elem))
            line_idx += n

        chunks     = simulate_flat_chunks(content)
        page_splits = []

        for t_idx, (t_start, t_end, t_elem) in enumerate(
                (o for o in elem_offsets if o[2]["type"] == "table"), 0):
            for c_idx, (c_start, c_end) in enumerate(chunks):
                # Chunk boundary falls inside the table
                if t_start < c_end < t_end:
                    rows_before = max(0, (c_end - t_start) // max(1, (t_end - t_start)) *
                                      t_elem.get("data_rows", 0))
                    page_splits.append({
                        "table_idx":    t_idx + 1,
                        "table_rows":   t_elem.get("data_rows", 0),
                        "table_chars":  t_elem.get("char_count", 0),
                        "chunk_idx":    c_idx,
                        "split_at_char": c_end,
                        "severity":     "HIGH" if t_elem.get("data_rows", 0) > 10 else "LOW",
                    })

        if page_splits:
            split_violations.append({
                "url":       page["url"],
                "content_len": len(content),
                "est_chunks":  est_chunks(len(content)),
                "splits":    page_splits,
            })
        else:
            clean_pages += 1

    return {
        "total_pages":          len(pages),
        "total_tables_checked": total_tables_checked,
        "pages_with_splits":    len(split_violations),
        "clean_pages":          clean_pages,
        "violations":           split_violations,
    }


# ── Check 3: Non-pipe table detection ─────────────────────────────────────────

# Patterns that suggest a table crawl4ai failed to convert to pipe-markdown
_HTML_TABLE_RE   = re.compile(r'<(table|tr|td|th)\b', re.IGNORECASE)
_ALIGNED_ROW_RE  = re.compile(r'(\S+\s{3,}\S+\s{3,}\S+)')  # 3+ cols, wide gaps


def check_nonpipe_tables(pages: list) -> dict:
    """
    Detect pages where crawl4ai may have left table content as HTML tags
    or as whitespace-aligned columns rather than |pipe| markdown.
    """
    html_table_pages    = []
    aligned_col_pages   = []

    for page in pages:
        content = page.get("content", "")
        if not content:
            continue

        # Check 1: raw HTML table tags in output
        html_matches = _HTML_TABLE_RE.findall(content)
        if html_matches:
            html_table_pages.append({
                "url":     page["url"],
                "matches": len(html_matches),
                "sample":  content[content.lower().find("<table"):][:200]
                           if "<table" in content.lower() else "",
            })

        # Check 2: whitespace-aligned multi-column rows (3+ consistent gaps)
        aligned_lines = [l for l in content.splitlines()
                         if _ALIGNED_ROW_RE.search(l) and len(l) > 40]
        if len(aligned_lines) >= 5:  # 5+ such lines = likely a table
            aligned_col_pages.append({
                "url":          page["url"],
                "aligned_lines": len(aligned_lines),
                "sample":        aligned_lines[0][:120] if aligned_lines else "",
            })

    return {
        "html_table_leakage":  {
            "count": len(html_table_pages),
            "pages": html_table_pages,
        },
        "whitespace_aligned_tables": {
            "count": len(aligned_col_pages),
            "pages": aligned_col_pages,
        },
        "total_risk_pages": len(set(
            [p["url"] for p in html_table_pages] +
            [p["url"] for p in aligned_col_pages]
        )),
    }


# ── Console reporter ───────────────────────────────────────────────────────────

SEP  = "=" * 70
SEP2 = "-" * 70


def print_check1(r: dict):
    print(SEP)
    print("  CHECK 1 — RANDOM OK PAGE SAMPLE (pipe-markdown table detection)")
    print(SEP)
    print(f"  OK pages in dataset : {r['ok_pages_total']}")
    print(f"  Sample size         : {r['sample_size']}")
    print(f"  With |pipe| tables  : {r['with_pipe_tables']}")
    print(f"  Without tables      : {r['without_pipe_tables']}")
    print(f"  Pipe detection rate : {r['pipe_detection_rate_pct']}%")
    if r["hidden_complexity"]:
        print(f"\n  ⚠  HIDDEN COMPLEXITY ({len(r['hidden_complexity'])} OK pages with >5 table rows):")
        for p in r["hidden_complexity"]:
            print(f"     [{p['est_chunks']:>3} chunks | {p['tables']} tables | "
                  f"{p['total_rows']} rows] {p['url']}")
    else:
        print("\n  ✓  No hidden table complexity in OK pages")
    print()


def print_check2(r: dict):
    print(SEP)
    print("  CHECK 2 — TABLE INTEGRITY (mid-table split detection, all pages)")
    print(SEP)
    print(f"  Total pages checked      : {r['total_pages']}")
    print(f"  Total tables inspected   : {r['total_tables_checked']}")
    print(f"  Pages with split tables  : {r['pages_with_splits']}")
    print(f"  Clean pages              : {r['clean_pages']}")
    if r["violations"]:
        print(f"\n  VIOLATIONS (top 10):")
        for v in r["violations"][:10]:
            high = sum(1 for s in v["splits"] if s["severity"] == "HIGH")
            print(f"     [{v['est_chunks']:>4} chunks | {len(v['splits'])} splits"
                  f" ({high} HIGH)] {v['url']}")
    else:
        print("\n  ✓  No mid-table splits detected under flat chunking")
    print()


def print_check3(r: dict):
    print(SEP)
    print("  CHECK 3 — NON-PIPE TABLE DETECTION (all pages)")
    print(SEP)
    print(f"  HTML table tag leakage : {r['html_table_leakage']['count']} pages")
    print(f"  Whitespace-aligned     : {r['whitespace_aligned_tables']['count']} pages")
    print(f"  Total at-risk pages    : {r['total_risk_pages']}")
    if r["html_table_leakage"]["pages"]:
        print("\n  HTML LEAKAGE pages (top 5):")
        for p in r["html_table_leakage"]["pages"][:5]:
            print(f"     [{p['matches']} tags] {p['url']}")
    if r["whitespace_aligned_tables"]["pages"]:
        print("\n  WHITESPACE-ALIGNED pages (top 5):")
        for p in r["whitespace_aligned_tables"]["pages"][:5]:
            print(f"     [{p['aligned_lines']} lines] {p['url']}")
    if r["total_risk_pages"] == 0:
        print("\n  ✓  No non-pipe table risk detected")
    print()


def print_verdict(c1: dict, c2: dict, c3: dict):
    print(SEP)
    print("  PHASE 0 VERDICT")
    print(SEP)

    issues = []
    if c1["pipe_detection_rate_pct"] < 80:
        issues.append(f"  ✗ Pipe detection rate low ({c1['pipe_detection_rate_pct']}%) "
                      f"— element detector may miss tables on OK pages")
    if c1["hidden_complexity"]:
        issues.append(f"  ✗ {len(c1['hidden_complexity'])} OK pages have hidden table complexity "
                      f"— oversized threshold may be too low")
    if c2["pages_with_splits"] > 0:
        issues.append(f"  ✗ {c2['pages_with_splits']} pages have mid-table splits under flat "
                      f"chunking — element-aware fix is confirmed necessary")
    if c3["total_risk_pages"] > 0:
        issues.append(f"  ✗ {c3['total_risk_pages']} pages may have non-pipe tables — "
                      f"scraper output needs manual spot-check")

    if not issues:
        print("  ✓  ALL CHECKS PASSED — safe to proceed to Phase 1 chunker build")
    else:
        print("  ISSUES FOUND — resolve before Phase 1:")
        for issue in issues:
            print(issue)
    print(SEP)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Phase 0 page structure audit")
    parser.add_argument("--json",   required=True,  help="Path to scraper JSON")
    parser.add_argument("--sample", type=int, default=DEFAULT_SAMPLE,
                        help=f"OK pages to sample (default: {DEFAULT_SAMPLE})")
    parser.add_argument("--seed",   type=int, default=DEFAULT_SEED,
                        help=f"Random seed (default: {DEFAULT_SEED})")
    parser.add_argument("--out",    default="phase0_report.json",
                        help="Output JSON report path (default: phase0_report.json)")
    args = parser.parse_args()

    json_path = Path(args.json)
    if not json_path.exists():
        print(f"ERROR: {json_path} not found", file=sys.stderr)
        sys.exit(1)

    print(f"Loading {json_path} ...")
    data = json.loads(json_path.read_text(encoding="utf-8"))

    # Exclude dropdown-state entries
    pages = [d for d in data if not d.get("dropdown_state", "")]
    print(f"Pages loaded: {len(pages)} (after excluding dropdown entries)\n")

    # Run checks
    print("Running Check 1 — OK page sample ...")
    c1 = check_ok_sample(pages, args.sample, args.seed)

    print("Running Check 2 — Table integrity ...")
    c2 = check_table_integrity(pages)

    print("Running Check 3 — Non-pipe table detection ...")
    c3 = check_nonpipe_tables(pages)

    print()

    # Console output
    print_check1(c1)
    print_check2(c2)
    print_check3(c3)
    print_verdict(c1, c2, c3)

    # JSON report
    report = {
        "json_source":   str(json_path),
        "pages_total":   len(pages),
        "check1_ok_sample":       c1,
        "check2_table_integrity": c2,
        "check3_nonpipe_tables":  c3,
    }
    out_path = Path(args.out)
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nReport saved → {out_path}")


if __name__ == "__main__":
    main()