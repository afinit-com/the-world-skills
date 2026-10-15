"""Explicit bot-token conversation checks; never an approval credential."""
from __future__ import annotations

import hmac
import os
import re
import sys

OPEN = "[as-human:"
MARKER = re.compile(r"\[as-human:[^\]\r\n]*\]")
PREFIX = re.compile(r"^\[as-human:([^\]\r\n]*)\] ")
# Shorter is guessable, and redacting it would blank ordinary words in every message.
# `say-as-human.py init` writes 43 characters.
MIN_SECRET = 16
_warned_short: set[str] = set()


def secret_env(slack) -> str:
    """Follow the main bot token's <PREFIX>_SLACK_* namespace."""
    token_env = getattr(slack, "bot_token_env", "SLACK_BOT_TOKEN")
    prefix = token_env.rpartition("SLACK_")[0]
    return prefix + "SLACK_AS_HUMAN_SECRET"


def secret_recovery(key: str) -> str:
    prefix = key.removesuffix("SLACK_AS_HUMAN_SECRET").removesuffix("_")
    command = "bin/say-as-human.py" + (f" --prefix {prefix}" if prefix else "") + " init"
    return (f"clear {key} in env/harness.env and unset it in the shell, then run "
            f"{command} and restart the harness")


def _usable(key: str, value: str) -> bool:
    """A short secret leaves the feature off, said once per key and never shown."""
    if len(value) >= MIN_SECRET:
        return True
    if key not in _warned_short:
        _warned_short.add(key)
        print(f"[world] {key} is shorter than {MIN_SECRET} characters, so as-human checks are "
              f"off; {secret_recovery(key)}", file=sys.stderr)
    return False


def configured_secret(key: str) -> str:
    """The secret under `key`, or "" when it is missing or too short to use."""
    value = os.environ.get(key, "")
    return value if value and _usable(key, value) else ""


def _secrets() -> list[str]:
    return [value for key, value in os.environ.items()
            if value and (key == "SLACK_AS_HUMAN_SECRET" or key.endswith("_SLACK_AS_HUMAN_SECRET"))
            and _usable(key, value)]


def safe_text(text: str, *, posting: bool = False) -> str:
    """Remove even an unknown marker, and bare configured secrets.

    Of an unfinished marker only the opening goes: the text after it is the person's or
    the model's, and dropping it to the end of the message lost it without a sign.

    With no secret in the environment the feature is off and input is left exactly as it
    was before it existed. A post (posting=True) still loses complete markers: a secret
    set later would otherwise meet them again as thread history.
    """
    secrets = _secrets()
    if not secrets:
        if posting:
            while MARKER.search(text):
                text = MARKER.sub("", text)
        return text
    while OPEN in text:
        text = MARKER.sub("", text).replace(OPEN, "")
    for value in secrets:
        text = text.replace(value, "[redacted]")
    return text


def safe_payload(value, *, posting: bool = False):
    """Block Kit, tool arguments and event metadata may contain nested strings."""
    if isinstance(value, str):
        return safe_text(value, posting=posting)
    if isinstance(value, dict):
        return {safe_text(k, posting=posting): safe_payload(v, posting=posting) for k, v in value.items()}
    if isinstance(value, list):
        return [safe_payload(v, posting=posting) for v in value]
    if isinstance(value, tuple):
        return tuple(safe_payload(v, posting=posting) for v in value)
    return value


def normalize_message(listener, event: dict) -> tuple[dict, bool] | None:
    """Return sanitized input + simulation flag; invalid bot markers are ignored.

    Only the socket owner's identity can impersonate the configured human. Other
    personas/apps cannot use this credential, and a real human's marker is just text.
    """
    if not _secrets():
        return event, False  # off: every message is handled as it was without the feature
    text = event.get("text") or ""
    user = event.get("user", "")
    is_bot = bool(event.get("bot_id")) or user in listener.user_to_identity
    simulated = False
    if is_bot and OPEN in text:
        match = PREFIX.match(text)
        configured = configured_secret(secret_env(listener.harness.slack))
        approver = getattr(listener.harness.slack, "approver", None)
        if (not match or not configured or not approver or user != listener.bot_user_id
                or not hmac.compare_digest(match[1].encode(), configured.encode())):
            return None
        event = {**event, "user": approver, "text": text[match.end():]}
        event.pop("bot_id", None)
        event.pop("bot_profile", None)
        simulated = True
    return safe_payload(event), simulated
