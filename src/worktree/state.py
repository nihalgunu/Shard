"""State persistence: journal, checkpoints, and atomic graph writes."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

import msgpack

from worktree.models import ExecutionGraph, TaskNode, TaskStatus


class StateManager:
    """Manages execution state persistence with write-ahead journal."""

    def __init__(self, repo_root: Path, run_id: str) -> None:
        self.repo_root = repo_root
        self.run_id = run_id
        self.state_dir = repo_root / ".worktree"
        self.journal_dir = self.state_dir / "journal"
        self.prompts_dir = self.state_dir / "prompts"
        self.logs_dir = self.state_dir / "logs"
        self.checkpoints_dir = self.state_dir / "checkpoints"
        self._journal_file: Path | None = None
        self._journal_seq = 0

    def initialize(self) -> None:
        """Create the .worktree directory structure."""
        for d in [self.journal_dir, self.prompts_dir, self.logs_dir, self.checkpoints_dir]:
            d.mkdir(parents=True, exist_ok=True)

    def _get_journal_path(self) -> Path:
        """Get current journal file, rotating at 10MB."""
        if self._journal_file is None or self._journal_file.stat().st_size > 10 * 1024 * 1024:
            self._journal_seq += 1
            self._journal_file = self.journal_dir / f"{self._journal_seq:06d}.jsonl"
            if not self._journal_file.exists():
                self._journal_file.touch()
        return self._journal_file

    def append_event(self, event_type: str, data: dict[str, Any]) -> None:
        """Append an event to the write-ahead journal."""
        event = {
            "timestamp": datetime.utcnow().isoformat(),
            "run_id": self.run_id,
            "event": event_type,
            "data": data,
        }
        path = self._get_journal_path()
        with open(path, "a") as f:
            f.write(json.dumps(event) + "\n")

    def save_graph(self, graph: ExecutionGraph) -> None:
        """Atomically save the execution graph via rename."""
        graph_path = self.state_dir / "graph.json"
        data = json.dumps(graph.to_dict(), indent=2)

        # Atomic write via temp file + rename
        fd, tmp_path = tempfile.mkstemp(
            dir=self.state_dir, suffix=".json.tmp", prefix="graph_"
        )
        try:
            os.write(fd, data.encode())
            os.fsync(fd)
            os.close(fd)
            os.rename(tmp_path, graph_path)
        except Exception:
            os.close(fd)
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

    def load_graph(self) -> ExecutionGraph | None:
        """Load the execution graph from disk."""
        graph_path = self.state_dir / "graph.json"
        if not graph_path.exists():
            return None
        with open(graph_path) as f:
            data = json.load(f)
        return ExecutionGraph.from_dict(data)

    def save_checkpoint(self, graph: ExecutionGraph) -> Path:
        """Save a binary checkpoint for fast resume."""
        checkpoint_path = self.checkpoints_dir / f"{graph.run_id}.snapshot"
        packed = msgpack.packb(graph.to_dict(), use_bin_type=True)
        with open(checkpoint_path, "wb") as f:
            f.write(packed)
            f.flush()
            os.fsync(f.fileno())
        return checkpoint_path

    def load_checkpoint(self) -> ExecutionGraph | None:
        """Load from binary checkpoint."""
        checkpoint_path = self.checkpoints_dir / f"{self.run_id}.snapshot"
        if not checkpoint_path.exists():
            return None
        with open(checkpoint_path, "rb") as f:
            data = msgpack.unpackb(f.read(), raw=False)
        return ExecutionGraph.from_dict(data)

    def write_prompt_file(self, task: TaskNode) -> Path:
        """Write the agent prompt to a file and update task.prompt_file."""
        prompt_path = self.prompts_dir / f"{task.task_id}.md"
        with open(prompt_path, "w") as f:
            f.write(f"# Task: {task.title}\n\n")
            f.write(task.prompt)
            f.write("\n\n## Owned Files\n\n")
            for fp in task.owned_files:
                f.write(f"- `{fp}`\n")
            f.write("\n## Constraints\n\n")
            f.write("You MUST only create or modify files listed in your owned_files manifest. ")
            f.write("Do not touch any other files.\n")
        task.prompt_file = str(prompt_path)
        return prompt_path

    def get_log_paths(self, task_id: str) -> tuple[Path, Path]:
        """Return (stdout_log_path, stderr_log_path) for a task."""
        return (
            self.logs_dir / f"{task_id}.stdout.log",
            self.logs_dir / f"{task_id}.stderr.log",
        )

    def checkpoint_task(self, graph: ExecutionGraph, task: TaskNode, event: str) -> None:
        """Checkpoint a task state transition."""
        self.append_event(event, {
            "task_id": task.task_id,
            "status": task.status.value,
            "retry_count": task.retry_count,
            "exit_code": task.exit_code,
        })
        self.save_graph(graph)

    def read_journal(self) -> list[dict[str, Any]]:
        """Read all journal events in order."""
        events: list[dict[str, Any]] = []
        for jf in sorted(self.journal_dir.glob("*.jsonl")):
            with open(jf) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        events.append(json.loads(line))
        return events
