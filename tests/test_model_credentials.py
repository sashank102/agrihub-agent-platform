import pytest

from agent_platform.main import _validate_model_credentials


def test_real_graph_requires_selected_provider_key(monkeypatch):
    monkeypatch.setenv("MODEL", "groq:openai/gpt-oss-120b")
    monkeypatch.setenv("SEARCH_API", "duckduckgo")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="groq:openai/gpt-oss-120b"):
        _validate_model_credentials()

    monkeypatch.setenv("GROQ_API_KEY", "test-provider-key")
    _validate_model_credentials()


def test_tavily_search_requires_its_key(monkeypatch):
    monkeypatch.setenv("MODEL", "groq:openai/gpt-oss-120b")
    monkeypatch.setenv("GROQ_API_KEY", "test-provider-key")
    monkeypatch.setenv("SEARCH_API", "tavily")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="Tavily search"):
        _validate_model_credentials()

    monkeypatch.setenv("TAVILY_API_KEY", "test-search-key")
    _validate_model_credentials()
