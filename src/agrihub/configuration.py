"""Runtime configuration for the ``agrihub_study`` graph."""

import os
from pathlib import Path
from typing import Any

from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, Field

from agent_platform.core.settings import get_data_paths

DEFAULT_MODEL = "anthropic:claude-sonnet-4-20250514"
MODEL_FIELDS = frozenset(
    {
        "orchestrator_model",
        "specialist_model",
        "verifier_model",
        "writer_model",
        "qa_model",
    }
)
# Filesystem locations come from server settings only; a client-supplied
# configurable must never redirect bundle reads.
SERVER_FIELDS = frozenset({"data_dir"})


def data_root() -> Path:
    """Return ``AGRIHUB_DATA_DIR``, which holds one bundle directory per species."""
    return get_data_paths().data_dir


def run_root() -> Path:
    """Return ``AGRIHUB_RUN_DIR``, which holds one store directory per run."""
    return get_data_paths().run_dir


class StudyConfiguration(BaseModel):
    """Per-role models, research budgets, and data locations."""

    orchestrator_model: str = DEFAULT_MODEL
    specialist_model: str = DEFAULT_MODEL
    verifier_model: str = DEFAULT_MODEL
    writer_model: str = DEFAULT_MODEL
    qa_model: str = DEFAULT_MODEL

    max_rounds: int = Field(default=2, ge=1)
    """Research rounds including the first; 2 allows one follow-up round."""
    max_specialist_steps: int = Field(default=8, ge=1)
    max_orchestrator_steps: int = Field(default=6, ge=2)
    max_focus_genes: int = Field(default=12, ge=1)
    """Genes one dispatch may give a specialist; extra genes are dropped and reported."""
    top_k_per_locus: int = Field(default=5, ge=1)
    history_tool_results: int = Field(default=6, ge=1)
    """Tool results an agent sees verbatim; older ones are reduced to a one-line digest."""
    tool_output_chars: int = Field(default=6_000, ge=500)
    model_max_tokens: int = Field(default=4_096, ge=256)
    model_max_retries: int = Field(default=3, ge=0)
    enable_web_fallback: bool = True
    data_dir: str = Field(default_factory=lambda: str(data_root()))

    @classmethod
    def from_runnable_config(
        cls,
        config: RunnableConfig | None = None,
    ) -> "StudyConfiguration":
        """Build configuration from environment and run-level overrides.

        Every model role falls back to the shared ``MODEL`` variable.
        ``data_dir`` always comes from ``AGRIHUB_DATA_DIR``.
        """
        configurable = config.get("configurable", {}) if config else {}
        shared_model = os.environ.get("MODEL")

        def pick(field_name: str) -> Any:
            if os.environ.get(field_name.upper()):
                return os.environ[field_name.upper()]
            if configurable.get(field_name) not in (None, ""):
                return configurable[field_name]
            return shared_model if field_name in MODEL_FIELDS else None

        values: dict[str, Any] = {
            field_name: pick(field_name) for field_name in cls.model_fields if field_name not in SERVER_FIELDS
        }
        return cls(**{key: value for key, value in values.items() if value is not None})


def run_id_from_config(config: RunnableConfig) -> str:
    """Return the platform run id that keys the evidence store.

    The run manager writes it to ``configurable.run_id`` and cannot be
    overridden by the client.
    """
    run_id = (config.get("configurable") or {}).get("run_id")
    if not run_id:
        raise ValueError("agrihub_study needs configurable.run_id to open its evidence store")
    return str(run_id)


def agent_id_from_config(config: RunnableConfig) -> str | None:
    """Return the dispatch id of the specialist this call runs under."""
    agent_id = (config.get("metadata") or {}).get("agrihub_agent_id")
    return str(agent_id) if agent_id else None
