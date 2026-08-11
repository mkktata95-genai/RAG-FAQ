"""
element_detector.py  v1.0.0
==============================
Element-aware chunking module. Single source of truth for
chunk_and_index_hqaV5.py, which imports it directly.

NOTE — content_freshnessV1.py does NOT import this module. It
INLINES an identical copy of this logic instead (prefixed _ea_),
by deliberate design — content_freshnessV1.py's stated principle
is to be a single independent script with zero dependency on other
project code files, so the nightly freshness job can never break
because an unrelated script changed. See that file's v1.7.0
changelog entry for the full rationale. This means a fix here must
be manually mirrored into content_freshnessV1.py's inlined copy —
a known, accepted tradeoff, not an oversight.

DESIGN — confirmed via Phase 0/1 audits before any code was written
-----------------------------------------------------------------------
1. TABLES (35 pages, 18 with split-risk under flat chunking):
   Atomic per table — never split mid-row. Header row repeated in
   every batch if a table exceeds TABLE_ROWS_PER_CHUNK (safety-net
   cap; current max table is 96 rows across 16 tables on one page,
   so this rarely/never fires in practice — defensive, not primary).

2. HEADERS as hard section boundaries (confirmed via tab pages AND
   accordion-style FAQ pages — both are structurally the same problem:
   content organised under ## / ### headings that must not bleed into
   each other when chunked):
   - `##` and `###` headers = hard boundaries (topic shift signal)
   - `####` and deeper = left as in-section formatting, not a boundary
   - No special-casing for "this is a tab" vs "this is an FAQ" — one
     mechanism (header-bounded splitting) covers both. Verified on
     investing-responsibly (tabs) and pensions-explained (FAQ, 53,918
     chars / 8 questions, ~6,740 chars/answer — well over CHUNK_SIZE,
     confirming these need real splitting, not atomic treatment).

3. Sections with NO headers (296/297 pages) fall through to the
   EXACT existing RecursiveCharacterTextSplitter behaviour — zero
   regression risk for the bulk of the index. This module only
   changes behaviour for pages that actually contain tables or
   `##`/`###` headers.

4. No artificial merging of small sections — a short FAQ answer
   becomes its own small chunk, matching how dropdown_state atomic
   chunks already behave (small, precise, single-purpose).

INTEGRATION
-----------
chunk_and_index_hqaV5.py calls:

    from element_detector import chunk_content_element_aware

    pieces = chunk_content_element_aware(content, CHUNK_SIZE, CHUNK_OVERLAP)
    # pieces: list of {"text": str, "element_type": str}
    # caller wraps each piece into its existing chunk dict structure
    # (chunk_id, content_hash, pipeline_version, etc.) exactly as it
    # already does for splitter.split_text() output today — this
    # module does NOT know about indexing metadata, only about text
    # segmentation, keeping the seam clean.

CHANGELOG
---------
v1.1.0 — Fixed orphaned-header bug: RecursiveCharacterTextSplitter
          could isolate a prepended "### <header>" line into its own
          useless first chunk when the section's body text was large
          and separator-sparse (header + first body fragment together
          exceeded chunk_size, so the splitter couldn't merge them).
          Added _merge_orphaned_header() post-processing step.
v1.0.0 — Initial shared module. Element detection (header/table/prose),
          header-bounded section grouping, table atomicity with row-cap
          safety net, prose splitting scoped per section.
"""

import re
from langchain_text_splitters import RecursiveCharacterTextSplitter

# ── Constants ──────────────────────────────────────────────────────────────────

# Safety-net cap — current max observed table is 96 rows across 16
# tables on one page (retirement-living-standards). Individual tables
# are much smaller in practice. This rarely/never fires today; it
# exists so a future oversized table degrades gracefully (row-batched,
# header repeated) instead of becoming one giant unsplittable chunk.
TABLE_ROWS_PER_CHUNK = 30

# Header levels that count as hard section boundaries. #### and deeper
# are left as in-section formatting — a boundary at every heading depth
# would over-fragment pages that use #### for minor sub-points inside
# a coherent ## or ### section.
_BOUNDARY_HEADER_RE = re.compile(r'^(#{2,3})\s+(.+)$')

_DEFAULT_SEPARATORS = ["\n\n", "\n", ". ", " ", ""]


# ── Element parsing ──────────────────────────────────────────────────────────

# Parse a contiguous block of |pipe| lines into header/rows/col_count.
def _parse_table_block(lines: list) -> dict:
    """Parse a contiguous block of |pipe| lines into header/rows/col_count."""
    header_row    = None
    separator_row = None
    data_rows     = []
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
    return {
        "header_row": header_row,
        "data_rows":  data_rows,
    }


# Parse raw markdown content into an ordered list of typed elements:
def parse_elements(content: str) -> list:
    """
    Parse raw markdown content into an ordered list of typed elements:
      {"type": "header", "level": 2|3|4.., "text": str, "lines": [...]}
      {"type": "table",  "lines": [...], "header_row": [...], "data_rows": [[...]]}
      {"type": "prose",  "lines": [...]}
      {"type": "blank",  "lines": [line]}

    This is intentionally simple — it only needs to distinguish the
    three things chunking cares about (headers as boundaries, tables
    as atomic units, everything else as splittable prose). It does
    NOT need list/code_block granularity the way the earlier audit
    scripts did; those extra types all fold into "prose" here since
    they're all handled identically by the splitter.
    """
    lines    = content.splitlines()
    elements = []
    i = 0
    while i < len(lines):
        line     = lines[i]
        stripped = line.strip()

        # Header (## or ### only — see _BOUNDARY_HEADER_RE)
        header_match = re.match(r'^(#{1,6})\s+(.+)$', stripped)
        if header_match:
            level = len(header_match.group(1))
            elements.append({
                "type":  "header",
                "level": level,
                "text":  header_match.group(2).strip(),
                "lines": [line],
            })
            i += 1
            continue

        # Table block
        if stripped.startswith("|") and "|" in stripped[1:]:
            block = [line]
            i += 1
            while i < len(lines) and lines[i].strip().startswith("|"):
                block.append(lines[i])
                i += 1
            parsed = _parse_table_block(block)
            elements.append({
                "type":       "table",
                "lines":      block,
                "header_row": parsed["header_row"],
                "data_rows":  parsed["data_rows"],
            })
            continue

        # Blank line
        if not stripped:
            elements.append({"type": "blank", "lines": [line]})
            i += 1
            continue

        # Prose block — accumulate until next header/table/blank
        block = [line]
        i += 1
        while i < len(lines):
            ns = lines[i].strip()
            if (ns.startswith("|") or re.match(r'^#{1,6}\s', ns) or not ns):
                break
            block.append(lines[i])
            i += 1
        elements.append({"type": "prose", "lines": block})

    return elements


# ── Section grouping (header-bounded) ────────────────────────────────────────

# Group elements into sections using ## and ### headers as hard
def group_into_sections(elements: list) -> list:
    """
    Group elements into sections using ## and ### headers as hard
    boundaries. #### and deeper headers do NOT start a new section —
    they stay inside the current section's body as formatting.

    Returns list of sections:
      {"header_text": str | None, "header_level": int | None,
       "body": [elements]}

    The first section may have header_text=None if content starts
    with prose before any boundary-level header (e.g. an intro
    paragraph before the first ## heading).
    """
    sections = []
    current  = {"header_text": None, "header_level": None, "body": []}

    for el in elements:
        if el["type"] == "header" and el["level"] in (2, 3):
            # Start a new section — flush the current one first
            if current["body"] or current["header_text"] is not None:
                sections.append(current)
            current = {
                "header_text":  el["text"],
                "header_level": el["level"],
                "body":         [],
            }
        else:
            current["body"].append(el)

    if current["body"] or current["header_text"] is not None:
        sections.append(current)

    return sections


# ── Table chunking (atomic, row-capped) ──────────────────────────────────────

# Render a (possibly batched) set of table rows back to markdown.
def _render_table_chunk(header_row: list, data_rows: list) -> str:
    """Render a (possibly batched) set of table rows back to markdown."""
    if not header_row:
        # No detected header — just join whatever rows we have
        lines = ["| " + " | ".join(row) + " |" for row in data_rows]
        return "\n".join(lines)

    sep = ["-" * max(3, len(h)) for h in header_row]
    lines = [
        "| " + " | ".join(header_row) + " |",
        "| " + " | ".join(sep) + " |",
    ]
    for row in data_rows:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


# Convert a table element into one or more atomic chunk texts.
def chunk_table_element(table_el: dict) -> list:
    """
    Convert a table element into one or more atomic chunk texts.
    Single chunk if data_rows <= TABLE_ROWS_PER_CHUNK; otherwise
    row-batched with the header row repeated in every batch.
    """
    header_row = table_el.get("header_row")
    data_rows  = table_el.get("data_rows") or []

    if not data_rows:
        # Degenerate table (header only, or malformed) — return as-is
        return ["\n".join(table_el["lines"])]

    if len(data_rows) <= TABLE_ROWS_PER_CHUNK:
        return [_render_table_chunk(header_row, data_rows)]

    batches = []
    for start in range(0, len(data_rows), TABLE_ROWS_PER_CHUNK):
        batch = data_rows[start:start + TABLE_ROWS_PER_CHUNK]
        batches.append(_render_table_chunk(header_row, batch))
    return batches


# ── Section → chunks ─────────────────────────────────────────────────────────

# RecursiveCharacterTextSplitter can end up putting a prepended
def _merge_orphaned_header(pieces: list, header_text: str) -> list:
    """
    RecursiveCharacterTextSplitter can end up putting a prepended
    section header in its own tiny first chunk when the body content
    that follows is large and separator-sparse (e.g. one long
    sentence-heavy paragraph with no early \\n\\n break) — the header
    line plus the first body fragment exceed chunk_size together, so
    the splitter can't merge them, leaving the header isolated.

    A chunk containing ONLY the header (no body content) is useless
    for retrieval — merge it into the following chunk instead.
    """
    if len(pieces) < 2:
        return pieces

    header_line = f"### {header_text}".strip()
    first       = pieces[0].strip()

    if first == header_line:
        merged = pieces[0] + "\n" + pieces[1]
        return [merged] + pieces[2:]

    return pieces


# Convert one section's body elements into an ordered list of
def _section_to_text_segments(section: dict) -> list:
    """
    Convert one section's body elements into an ordered list of
    {"text": str, "element_type": "prose"|"table"} segments, BEFORE
    the prose splitter runs. Consecutive prose/blank elements are
    merged into one prose blob; each table becomes its own segment
    (or multiple, if row-batched).
    """
    segments   = []
    prose_buf  = []

    def _flush_prose():
        # Join buffered prose lines into one segment and reset the buffer.
        if prose_buf:
            text = "\n".join(prose_buf).strip()
            if text:
                segments.append({"text": text, "element_type": "prose"})
            prose_buf.clear()

    for el in section["body"]:
        if el["type"] == "table":
            _flush_prose()
            for chunk_text in chunk_table_element(el):
                segments.append({"text": chunk_text, "element_type": "table"})
        elif el["type"] == "blank":
            prose_buf.append("")
        else:
            # prose or a non-boundary header (####+) — keep as text
            prose_buf.extend(el["lines"])

    _flush_prose()
    return segments


# Main entry point. Returns an ordered list of
def chunk_content_element_aware(
    content: str,
    chunk_size: int    = 1600,
    chunk_overlap: int = 200,
) -> list:
    """
    Main entry point. Returns an ordered list of
    {"text": str, "element_type": str} chunk pieces for the given
    page content.

    Behaviour:
      - No headers, no tables  → identical output to the existing
        flat RecursiveCharacterTextSplitter (element_type="prose"
        for every piece) — zero behavioural change for the 296/297
        pages with plain content.
      - Headers/tables present → header-bounded sections, each
        section's prose split independently (never bleeding into
        the next section), tables rendered atomically within their
        section (row-capped for safety).
    """
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        separators=_DEFAULT_SEPARATORS,
    )

    elements = parse_elements(content)

    has_boundary_header = any(
        e["type"] == "header" and e["level"] in (2, 3) for e in elements
    )
    has_table = any(e["type"] == "table" for e in elements)

    # Fast path — no structural elements at all, behave exactly as
    # the current flat splitter does today.
    if not has_boundary_header and not has_table:
        pieces = splitter.split_text(content)
        return [{"text": p, "element_type": "prose"} for p in pieces if p.strip()]

    sections = group_into_sections(elements)
    results  = []

    for section in sections:
        header_text = section.get("header_text")
        segments    = _section_to_text_segments(section)

        for seg in segments:
            if seg["element_type"] == "table":
                # Atomic — never run through the splitter. Prepend
                # the section header for context if present, same
                # convention as chunk_and_index_hqaV5.py's title-
                # prepend pattern for other atomic chunk types.
                text = seg["text"]
                if header_text:
                    text = f"### {header_text}\n{text}"
                results.append({"text": text, "element_type": "table"})
            else:
                # Prose — split normally, but SCOPED to this section
                # only. A chunk can never contain content from two
                # different sections.
                section_text = seg["text"]
                if header_text:
                    section_text = f"### {header_text}\n{section_text}"
                pieces = splitter.split_text(section_text)
                if header_text:
                    pieces = _merge_orphaned_header(pieces, header_text)
                for p in pieces:
                    if p.strip():
                        results.append({"text": p, "element_type": "prose"})

    return results
