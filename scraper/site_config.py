"""
site_config.py  v1.0.0
========================
Client-specific detection signals for scraper element detection.

WHY THIS FILE EXISTS
---------------------
Cookie banner classes, tab selectors, table selectors — these are all
specific to Royal London's current tech stack (Bootstrap-Vue frontend,
OneTrust cookie consent). If a new client is onboarded with a different
CMS/frontend, only THIS file needs to change — detection logic in
scrape_approved_urls_updatedV4.py stays the same.

WHAT STAYS GENERIC (do not move here)
---------------------------------------
- ARIA role-based detection (role="tablist", role="tab") — these are
  web standards, not RLG-specific, and already generalise reasonably
  well across sites that follow accessibility best practices.

WHAT IS RLG-SPECIFIC (lives here)
------------------------------------
- Cookie consent banner vendor signals (OneTrust)
- Bootstrap-Vue specific table/tab class names
- Citation domain restriction
- Contact-routing dropdown phone/contact signals

USAGE
-----
    from site_config import SITE_CONFIG
    signals = SITE_CONFIG["cookie_banner_parent_signals"]

CHANGELOG
---------
v1.0.0 — Initial extraction of RLG-specific signals from inline
          detection logic. First step toward multi-client portability.
"""

SITE_CONFIG = {

    # ── Citation / domain restriction ──────────────────────────────────────
    "citation_domain": "royallondon.com",

    # ── Cookie / consent banner detection ──────────────────────────────────
    # Parent element class/id signals — skip any tablist/element found
    # inside a container matching these (OneTrust is RLG's current vendor)
    "cookie_banner_parent_signals": (
        "cookie", "consent", "privacy", "gdpr", "cmp", "onetrust"
    ),

    # Label text signals — skip any tab/element whose text matches these
    "cookie_banner_label_signals": (
        "your privacy", "strictly necessary", "strictly necessary cookies",
        "performance cookies", "functional cookies", "targeting cookies",
        "always active", "cookie", "consent",
    ),

    # ── Content tab detection (ARIA-standard, kept broad intentionally) ────
    "tab_list_selectors": (
        '[role="tablist"]',
        '.nav-tabs',
        'ul.tabs',
    ),
    "tab_item_selectors": (
        '[role="tab"]',
        '.nav-tabs .nav-link',
        '.nav-tabs li a',
    ),
    "tab_panel_selectors": (
        '[role="tabpanel"]',
        '.tab-pane',
        '.tab-content > div',
    ),

    # Labels to exclude from tab detection (nav-style, not content-style)
    "tab_nav_exclude_labels": (
        "home", "about", "contact", "menu", "next", "back", "more"
    ),

    # Minimum/maximum label length to count as a genuine content tab
    "tab_label_min_len": 3,
    "tab_label_max_len": 100,

    # ── JS-paginated table detection (Bootstrap-Vue specific) ──────────────
    "btable_selectors": (
        'table[initialpagesize]',
        'table.b-table',
        '.fl-fund-list__table',
    ),
    "pagination_button_selectors": (
        '[aria-label*="page"]',
        'nav[aria-label*="pagination"] li',
    ),

    # ── Contact-routing dropdown signals (existing 3-layer filter) ─────────
    "contact_signals": (
        "call us", "call:", "phone:", "tel:", "write to us",
        "contact us on", "speak to", "get in touch",
    ),

    # ── Playwright wait/timeout config for tab clicks ───────────────────────
    "tab_click_wait_ms": 800,       # wait after click before scraping panel
    "tab_click_timeout_ms": 15000,  # max wait for tab panel to render
}