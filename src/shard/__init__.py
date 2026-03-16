"""Shard: A TDD-Driven, Parallelized AI Coding Orchestrator."""

__version__ = "1.0.0rc2"

from shard.models import (
    AgentBackend,
    ExecutionGraph,
    PlannerProvider,
    RunConfig,
    RunStatus,
    TaskNode,
    TaskStatus,
)
from shard.planner import APIKeyError

__all__ = [
    "__version__",
    "AgentBackend",
    "APIKeyError",
    "ExecutionGraph",
    "PlannerProvider",
    "RunConfig",
    "RunStatus",
    "TaskNode",
    "TaskStatus",
]
