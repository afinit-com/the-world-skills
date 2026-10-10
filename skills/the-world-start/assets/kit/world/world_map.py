"""Export the declared cast and tools without running agents or tool factories."""

from __future__ import annotations

import base64
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .types import MentionTrigger, PollTrigger, ScheduleTrigger


def _portrait(home: Path, filename: str) -> str:
    if not filename:
        return ""
    path = (home / filename).resolve()
    if not path.is_relative_to(home.resolve()):
        raise ValueError("Portrait must be inside the harness directory")
    mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".webp": "image/webp"}.get(path.suffix.lower())
    if mime is None or path.stat().st_size > 2_000_000:
        raise ValueError("Portrait must be a PNG, JPEG, or WebP under 2 MB")
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def _tool_group(label: str, tools) -> dict:
    if callable(tools):
        return {"label": label, "dynamic": True, "tools": []}
    return {
        "label": label,
        "dynamic": False,
        "tools": [
            {"name": getattr(tool, "name", type(tool).__name__),
             "description": getattr(tool, "description", ""),
             "write_roots": list(getattr(tool, "write_roots", ()))}
            for tool in tools
        ],
    }


def _runs(trigger) -> str:
    """When the job runs, in the words STATUS.md uses, so the two pages agree."""
    if isinstance(trigger, ScheduleTrigger):
        sched = trigger.schedule
        at = f"{sched.hour:02d}:{sched.minute:02d} ({sched.tz})"
        if sched.kind == "weekly":
            return f"매주 {'월화수목금토일'[sched.weekday or 0]}요일 {at}"
        if sched.kind == "monthly":
            return f"매월 {sched.day or 1}일 {at}"
        return f"매일 {at}"
    if isinstance(trigger, MentionTrigger):
        return "멘션 또는 넘겨받을 때"
    if isinstance(trigger, PollTrigger):
        return "변화가 생길 때"
    return "조건이 맞으면"


def world_map_data(harness) -> dict:
    personas = harness.personas
    relationships = []
    for relation in harness.relationships:
        if relation.source not in personas or relation.target not in personas:
            raise ValueError(f"Unknown persona in relationship: {relation.source} → {relation.target}")
        if relation.source == relation.target or not relation.label.strip():
            raise ValueError("A relationship needs distinct personas and a non-empty label")
        relationships.append(asdict(relation))
    characters = []
    for key, persona in personas.items():
        jobs = []
        for agent in harness.agents:
            if harness.persona_for_agent(agent.name).key != key:
                continue
            jobs.append({
                "name": agent.name, "trigger": _runs(agent.trigger),
                "watches": list(agent.watches), "mutates": agent.mutates,
                "write_scope": list(agent.write_scope),
                "tools": _tool_group("작업에 등록된 tool", agent.tools),
                "extensions": {"shell": agent.allow_bash, "subagents": agent.task_allowed,
                               "plugins": agent.allow_plugins},
            })
        characters.append({
            "key": key, "name": persona.display, "flavor": persona.flavor,
            "portrait": _portrait(harness.home, persona.portrait),
            "main": key == harness.main_persona.key, "jobs": jobs,
            "tool_groups": [
                _tool_group("대화할 때 · 모두에게 공통", harness.mention_tools),
                _tool_group("대화할 때 · 이 캐릭터 전용 · 승인 필요", persona.mention_tools),
                _tool_group("대화할 때 · 이 스레드 전용", persona.thread_tools),
            ],
            "conversation_plugins": bool(harness.plugins) and harness.mention_plugins,
        })
    return {"name": harness.name, "generated_at": datetime.now(timezone.utc).isoformat(),
            "characters": characters, "relationships": relationships}


def write_world_map(harness, destination: Path) -> Path:
    data = json.dumps(world_map_data(harness), ensure_ascii=False).replace("<", "\\u003c")
    template = Path(__file__).with_name("world_map.html").read_text(encoding="utf-8")
    rendered = template.replace("__WORLD_DATA__", data)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(rendered, encoding="utf-8")
    return destination
