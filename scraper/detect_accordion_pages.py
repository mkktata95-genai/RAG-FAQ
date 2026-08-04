"""
detect_accordion_pages.py  v1.0.0
====================================
Checks whether any pages in the current approved URL list actually
contain accordion-style content (repeating header + short-body pairs,
e.g. FAQ "Q: ... A: ..." patterns) before committing engineering time
to building accordion detection/chunking logic.

CONTENT-ONLY — no CDP, no live scraping. Reads the already-scraped
JSON and looks for a structural SIGNATURE:

  A genuine accordion/FAQ section looks like repeated:
      ### <short question-like header>
      <short-to-medium prose answer>
      ### <another short question-like header>
      <short-to-medium prose answer>
      ... (3+ times in a row, similar body lengths)

  This is structurally different from:
    - A single long header + long prose section (regular content page)
    - Tab-header sections (12 confirmed pages — few headers, often
      longer/varied body lengths, not a repeating short-form pattern)

DETECTION HEURISTIC
--------------------
For each page:
  1. Parse into header/prose elements (reused logic from table_audit.py)
  2. Find RUNS of 3+ consecutive (header, prose) pairs
  3. Flag as accordion-candidate if:
     - Run length >= 3
     - Header text looks question-like (short, often ends in ? or
       starts with What/How/Why/When/Can/Do/Is)
     - Body lengths are relatively short and consistent (not wildly
       varying — a real accordion has similar-sized answers)

OUTPUT
------
  Console summary + accordion_candidates_report.json

USAGE
-----
  python detect_accordion_pages.py --json scraper/data/<fresh_scrape>.json

CHANGELOG
---------
v1.0.0 — Initial accordion existence check, content-only.
"""

import argparse
import json
import re
import statistics
import sys
from pathlib import Path

SEP = "=" * 70

# Question-like header signals
_QUESTION_STARTERS = (
    "what", "how", "why", "when", "can", "do", "does", "is", "are",
    "will", "should", "could", "would", "who", "which"
)


def parse_elements(content: str) -> list:
    """
    Minimal header/prose/table parser — reused pattern from
    table_audit.py's detect_tables(), extended for headers.
    Returns ordered list of {"type": "header"|"prose"|"table"|"other",
    "text": str, "char_count": int}.
    """
    lines = content.splitlines()
    elements = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Header
        if re.match(r'^#{1,4}\s', stripped):
            elements.append({
                "type": "header",
                "text": stripped.lstrip("#").strip(),
                "char_count": len(stripped),
            })
            i += 1
            continue

        # Table (pipe-prefixed block) — skip as its own element
        if stripped.startswith("|") and "|" in stripped[1:]:
            block = [line]
            i += 1
            while i < len(lines) and lines[i].strip().startswith("|"):
                block.append(lines[i])
                i += 1
            elements.append({
                "type": "table",
                "text": "",
                "char_count": sum(len(l) for l in block),
            })
            continue

        # Blank
        if not stripped:
            i += 1
            continue

        # Prose block — accumulate until next header/table/blank-blank
        block = [line]
        i += 1
        while i < len(lines):
            ns = lines[i].strip()
            if ns.startswith("|") or re.match(r'^#{1,4}\s', ns) or not ns:
                break
            block.append(lines[i])
            i += 1
        text = " ".join(l.strip() for l in block)
        elements.append({
            "type": "prose",
            "text": text,
            "char_count": len(text),
        })

    return elements


def is_question_like(header_text: str) -> bool:
    """Heuristic: does this header look like an FAQ question?"""
    lower = header_text.lower().strip()
    if lower.endswith("?"):
        return True
    first_word = lower.split()[0] if lower.split() else ""
    return first_word in _QUESTION_STARTERS


def find_accordion_runs(elements: list) -> list:
    """
    Find runs of 3+ consecutive (header, prose) pairs where headers
    look question-like and body lengths are relatively consistent.
    Returns list of runs, each a list of (header_text, body_char_count).
    """
    runs = []
    current_run = []

    i = 0
    while i < len(elements) - 1:
        el = elements[i]
        nxt = elements[i + 1]

        if (el["type"] == "header" and nxt["type"] == "prose"
                and is_question_like(el["text"])):
            current_run.append((el["text"], nxt["char_count"]))
            i += 2
        else:
            if len(current_run) >= 3:
                runs.append(current_run)
            current_run = []
            i += 1

    if len(current_run) >= 3:
        runs.append(current_run)

    return runs


def check_consistency(run: list) -> bool:
    """
    Real accordions have relatively similar answer lengths.
    Flag as consistent if stdev/mean ratio is reasonable (not wildly
    varying — e.g. one 50-char answer next to a 3000-char one suggests
    this isn't a true accordion, just headers that happen to repeat).
    """
    lengths = [body_len for _, body_len in run]
    if not lengths or statistics.mean(lengths) == 0:
        return False
    mean = statistics.mean(lengths)
    stdev = statistics.stdev(lengths) if len(lengths) > 1 else 0
    cv = stdev / mean  # coefficient of variation
    return cv < 1.5  # generous threshold — real accordions vary some


def main():
    parser = argparse.ArgumentParser(description="Detect accordion-pattern pages")
    parser.add_argument("--json", required=True, help="Scraped JSON file")
    parser.add_argument("--out", default="accordion_candidates_report.json")
    args = parser.parse_args()

    json_path = Path(args.json)
    if not json_path.exists():
        print(f"[ERROR] {json_path} not found")
        sys.exit(1)

    print(f"\n{SEP}")
    print("  ACCORDION PATTERN DETECTION (content-only)")
    print(SEP)

    print(f"\nLoading {json_path} ...")
    data  = json.loads(json_path.read_text(encoding="utf-8"))
    pages = [d for d in data if not d.get("dropdown_state", "")]
    print(f"Pages loaded: {len(pages)}\n")

    candidates = []

    for page in pages:
        content = page.get("content", "")
        if not content:
            continue

        elements = parse_elements(content)
        runs     = find_accordion_runs(elements)

        genuine_runs = [r for r in runs if check_consistency(r)]

        if genuine_runs:
            longest_run = max(genuine_runs, key=len)
            candidates.append({
                "url":         page.get("url", ""),
                "title":       page.get("title", ""),
                "run_count":   len(genuine_runs),
                "longest_run": len(longest_run),
                "sample_headers": [h for h, _ in longest_run[:5]],
            })

    print(f"{SEP}")
    print("  RESULTS")
    print(SEP)
    print(f"  Total pages scanned      : {len(pages)}")
    print(f"  Accordion candidates     : {len(candidates)}")

    if candidates:
        print(f"\n  CANDIDATE PAGES ({len(candidates)}):")
        for c in sorted(candidates, key=lambda x: x["longest_run"], reverse=True):
            print(f"\n    [{c['longest_run']} Q&A pairs] {c['url']}")
            print(f"      Sample headers:")
            for h in c["sample_headers"]:
                print(f"        - {h}")
    else:
        print("\n  ✓ No accordion patterns detected in current URL list.")
        print("    Accordion detection/chunking is NOT needed for this scope.")

    print(f"\n{SEP}")

    out_path = Path(args.out)
    out_path.write_text(json.dumps(candidates, indent=2), encoding="utf-8")
    print(f"  Report saved → {out_path}")


if __name__ == "__main__":
    main()