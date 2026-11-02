"""Pure URL logic: section, content type, product category, audience, normalisation (index-identity critical)."""
import pytest

from src import scrape_approved_urls_httpV1 as s

BASE = "https://www.royallondon.com"


@pytest.mark.parametrize("url, expected", [
    (f"{BASE}/pensions/annual-allowance/", "Pensions"),
    (f"{BASE}/existing-customers/my-account/", "Existing Customers"),
    (f"{BASE}/isa/stocks-and-shares/", "ISA"),
    (f"{BASE}/Pensions/Upper-Case", "Pensions"),
    (f"{BASE}/some-new-unmapped-path/", "General"),
    ("not a url at all", "General"),
])
def test_derive_section(url, expected):
    assert s.derive_section(url) == expected


@pytest.mark.parametrize("url, expected", [
    ("https://adviser.royallondon.com/x", "adviser"), (f"{BASE}/adviser/x", "adviser"),
    ("https://employer.royallondon.com/x", "employer"), (f"{BASE}/employer/x", "employer"),
    (f"{BASE}/pensions/x", "customer"),
])
def test_derive_audience(url, expected):
    assert s.derive_audience_from_url(url) == expected


def test_content_type_first_specific_pattern_wins_and_default_is_article():
    assert s.derive_content_type(f"{BASE}/webinars/x") == "webinar"
    assert s.derive_content_type(f"{BASE}/existing-customers/webinars/x") == "webinar"   # specific beats section
    assert s.derive_content_type(f"{BASE}/pensions/x") == "article"


def test_product_category_default_is_general():
    assert s.derive_product_category(f"{BASE}/life-insurance/x") == "life_insurance"
    assert s.derive_product_category(f"{BASE}/zzz") == "general"


@pytest.mark.parametrize("excel, url, expected", [
    ("", f"{BASE}/pensions/x", "article"),
    ("faq", f"{BASE}/pensions/x", "article"),
    ("guidance", f"{BASE}/pensions/x", "guide"),
    ("tool", f"{BASE}/pensions/x", "tool"),
    ("faq", f"{BASE}/webinars/x", "webinar"),      # a high-signal URL overrides the Excel category
])
def test_excel_category_mapping(excel, url, expected):
    assert s.map_excel_category_to_content_type(excel, url) == expected


@pytest.mark.parametrize("raw, expected", [
    (f"{BASE}/Pensions/Test/?a=1#frag", f"{BASE}/pensions/test"),
    (f"  {BASE}/pensions/test/  ", f"{BASE}/pensions/test"),
    (f"{BASE}/Should-I-Consolidate", f"{BASE}/should-i-consolidate"),
    ("https://WWW.Example.com", "https://WWW.Example.com"),     # the domain is left as typed
    ("ABC/", "abc"),
])
def test_normalize_url(raw, expected):
    assert s.normalize_url(raw) == expected


def test_normalize_url_makes_differently_typed_rows_one_page():
    assert s.normalize_url(f"{BASE}/Pensions/X/") == s.normalize_url(f"{BASE}/pensions/x?utm=1")
