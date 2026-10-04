import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from agrihub import events, models
from open_deep_research.deep_researcher import configurable_model
from open_deep_research.utils import prompt_cache_kwargs

HAIKU = "anthropic:claude-haiku-4-5"


@pytest.fixture(autouse=True)
def anthropic_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    models.chat_model.cache_clear()
    yield
    models.chat_model.cache_clear()


def _payload(model) -> dict:
    return model._get_request_payload([SystemMessage(content="system"), HumanMessage(content="question")])


def test_anthropic_study_models_send_top_level_cache_control():
    model = models.chat_model(HAIKU, 1024, 0)
    assert _payload(model)["cache_control"] == {"type": "ephemeral"}


def test_prompt_caching_can_be_switched_off_and_skips_other_providers():
    assert "cache_control" not in _payload(models.chat_model(HAIKU, 1024, 0, False))
    assert "cache_control" not in models.chat_model("openai:gpt-4.1-mini", 1024, 0).model_kwargs


def test_cached_input_tokens_reads_the_cache_read_count():
    response = AIMessage(
        content="",
        usage_metadata={
            "input_tokens": 1200,
            "output_tokens": 40,
            "total_tokens": 1240,
            "input_token_details": {"cache_read": 1000, "cache_creation": 0},
        },
    )
    assert models.cached_input_tokens(response) == 1000
    assert models.cached_input_tokens(AIMessage(content="")) == 0


def test_agent_usage_sends_cached_tokens_only_when_reported():
    plain = events.agent_usage(events.ORCHESTRATOR, model=HAIKU, input_tokens=10, output_tokens=2)
    cached = events.agent_usage(events.ORCHESTRATOR, model=HAIKU, input_tokens=10, output_tokens=2, cached_input_tokens=8)
    assert "cached_input_tokens" not in plain.data
    assert cached.data["cached_input_tokens"] == 8


def test_chat_graph_configurable_model_caches_anthropic_prompts():
    assert prompt_cache_kwargs("openai:gpt-4.1") == {}
    assert prompt_cache_kwargs(HAIKU, enabled=False) == {}
    config = {"model": HAIKU, "max_tokens": 1024, "api_key": "sk-ant-test", **prompt_cache_kwargs(HAIKU)}
    model = configurable_model.with_config(config)._model()
    assert _payload(model)["cache_control"] == {"type": "ephemeral"}
