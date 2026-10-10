"""discord.py → DiscordGateway's plain events. The only module in world that imports
discord.py, and it is imported only when a Discord-configured harness starts.

Gateway websocket, not an Interactions Endpoint URL: button clicks arrive over the same
connection as messages (INTERACTION_CREATE), so no public URL is needed — the Discord
counterpart of Slack's Socket Mode.

Everything that decides anything lives in gateway.py. This file only translates, and the
translation is where the trust boundary is drawn:
  - InboundMessage.mentions_bot comes from message.mentions (Discord's resolved list),
    not from searching the text for an id.
  - ButtonClick.user_id comes from interaction.user — set by Discord for the human who
    clicked. Nothing in a message body can put a value there.
  - Outbound messages never ping @everyone/@here or roles, whatever a model writes.

A persona other than the bot posts through one webhook per channel that this bot created
(name "the-world"), with that persona's name and avatar on each message. A webhook the bot
cannot create or use (no Manage Webhooks) is reported once and the message goes out as the
bot instead, so a missing permission costs the name, never the message.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from typing import Awaitable, Callable

import discord

from .gateway import ButtonClick, HistoryLine, InboundMessage

_STYLES = {"success": discord.ButtonStyle.success, "danger": discord.ButtonStyle.danger}
_NO_PINGS = discord.AllowedMentions(everyone=False, roles=False, users=True)
WEBHOOK_NAME = "the-world"


class DiscordPyTransport:
    def __init__(
        self,
        token: str,
        *,
        on_ready: Callable[[str], Awaitable[None]],
        on_message: Callable[[InboundMessage], Awaitable[None]],
        on_click: Callable[[ButtonClick], Awaitable[None]],
        log: Callable[[str], None],
    ) -> None:
        self._token = token
        intents = discord.Intents.default()
        # Privileged. Without it, mention turns still work (Discord delivers the content of
        # a message that mentions the bot) but thread follow-ups and history come back
        # empty. bin/discord-setup.py checks whether the portal toggle is on.
        intents.message_content = True
        self.client = discord.Client(
            intents=intents,
            allowed_mentions=_NO_PINGS,
        )
        self._log = log
        self._webhooks: dict[int, discord.Webhook] = {}  # parent channel id → our webhook
        # Channels where Discord refused the webhook (no Manage Webhooks). Not retried per
        # message — that would be one more 403 for every post — only after a restart.
        self._no_webhook: set[int] = set()
        self._webhook_warned = False
        client = self.client
        # Captured under other names first: discord.py registers a handler by its
        # __name__, so the handlers below must be called on_ready/on_message/... and
        # would otherwise shadow these parameters.
        on_ready_cb, on_message_cb, on_click_cb = on_ready, on_message, on_click

        @client.event
        async def on_ready() -> None:
            await on_ready_cb(str(client.user.id))

        @client.event
        async def on_message(message: discord.Message) -> None:
            await on_message_cb(self._inbound(message))

        @client.event
        async def on_interaction(interaction: discord.Interaction) -> None:
            if interaction.type != discord.InteractionType.component:
                return
            custom_id = (interaction.data or {}).get("custom_id", "")
            if not custom_id.startswith("world_") or interaction.message is None:
                return
            await on_click_cb(self._click(interaction, custom_id))

    def _inbound(self, message: discord.Message) -> InboundMessage:
        me = self.client.user
        channel = message.channel
        content = message.content or ""
        # Discord gives every invited bot a managed role with the bot's name, and the
        # @-autocomplete offers it right next to the bot. Picking it means the bot. Only a
        # role Discord itself tags with this bot's id counts — never one matched by name.
        own_roles = [
            r for r in getattr(message, "role_mentions", None) or []
            if me is not None and getattr(r.tags, "bot_id", None) == me.id
        ]
        if me is not None:
            content = re.sub(rf"<@!?{me.id}>", "", content)
        for role in own_roles:
            content = content.replace(f"<@&{role.id}>", "")
        content = content.strip()
        return InboundMessage(
            id=str(message.id),
            channel_id=str(channel.id),
            parent_id=str(channel.parent_id) if isinstance(channel, discord.Thread) else None,
            author_id=str(message.author.id),
            author_is_bot=bool(message.author.bot or message.webhook_id),
            mentions_bot=me is not None and (
                any(u.id == me.id for u in message.mentions) or bool(own_roles)
            ),
            content=content,
        )

    @staticmethod
    def _click(interaction: discord.Interaction, custom_id: str) -> ButtonClick:
        original = interaction.message.content or ""

        async def ephemeral(text: str) -> None:
            await interaction.response.send_message(text, ephemeral=True)

        async def resolve(line: str) -> None:
            # Buttons gone, audit line kept — a decided ask cannot be clicked again.
            await interaction.response.edit_message(content=f"{original}\n{line}"[:2000], view=None)

        return ButtonClick(
            custom_id=custom_id,
            message_id=str(interaction.message.id),
            channel_id=str(interaction.channel_id),
            user_id=str(interaction.user.id),
            ephemeral=ephemeral,
            resolve=resolve,
        )

    async def _channel(self, channel_id: str):
        cid = int(channel_id)
        return self.client.get_channel(cid) or await self.client.fetch_channel(cid)

    async def run(self, stop: asyncio.Event) -> None:
        runner = asyncio.create_task(self.client.start(self._token))
        stopper = asyncio.create_task(stop.wait())
        done, _ = await asyncio.wait({runner, stopper}, return_when=asyncio.FIRST_COMPLETED)
        await self.client.close()
        stopper.cancel()
        if runner in done:
            runner.result()  # surface a login/intent failure instead of idling silently
        else:
            with contextlib.suppress(asyncio.CancelledError, Exception):  # noqa: BLE001
                await runner

    async def send(
        self, channel_id: str, text: str, *, buttons: list[tuple[str, str, str]] | None = None,
        as_persona: tuple[str, str] | None = None,
    ) -> str:
        channel = await self._channel(channel_id)
        if as_persona and not buttons:
            mid = await self._send_as(channel, text, *as_persona)
            if mid:
                return mid
        view = None
        if buttons:
            view = discord.ui.View(timeout=None)
            for custom_id, label, style in buttons:
                view.add_item(discord.ui.Button(
                    label=label, custom_id=custom_id,
                    style=_STYLES.get(style, discord.ButtonStyle.secondary),
                ))
        message = await channel.send(text, view=view) if view else await channel.send(text)
        return str(message.id)

    async def _send_as(self, channel, text: str, name: str, avatar: str) -> str | None:
        """Post `text` under `name`/`avatar` through this bot's webhook on the channel (or
        on a thread's parent, into the thread). None when that is not possible."""
        parent = channel.parent if isinstance(channel, discord.Thread) else channel
        if getattr(parent, "id", None) in self._no_webhook:
            return None
        try:
            webhook = self._webhooks.get(parent.id)
            if webhook is None:
                me = self.client.user
                webhook = next((w for w in await parent.webhooks()
                                if me is not None and w.user is not None and w.user.id == me.id
                                and w.name == WEBHOOK_NAME), None)
                webhook = webhook or await parent.create_webhook(name=WEBHOOK_NAME)
                self._webhooks[parent.id] = webhook
            kwargs = {"thread": channel} if isinstance(channel, discord.Thread) else {}
            if avatar:
                kwargs["avatar_url"] = avatar
            message = await webhook.send(text, username=name[:80], wait=True,
                                         allowed_mentions=_NO_PINGS, **kwargs)
            return str(message.id)
        except (discord.Forbidden, discord.HTTPException, AttributeError) as exc:
            self._webhooks.pop(getattr(parent, "id", 0), None)
            if isinstance(exc, discord.Forbidden):
                self._no_webhook.add(getattr(parent, "id", 0))
            if not self._webhook_warned:
                self._webhook_warned = True
                self._log(f"discord: cannot post as {name} through a webhook ({exc!r}); "
                          "posting as the bot. Grant the bot Manage Webhooks to fix.")
            return None

    async def create_thread(self, channel_id: str, message_id: str, name: str) -> str:
        channel = await self._channel(channel_id)
        thread = await channel.get_partial_message(int(message_id)).create_thread(
            name=(name or "thread")[:100], auto_archive_duration=1440,
        )
        return str(thread.id)

    @contextlib.asynccontextmanager
    async def typing(self, channel_id: str):
        # discord.py re-sends the typing trigger every few seconds while the block runs;
        # Discord drops it on its own ~10s after the last one, or when we post.
        channel = await self._channel(channel_id)
        async with channel.typing():
            yield

    async def history(self, channel_id: str, limit: int) -> list[HistoryLine]:
        channel = await self._channel(channel_id)
        me = self.client.user
        lines = [
            HistoryLine(str(m.author.id), me is not None and m.author.id == me.id, m.content or "")
            async for m in channel.history(limit=limit)
        ]
        lines.reverse()  # history() is newest-first
        return lines


__all__ = ["DiscordPyTransport"]
