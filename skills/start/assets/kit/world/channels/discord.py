"""EXPERIMENTAL Discord implementation of the conversation gateway (docs/discord.md).

Importing this module does not import discord.py; starting the gateway does.
"""

from ..discord.gateway import UNSUPPORTED, DiscordGateway

__all__ = ["DiscordGateway", "UNSUPPORTED"]
