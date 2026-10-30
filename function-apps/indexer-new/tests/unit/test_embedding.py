from types import SimpleNamespace as NS

import pytest


def chunks(n):
    return [{"content": f"text {i}"} for i in range(n)]


class FakeOpenAI:
    def __init__(self, dims=1536, drop=0, reverse=False):
        self.calls, self.dims, self.drop, self.reverse = [], dims, drop, reverse
        self.embeddings = NS(create=self._create)

    def _create(self, input, model, dimensions):
        self.calls.append((list(input), model, dimensions))
        data = [NS(index=i, embedding=[float(i)] * self.dims) for i in range(len(input) - self.drop)]
        return NS(data=list(reversed(data)) if self.reverse else data)


def test_embed_chunks_batches_and_keeps_order(mocker, script):
    client = FakeOpenAI(reverse=True)  # service may return out of order; index must win
    mocker.patch.object(script, "get_openai_client", return_value=client)
    vectors = script.embed_chunks(chunks(120))
    assert len(vectors) == 120 and len(client.calls) == 3  # batches of 50
    assert vectors[0][0] == 0.0 and vectors[1][0] == 1.0 and vectors[49][0] == 49.0
    assert client.calls[0][1:] == (script.EMBEDDING_DEPLOYMENT, 1536)


def test_embed_chunks_empty_input_makes_no_calls(mocker, script):
    client = FakeOpenAI()
    mocker.patch.object(script, "get_openai_client", return_value=client)
    assert script.embed_chunks([]) == [] and client.calls == []


def test_embed_chunks_rejects_short_response(mocker, script):
    mocker.patch.object(script, "get_openai_client", return_value=FakeOpenAI(drop=1))
    with pytest.raises(RuntimeError, match="expected 3 vectors"):
        script.embed_chunks(chunks(3))


def test_embed_chunks_rejects_wrong_dimensions(mocker, script):
    mocker.patch.object(script, "get_openai_client", return_value=FakeOpenAI(dims=8))
    with pytest.raises(RuntimeError, match="dims"):
        script.embed_chunks(chunks(2))


def test_openai_client_requires_endpoint(mocker, script):
    mocker.patch.object(script, "_openai_client", None)
    mocker.patch.object(script, "AZURE_OPENAI_ENDPOINT", "")
    with pytest.raises(ValueError, match="AZURE_OPENAI_ENDPOINT"):
        script.get_openai_client()


def test_openai_client_is_created_once(mocker, script):
    mocker.patch.object(script, "_openai_client", None)
    mocker.patch.object(script, "get_credential")
    mocker.patch.object(script, "get_bearer_token_provider")
    factory = mocker.patch.object(script, "AzureOpenAI")
    assert script.get_openai_client() is script.get_openai_client()
    factory.assert_called_once()
