"""Runtime configuration for the ``agrihub_study`` graph."""

import os
from pathlib import Path
from typing import Any

from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).resolve().parents[2]
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


class StudyConfiguration(BaseModel):
    """Per-role models, research budgets, and data locations."""

    orchestrator_model: str = DEFAULT_MODEL
    specialist_model: str = DEFAULT_MODEL
    verifier_model: str = DEFAULT_MODEL
    writer_model: str = DEFAULT_MODEL
    qa_model: str = DEFAULT_MODEL

    max_rounds: int = Field(default=2, ge=1)
    max_specialist_steps: int = Field(default=8, ge=1)
    top_k_per_locus: int = Field(default=5, ge=1)
    data_dir: str = str(PROJECT_ROOT / "var" / "data")

    @classmethod
    def from_runnable_config(
        cls,
        config: RunnableConfig | None = None,
    ) -> "StudyConfiguration":
        """Build configuration from environment and run-level overrides.

        Every model role falls back to the shared ``MODEL`` variable.
        """
        configurable = config.get("configurable", {}) if config else {}
        shared_model = os.environ.get("MODEL")
        values: dict[str, Any] = {
            field_name: os.environ.get(field_name.upper())
            or configurable.get(field_name)
            or (shared_model if field_name in MODEL_FIELDS else None)
            for field_name in cls.model_fields
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
