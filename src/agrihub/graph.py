"""Builder for the ``agrihub_study`` graph.

Flow: ``intake`` routes trait studies to ``model_agent`` and SNP studies to
``locus_builder``; then ``harvest`` and ``orchestrator``, which fans out one
``Send("specialist")`` per enabled specialist; then ``collect``,
``rank_verify`` and ``writer``.
"""

from typing import Any

from langgraph.graph import END, START, StateGraph

from agrihub.nodes.collect import collect
from agrihub.nodes.harvest import harvest
from agrihub.nodes.intake import intake, route_after_intake
from agrihub.nodes.locus_builder import locus_builder
from agrihub.nodes.model_agent import model_agent
from agrihub.nodes.orchestrator import orchestrator
from agrihub.nodes.rank_verify import rank_verify
from agrihub.nodes.specialists import specialist
from agrihub.nodes.writer import ArtifactSink, make_writer
from agrihub.state import StudyState

GRAPH_ID = "agrihub_study"


def build_study_graph(
    *,
    checkpointer: Any = None,
    store: Any = None,
    artifact_sink: ArtifactSink | None = None,
) -> Any:
    """Compile the study graph with the caller's persistence."""
    builder = StateGraph(StudyState)
    builder.add_node("intake", intake)
    builder.add_node("model_agent", model_agent)
    builder.add_node("locus_builder", locus_builder)
    builder.add_node("harvest", harvest)
    builder.add_node("orchestrator", orchestrator, destinations=("specialist",))
    builder.add_node("specialist", specialist)
    builder.add_node("collect", collect)
    builder.add_node("rank_verify", rank_verify)
    builder.add_node("writer", make_writer(artifact_sink))

    builder.add_edge(START, "intake")
    builder.add_conditional_edges(
        "intake",
        route_after_intake,
        ["model_agent", "locus_builder"],
    )
    builder.add_edge("model_agent", "locus_builder")
    builder.add_edge("locus_builder", "harvest")
    builder.add_edge("harvest", "orchestrator")
    builder.add_edge("specialist", "collect")
    builder.add_edge("collect", "rank_verify")
    builder.add_edge("rank_verify", "writer")
    builder.add_edge("writer", END)
    return builder.compile(checkpointer=checkpointer, store=store, name=GRAPH_ID)
