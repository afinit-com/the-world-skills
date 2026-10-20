"""Spawning agents, the poll loop, restart-safe state, and executor worktrees.

Depends only on the core modules (types, toolkit). Nothing here imports the
Slack layer — world.slack sits ON TOP of this package, not beside it, because a
mention session spawns an agent but an agent never posts to Slack itself
(findings reach Slack through world.bridge, which the orchestrator calls).
"""
