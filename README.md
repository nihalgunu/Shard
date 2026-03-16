# Shard

A TDD-driven, parallelized AI coding orchestrator that breaks down complex tasks and executes them concurrently using git worktrees.

## Overview

Shard takes a natural language prompt describing a coding task and:

1. **Plans** - Generates a DAG (Directed Acyclic Graph) of sub-tasks with file ownership boundaries
2. **Partitions** - Provisions isolated git worktrees for parallel execution
3. **Dispatches** - Runs AI coding agents (Claude Code, Aider, Cursor CLI) concurrently on each task
4. **Aggregates** - Merges all branches and resolves conflicts
5. **Self-heals** - Runs tests and automatically fixes failures

## Installation

```bash
pip install worktree
```

Or install from source:

```bash
git clone https://github.com/nihalgunu/Shard.git
cd Shard
pip install -e ".[dev]"
```

## Quick Start

```bash
# Run a full pipeline
worktree run -p "Add user authentication with JWT tokens and refresh token support"

# Plan only (preview the DAG without executing)
worktree plan -p "Refactor the database layer to use async operations"

# Check status of a run
worktree status

# Resume an interrupted run
worktree resume <run-id>

# View logs for a specific task
worktree logs <task-id>
```

## Configuration

Create a `worktree.toml` in your repository root:

```toml
[worktree]
max_agents = 4
agent_backend = "claude-code"  # or "aider", "cursor-cli", "custom"
global_timeout_s = 3600
max_cost_usd = 10.0
max_retries_per_task = 2
max_retries_global = 3
auto_cleanup = true
```

## CLI Commands

| Command | Description |
|---------|-------------|
| `worktree run` | Execute a full pipeline run |
| `worktree plan` | Generate DAG and test scaffold without executing |
| `worktree resume` | Resume an interrupted run |
| `worktree status` | Show execution graph status |
| `worktree logs` | View agent stdout/stderr |
| `worktree abort` | Terminate all agents and mark run as aborted |
| `worktree clean` | Remove worktrees, branches, and artifacts |
| `worktree config` | Show current configuration |

## How It Works

### Stage 1: Planning (Architect)
An LLM analyzes your prompt and codebase to generate:
- A DAG of independent tasks with dependencies
- File ownership assignments (each file owned by exactly one task)
- Test scaffolds for TDD-style development

### Stage 2: Partitioning
For each task node, Shard provisions an isolated git worktree branched from a common scaffold commit.

### Stage 3: Dispatching
Tasks are dispatched to AI coding agents respecting:
- DAG dependencies (topological order)
- Concurrency limits
- Per-task timeouts
- Cost budgets

### Stage 4: Aggregation
Completed task branches are merged in topological order into a staging branch. File ownership boundaries prevent merge conflicts.

### Stage 5: Self-Healing
Tests run on the merged result. If failures occur, Shard enters a healing loop that:
- Identifies failing tests
- Dispatches fix attempts
- Retries up to the configured limit

## Requirements

- Python 3.11+
- Git 2.20+
- An AI coding agent (Claude Code, Aider, or Cursor CLI)

## License

Apache 2.0
