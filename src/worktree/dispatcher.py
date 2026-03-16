"""Stage 3: The Async Dispatcher - Parallel agent execution engine."""

from __future__ import annotations

import asyncio
import logging
import signal
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from worktree.models import ExecutionGraph, RunStatus, TaskNode, TaskStatus
from worktree.planner import topological_sort
from worktree.state import StateManager

logger = logging.getLogger(__name__)


class AgentProvider:
    """Base class for agent provider adapters."""

    def get_command(self, task: TaskNode) -> list[str]:
        """Return the command to spawn the agent."""
        raise NotImplementedError

    def get_env(self, task: TaskNode) -> dict[str, str] | None:
        """Return additional environment variables for the agent, or None."""
        return None


class ClaudeCodeProvider(AgentProvider):
    """Provider for Claude Code CLI."""

    def __init__(self, binary: str = "claude") -> None:
        self.binary = binary

    def get_command(self, task: TaskNode) -> list[str]:
        return [
            self.binary,
            "--print",
            "--dangerously-skip-permissions",
            task.prompt,
        ]


class AiderProvider(AgentProvider):
    """Provider for Aider CLI."""

    def __init__(self, binary: str = "aider") -> None:
        self.binary = binary

    def get_command(self, task: TaskNode) -> list[str]:
        cmd = [self.binary, "--yes-always", "--no-auto-commits", "--message", task.prompt]
        for f in task.owned_files:
            cmd.extend(["--file", f])
        return cmd


class CustomProvider(AgentProvider):
    """Provider for custom agent commands."""

    def __init__(self, binary: str) -> None:
        self.binary = binary

    def get_command(self, task: TaskNode) -> list[str]:
        assert task.prompt_file is not None
        return [self.binary, "--prompt-file", task.prompt_file, "--workdir", task.worktree_path or "."]


def get_provider(backend: str, binary: str) -> AgentProvider:
    """Factory for agent providers."""
    providers: dict[str, type[AgentProvider]] = {
        "claude-code": ClaudeCodeProvider,
        "aider": AiderProvider,
        "cursor-cli": CustomProvider,
        "custom": CustomProvider,
    }
    provider_cls = providers.get(backend, CustomProvider)
    return provider_cls(binary=binary)


class Dispatcher:
    """Async dispatcher that walks the DAG and spawns agent subprocesses."""

    def __init__(
        self,
        graph: ExecutionGraph,
        state: StateManager,
        provider: AgentProvider,
        on_output: Callable[[str, str], None] | None = None,
    ) -> None:
        self.graph = graph
        self.state = state
        self.provider = provider
        self.on_output = on_output  # callback(task_id, line)
        self._semaphore = asyncio.Semaphore(graph.config.max_agents)
        self._shutdown = False
        self._running_tasks: dict[str, asyncio.subprocess.Process] = {}
        self._start_time = time.monotonic()

    async def dispatch_all(self) -> None:
        """Dispatch all tasks in the DAG, respecting dependencies."""
        self.graph.status = RunStatus.DISPATCHING
        self.state.save_graph(self.graph)
        self._start_time = time.monotonic()

        # Set up signal handlers for graceful shutdown
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._handle_shutdown)

        try:
            await self._dispatch_loop()
        finally:
            # Remove signal handlers
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.remove_signal_handler(sig)

    async def _dispatch_loop(self) -> None:
        """Main dispatch loop: find runnable tasks, spawn agents, wait."""
        pending_tasks: set[str] = set()
        running_futures: dict[str, asyncio.Task[None]] = {}

        while not self._shutdown:
            # Check global timeout
            elapsed = time.monotonic() - self._start_time
            if elapsed > self.graph.config.global_timeout_s:
                logger.error("Global timeout exceeded (%.0fs)", elapsed)
                await self._terminate_all()
                self.graph.status = RunStatus.INTERRUPTED
                self.state.save_graph(self.graph)
                return

            # Find newly runnable tasks
            runnable = self.graph.get_runnable_nodes()
            for node in runnable:
                if node.task_id not in pending_tasks and node.task_id not in running_futures:
                    pending_tasks.add(node.task_id)
                    node.status = TaskStatus.QUEUED
                    self.state.checkpoint_task(self.graph, node, "task_queued")

                    future = asyncio.create_task(self._dispatch_node(node))
                    running_futures[node.task_id] = future

            # Check for completed futures
            done_ids: list[str] = []
            for task_id, future in running_futures.items():
                if future.done():
                    done_ids.append(task_id)
                    pending_tasks.discard(task_id)
                    if future.exception():
                        logger.error(
                            "Task %s raised exception: %s", task_id, future.exception()
                        )

            for task_id in done_ids:
                del running_futures[task_id]

            # Check if all tasks are done
            all_done = all(
                n.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.TIMED_OUT)
                for n in self.graph.nodes
            )
            if all_done:
                break

            # Check for deadlock (no runnable tasks and no running tasks)
            if not runnable and not running_futures:
                remaining = [
                    n for n in self.graph.nodes
                    if n.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.TIMED_OUT)
                ]
                if remaining:
                    logger.error("Deadlock detected! Tasks stuck: %s",
                                 [t.task_id for t in remaining])
                    self.graph.status = RunStatus.FAILED
                    self.state.save_graph(self.graph)
                    return
                break

            # Apply stagger delay between launches
            if runnable:
                await asyncio.sleep(self.graph.config.stagger_delay_s)
            else:
                await asyncio.sleep(0.5)

    async def _dispatch_node(self, node: TaskNode) -> None:
        """Dispatch a single task node to an agent subprocess."""
        async with self._semaphore:
            if self._shutdown:
                return

            node.status = TaskStatus.RUNNING
            node.started_at = datetime.utcnow().isoformat()
            self.state.checkpoint_task(self.graph, node, "task_started")

            cmd = self.provider.get_command(node)
            env = self.provider.get_env(node)
            stdout_path, stderr_path = self.state.get_log_paths(node.task_id)
            node.stdout_log = str(stdout_path)
            node.stderr_log = str(stderr_path)

            logger.info("Dispatching task %s: %s (cwd=%s)", node.task_id, node.title, node.worktree_path)

            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=node.worktree_path,
                    env=env,
                )
                node.agent_pid = proc.pid
                self._running_tasks[node.task_id] = proc

                # Stream output with timeout and stall detection
                await self._monitor_process(node, proc, stdout_path, stderr_path)

            except FileNotFoundError:
                logger.error("Agent binary not found: %s", cmd[0])
                node.status = TaskStatus.FAILED
                node.exit_code = -1
            except asyncio.TimeoutError:
                logger.warning("Task %s timed out", node.task_id)
                node.status = TaskStatus.TIMED_OUT
                await self._terminate_process(node.task_id)
            except Exception as e:
                logger.error("Task %s failed with exception: %s", node.task_id, e)
                node.status = TaskStatus.FAILED
                node.exit_code = -1
            finally:
                self._running_tasks.pop(node.task_id, None)
                node.completed_at = datetime.utcnow().isoformat()
                if node.started_at:
                    start = datetime.fromisoformat(node.started_at)
                    end = datetime.fromisoformat(node.completed_at)
                    node.duration_s = (end - start).total_seconds()
                self.state.checkpoint_task(self.graph, node, "task_completed")

    async def _monitor_process(
        self,
        node: TaskNode,
        proc: asyncio.subprocess.Process,
        stdout_path: Path,
        stderr_path: Path,
    ) -> None:
        """Monitor process with output streaming, stall detection, and timeout."""
        last_output_time = time.monotonic()

        async def stream_to_file(
            stream: asyncio.StreamReader | None, path: Path, prefix: str
        ) -> str:
            nonlocal last_output_time
            output_lines: list[str] = []
            if stream is None:
                return ""
            with open(path, "w") as f:
                async for line_bytes in stream:
                    decoded = line_bytes.decode(errors="replace").rstrip()
                    f.write(decoded + "\n")
                    f.flush()
                    output_lines.append(decoded)
                    last_output_time = time.monotonic()
                    if self.on_output:
                        self.on_output(node.task_id, decoded)
            return "\n".join(output_lines[-100:])  # Keep last 100 lines

        # Create streaming tasks
        stdout_task = asyncio.create_task(
            stream_to_file(proc.stdout, stdout_path, node.task_id)
        )
        stderr_task = asyncio.create_task(
            stream_to_file(proc.stderr, stderr_path, node.task_id)
        )

        # Wait with timeout
        try:
            await asyncio.wait_for(proc.wait(), timeout=node.timeout if hasattr(node, 'timeout') else self.graph.config.per_task_timeout_s)
        except asyncio.TimeoutError:
            logger.warning("Task %s exceeded timeout", node.task_id)
            node.status = TaskStatus.TIMED_OUT
            await self._terminate_process(node.task_id)

        await stdout_task
        await stderr_task

        if proc.returncode == 0:
            node.status = TaskStatus.COMPLETED
        else:
            node.status = TaskStatus.FAILED
            node.exit_code = proc.returncode

    async def _terminate_process(self, task_id: str) -> None:
        """Terminate a running agent process (SIGTERM, then SIGKILL)."""
        proc = self._running_tasks.get(task_id)
        if proc is None or proc.returncode is not None:
            return

        logger.info("Terminating task %s (PID %s)", task_id, proc.pid)
        try:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                logger.warning("Task %s did not exit after SIGTERM, sending SIGKILL", task_id)
                proc.kill()
                await proc.wait()
        except ProcessLookupError:
            pass

    async def _terminate_all(self) -> None:
        """Terminate all running agent processes."""
        for task_id in list(self._running_tasks.keys()):
            await self._terminate_process(task_id)

    def _handle_shutdown(self) -> None:
        """Handle SIGTERM/SIGINT for graceful shutdown."""
        logger.info("Shutdown signal received, terminating agents...")
        self._shutdown = True
        # Mark in-flight tasks as interrupted
        for node in self.graph.nodes:
            if node.status == TaskStatus.RUNNING:
                node.status = TaskStatus.INTERRUPTED
        self.graph.status = RunStatus.INTERRUPTED
        self.state.save_graph(self.graph)

    async def dispatch_single(self, node: TaskNode) -> None:
        """Dispatch a single task (used by self-healing loop)."""
        await self._dispatch_node(node)
