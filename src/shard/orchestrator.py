"""Main orchestrator: ties all five stages together into a single pipeline."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import uuid

from shard.aggregator import Aggregator, SelfHealingLoop
from shard.config import load_config
from shard.dispatcher import Dispatcher, get_provider
from shard.git_manager import GitManager
from shard.models import (
    ExecutionGraph,
    RunConfig,
    RunStatus,
    TaskNode,
    TaskStatus,
)
from shard.planner import (
    detect_collisions,
    invoke_planner,
    resolve_collisions_by_serialization,
    topological_sort,
    validate_acyclicity,
)
from shard.state import StateManager
from shard.tui import ShardTUI

logger = logging.getLogger(__name__)


class Orchestrator:
    """Top-level orchestrator that drives the five-stage pipeline."""

    def __init__(
        self,
        repo_root: Path,
        config: RunConfig | None = None,
        run_id: str | None = None,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.config = config or load_config(self.repo_root)
        self.run_id = run_id or f"wt-{uuid.uuid4().hex[:16]}"

        self.state = StateManager(self.repo_root, self.run_id)
        self.git = GitManager(self.repo_root, self.config.worktree_dir)
        self.graph: ExecutionGraph | None = None
        self.tui: ShardTUI | None = None

    async def run(self, prompt: str, plan_only: bool = False) -> ExecutionGraph:
        """Execute the full pipeline for a given prompt."""
        self.state.initialize()

        # Get current git state
        root_commit = await self.git.get_current_commit()
        target_branch = await self.git.get_current_branch()

        self.graph = ExecutionGraph(
            run_id=self.run_id,
            root_commit=root_commit,
            target_branch=target_branch,
            config=self.config,
        )
        self.state.save_graph(self.graph)

        try:
            # Stage 1: Planning
            await self._stage_architect(prompt)
            if plan_only:
                logger.info("Plan-only mode: stopping after Stage 1")
                return self.graph

            # Stage 2: Partitioning
            await self._stage_partitioner()

            # Stage 3: Dispatching
            await self._stage_dispatcher()

            # Check if any tasks failed
            failed = [n for n in self.graph.nodes if n.status == TaskStatus.FAILED]
            timed_out = [n for n in self.graph.nodes if n.status == TaskStatus.TIMED_OUT]
            if failed or timed_out:
                logger.warning(
                    "%d failed, %d timed out tasks", len(failed), len(timed_out)
                )

            # Stage 4: Aggregation
            staging_path = await self._stage_aggregator()

            # Stage 5: Self-healing (if needed)
            if staging_path:
                success = await self._stage_self_healing(staging_path)
                if success:
                    # Fast-forward target branch
                    ff_ok = await self.git.fast_forward_target(self.graph)
                    if ff_ok:
                        self.graph.status = RunStatus.COMPLETED
                        logger.info("Pipeline completed successfully")
                    else:
                        self.graph.status = RunStatus.FAILED
                        logger.error("Fast-forward to target branch failed")
                else:
                    self.graph.status = RunStatus.FAILED

            # Cleanup on success
            if self.graph.status == RunStatus.COMPLETED and self.config.auto_cleanup:
                await self.git.cleanup_run(self.graph)

        except Exception as e:
            logger.error("Pipeline failed with exception: %s", e)
            if self.graph:
                self.graph.status = RunStatus.FAILED
                self.state.save_graph(self.graph)
            raise
        finally:
            if self.tui:
                self.tui.stop()
                self.tui.print_summary()
            if self.graph:
                self.state.save_graph(self.graph)
                self.state.save_checkpoint(self.graph)

        return self.graph

    async def resume(self) -> ExecutionGraph:
        """Resume an interrupted run."""
        self.state.initialize()
        graph = self.state.load_graph()
        if graph is None:
            graph = self.state.load_checkpoint()
        if graph is None:
            raise RuntimeError(f"No state found for run {self.run_id}")

        self.graph = graph
        self.config = graph.config

        # Reset interrupted/running tasks to QUEUED
        for node in graph.nodes:
            if node.status in (TaskStatus.RUNNING, TaskStatus.INTERRUPTED):
                node.status = TaskStatus.QUEUED
                node.agent_pid = None

        logger.info("Resuming run %s from status %s", self.run_id, graph.status.value)

        # Determine which stage to resume from
        if graph.status in (RunStatus.DISPATCHING, RunStatus.INTERRUPTED):
            await self._stage_dispatcher()
            staging_path = await self._stage_aggregator()
            if staging_path:
                success = await self._stage_self_healing(staging_path)
                if success:
                    ff_ok = await self.git.fast_forward_target(graph)
                    graph.status = RunStatus.COMPLETED if ff_ok else RunStatus.FAILED
                else:
                    graph.status = RunStatus.FAILED
        elif graph.status == RunStatus.MERGING:
            staging_path = await self._stage_aggregator()
            if staging_path:
                success = await self._stage_self_healing(staging_path)
                if success:
                    ff_ok = await self.git.fast_forward_target(graph)
                    graph.status = RunStatus.COMPLETED if ff_ok else RunStatus.FAILED
                else:
                    graph.status = RunStatus.FAILED
        elif graph.status in (RunStatus.TESTING, RunStatus.HEALING):
            staging_path = self.git.worktree_base / graph.run_id / "staging"
            if staging_path.exists():
                success = await self._stage_self_healing(staging_path)
                if success:
                    ff_ok = await self.git.fast_forward_target(graph)
                    graph.status = RunStatus.COMPLETED if ff_ok else RunStatus.FAILED
                else:
                    graph.status = RunStatus.FAILED

        self.state.save_graph(graph)
        self.state.save_checkpoint(graph)
        return graph

    async def _stage_architect(self, prompt: str) -> None:
        """Stage 1: Generate DAG and test scaffold."""
        assert self.graph is not None
        self.graph.status = RunStatus.PLANNING
        self.state.save_graph(self.graph)

        logger.info("Stage 1: Planning - generating execution DAG")
        nodes, test_code = await invoke_planner(
            prompt, self.repo_root, self.config
        )

        self.graph.nodes = nodes

        # Validate and resolve collisions
        collisions = detect_collisions(self.graph)
        if collisions:
            logger.warning("Detected %d file ownership collisions", len(collisions))
            resolve_collisions_by_serialization(self.graph, collisions)

            # Verify resolution didn't create cycles
            if not validate_acyclicity(self.graph.nodes):
                raise RuntimeError("Collision resolution created a cycle in the DAG")

        # Create scaffold branch and commit tests
        await self.git.create_scaffold_branch(self.graph)
        if test_code:
            await self.git.commit_test_scaffold(self.graph, test_code)

        # Write prompt files
        for node in self.graph.nodes:
            self.state.write_prompt_file(node)

        self.state.save_graph(self.graph)
        self.state.append_event("planning_complete", {
            "num_tasks": len(nodes),
            "num_tests": len(test_code),
        })
        logger.info("Stage 1 complete: %d tasks, %d test files", len(nodes), len(test_code))

    async def _stage_partitioner(self) -> None:
        """Stage 2: Provision git worktrees."""
        assert self.graph is not None
        self.graph.status = RunStatus.PARTITIONING
        self.state.save_graph(self.graph)

        logger.info("Stage 2: Partitioning - provisioning worktrees")
        for node in self.graph.nodes:
            path = await self.git.provision_task_worktree(self.graph, node)
            logger.info("Provisioned worktree for %s at %s", node.task_id, path)

        self.state.save_graph(self.graph)
        self.state.append_event("partitioning_complete", {
            "num_worktrees": len(self.graph.nodes),
        })

    async def _stage_dispatcher(self) -> None:
        """Stage 3: Dispatch agents."""
        assert self.graph is not None

        provider = get_provider(
            self.config.agent_backend.value,
            self.config.agent_binary,
        )

        # Set up TUI
        self.tui = ShardTUI(self.graph)

        def on_output(task_id: str, line: str) -> None:
            if self.tui:
                self.tui.write_log(task_id, line)

        dispatcher = Dispatcher(self.graph, self.state, provider, on_output=on_output)

        self.tui.start()
        try:
            await dispatcher.dispatch_all()
        finally:
            self.tui.stop()

        # Audit file modifications
        await self._audit_file_modifications()

        self.state.save_graph(self.graph)
        self.state.append_event("dispatching_complete", {
            "completed": sum(1 for n in self.graph.nodes if n.status == TaskStatus.COMPLETED),
            "failed": sum(1 for n in self.graph.nodes if n.status == TaskStatus.FAILED),
        })

    async def _audit_file_modifications(self) -> None:
        """Check that agents only modified their owned files (Level 3 guardrail)."""
        assert self.graph is not None
        for node in self.graph.nodes:
            if node.status != TaskStatus.COMPLETED or not node.worktree_path:
                continue

            wt_path = Path(node.worktree_path)
            if not wt_path.exists():
                continue

            modified = await self.git.get_modified_files(wt_path)
            unauthorized = [f for f in modified if f not in node.owned_files]
            if unauthorized:
                logger.warning(
                    "Task %s modified unauthorized files: %s. Reverting.",
                    node.task_id, unauthorized,
                )
                for f in unauthorized:
                    await self.git.revert_file(wt_path, f)
                # Re-commit after revert
                await self.git.commit_worktree_changes(wt_path, f"{node.task_id}-revert")

    async def _stage_aggregator(self) -> Path | None:
        """Stage 4: Merge and test."""
        assert self.graph is not None

        aggregator = Aggregator(self.graph, self.git, self.state)

        logger.info("Stage 4: Aggregating - merging branches")
        conflicts = await aggregator.merge_all()

        if conflicts:
            logger.error(
                "Unresolved merge conflicts: %s",
                [(c.task_id, c.conflicting_files) for c in conflicts],
            )
            self.graph.status = RunStatus.FAILED
            self.state.save_graph(self.graph)
            return None

        staging_path = self.git.worktree_base / self.graph.run_id / "staging"
        return staging_path

    async def _stage_self_healing(self, staging_path: Path) -> bool:
        """Stage 5: Run tests and self-healing loop."""
        assert self.graph is not None

        aggregator = Aggregator(self.graph, self.git, self.state)
        healer = SelfHealingLoop(self.graph, aggregator, self.git, self.state)

        # Initial test run
        logger.info("Stage 5: Testing and self-healing")
        results = await aggregator.run_tests(staging_path)
        failures = [r for r in results if not r.passed]

        if not failures:
            logger.info("All tests passed on first run")
            return True

        logger.warning("%d tests failing, entering self-healing loop", len(failures))
        return await healer.heal(staging_path)

    async def abort(self) -> None:
        """Abort the current run."""
        if self.graph:
            self.graph.status = RunStatus.ABORTED
            self.state.save_graph(self.graph)
            self.state.save_checkpoint(self.graph)
            logger.info("Run %s aborted", self.run_id)

    async def clean(self) -> None:
        """Clean up all artifacts for this run."""
        if self.graph is None:
            self.graph = self.state.load_graph()
        if self.graph:
            await self.git.cleanup_run(self.graph)
            logger.info("Cleaned up run %s", self.run_id)
