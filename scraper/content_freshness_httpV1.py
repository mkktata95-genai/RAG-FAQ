"""
Royal London Digital Assistance — Content Freshness Manager (HTTP, no HQA)
============================================================================
Rebuild of content_freshnessV1.py against the new no-Playwright pipeline
(scrape_approved_urls_httpV1.py + chunk_embed_index_v1.py). See project
discussion log for the full list of decisions:
  - Scraping: HTTP + BeautifulSoup, mirrors scrape_approved_urls_httpV1.py
    exactly (no crawl4ai/Playwright/CDP — dropped entirely).
  - HQA: dropped entirely (matches chunk_embed_index_v1.py).
  - Index: single index only (no dual main/baseline split).
  - Redis: cache-invalidation step dropped entirely.
  - thumbnail_url: always "" — matches scraper (image logic not finalized).
  - Standalone: zero cross-file imports, same principle as every other
    script in this pipeline family. Functions duplicated, not imported.

THIS FILE (WIP): hashing-only slice, built and verified FIRST per explicit
instruction — validate the hash chain against a real fixture before writing
the full scrape/diff/apply orchestration.

CRITICAL — the two-stage hash chain (found by cross-checking the already-
built scripts, not carried over from the old file's assumptions):

    fetch_html
      -> extract_main_html            (BeautifulSoup, strip nav/header/etc.)
      -> html_fragment_to_markdown    (markdownify)
      -> clean_scraped_content        (scraper's clean_content() — boilerplate
                                        strip; renamed here to avoid a name
                                        clash with the URL-stripper below)
      -> clean_content                (chunk_embed_index_v1.py's clean_content()
                                        — external-URL stripper + whitespace
                                        collapse — this is a SECOND pass, and
                                        chunk_embed_index_v1.py's chunk_pages()
                                        applies it AGAIN on top of the
                                        scraper's already-cleaned content
                                        before hashing — so the hash actually
                                        stored in the index is
                                        compute_content_hash(clean_content(
                                        clean_scraped_content(raw))), not just
                                        the scraper's own content_hash field)
      -> compute_content_hash         (SHA-256)

Skipping the second clean_content() pass reproduces the exact class of bug
that content_freshnessV1.py's v1.7.9 entry documents (formula mismatch
between what freshness computes and what the indexer actually stores).

NOT NEEDED (confirmed by inspection, not carried over from the old file):
  - dropdown truncate-before-hash (old v1.7.10) — the new scraper extracts
    dropdown states cleanly via BeautifulSoup into separate page dicts, it
    never blends multiple panels into one blob, so there is nothing to
    truncate before hashing.
"""

from __future__ import annotations

import hashlib
import re

from bs4 import BeautifulSoup
from markdownify import markdownify as _markdownify

CONTENT_SELECTOR = "main, article, .content, #content, .page-content, .main-content, [role='main']"
EXCLUDED_TAGS = ["nav", "header", "footer", "aside", "script", "style", "noscript"]


# ═══════════════════════════════════════════════════════════════
# Stage 1-3: HTML -> markdown -> boilerplate-stripped
# (byte-identical port of scrape_approved_urls_httpV1.py)
# ═══════════════════════════════════════════════════════════════

def extract_main_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag_name in EXCLUDED_TAGS:
        for tag in soup.find_all(tag_name):
            tag.decompose()
    container = soup.select_one(CONTENT_SELECTOR)
    if container is None:
        container = soup.body or soup
    return str(container)


def html_fragment_to_markdown(html_fragment: str) -> str:
    return _markdownify(html_fragment, strip=["img"], heading_style="ATX")


def _remove_duplicate_content(content: str) -> str:
    h1_pattern = re.compile(r'^#{1,2}\s+\S', re.MULTILINE)
    matches = list(h1_pattern.finditer(content))
    if len(matches) < 2:
        return content
    second_pos = matches[1].start()
    content_len = len(content)
    if second_pos < content_len * 0.40:
        return content
    first_chunk = content[:second_pos].strip()
    second_chunk = content[second_pos:].strip()
    first_words = set(first_chunk.split()[:200])
    second_words = set(second_chunk.split()[:200])
    if not first_words:
        return content
    overlap_pct = len(first_words & second_words) / len(first_words)
    if overlap_pct > 0.60:
        return first_chunk
    return content


def clean_scraped_content(content: str) -> str:
    """
    Scraper's boilerplate/dedup stripper. Renamed from the scraper's
    clean_content() to avoid colliding with the indexer's clean_content()
    (URL-stripper) below — both are needed here, in sequence.
    """
    content = _remove_duplicate_content(content)

    content = re.sub(r'^\s*\d+\.\s*\[.*?\]\(.*?\)\s*>\s*$', '', content, flags=re.MULTILINE)
    content = re.sub(r'^\s*\d+\.\s*\[.*?\]\(.*?\)\s*$', '', content, flags=re.MULTILINE)
    content = re.sub(r'^\s*\d+\.\s+[A-Z][^\n]{3,60}$', '', content, flags=re.MULTILINE)

    content = re.sub(r'Share\s*\n(\s*\*\s*(\[?\s*\]?\([^\)]*\))?\s*\n)+', '', content)
    content = re.sub(r'^\s*\*\s*\[?\s*\]?\(\s*[^\)]{0,10}\)\s*$', '', content, flags=re.MULTILINE)
    content = re.sub(r'^Share\s*$', '', content, flags=re.MULTILINE)

    content = re.sub(r'\[?\s*\]?\(https://twitter\.com/intent/tweet[^\)]*\)\s*', '', content)

    content = re.sub(
        r'https://www\.(facebook|instagram|linkedin|x|youtube|twitter)\.com/\S+', '', content,
    )

    content = re.sub(r'\[\s*\]\(\s*\)', '', content)
    content = re.sub(r'^\s*\*\s*\[\s*\]\s*$', '', content, flags=re.MULTILINE)

    content = re.sub(r'^(Previous Item|Next Item)\s*$', '', content, flags=re.MULTILINE | re.IGNORECASE)

    content = re.sub(r'Your browser is not supported\..*?×\s*', '', content, flags=re.DOTALL)
    content = re.sub(r'#{1,3}\s*Connect with us.*$', '', content, flags=re.DOTALL | re.MULTILINE)
    content = re.sub(r'#{1,3}\s*Products and services.*$', '', content, flags=re.DOTALL | re.MULTILINE)
    content = re.sub(r'#{1,3}\s*About Royal London.*$', '', content, flags=re.DOTALL | re.MULTILINE)
    content = re.sub(r'#{1,3}\s*Useful links.*$', '', content, flags=re.DOTALL | re.MULTILINE)
    content = re.sub(r'\*\*The Royal London Mutual Insurance.*$', '', content, flags=re.DOTALL | re.MULTILINE)
    content = re.sub(r'©\s*Royal London \d{4}.*$', '', content, flags=re.DOTALL | re.MULTILINE)
    content = re.sub(r'\[Back to top\].*?\n', '', content)

    content = re.sub(r'\n{3,}', '\n\n', content)
    content = re.sub(r'[ \t]+\n', '\n', content)
    content = re.sub(r'\n[ \t]+\n', '\n\n', content)

    return content.strip()


# ═══════════════════════════════════════════════════════════════
# Stage 4: URL-stripper (byte-identical port of chunk_embed_index_v1.py's
# clean_content()) — second pass, applied on top of clean_scraped_content()
# ═══════════════════════════════════════════════════════════════

def clean_content(text: str) -> str:
    def replace_markdown_link(match):
        anchor_text, url = match.group(1), match.group(2)
        return match.group(0) if "royallondon.com" in url else anchor_text

    text = re.sub(r'\[([^\]]+)\]\((https?://[^\)]+)\)', replace_markdown_link, text)

    def replace_raw_url(match):
        url = match.group(0)
        return url if "royallondon.com" in url else ""

    text = re.sub(r'https?://[^\s\)\]"\'<>,]+', replace_raw_url, text)
    text = re.sub(r'  +', ' ', text)
    return text.strip()


# ═══════════════════════════════════════════════════════════════
# Stage 5: hash
# ═══════════════════════════════════════════════════════════════

def compute_content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def hash_from_raw_html(html: str) -> str:
    """
    Full chain, single entry point: raw page HTML -> the exact hash that
    would end up stored in the index for this page's content. This is
    what content_freshness's scrape-and-compare step calls per URL.
    """
    main_html = extract_main_html(html)
    markdown = html_fragment_to_markdown(main_html)
    scraped_clean = clean_scraped_content(markdown)
    indexer_clean = clean_content(scraped_clean)
    return compute_content_hash(indexer_clean)  