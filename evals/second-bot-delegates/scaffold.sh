#!/usr/bin/env bash
# Fixture: an existing 밤하늘 관측대 harness with a main persona and ONE worker
# already in service. Shapes match the real kit API (world/harness.py) so nothing here
# misrepresents the system under test.
#
# Runs in the case's workspace (cwd). Referenced from case.yaml as
# `scaffold_script: scaffold.sh` — a PATH, not inline bash; the runner resolves
# it relative to the case dir and only executes it under `--scaffold`.
set -eu

mkdir -p abilities/watcher env .the-world tasks

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
"""watcher — 매일 저장소 변경을 요약한다. Persona: Obs-Watcher (scout/observer)."""

from world import Agent, Persona, PollTrigger

from abilities.watcher.check import check

AGENT = Agent(
    name="watcher",
    trigger=PollTrigger(check=check),
    prompt_file="abilities/watcher/prompt.md",
    persona=Persona(
        key="watcher",
        display="Obs-Watcher",
        flavor="밤새 저장소에 무엇이 바뀌었는지 지켜보고 적는다",
        bot_token_env="OBS_PERSONA_WATCHER_TOKEN",
    ),
    writes_finding=True,
    mutates=False,
    heartbeat_fields=["last_run"],
    write_scope=("tasks/{task}/work",),
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

    return Signal(prompt="어젯밤 저장소 변경을 요약하세요.", on_done=on_done)
PY

cat > abilities/watcher/prompt.md <<'MD'
너는 Obs-Watcher다. 밤새 저장소에 무엇이 바뀌었는지 읽고, 사람이 훑어볼 수 있게
짧게 적는다. 읽기만 한다. 고치지 않는다.
MD

cat > WORLD.md <<'MD'
# WORLD — 밤하늘 관측대

**Bot name prefix**: `Obs-`

## Roster (in service)

| Persona | Archetype | Agent | Flavor |
|---------|-----------|-------|--------|
| Keeper | router/interface | (main) | 관측대를 지키고 관측 기록을 모은다 |
| Watcher | scout/observer | watcher | 밤새 저장소에 무엇이 바뀌었는지 지켜보고 적는다. 저장소 읽기와 자기 작업 폴더 쓰기만 가진다 |

## 동료에게 넘기기

자기 장비로 닿지 않는 일은 그 일을 아는 동료를 호명해서 넘긴다. 같은 자리에서
상대를 부르면 관측대가 그 동료를 깨운다. 사람이 다시 입을 열기 전까지 넘기기는
몇 번으로 제한되고, 자기 자신은 부르지 않는다.

## Naming pool (unused)

- **seer/analyst**: Oracle, Augur
- **forensic**: Lantern, Tracer, Dowser
- **infrastructure/keepalive**: Pillar, Ember

## Vocabulary

| Kit concept | Generic word | Your word |
|---|---|---|
| creating a new agent | add an agent | recruit |
| creating/wiring a tool | build a tool | equip |
MD

printf '%s\n' '{"watcher": {"write_scope": ["tasks/{task}/work"], "task_allowed": false}}' \
  > .the-world/agent-scope.json

printf 'OBS_SLACK_BOT_TOKEN=\n' > env/harness.env.example
