"""Stage 4: The Aggregator - Merging and test execution.
Stage 5: The Self-Healing Loop - Error resolution routing."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from shard.config import is_barrel_file, is_structural_file
from shard.git_manager import GitManager
from shard.models import (
    ExecutionGraph,
    MergeConflict,
    RunStatus,
    TaskNode,
    TaskStatus,
    TaskTestResult,
)
from shard.planner import topological_sort
from shard.state import StateManager

logger = logging.getLogger(__name__)


class Aggregator:
    """Handles merging completed branches and running tests."""

    def __init__(
        self,
        graph: ExecutionGraph,
        git: GitManager,
        state: StateManager,
    ) -> None:
        self.graph = graph
        self.git = git
        self.state = state

    async def merge_all(self) -> list[MergeConflict]:
        """Merge all completed task branches into staging in dependency order."""
        self.graph.status = RunStatus.MERGING
        self.state.save_graph(self.graph)

        # Create staging branch
        await self.git.create_staging_branch(self.graph)
        staging_path = await self.git.provision_staging_worktree(self.graph)

        conflicts: list[MergeConflict] = []
        topo_order = topological_sort(self.graph)

        for task_id in topo_order:
            node = self.graph.get_node(task_id)
            if node is None or node.status != TaskStatus.COMPLETED:
                continue
            if node.branch_name is None:
                continue

            logger.info("Merging branch for task %s: %s", task_id, node.branch_name)
            conflict = await self.git.merge_branch(
                staging_path, node.branch_name, task_id
            )
            if conflict is not None:
                # Classify and attempt resolution
                resolved = await self._resolve_conflict(staging_path, conflict)
                if not resolved:
                    await self.git.abort_merge(staging_path)
                    conflicts.append(conflict)
                    logger.error(
                        "Unresolved merge conflict for task %s: %s",
                        task_id, conflict.conflicting_files,
                    )

        return conflicts

    async def _resolve_conflict(
        self, staging_path: Path, conflict: MergeConflict
    ) -> bool:
        """Attempt to resolve a merge conflict."""
        for file_path in conflict.conflicting_files:
            if is_structural_file(file_path):
                conflict.conflict_type = "structural"
                resolved = await self._resolve_structural_conflict(staging_path, file_path)
                if not resolved:
                    return False
            elif is_barrel_file(file_path):
                conflict.conflict_type = "structural"
                resolved = await self._resolve_barrel_conflict(staging_path, file_path)
                if not resolved:
                    return False
            else:
                conflict.conflict_type = "application"
                return False
        # If all conflicts resolved, commit
        rc, _, _ = await self.git._run_git("add", "-A", cwd=staging_path)
        rc, _, _ = await self.git._run_git(
            "commit", "-m", f"wt: resolve merge conflict for {conflict.task_id}",
            cwd=staging_path,
        )
        return rc == 0

    async def _resolve_structural_conflict(
        self, staging_path: Path, file_path: str
    ) -> bool:
        """Resolve conflicts in structural files (requirements.txt, etc.) via union."""
        full_path = staging_path / file_path
        if not full_path.exists():
            return False

        content = full_path.read_text()
        if "<<<<<<" not in content:
            return True

        # Extract both sides and merge
        lines: set[str] = set()
        in_conflict = False
        for line in content.split("\n"):
            if line.startswith("<<<<<<"):
                in_conflict = True
                continue
            if line.startswith("======"):
                continue
            if line.startswith(">>>>>>"):
                in_conflict = False
                continue
            stripped = line.strip()
            if stripped:
                lines.add(stripped)

        # Write sorted union
        full_path.write_text("\n".join(sorted(lines)) + "\n")
        return True

    async def _resolve_barrel_conflict(
        self, staging_path: Path, file_path: str
    ) -> bool:
        """Resolve conflicts in barrel/index files via alphabetical union."""
        return await self._resolve_structural_conflict(staging_path, file_path)

    async def run_tests(self, staging_path: Path) -> list[TaskTestResult]:
        """Run the test suite in the staging worktree."""
        self.graph.status = RunStatus.TESTING
        self.state.save_graph(self.graph)

        cmd = [self.graph.config.test_runner] + list(self.graph.config.test_args)
        if self.graph.config.test_json_report:
            report_path = staging_path / ".report.json"
            cmd.extend(["--json-report", f"--json-report-file={report_path}"])

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=staging_path,
        )
        stdout, stderr = await proc.communicate()

        results: list[TaskTestResult] = []

        # Try to parse JSON report
        if self.graph.config.test_json_report:
            report_path = staging_path / ".report.json"
            if report_path.exists():
                results = self._parse_json_report(report_path)
                return results

        # Fallback: parse text output
        results = self._parse_text_output(stdout.decode(), stderr.decode())
        return results

    def _parse_json_report(self, report_path: Path) -> list[TaskTestResult]:
        """Parse pytest-json-report output."""
        with open(report_path) as f:
            report = json.load(f)

        results: list[TaskTestResult] = []
        for test in report.get("tests", []):
            node_id = test.get("nodeid", "")
            parts = node_id.split("::")
            test_file = parts[0] if parts else ""

            outcome = test.get("outcome", "unknown")
            message = ""
            if outcome == "failed":
                call = test.get("call", {})
                message = call.get("longrepr", "")

            results.append(TaskTestResult(
                test_name=node_id,
                test_file=test_file,
                passed=outcome == "passed",
                message=message,
                duration_s=test.get("duration", 0.0),
            ))
        return results

    def _parse_text_output(self, stdout: str, stderr: str) -> list[TaskTestResult]:
        """Fallback parser for text test output."""
        results: list[TaskTestResult] = []
        combined = stdout + "\n" + stderr
        for line in combined.split("\n"):
            if "PASSED" in line:
                results.append(TaskTestResult(
                    test_name=line.strip(), test_file="", passed=True
                ))
            elif "FAILED" in line:
                results.append(TaskTestResult(
                    test_name=line.strip(), test_file="", passed=False,
                    message=line.strip(),
                ))
        return results

    def map_failures_to_tasks(
        self, results: list[TaskTestResult]
    ) -> dict[str, list[TaskTestResult]]:
        """Map failing tests back to their owning task nodes."""
        task_failures: dict[str, list[TaskTestResult]] = {}
        for result in results:
            if result.passed:
                continue
            # Find the task that owns this test file
            for node in self.graph.nodes:
                if result.test_file in node.test_files:
                    if node.task_id not in task_failures:
                        task_failures[node.task_id] = []
                    task_failures[node.task_id].append(result)
                    break
            else:
                # Unmapped failure - assign to a catch-all
                if "__unmapped__" not in task_failures:
                    task_failures["__unmapped__"] = []
                task_failures["__unmapped__"].append(result)
        return task_failures


class SelfHealingLoop:
    """Stage 5: Bounded self-healing loop for test failure resolution."""

    def __init__(
        self,
        graph: ExecutionGraph,
        aggregator: Aggregator,
        git: GitManager,
        state: StateManager,
    ) -> None:
        self.graph = graph
        self.aggregator = aggregator
        self.git = git
        self.state = state
        self._previous_failures: set[str] = set()

    async def heal(self, staging_path: Path) -> bool:
        """Run the self-healing loop. Returns True if all tests pass."""
        self.graph.status = RunStatus.HEALING
        self.state.save_graph(self.graph)

        while self.graph.global_retry_count < self.graph.config.max_retries_global:
            # Run tests
            results = await self.aggregator.run_tests(staging_path)
            failures = [r for r in results if not r.passed]

            if not failures:
                logger.info("All tests passing after healing")
                return True

            # Check for regression (more failures than before)
            current_failure_names = {r.test_name for r in failures}
            if self._previous_failures and len(current_failure_names) > len(self._previous_failures):
                new_failures = current_failure_names - self._previous_failures
                logger.error(
                    "Regression detected! %d new failures: %s",
                    len(new_failures), new_failures,
                )
                return False

            self._previous_failures = current_failure_names

            # Map failures to tasks
            task_failures = self.aggregator.map_failures_to_tasks(results)

            # Re-dispatch responsible agents
            healed_any = False
            for task_id, task_results in task_failures.items():
                if task_id == "__unmapped__":
                    logger.warning("Unmapped test failures: %s",
                                   [r.test_name for r in task_results])
                    continue

                node = self.graph.get_node(task_id)
                if node is None:
                    continue

                if node.retry_count >= self.graph.config.max_retries_per_task:
                    logger.warning(
                        "Task %s has exhausted retries (%d/%d)",
                        task_id, node.retry_count, self.graph.config.max_retries_per_task,
                    )
                    continue

                # Update prompt for healing
                failure_details = "\n".join(
                    f"- {r.test_name}: {r.message}" for r in task_results
                )
                node.prompt = (
                    f"The following test(s) are failing:\n{failure_details}\n\n"
                    f"The test code is authoritative; fix the implementation.\n\n"
                    f"Original task: {node.title}\n"
                    f"You MUST only modify files: {', '.join(node.owned_files)}"
                )
                node.status = TaskStatus.HEALING
                node.retry_count += 1
                self.graph.global_retry_count += 1
                healed_any = True

                self.state.checkpoint_task(self.graph, node, "task_healing")
                logger.info(
                    "Re-dispatching task %s for healing (retry %d/%d)",
                    task_id, node.retry_count, self.graph.config.max_retries_per_task,
                )

            if not healed_any:
                logger.error("No tasks could be healed (all retries exhausted or unmapped)")
                return False

            self.state.save_graph(self.graph)

            # Wait for healing tasks to complete (they need to be dispatched externally)
            # The orchestrator will handle re-dispatch
            return False  # Signal that healing tasks need dispatch

        logger.error("Global retry limit exceeded (%d)", self.graph.config.max_retries_global)
        return False
