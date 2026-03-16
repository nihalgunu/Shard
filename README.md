# Shard

A TDD-driven, parallelized AI coding orchestrator that breaks down complex tasks and executes them concurrently using git worktrees.

## What is Shard?

Shard takes a natural language prompt describing a coding task and automatically:

1. **Plans** - Uses an LLM to decompose your task into a DAG of parallel sub-tasks, each with exclusive file ownership
2. **Partitions** - Creates isolated git worktrees so agents can work simultaneously without conflicts
3. **Dispatches** - Runs AI coding agents (Claude Code, Aider, or Cursor) in parallel across tasks
4. **Aggregates** - Merges all branches back together, resolving structural conflicts automatically
5. **Self-heals** - Runs your test suite and automatically fixes failures

## Installation

### Prerequisites

**Required:**
- Python 3.11+
- Git 2.20+

**AI Agent (install one):**
```bash
# Claude Code (recommended)
npm install -g @anthropic-ai/claude-code

# Aider
pip install aider-chat

# Cursor CLI
# Install from https://cursor.sh
```

### Install Shard

```bash
# From PyPI
pip install shard-code

# From source
git clone https://github.com/nihalgunu/Shard.git
cd Shard
pip install -e ".[dev]"
```

## Quick Start

```bash
cd your-project

# Run a full pipeline
shard run -p "Add user authentication with JWT tokens and refresh token support"

# Preview the execution plan without running
shard plan -p "Refactor the database layer to use async operations"

# Check status of current run
shard status

# Resume an interrupted run
shard resume <run-id>

# View logs for a specific task
shard logs <task-id>
```

## Commands

| Command | Description |
|---------|-------------|
| `shard run -p "..."` | Execute a full pipeline with the given prompt |
| `shard run -f prompt.txt` | Execute using prompt from file |
| `shard plan -p "..."` | Preview execution DAG without running |
| `shard status` | Show current execution status |
| `shard resume <run-id>` | Resume an interrupted run |
| `shard logs <task-id>` | View agent stdout/stderr |
| `shard abort <run-id>` | Stop all running agents |
| `shard clean <run-id>` | Remove worktrees, branches, artifacts |
| `shard config` | Show current configuration |

### Run Options

```bash
shard run -p "Your prompt" \
  --agents 4 \              # Max concurrent agents (default: 4)
  --backend claude-code \   # Agent backend: claude-code, aider, cursor-cli
  --timeout 3600 \          # Global timeout in seconds
  --max-cost 10.0           # Maximum spend in USD
```

## Configuration

Create `shard.toml` in your repository root:

```toml
[agent]
backend = "claude-code"     # claude-code | aider | cursor-cli | custom
binary = ""                 # Custom agent binary path (for custom backend)
max_concurrent = 4          # Max parallel agents
stagger_delay_s = 2.0       # Delay between agent launches

[timeouts]
per_task_s = 600            # Per-task timeout
global_s = 3600             # Global timeout
output_stall_s = 120        # Kill agent if no output for this long
commit_stall_s = 300        # Kill agent if no commit for this long

[retries]
max_per_task = 3            # Max retries per failed task
max_global = 5              # Max total retries across all tasks

[cost]
max_usd = 5.00              # Hard spending limit
warn_usd = 3.00             # Warning threshold

[git]
worktree_dir = "../worktrees"  # Where to create worktrees
branch_prefix = "wt"           # Branch naming prefix
auto_cleanup = true            # Clean up on success

[test]
runner = "pytest"           # Test command
args = ["-xvs"]             # Test arguments
json_report = true          # Use pytest-json-report for better parsing

[logging]
level = "INFO"              # DEBUG | INFO | WARNING | ERROR
format = "json"             # json | text
```

## Why Git Worktrees?

Traditional approaches to parallel AI coding have a problem: **file conflicts**. If two agents try to edit the same file simultaneously, you get race conditions, merge conflicts, or corrupted state.

Shard solves this with **git worktrees**:

| Approach | Problem |
|----------|---------|
| Single working directory | Agents overwrite each other's changes |
| Multiple clones | Wastes disk space, slow to set up |
| File locking | Serializes work, kills parallelism |
| **Git worktrees** | Lightweight, isolated, native git support |

**How worktrees help:**
- Each agent gets its own complete working directory
- All worktrees share the same `.git` folder (minimal disk overhead)
- Each worktree is on its own branch
- Merging is just `git merge` - git handles the complexity
- Native git tooling works everywhere

This means 4 agents can simultaneously edit 4 different parts of your codebase with zero coordination overhead.

## How It Works

### Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         SHARD PIPELINE                          │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐  │
│  │  Stage 1 │───▶│  Stage 2 │───▶│  Stage 3 │───▶│  Stage 4 │  │
│  │ Planner  │    │Partitioner│   │Dispatcher │   │Aggregator│  │
│  └──────────┘    └──────────┘    └──────────┘    └──────────┘  │
│       │                               │               │         │
│       ▼                               ▼               ▼         │
│   DAG + Tests              Parallel Agents      Merge + Test    │
│                                   │                   │         │
│                                   │          ┌───────┴───────┐  │
│                                   │          │    Stage 5    │  │
│                                   │          │  Self-Healer  │  │
│                                   │          └───────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

### Stage Details

**Stage 1: Planner**
- Analyzes your codebase structure
- Decomposes prompt into independent tasks
- Assigns exclusive file ownership to prevent conflicts
- Generates test scaffolds (TDD approach)

**Stage 2: Partitioner**
- Creates a git worktree for each task
- Each worktree branches from a common scaffold commit
- Enables true parallel execution

**Stage 3: Dispatcher**
- Launches AI agents respecting DAG dependencies
- Monitors output, cost, and timeouts
- Live TUI shows progress across all agents

**Stage 4: Aggregator**
- Merges completed branches in topological order
- Auto-resolves structural conflicts (package.json, requirements.txt, etc.)
- Runs test suite on merged result

**Stage 5: Self-Healer**
- Maps test failures back to responsible tasks
- Re-dispatches agents with failure context
- Bounded retry loop prevents infinite loops

## State & Artifacts

Shard stores state in `.shard/` in your repo:

```
.shard/
├── graph.json          # Current execution DAG
├── journal/            # Write-ahead event log
├── prompts/            # Per-task prompt files
├── logs/               # Agent stdout/stderr
└── checkpoints/        # Binary snapshots for fast resume
```

Worktrees are created in `../worktrees/<run-id>/` by default.

## Examples

### Add a Feature
```bash
shard run -p "Add a REST API endpoint for user profiles with GET, POST, PUT, DELETE operations. Include input validation and proper error handling."
```

### Refactor Code
```bash
shard run -p "Refactor the authentication module to use dependency injection. Update all tests accordingly."
```

### Fix a Bug
```bash
shard run -p "Fix the race condition in the connection pool that causes intermittent timeouts under high load."
```

### Preview Only
```bash
shard plan -p "Implement caching layer for database queries"
# Shows DAG without executing - review before committing resources
```

## License

Apache 2.0
