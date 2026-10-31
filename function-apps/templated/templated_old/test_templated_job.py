"""Offline check: python test_templated_job.py  (no Azure, no network; needs openpyxl)."""
import json
import os
import tempfile
from types import SimpleNamespace as NS
from openpyxl import Workbook
from src import templated_job as j

os.environ["AZURE_OPENAI_EMBEDDING_DIMENSIONS"] = "4"
OK = "https://www.royallondon.com/x/"


def xlsx(rows, header=None):
    h = header or ["id", "question", "answer", "active", "intent", "scenario", "advice_boundary",
                   "citation_1_title", "citation_1_url", "citation_2_title", "citation_2_url",
                   "citation_3_title", "citation_3_url", "citation_4_title", "citation_4_url",
                   "cta_1_label", "cta_1_url"]
    wb = Workbook(); ws = wb.active; ws.append(h)
    for r in rows: ws.append(r)
    p = tempfile.mktemp(suffix=".xlsx"); wb.save(p); return p


def row(**kw):
    base = dict(id="", question="What is a pension?", answer="A pension is savings for retirement.", active="Y",
                intent="", scenario="", advice_boundary="", c1t="Pensions", c1u=OK, c2t="", c2u="", c3t="", c3u="", c4t="", c4u="",
                l1="Basics", l1u=OK + "b/")
    base.update(kw)
    return [base[k] for k in ["id", "question", "answer", "active", "intent", "scenario", "advice_boundary", "c1t", "c1u", "c2t", "c2u", "c3t", "c3u", "c4t", "c4u", "l1", "l1u"]]


def check(rows):
    return j.validate(j.read_excel(xlsx(rows)))


# happy path: defaults, exact response_json shape/order, ids, source_urls
e, errs, w = check([row()])
assert not errs and len(e) == 1
d = json.loads(e[0]["response_json"])
assert list(d) == ["intent", "scenario", "answer", "advice_boundary", "citations", "read_more"]
assert d["intent"] == "grounded" and d["scenario"] == "standard" and d["advice_boundary"] is False
assert d["citations"] == [{"title": "Pensions", "url": OK}] and d["read_more"] == [{"label": "Basics", "url": OK + "b/"}]
assert e[0]["source_urls"] == [OK, OK + "b/"] and e[0]["id"] == j.make_id("what is a pension")  # id ignores case, ? and spacing

# each rule rejects
bad = lambda **kw: check([row(**kw)])[1]
assert any("answer is empty" in x for x in bad(answer=""))
assert any("over the 250" in x for x in bad(answer="x" * 251))
assert any("soft limit" in x for x in check([row(answer="x" * 200)])[2])           # warn only
assert any("intent" in x for x in bad(intent="maybe")) and any("scenario" in x for x in bad(scenario="nope"))
assert any("https" in x for x in bad(c1u="http://www.royallondon.com/x"))
assert any("not allowed" in x for x in bad(c1u="https://www.moneyhelper.org.uk/en"))
assert any("not allowed" in x for x in bad(c1u="https://royallondon.com.evil.com/"))   # suffix trick
assert not bad(c1u="https://royallondon.com/x")                                        # bare domain ok
assert any("both title and url" in x for x in bad(c1t=""))
assert any("at most 3" in x for x in bad(c1t="a", c2t="b", c2u=OK, c3t="c", c3u=OK, c4t="d", c4u=OK))
assert any("duplicate" in x for x in check([row(), row(question="what is a  PENSION ?")])[1])
assert any("already used" in x for x in check([row(id="a1"), row(id="a1", question="Other?")])[1])
assert any("invalid characters" in x for x in bad(id="bad id!"))
# inactive rows are skipped, not errors; empty sheet is an error
e, errs, _ = check([row(active="N", answer=""), row(question="Other?")])
assert not errs and len(e) == 1
assert check([row(active="N")])[1] == ["no active rows found"]
# header typos/case/spaces tolerated
assert not j.validate(j.read_excel(xlsx([["Q?", "A."]], header=["Question", " Answer "])))[1]


# build + promote with fakes
class Box:
    def __init__(s, p): s.p = p
    def download_blob(s, n): return NS(readall=lambda: open(s.p, "rb").read())


class Ops:
    def __init__(s, idx=(), alias=None, counts=None): s.idx, s.alias, s.counts, s.deleted, s.docs, s.fail = list(idx), alias, counts or {}, [], {}, False
    def list_indexes(s): return list(s.idx)
    def count(s, i): return s.counts[i]
    def get_alias(s): return s.alias
    def set_alias(s, i): s.alias = i
    def delete(s, i): s.deleted.append(i); s.idx.remove(i)
    def create(s, i, dims): s.idx.append(i); s.dims = dims
    def upload(s, i, docs):
        if s.fail: raise RuntimeError("boom")
        s.docs[i] = docs


emb = lambda texts: [[0.1] * 4 for _ in texts]
ops = Ops()
r = j.build_index(Box(xlsx([row(), row(question="Other?")])), "templated-questions/a.xlsx", embed=emb, ops_factory=lambda: ops)
doc = ops.docs[r["index_name"]][0]
assert j.INDEX_RE.match(r["index_name"]) and r["rows"] == 2 and ops.dims == 4 and not ops.deleted
assert set(doc) == {"id", "question", "question_vector", "response_json", "source_urls", "created_at"}
# validation failure: raises, creates nothing
ops = Ops()
try:
    j.build_index(Box(xlsx([row(answer="")])), "templated-questions/a.xlsx", embed=emb, ops_factory=lambda: ops); raise SystemExit("should raise")
except ValueError as e:
    assert "rejected" in str(e) and ops.idx == []
# upload failure: partial index deleted
ops = Ops(); ops.fail = True
try:
    j.build_index(Box(xlsx([row()])), "templated-questions/a.xlsx", embed=emb, ops_factory=lambda: ops); raise SystemExit("should raise")
except RuntimeError as e:
    assert "boom" in str(e) and ops.idx == [] and len(ops.deleted) == 1
# wrong embedding size is caught before any upload
ops = Ops()
try:
    j.build_index(Box(xlsx([row()])), "templated-questions/a.xlsx", embed=lambda t: [[0.1] * 3 for _ in t], ops_factory=lambda: ops); raise SystemExit("should raise")
except RuntimeError as e:
    assert "mismatch" in str(e) and ops.idx == [] and not ops.docs

# names / messages
assert j.is_excel("templated-questions/a.xlsx") and not j.is_excel("templated-questions/~$a.xlsx") and not j.is_excel("templated-questions/a.csv")
assert j.parse_build_message('{"file":"x.xlsx"}') == "templated-questions/x.xlsx" and j.parse_build_message("run") is None
assert j.parse_promote_message("promote FORCE") == (None, True)

# promote: bootstrap, retention, shrink gate, force (legacy/other names never touched)
n = lambda d: f"rlg-faq-templated-2026010{d}-000000"
ops = Ops([n(1), n(2), n(3), "scache", "rlg-faq-index-v5"])
assert j.promote(ops)["promoted"] and ops.alias == n(3) and ops.deleted == []
j.KEEP_INDEXES = 2
ops = Ops([n(1), n(2), n(3), n(4), "scache"], alias=n(2), counts={n(2): 100, n(4): 90})
assert j.promote(ops, n(4))["promoted"] and ops.deleted == [n(1)] and "scache" in ops.idx
ops = Ops([n(1), n(2)], alias=n(1), counts={n(1): 100, n(2): 10})
assert not j.promote(ops, n(2))["promoted"] and j.promote(ops, n(2), force=True)["promoted"]
assert j.promote(Ops([n(1)], alias=n(1)))["reason"] == "already live" and not j.promote(Ops([n(1)]), "scache")["promoted"]

# the generated sample file passes
import subprocess, sys
subprocess.run([sys.executable, "make_sample_xlsx.py"], check=True, capture_output=True)
e, errs, w = j.validate(j.read_excel("sample_templated.xlsx"))
assert len(e) == 3 and not errs, errs
print("OK")
