"""Configuration loading from worktree.toml."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from worktree.models import RunConfig

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

try:
    import tomli_w
except ImportError:
    tomli_w = None  # type: ignore[assignment]


DEFAULT_CONFIG_NAME = "worktree.toml"

# Structural files that get special merge handling
STRUCTURAL_FILES = {
    "requirements.txt",
    "setup.py",
    "setup.cfg",
    "pyproject.toml",
    "package.json",
    "package-lock.json",
    "Cargo.toml",
    "go.mod",
    "go.sum",
    "Gemfile",
    "Gemfile.lock",
    "pom.xml",
    "build.gradle",
}

# Files that are typically auto-generated barrel exports
BARREL_PATTERNS = {"__init__.py", "index.ts", "index.js", "mod.rs"}


def load_config(repo_root: Path) -> RunConfig:
    """Load RunConfig from worktree.toml in the repo root."""
    config_path = repo_root / DEFAULT_CONFIG_NAME
    if not config_path.exists():
        return RunConfig()

    with open(config_path, "rb") as f:
        raw = tomllib.load(f)

    flat: dict[str, Any] = {}
    # Flatten TOML sections into RunConfig fields
    section_map = {
        "agent": [
            ("backend", "agent_backend"),
            ("binary", "agent_binary"),
            ("max_concurrent", "max_agents"),
            ("stagger_delay_s", "stagger_delay_s"),
        ],
        "timeouts": [
            ("per_task_s", "per_task_timeout_s"),
            ("global_s", "global_timeout_s"),
            ("output_stall_s", "output_stall_s"),
            ("commit_stall_s", "commit_stall_s"),
        ],
        "retries": [
            ("max_per_task", "max_retries_per_task"),
            ("max_global", "max_retries_global"),
        ],
        "cost": [
            ("max_usd", "max_cost_usd"),
            ("warn_usd", "warn_cost_usd"),
        ],
        "git": [
            ("worktree_dir", "worktree_dir"),
            ("branch_prefix", "branch_prefix"),
            ("auto_cleanup", "auto_cleanup"),
        ],
        "planner": [
            ("model", "planner_model"),
            ("temperature", "planner_temperature"),
            ("max_replan_attempts", "max_replan_attempts"),
        ],
        "test": [
            ("runner", "test_runner"),
            ("args", "test_args"),
            ("json_report", "test_json_report"),
        ],
        "logging": [
            ("level", "log_level"),
            ("format", "log_format"),
            ("archive_agent_logs", "archive_agent_logs"),
        ],
    }

    for section, mappings in section_map.items():
        section_data = raw.get(section, {})
        for toml_key, config_key in mappings:
            if toml_key in section_data:
                flat[config_key] = section_data[toml_key]

    return RunConfig.from_dict(flat)


def save_config(repo_root: Path, config: RunConfig) -> None:
    """Save RunConfig to worktree.toml."""
    if tomli_w is None:
        raise RuntimeError("tomli_w is required to save configuration. Install it with: pip install tomli-w")

    toml_data = {
        "agent": {
            "backend": config.agent_backend.value,
            "binary": config.agent_binary,
            "max_concurrent": config.max_agents,
            "stagger_delay_s": config.stagger_delay_s,
        },
        "timeouts": {
            "per_task_s": config.per_task_timeout_s,
            "global_s": config.global_timeout_s,
            "output_stall_s": config.output_stall_s,
            "commit_stall_s": config.commit_stall_s,
        },
        "retries": {
            "max_per_task": config.max_retries_per_task,
            "max_global": config.max_retries_global,
        },
        "cost": {
            "max_usd": config.max_cost_usd,
            "warn_usd": config.warn_cost_usd,
        },
        "git": {
            "worktree_dir": config.worktree_dir,
            "branch_prefix": config.branch_prefix,
            "auto_cleanup": config.auto_cleanup,
        },
        "planner": {
            "model": config.planner_model,
            "temperature": config.planner_temperature,
            "max_replan_attempts": config.max_replan_attempts,
        },
        "test": {
            "runner": config.test_runner,
            "args": config.test_args,
            "json_report": config.test_json_report,
        },
        "logging": {
            "level": config.log_level,
            "format": config.log_format,
            "archive_agent_logs": config.archive_agent_logs,
        },
    }

    config_path = repo_root / DEFAULT_CONFIG_NAME
    with open(config_path, "wb") as f:
        tomli_w.dump(toml_data, f)


def is_structural_file(path: str) -> bool:
    """Check if a file path is a structural/manifest file."""
    name = Path(path).name
    return name in STRUCTURAL_FILES


def is_barrel_file(path: str) -> bool:
    """Check if a file path is a barrel/index export file."""
    name = Path(path).name
    return name in BARREL_PATTERNS
