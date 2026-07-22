"""
analyse_page_structure.py
==========================
Analyses the element structure of a specific page from the scraper JSON.
Identifies: prose sections, markdown tables (with accurate row/col counts),
headers, lists, code blocks.

USAGE:
    python analyse_page_structure.py
    python analyse_page_structure.py --url "fund-changes" --json scraper/data/<file>.json
"""
import argparse
import json
import re
from pathlib import Path


# ── Element detection ─────────────────────────────────────────

def parse_table_block(lines: list[str]) -> dict:
    """
    Parse a contiguous block of markdown table lines.
    Returns accurate row count (excl. header + separator),
    column count, and whether it has a separator row.
    """
    header_row = None
    separator_row = None
    data_rows = []

    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        # Separator row: all cells are dashes/colons
        if all(re.match(r'^[-:]+$', c) for c in cells if c):
            separator_row = cells
        elif header_row is None and separator_row is None:
            header_row = cells
        else:
            data_rows.append(cells)

    # Column count from widest row
    all_rows = [r for r in [header_row, separator_row] + data_rows if r]
    col_count = max((len(r) for r in all_rows), default=0)

    return {
        "has_header":   header_row is not None,
        "has_separator": separator_row is not None,
        "col_count":    col_count,
        "data_rows":    len(data_rows),
        "total_lines":  len(lines),
        "header":       header_row,
        "sample_rows":  data_rows[:3],
    }


def analyse_content(content: str) -> dict:
    """
    Segment content into typed elements and return statistics.
    Elements: header, prose, table, list, code_block, blank
    """
    lines = content.splitlines()
    elements = []
    i = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # ── Code block ───────────────────────────────────────
        if stripped.startswith("```"):
            block = [line]
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                block.append(lines[i])
                i += 1
            if i < len(lines):
                block.append(lines[i])
                i += 1
            elements.append({"type": "code_block", "lines": block,
                             "char_count": sum(len(l) for l in block)})
            continue

        # ── Markdown table ───────────────────────────────────
        if stripped.startswith("|") and "|" in stripped[1:]:
            block = [line]
            i += 1
            while i < len(lines) and lines[i].strip().startswith("|"):
                block.append(lines[i])
                i += 1
            parsed = parse_table_block(block)
            elements.append({
                "type":       "table",
                "lines":      block,
                "char_count": sum(len(l) for l in block),
                **parsed,
            })
            continue

        # ── Header ───────────────────────────────────────────
        if re.match(r'^#{1,4}\s', stripped):
            elements.append({"type": "header", "lines": [line],
                             "char_count": len(line),
                             "level": len(re.match(r'^(#+)', stripped).group(1)),
                             "text": stripped.lstrip("#").strip()})
            i += 1
            continue

        # ── List item ────────────────────────────────────────
        if re.match(r'^[-*+]\s|^\d+\.\s', stripped):
            block = [line]
            i += 1
            while i < len(lines) and re.match(r'^[-*+]\s|^\d+\.\s|^\s+', lines[i]) \
                    and lines[i].strip():
                block.append(lines[i])
                i += 1
            elements.append({"type": "list", "lines": block,
                             "char_count": sum(len(l) for l in block),
                             "item_count": len([l for l in block
                                               if re.match(r'^[-*+]\s|^\d+\.\s',
                                                           l.strip())])})
            continue

        # ── Blank ────────────────────────────────────────────
        if not stripped:
            elements.append({"type": "blank", "lines": [line], "char_count": 0})
            i += 1
            continue

        # ── Prose ────────────────────────────────────────────
        block = [line]
        i += 1
        while i < len(lines):
            next_line = lines[i]
            next_stripped = next_line.strip()
            # Stop at structural elements
            if (next_stripped.startswith("|") or
                next_stripped.startswith("#") or
                next_stripped.startswith("```") or
                re.match(r'^[-*+]\s|^\d+\.\s', next_stripped) or
                not next_stripped):
                break
            block.append(next_line)
            i += 1
        elements.append({"type": "prose", "lines": block,
                         "char_count": sum(len(l) for l in block)})

    return elements


def summarise(elements: list[dict], url: str, content_length: int):
    """Print a structured summary of page elements."""
    sep = "=" * 65

    type_counts = {}
    type_chars  = {}
    for e in elements:
        t = e["type"]
        type_counts[t] = type_counts.get(t, 0) + 1
        type_chars[t]  = type_chars.get(t, 0) + e.get("char_count", 0)

    tables = [e for e in elements if e["type"] == "table"]
    prose  = [e for e in elements if e["type"] == "prose"]

    print(sep)
    print("  PAGE STRUCTURE ANALYSIS")
    print(sep)
    print(f"  URL            : {url}")
    print(f"  Content length : {content_length:,} chars")
    print(f"  Total lines    : {sum(len(e['lines']) for e in elements):,}")
    print()

    print("  ELEMENT BREAKDOWN:")
    for t, count in sorted(type_counts.items(), key=lambda x: -x[1]):
        pct = type_chars.get(t, 0) / content_length * 100 if content_length else 0
        print(f"    {t:<12} {count:>4} blocks  |  "
              f"{type_chars.get(t,0):>8,} chars  ({pct:.1f}%)")

    print()
    print(f"  TABLE DETAILS ({len(tables)} tables):")
    print(f"  {'#':<4} {'Rows':>6} {'Cols':>5} {'Chars':>8}  Header sample")
    print(f"  {'-'*60}")
    for idx, t in enumerate(tables, 1):
        header_preview = ""
        if t.get("header"):
            header_preview = " | ".join(str(c) for c in t["header"][:4])
            if len(t["header"]) > 4:
                header_preview += " | ..."
        print(f"  {idx:<4} {t['data_rows']:>6} {t['col_count']:>5} "
              f"{t['char_count']:>8,}  {header_preview[:45]}")

    if tables:
        total_data_rows = sum(t["data_rows"] for t in tables)
        total_cols      = [t["col_count"] for t in tables]
        print(f"\n  Total data rows across all tables : {total_data_rows:,}")
        print(f"  Column counts                     : "
              f"min={min(total_cols)} max={max(total_cols)} "
              f"avg={sum(total_cols)/len(total_cols):.1f}")

    print()
    print(f"  PROSE SECTIONS ({len(prose)} blocks):")
    for idx, p in enumerate(prose[:5], 1):
        preview = " ".join(" ".join(p["lines"]).split())[:80]
        print(f"  {idx}. [{p['char_count']:>5} chars] {preview}")
    if len(prose) > 5:
        print(f"  ... and {len(prose)-5} more prose blocks")

    print()
    # Estimate chunks under current strategy vs element-aware
    CHUNK_SIZE, CHUNK_OVERLAP = 1600, 200
    effective = CHUNK_SIZE - CHUNK_OVERLAP
    est_current = max(1, (content_length - CHUNK_OVERLAP) // effective)

    # Element-aware estimate: 1 chunk per table section + prose chunks
    prose_chars = type_chars.get("prose", 0) + type_chars.get("header", 0)
    est_prose_chunks = max(1, (prose_chars - CHUNK_OVERLAP) // effective) \
                       if prose_chars > 0 else 0
    # Tables: group into sections of max 30 rows
    TABLE_ROWS_PER_CHUNK = 30
    est_table_chunks = sum(
        max(1, (t["data_rows"] + TABLE_ROWS_PER_CHUNK - 1) // TABLE_ROWS_PER_CHUNK)
        for t in tables
    )
    est_element_aware = est_prose_chunks + est_table_chunks

    print(f"  CHUNK ESTIMATE COMPARISON:")
    print(f"    Current strategy    : ~{est_current:,} chunks")
    print(f"    Element-aware       : ~{est_element_aware:,} chunks")
    print(f"      (prose: ~{est_prose_chunks}, tables: ~{est_table_chunks} "
          f"@ {TABLE_ROWS_PER_CHUNK} rows/chunk)")
    print(f"    Reduction           : ~{est_current - est_element_aware:,} chunks "
          f"({(1-est_element_aware/est_current)*100:.0f}% fewer)"
          if est_current > 0 else "")
    print(sep)


# ── Main ──────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url",  default="fund-changes",
                        help="URL fragment to match (default: fund-changes)")
    parser.add_argument("--json", default=None,
                        help="Path to scraper JSON (default: latest in scraper/data/)")
    args = parser.parse_args()

    # Find JSON
    if args.json:
        json_path = Path(args.json)
    else:
        data_dir = Path("scraper/data")
        jsons = sorted(data_dir.glob("royal_london_faq_approved_*.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        if not jsons:
            raise FileNotFoundError("No scraper JSON in scraper/data/")
        json_path = jsons[0]

    data = json.loads(json_path.read_text(encoding="utf-8"))

    # Filter pages
    pages = [d for d in data
             if args.url in d.get("url", "")
             and not d.get("dropdown_state", "")]

    if not pages:
        print(f"No pages matching '{args.url}' found.")
        return

    print(f"Found {len(pages)} page(s) matching '{args.url}'\n")
    for page in pages:
        content = page.get("content", "")
        elements = analyse_content(content)
        summarise(elements, page["url"], len(content))
        print()


if __name__ == "__main__":
    main()