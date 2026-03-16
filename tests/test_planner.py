"""Tests for the planning engine (Stage 1)."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from shard.models import ExecutionGraph, RunConfig, TaskNode, TaskStatus
from shard.planner import (
    _extract_json,
    build_file_tree,
    compute_critical_path,
    detect_collisions,
    detect_language,
    find_cycle,
    parse_planner_response,
    resolve_collisions_by_serialization,
    topological_sort,
    validate_acyclicity,
    validate_dag_schema,
    validate_file_paths,
)


class TestBuildFileTree:
    def test_basic_tree(self, tmp_path: Path) -> None:
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").touch()
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_main.py").touch()
        (tmp_path / "README.md").touch()

        tree = build_file_tree(tmp_path)
        assert "src" in tree
        assert "main.py" in tree
        assert "tests" in tree
        assert "README.md" in tree

    def test_ignores_git_dir(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "config").touch()
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").touch()

        tree = build_file_tree(tmp_path)
        assert ".git" not in tree
        assert "main.py" in tree

    def test_respects_max_depth(self, tmp_path: Path) -> None:
        # Create deeply nested structure
        current = tmp_path
        for i in range(10):
            current = current / f"level{i}"
            current.mkdir()
            (current / "file.py").touch()

        tree = build_file_tree(tmp_path, max_depth=3)
        assert "level0" in tree
        assert "level1" in tree
        assert "level2" in tree
        # Depth 3 may or may not appear depending on how counting works
        # but deeper levels should not
        assert "level8" not in tree


class TestDetectLanguage:
    def test_python_project(self, tmp_path: Path) -> None:
        (tmp_path / "main.py").touch()
        (tmp_path / "utils.py").touch()
        (tmp_path / "README.md").touch()
        assert detect_language(tmp_path) == "Python"

    def test_javascript_project(self, tmp_path: Path) -> None:
        (tmp_path / "index.js").touch()
        (tmp_path / "app.js").touch()
        (tmp_path / "package.json").touch()
        assert detect_language(tmp_path) == "JavaScript"

    def test_empty_project(self, tmp_path: Path) -> None:
        assert detect_language(tmp_path) == "Unknown"


class TestValidateDagSchema:
    def test_valid_dag(self) -> None:
        data = {
            "nodes": [
                {
                    "task_id": "t1",
                    "title": "Task 1",
                    "prompt": "Do thing",
                    "owned_files": ["a.py"],
                }
            ]
        }
        errors = validate_dag_schema(data)
        assert errors == []

    def test_missing_required_field(self) -> None:
        data = {
            "nodes": [
                {
                    "task_id": "t1",
                    "title": "Task 1",
                    # missing prompt and owned_files
                }
            ]
        }
        errors = validate_dag_schema(data)
        assert len(errors) > 0

    def test_empty_nodes(self) -> None:
        data = {"nodes": []}
        errors = validate_dag_schema(data)
        assert len(errors) > 0  # minItems: 1


class TestValidateAcyclicity:
    def test_valid_dag(self) -> None:
        nodes = [
            TaskNode(task_id="t1", title="T", prompt="P", owned_files=["a.py"]),
            TaskNode(task_id="t2", title="T", prompt="P", owned_files=["b.py"],
                     depends_on=["t1"]),
        ]
        assert validate_acyclicity(nodes) is True

    def test_cycle_detected(self) -> None:
        nodes = [
            TaskNode(task_id="t1", title="T", prompt="P", owned_files=["a.py"],
                     depends_on=["t2"]),
            TaskNode(task_id="t2", title="T", prompt="P", owned_files=["b.py"],
                     depends_on=["t1"]),
        ]
        assert validate_acyclicity(nodes) is False

    def test_find_cycle(self) -> None:
        nodes = [
            TaskNode(task_id="t1", title="T", prompt="P", owned_files=["a.py"],
                     depends_on=["t2"]),
            TaskNode(task_id="t2", title="T", prompt="P", owned_files=["b.py"],
                     depends_on=["t1"]),
        ]
        cycle = find_cycle(nodes)
        assert cycle is not None
        assert "t1" in cycle and "t2" in cycle


class TestDetectCollisions:
    def test_no_collisions(self) -> None:
        n1 = TaskNode(task_id="t1", title="T", prompt="P", owned_files=["a.py"])
        n2 = TaskNode(task_id="t2", title="T", prompt="P", owned_files=["b.py"])
        graph = ExecutionGraph(run_id="r", root_commit="c", target_branch="main",
                                nodes=[n1, n2])
        collisions = detect_collisions(graph)
        assert collisions == []

    def test_collision_detected(self) -> None:
        n1 = TaskNode(task_id="t1", title="T", prompt="P", owned_files=["a.py", "shared.py"])
        n2 = TaskNode(task_id="t2", title="T", prompt="P", owned_files=["b.py", "shared.py"])
        graph = ExecutionGraph(run_id="r", root_commit="c", target_branch="main",
                                nodes=[n1, n2])
        collisions = detect_collisions(graph)
        assert len(collisions) == 1
        assert "shared.py" in collisions[0].overlapping_files

    def test_no_collision_when_dependency_exists(self) -> None:
        n1 = TaskNode(task_id="t1", title="T", prompt="P", owned_files=["shared.py"])
        n2 = TaskNode(task_id="t2", title="T", prompt="P",
                       owned_files=["shared.py"], depends_on=["t1"])
        graph = ExecutionGraph(run_id="r", root_commit="c", target_branch="main",
                                nodes=[n1, n2])
        collisions = detect_collisions(graph)
        assert collisions == []


class TestResolveCollisions:
    def test_auto_serialize(self) -> None:
        n1 = TaskNode(task_id="t1", title="T", prompt="P",
                       owned_files=["shared.py"], priority=1)
        n2 = TaskNode(task_id="t2", title="T", prompt="P",
                       owned_files=["shared.py"], priority=2)
        graph = ExecutionGraph(run_id="r", root_commit="c", target_branch="main",
                                nodes=[n1, n2])
        collisions = detect_collisions(graph)
        assert len(collisions) == 1

        resolve_collisions_by_serialization(graph, collisions)
        # n2 (higher priority) should now depend on n1 (lower priority)
        assert "t1" in n2.depends_on


class TestTopologicalSort:
    def test_simple_sort(self) -> None:
        n1 = TaskNode(task_id="t1", title="T", prompt="P", owned_files=["a.py"])
        n2 = TaskNode(task_id="t2", title="T", prompt="P", owned_files=["b.py"],
                       depends_on=["t1"])
        n3 = TaskNode(task_id="t3", title="T", prompt="P", owned_files=["c.py"],
                       depends_on=["t1", "t2"])
        graph = ExecutionGraph(run_id="r", root_commit="c", target_branch="main",
                                nodes=[n1, n2, n3])
        order = topological_sort(graph)
        assert order.index("t1") < order.index("t2")
        assert order.index("t2") < order.index("t3")


class TestCriticalPath:
    def test_critical_path(self) -> None:
        n1 = TaskNode(task_id="t1", title="T", prompt="P", owned_files=["a.py"])
        n2 = TaskNode(task_id="t2", title="T", prompt="P", owned_files=["b.py"],
                       depends_on=["t1"])
        n3 = TaskNode(task_id="t3", title="T", prompt="P", owned_files=["c.py"],
                       depends_on=["t2"])
        n4 = TaskNode(task_id="t4", title="T", prompt="P", owned_files=["d.py"])
        graph = ExecutionGraph(run_id="r", root_commit="c", target_branch="main",
                                nodes=[n1, n2, n3, n4])
        path = compute_critical_path(graph)
        assert path == ["t1", "t2", "t3"]


class TestValidateFilePaths:
    def test_valid_paths(self, tmp_path: Path) -> None:
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").touch()
        nodes = [
            TaskNode(task_id="t1", title="T", prompt="P", owned_files=["src/main.py"])
        ]
        errors = validate_file_paths(nodes, tmp_path)
        assert errors == []

    def test_invalid_parent_dir(self, tmp_path: Path) -> None:
        nodes = [
            TaskNode(task_id="t1", title="T", prompt="P",
                     owned_files=["nonexistent/dir/file.py"])
        ]
        errors = validate_file_paths(nodes, tmp_path)
        assert len(errors) == 1
        assert "nonexistent" in errors[0]

    def test_new_file_in_existing_dir(self, tmp_path: Path) -> None:
        (tmp_path / "src").mkdir()
        nodes = [
            TaskNode(task_id="t1", title="T", prompt="P",
                     owned_files=["src/new_module.py"])
        ]
        errors = validate_file_paths(nodes, tmp_path)
        assert errors == []


class TestExtractJson:
    def test_plain_json(self) -> None:
        text = '{"nodes": []}'
        assert json.loads(_extract_json(text)) == {"nodes": []}

    def test_json_in_code_block(self) -> None:
        text = 'Here is the plan:\n```json\n{"nodes": [{"task_id": "t1"}]}\n```\nDone.'
        result = json.loads(_extract_json(text))
        assert result["nodes"][0]["task_id"] == "t1"

    def test_json_in_generic_code_block(self) -> None:
        text = '```\n{"nodes": []}\n```'
        assert json.loads(_extract_json(text)) == {"nodes": []}

    def test_json_with_surrounding_text(self) -> None:
        text = 'The output is {"nodes": []} and that is all.'
        assert json.loads(_extract_json(text)) == {"nodes": []}


class TestParsePlannerResponse:
    def test_valid_response(self) -> None:
        data = {
            "nodes": [
                {
                    "task_id": "t1",
                    "title": "Task 1",
                    "prompt": "Do something",
                    "owned_files": ["a.py"],
                    "depends_on": [],
                    "test_files": ["tests/test_a.py"],
                    "priority": 1,
                }
            ],
            "test_code": {
                "tests/test_a.py": "import pytest\ndef test_a(): pass",
            },
        }
        nodes, test_code = parse_planner_response(data)
        assert len(nodes) == 1
        assert nodes[0].task_id == "t1"
        assert "tests/test_a.py" in test_code

    def test_invalid_schema(self) -> None:
        data = {"nodes": [{"task_id": "t1"}]}  # missing required fields
        with pytest.raises(ValueError, match="Invalid planner response"):
            parse_planner_response(data)

    def test_cyclic_dag_rejected(self) -> None:
        data = {
            "nodes": [
                {
                    "task_id": "t1",
                    "title": "T",
                    "prompt": "P",
                    "owned_files": ["a.py"],
                    "depends_on": ["t2"],
                },
                {
                    "task_id": "t2",
                    "title": "T",
                    "prompt": "P",
                    "owned_files": ["b.py"],
                    "depends_on": ["t1"],
                },
            ]
        }
        with pytest.raises(ValueError, match="cycle"):
            parse_planner_response(data)
