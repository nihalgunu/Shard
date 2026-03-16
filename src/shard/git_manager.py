"""Stage 2: The Partitioner - Git worktree isolation manager."""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

from shard.models import ExecutionGraph, MergeConflict, TaskNode

logger = logging.getLogger(__name__)


class GitManager:
    """Manages git worktree provisioning, merging, and cleanup."""

    def __init__(self, repo_root: Path, worktree_base: str = "../worktrees") -> None:
        self.repo_root = repo_root
        self.worktree_base = (repo_root / worktree_base).resolve()
        self._lock = asyncio.Lock()

    async def _run_git(self, *args: str, cwd: Path | None = None) -> tuple[int, str, str]:
        """Run a git command and return (returncode, stdout, stderr)."""
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd or self.repo_root,
        )
        stdout, stderr = await proc.communicate()
        return proc.returncode, stdout.decode().strip(), stderr.decode().strip()

    async def get_current_commit(self) -> str:
        """Get the current HEAD commit SHA."""
        rc, stdout, _ = await self._run_git("rev-parse", "HEAD")
        if rc != 0:
            raise RuntimeError("Failed to get current commit")
        return stdout

    async def get_current_branch(self) -> str:
        """Get the current branch name."""
        rc, stdout, _ = await self._run_git("rev-parse", "--abbrev-ref", "HEAD")
        if rc != 0:
            raise RuntimeError("Failed to get current branch")
        return stdout

    async def create_scaffold_branch(self, graph: ExecutionGraph) -> None:
        """Create the tests-scaffold branch from the root commit."""
        rc, _, stderr = await self._run_git(
            "branch", graph.scaffold_branch, graph.root_commit
        )
        if rc != 0:
            # Branch may already exist from a previous run
            if "already exists" not in stderr:
                raise RuntimeError(f"Failed to create scaffold branch: {stderr}")

    async def commit_test_scaffold(
        self, graph: ExecutionGraph, test_files: dict[str, str]
    ) -> None:
        """Commit test files to the scaffold branch via a temporary worktree."""
        scaffold_path = self.worktree_base / graph.run_id / "scaffold"
        await self.provision_worktree(scaffold_path, graph.scaffold_branch)
        try:
            for rel_path, content in test_files.items():
                full_path = scaffold_path / rel_path
                full_path.parent.mkdir(parents=True, exist_ok=True)
                full_path.write_text(content)
                await self._run_git("add", rel_path, cwd=scaffold_path)

            await self._run_git(
                "commit", "-m", f"wt: test scaffold for {graph.run_id}",
                cwd=scaffold_path,
            )
        finally:
            await self.remove_worktree(scaffold_path)

    async def provision_worktree(self, path: Path, branch: str) -> None:
        """Create a git worktree at the given path on the given branch."""
        async with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            rc, _, stderr = await self._run_git(
                "worktree", "add", str(path), "-b", branch,
            )
            if rc != 0:
                # Try without -b if branch already exists
                rc, _, stderr = await self._run_git(
                    "worktree", "add", str(path), branch,
                )
                if rc != 0:
                    raise RuntimeError(f"Failed to provision worktree at {path}: {stderr}")
        logger.info("Provisioned worktree: %s on branch %s", path, branch)

    async def provision_worktree_from_ref(
        self, path: Path, branch: str, start_ref: str
    ) -> None:
        """Create a worktree branching from a specific ref."""
        async with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            rc, _, stderr = await self._run_git(
                "worktree", "add", str(path), "-b", branch, start_ref,
            )
            if rc != 0:
                raise RuntimeError(f"Failed to provision worktree: {stderr}")
        logger.info("Provisioned worktree: %s on branch %s from %s", path, branch, start_ref)

    async def remove_worktree(self, path: Path) -> None:
        """Remove a git worktree."""
        async with self._lock:
            rc, _, stderr = await self._run_git("worktree", "remove", str(path), "--force")
            if rc != 0:
                logger.warning("git worktree remove failed for %s: %s. Cleaning up manually.", path, stderr)
                if path.exists():
                    shutil.rmtree(path, ignore_errors=True)
                # Prune stale worktree entries
                await self._run_git("worktree", "prune")
        logger.info("Removed worktree: %s", path)

    async def provision_task_worktree(
        self, graph: ExecutionGraph, task: TaskNode
    ) -> Path:
        """Provision a worktree for a specific task node."""
        prefix = graph.config.branch_prefix
        branch = f"{prefix}/{graph.run_id}/{task.task_id}"
        path = self.worktree_base / graph.run_id / task.task_id

        await self.provision_worktree_from_ref(path, branch, graph.scaffold_branch)

        task.worktree_path = str(path)
        task.branch_name = branch
        return path

    async def commit_worktree_changes(
        self, worktree_path: Path, task_id: str
    ) -> bool:
        """Stage and commit any uncommitted changes in a worktree."""
        rc, stdout, _ = await self._run_git("status", "--porcelain", cwd=worktree_path)
        if not stdout:
            return False
        await self._run_git("add", "-A", cwd=worktree_path)
        await self._run_git(
            "commit", "-m", f"wt: {task_id} completed",
            cwd=worktree_path,
        )
        return True

    async def get_modified_files(self, worktree_path: Path) -> list[str]:
        """Get list of files modified in the worktree relative to its base."""
        rc, stdout, _ = await self._run_git(
            "diff", "--name-only", "HEAD~1", "HEAD",
            cwd=worktree_path,
        )
        if rc != 0 or not stdout:
            return []
        return stdout.split("\n")

    async def create_staging_branch(self, graph: ExecutionGraph) -> None:
        """Create the staging branch from the scaffold."""
        rc, _, stderr = await self._run_git(
            "branch", graph.staging_branch, graph.scaffold_branch
        )
        if rc != 0 and "already exists" not in stderr:
            raise RuntimeError(f"Failed to create staging branch: {stderr}")

    async def provision_staging_worktree(self, graph: ExecutionGraph) -> Path:
        """Create a worktree for the staging branch."""
        staging_path = self.worktree_base / graph.run_id / "staging"
        await self.provision_worktree(staging_path, graph.staging_branch)
        return staging_path

    async def merge_branch(
        self, staging_path: Path, branch: str, task_id: str
    ) -> MergeConflict | None:
        """Merge a task branch into the staging worktree."""
        rc, stdout, stderr = await self._run_git(
            "merge", "--no-ff", branch, "-m", f"Merge {task_id}",
            cwd=staging_path,
        )
        if rc == 0:
            return None

        # Parse conflict info
        conflicting_files: list[str] = []
        for line in stderr.split("\n"):
            if "CONFLICT" in line:
                # Extract file path from conflict message
                parts = line.split()
                if parts:
                    conflicting_files.append(parts[-1])

        # Also check git status for unmerged paths
        _, status_out, _ = await self._run_git("status", "--porcelain", cwd=staging_path)
        for line in status_out.split("\n"):
            if line.startswith("UU ") or line.startswith("AA "):
                conflicting_files.append(line[3:].strip())

        return MergeConflict(
            branch=branch,
            task_id=task_id,
            conflicting_files=list(set(conflicting_files)),
        )

    async def abort_merge(self, staging_path: Path) -> None:
        """Abort a failed merge."""
        await self._run_git("merge", "--abort", cwd=staging_path)

    async def fast_forward_target(self, graph: ExecutionGraph) -> bool:
        """Fast-forward the target branch to the staging branch."""
        rc, _, stderr = await self._run_git(
            "checkout", graph.target_branch
        )
        if rc != 0:
            logger.error("Failed to checkout target branch: %s", stderr)
            return False

        rc, _, stderr = await self._run_git(
            "merge", "--ff-only", graph.staging_branch
        )
        if rc != 0:
            logger.error("Fast-forward merge failed: %s", stderr)
            return False

        return True

    async def check_head_advanced(self, graph: ExecutionGraph) -> bool:
        """Check if HEAD of target branch has advanced past root_commit."""
        rc, stdout, _ = await self._run_git("rev-parse", graph.target_branch)
        if rc != 0:
            return False
        return stdout != graph.root_commit

    async def cleanup_run(self, graph: ExecutionGraph) -> None:
        """Clean up all worktrees and branches for a run."""
        run_dir = self.worktree_base / graph.run_id
        if run_dir.exists():
            # Remove all worktrees
            for wt_dir in run_dir.iterdir():
                if wt_dir.is_dir():
                    await self.remove_worktree(wt_dir)
            # Remove the run directory
            shutil.rmtree(run_dir, ignore_errors=True)

        # Delete branches
        prefix = f"{graph.config.branch_prefix}/{graph.run_id}/"
        rc, stdout, _ = await self._run_git("branch", "--list", f"{prefix}*")
        if rc == 0 and stdout:
            for branch in stdout.split("\n"):
                branch = branch.strip().lstrip("* ")
                if branch:
                    await self._run_git("branch", "-D", branch)

        # Prune worktree metadata
        await self._run_git("worktree", "prune")
        logger.info("Cleaned up run: %s", graph.run_id)

    async def revert_file(self, worktree_path: Path, file_path: str) -> None:
        """Revert unauthorized changes to a specific file."""
        await self._run_git("checkout", "--", file_path, cwd=worktree_path)

    async def verify_worktree_exists(self, path: Path, branch: str) -> bool:
        """Verify a worktree still exists and is on the expected branch."""
        if not path.exists():
            return False
        rc, stdout, _ = await self._run_git("rev-parse", "--abbrev-ref", "HEAD", cwd=path)
        return rc == 0 and stdout == branch
