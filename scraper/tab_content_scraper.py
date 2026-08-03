"""
tab_content_scraper.py  v2.0.0
================================
Detects and scrapes content hidden behind prose tabs (Invest | Engage |
Embed style) that the standard crawler.arun() call misses — it only
captures the default/active tab on page load.

ARCHITECTURE — mirrors _scrape_dropdown_states_playwright() EXACTLY
-----------------------------------------------------------------------
This is not just "inspired by" the dropdown pattern — it is built to
slot into scrape_page() the same way, with the same call signature
shape, so integration is a copy-paste-adjacent addition, not a rewrite:

  - SYNC Playwright (not async) — run via ThreadPoolExecutor from the
    asyncio event loop, exactly like the dropdown scraper is invoked.
  - Detection happens on already-fetched HTML via BeautifulSoup —
    zero extra network calls, mirrors _has_routing_dropdowns_in_html().
  - Output entries are full page_data-shaped dicts carrying every
    field the dropdown entries carry (scraper_version, content_hash,
    audience, has_video, etc.) so chunk_and_index_hqaV4.py needs no
    special-casing — it already knows how to index dropdown_state
    entries, and tab_state entries follow the identical shape.
  - New field `tab_state` (parallel to `dropdown_state`) distinguishes
    tab entries. `dropdown_state` stays absent on tab entries and vice
    versa — the two mechanisms are mutually exclusive per page in
    practice, but nothing prevents both existing in the schema.

INTEGRATION POINT in scrape_page() (v4.8.0, after line ~2422)
-----------------------------------------------------------------
Insert AFTER the existing dropdown check block, using the same
raw_html already extracted at that point:

    if has_content_tabs_in_html(raw_html):
        try:
            loop     = asyncio.get_event_loop()
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            tab_states = await loop.run_in_executor(
                executor,
                scrape_tab_states_playwright,
                url,
                page_data["title"],
                page_data,
            )
        except Exception as _te:
            log.warning("playwright_tabs_skipped", url=url, error=str(_te))
            tab_states = []

        if tab_states:
            log.info("multi_tab_page_scraped", url=url, tab_count=len(tab_states))
            # tab_states already includes the default/active tab (relabelled,
            # not re-scraped) — this REPLACES page_data rather than
            # prepending it, unlike the dropdown case. See rationale below.
            return tab_states

    return page_data

WHY TABS REPLACE page_data BUT DROPDOWNS PREPEND IT
-------------------------------------------------------
Dropdown base page = genuine intro content BEFORE the dropdown widget.
That intro is real, distinct content — kept as its own entry.

Tab pages have NO content outside the tabs — the entire visible body
IS tab content. Keeping page_data (which contains only the default
tab's text, unlabelled) alongside tab_state entries (which include
that SAME default tab, now labelled) would duplicate it in the index.
So: replace, don't prepend.

CHANGELOG
---------
v2.0.0 — Full rewrite to match _scrape_dropdown_states_playwright()
          architecture exactly: sync Playwright, executor invocation,
          full page_data-shaped output dicts. Detection via
          BeautifulSoup on already-fetched HTML (zero extra calls).
v1.0.0 — Initial async-based draft (superseded — did not match the
          actual scraper's sync Playwright + executor pattern).
"""

import hashlib
from datetime import datetime, timezone

from bs4 import BeautifulSoup

from site_config import SITE_CONFIG


# ── Detection (BeautifulSoup on already-fetched HTML, zero network calls) ────

def _is_cookie_parent(element) -> bool:
    """Walk up parents checking for cookie/consent banner signals."""
    signals = SITE_CONFIG["cookie_banner_parent_signals"]
    parent = element
    for _ in range(6):
        parent = getattr(parent, "parent", None)
        if parent is None:
            break
        cls = " ".join(parent.get("class", []) or []).lower()
        pid = (parent.get("id") or "").lower()
        if any(sig in cls + " " + pid for sig in signals):
            return True
    return False


def _is_cookie_label(label: str) -> bool:
    lower = label.lower()
    return any(sig in lower for sig in SITE_CONFIG["cookie_banner_label_signals"])


def _extract_content_tab_labels(soup: BeautifulSoup) -> list:
    """
    Return ordered, de-duplicated list of genuine content tab labels
    found in the page. Empty/single-item result means no tab handling
    needed by the caller.
    """
    all_tab_lists = []
    for sel in SITE_CONFIG["tab_list_selectors"]:
        all_tab_lists += soup.select(sel)

    content_tab_lists = [tl for tl in all_tab_lists if not _is_cookie_parent(tl)]
    if not content_tab_lists:
        return []

    exclude = set(SITE_CONFIG["tab_nav_exclude_labels"])
    min_len = SITE_CONFIG["tab_label_min_len"]
    max_len = SITE_CONFIG["tab_label_max_len"]

    labels = []
    for tl in content_tab_lists:
        items = []
        for sel in SITE_CONFIG["tab_item_selectors"]:
            found = tl.select(sel)
            if found:
                items = found
                break
        if not items:
            items = tl.find_all("a")

        for item in items:
            label = item.get_text(strip=True)
            if not (min_len < len(label) < max_len):
                continue
            if label.lower() in exclude:
                continue
            if _is_cookie_label(label):
                continue
            if label not in labels:
                labels.append(label)

    return labels


def _has_content_tabs_in_html(html: str) -> bool:
    """
    Check rendered HTML for genuine content tabs (2+ non-cookie,
    non-nav tab labels). Mirrors _has_routing_dropdowns_in_html()
    exactly — BeautifulSoup on already-fetched HTML, no extra calls.
    """
    if not html:
        return False
    try:
        soup   = BeautifulSoup(html, "html.parser")
        labels = _extract_content_tab_labels(soup)
        return len(labels) > 1
    except Exception:
        return False


# ── Sync Playwright tab click + scrape (executor-invoked) ───────────────────

def _scrape_tab_states_playwright(
    url:            str,
    base_title:     str,
    base_page_data: dict,
) -> list:
    """
    Click through each detected content tab and scrape its text.

    Runs SYNCHRONOUSLY — called via asyncio thread pool executor from
    scrape_page(), exactly like _scrape_dropdown_states_playwright().

    Returns list of page_data-shaped dicts, one per tab, each carrying
    every field base_page_data has (so downstream indexing code needs
    no special-casing), plus a new `tab_state` field holding the tab's
    label. Returns [] on any failure — caller falls back to returning
    the original page_data unchanged.

    The default/active tab is NOT re-scraped via click — we already
    have its text in base_page_data["content"] from the initial
    crawler.arun() call, so it's relabelled and reused as-is. This
    avoids a redundant page load for the tab we already captured.
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        print(
            "[tab_content_scraper] playwright not installed — "
            "pip install playwright && playwright install chromium"
        )
        return []

    results = []
    wait_ms    = SITE_CONFIG["tab_click_wait_ms"]
    timeout_ms = SITE_CONFIG["tab_click_timeout_ms"]

    try:
        with sync_playwright() as pw:
            import os as _os
            # Same PLAYWRIGHT_EXECUTABLE_PATH convention as the dropdown
            # scraper — read from environment so this module stays
            # import-independent of the main scraper file's globals.
            _exec = _os.getenv("PLAYWRIGHT_EXECUTABLE_PATH", "")
            _exec_arg = _exec if _exec and _os.path.exists(_exec) else None
            browser = pw.chromium.launch(
                headless=True,
                executable_path=_exec_arg,
            )
            try:
                page = browser.new_page()
                page.route(
                    "**/*.{png,jpg,jpeg,gif,svg,woff,woff2,ttf,eot}",
                    lambda route: route.abort(),
                )

                page.goto(url, wait_until="networkidle", timeout=45000)

                try:
                    page.wait_for_selector(
                        "main, article, [role='main']",
                        timeout=10000,
                    )
                except PWTimeout:
                    pass

                # Re-detect tab labels from live DOM (page.content() is
                # sync-Playwright's HTML snapshot — consistent with how
                # the dropdown function re-queries selects live rather
                # than trusting the crawl4ai-fetched HTML for clicking)
                soup   = BeautifulSoup(page.content(), "html.parser")
                labels = _extract_content_tab_labels(soup)

                if len(labels) <= 1:
                    return []

                # Determine which label is the default/active one so we
                # can reuse base_page_data["content"] for it without a
                # redundant click+scrape.
                active_el = (
                    soup.find(attrs={"role": "tab", "aria-selected": "true"}) or
                    soup.select_one(".nav-tabs .nav-link.active") or
                    soup.select_one(".nav-link.active")
                )
                active_label = (
                    active_el.get_text(strip=True) if active_el else labels[0]
                )

                for label in labels:
                    if label == active_label:
                        # Already have this content — relabel, don't re-scrape
                        entry = dict(base_page_data)
                        entry["tab_state"] = label
                        entry["content_hash"] = hashlib.sha256(
                            entry["content"].encode("utf-8")
                        ).hexdigest()
                        results.append(entry)
                        continue

                    try:
                        locator = page.get_by_text(label, exact=True)
                        locator.first.click(timeout=timeout_ms)
                        page.wait_for_timeout(wait_ms)

                        panel_text = _get_visible_panel_text_sync(page)
                        if not panel_text or len(panel_text.strip()) < 20:
                            print(
                                f"[tab_content_scraper] empty panel for "
                                f"'{label}' on {url} — skipped"
                            )
                            continue

                        content = panel_text.strip()

                        entry = dict(base_page_data)
                        entry["content"]        = content
                        entry["content_length"] = len(content)
                        entry["content_hash"]   = hashlib.sha256(
                            content.encode("utf-8")
                        ).hexdigest()
                        entry["title"]          = f"{base_title} — {label}"
                        entry["tab_state"]      = label
                        entry["scraped_at"]     = datetime.now(
                            timezone.utc
                        ).isoformat()
                        # url/parent_url stay identical to base_page_data —
                        # citations always resolve to the clean parent page,
                        # same convention as dropdown_state entries.
                        results.append(entry)

                    except Exception as e:
                        print(
                            f"[tab_content_scraper] tab click failed for "
                            f"'{label}' on {url}: {e}"
                        )
                        continue

            finally:
                browser.close()

    except Exception as e:
        print(f"[tab_content_scraper] playwright_tab_scrape_error {url}: {e}")
        return []

    return results


def _get_visible_panel_text_sync(page) -> str:
    """
    Extract text from the currently-visible tab panel (sync Playwright).
    Tries known panel selectors in order; falls back to main content area.
    """
    for sel in SITE_CONFIG["tab_panel_selectors"]:
        try:
            panels = page.query_selector_all(sel)
            for panel in panels:
                if panel.is_visible():
                    text = panel.inner_text()
                    if text and text.strip():
                        return text.strip()
        except Exception:
            continue

    try:
        main = page.query_selector("main, article, .content, #content")
        if main:
            return main.inner_text().strip()
    except Exception:
        pass

    return ""


# ── Public wrappers for integration into scrape_page() ──────────────────────

def has_content_tabs_in_html(html: str) -> bool:
    """
    Public wrapper — call this in scrape_page() right after the
    existing _has_routing_dropdowns_in_html(raw_html) check, using
    the same already-fetched raw_html variable.
    """
    return _has_content_tabs_in_html(html)


def scrape_tab_states_playwright(
    url:            str,
    base_title:     str,
    base_page_data: dict,
) -> list:
    """
    Public wrapper for the executor call in scrape_page(). Signature
    matches _scrape_dropdown_states_playwright() exactly so it can be
    passed to loop.run_in_executor() the same way.
    """
    return _scrape_tab_states_playwright(url, base_title, base_page_data)