#!/usr/bin/env bash
# Fixture: the 관측대 harness with Watcher in service and a build-log archive it cannot
# reach yet. The job the case asks for needs a capability, and the cheap wrong answer is
# `allow_bash=True` plus grep. Building a tool is ground the other four cases never touch.
#
# No external binary is involved on purpose: the sandbox has no usable git
# (xcrun: Failed to locate 'git'), so a git-shaped tool would fail for
# reasons unrelated to the skill.
set -eu

mkdir -p abilities/watcher env .the-world tasks logs/build

cat > harness.py <<'PY'
#!/usr/bin/env python3
"""밤하늘 관측대 — 하네스."""

from pathlib import Path

from world import Harness, Persona

from abilities.watcher import agent as watcher

h = Harness(
    name="obs",
    provider="claude",
    home=Path(__file__).resolve().parent,
    world_doc="WORLD.md",
    main_persona=Persona(
        key="keeper",
        display="Obs-Keeper",
        flavor="관측대를 지키고 관측 기록을 모은다",
    ),
)

h.register(watcher.AGENT)

if __name__ == "__main__":
    h.main()
PY

: > abilities/__init__.py
: > abilities/watcher/__init__.py

cat > abilities/watcher/agent.py <<'PY'
"""watcher — 밤새 무엇이 바뀌었는지 적는다. Persona: Obs-Watcher (scout/observer)."""

from world import Agent, Persona, PollTrigger

from abilities.watcher.check import check

AGENT = Agent(
    name="watcher",
    trigger=PollTrigger(check=check),
    prompt_file="abilities/watcher/prompt.md",
    persona=Persona(
        key="watcher",
        display="Obs-Watcher",
        flavor="밤새 무엇이 바뀌었는지 지켜보고 적는다",
        bot_token_env="OBS_PERSONA_WATCHER_TOKEN",
    ),
    writes_report=True,
    mutates=False,
    heartbeat_fields=["last_run"],
    write_scope=("tasks/{task}/work",),
    tools=[],
    allow_bash=False,
)
PY

cat > abilities/watcher/check.py <<'PY'
"""Poll check for watcher — cheap and LLM-free."""

from __future__ import annotations

from world import RunResult, Signal

COOLDOWN_S = 86400


def check(state: dict, now: float) -> Signal | None:
    me = state["agents"]["watcher"]
    if now - me.get("last_run", 0) < COOLDOWN_S:
        return None

    def on_done(_result: RunResult) -> None:
        me["last_run"] = now

    return Signal(prompt="어젯밤 무엇이 바뀌었는지 적으세요.", on_done=on_done)
PY

cat > abilities/watcher/prompt.md <<'MD'
너는 Obs-Watcher다. 밤새 무엇이 바뀌었는지 읽고 사람이 훑어볼 수 있게 짧게 적는다.
읽기만 한다. 고치지 않는다.
MD

cat > WORLD.md <<'MD'
# WORLD — 밤하늘 관측대

**Bot name prefix**: `Obs-`

## Roster (in service)

| Persona | Archetype | Agent | Flavor |
|---------|-----------|-------|--------|
| Keeper | router/interface | (main) | 관측대를 지키고 관측 기록을 모은다 |
| Watcher | scout/observer | watcher | 밤새 무엇이 바뀌었는지 적는다. 지금 가진 장비는 없다 |

## Vocabulary

| Kit concept | Generic word | Your word |
|---|---|---|
| creating a new agent | add an agent | recruit |
| creating/wiring a tool | build a tool | equip |
MD

cat > logs/build/2026-09-18-0412.log <<'MD'
[04:12:03] build start rev=8f44837
[04:12:41] compile ok
[04:13:02] test suite ok (123 passed)
[04:13:04] build SUCCESS
MD

cat > logs/build/2026-09-19-0355.log <<'MD'
[03:55:10] build start rev=97037eb
[03:55:48] compile ok
[03:56:20] test suite FAILED
[03:56:20]   tests/test_guard_preflight.py::TaskTokenScopeTests - FileNotFoundError
[03:56:21] build FAILURE
MD

cat > logs/build/2026-09-20-0401.log <<'MD'
[04:01:07] build start rev=cfa8462
[04:01:44] compile ok
[04:02:19] test suite ok (123 passed)
[04:02:20] build SUCCESS
MD

printf '%s\n' '{"watcher": {"write_scope": ["tasks/{task}/work"], "task_allowed": false}}' \
  > .the-world/agent-scope.json

printf 'OBS_SLACK_BOT_TOKEN=\n' > env/harness.env.example
