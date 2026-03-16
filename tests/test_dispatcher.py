"""Tests for the async dispatcher (Stage 3)."""

from __future__ import annotations

import pytest

from worktree.dispatcher import (
    AiderProvider,
    ClaudeCodeProvider,
    CustomProvider,
    get_provider,
)
from worktree.models import TaskNode, TaskStatus


class TestClaudeCodeProvider:
    def test_get_command(self) -> None:
        provider = ClaudeCodeProvider(binary="claude")
        task = TaskNode(
            task_id="t1", title="T", prompt="Do something",
            owned_files=["a.py"],
        )
        cmd = provider.get_command(task)
        assert cmd[0] == "claude"
        assert "--print" in cmd
        assert "Do something" in cmd


class TestAiderProvider:
    def test_get_command(self) -> None:
        provider = AiderProvider(binary="aider")
        task = TaskNode(
            task_id="t1", title="T", prompt="Do something",
            owned_files=["a.py", "b.py"],
        )
        cmd = provider.get_command(task)
        assert cmd[0] == "aider"
        assert "--message" in cmd
        assert "Do something" in cmd
        assert "--file" in cmd
        assert "a.py" in cmd
        assert "b.py" in cmd


class TestCustomProvider:
    def test_get_command(self) -> None:
        provider = CustomProvider(binary="/usr/bin/my-agent")
        task = TaskNode(
            task_id="t1", title="T", prompt="Do something",
            owned_files=["a.py"],
            prompt_file="/tmp/prompt.md",
            worktree_path="/tmp/wt",
        )
        cmd = provider.get_command(task)
        assert cmd[0] == "/usr/bin/my-agent"
        assert "--prompt-file" in cmd
        assert "/tmp/prompt.md" in cmd


class TestGetProvider:
    def test_claude_code(self) -> None:
        p = get_provider("claude-code", "claude")
        assert isinstance(p, ClaudeCodeProvider)

    def test_aider(self) -> None:
        p = get_provider("aider", "aider")
        assert isinstance(p, AiderProvider)

    def test_custom(self) -> None:
        p = get_provider("custom", "my-agent")
        assert isinstance(p, CustomProvider)

    def test_unknown_falls_back_to_custom(self) -> None:
        p = get_provider("unknown-backend", "something")
        assert isinstance(p, CustomProvider)
