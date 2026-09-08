import pytest

from config import Settings, get_settings


def _settings(**overrides) -> Settings:
    defaults = {"database_url": "postgresql://asmr:asmr@localhost:5432/asmr_rag"}
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)


def test_defaults_use_ollama_provider():
    s = _settings()
    assert s.embedding_provider == "ollama"
    assert s.llm_provider == "ollama"
    assert s.embedding_dim == 1024


def test_database_url_normalizes_sqlalchemy_style_prefix():
    s = _settings(database_url="postgresql+asyncpg://u:p@localhost:5432/db")
    assert s.database_url == "postgresql://u:p@localhost:5432/db"


def test_internal_embedding_provider_requires_base_and_key():
    with pytest.raises(ValueError):
        _settings(embedding_provider="internal")

    s = _settings(
        embedding_provider="internal",
        internal_embedding_api_base="https://x",
        internal_embedding_api_key="k",
    )
    assert s.embedding_provider == "internal"


def test_internal_llm_provider_requires_base_and_key():
    with pytest.raises(ValueError):
        _settings(llm_provider="internal")

    s = _settings(
        llm_provider="internal",
        internal_llm_api_base="https://x",
        internal_llm_api_key="k",
    )
    assert s.llm_provider == "internal"


def test_invalid_provider_value_rejected():
    with pytest.raises(ValueError):
        _settings(embedding_provider="not-a-provider")


def test_get_settings_is_cached(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://asmr:asmr@localhost:5432/asmr_rag")
    get_settings.cache_clear()
    try:
        assert get_settings() is get_settings()
    finally:
        get_settings.cache_clear()
