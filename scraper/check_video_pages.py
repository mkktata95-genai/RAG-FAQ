"""
check_video_pages.py  v1.0.0
════════════════════════════════════════════════════════════════
Standalone DIAGNOSTIC script — does NOT modify or import the main
scraper (scrape_approved_urls_httpV1.py). Self-contained on purpose,
so it can be run in VDI independently of whether the main scraper
has the has_video fix deployed yet.

WHAT IT DOES
  1. Loads the approved-URLs Excel (same header-detection logic as
     the main scraper — URL / Title columns, case-insensitive).
  2. Fetches each page (plain requests.get(), no browser).
  3. Runs has_video detection (same 3-signal logic as the main
     scraper, WITH the Vimeo-signal fix already applied here).
  4. If has_video, tries to extract the actual video_url from the
     page's iframe (Vimeo primarily — the confirmed case — plus a
     few common secondary players as a bonus, see VIDEO_IFRAME_HOSTS).
  5. Writes an Excel file with one row per approved URL:
        url | title | has_video | video_url | detected_via | http_status | error

USAGE
  python check_video_pages.py --file Approved_URLs.xlsx
  python check_video_pages.py --file Approved_URLs.xlsx --limit 20   # quick sample
  python check_video_pages.py --file Approved_URLs.xlsx --output video_check_report.xlsx

OUTPUT
  video_check_report.xlsx (or --output path) in the current directory,
  plus a console summary (total pages, video pages, extraction hits).
════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from openpyxl import Workbook, load_workbook

# ── Config ──────────────────────────────────────────────────────
BATCH_SIZE = 5
BATCH_DELAY_SECONDS = 2
REQUEST_TIMEOUT_SECONDS = 20
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36 RLG-Aria-VideoCheck/1.0"
    ),
}

# ── has_video detection — same 3-signal logic as the main scraper,
# with the Vimeo fix already included (vimeoapi / vimeovideoblock /
# player.vimeo.com added to the CSS-signal list). ──
VIDEO_URL_SIGNALS = ["/webinars/", "/videos/", "/video/", "/webinar/"]
VIDEO_COLLECTION_SIGNALS = ["webinar", "video", "podcast"]
VIDEO_CSS_SIGNALS = [
    "video-player", "webinar-player", "brightcove-player", "bc-player",
    "vjs-tech", "kaltura-player", "jwplayer", "data-video-id",
    "data-webinar-id", "data-brightcove",
    "vimeoapi", "vimeovideoblock", "player.vimeo.com",
]

# ── video_url extraction — iframe src hosts we know how to pull a
# direct link from. Vimeo is the confirmed, primary case; the other
# two are included as a bonus in case they show up, and are easy to
# drop if they turn out to be noise. ──
VIDEO_IFRAME_HOSTS = [
    "player.vimeo.com",
    "youtube.com/embed",
    "players.brightcove.net",
]


def detect_video_from_html(html: str, url: str) -> tuple[bool, str]:
    """
    Same logic/order as the main scraper's detect_video_from_html(),
    but also returns WHICH signal fired (for this diagnostic report
    only — the main scraper doesn't need this, it just needs True/False).

    Returns (has_video, detected_via) where detected_via is one of:
        "url_pattern", "collection_meta", "og_type", "css_signal", ""
    """
    url_lower = url.lower()
    for pattern in VIDEO_URL_SIGNALS:
        if pattern in url_lower:
            return True, "url_pattern"

    if not html:
        return False, ""

    try:
        soup = BeautifulSoup(html, "html.parser")

        collection_meta = (
            soup.find("meta", attrs={"name": "Collection_name"}) or
            soup.find("meta", attrs={"name": "collection_name"}) or
            soup.find("meta", property="Collection_name")
        )
        if collection_meta:
            collection_val = (collection_meta.get("content", "") or "").lower()
            for signal in VIDEO_COLLECTION_SIGNALS:
                if signal in collection_val:
                    return True, "collection_meta"

        og_type = soup.find("meta", property="og:type")
        if og_type and "video" in (og_type.get("content", "") or "").lower():
            return True, "og_type"

        html_lower = html.lower()
        for signal in VIDEO_CSS_SIGNALS:
            if signal in html_lower:
                return True, "css_signal"

    except Exception:
        pass

    return False, ""


def extract_video_url(html: str) -> str:
    """
    Pull the direct video iframe src, if one of the known
    VIDEO_IFRAME_HOSTS is present. Static HTML only — no JS needed
    (confirmed for Vimeo via the same investigation that confirmed
    the rest of the site needs no browser rendering).

    Returns "" if no matching iframe is found (has_video can still
    be True from a signal that isn't an iframe-based player, e.g. a
    URL-pattern or metadata match with no visible player markup —
    that's expected and fine, just means no video_url to extract for
    that page).
    """
    if not html:
        return ""
    try:
        soup = BeautifulSoup(html, "html.parser")
        for iframe in soup.find_all("iframe"):
            src = (iframe.get("src") or "").strip()
            if not src:
                continue
            for host in VIDEO_IFRAME_HOSTS:
                if host in src.lower():
                    return src.split("?")[0]  # strip tracking params, keep the id
    except Exception:
        pass
    return ""


# ── Excel loading — same header-detection approach as the main
# scraper (URL_HEADERS / TITLE_HEADERS), read-only, no writes back
# to the input file. ──
URL_HEADERS = {"url", "page url", "link", "webpage", "web page", "web url"}
TITLE_HEADERS = {"title", "page title", "name"}


def load_urls(excel_path: str) -> list[dict]:
    wb = load_workbook(excel_path, read_only=True)
    ws = wb.active

    header_row = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), ())
    headers = [str(h).strip().lower() if h is not None else "" for h in header_row]

    def find_col(candidates):
        for idx, h in enumerate(headers):
            if h in candidates:
                return idx
        return None

    url_idx = find_col(URL_HEADERS)
    title_idx = find_col(TITLE_HEADERS)

    if url_idx is None:
        wb.close()
        raise ValueError(
            f"No URL column found in {excel_path!r}. "
            f"Expected one of {sorted(URL_HEADERS)}. Headers seen: {header_row!r}"
        )

    seen, rows = set(), []
    for row in ws.iter_rows(min_row=2, values_only=True):
        if not row or len(row) <= url_idx:
            continue
        url = str(row[url_idx]).strip() if row[url_idx] else ""
        if not url.startswith("http"):
            continue
        norm = url.rstrip("/").lower()
        if norm in seen:
            continue
        seen.add(norm)
        title = (
            str(row[title_idx]).strip()
            if title_idx is not None and len(row) > title_idx and row[title_idx]
            else ""
        )
        rows.append({"url": url, "title": title})

    wb.close()
    return rows


def fetch_html(url: str) -> tuple[str | None, int | None, str | None]:
    try:
        resp = requests.get(url, headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT_SECONDS)
        return resp.text, resp.status_code, None
    except requests.exceptions.RequestException as e:
        return None, None, str(e)


def check_page(page_info: dict, index: int, total: int) -> dict:
    url, title = page_info["url"], page_info["title"]
    print(f"[{index}/{total}] checking {url}", flush=True)

    html, status_code, err = fetch_html(url)

    if err is not None:
        return {"url": url, "title": title, "has_video": "no", "video_url": "",
                "detected_via": "", "http_status": "", "error": err}

    if status_code and status_code >= 400:
        return {"url": url, "title": title, "has_video": "no", "video_url": "",
                "detected_via": "", "http_status": status_code, "error": f"HTTP {status_code}"}

    has_video, detected_via = detect_video_from_html(html, url)
    video_url = extract_video_url(html) if has_video else ""

    return {
        "url": url, "title": title,
        "has_video": "yes" if has_video else "no",
        "video_url": video_url,
        "detected_via": detected_via,
        "http_status": status_code,
        "error": "",
    }


def run(excel_path: str, output_path: str, limit: int | None = None):
    pages = load_urls(excel_path)
    if limit:
        pages = pages[:limit]
    total = len(pages)
    print(f"\nLoaded {total} URLs from {excel_path}\n")

    results = []
    for batch_start in range(0, total, BATCH_SIZE):
        batch = pages[batch_start:batch_start + BATCH_SIZE]
        with ThreadPoolExecutor(max_workers=BATCH_SIZE) as executor:
            futures = [
                executor.submit(check_page, p, batch_start + i + 1, total)
                for i, p in enumerate(batch)
            ]
            results.extend(f.result() for f in futures)
        if batch_start + BATCH_SIZE < total:
            time.sleep(BATCH_DELAY_SECONDS)

    # ── Write output Excel ──
    wb = Workbook()
    ws = wb.active
    ws.title = "video_check"
    headers = ["url", "title", "has_video", "video_url", "detected_via", "http_status", "error"]
    ws.append(headers)
    for r in results:
        ws.append([r[h] for h in headers])
    wb.save(output_path)

    # ── Summary ──
    video_pages = [r for r in results if r["has_video"] == "yes"]
    extracted = [r for r in video_pages if r["video_url"]]
    failed = [r for r in results if r["error"]]

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"Total URLs checked:        {total}")
    print(f"Pages with has_video=yes:  {len(video_pages)}")
    print(f"  → video_url extracted:   {len(extracted)}")
    print(f"  → no iframe match:       {len(video_pages) - len(extracted)}  (has_video true, no known iframe host found)")
    print(f"Fetch failures:            {len(failed)}")
    print(f"\nReport saved to: {output_path}")

    if video_pages:
        print("\nBy detection signal:")
        from collections import Counter
        c = Counter(r["detected_via"] for r in video_pages)
        for signal, count in c.most_common():
            print(f"  {signal}: {count}")


def main():
    parser = argparse.ArgumentParser(description="Check approved URLs for video content + extract video_url")
    parser.add_argument("--file", required=True, help="Path to approved URLs Excel file")
    parser.add_argument("--output", default="video_check_report.xlsx", help="Output Excel path")
    parser.add_argument("--limit", type=int, default=None, help="Only check first N URLs (quick sample)")
    args = parser.parse_args()

    if not Path(args.file).exists():
        print(f"ERROR: file not found: {args.file}")
        sys.exit(1)

    run(args.file, args.output, args.limit)


if __name__ == "__main__":
    main()