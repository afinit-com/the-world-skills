"""The choices this harness was installed for, read from `project-profile.json`.

install-kit.py writes that file beside `world/` and copies only what the profile needs:
a Codex-only harness carries no Claude runner, a Slack-only one no Discord gateway. This
module is how the runtime learns which of those it has, so a mismatch is an error naming
the profile rather than an ImportError from a file the installer left out on purpose.

No file (the kit's own checkout, its tests, an older harness) means everything is
present, which was true of every install before profiles existed.

    {"schema_version": 1, "os": "windows", "provider": "codex", "channel": "slack"}
"""

from __future__ import annotations

import json
from pathlib import Path

PROFILE_PATH = Path(__file__).resolve().parents[1] / "project-profile.json"


def _load() -> dict:
    try:
        return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f"{PROFILE_PATH} is unreadable ({exc}); fix it or re-run install-kit.py"
        ) from exc


PROFILE: dict = _load()
OS: str = PROFILE.get("os", "any")
_provider = PROFILE.get("provider", "both")
SUPPORTED_PROVIDERS: tuple[str, ...] = ("claude", "codex") if _provider == "both" else (_provider,)
DEFAULT_PROVIDER: str = "codex" if SUPPORTED_PROVIDERS == ("codex",) else "claude"
# "slack", "discord", "both", or "none" (poll and schedule only, no conversation channel).
CHANNEL: str = PROFILE.get("channel", "both")


def reinstall_hint(**choice: str) -> str:
    flags = " ".join(f"--{k} {v}" for k, v in choice.items())
    return (
        f"this harness was installed for {PROFILE_PATH.name} = {json.dumps(PROFILE)}; "
        f"re-run install-kit.py with {flags} to add it"
    )
