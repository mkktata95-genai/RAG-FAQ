"""HTML -> markdown -> cleaned content. A cleaning-rule change alters content hashes (and re-chunks the index):
these tests pin the current behaviour so a change is a deliberate decision."""
from src import scrape_approved_urls_httpV1 as s


# ── extract_main_html ─────────────────────────────────────────
def test_extract_main_html_keeps_main_and_drops_boilerplate(page_html):
    out = s.extract_main_html(page_html(body_text="Real content here."))
    assert "Real content here." in out
    for gone in ("Menu Home Products", "Site header", "Copyright Royal London", "var x = 1"):
        assert gone not in out


def test_extract_main_html_removes_volatile_widgets_and_hidden_panels():
    html = ('<main><p>Keep me</p><div class="sliderblock">rotating banner</div>'
            '<div data-content="a">hidden dropdown panel</div></main>')
    out = s.extract_main_html(html)
    assert "Keep me" in out and "rotating banner" not in out and "hidden dropdown panel" not in out


def test_extract_main_html_falls_back_to_body_when_no_container():
    out = s.extract_main_html("<html><body><p>plain body text</p></body></html>")
    assert "plain body text" in out


def test_selector_priority_prefers_main_content_region():
    html = "<body><div id='content'><p>inside content div</p></div><p>outside</p></body>"
    out = s.extract_main_html(html)
    assert "inside content div" in out and "outside" not in out


# ── markdown ──────────────────────────────────────────────────
def test_markdown_keeps_links_headings_atx_and_drops_images():
    md = s.html_fragment_to_markdown("<h2>Hi</h2><p>a <a href='https://x'>link</a><img src='i.png' alt='pic'></p>")
    assert md.startswith("## Hi") and "[link](https://x)" in md and "pic" not in md and "i.png" not in md


# ── clean_content ─────────────────────────────────────────────
def test_clean_content_removes_ui_noise_but_keeps_article_text():
    raw = ("Intro paragraph.\n\n[Back to top](#top)\nMiddle paragraph.\n\nShare\n\n"
           "Previous Item\nNext Item\nhttps://www.facebook.com/royallondon\n\nFinal paragraph.")
    out = s.clean_content(raw)
    for kept in ("Intro paragraph.", "Middle paragraph.", "Final paragraph."):
        assert kept in out
    for gone in ("Back to top", "Previous Item", "Next Item", "facebook.com"):
        assert gone not in out
    assert "\n\n\n" not in out


def test_clean_content_cuts_everything_from_latest_news_heading():
    out = s.clean_content("Real article.\n\n### Latest news\nOld headline\nAnother headline")
    assert out.strip() == "Real article." and "Old headline" not in out


def test_clean_content_removes_breadcrumb_lines():
    out = s.clean_content("1. [ Home ](https://www.royallondon.com) >\n2. [ Pensions ](https://x) >\n3. Annual allowance\n\nBody text.")
    assert "Home" not in out and "Body text." in out


def test_clean_content_removes_browser_banner():
    out = s.clean_content("Your browser is not supported. Please upgrade. ×\nReal text.")
    assert "browser is not supported" not in out and "Real text." in out


def test_clean_content_is_stable_on_already_clean_text():
    text = "Heading\n\nA plain paragraph with nothing to strip."
    assert s.clean_content(text) == text


# ── remove_duplicate_content ──────────────────────────────────
def test_full_duplicate_body_is_cut():
    body = "# Title\n\n" + " ".join(f"word{i}" for i in range(80))
    assert s.remove_duplicate_content(body + "\n\n" + body).strip() == body.strip()


def test_repeated_heading_with_different_text_is_kept():
    text = ("# Overview\n\n" + " ".join(f"alpha{i}" for i in range(60)) + "\n\n# Overview\n\n"
            + " ".join(f"beta{i}" for i in range(60)))
    assert s.remove_duplicate_content(text) == text


def test_single_heading_untouched():
    assert s.remove_duplicate_content("# Only\n\ntext") == "# Only\n\ntext"

def test_extract_main_html_keeps_hero_header_but_drops_other_headers():
    html = (
        "<html><body><header class='site'>SITE NAV</header>"
        "<main><header class='hero'><h1>Insurance</h1><p>Strapline text</p></header>"
        "<header class='card'>CARD HEADER</header><p>Body text.</p></main></body></html>"
    )
    out = s.extract_main_html(html)
    assert "Insurance" in out and "Strapline text" in out
    assert "SITE NAV" not in out and "CARD HEADER" not in out


def test_scraper_version_is_1_2_1():
    assert s.SCRAPER_VERSION == "1.2.1"
