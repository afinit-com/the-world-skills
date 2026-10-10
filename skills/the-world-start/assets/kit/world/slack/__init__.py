"""The Slack bots: one socket, N posting personas, approval buttons, mentions.

May import world.process (a mention turn spawns an agent and records its usage).
The reverse must never happen — that direction is what world.bridge exists to
keep open, and it is why bridge.py and harness.py live above both packages
rather than inside one.
"""
