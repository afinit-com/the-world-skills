"""Conversation-local context shared by channel adapters and scoped tools."""

from contextvars import ContextVar

_CURRENT_ASKER: ContextVar[str] = ContextVar("world_current_asker", default="")


def current_asker() -> str:
    """Identity that addressed the active conversational turn, if any."""

    return _CURRENT_ASKER.get()


def set_current_asker(value: str):
    """Set the active asker and return the ContextVar reset token."""

    return _CURRENT_ASKER.set(value)


def reset_current_asker(token) -> None:
    """Restore the value that preceded ``set_current_asker``."""

    _CURRENT_ASKER.reset(token)


_CURRENT_TASK: ContextVar[str] = ContextVar("world_current_task", default="")


def current_task() -> str:
    """Task id the active run belongs to, if any.

    Read by scoped write tools to resolve the `tasks/{task}/work` root, and injected
    into a spawn's env as WORLD_TASK so the guard hooks can resolve the same root.

    A contextvar rather than a tool argument on purpose: a task id the AGENT supplies is
    a parameter that reopens the boundary — it could name another task's directory. The
    scheduler sets this in the parent process before the run and the agent never sees it.
    """

    return _CURRENT_TASK.get()


def set_current_task(value: str):
    """Set the active task id and return the ContextVar reset token."""

    return _CURRENT_TASK.set(value)


def reset_current_task(token) -> None:
    """Restore the value that preceded ``set_current_task``."""

    _CURRENT_TASK.reset(token)


_CURRENT_WORKTREE: ContextVar[str] = ContextVar("world_current_worktree", default="")


def current_worktree() -> str:
    """The worktree directory the active mutates:true run was spawned in, if any.

    The runner's cwd moves into the worktree, but a scoped tool's roots are bound when
    the tool is constructed and do not follow cwd. A tool that edits or commits repo
    files reads this instead of a path of its own, so it lands where the run does. Set
    by spawn.py in the parent process, for the same reason as current_task: a checkout
    path the AGENT supplies could name the main checkout.
    """

    return _CURRENT_WORKTREE.get()


def set_current_worktree(value: str):
    """Set the active worktree and return the ContextVar reset token."""

    return _CURRENT_WORKTREE.set(value)


def reset_current_worktree(token) -> None:
    """Restore the value that preceded ``set_current_worktree``."""

    _CURRENT_WORKTREE.reset(token)
