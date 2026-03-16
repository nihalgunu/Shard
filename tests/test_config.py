"""Tests for configuration loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from worktree.config import (
    is_barrel_file,
    is_structural_file,
    load_config,
)
from worktree.models import AgentBackend


class TestLoadConfig:
    def test_defaults_when_no_file(self, tmp_path: Path) -> None:
        config = load_config(tmp_path)
        assert config.max_agents == 4
        assert config.agent_backend == AgentBackend.CLAUDE_CODE

    def test_load_from_toml(self, tmp_path: Path) -> None:
        toml_content = """
[agent]
backend = "aider"
binary = "/usr/bin/aider"
max_concurrent = 8
stagger_delay_s = 1.0

[timeouts]
per_task_s = 300
global_s = 1800

[retries]
max_per_task = 5
max_global = 10

[cost]
max_usd = 10.00
warn_usd = 5.00

[git]
worktree_dir = "/tmp/worktrees"
branch_prefix = "custom"

[planner]
model = "gpt-4"
temperature = 0.1

[test]
runner = "pytest"
args = ["-v", "--tb=long"]

[logging]
level = "DEBUG"
format = "text"
"""
        (tmp_path / "worktree.toml").write_text(toml_content)
        config = load_config(tmp_path)
        assert config.agent_backend == AgentBackend.AIDER
        assert config.agent_binary == "/usr/bin/aider"
        assert config.max_agents == 8
        assert config.per_task_timeout_s == 300
        assert config.max_retries_per_task == 5
        assert config.max_cost_usd == 10.00
        assert config.branch_prefix == "custom"
        assert config.planner_model == "gpt-4"
        assert config.log_level == "DEBUG"


class TestFileClassification:
    def test_structural_files(self) -> None:
        assert is_structural_file("requirements.txt")
        assert is_structural_file("package.json")
        assert is_structural_file("path/to/pyproject.toml")
        assert not is_structural_file("src/main.py")

    def test_barrel_files(self) -> None:
        assert is_barrel_file("__init__.py")
        assert is_barrel_file("src/__init__.py")
        assert is_barrel_file("index.ts")
        assert not is_barrel_file("main.py")
