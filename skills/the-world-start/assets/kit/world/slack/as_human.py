"""Explicit bot-token conversation checks; never an approval credential."""
from __future__ import annotations

import hmac
import os
import re

OPEN = "[as-human:"
MARKER = re.compile(r"\[as-human:[^\]\r\n]*\]")
PREFIX = re.compile(r"^\[as-human:([^\]\r\n]*)\] ")


def secret_env(slack) -> str:
    """Follow the main bot token's <PREFIX>_SLACK_* namespace."""
    token_env = getattr(slack, "bot_token_env", "SLACK_BOT_TOKEN")
    prefix = token_env.rpartition("SLACK_")[0]
    return prefix + "SLACK_AS_HUMAN_SECRET"


def safe_text(text: str) -> str:
    """Remove even an unknown marker, and bare configured secrets.

    Of an unfinished marker only the opening goes: the text after it is the person's or
    the model's, and dropping it to the end of the message lost it without a sign.
    """
    while OPEN in text:
        text = MARKER.sub("", text).replace(OPEN, "")
    for key, value in os.environ.items():
        if value and (key == "SLACK_AS_HUMAN_SECRET" or key.endswith("_SLACK_AS_HUMAN_SECRET")):
            text = text.replace(value, "[redacted]")
    return text


def safe_payload(value):
    """Block Kit, tool arguments and event metadata may contain nested strings."""
    if isinstance(value, str):
        return safe_text(value)
    if isinstance(value, dict):
        return {safe_text(k): safe_payload(v) for k, v in value.items()}
    if isinstance(value, list):
        return [safe_payload(v) for v in value]
    if isinstance(value, tuple):
        return tuple(safe_payload(v) for v in value)
    return value


def normalize_message(listener, event: dict) -> tuple[dict, bool] | None:
    """Return sanitized input + simulation flag; invalid bot markers are ignored.

    Only the socket owner's identity can impersonate the configured human. Other
    personas/apps cannot use this credential, and a real human's marker is just text.
    """
    text = event.get("text") or ""
    user = event.get("user", "")
    is_bot = bool(event.get("bot_id")) or user in listener.user_to_identity
    simulated = False
    if is_bot and "[as-human:" in text:
        match = PREFIX.match(text)
        configured = os.environ.get(secret_env(listener.harness.slack), "")
        approver = getattr(listener.harness.slack, "approver", None)
        if (not match or not configured or not approver or user != listener.bot_user_id
                or not hmac.compare_digest(match[1].encode(), configured.encode())):
            return None
        event = {**event, "user": approver, "text": text[match.end():]}
        event.pop("bot_id", None)
        event.pop("bot_profile", None)
        simulated = True
    return safe_payload(event), simulated
