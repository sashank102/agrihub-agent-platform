import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END
from langgraph.store.memory import InMemoryStore

import open_deep_research.deep_researcher as graph_module
from open_deep_research.deep_researcher import (
    build_graph,
    deep_researcher,
    supervisor_tools,
)
from open_deep_research.prompts import CROP_PROMPT_PACK, DEFAULT_PROMPT_PACK


class FailingResearcherGraph:
    def __init__(self, error):
        self.error = error

    async def ainvoke(self, state, config):
        raise self.error


class CapturingModel:
    def __init__(self):
        self.invocations = []

    def with_structured_output(self, schema):
        return self

    def with_retry(self, **kwargs):
        return self

    def with_config(self, config):
        return self

    async def ainvoke(self, messages):
        self.invocations.append(messages)
        return SimpleNamespace(
            need_clarification=True,
            question="Please clarify.",
            verification="",
        )


def supervisor_state():
    return {
        "supervisor_messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "ConductResearch",
                        "args": {"research_topic": "A focused topic"},
                        "id": "research-call",
                        "type": "tool_call",
                    }
                ],
            )
        ],
        "research_brief": "Research this topic.",
        "research_iterations": 1,
    }


def test_build_graph_compiles_without_persistence():
    graph = build_graph()

    assert graph.checkpointer is None
    assert graph.store is None
    assert "final_report_generation" in graph.get_graph().nodes


def test_build_graph_accepts_injected_checkpointer_and_store():
    checkpointer = MemorySaver()
    store = InMemoryStore()

    graph = build_graph(checkpointer=checkpointer, store=store)

    assert graph.checkpointer is checkpointer
    assert graph.store is store


def test_deep_researcher_export_remains_compiled_and_valid():
    assert deep_researcher.get_graph().nodes["clarify_with_user"]
    assert deep_researcher.get_graph().nodes["final_report_generation"]


def test_supervisor_token_limit_failure_ends_with_partial_results(monkeypatch):
    monkeypatch.setattr(
        graph_module,
        "is_token_limit_exceeded",
        lambda error, model: True,
    )

    command = asyncio.run(
        supervisor_tools(
            supervisor_state(),
            {"configurable": {"research_model": "test:model"}},
            researcher_graph=FailingResearcherGraph(RuntimeError("context too long")),
        )
    )

    assert command.goto == END
    assert command.update["research_brief"] == "Research this topic."


def test_supervisor_unrelated_failure_is_propagated_with_context(monkeypatch):
    monkeypatch.setattr(
        graph_module,
        "is_token_limit_exceeded",
        lambda error, model: False,
    )

    with pytest.raises(RuntimeError, match="1 delegated research task") as exc_info:
        asyncio.run(
            supervisor_tools(
                supervisor_state(),
                {"configurable": {"research_model": "test:model"}},
                researcher_graph=FailingResearcherGraph(
                    ValueError("provider unavailable")
                ),
            )
        )

    assert "test:model" in str(exc_info.value)
    assert "provider unavailable" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, ValueError)


def test_prompt_pack_injection_preserves_crop_default(monkeypatch):
    model = CapturingModel()
    monkeypatch.setattr(graph_module, "configurable_model", model)
    injected_pack = replace(
        DEFAULT_PROMPT_PACK,
        clarify_with_user_instructions=(
            "INJECTED PROMPT\nMessages: {messages}\nDate: {date}"
        ),
    )

    graph = build_graph(prompt_pack=injected_pack)
    asyncio.run(
        graph.ainvoke(
            {"messages": [HumanMessage(content="Investigate this request.")]},
        )
    )

    assert model.invocations[0][0].content.startswith("INJECTED PROMPT")
    assert DEFAULT_PROMPT_PACK is CROP_PROMPT_PACK
    assert "AgriHub" in DEFAULT_PROMPT_PACK.clarify_with_user_instructions
    assert "crop candidate-gene" in DEFAULT_PROMPT_PACK.lead_researcher_prompt
