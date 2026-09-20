"""Configuration management for the research agent."""

import os
from enum import Enum
from typing import Any

from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, ConfigDict


class SearchAPI(str, Enum):
    """Available web-search providers."""

    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    TAVILY = "tavily"
    DUCKDUCKGO = "duckduckgo"
    NONE = "none"


class MCPConfig(BaseModel):
    """One streamable-HTTP MCP server and its allowed tools."""

    url: str | None = None
    tools: list[str] | None = None


class Configuration(BaseModel):
    """Runtime configuration for the research graph."""

    max_structured_output_retries: int = 3
    allow_clarification: bool = True
    max_concurrent_research_units: int = 5
    search_api: SearchAPI = SearchAPI.ANTHROPIC
    max_researcher_iterations: int = 6
    max_react_tool_calls: int = 10

    summarization_model: str = "anthropic:claude-sonnet-4-20250514"
    summarization_model_max_tokens: int = 8192
    max_content_length: int = 50000
    research_model: str = "anthropic:claude-sonnet-4-20250514"
    research_model_max_tokens: int = 10000
    compression_model: str = "anthropic:claude-sonnet-4-20250514"
    compression_model_max_tokens: int = 8192
    final_report_model: str = "anthropic:claude-sonnet-4-20250514"
    final_report_model_max_tokens: int = 10000

    mcp_config: MCPConfig | None = None
    mcp_prompt: str | None = None

    model_config = ConfigDict(arbitrary_types_allowed=True)

    @classmethod
    def from_runnable_config(
        cls,
        config: RunnableConfig | None = None,
    ) -> "Configuration":
        """Build configuration from environment and run-level overrides."""
        configurable = config.get("configurable", {}) if config else {}
        shared_model = os.environ.get("MODEL")
        model_fields = {
            "summarization_model",
            "research_model",
            "compression_model",
            "final_report_model",
        }
        values: dict[str, Any] = {
            field_name: os.environ.get(field_name.upper())
            or configurable.get(field_name)
            or (shared_model if field_name in model_fields else None)
            for field_name in cls.model_fields
        }
        return cls(**{key: value for key, value in values.items() if value is not None})
