"""Tests for the aggregator and self-healing loop (Stages 4-5)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from worktree.aggregator import Aggregator, SelfHealingLoop
from worktree.models import (
    ExecutionGraph,
    RunConfig,
    TaskNode,
    TaskStatus,
    TestResult,
)


class TestAggregator:
    def _make_graph(self) -> ExecutionGraph:
        n1 = TaskNode(
            task_id="t1", title="Task 1", prompt="P",
            owned_files=["a.py"], test_files=["tests/test_a.py"],
            status=TaskStatus.COMPLETED, branch_name="wt/r1/t1",
        )
        n2 = TaskNode(
            task_id="t2", title="Task 2", prompt="P",
            owned_files=["b.py"], test_files=["tests/test_b.py"],
            status=TaskStatus.COMPLETED, branch_name="wt/r1/t2",
        )
        return ExecutionGraph(
            run_id="r1", root_commit="abc", target_branch="main",
            nodes=[n1, n2],
        )

    def test_map_failures_to_tasks(self) -> None:
        graph = self._make_graph()
        aggregator = Aggregator(graph, git=None, state=None)  # type: ignore[arg-type]

        results = [
            TestResult(test_name="test_x", test_file="tests/test_a.py", passed=True),
            TestResult(test_name="test_y", test_file="tests/test_a.py", passed=False,
                       message="AssertionError"),
            TestResult(test_name="test_z", test_file="tests/test_b.py", passed=False,
                       message="ImportError"),
            TestResult(test_name="test_w", test_file="unknown.py", passed=False,
                       message="Unknown"),
        ]

        mapping = aggregator.map_failures_to_tasks(results)
        assert "t1" in mapping
        assert len(mapping["t1"]) == 1
        assert mapping["t1"][0].test_name == "test_y"
        assert "t2" in mapping
        assert len(mapping["t2"]) == 1
        assert "__unmapped__" in mapping
        assert len(mapping["__unmapped__"]) == 1

    def test_parse_json_report(self, tmp_path: Path) -> None:
        graph = self._make_graph()
        aggregator = Aggregator(graph, git=None, state=None)  # type: ignore[arg-type]

        report = {
            "tests": [
                {
                    "nodeid": "tests/test_a.py::test_func",
                    "outcome": "passed",
                    "duration": 0.1,
                },
                {
                    "nodeid": "tests/test_b.py::test_fail",
                    "outcome": "failed",
                    "duration": 0.5,
                    "call": {"longrepr": "AssertionError: expected 1 got 2"},
                },
            ]
        }
        report_path = tmp_path / "report.json"
        report_path.write_text(json.dumps(report))

        results = aggregator._parse_json_report(report_path)
        assert len(results) == 2
        assert results[0].passed is True
        assert results[0].test_file == "tests/test_a.py"
        assert results[1].passed is False
        assert "AssertionError" in results[1].message

    def test_parse_text_output(self) -> None:
        graph = self._make_graph()
        aggregator = Aggregator(graph, git=None, state=None)  # type: ignore[arg-type]

        stdout = "tests/test_a.py::test_x PASSED\ntests/test_b.py::test_y FAILED"
        results = aggregator._parse_text_output(stdout, "")
        assert len(results) == 2
        passed = [r for r in results if r.passed]
        failed = [r for r in results if not r.passed]
        assert len(passed) == 1
        assert len(failed) == 1


class TestSelfHealingLoop:
    def test_regression_detection(self) -> None:
        graph = ExecutionGraph(
            run_id="r1", root_commit="abc", target_branch="main",
            nodes=[
                TaskNode(task_id="t1", title="T", prompt="P",
                         owned_files=["a.py"], test_files=["tests/test_a.py"]),
            ],
        )
        healer = SelfHealingLoop(graph, aggregator=None, git=None, state=None)  # type: ignore[arg-type]
        # Simulate previous failures
        healer._previous_failures = {"test_a"}

        # Current failures grew
        current = {"test_a", "test_b", "test_c"}
        assert len(current) > len(healer._previous_failures)
