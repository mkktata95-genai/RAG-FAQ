"""azure_embed: batching, ordering, retry. The OpenAI client is faked."""
from types import SimpleNamespace as NS
from unittest import mock

import pytest

from src import templated_job as j


class RateLimitError(Exception):
    pass


class FakeClient:
    def __init__(self, fail_first=0, exc=RateLimitError("429")):
        self.calls, self.fail_first, self.exc = [], fail_first, exc
        self.embeddings = NS(create=self._create)

    def _create(self, model, input, dimensions):
        self.calls.append({"model": model, "input": list(input), "dimensions": dimensions})
        if self.fail_first > 0:
            self.fail_first -= 1
            raise self.exc
        # return out of order on purpose: azure_embed must sort by index
        data = [NS(index=i, embedding=[float(len(t)), 0.0, 0.0, 0.0]) for i, t in enumerate(input)]
        return NS(data=list(reversed(data)))


def test_batches_of_16_in_order_with_deployment_and_dims(monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(j, "_openai_client", lambda: fake)
    texts = ["a" * (i + 1) for i in range(20)]
    out = j.azure_embed(texts)
    assert [len(c["input"]) for c in fake.calls] == [16, 4]
    assert fake.calls[0]["model"] == "test-embedding" and fake.calls[0]["dimensions"] == 4
    assert [v[0] for v in out] == [float(len(t)) for t in texts]          # order preserved


def test_transient_error_is_retried(monkeypatch):
    fake = FakeClient(fail_first=2)
    monkeypatch.setattr(j, "_openai_client", lambda: fake)
    assert len(j.azure_embed(["q"])) == 1 and len(fake.calls) == 3


def test_permanent_error_is_raised_immediately(monkeypatch):
    fake = FakeClient(fail_first=5, exc=ValueError("deployment not found"))
    monkeypatch.setattr(j, "_openai_client", lambda: fake)
    with pytest.raises(ValueError):
        j.azure_embed(["q"])
    assert len(fake.calls) == 1


def test_openai_client_uses_managed_identity_token_provider(monkeypatch):
    seen = {}
    monkeypatch.setattr("openai.AzureOpenAI", lambda **kw: seen.update(kw) or "client")
    monkeypatch.setattr("azure.identity.get_bearer_token_provider", lambda cred, scope: ("provider", scope))
    assert j._openai_client() == "client"
    assert seen["azure_endpoint"] == "https://openai.test.invalid"
    assert seen["azure_ad_token_provider"] == ("provider", "https://cognitiveservices.azure.com/.default")
