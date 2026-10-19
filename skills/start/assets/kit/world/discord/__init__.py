"""EXPERIMENTAL Discord gateway — one bot, one channel, one approver (docs/discord.md).

Two modules, split on the one line that matters for testing and for `import world`:

  gateway.py  DiscordGateway — every decision (who may talk to the bot, what a turn
              sees, who may approve, what a grant unlocks). Imports no Discord SDK.
  client.py   DiscordPyTransport — the discord.py websocket client, translated into
              gateway.py's plain dataclasses. The ONLY module that imports discord.py,
              and only when a harness configured for Discord actually starts.

Like world.slack, this package may import world.process; the reverse never happens.
"""
