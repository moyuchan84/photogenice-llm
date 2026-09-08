from clients.embedding_client import (
    InternalEmbeddingClient,
    OllamaEmbeddingClient,
    get_embedding_client,
)
from clients.llm_client import InternalLLMClient, OllamaLLMClient, get_llm_client
from config import Settings


def _settings(**overrides) -> Settings:
    defaults = {"database_url": "postgresql://asmr:asmr@localhost:5432/asmr_rag"}
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)


def test_get_embedding_client_returns_ollama_by_default():
    client = get_embedding_client(_settings())
    assert isinstance(client, OllamaEmbeddingClient)


def test_get_embedding_client_returns_internal_when_configured():
    client = get_embedding_client(
        _settings(
            embedding_provider="internal",
            internal_embedding_api_base="https://x",
            internal_embedding_api_key="k",
        )
    )
    assert isinstance(client, InternalEmbeddingClient)


def test_get_llm_client_returns_ollama_by_default():
    client = get_llm_client(_settings())
    assert isinstance(client, OllamaLLMClient)


def test_get_llm_client_returns_internal_when_configured():
    client = get_llm_client(
        _settings(
            llm_provider="internal",
            internal_llm_api_base="https://x",
            internal_llm_api_key="k",
        )
    )
    assert isinstance(client, InternalLLMClient)
