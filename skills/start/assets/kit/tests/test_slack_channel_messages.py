"""SlackConfig.respond_to_channel_messages: a plain message in the home channel reaches
the main persona without an @-mention; relay channels, bots and edits still do not."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from world.slack.listener import SlackListener


class ChannelMessageTests(unittest.IsolatedAsyncioTestCase):
    def listener(self, enabled=True):
        obj = object.__new__(SlackListener)
        obj.harness = SimpleNamespace(
            main_persona=SimpleNamespace(key='lumen'),
            slack=SimpleNamespace(channel='HOME', relay_channels=['RELAY'],
                respond_to_channel_messages=enabled, escalate_to=None,
                co_approver=None, is_approver=lambda user: user == 'HUMAN'))
        obj.state = {'threads': {}}
        obj._seen_msg_ts = {}
        obj.bot_user_id = 'UBOT'
        obj.user_to_identity = {'UBOT': 'lumen'}
        obj._handle_mention = AsyncMock()
        obj._handle_relay_mention = AsyncMock()
        obj.on_approver_message = AsyncMock(return_value=False)
        return obj

    async def deliver(self, obj, **changes):
        event = {'type': 'message', 'channel': 'HOME', 'user': 'HUMAN',
                 'text': '사이트 괜찮아?', 'ts': '123.1'}
        event.update(changes)
        await obj._on_request(SimpleNamespace(send_socket_mode_response=AsyncMock()),
            SimpleNamespace(type='events_api', envelope_id='event', payload={'event': event}))

    async def test_plain_message_routes_to_main_once(self):
        obj = self.listener()
        await self.deliver(obj)
        await self.deliver(obj, type='app_mention', text='<@UBOT> 사이트 괜찮아?')
        obj._handle_mention.assert_awaited_once()
        self.assertEqual(obj._handle_mention.call_args.args[3], 'lumen')

    async def test_default_still_requires_mention(self):
        obj = self.listener(False)
        await self.deliver(obj)
        obj._handle_mention.assert_not_awaited()
        await self.deliver(obj, type='app_mention', text='<@UBOT> 안녕')
        obj._handle_mention.assert_awaited_once()

    async def test_other_channels_and_bots_and_nonmessages_ignored(self):
        for changes in [{'channel': 'OTHER'}, {'channel': 'RELAY'},
                        {'user': 'UBOT', 'bot_id': 'OWN'}, {'user': 'EXTERNAL', 'bot_id': 'OTHER'},
                        {'text': ''}, {'user': ''}, {'subtype': 'message_changed'}]:
            with self.subTest(changes=changes):
                obj = self.listener()
                await self.deliver(obj, **changes)
                obj._handle_mention.assert_not_awaited()
                obj._handle_relay_mention.assert_not_awaited()

    async def test_a_message_addressed_to_someone_else_is_left_alone(self):
        obj = self.listener()
        await self.deliver(obj, text='<@UCOLLEAGUE> 이거 봐줘')
        obj._handle_mention.assert_not_awaited()

    async def test_existing_thread_keeps_owner(self):
        obj = self.listener()
        obj.state['threads']['111'] = {'persona': 'scout', 'bot_hops': 3}
        await self.deliver(obj, thread_ts='111')
        self.assertEqual(obj._handle_mention.call_args.args[3], 'scout')
        self.assertNotIn('bot_hops', obj.state['threads']['111'])
