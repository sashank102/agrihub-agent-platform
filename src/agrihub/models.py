"""Chat models for the agents, chosen per role in ``StudyConfiguration``.

Any ``provider:model`` string that ``init_chat_model`` understands works
(``anthropic:...``, ``openai:...``); calls use the model's own
``max_retries`` and a ``max_tokens`` cap, and are tagged
``langsmith:nostream`` so token chunks never reach the run stream. Names
starting with ``agrihub-fake:`` select a scripted deterministic model from
``agrihub.fake_llm`` for tests, fixture recording and offline demos.

Anthropic models get the top-level ``cache_control`` request parameter, so
each call caches its prompt prefix (system prompt, tools and history) and the
next step of the same agent reads it back instead of paying for it again.
"""

from functools import lru_cache
from typing import Any

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool

from agrihub import fake_llm

NOSTREAM_TAGS = ["langsmith:nostream"]
PROMPT_CACHE_CONTROL = {"type": "ephemeral"}


def is_anthropic(name: str) -> bool:
    """Return whether ``init_chat_model`` sends ``name`` to the Anthropic API."""
    lowered = name.lower()
    return lowered.startswith("anthropic:") or (":" not in lowered and lowered.startswith("claude"))


@lru_cache(maxsize=32)
def chat_model(name: str, max_tokens: int, max_retries: int, prompt_caching: bool = True) -> BaseChatModel:
    """Return the chat model for ``name`` (cached per settings)."""
    if name.startswith(fake_llm.PREFIX):
        return fake_llm.scripted_model(name.removeprefix(fake_llm.PREFIX))
    options: dict[str, Any] = {}
    if prompt_caching and is_anthropic(name):
        options["model_kwargs"] = {"cache_control": PROMPT_CACHE_CONTROL}
    return init_chat_model(name, max_tokens=max_tokens, max_retries=max_retries, **options)


def tool_model(
    name: str,
    tools: list[BaseTool],
    *,
    max_tokens: int,
    max_retries: int,
    prompt_caching: bool = True,
) -> Runnable[Any, Any]:
    """Return ``name`` bound to ``tools`` and tagged so it never streams tokens."""
    model = chat_model(name, max_tokens, max_retries, prompt_caching)
    return model.bind_tools(tools).with_config({"tags": NOSTREAM_TAGS})


def cached_input_tokens(response: Any) -> int:
    """Return the input tokens a response read from the prompt cache (0 when none or unreported)."""
    metadata = getattr(response, "usage_metadata", None) or {}
    details = metadata.get("input_token_details") or {}
    return int(details.get("cache_read") or 0)
