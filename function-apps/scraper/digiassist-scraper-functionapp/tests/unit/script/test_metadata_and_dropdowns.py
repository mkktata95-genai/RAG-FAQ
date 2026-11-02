"""Page metadata, video detection and dropdown-state extraction."""
import pytest

from src import scrape_approved_urls_httpV1 as s

URL = "https://www.royallondon.com/pensions/x"


def test_extract_page_metadata_defaults_and_description(page_html):
    m = s.extract_page_metadata(page_html(description="A short description"), URL)
    assert m["description"] == "A short description" and m["audience"] == "customer"
    assert m["product_category"] == "pensions" and m["has_video"] is False


def test_webinar_url_counts_as_video():
    assert s.detect_video_from_html("<html></html>", "https://www.royallondon.com/webinars/x") is True
    assert s.detect_video_from_html("<html></html>", URL) is False


def test_extract_video_url_finds_embedded_player():
    html = '<iframe src="https://www.youtube.com/embed/abc"></iframe>'
    assert s.extract_video_url(html, URL) == "https://www.youtube.com/embed/abc"
    assert s.extract_video_url("<p>none</p>", URL) == ""


# ── dropdown helpers ──────────────────────────────────────────
def test_jaccard_similarity():
    assert s._jaccard_similarity({"a", "b"}, {"b", "c"}) == pytest.approx(1 / 3)
    assert s._jaccard_similarity(set(), set()) == 0.0
    assert s._jaccard_similarity({"a"}, {"a"}) == 1.0


def test_panel_word_set_lowercases_and_caps():
    assert s._panel_word_set("Hello, HELLO world!") == {"hello", "world"}
    assert len(s._panel_word_set(" ".join(f"w{i}" for i in range(500)), cap=10)) == 10


@pytest.mark.parametrize("text, expected", [
    ("Call us on 0345 604 1234", True), ("Lines are open Monday to Friday", True), ("Nothing relevant here", False),
])
def test_contact_signals(text, expected):
    assert s._has_contact_signals(text) is expected


def test_group_variance_distinguishes_real_dropdowns_from_sort_controls():
    different = ["alpha beta gamma delta epsilon", "one two three four five"]
    reordered = ["alpha beta gamma delta epsilon", "epsilon delta gamma beta alpha"]
    assert s._dropdown_group_has_variance(different) is True
    assert s._dropdown_group_has_variance(reordered) is False
    assert s._dropdown_group_has_variance(["only one"]) is False


def select_page(options, panels):
    opts = "".join(f'<option value="{v}">{t}</option>' for v, t in options)
    pans = "".join(f'<div data-content="{v}">{t}</div>' for v, t in panels)
    return f"<html><body><main><select>{opts}</select>{pans}</main></body></html>"


def test_routing_dropdown_detection():
    two = select_page([("a", "Option A"), ("b", "Option B")], [])
    placeholder_only = select_page([("", "Select..."), ("a", "Option A")], [])
    assert s._has_routing_dropdowns_in_html(two) is True
    assert s._has_routing_dropdowns_in_html(placeholder_only) is False
    assert s._has_routing_dropdowns_in_html("") is False
    assert s._has_routing_dropdowns_in_html("<p>no select</p>") is False


def test_find_panel_matches_value_then_text():
    from bs4 import BeautifulSoup
    soup = BeautifulSoup('<div data-content="Alpha">by value</div><div data-content="beta text">by text</div>', "html.parser")
    assert s._find_panel_for_option(soup, "alpha", "x").get_text() == "by value"
    assert s._find_panel_for_option(soup, "zzz", "Beta Text").get_text() == "by text"
    assert s._find_panel_for_option(soup, "none", "none") is None


BASE_PAGE = {"source_url": URL, "title": "Bereavement", "section": "Pensions", "audience": "customer",
             "content_type": "article", "description": "", "has_video": False, "video_url": "",
             "page_image_url": "", "product_category": "pensions", "publish_date": "",
             "collection_name": ""}


def test_genuine_dropdown_produces_one_state_per_option():
    html = select_page([("uk", "United Kingdom"), ("ie", "Ireland")],
                       [("uk", "Call us on 0345 604 1234 about a UK policy claim today."),
                        ("ie", "Write to us at our Dublin office about an Irish policy claim.")])
    states = s.extract_dropdown_states_from_html(html, URL, "Bereavement", dict(BASE_PAGE))
    assert len(states) == 2
    assert {x["dropdown_title"] for x in states} == {"United Kingdom", "Ireland"}
    assert all(x["source_url"] == URL and x["dropdown_url"] for x in states)


def test_sort_control_dropdown_is_rejected():
    panel = "alpha beta gamma delta epsilon zeta"
    html = select_page([("n", "Newest"), ("o", "Oldest")], [("n", panel), ("o", " ".join(reversed(panel.split())))])
    assert s.extract_dropdown_states_from_html(html, URL, "T", dict(BASE_PAGE)) == []


def test_dropdown_options_without_panels_are_skipped():
    html = select_page([("a", "Option A"), ("b", "Option B")], [("a", "Only the first panel has content here ok.")])
    assert s.extract_dropdown_states_from_html(html, URL, "T", dict(BASE_PAGE)) == []


def test_empty_html_gives_no_states():
    assert s.extract_dropdown_states_from_html("", URL, "T", dict(BASE_PAGE)) == []
