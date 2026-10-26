#!/usr/bin/env python3
"""
generate_synthetic_dataset.py  v1.0.2
Synthetic retrieval-eval dataset generator (Royal London products/services focus).

Reads the scraper JSON (page text), NOT the index chunks and NOT augmented_questions
(no circularity). Produces ~30 rows: single-page, multi-page, unanswerable.
Output schema is identical to the future SME dataset, so swapping is just --dataset.

Usage (VDI, .venv Python 3.11):
  1) Inspect page selection first (no LLM calls):
       python generate_synthetic_dataset.py --input <scraper.json|dir> --list-pages
  2) Generate:
       python generate_synthetic_dataset.py --input <scraper.json|dir> --out out_dir

Env (read from a .env file at the repo root, or the shell; see .env.example):
  AZURE_OPENAI_ENDPOINT, DEPLOYMENT_MAIN (or --deployment), AZURE_OPENAI_API_VERSION (optional)
Auth: DefaultAzureCredential only. No API keys.

Changelog (newest first)
  v1.0.2  Exclude hub/landing pages (fewer than 2 path segments, e.g. homepage, /insurance).
          Rollback: git revert the commit.
  v1.0.1 Load .env automatically (python-dotenv); warn if .env exists but python-dotenv missing.
          Rollback: git revert the commit.
  v1.0.0  Initial version. Rollback: delete file.
"""
from __future__ import annotations

import argparse
import csv
import difflib
import json
import logging
import os
import random
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

LOG = logging.getLogger("synthetic_gen")

# --------------------------------------------------------------------------- config
URL_KEYS = ["source_url", "url", "page_url", "link"]
TITLE_KEYS = ["title", "page_title", "name", "heading"]
CONTENT_KEYS = ["content", "clean_content", "text", "markdown", "body", "page_content", "raw_content"]
CATEGORY_KEYS = ["category", "product_category", "section", "topic"]
LIST_KEYS = ["pages", "data", "items", "documents", "records", "results"]

# Corporate / non-product pages are excluded (URL or title match).
DENY_URL = re.compile(
    r"(about-us|/about/|leadership|board|executive|management-team|our-people|meet-the|/ceo|/coo|"
    r"careers|jobs|/media|press|/news|investor|governance|modern-slavery|sustainab|annual-report|"
    r"csr|cookie|privacy|terms|accessibility|sitemap|corporate|our-history|our-purpose)",
    re.I,
)
DENY_TITLE = re.compile(
    r"(chief executive|chief operating|\bceo\b|\bcoo\b|our people|meet the|board of directors|"
    r"leadership team|careers|press release|annual report)",
    re.I,
)
PRODUCT_TERMS = [
    "pension", "isa", "annuity", "drawdown", "retire", "equity release", "lifetime mortgage",
    "protection", "life insurance", "life cover", "critical illness", "income protection",
    "bereavement", "death", "investment", "fund", "sipp", "savings", "transfer", "consolidat",
    "tax-free", "allowance", "will", "probate", "claim", "premium", "cover", "withdraw",
    "contribution", "workplace", "inheritance", "estate", "beneficiar",
]
BANNED_Q = re.compile(
    r"(\bceo\b|chief executive|\bcoo\b|board of directors|the page|the article|the text|"
    r"according to (the|this)|this page|provided (text|content))",
    re.I,
)
STOP = set(
    "the a an and or of to in for on with is are be by at as it that this from your you we our can will "
    "if not have has how what when who which more about royal london their they there".split()
)
MAX_CHARS = 6000
MIN_CHARS = 600


class LLMError(RuntimeError):
    pass


# --------------------------------------------------------------------------- helpers
def normalise_url(url: str) -> str:
    """Lowercase scheme/host, drop query/fragment, strip trailing slash. (Redirects handled separately.)"""
    try:
        p = urlsplit(url.strip())
        path = p.path.rstrip("/") or ""
        return urlunsplit((p.scheme.lower(), p.netloc.lower(), path, "", ""))
    except Exception:
        LOG.exception("normalise_url failed for %r", url)
        raise


def norm_text(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", s.lower())).strip()


def clean_content(s: str) -> str:
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", s)          # images
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)        # links -> text
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def pick(rec: dict, keys: list[str]) -> Any:
    lower = {str(k).lower(): v for k, v in rec.items()}
    for k in keys:
        v = lower.get(k)
        if v not in (None, "", []):
            return v
    return None


def extract_records(obj: Any) -> list[dict]:
    if isinstance(obj, list):
        return [r for r in obj if isinstance(r, dict)]
    if isinstance(obj, dict):
        for k in LIST_KEYS:
            if isinstance(obj.get(k), list):
                return [r for r in obj[k] if isinstance(r, dict)]
        # dict keyed by url
        vals = list(obj.values())
        if vals and all(isinstance(v, dict) for v in vals):
            out = []
            for k, v in obj.items():
                v = dict(v)
                v.setdefault("url", k)
                out.append(v)
            return out
    return []


def load_pages(path: str) -> list[dict]:
    p = Path(path)
    files = sorted(p.rglob("*.json*")) if p.is_dir() else [p]
    if not files:
        raise FileNotFoundError(f"No JSON files found at {path}")
    raw: list[dict] = []
    for f in files:
        try:
            text = f.read_text(encoding="utf-8")
            if f.suffix == ".jsonl":
                recs = [json.loads(line) for line in text.splitlines() if line.strip()]
            else:
                recs = extract_records(json.loads(text))
            LOG.info("Loaded %d records from %s", len(recs), f)
            raw.extend(recs)
        except Exception:
            LOG.exception("Failed to read %s (skipped)", f)
    pages, seen = [], set()
    for r in raw:
        url, content = pick(r, URL_KEYS), pick(r, CONTENT_KEYS)
        if not url or not content or not isinstance(content, str):
            continue
        nurl = normalise_url(str(url))
        if nurl in seen:
            continue
        seen.add(nurl)
        title = str(pick(r, TITLE_KEYS) or "")
        cat = pick(r, CATEGORY_KEYS)
        if not cat:
            segs = [s for s in urlsplit(nurl).path.split("/") if s]
            cat = segs[0] if segs else "general"
        pages.append(
            {"url": nurl, "title": title, "category": str(cat).lower(), "content": clean_content(content)}
        )
    if not pages:
        raise ValueError(
            "No usable pages. Expected url/content fields; found keys: "
            + str(sorted({k for r in raw[:5] for k in r}))
        )
    return pages


def product_score(pg: dict) -> int:
    head = (pg["url"] + " " + pg["title"]).lower()
    body = pg["content"][:MAX_CHARS].lower()
    s = sum(3 for t in PRODUCT_TERMS if t in head)
    s += min(10, sum(body.count(t) for t in PRODUCT_TERMS) // 3)
    return s


def eligible(pg: dict, min_score: int) -> bool:
    if DENY_URL.search(pg["url"]) or DENY_TITLE.search(pg["title"]):
        return False
    if len([s for s in urlsplit(pg["url"]).path.split("/") if s]) < 2:  # hub/landing pages
        return False
    if len(pg["content"]) < MIN_CHARS:
        return False
    return product_score(pg) >= min_score


def top_terms(pg: dict, n: int = 30) -> set[str]:
    words = [w for w in norm_text(pg["title"] + " " + pg["content"][:MAX_CHARS]).split() if w not in STOP and len(w) > 3]
    return {w for w, _ in Counter(words).most_common(n)}


def select_pages(pages: list[dict], min_score: int) -> list[dict]:
    el = [p for p in pages if eligible(p, min_score)]
    for p in el:
        p["score"] = product_score(p)
    return sorted(el, key=lambda p: -p["score"])


def stratified(pages: list[dict], n: int, rng: random.Random, exclude: set[str]) -> list[dict]:
    by_cat: dict[str, list[dict]] = defaultdict(list)
    for p in pages:
        if p["url"] not in exclude:
            by_cat[p["category"]].append(p)
    for v in by_cat.values():
        rng.shuffle(v)
    out, cats = [], sorted(by_cat)
    while len(out) < n and any(by_cat[c] for c in cats):
        for c in cats:
            if by_cat[c] and len(out) < n:
                out.append(by_cat[c].pop())
    return out


def pick_pairs(pages: list[dict], n: int, rng: random.Random, exclude: set[str]) -> list[tuple[dict, dict]]:
    """Pairs from the same category with the highest topical overlap."""
    cands = []
    pool = [p for p in pages if p["url"] not in exclude]
    terms = {p["url"]: top_terms(p) for p in pool}
    for i, a in enumerate(pool):
        for b in pool[i + 1:]:
            if a["category"] != b["category"]:
                continue
            ta, tb = terms[a["url"]], terms[b["url"]]
            j = len(ta & tb) / max(1, len(ta | tb))
            if j > 0.08:
                cands.append((j, a, b))
    cands.sort(key=lambda x: -x[0])
    out, used = [], set()
    for _, a, b in cands:
        if a["url"] in used or b["url"] in used:
            continue
        out.append((a, b))
        used.update([a["url"], b["url"]])
        if len(out) >= n:
            break
    rng.shuffle(out)
    return out


# --------------------------------------------------------------------------- LLM
def make_client(args):
    endpoint = args.endpoint or os.environ.get("AZURE_OPENAI_ENDPOINT")
    if not endpoint:
        raise LLMError("Set AZURE_OPENAI_ENDPOINT or pass --endpoint")
    from azure.identity import DefaultAzureCredential, get_bearer_token_provider
    from openai import AzureOpenAI

    token = get_bearer_token_provider(DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default")
    return AzureOpenAI(
        azure_endpoint=endpoint,
        azure_ad_token_provider=token,
        api_version=args.api_version or os.environ.get("AZURE_OPENAI_API_VERSION", "2025-03-01-preview"),
    )


def call_llm(client, deployment: str, system: str, user: str, retries: int = 4) -> dict:
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            resp = client.chat.completions.create(
                model=deployment,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                max_completion_tokens=2500,
            )
            txt = (resp.choices[0].message.content or "").strip()
            m = re.search(r"\{.*\}", txt, re.S)
            if not m:
                raise ValueError(f"No JSON in response: {txt[:200]!r}")
            return json.loads(m.group(0))
        except Exception as e:  # noqa: BLE001 - logged and retried, raised after final attempt
            last = e
            wait = min(30, 2 ** attempt) + random.random()
            LOG.warning("LLM call failed (attempt %d/%d): %s; retry in %.1fs", attempt, retries, e, wait)
            time.sleep(wait)
    raise LLMError(f"LLM failed after {retries} attempts: {last}")


SYS_BASE = (
    "You write evaluation questions to test search retrieval for the Royal London (UK insurer) customer FAQ chatbot. "
    "Rules: the question must be one a real customer or prospect would ask about Royal London products and services "
    "(pensions, ISAs, protection/life cover, equity release, bereavement, investments, transfers, claims). "
    "Never ask about executives, staff, company history, careers, or corporate matters. "
    "The question must be self-contained (name the product), must never mention 'page', 'article', 'text' or 'according to', "
    "and must be answerable ONLY from the provided content. Do not invent facts. Reply with a single JSON object only."
)
STYLE = {
    "plain": "Natural, clear wording.",
    "casual": "Casual, short, conversational wording, as typed on a phone.",
    "typo": "Casual wording that includes 1-2 realistic typos or misspellings in the question only.",
}


def prompt_single(pg: dict, style: str) -> tuple[str, str]:
    user = (
        f"Style: {STYLE[style]}\n"
        'Return {"question": str, "expected_answer": str (<=250 chars, grounded in content), '
        '"evidence": str (one sentence copied VERBATIM from the content, <=300 chars)}. '
        'If the content has no suitable customer/product information return {"skip": true}.\n\n'
        f"TITLE: {pg['title']}\nCONTENT:\n{pg['content'][:MAX_CHARS]}"
    )
    return SYS_BASE, user


def prompt_multi(a: dict, b: dict, mode: str) -> tuple[str, str]:
    need = (
        "The question must REQUIRE facts from BOTH A and B to answer fully (neither alone is enough)."
        if mode == "multi_required"
        else "The question must be answerable independently from A alone AND from B alone (each page gives a valid answer)."
    )
    user = (
        f"{need}\n"
        'Return {"question": str, "expected_answer": str (<=250 chars), "evidence_a": str (VERBATIM sentence from A), '
        '"evidence_b": str (VERBATIM sentence from B)}. If impossible return {"skip": true}.\n\n'
        f"A TITLE: {a['title']}\nA CONTENT:\n{a['content'][:MAX_CHARS // 2]}\n\n"
        f"B TITLE: {b['title']}\nB CONTENT:\n{b['content'][:MAX_CHARS // 2]}"
    )
    return SYS_BASE, user


def prompt_unanswerable(pg: dict, mode: str) -> tuple[str, str]:
    kind = (
        "a Royal London product question that this content does NOT answer (e.g. a specific personal quote, "
        "an individual account/policy detail, or a figure not stated in the content)"
        if mode == "in_domain_gap"
        else "a question clearly outside Royal London's scope (e.g. a competitor's product or general knowledge unrelated to insurance/pensions)"
    )
    user = f'Write {kind}. Return {{"question": str}}.\n\nCONTEXT TITLE: {pg["title"]}\nCONTENT:\n{pg["content"][:2000]}'
    return SYS_BASE.replace("and must be answerable ONLY from the provided content", "and must NOT be answerable from the provided content"), user


# --------------------------------------------------------------------------- validation
def grounded(evidence: str, text: str) -> bool:
    e = norm_text(evidence or "")
    return len(e) >= 20 and e in norm_text(text)


def valid_question(q: str, existing: list[str]) -> bool:
    if not q or not (8 <= len(q) <= 220) or BANNED_Q.search(q):
        return False
    nq = norm_text(q)
    return all(difflib.SequenceMatcher(None, nq, norm_text(x)).ratio() < 0.85 for x in existing)


def row(qid, q, atype, rel, answerable, cat, ans, ev, variant, model, review) -> dict:
    return {
        "query_id": qid, "query": q.strip(), "answer_type": atype,
        "relevant_ids": rel, "answerable": answerable, "category": cat,
        "expected_answer": (ans or "")[:250], "evidence": ev, "variant": variant,
        "provenance": "synthetic", "generator_model": model,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "needs_review": review,
    }


# --------------------------------------------------------------------------- generation
def generate(args, pages: list[dict], client) -> list[dict]:
    rng = random.Random(args.seed)
    sel = select_pages(pages, args.min_score)
    model, rows, qs, used = args.deployment, [], [], set()
    fails = Counter()

    # ---- single
    styles = (["plain"] * (args.n_single - 6) + ["casual"] * 3 + ["typo"] * 3) if args.n_single >= 8 else ["plain"] * args.n_single
    cand = stratified(sel, args.n_single * 3, rng, set())
    si = 0
    for pg in cand:
        if si >= args.n_single:
            break
        style = styles[si]
        try:
            r = call_llm(client, model, *prompt_single(pg, style))
        except LLMError:
            fails["single_llm"] += 1
            continue
        q = r.get("question", "")
        if r.get("skip") or not valid_question(q, qs) or not grounded(r.get("evidence", ""), pg["content"][:MAX_CHARS]):
            fails["single_rejected"] += 1
            LOG.info("Rejected single for %s", pg["url"])
            continue
        si += 1
        qs.append(q)
        used.add(pg["url"])
        rows.append(row(f"SYN-S{si:03d}", q, "single", [{"url": pg["url"], "grade": 2}], True,
                        pg["category"], r.get("expected_answer"), r.get("evidence"), style, model, False))
        LOG.info("single %d/%d ok (%s)", si, args.n_single, pg["url"])

    # ---- multi
    modes = ["multi_required"] * args.n_multi_required + ["multi_any"] * args.n_multi_any
    pairs = pick_pairs(sel, len(modes) * 3, rng, set())
    mi, pi = 0, 0
    while mi < len(modes) and pi < len(pairs):
        a, b = pairs[pi]
        pi += 1
        mode = modes[mi]
        try:
            r = call_llm(client, model, *prompt_multi(a, b, mode))
        except LLMError:
            fails["multi_llm"] += 1
            continue
        q = r.get("question", "")
        if (r.get("skip") or not valid_question(q, qs)
                or not grounded(r.get("evidence_a", ""), a["content"][:MAX_CHARS // 2])
                or not grounded(r.get("evidence_b", ""), b["content"][:MAX_CHARS // 2])):
            fails["multi_rejected"] += 1
            LOG.info("Rejected %s for %s + %s", mode, a["url"], b["url"])
            continue
        mi += 1
        qs.append(q)
        rows.append(row(f"SYN-M{mi:03d}", q, mode,
                        [{"url": a["url"], "grade": 2}, {"url": b["url"], "grade": 2 if mode == "multi_required" else 1}],
                        True, a["category"], r.get("expected_answer"),
                        f"A: {r.get('evidence_a')} | B: {r.get('evidence_b')}", "plain", model,
                        mode == "multi_any"))  # multi_any grading is a judgement call -> flag for SME
        LOG.info("%s %d/%d ok", mode, mi, len(modes))

    # ---- unanswerable
    umodes = (["in_domain_gap", "out_of_scope"] * args.n_unanswerable)[: args.n_unanswerable]
    ctx = stratified(sel, args.n_unanswerable * 2, rng, set())
    ui = 0
    for pg in ctx:
        if ui >= len(umodes):
            break
        try:
            r = call_llm(client, model, *prompt_unanswerable(pg, umodes[ui]))
        except LLMError:
            fails["unans_llm"] += 1
            continue
        q = r.get("question", "")
        if not valid_question(q, qs):
            fails["unans_rejected"] += 1
            continue
        ui += 1
        qs.append(q)
        rows.append(row(f"SYN-U{ui:03d}", q, "unanswerable", [], False,
                        "out_of_scope" if umodes[ui - 1] == "out_of_scope" else pg["category"],
                        "", "", umodes[ui - 1], model, True))
        LOG.info("unanswerable %d/%d ok", ui, len(umodes))

    got = Counter(r["answer_type"] for r in rows)
    LOG.info("Generated: %s | failures: %s", dict(got), dict(fails))
    want = args.n_single + len(modes) + args.n_unanswerable
    if len(rows) < want:
        LOG.error("SHORTFALL: %d of %d rows generated. Re-run (new --seed) or lower --min-score.", len(rows), want)
    return rows


def write_outputs(rows: list[dict], out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    jp, cp = out / "synthetic_dataset_v1.json", out / "synthetic_dataset_v1.csv"
    jp.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    with cp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        for r in rows:
            w.writerow({**r, "relevant_ids": json.dumps(r["relevant_ids"])})
    LOG.info("Wrote %s and %s", jp, cp)


def load_env() -> bool:
    """Load .env (searched upward from this file). Returns False if python-dotenv is missing."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return False
    load_dotenv()  # existing shell variables win over .env
    return True


def main() -> int:
    dotenv_ok = load_env()  # must run before argparse: defaults read os.environ
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", required=True, help="Scraper JSON file or directory")
    ap.add_argument("--out", default="synthetic_out")
    ap.add_argument("--list-pages", action="store_true", help="Show page selection only (no LLM calls)")
    ap.add_argument("--n-single", type=int, default=20)
    ap.add_argument("--n-multi-required", type=int, default=4)
    ap.add_argument("--n-multi-any", type=int, default=2)
    ap.add_argument("--n-unanswerable", type=int, default=4)
    ap.add_argument("--min-score", type=int, default=3, help="Min product-relevance score for a page")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--deployment", default=os.environ.get("DEPLOYMENT_MAIN", ""))
    ap.add_argument("--endpoint", default="")
    ap.add_argument("--api-version", default="")
    args = ap.parse_args()

    Path(args.out).mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(Path(args.out) / "generate.log", encoding="utf-8")],
    )
    if not dotenv_ok and any(p.exists() for p in (Path(".env"), Path(__file__).resolve().parent.parent / ".env")):
        LOG.warning("A .env file exists but python-dotenv is not installed, so it was ignored. Run: pip install python-dotenv")
    try:
        pages = load_pages(args.input)
        sel = select_pages(pages, args.min_score)
        LOG.info("Pages loaded: %d | eligible (product-focused): %d | excluded: %d",
                 len(pages), len(sel), len(pages) - len(sel))
        if args.list_pages:
            print("\nCategory counts (eligible):", dict(Counter(p["category"] for p in sel)))
            print("\nTop eligible pages:")
            for p in sel[:40]:
                print(f"  [{p['score']:>2}] {p['category']:<18} {p['url']}")
            print("\nExcluded sample (check no product pages are wrongly excluded):")
            ex = [p for p in pages if p not in sel]
            for p in ex[:25]:
                why = "deny-url/title" if (DENY_URL.search(p["url"]) or DENY_TITLE.search(p["title"])) else (
                    "short" if len(p["content"]) < MIN_CHARS else "low-score")
                print(f"  ({why}) {p['url']}")
            return 0
        if len(sel) < args.n_single:
            LOG.error("Only %d eligible pages for %d single questions; lower --min-score", len(sel), args.n_single)
            return 2
        if not args.deployment:
            LOG.error("Set DEPLOYMENT_MAIN or pass --deployment")
            return 2
        rows = generate(args, pages, make_client(args))
        if not rows:
            LOG.error("No rows generated")
            return 1
        write_outputs(rows, Path(args.out))
        return 0
    except Exception:
        LOG.exception("Fatal error")
        return 1


if __name__ == "__main__":
    sys.exit(main())