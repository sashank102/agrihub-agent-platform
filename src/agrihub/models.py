"""Chat models for the agents, chosen per role in ``StudyConfiguration``.

Any ``provider:model`` string that ``init_chat_model`` understands works
(``anthropic:...``, ``openai:...``); calls use the model's own
``max_retries`` and a ``max_tokens`` cap, and are tagged
``langsmith:nostream`` so token chunks never reach the run stream. Names
starting with ``agrihub-fake:`` select a scripted deterministic model from
``agrihub.fake_llm`` for tests, fixture recording and offline demos.
"""

from functools import lru_cache
from typing import Any

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool

from agrihub import fake_llm

NOSTREAM_TAGS = ["langsmith:nostream"]


@lru_cache(maxsize=32)
def chat_model(name: str, max_tokens: int, max_retries: int) -> BaseChatModel:
    """Return the chat model for ``name`` (cached per settings)."""
    if name.startswith(fake_llm.PREFIX):
        return fake_llm.scripted_model(name.removeprefix(fake_llm.PREFIX))
    return init_chat_model(name, max_tokens=max_tokens, max_retries=max_retries)


def tool_model(name: str, tools: list[BaseTool], *, max_tokens: int, max_retries: int) -> Runnable[Any, Any]:
    """Return ``name`` bound to ``tools`` and tagged so it never streams tokens."""
    model = chat_model(name, max_tokens, max_retries)
    return model.bind_tools(tools).with_config({"tags": NOSTREAM_TAGS})
