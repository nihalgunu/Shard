"""Tests for state persistence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from worktree.models import ExecutionGraph, RunConfig, TaskNode, TaskStatus
from worktree.state import StateManager


class TestStateManager:
    def setup_method(self) -> None:
        self.run_id = "test-run-001"

    def test_initialize_creates_dirs(self, tmp_path: Path) -> None:
        state = StateManager(tmp_path, self.run_id)
        state.initialize()
        assert (tmp_path / ".worktree" / "journal").is_dir()
        assert (tmp_path / ".worktree" / "prompts").is_dir()
        assert (tmp_path / ".worktree" / "logs").is_dir()
        assert (tmp_path / ".worktree" / "checkpoints").is_dir()

    def test_save_and_load_graph(self, tmp_path: Path) -> None:
        state = StateManager(tmp_path, self.run_id)
        state.initialize()

        node = TaskNode(task_id="t1", title="T", prompt="P", owned_files=["a.py"])
        graph = ExecutionGraph(
            run_id=self.run_id,
            root_commit="abc123",
            target_branch="main",
            nodes=[node],
        )

        state.save_graph(graph)
        loaded = state.load_graph()

        assert loaded is not None
        assert loaded.run_id == self.run_id
        assert len(loaded.nodes) == 1
        assert loaded.nodes[0].task_id == "t1"

    def test_load_graph_nonexistent(self, tmp_path: Path) -> None:
        state = StateManager(tmp_path, self.run_id)
        state.initialize()
        assert state.load_graph() is None

    def test_append_and_read_journal(self, tmp_path: Path) -> None:
        state = StateManager(tmp_path, self.run_id)
        state.initialize()

        state.append_event("task_started", {"task_id": "t1"})
        state.append_event("task_completed", {"task_id": "t1", "status": "COMPLETED"})

        events = state.read_journal()
        assert len(events) == 2
        assert events[0]["event"] == "task_started"
        assert events[1]["event"] == "task_completed"
        assert events[0]["run_id"] == self.run_id

    def test_save_and_load_checkpoint(self, tmp_path: Path) -> None:
        state = StateManager(tmp_path, self.run_id)
        state.initialize()

        graph = ExecutionGraph(
            run_id=self.run_id,
            root_commit="abc",
            target_branch="main",
        )
        state.save_checkpoint(graph)
        loaded = state.load_checkpoint()
        assert loaded is not None
        assert loaded.run_id == self.run_id

    def test_write_prompt_file(self, tmp_path: Path) -> None:
        state = StateManager(tmp_path, self.run_id)
        state.initialize()

        node = TaskNode(
            task_id="task-001",
            title="Test Task",
            prompt="Implement feature X",
            owned_files=["src/x.py", "src/y.py"],
        )
        path = state.write_prompt_file(node)

        assert path.exists()
        content = path.read_text()
        assert "Test Task" in content
        assert "Implement feature X" in content
        assert "src/x.py" in content
        assert "MUST only create or modify" in content
        assert node.prompt_file == str(path)

    def test_get_log_paths(self, tmp_path: Path) -> None:
        state = StateManager(tmp_path, self.run_id)
        stdout, stderr = state.get_log_paths("task-001")
        assert "task-001.stdout.log" in str(stdout)
        assert "task-001.stderr.log" in str(stderr)

    def test_checkpoint_task(self, tmp_path: Path) -> None:
        state = StateManager(tmp_path, self.run_id)
        state.initialize()

        node = TaskNode(task_id="t1", title="T", prompt="P", owned_files=["a.py"],
                         status=TaskStatus.RUNNING)
        graph = ExecutionGraph(
            run_id=self.run_id, root_commit="abc", target_branch="main",
            nodes=[node],
        )

        state.checkpoint_task(graph, node, "task_started")

        events = state.read_journal()
        assert len(events) == 1
        assert events[0]["data"]["task_id"] == "t1"
        assert events[0]["data"]["status"] == "RUNNING"

        # Graph should be saved
        loaded = state.load_graph()
        assert loaded is not None
