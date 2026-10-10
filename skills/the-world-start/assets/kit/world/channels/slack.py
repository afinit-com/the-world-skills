"""Slack implementation of the conversation gateway."""

from ..slack.listener import SlackListener


class SlackGateway(SlackListener):
    """Compatibility name for the existing, feature-complete Slack listener."""


__all__ = ["SlackGateway"]
