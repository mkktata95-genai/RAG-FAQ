"""
extract_base_image_url.py

Standalone extraction function for citation thumbnail images.

Finalized approach (validated against 7 sample URLs across page templates):
  - og:image is NOT usable — always generic (RL logo or teaser stock photo)
    across every page checked. Do not extract or use it.
  - The only page-specific image source is the CSS feature-banner
    background-image, present in inline <style> blocks as:
        background-image: url(/globalassets/.../<name>-<size>.jpg);
    with responsive size tiers: small-800x800, medium-1000x570, large-1200x700.
  - Prefer the medium-1000x570 tier (best balance of quality vs payload);
    fall back to whichever tier is present if medium is missing.
  - Path is relative -> MUST resolve with urljoin against the page URL,
    not string concatenation (concatenation caused a double-slash bug:
    royallondon.com//globalassets/...).

FIX (this version): the original regex tried to match
"linear-gradient(...)" with `[^)]*`, which breaks on any gradient that
contains nested parentheses — e.g. an rgba() overlay color, which is the
most common real-world form:
    background-image: linear-gradient(rgba(0,0,0,.4),rgba(0,0,0,.4)), url(...)
`[^)]*` stops at the FIRST `)` it sees, which is the closing paren of the
inner rgba(...), not the end of linear-gradient(...) — so the whole match
silently failed and returned None for any banner using a gradient overlay,
which is a very common CSS pattern for hero banners with text on top.

Fix: don't try to hand-parse the gradient's internal structure at all.
Instead, isolate the full "background-image: ...;" declaration first (up
to the semicolon), then just grab the LAST url(...) inside that
declaration — the image is always the final url() in the stack, after
any gradient overlays. This is robust to single gradients, stacked
multiple gradients, and gradients with any level of nested parentheses,
without needing to model CSS gradient syntax at all.

Also added .webp to the supported extension list, since modern sites
increasingly serve WebP for banner images alongside/instead of jpg/png.

Drop this function into the scraper. Call it once per page with the page's
already-fetched HTML (no extra HTTP call needed) and the page's URL.
"""

import re
from urllib.parse import urljoin
from typing import Optional

# Step 1: isolate the full background-image declaration (everything between
# "background-image:" and the next semicolon).
_DECLARATION_RE = re.compile(r'background-image:\s*([^;]+);', re.IGNORECASE)

# Step 2: within an isolated declaration, find url(...) references pointing
# at an image file. Handles optional quotes inside url().
_URL_RE = re.compile(
    r'url\(\s*[\'"]?([^\'")]+\.(?:jpg|jpeg|png|webp))[\'"]?\s*\)',
    re.IGNORECASE,
)

# Preference order for size tier when multiple are found on the page.
SIZE_TIER_PREFERENCE = ["medium-1000x570", "small-800x800", "large-1200x700"]


def extract_base_image_url(html: str, page_url: str) -> Optional[str]:
    """
    Extract the page-specific citation thumbnail image URL from a scraped
    page's HTML.

    Args:
        html: Raw HTML of the page (already fetched by the scraper -
              no extra HTTP call).
        page_url: The canonical URL of the page being scraped. Used to
                  resolve the relative image path to an absolute URL.

    Returns:
        Absolute image URL (str) if a banner image was found, else None.
    """
    matches = []
    seen = set()

    for decl_match in _DECLARATION_RE.finditer(html):
        declaration = decl_match.group(1)
        # A declaration can stack multiple url() refs (gradient layers,
        # occasionally multiple background images) — the actual photo is
        # always the LAST url() in the stack, after any gradient overlays.
        urls_in_declaration = _URL_RE.findall(declaration)
        if not urls_in_declaration:
            continue
        rel_path = urls_in_declaration[-1]
        if rel_path not in seen:
            seen.add(rel_path)
            matches.append(rel_path)

    if not matches:
        return None

    # Prefer medium tier; fall back through the preference list; else take
    # whatever was found first.
    chosen = None
    for tier in SIZE_TIER_PREFERENCE:
        for rel_path in matches:
            if tier in rel_path:
                chosen = rel_path
                break
        if chosen:
            break
    if not chosen:
        chosen = matches[0]

    # urljoin (not string concatenation) correctly resolves the relative
    # path regardless of leading/trailing slashes on either side, and
    # passes through an already-absolute URL unchanged.
    return urljoin(page_url, chosen)


if __name__ == "__main__":
    # Original sanity check
    sample_html = """
    <style>
    .featurebanner-split #feature-banner-group-executive-committee_9066 {
        background-image: url(/globalassets/all-site-images/brand-photography/people-and-moments/business-people-meeting-in-modern-office-medium-1000x570.jpg);
    }
    </style>
    """
    sample_url = "https://www.royallondon.com/about-us/how-we-are-run/governance-and-leadership-teams/group-executive-committee/"
    result = extract_base_image_url(sample_html, sample_url)
    assert result == "https://www.royallondon.com/globalassets/all-site-images/brand-photography/people-and-moments/business-people-meeting-in-modern-office-medium-1000x570.jpg"
    print("PASS - original sample")

    # Regression tests added after finding the nested-parenthesis bug
    tests = [
        ("no banner at all",
         "<style>.foo{color:red;}</style>",
         "https://www.royallondon.com/foo/",
         None),
        ("only small tier present",
         "<style>.b{background-image: url(/globalassets/img/hero-small-800x800.jpg);}</style>",
         "https://www.royallondon.com/foo/",
         "https://www.royallondon.com/globalassets/img/hero-small-800x800.jpg"),
        ("multiple tiers - prefers medium",
         """<style>
            .a{background-image: url(/globalassets/img/hero-large-1200x700.jpg);}
            .b{background-image: url(/globalassets/img/hero-medium-1000x570.jpg);}
            .c{background-image: url(/globalassets/img/hero-small-800x800.jpg);}
            </style>""",
         "https://www.royallondon.com/foo/",
         "https://www.royallondon.com/globalassets/img/hero-medium-1000x570.jpg"),
        ("linear-gradient + rgba overlay (the bug this fix addresses)",
         "<style>.b{background-image: linear-gradient(rgba(0,0,0,.4),rgba(0,0,0,.4)), url(/globalassets/img/hero-medium-1000x570.jpg);}</style>",
         "https://www.royallondon.com/foo/",
         "https://www.royallondon.com/globalassets/img/hero-medium-1000x570.jpg"),
        ("double-stacked gradients",
         "<style>.b{background-image: linear-gradient(to right, rgba(0,0,0,.6), rgba(0,0,0,0)), linear-gradient(rgba(1,1,1,.2),rgba(1,1,1,.2)), url(/globalassets/img/hero-medium-1000x570.jpg);}</style>",
         "https://www.royallondon.com/foo/",
         "https://www.royallondon.com/globalassets/img/hero-medium-1000x570.jpg"),
        ("page_url without trailing slash",
         "<style>.b{background-image: url(/globalassets/img/hero-medium-1000x570.jpg);}</style>",
         "https://www.royallondon.com/existing-customers/help-and-support/make-a-claim",
         "https://www.royallondon.com/globalassets/img/hero-medium-1000x570.jpg"),
        ("png extension",
         "<style>.b{background-image: url(/globalassets/img/hero-medium-1000x570.png);}</style>",
         "https://www.royallondon.com/foo/",
         "https://www.royallondon.com/globalassets/img/hero-medium-1000x570.png"),
        ("webp extension",
         "<style>.b{background-image: url(/globalassets/img/hero-medium-1000x570.webp);}</style>",
         "https://www.royallondon.com/foo/",
         "https://www.royallondon.com/globalassets/img/hero-medium-1000x570.webp"),
        ("uppercase CSS property/value",
         "<style>.b{BACKGROUND-IMAGE: URL(/globalassets/img/hero-medium-1000x570.jpg);}</style>",
         "https://www.royallondon.com/foo/",
         "https://www.royallondon.com/globalassets/img/hero-medium-1000x570.jpg"),
        ("duplicate same image across media queries",
         """<style>
            @media (min-width:768px){.b{background-image: url(/globalassets/img/hero-medium-1000x570.jpg);}}
            @media (max-width:767px){.b{background-image: url(/globalassets/img/hero-medium-1000x570.jpg);}}
            </style>""",
         "https://www.royallondon.com/foo/",
         "https://www.royallondon.com/globalassets/img/hero-medium-1000x570.jpg"),
    ]

    for name, html, url, expected in tests:
        result = extract_base_image_url(html, url)
        status = "PASS" if result == expected else "FAIL"
        print(f"{status} - {name}")
        if status == "FAIL":
            print(f"       expected: {expected}")
            print(f"       got:      {result}")