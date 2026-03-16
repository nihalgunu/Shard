"""Terminal UI using Rich for live status display during execution."""

from __future__ import annotations

import time
from collections import deque
from typing import Any

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from shard.models import ExecutionGraph, TaskNode, TaskStatus

# Color mapping for task statuses
STATUS_COLORS: dict[TaskStatus, str] = {
    TaskStatus.PENDING: "dim",
    TaskStatus.QUEUED: "yellow",
    TaskStatus.RUNNING: "blue",
    TaskStatus.COMPLETED: "green",
    TaskStatus.FAILED: "red",
    TaskStatus.TIMED_OUT: "red",
    TaskStatus.HEALING: "magenta",
    TaskStatus.INTERRUPTED: "yellow",
}


class ShardTUI:
    """Rich-based terminal UI for monitoring Shard execution."""

    def __init__(self, graph: ExecutionGraph) -> None:
        self.graph = graph
        self.console = Console()
        self._log_buffer: deque[str] = deque(maxlen=20)
        self._start_time = time.monotonic()
        self._live: Live | None = None
        self._estimated_cost = 0.0

    def start(self) -> None:
        """Start the live display."""
        self._start_time = time.monotonic()
        self._live = Live(
            self._render(),
            console=self.console,
            refresh_per_second=2,
            transient=False,
        )
        self._live.start()

    def stop(self) -> None:
        """Stop the live display."""
        if self._live:
            self._live.stop()
            self._live = None

    def update(self) -> None:
        """Refresh the display."""
        if self._live:
            self._live.update(self._render())

    def write_log(self, task_id: str, line: str) -> None:
        """Add a log line from an agent."""
        self._log_buffer.append(f"[{task_id}] {line}")
        self.update()

    def set_cost(self, cost: float) -> None:
        """Update estimated cost display."""
        self._estimated_cost = cost
        self.update()

    def _render(self) -> Panel:
        """Render the full TUI panel."""
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=1),
            Layout(name="table", ratio=3),
            Layout(name="logs", ratio=2),
            Layout(name="footer", size=1),
        )

        # Header
        elapsed = time.monotonic() - self._start_time
        elapsed_str = f"{int(elapsed // 60):02d}:{int(elapsed % 60):02d}"
        active = sum(1 for n in self.graph.nodes if n.status == TaskStatus.RUNNING)
        total = len(self.graph.nodes)
        header_text = Text(
            f" Run: {self.graph.run_id}  |  {elapsed_str} elapsed  |  "
            f"Status: {self.graph.status.value}  |  Agents: {active}/{total}",
            style="bold",
        )
        layout["header"].update(header_text)

        # Task table
        table = Table(expand=True, box=None, padding=(0, 1))
        table.add_column("TASK", style="cyan", width=15)
        table.add_column("TITLE", width=35)
        table.add_column("STATUS", width=12)
        table.add_column("PID", width=8, justify="right")
        table.add_column("TIME", width=8, justify="right")
        table.add_column("RETRIES", width=10, justify="right")
        table.add_column("INFO", width=25)

        for node in self.graph.nodes:
            status_color = STATUS_COLORS.get(node.status, "white")
            status_text = Text(node.status.value, style=status_color)

            pid_str = str(node.agent_pid) if node.agent_pid else "-"
            time_str = f"{node.duration_s:.0f}s" if node.duration_s else "-"

            retry_str = f"{node.retry_count}/{self.graph.config.max_retries_per_task}"

            # Info column
            info = ""
            if node.status == TaskStatus.QUEUED and node.depends_on:
                info = f"(blocked by {', '.join(node.depends_on)})"
            elif node.status == TaskStatus.FAILED:
                info = f"exit={node.exit_code}"

            table.add_row(
                node.task_id,
                node.title[:35],
                status_text,
                pid_str,
                time_str,
                retry_str,
                info,
            )

        layout["table"].update(Panel(table, title="Tasks"))

        # Log pane
        log_lines = list(self._log_buffer)[-5:]
        log_text = "\n".join(log_lines) if log_lines else "(no output yet)"
        layout["logs"].update(Panel(Text(log_text), title="Agent Output"))

        # Footer
        tokens = self.graph.token_budget_actual or self.graph.token_budget_estimate
        footer_text = Text(
            f" Tokens: ~{tokens:,}  |  Est. Cost: ${self._estimated_cost:.2f}  |  "
            f"q:abort  l:logs  s:status",
            style="dim",
        )
        layout["footer"].update(footer_text)

        return Panel(
            layout,
            title=f"[bold]Shard v1.0.0[/bold]",
            border_style="blue",
        )

    def print_summary(self) -> None:
        """Print a final summary after execution."""
        self.console.print()
        completed = sum(1 for n in self.graph.nodes if n.status == TaskStatus.COMPLETED)
        failed = sum(1 for n in self.graph.nodes if n.status == TaskStatus.FAILED)
        total = len(self.graph.nodes)

        status_style = "green" if self.graph.status == RunStatus.COMPLETED else "red"

        self.console.print(
            f"[bold]Run {self.graph.run_id}[/bold]: "
            f"[{status_style}]{self.graph.status.value}[/{status_style}]"
        )
        self.console.print(
            f"  Tasks: {completed}/{total} completed, {failed} failed"
        )
        self.console.print(
            f"  Retries: {self.graph.global_retry_count}/{self.graph.config.max_retries_global}"
        )
        if self.graph.token_budget_actual:
            self.console.print(
                f"  Tokens: {self.graph.token_budget_actual:,}"
            )
        self.console.print()
