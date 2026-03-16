"""Stage 1: The Architect - Planning engine and DAG generation."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import jsonschema
import networkx as nx

from shard.models import Collision, ExecutionGraph, RunConfig, TaskNode, TaskStatus

logger = logging.getLogger(__name__)

# JSON Schema for validating planner LLM output
TASK_NODE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["task_id", "title", "prompt", "owned_files"],
    "properties": {
        "task_id": {"type": "string"},
        "title": {"type": "string"},
        "prompt": {"type": "string"},
        "owned_files": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "depends_on": {"type": "array", "items": {"type": "string"}},
        "test_files": {"type": "array", "items": {"type": "string"}},
        "priority": {"type": "integer"},
    },
}

EXECUTION_GRAPH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["nodes"],
    "properties": {
        "nodes": {
            "type": "array",
            "items": TASK_NODE_SCHEMA,
            "minItems": 1,
        },
        "test_code": {
            "type": "object",
            "additionalProperties": {"type": "string"},
        },
    },
}

PLANNER_SYSTEM_PROMPT = """\
You are a software architect. Given a user's natural language prompt and a repository file tree, \
decompose the task into a Directed Acyclic Graph (DAG) of sub-tasks.

CRITICAL CONSTRAINTS:
1. Each task MUST declare an 'owned_files' array listing every file it will create or modify.
2. No two sibling tasks (tasks that can run in parallel) may share any entry in 'owned_files'.
3. If two tasks must modify the same file, they MUST be connected by a dependency edge (one depends_on the other).
4. Only reference files from the provided file tree or declare new files under existing source directories.
5. Each task must have a clear, self-contained prompt that an AI coding agent can execute independently.
6. Generate test files for each task. Test file names should follow the pattern tests/test_<module>.py.

Output valid JSON matching this schema:
{schema}

Also include a "test_code" object mapping test file paths to their content. These tests should verify \
the acceptance criteria from the user's prompt. Tests should be written BEFORE implementation \
(TDD approach) - they define the expected behavior.

Example:
{{
  "nodes": [
    {{
      "task_id": "task-001",
      "title": "Implement feature X",
      "prompt": "Create module X in src/x.py that...",
      "owned_files": ["src/x.py"],
      "depends_on": [],
      "test_files": ["tests/test_x.py"],
      "priority": 1
    }}
  ],
  "test_code": {{
    "tests/test_x.py": "import pytest\\n..."
  }}
}}
"""


def build_file_tree(repo_root: Path, max_depth: int = 6) -> str:
    """Generate a bounded file tree string, respecting .gitignore patterns."""
    lines: list[str] = []
    gitignore_patterns = _load_gitignore(repo_root)

    def _walk(current: Path, depth: int, prefix: str) -> None:
        if depth > max_depth:
            return
        try:
            entries = sorted(current.iterdir(), key=lambda p: (not p.is_dir(), p.name))
        except PermissionError:
            return

        for i, entry in enumerate(entries):
            rel = str(entry.relative_to(repo_root))
            if _should_ignore(rel, entry.name, gitignore_patterns):
                continue

            is_last = i == len(entries) - 1
            connector = "└── " if is_last else "├── "
            lines.append(f"{prefix}{connector}{entry.name}")

            if entry.is_dir():
                extension = "    " if is_last else "│   "
                _walk(entry, depth + 1, prefix + extension)

    lines.append(repo_root.name + "/")
    _walk(repo_root, 1, "")
    return "\n".join(lines)


def _load_gitignore(repo_root: Path) -> list[str]:
    """Load .gitignore patterns."""
    gitignore = repo_root / ".gitignore"
    if not gitignore.exists():
        return []
    patterns = []
    with open(gitignore) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                patterns.append(line)
    return patterns


def _should_ignore(rel_path: str, name: str, patterns: list[str]) -> bool:
    """Basic gitignore-style check."""
    # Always ignore .git and .shard directories
    if name in (".git", ".shard", "__pycache__", "node_modules", ".venv", "venv"):
        return True
    for pattern in patterns:
        clean = pattern.rstrip("/")
        if name == clean or rel_path.startswith(clean):
            return True
    return False


def detect_language(repo_root: Path) -> str:
    """Auto-detect the repository's primary language."""
    extension_counts: dict[str, int] = {}
    lang_map = {
        ".py": "Python",
        ".js": "JavaScript",
        ".ts": "TypeScript",
        ".tsx": "TypeScript",
        ".go": "Go",
        ".rs": "Rust",
        ".java": "Java",
        ".rb": "Ruby",
        ".cpp": "C++",
        ".c": "C",
        ".cs": "C#",
    }
    for f in repo_root.rglob("*"):
        if f.is_file() and f.suffix in lang_map:
            lang = lang_map[f.suffix]
            extension_counts[lang] = extension_counts.get(lang, 0) + 1
    if not extension_counts:
        return "Unknown"
    return max(extension_counts, key=extension_counts.get)  # type: ignore[arg-type]


def validate_dag_schema(data: dict[str, Any]) -> list[str]:
    """Validate DAG JSON against the schema. Returns list of errors."""
    validator = jsonschema.Draft7Validator(EXECUTION_GRAPH_SCHEMA)
    return [e.message for e in validator.iter_errors(data)]


def validate_acyclicity(nodes: list[TaskNode]) -> bool:
    """Check that the task graph is a valid DAG (no cycles)."""
    g = nx.DiGraph()
    for node in nodes:
        g.add_node(node.task_id)
        for dep in node.depends_on:
            g.add_edge(dep, node.task_id)
    return nx.is_directed_acyclic_graph(g)


def find_cycle(nodes: list[TaskNode]) -> list[str] | None:
    """Find and return a cycle if one exists."""
    g = nx.DiGraph()
    for node in nodes:
        g.add_node(node.task_id)
        for dep in node.depends_on:
            g.add_edge(dep, node.task_id)
    try:
        cycle = nx.find_cycle(g)
        return [edge[0] for edge in cycle]
    except nx.NetworkXNoCycle:
        return None


def detect_collisions(graph: ExecutionGraph) -> list[Collision]:
    """Detect file ownership collisions between concurrent (sibling) tasks.

    Two tasks collide if they share owned_files AND neither depends on the other.
    """
    g = nx.DiGraph()
    for node in graph.nodes:
        g.add_node(node.task_id)
        for dep in node.depends_on:
            g.add_edge(dep, node.task_id)

    collisions: list[Collision] = []
    nodes = graph.nodes
    for i, a in enumerate(nodes):
        for b in nodes[i + 1 :]:
            # Check if there's a dependency path between them
            has_path_ab = nx.has_path(g, a.task_id, b.task_id) if g.has_node(a.task_id) and g.has_node(b.task_id) else False
            has_path_ba = nx.has_path(g, b.task_id, a.task_id) if g.has_node(a.task_id) and g.has_node(b.task_id) else False
            if has_path_ab or has_path_ba:
                continue
            overlap = set(a.owned_files) & set(b.owned_files)
            if overlap:
                collisions.append(Collision(a.task_id, b.task_id, overlap))
    return collisions


def resolve_collisions_by_serialization(graph: ExecutionGraph, collisions: list[Collision]) -> None:
    """Resolve collisions by injecting dependency edges (auto-serialize)."""
    for collision in collisions:
        node_a = graph.get_node(collision.task_a)
        node_b = graph.get_node(collision.task_b)
        if node_a is None or node_b is None:
            continue
        # Lower priority task depends on higher priority
        if node_a.priority <= node_b.priority:
            if collision.task_a not in node_b.depends_on:
                node_b.depends_on.append(collision.task_a)
                logger.info(
                    "Auto-serialized: %s now depends on %s (shared files: %s)",
                    collision.task_b, collision.task_a, collision.overlapping_files,
                )
        else:
            if collision.task_b not in node_a.depends_on:
                node_a.depends_on.append(collision.task_b)
                logger.info(
                    "Auto-serialized: %s now depends on %s (shared files: %s)",
                    collision.task_a, collision.task_b, collision.overlapping_files,
                )


def validate_file_paths(nodes: list[TaskNode], repo_root: Path) -> list[str]:
    """Validate that all owned_files are real or under existing directories."""
    errors: list[str] = []
    for node in nodes:
        for fp in node.owned_files:
            full_path = repo_root / fp
            if full_path.exists():
                continue
            parent = full_path.parent
            if not parent.exists():
                errors.append(
                    f"Task {node.task_id}: path '{fp}' references non-existent "
                    f"directory '{parent.relative_to(repo_root)}'"
                )
    return errors


def topological_sort(graph: ExecutionGraph) -> list[str]:
    """Return task IDs in topological order."""
    g = nx.DiGraph()
    for node in graph.nodes:
        g.add_node(node.task_id)
        for dep in node.depends_on:
            g.add_edge(dep, node.task_id)
    return list(nx.topological_sort(g))


def compute_critical_path(graph: ExecutionGraph) -> list[str]:
    """Compute the critical path through the DAG (longest path)."""
    g = nx.DiGraph()
    for node in graph.nodes:
        g.add_node(node.task_id)
        for dep in node.depends_on:
            g.add_edge(dep, node.task_id)
    return list(nx.dag_longest_path(g))


def parse_planner_response(response_json: dict[str, Any]) -> tuple[list[TaskNode], dict[str, str]]:
    """Parse the planner LLM's JSON response into TaskNodes and test code."""
    errors = validate_dag_schema(response_json)
    if errors:
        raise ValueError(f"Invalid planner response: {'; '.join(errors)}")

    nodes = [TaskNode.from_dict(n) for n in response_json["nodes"]]
    test_code = response_json.get("test_code", {})

    if not validate_acyclicity(nodes):
        cycle = find_cycle(nodes)
        raise ValueError(f"DAG contains a cycle: {cycle}")

    return nodes, test_code


def _fix_json_newlines(json_str: str) -> str:
    """Attempt to fix JSON with unescaped newlines in strings."""
    result = []
    in_string = False
    escape_next = False

    for ch in json_str:
        if escape_next:
            result.append(ch)
            escape_next = False
        elif ch == '\\':
            result.append(ch)
            escape_next = True
        elif ch == '"':
            result.append(ch)
            in_string = not in_string
        elif ch == '\n' and in_string:
            result.append('\\n')
        else:
            result.append(ch)

    return ''.join(result)


async def _call_cli(binary: str, system_prompt: str, user_message: str) -> str:
    """Use the agent CLI for planning."""
    import asyncio
    import shutil

    if not binary or binary == "claude":
        binary = "claude"

    if not shutil.which(binary):
        raise RuntimeError(
            f"CLI '{binary}' not found.\n\n"
            "Install one of:\n"
            "  Claude Code: npm install -g @anthropic-ai/claude-code\n"
            "  Aider: pip install aider-chat"
        )

    full_prompt = f"{system_prompt}\n\n{user_message}"
    logger.info("Using CLI for planning: %s", binary)

    if "claude" in binary:
        cmd = [
            binary,
            "--print",
            "--permission-mode", "bypassPermissions",
            "--output-format", "text",
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate(input=full_prompt.encode())
    elif "aider" in binary:
        cmd = [binary, "--yes-always", "--no-auto-commits", "--message", full_prompt]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
    else:
        cmd = [binary, "--print", full_prompt]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        error_msg = stderr.decode() if stderr else "Unknown error"
        raise RuntimeError(f"CLI planning failed (exit {proc.returncode}): {error_msg}")

    return stdout.decode()


async def invoke_planner(
    prompt: str,
    repo_root: Path,
    config: RunConfig,
) -> tuple[list[TaskNode], dict[str, str]]:
    """Invoke the planner via CLI to generate the execution DAG.

    Args:
        prompt: User's natural language prompt.
        repo_root: Path to the repository root.
        config: Run configuration.

    Returns:
        Tuple of (task nodes, test code dict).
    """
    file_tree = build_file_tree(repo_root)
    language = detect_language(repo_root)

    system_prompt = PLANNER_SYSTEM_PROMPT.format(
        schema=json.dumps(EXECUTION_GRAPH_SCHEMA, indent=2)
    )

    user_message = (
        f"## Repository Language\n{language}\n\n"
        f"## File Tree\n```\n{file_tree}\n```\n\n"
        f"## User Prompt\n{prompt}"
    )

    for attempt in range(config.max_replan_attempts + 1):
        response_text = await _call_cli(config.agent_binary, system_prompt, user_message)

        # Extract JSON from response (may be wrapped in markdown code blocks)
        json_str = _extract_json(response_text)

        try:
            response_json = json.loads(json_str)
        except json.JSONDecodeError as e:
            # Try to fix common JSON issues
            try:
                fixed_json = _fix_json_newlines(json_str)
                response_json = json.loads(fixed_json)
            except json.JSONDecodeError:
                if attempt < config.max_replan_attempts:
                    user_message += f"\n\n## JSON Parse Error\n{e}\nPlease output valid JSON only."
                    logger.warning("Planner attempt %d failed with JSON error: %s", attempt + 1, e)
                    continue
                raise ValueError(f"Invalid JSON from planner: {e}")

        try:
            nodes, test_code = parse_planner_response(response_json)
        except ValueError as e:
            if attempt < config.max_replan_attempts:
                user_message += f"\n\n## Error in previous response\n{e}\nPlease fix and try again."
                logger.warning("Planner attempt %d failed: %s", attempt + 1, e)
                continue
            raise

        # Validate file paths
        path_errors = validate_file_paths(nodes, repo_root)
        if path_errors and attempt < config.max_replan_attempts:
            error_msg = "\n".join(path_errors)
            user_message += (
                f"\n\n## Invalid file paths\n{error_msg}\n"
                "Revise the task decomposition using only valid paths from the file tree provided."
            )
            logger.warning("Path validation failed on attempt %d: %s", attempt + 1, error_msg)
            continue

        return nodes, test_code

    raise RuntimeError("Planner failed after all attempts")


def _extract_json(text: str) -> str:
    """Extract JSON from text that may be wrapped in markdown code blocks."""
    # Try to find JSON in code blocks
    if "```json" in text:
        start = text.index("```json") + 7
        end = text.index("```", start)
        return text[start:end].strip()
    if "```" in text:
        start = text.index("```") + 3
        end = text.index("```", start)
        return text[start:end].strip()
    # Try raw JSON
    for i, ch in enumerate(text):
        if ch == "{":
            # Find matching brace
            depth = 0
            for j in range(i, len(text)):
                if text[j] == "{":
                    depth += 1
                elif text[j] == "}":
                    depth -= 1
                    if depth == 0:
                        return text[i : j + 1]
    return text
