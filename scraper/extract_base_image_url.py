import re
from urllib.parse import urljoin
from typing import Optional

# ---------------------------------------------------------------------------
# STEP 1 — locate the actual banner CONTAINER, not the whole page.
# Both templates seen so far anchor their banner block with an id like:
#   id="featurebanner9066"        (CSS background-image template)
#   id="herobannerblock428217"    (<picture><source> template)
# We scope all extraction to a window starting at this anchor, ending at the
# next top-level "container" div (a reliable section boundary in this CMS),
# so we never leak into unrelated later blocks on the page — e.g. the
# "More articles" related-content card thumbnails, which also use
# background-image but are NOT the page's own image.
# ---------------------------------------------------------------------------
_BANNER_ANCHOR_RE = re.compile(
    r'id=["\'](?:feature|hero)banner(?:block)?\d+["\']', re.IGNORECASE
)

_DECLARATION_RE = re.compile(r'background-image:\s*([^;]+);', re.IGNORECASE)
_URL_RE = re.compile(
    r'url\(\s*[\'"]?([^\'")]+\.(?:jpg|jpeg|png|webp))[\'"]?\s*\)',
    re.IGNORECASE,
)
_SOURCE_TAG_RE = re.compile(r'<source\b[^>]*>', re.IGNORECASE)
_SRCSET_RE = re.compile(
    r'srcset=["\']([^"\']+\.(?:jpg|jpeg|png|webp))["\']', re.IGNORECASE
)

SIZE_TIER_PREFERENCE = ["medium-1000x570", "small-800x800", "large-1200x700"]

_WINDOW_FALLBACK_SIZE = 8000  # chars, only used if no container boundary found


def _get_banner_window(html: str) -> Optional[str]:
    anchor = _BANNER_ANCHOR_RE.search(html)
    if not anchor:
        return None
    start = anchor.end()
    # skip past the anchor's own opening div before looking for the NEXT
    # container boundary, so we don't immediately match the same div.
    next_container = html.find('<div class="container', start + 100)
    end = next_container if next_container != -1 else start + _WINDOW_FALLBACK_SIZE
    return html[start:end]


def _extract_from_picture(window: str) -> Optional[str]:
    """
    <picture><source srcset="..."> hero pattern. Sources without a `media`
    attribute are the default/base image (typically the largest, desktop
    version); sources WITH `media` are breakpoint-specific overrides.
    Prefer the no-media (base) source.
    """
    candidates = []
    for tag in _SOURCE_TAG_RE.findall(window):
        srcset_match = _SRCSET_RE.search(tag)
        if not srcset_match:
            continue
        has_media = "media=" in tag.lower()
        candidates.append((srcset_match.group(1), has_media))

    if not candidates:
        return None

    for path, has_media in candidates:
        if not has_media:
            return path
    return candidates[0][0]


def _extract_from_css_banner(window: str) -> Optional[str]:
    """
    CSS feature-banner background-image pattern. Isolates the full
    "background-image: ...;" declaration and takes the LAST url(...) in it
    (handles linear-gradient overlays with nested rgba() parens correctly).
    """
    matches = []
    seen = set()
    for decl_match in _DECLARATION_RE.finditer(window):
        declaration = decl_match.group(1)
        urls_in_declaration = _URL_RE.findall(declaration)
        if not urls_in_declaration:
            continue
        rel_path = urls_in_declaration[-1]
        if rel_path not in seen:
            seen.add(rel_path)
            matches.append(rel_path)

    if not matches:
        return None

    for tier in SIZE_TIER_PREFERENCE:
        for rel_path in matches:
            if tier in rel_path:
                return rel_path
    return matches[0]


def extract_base_image_url(html: str, page_url: str) -> Optional[str]:
    """
    Extract the page-specific citation thumbnail image URL from a scraped
    page's HTML.

    Scoped to the actual banner container (id="featurebanner*" or
    "herobannerblock*") so unrelated background-image usages elsewhere on
    the page (e.g. related-article thumbnail cards) are never picked up.
    Supports two hero templates seen on this site:
      1. <picture><source srcset="..."> (newer template)
      2. CSS background-image: url(...) on the feature-banner div (older template)

    Returns None if no banner container is found on the page, or if the
    banner container has no extractable image — does NOT fall back to
    scanning the rest of the page.
    """
    window = _get_banner_window(html)
    if window is None:
        return None

    picture_result = _extract_from_picture(window)
    if picture_result:
        return urljoin(page_url, picture_result)

    css_result = _extract_from_css_banner(window)
    if css_result:
        return urljoin(page_url, css_result)

    return None


if __name__ == "__main__":
    tests = []

    # --- Original CSS background-image template (GEC page) ---
    css_html = """
    <div class="container featured">
      <div class="row feature-banners"><div class="featurebannerblock full col-md-12" id="featurebanner9066">
      <style>
        .featurebanner-split #feature-banner-group-executive-committee_9066 {
            background-image: url(/globalassets/all-site-images/brand-photography/people-and-moments/business-people-meeting-in-modern-office-medium-1000x570.jpg);
        }
      </style>
      </div></div>
    </div>
    <div class="container productpage">More page content here</div>
    """
    tests.append((
        "CSS background-image template still works",
        css_html,
        "https://www.royallondon.com/about-us/group-executive-committee/",
        "https://www.royallondon.com/globalassets/all-site-images/brand-photography/people-and-moments/business-people-meeting-in-modern-office-medium-1000x570.jpg",
    ))

    # --- New <picture><source> hero template (Using your pension page) ---
    picture_html = """
    <div class="container featured">
      <div class="row feature-banners"><div class="herobannerblock full col-md-12" id="herobannerblock428217">
      <header class="hero hero--no-margin" role="banner">
        <div class="hero-banner hero-banner--theme-primary">
          <div class="hero-banner__image">
            <picture>
              <source srcset="/globalassets/all-site-images/brand-photography/hero-banner-block-images/solid-images/man-at-home-with-his-dog-looking-at-phone-4.jpg">
              <source srcset="/globalassets/all-site-images/brand-photography/hero-banner-block-images/solid-images/tablet-992x558-man-at-home-with-his-dog-looking-at-phone.jpg" media="(min-width: 62rem)">
              <source srcset="/globalassets/all-site-images/brand-photography/hero-banner-block-images/solid-images/mobile-768x432-man-at-home-with-his-dog-looking-at-phone.jpg" media="(max-width: 48rem)">
              <img loading="eager" />
            </picture>
          </div>
        </div>
      </header>
      </div></div>
    </div>
    <div class="container productpage">
      <div class="featuredcontentblock" id="featuredcontentcontainer304848">
        <div class="featured-content-image_background" style="background-image: url(/globalassets/all-site-images/brand-photography/teaser-images/working-from-home-life-350x200.jpg);"></div>
        <div class="featured-content-image_background" style="background-image: url(/globalassets/all-site-images/brand-photography/teaser-images/coffee-cup-350x200.jpg);"></div>
      </div>
    </div>
    """
    tests.append((
        "picture/source hero template - picks real hero, not related-article thumbnails",
        picture_html,
        "https://www.royallondon.com/retirement-planning/using-pension/",
        "https://www.royallondon.com/globalassets/all-site-images/brand-photography/hero-banner-block-images/solid-images/man-at-home-with-his-dog-looking-at-phone-4.jpg",
    ))

    # --- Second real page: cash-lump-sums. Different variant of the same
    # <picture> template — ALL THREE sources have a media attribute (no
    # bare/default source this time), which exercises the "take first found"
    # fallback branch instead of the "prefer no-media" branch. Also confirms
    # the unrelated "rugby-teaser" thumbnail from the later "More articles"
    # section (in a separate container) is correctly excluded by the window
    # scoping, not accidentally picked up.
    cash_lump_html = """
    <div class="container featured">
    <div class="row feature-banners"><div class="herobannerblock full col-md-12" id="herobannerblock427193">
    <header class="hero hero--no-margin" role="banner" data-swiftype-index="true">
    <div class="hero-banner hero-banner--theme-primary  hero-banner--no-nav">
    <div class="hero-banner__image hero-banner__image--fit-center">
    <picture>
    <source srcset="/globalassets/all-site-images/brand-photography/hero-banner-block-images/solid-images/group-of-ladies-drinking-tea/desktop-1400x700-group-of-ladies-drinking-tea-1.jpg" media="(min-width: 62rem)" />
    <source srcset="/globalassets/all-site-images/brand-photography/hero-banner-block-images/solid-images/group-of-ladies-drinking-tea/tablet-992x558-group-of-ladies-drinking-tea-1.jpg" media="(min-width: 48rem)" />
    <source srcset="/globalassets/all-site-images/brand-photography/hero-banner-block-images/solid-images/group-of-ladies-drinking-tea/mobile-768x432-group-of-ladies-drinking-tea-1.jpg" media="(min-width: 0rem)" />
    <img loading="eager" />
    </picture>
    </div>
    </div>
    <div class="hero-banner__content">
    <h1 class="hero-banner__content_heading">Take a pension lump sum</h1>
    </div>
    <div class="hero-banner__background"></div>
    </div>
    </header></div></div>
    </div>
    <div class="container productpage">
    <div class="featuredcontentblock full col-md-12" id="featuredcontentcontainer305066">
    <div class="container">
    <div class="featured-content-image_background" style="background-image: url(/globalassets/all-site-images/brand-photography/teaser-images/rugby-teaser-350x200.jpg);"></div>
    <div class="featured-content-image_background" style="background-image: url(/globalassets/all-site-images/brand-photography/teaser-images/newspaper-and-glasses-350x200.jpg);"></div>
    </div>
    </div>
    </div>
    """
    tests.append((
        "cash-lump-sums page - all sources have media attr, picks first (ladies), not rugby-teaser",
        cash_lump_html,
        "https://www.royallondon.com/retirement-planning/using-pension/cash-lump-sums/",
        "https://www.royallondon.com/globalassets/all-site-images/brand-photography/hero-banner-block-images/solid-images/group-of-ladies-drinking-tea/desktop-1400x700-group-of-ladies-drinking-tea-1.jpg",
    ))


    # --- No banner container at all ---
    tests.append((
        "no banner container anywhere - returns None, does not scan rest of page",
        '<div class="container productpage"><div style="background-image:url(/foo/bar.jpg);"></div></div>',
        "https://www.royallondon.com/foo/",
        None,
    ))

    passed = 0
    for name, html, url, expected in tests:
        result = extract_base_image_url(html, url)
        status = "PASS" if result == expected else "FAIL"
        if status == "PASS":
            passed += 1
        print(f"{status} - {name}")
        if status == "FAIL":
            print(f"       expected: {expected}")
            print(f"       got:      {result}")
    print(f"\n{passed}/{len(tests)} tests passed")