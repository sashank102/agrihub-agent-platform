"""Static graph resolution for the local FastAPI server."""

import uuid
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class RegisteredGraph:
    """A compiled graph and its persisted agent identity."""

    graph_id: str
    agent_id: uuid.UUID
    graph: Any


class GraphRegistry:
    """Resolve configured graph or assistant identifiers."""

    def __init__(self) -> None:
        """Create an empty registry."""
        self._graphs: dict[str, RegisteredGraph] = {}

    def register(self, graph_id: str, agent_id: uuid.UUID, graph: Any) -> None:
        """Register both the graph name and assistant UUID."""
        entry = RegisteredGraph(graph_id=graph_id, agent_id=agent_id, graph=graph)
        self._graphs[graph_id] = entry
        self._graphs[str(agent_id)] = entry

    def resolve(self, identifier: str) -> RegisteredGraph | None:
        """Return a graph by graph ID or assistant UUID."""
        return self._graphs.get(identifier)
