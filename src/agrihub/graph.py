"""Builder for the ``agrihub_study`` graph.

Flow: ``intake`` routes trait studies to ``model_agent`` and SNP studies to
``locus_builder``; then ``harvest`` and the ``orchestrator`` agent, which fans
out one ``Send("specialist")`` per dispatch (or finishes); the specialists
join in ``collect``, which returns to the orchestrator for a follow-up
decision while rounds are left; then ``rank_verify`` and ``writer``.
"""

from typing import Any

from langgraph.graph import END, START, StateGraph

from agrihub.nodes.collect import collect
from agrihub.nodes.followup import ArtifactReader, make_followup
from agrihub.nodes.harvest import harvest
from agrihub.nodes.intake import intake, route_after_intake
from agrihub.nodes.locus_builder import locus_builder
from agrihub.nodes.model_agent import model_agent
from agrihub.nodes.orchestrator import orchestrator
from agrihub.nodes.rank_verify import rank_verify
from agrihub.nodes.specialist import specialist
from agrihub.nodes.writer import ArtifactSink, make_writer
from agrihub.state import StudyState

GRAPH_ID = "agrihub_study"


def route_entry(state: StudyState) -> str:
    """Send a follow-up question on a finished study to the read-only Q&A node."""
    if str(state.get("followup") or "").strip() and state.get("report"):
        return "followup_qa"
    return "intake"


def build_study_graph(
    *,
    checkpointer: Any = None,
    store: Any = None,
    artifact_sink: ArtifactSink | None = None,
    artifact_reader: ArtifactReader | None = None,
) -> Any:
    """Compile the study graph with the caller's persistence.

    ``artifact_sink`` stores the writer's artifacts; ``artifact_reader``
    lets follow-up runs restore the evidence store from them.
    """
    builder = StateGraph(StudyState)
    builder.add_node("intake", intake)
    builder.add_node("model_agent", model_agent)
    builder.add_node("locus_builder", locus_builder)
    builder.add_node("harvest", harvest)
    builder.add_node("orchestrator", orchestrator, destinations=("specialist", "rank_verify"))
    builder.add_node("specialist", specialist)
    builder.add_node("collect", collect, destinations=("orchestrator", "rank_verify"))
    builder.add_node("rank_verify", rank_verify)
    builder.add_node("writer", make_writer(artifact_sink))
    builder.add_node("followup_qa", make_followup(artifact_reader))

    builder.add_conditional_edges(START, route_entry, ["followup_qa", "intake"])
    builder.add_edge("followup_qa", END)
    builder.add_conditional_edges(
        "intake",
        route_after_intake,
        ["model_agent", "locus_builder"],
    )
    builder.add_edge("model_agent", "locus_builder")
    builder.add_edge("locus_builder", "harvest")
    builder.add_edge("harvest", "orchestrator")
    builder.add_edge("specialist", "collect")
    builder.add_edge("rank_verify", "writer")
    builder.add_edge("writer", END)
    return builder.compile(checkpointer=checkpointer, store=store, name=GRAPH_ID)
