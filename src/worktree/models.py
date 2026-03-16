"""Data models for WorkTree execution graph and task nodes."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


class RunStatus(str, enum.Enum):
    PLANNING = "PLANNING"
    PARTITIONING = "PARTITIONING"
    DISPATCHING = "DISPATCHING"
    MERGING = "MERGING"
    TESTING = "TESTING"
    HEALING = "HEALING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"
    ABORTED = "ABORTED"


class TaskStatus(str, enum.Enum):
    PENDING = "PENDING"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    HEALING = "HEALING"
    INTERRUPTED = "INTERRUPTED"


class AgentBackend(str, enum.Enum):
    CLAUDE_CODE = "claude-code"
    AIDER = "aider"
    CURSOR_CLI = "cursor-cli"
    CUSTOM = "custom"


@dataclass
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def to_dict(self) -> dict[str, int]:
        return {"prompt_tokens": self.prompt_tokens, "completion_tokens": self.completion_tokens}

    @classmethod
    def from_dict(cls, data: dict[str, int]) -> TokenUsage:
        return cls(
            prompt_tokens=data.get("prompt_tokens", 0),
            completion_tokens=data.get("completion_tokens", 0),
        )


@dataclass
class TaskNode:
    task_id: str
    title: str
    prompt: str
    owned_files: list[str]
    depends_on: list[str] = field(default_factory=list)
    test_files: list[str] = field(default_factory=list)
    status: TaskStatus = TaskStatus.PENDING
    priority: int = 0
    prompt_file: str | None = None
    worktree_path: str | None = None
    branch_name: str | None = None
    agent_pid: int | None = None
    exit_code: int | None = None
    stdout_log: str | None = None
    stderr_log: str | None = None
    retry_count: int = 0
    started_at: str | None = None
    completed_at: str | None = None
    duration_s: float | None = None
    token_usage: TokenUsage | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "task_id": self.task_id,
            "title": self.title,
            "prompt": self.prompt,
            "owned_files": self.owned_files,
            "depends_on": self.depends_on,
            "test_files": self.test_files,
            "status": self.status.value,
            "priority": self.priority,
            "prompt_file": self.prompt_file,
            "worktree_path": self.worktree_path,
            "branch_name": self.branch_name,
            "agent_pid": self.agent_pid,
            "exit_code": self.exit_code,
            "stdout_log": self.stdout_log,
            "stderr_log": self.stderr_log,
            "retry_count": self.retry_count,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_s": self.duration_s,
            "token_usage": self.token_usage.to_dict() if self.token_usage else None,
        }
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskNode:
        token_usage = None
        if data.get("token_usage"):
            token_usage = TokenUsage.from_dict(data["token_usage"])
        return cls(
            task_id=data["task_id"],
            title=data["title"],
            prompt=data["prompt"],
            owned_files=data["owned_files"],
            depends_on=data.get("depends_on", []),
            test_files=data.get("test_files", []),
            status=TaskStatus(data.get("status", "PENDING")),
            priority=data.get("priority", 0),
            prompt_file=data.get("prompt_file"),
            worktree_path=data.get("worktree_path"),
            branch_name=data.get("branch_name"),
            agent_pid=data.get("agent_pid"),
            exit_code=data.get("exit_code"),
            stdout_log=data.get("stdout_log"),
            stderr_log=data.get("stderr_log"),
            retry_count=data.get("retry_count", 0),
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
            duration_s=data.get("duration_s"),
            token_usage=token_usage,
        )


@dataclass
class RunConfig:
    max_agents: int = 4
    agent_backend: AgentBackend = AgentBackend.CLAUDE_CODE
    agent_binary: str = "claude"
    per_task_timeout_s: int = 600
    global_timeout_s: int = 3600
    max_retries_per_task: int = 3
    max_retries_global: int = 5
    stagger_delay_s: float = 2.0
    max_cost_usd: float = 5.00
    warn_cost_usd: float = 2.50
    output_stall_s: int = 120
    commit_stall_s: int = 300
    worktree_dir: str = "../worktrees"
    branch_prefix: str = "wt"
    auto_cleanup: bool = True
    planner_model: str = "claude-sonnet-4-20250514"
    planner_temperature: float = 0.2
    max_replan_attempts: int = 2
    test_runner: str = "pytest"
    test_args: list[str] = field(default_factory=lambda: ["--tb=short", "-q"])
    test_json_report: bool = True
    log_level: str = "INFO"
    log_format: str = "json"
    archive_agent_logs: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_agents": self.max_agents,
            "agent_backend": self.agent_backend.value,
            "agent_binary": self.agent_binary,
            "per_task_timeout_s": self.per_task_timeout_s,
            "global_timeout_s": self.global_timeout_s,
            "max_retries_per_task": self.max_retries_per_task,
            "max_retries_global": self.max_retries_global,
            "stagger_delay_s": self.stagger_delay_s,
            "max_cost_usd": self.max_cost_usd,
            "warn_cost_usd": self.warn_cost_usd,
            "output_stall_s": self.output_stall_s,
            "commit_stall_s": self.commit_stall_s,
            "worktree_dir": self.worktree_dir,
            "branch_prefix": self.branch_prefix,
            "auto_cleanup": self.auto_cleanup,
            "planner_model": self.planner_model,
            "planner_temperature": self.planner_temperature,
            "max_replan_attempts": self.max_replan_attempts,
            "test_runner": self.test_runner,
            "test_args": self.test_args,
            "test_json_report": self.test_json_report,
            "log_level": self.log_level,
            "log_format": self.log_format,
            "archive_agent_logs": self.archive_agent_logs,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunConfig:
        backend = data.get("agent_backend", "claude-code")
        return cls(
            max_agents=data.get("max_agents", 4),
            agent_backend=AgentBackend(backend),
            agent_binary=data.get("agent_binary", "claude"),
            per_task_timeout_s=data.get("per_task_timeout_s", 600),
            global_timeout_s=data.get("global_timeout_s", 3600),
            max_retries_per_task=data.get("max_retries_per_task", 3),
            max_retries_global=data.get("max_retries_global", 5),
            stagger_delay_s=data.get("stagger_delay_s", 2.0),
            max_cost_usd=data.get("max_cost_usd", 5.00),
            warn_cost_usd=data.get("warn_cost_usd", 2.50),
            output_stall_s=data.get("output_stall_s", 120),
            commit_stall_s=data.get("commit_stall_s", 300),
            worktree_dir=data.get("worktree_dir", "../worktrees"),
            branch_prefix=data.get("branch_prefix", "wt"),
            auto_cleanup=data.get("auto_cleanup", True),
            planner_model=data.get("planner_model", "claude-sonnet-4-20250514"),
            planner_temperature=data.get("planner_temperature", 0.2),
            max_replan_attempts=data.get("max_replan_attempts", 2),
            test_runner=data.get("test_runner", "pytest"),
            test_args=data.get("test_args", ["--tb=short", "-q"]),
            test_json_report=data.get("test_json_report", True),
            log_level=data.get("log_level", "INFO"),
            log_format=data.get("log_format", "json"),
            archive_agent_logs=data.get("archive_agent_logs", True),
        )


@dataclass
class ExecutionGraph:
    run_id: str
    root_commit: str
    target_branch: str
    nodes: list[TaskNode] = field(default_factory=list)
    status: RunStatus = RunStatus.PLANNING
    config: RunConfig = field(default_factory=RunConfig)
    scaffold_branch: str = ""
    staging_branch: str = ""
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    global_retry_count: int = 0
    token_budget_estimate: int = 0
    token_budget_actual: int = 0

    def __post_init__(self) -> None:
        prefix = self.config.branch_prefix
        if not self.scaffold_branch:
            self.scaffold_branch = f"{prefix}/{self.run_id}/tests-scaffold"
        if not self.staging_branch:
            self.staging_branch = f"{prefix}/{self.run_id}/staging"

    def get_node(self, task_id: str) -> TaskNode | None:
        for node in self.nodes:
            if node.task_id == task_id:
                return node
        return None

    def get_runnable_nodes(self) -> list[TaskNode]:
        """Return nodes whose dependencies are all COMPLETED and that are PENDING/QUEUED."""
        completed = {n.task_id for n in self.nodes if n.status == TaskStatus.COMPLETED}
        runnable = []
        for node in self.nodes:
            if node.status not in (TaskStatus.PENDING, TaskStatus.QUEUED):
                continue
            if all(dep in completed for dep in node.depends_on):
                runnable.append(node)
        return runnable

    def to_dict(self) -> dict[str, Any]:
        return {
            "$schema": "ExecutionGraph/v1",
            "run_id": self.run_id,
            "created_at": self.created_at,
            "root_commit": self.root_commit,
            "target_branch": self.target_branch,
            "scaffold_branch": self.scaffold_branch,
            "staging_branch": self.staging_branch,
            "status": self.status.value,
            "config": self.config.to_dict(),
            "nodes": [n.to_dict() for n in self.nodes],
            "global_retry_count": self.global_retry_count,
            "token_budget_estimate": self.token_budget_estimate,
            "token_budget_actual": self.token_budget_actual,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExecutionGraph:
        config = RunConfig.from_dict(data.get("config", {}))
        nodes = [TaskNode.from_dict(n) for n in data.get("nodes", [])]
        graph = cls(
            run_id=data["run_id"],
            root_commit=data["root_commit"],
            target_branch=data["target_branch"],
            nodes=nodes,
            status=RunStatus(data.get("status", "PLANNING")),
            config=config,
            scaffold_branch=data.get("scaffold_branch", ""),
            staging_branch=data.get("staging_branch", ""),
            created_at=data.get("created_at", ""),
            global_retry_count=data.get("global_retry_count", 0),
            token_budget_estimate=data.get("token_budget_estimate", 0),
            token_budget_actual=data.get("token_budget_actual", 0),
        )
        return graph


@dataclass
class Collision:
    task_a: str
    task_b: str
    overlapping_files: set[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_a": self.task_a,
            "task_b": self.task_b,
            "overlapping_files": sorted(self.overlapping_files),
        }


@dataclass
class TestResult:
    test_name: str
    test_file: str
    passed: bool
    message: str = ""
    duration_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "test_name": self.test_name,
            "test_file": self.test_file,
            "passed": self.passed,
            "message": self.message,
            "duration_s": self.duration_s,
        }


@dataclass
class MergeConflict:
    branch: str
    task_id: str
    conflicting_files: list[str]
    conflict_type: str = "unknown"  # structural | application | unknown
