# diff_pages_advance.py - compare two scrape JSONs taken on different days
# Usage (from RAG\):
#   python scraper\diff_pages_advance.py <old.json> <new.json>                  -> summary + diffs for all pages
#   python scraper\diff_pages_advance.py <old.json> <new.json> --url <url>      -> detailed diff for one page
#   python scraper\diff_pages_advance.py <old.json> <new.json> --out report.txt --csv summary.csv
# Options: --max-diff-lines N (default 60 per page), --min-change PCT (hide pages changed < PCT %)
import argparse, csv, difflib, hashlib, json, re, sys


def load(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    pages = data.get("pages", data) if isinstance(data, dict) else data
    out = {}
    for p in pages:
        url = p.get("source_url")
        if not url:
            continue
        key = url + (f"  [dropdown: {p['dropdown_url']}]" if p.get("dropdown_url") else "")
        out[key] = p
    return out


def norm_idx(text):
    """Normalized non-empty lines plus their ORIGINAL 1-based line numbers."""
    lines, nums = [], []
    for i, l in enumerate((text or "").splitlines(), 1):
        l = re.sub(r"\s+", " ", l).strip()
        if l:
            lines.append(l); nums.append(i)
    return lines, nums


def norm(text):
    return norm_idx(text)[0]


def section_at(lines, idx):
    """Nearest markdown header at or before line index idx."""
    for j in range(min(idx, len(lines) - 1), -1, -1):
        if re.match(r"^#{1,6}\s", lines[j]):
            return lines[j]
    return "(top of page)"


def rng(nums, a, b):
    if a >= b:
        return "-"
    return f"{nums[a]}" if b - a == 1 else f"{nums[a]}-{nums[b - 1]}"


def word_diff(a, b):
    sa, sb = a.split(), b.split()
    m = difflib.SequenceMatcher(None, sa, sb, autojunk=False)
    parts = []
    for t, i1, i2, j1, j2 in m.get_opcodes():
        if t == "replace":
            parts.append(f"[-{' '.join(sa[i1:i2])}-]{{+{' '.join(sb[j1:j2])}+}}")
        elif t == "delete":
            parts.append(f"[-{' '.join(sa[i1:i2])}-]")
        elif t == "insert":
            parts.append(f"{{+{' '.join(sb[j1:j2])}+}}")
        else:
            w = sa[i1:i2]
            parts.append(" ".join(w) if len(w) <= 6 else " ".join(w[:3]) + " ... " + " ".join(w[-3:]))
    return " ".join(parts)


def sections(lines):
    """Split normalized lines into {header: [lines]} using markdown headers."""
    secs, cur = {}, "(top)"
    secs[cur] = []
    for l in lines:
        if re.match(r"^#{1,6}\s", l):
            cur = l
            secs.setdefault(cur, [])
        else:
            secs[cur].append(l)
    return secs


def digest(lines):
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()[:12]


def compare(op, np_):
    o, onum = norm_idx(op.get("content"))
    n, nnum = norm_idx(np_.get("content"))
    r = {"old_lines": len(o), "new_lines": len(n),
         "old_chars": sum(map(len, o)), "new_chars": sum(map(len, n))}
    r["same"] = digest(o) == digest(n)
    sm = difflib.SequenceMatcher(None, o, n, autojunk=False)
    r["similarity"] = round(sm.ratio() * 100, 1)
    r["added"] = sum(j2 - j1 for t, i1, i2, j1, j2 in sm.get_opcodes() if t in ("insert", "replace"))
    r["removed"] = sum(i2 - i1 for t, i1, i2, j1, j2 in sm.get_opcodes() if t in ("delete", "replace"))
    so, sn = sections(o), sections(n)
    r["sec_added"] = [s for s in sn if s not in so]
    r["sec_removed"] = [s for s in so if s not in sn]
    r["sec_modified"] = [s for s in sn if s in so and so[s] != sn[s]]
    changes = []
    for t, i1, i2, j1, j2 in sm.get_opcodes():
        if t == "equal":
            continue
        kind = {"insert": "ADDED", "delete": "REMOVED", "replace": "MODIFIED"}[t]
        c = [f"  [{kind}] section: {section_at(n if t != 'delete' else o, j1 if t != 'delete' else i1)[:80]}",
             f"      old lines {rng(onum, i1, i2)} | new lines {rng(nnum, j1, j2)}"]
        if t == "replace" and (i2 - i1) == (j2 - j1):
            for x in range(i2 - i1):
                c.append(f"      ~ {word_diff(o[i1 + x], n[j1 + x])[:300]}")
        else:
            c += [f"      - {l[:200]}" for l in o[i1:i2]]
            c += [f"      + {l[:200]}" for l in n[j1:j2]]
        changes.append(c)
    r["diff"] = changes
    # metadata fields that changed (title, category, etc.)
    meta = {}
    for k in sorted(set(op) | set(np_)):
        if k == "content":
            continue
        if op.get(k) != np_.get(k):
            meta[k] = (str(op.get(k))[:100], str(np_.get(k))[:100])
    r["meta"] = meta
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("old"); ap.add_argument("new")
    ap.add_argument("--url"); ap.add_argument("--out"); ap.add_argument("--csv")
    ap.add_argument("--max-diff-lines", type=int, default=60)
    ap.add_argument("--min-change", type=float, default=0.0)
    a = ap.parse_args()

    old, new = load(a.old), load(a.new)
    if a.url:
        old = {k: v for k, v in old.items() if k.startswith(a.url)}
        new = {k: v for k, v in new.items() if k.startswith(a.url)}
        a.max_diff_lines = 10**6

    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    common = sorted(set(old) & set(new))
    results = {k: compare(old[k], new[k]) for k in common}
    changed = [k for k in common if not results[k]["same"]
               and (100 - results[k]["similarity"]) >= a.min_change]
    unchanged = [k for k in common if results[k]["same"]]

    L = []
    L += ["=" * 80, "SCRAPE COMPARISON", f"OLD: {a.old}  ({len(old)} pages)", f"NEW: {a.new}  ({len(new)} pages)", "=" * 80,
          f"Pages added (only in NEW)    : {len(added)}",
          f"Pages removed (only in OLD)  : {len(removed)}",
          f"Pages changed                : {len(changed)}",
          f"Pages unchanged              : {len(unchanged)}", ""]
    if added:
        L += ["--- ADDED PAGES ---"] + [f"+ {k}  ({len(norm(new[k].get('content')))} lines)" for k in added] + [""]
    if removed:
        L += ["--- REMOVED PAGES ---"] + [f"- {k}  ({len(norm(old[k].get('content')))} lines)" for k in removed] + [""]
    if changed:
        L += ["--- CHANGED PAGES (most changed first) ---"]
        for k in sorted(changed, key=lambda k: results[k]["similarity"]):
            r = results[k]
            L.append(f"~ {k}\n    similarity {r['similarity']}% | lines +{r['added']} / -{r['removed']} | "
                     f"chars {r['old_chars']} -> {r['new_chars']} ({r['new_chars'] - r['old_chars']:+d})")
        L.append("")
        L += ["=" * 80, "DETAILS PER CHANGED PAGE", "=" * 80]
        for k in sorted(changed, key=lambda k: results[k]["similarity"]):
            r = results[k]
            L += ["", f"### {k}",
                  f"similarity {r['similarity']}% | lines {r['old_lines']} -> {r['new_lines']} | +{r['added']} / -{r['removed']}"]
            if r["meta"]:
                L.append("metadata changed:")
                L += [f"    {f}: {v[0]!r} -> {v[1]!r}" for f, v in r["meta"].items()]
            if r["sec_added"]:
                L.append("sections added:    " + " | ".join(s[:60] for s in r["sec_added"]))
            if r["sec_removed"]:
                L.append("sections removed:  " + " | ".join(s[:60] for s in r["sec_removed"]))
            if r["sec_modified"]:
                L.append("sections modified: " + " | ".join(s[:60] for s in r["sec_modified"]))
            L.append(f"changes ({len(r['diff'])} blocks; line numbers = original file lines, blank lines included):")
            shown = 0
            for blk in r["diff"]:
                if shown + len(blk) > a.max_diff_lines and shown > 0:
                    break
                L += blk
                shown += len(blk)
            if shown < sum(map(len, r["diff"])):
                L.append("    ... more change blocks hidden (use --url <url> for all)")
    text = "\n".join(L)
    print(text)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"\nReport written to {a.out}")
    if a.csv:
        with open(a.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["url", "status", "similarity_pct", "lines_added", "lines_removed", "old_chars", "new_chars"])
            for k in added: w.writerow([k, "added", "", "", "", "", sum(map(len, norm(new[k].get("content"))))])
            for k in removed: w.writerow([k, "removed", "", "", "", sum(map(len, norm(old[k].get("content")))), ""])
            for k in common:
                r = results[k]
                w.writerow([k, "unchanged" if r["same"] else "changed", r["similarity"], r["added"], r["removed"], r["old_chars"], r["new_chars"]])
        print(f"CSV written to {a.csv}")


if __name__ == "__main__":
    main()