---
name: util
description: Standalone utilities that support a World harness but don't need one — invoked with a subcommand. Currently "slack icon generate", which turns any source image into a Slack-compliant app/bot icon (square, 512–2000px, under 2MB) by picking the right squaring strategy per image. Use when the user says "the world util", "슬랙 아이콘", "slack icon 만들어", "아이콘으로 편집해줘", or a Slack icon upload was rejected for size/ratio.
---

# /the-world:util — harness-adjacent utilities

Everything here is a one-off tool with no harness preconditions: no `harness.py`, no
`WORLD.md`, no venv. That is the line between this skill and /the-world:start — anything
that touches a harness's own code lives in the harness (its world-word commands and
`world/`), installed there by /the-world:start.

## Dispatch

Read the argument the user passed and follow the matching reference file **in full**. Match
on the leading words, not an exact string — `slack icon generate`, `slack icon`, and `icon`
all mean the same subcommand.

| Subcommand | What it does | Follow |
|---|---|---|
| `slack icon generate` | Any image → Slack-compliant app/bot icon | `references/slack-icon.md` |

**No argument, or one that matches nothing:** don't guess and don't improvise a utility
that isn't here. List the subcommands above in one line and ask which they want. If the
request is clearly about a harness's agents, tools, or bootstrapping, say so and point at
the harness's own command (its world-word) or /the-world:start instead.

## Adding a subcommand later

One row in the table above, one file under `references/`, and one clause in this skill's
`description` so the trigger actually matches. Keep the preconditions rule: if it needs a
live harness, it belongs in the kit (`skills/start/assets/kit/`), not here.
