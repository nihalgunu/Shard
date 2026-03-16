"""Tests for Shard data models."""

from __future__ import annotations

import json

import pytest

from shard.models import (
    AgentBackend,
    Collision,
    ExecutionGraph,
    PlannerProvider,
    RunConfig,
    RunStatus,
    TaskNode,
    TaskStatus,
    TaskTestResult,
    TokenUsage,
)


class TestTokenUsage:
    def test_to_dict(self) -> None:
        t = TokenUsage(prompt_tokens=100, completion_tokens=200)
        assert t.to_dict() == {"prompt_tokens": 100, "completion_tokens": 200}

    def test_from_dict(self) -> None:
        t = TokenUsage.from_dict({"prompt_tokens": 50, "completion_tokens": 75})
        assert t.prompt_tokens == 50
        assert t.completion_tokens == 75

    def test_from_dict_defaults(self) -> None:
        t = TokenUsage.from_dict({})
        assert t.prompt_tokens == 0
        assert t.completion_tokens == 0


class TestTaskNode:
    def test_roundtrip_serialization(self) -> None:
        node = TaskNode(
            task_id="task-001",
            title="Test task",
            prompt="Do something",
            owned_files=["src/main.py"],
            depends_on=["task-000"],
            test_files=["tests/test_main.py"],
            status=TaskStatus.RUNNING,
            priority=2,
            retry_count=1,
            token_usage=TokenUsage(100, 200),
        )
        d = node.to_dict()
        restored = TaskNode.from_dict(d)
        assert restored.task_id == "task-001"
        assert restored.status == TaskStatus.RUNNING
        assert restored.priority == 2
        assert restored.token_usage is not None
        assert restored.token_usage.prompt_tokens == 100
        assert restored.depends_on == ["task-000"]

    def test_minimal_node(self) -> None:
        node = TaskNode(
            task_id="t1",
            title="Minimal",
            prompt="p",
            owned_files=["a.py"],
        )
        assert node.status == TaskStatus.PENDING
        assert node.retry_count == 0
        assert node.depends_on == []

    def test_serialization_without_token_usage(self) -> None:
        node = TaskNode(task_id="t1", title="T", prompt="P", owned_files=["x.py"])
        d = node.to_dict()
        assert d["token_usage"] is None
        restored = TaskNode.from_dict(d)
        assert restored.token_usage is None


class TestPlannerProvider:
    def test_values(self) -> None:
        assert PlannerProvider.ANTHROPIC.value == "anthropic"
        assert PlannerProvider.OPENAI.value == "openai"

    def test_from_string(self) -> None:
        assert PlannerProvider("anthropic") == PlannerProvider.ANTHROPIC
        assert PlannerProvider("openai") == PlannerProvider.OPENAI


class TestRunConfig:
    def test_defaults(self) -> None:
        cfg = RunConfig()
        assert cfg.max_agents == 4
        assert cfg.agent_backend == AgentBackend.CLAUDE_CODE
        assert cfg.planner_provider == PlannerProvider.ANTHROPIC
        assert cfg.max_retries_per_task == 3
        assert cfg.max_retries_global == 5
        assert cfg.max_cost_usd == 5.00

    def test_roundtrip(self) -> None:
        cfg = RunConfig(max_agents=8, agent_backend=AgentBackend.AIDER)
        d = cfg.to_dict()
        restored = RunConfig.from_dict(d)
        assert restored.max_agents == 8
        assert restored.agent_backend == AgentBackend.AIDER


class TestExecutionGraph:
    def test_creation_and_branches(self) -> None:
        graph = ExecutionGraph(
            run_id="test-run",
            root_commit="abc123",
            target_branch="main",
        )
        assert graph.scaffold_branch == "wt/test-run/tests-scaffold"
        assert graph.staging_branch == "wt/test-run/staging"
        assert graph.status == RunStatus.PLANNING

    def test_get_node(self) -> None:
        node = TaskNode(task_id="t1", title="T", prompt="P", owned_files=["a.py"])
        graph = ExecutionGraph(
            run_id="r1", root_commit="abc", target_branch="main", nodes=[node]
        )
        assert graph.get_node("t1") is node
        assert graph.get_node("nonexistent") is None

    def test_get_runnable_nodes(self) -> None:
        n1 = TaskNode(task_id="t1", title="T1", prompt="P", owned_files=["a.py"],
                       status=TaskStatus.COMPLETED)
        n2 = TaskNode(task_id="t2", title="T2", prompt="P", owned_files=["b.py"],
                       depends_on=["t1"])
        n3 = TaskNode(task_id="t3", title="T3", prompt="P", owned_files=["c.py"],
                       depends_on=["t1", "t2"])
        graph = ExecutionGraph(
            run_id="r1", root_commit="abc", target_branch="main",
            nodes=[n1, n2, n3],
        )
        runnable = graph.get_runnable_nodes()
        assert len(runnable) == 1
        assert runnable[0].task_id == "t2"

    def test_get_runnable_no_deps(self) -> None:
        n1 = TaskNode(task_id="t1", title="T1", prompt="P", owned_files=["a.py"])
        n2 = TaskNode(task_id="t2", title="T2", prompt="P", owned_files=["b.py"])
        graph = ExecutionGraph(
            run_id="r1", root_commit="abc", target_branch="main",
            nodes=[n1, n2],
        )
        runnable = graph.get_runnable_nodes()
        assert len(runnable) == 2

    def test_roundtrip_serialization(self) -> None:
        node = TaskNode(task_id="t1", title="T", prompt="P", owned_files=["a.py"])
        graph = ExecutionGraph(
            run_id="r1", root_commit="abc", target_branch="main",
            nodes=[node],
            config=RunConfig(max_agents=8),
        )
        d = graph.to_dict()
        json_str = json.dumps(d)
        restored = ExecutionGraph.from_dict(json.loads(json_str))
        assert restored.run_id == "r1"
        assert len(restored.nodes) == 1
        assert restored.config.max_agents == 8


class TestCollision:
    def test_to_dict(self) -> None:
        c = Collision("t1", "t2", {"a.py", "b.py"})
        d = c.to_dict()
        assert d["task_a"] == "t1"
        assert d["overlapping_files"] == ["a.py", "b.py"]  # sorted


class TestTaskTestResult:
    def test_to_dict(self) -> None:
        r = TaskTestResult(
            test_name="test_foo", test_file="tests/test_foo.py",
            passed=False, message="assertion error", duration_s=0.5,
        )
        d = r.to_dict()
        assert d["passed"] is False
        assert d["duration_s"] == 0.5
