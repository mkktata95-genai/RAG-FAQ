"""Shared fakes for script tests (not collected as tests)."""
import hashlib


class Result(list):
    def __init__(self, items, count):
        super().__init__(items)
        self._count = count

    def get_count(self):
        return self._count


class Outcome:
    def __init__(self, key, ok=True):
        self.key, self.succeeded = key, ok
        self.error_message, self.status_code = ("" if ok else "boom"), (200 if ok else 500)


class FakeSearch:
    """Just enough SearchClient: search(paging), upload/delete with scripted failures, document count."""

    def __init__(self, docs=None, fail_upload=0, fail_delete_keys=(), count_override=None):
        self.docs = [dict(d) for d in (docs or [])]
        self.fail_upload = fail_upload            # number of upload calls that report one failed doc
        self.fail_delete_keys = set(fail_delete_keys)
        self.count_override = count_override
        self.uploaded, self.deleted, self.search_calls = [], [], 0

    def search(self, search_text="*", select=None, top=1000, skip=0, include_total_count=False):
        self.search_calls += 1
        total = self.count_override if self.count_override is not None else len(self.docs)
        return Result(self.docs[skip:skip + top], total)

    def get_document_count(self):
        return len(self.docs)

    def upload_documents(self, documents):
        out = []
        for i, d in enumerate(documents):
            bad = self.fail_upload > 0 and i == 0
            out.append(Outcome(d["chunk_id"], not bad))
            if not bad:
                self.uploaded.append(d)
                self.docs = [x for x in self.docs if x["chunk_id"] != d["chunk_id"]] + [dict(d)]
        if self.fail_upload > 0:
            self.fail_upload -= 1
        return out

    def delete_documents(self, documents):
        out = []
        for d in documents:
            k = d["chunk_id"]
            ok = k not in self.fail_delete_keys
            out.append(Outcome(k, ok))
            if ok:
                self.deleted.append(k)
                self.docs = [x for x in self.docs if x["chunk_id"] != k]
        return out


def doc(url, chunk_id, content_hash="h", dropdown_url="", refresh_count=0, video_url=""):
    return {"chunk_id": chunk_id, "source_url": url, "dropdown_url": dropdown_url, "content_hash": content_hash,
            "video_url": video_url, "refresh_count": refresh_count, "content": "old " + chunk_id}


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
