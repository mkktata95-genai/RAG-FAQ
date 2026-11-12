"""Excel loading and table-aware chunking in the Freshness script."""
import pytest

from src import content_freshness_httpV1 as m

BASE = "https://www.royallondon.com"
U1, U2 = f"{BASE}/pensions/one", f"{BASE}/pensions/two"


def test_load_by_header_name_strip_suffix_and_dedup(make_excel):
    p = make_excel([[U1, "Annual allowance | Royal London", "", "FAQ", ""], [f"{BASE}/Pensions/ONE/?x=1", "dup", "", "", ""]])
    pages = m.load_approved_pages(p)
    assert len(pages) == 1 and pages[0]["title"] == "Annual allowance" and pages[0]["excel_category"] == "faq"


@pytest.mark.parametrize("status, kept", [("404", False), ("dead", False), ("200", True), ("", True), ("???", True)])
def test_load_dead_status_rows(make_excel, status, kept):
    assert (len(m.load_approved_pages(make_excel([[U1, "T", status, "", ""]]))) == 1) is kept


def test_load_without_url_column_raises(make_excel):
    with pytest.raises(ValueError, match="Foo"):
        m.load_approved_pages(make_excel([["x"]], header=("Foo",)))


TABLE = ("Intro paragraph about the charges we make on your pension pot each year. " * 3 + "\n\n"
         "| Fund | Charge |\n| --- | --- |\n" + "\n".join(f"| Fund {i} | 0.{i:02d}% |" for i in range(70)) + "\n\nClosing words after the table. " * 3)


def test_long_table_is_split_with_header_repeated():
    cs = m.chunk_pages([{"source_url": U1, "title": "Charges", "content": TABLE, "scraped_at": "2026-01-01T00:00:00+00:00"}])
    tables = [c for c in cs if c["element_type"] == "table"]
    assert len(tables) >= 2 and all("| Fund | Charge |" in c["content"] for c in tables)


def test_element_parsing_finds_headings_paragraphs_and_tables():
    els = m._ea_parse_elements("# Heading\n\nA paragraph.\n\n| a | b |\n| - | - |\n| 1 | 2 |")
    assert {e["type"] for e in els} >= {"table"} and len(els) >= 2


# ── hero banner (script v1.1.2 behaviour, parity with scraper v1.2.1) ──
def test_hero_header_is_kept_other_headers_dropped():
    html = ('<html><body><main><header class="hero"><h1>Hero title</h1><p>Strapline text</p></header>'
            '<header class="site"><p>Site header</p></header><p>Body text</p></main></body></html>')
    out = m.extract_main_html(html)
    assert "Hero title" in out and "Strapline text" in out and "Site header" not in out