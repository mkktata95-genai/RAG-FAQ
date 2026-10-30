"""Shared test setup. Unit tests must never reach Azure or a real index:
- fake settings are ASSIGNED (not setdefault), so a developer's shell/.env values can not leak in
- DefaultAzureCredential is replaced with one that raises, so any accidental real call fails loudly
"""
import os
from unittest import mock

import pytest

os.environ["AZURE_SEARCH_ENDPOINT"] = "https://search.test.invalid"
os.environ["AZURE_SEARCH_INDEX_NAME"] = "test-index"
os.environ["AZURE_OPENAI_ENDPOINT"] = "https://openai.test.invalid"
os.environ["AZURE_OPENAI_EMBEDDING_DEPLOYMENT"] = "test-embedding"
os.environ["AZURE_OPENAI_EMBEDDING_DIMENSIONS"] = "1536"
os.environ["PIPELINE_RETRY_ATTEMPTS"] = "1"
os.environ["PIPELINE_RETRY_BASE_SECONDS"] = "0"


class FakeCredential:
    def __init__(self, *args, **kwargs):
        pass

    def get_token(self, *args, **kwargs):
        raise RuntimeError("credentials are disabled in tests")


mock.patch("azure.identity.DefaultAzureCredential", FakeCredential).start()


@pytest.fixture
def script():
    """The indexer script module, pinned to test settings even if a .env overrode them at import."""
    from src import chunk_embed_index_funcV1 as m
    with mock.patch.multiple(
        m, INDEX_NAME="test-index", SEARCH_ENDPOINT="https://search.test.invalid",
        AZURE_OPENAI_ENDPOINT="https://openai.test.invalid", EMBEDDING_DIMS=1536,
    ):
        yield m


@pytest.fixture(autouse=True)
def no_sleep(mocker):
    """Retry/backoff waits must never slow the tests down."""
    mocker.patch("time.sleep")
