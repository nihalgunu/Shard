# Shard

A TDD-driven, parallelized AI coding orchestrator that breaks down complex tasks and executes them concurrently using git worktrees.

## Installation

### Homebrew (macOS)

```bash
brew tap nihalgunu/shard
brew install shard
```

### pip

```bash
pip install shard-code
```

### From source

```bash
git clone https://github.com/nihalgunu/Shard.git
cd Shard
pip install -e ".[dev]"
```

## Quick Start

```bash
# Run a full pipeline
shard run -p "Add user authentication with JWT tokens"

# Preview the execution plan without running
shard plan -p "Refactor database layer to async"

# Check status
shard status

# Resume an interrupted run
shard resume <run-id>
```

## How It Works

Shard takes a natural language prompt and:

1. **Plans** - Generates a DAG of sub-tasks with file ownership boundaries
2. **Partitions** - Provisions isolated git worktrees for parallel execution
3. **Dispatches** - Runs AI agents concurrently on each task
4. **Aggregates** - Merges all branches and resolves conflicts
5. **Self-heals** - Runs tests and automatically fixes failures

## Configuration

Create `shard.toml` in your repository:

```toml
[agent]
backend = "claude-code"  # or "aider", "cursor-cli"
max_concurrent = 4

[cost]
max_usd = 10.0

[retries]
max_per_task = 3
max_global = 5
```

## Commands

| Command | Description |
|---------|-------------|
| `shard run` | Execute a full pipeline |
| `shard plan` | Preview execution DAG |
| `shard resume` | Resume interrupted run |
| `shard status` | Show current status |
| `shard logs` | View agent output |
| `shard abort` | Stop all agents |
| `shard clean` | Remove artifacts |
| `shard config` | Show configuration |

## Requirements

- Python 3.11+
- Git 2.20+
- AI coding agent (Claude Code, Aider, or Cursor CLI)

## License

Apache 2.0
