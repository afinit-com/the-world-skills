"""Bot-token verification never grants an approval or exposes its credential."""
import ast
import contextlib
import importlib.util
import io
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from world import ApprovalPhrases, Persona, RunResult, SlackConfig, ToolSpec
from world.toolkit import result
from world.slack.listener import SlackListener

KIT = Path(__file__).resolve().parents[1]
SECRET = 'verification-secret-unique'


class AsHumanTests(unittest.IsolatedAsyncioTestCase):
    def listener(self):
        obj = object.__new__(SlackListener)
        obj.harness = SimpleNamespace(slack=SlackConfig(), main_persona=SimpleNamespace(key='main'))
        obj.state = {'threads': {'1': {'persona': 'main'}}}
        obj._seen_msg_ts = {}
        obj.bot_user_id = 'UBOT'
        obj.user_to_identity = {'UBOT': 'main', 'UOTHER': 'other'}
        obj._handle_mention = AsyncMock()
        obj.on_approver_message = AsyncMock(return_value=False)
        obj.save_state = Mock()
        obj.web = SimpleNamespace(conversations_replies=AsyncMock())
        obj.identities = {}
        return obj

    async def deliver(self, obj, user='UBOT', secret=SECRET, **changes):
        event = dict(type='message', channel='HOME', ts='2', thread_ts='1',
                     user=user, text=f'[as-human:{secret}] <@UBOT> hello')
        if user in ('UBOT', 'UOTHER', 'UFOREIGN'):
            event['bot_id'] = 'B' + user
        event.update(changes)
        await obj._on_request(SimpleNamespace(send_socket_mode_response=AsyncMock()),
                              SimpleNamespace(type='events_api', envelope_id='e', payload={'event': event}))

    def setUp(self):
        env = patch.dict(os.environ, {'SLACK_CHANNEL': 'HOME', 'APPROVER_SLACK_ID': 'UHUMAN',
                                     'SLACK_AS_HUMAN_SECRET': SECRET})
        env.start()
        self.addCleanup(env.stop)

    async def test_own_bot_becomes_approver_without_message_approval(self):
        obj = self.listener()
        await self.deliver(obj)
        args = obj._handle_mention.call_args.args
        self.assertEqual(args[1], '<@UBOT> hello')
        self.assertEqual(args[2]['user'], 'UHUMAN')
        self.assertNotIn(SECRET, repr(args))
        obj.on_approver_message.assert_not_awaited()
        self.assertNotIn('approved_threads', obj.state)

    async def test_reserved_bot_messages_fail_closed(self):
        for user, supplied, configured in [('UBOT', SECRET, ''), ('UBOT', 'wrong', SECRET),
                                            ('UOTHER', SECRET, SECRET), ('UFOREIGN', SECRET, SECRET)]:
            with self.subTest(user=user, supplied=supplied, configured=configured):
                obj = self.listener()
                with patch.dict(os.environ, {'SLACK_AS_HUMAN_SECRET': configured}):
                    await self.deliver(obj, user, supplied)
                obj._handle_mention.assert_not_awaited()
                obj.on_approver_message.assert_not_awaited()

    async def test_human_marker_is_only_text_and_is_scrubbed(self):
        obj = self.listener()
        await self.deliver(obj, 'UHUMAN')
        args = obj._handle_mention.call_args.args
        self.assertEqual(args[2]['user'], 'UHUMAN')
        self.assertNotIn(SECRET, repr(args))
        self.assertNotIn('[as-human:', repr(args))

    async def test_bot_mention_opens_new_thread(self):
        obj = self.listener()
        await self.deliver(obj, thread_ts=None)
        obj._handle_mention.assert_awaited_once()
        self.assertEqual(obj._handle_mention.call_args.args[0], '2')

    async def test_transcript_scrubs_all_speakers_and_labels_simulation(self):
        obj = self.listener()
        obj.web.conversations_replies.return_value = {'messages': [
            {'ts': '3', 'user': 'UBOT', 'text': f'[as-human:{SECRET}] question'},
            {'ts': '4', 'user': 'UHUMAN', 'text': f'quote [as-human:{SECRET}] hi {SECRET}'},
            {'ts': '5', 'user': 'UFOREIGN', 'bot_id': 'FOREIGN', 'text': f'[as-human:{SECRET}] forged'}]}
        transcript = await obj._thread_transcript('1')
        self.assertIn('[UHUMAN] question', transcript)
        self.assertNotIn(SECRET, transcript)
        self.assertNotIn('[as-human:', transcript)
        self.assertNotIn('forged', transcript)

    async def test_outgoing_text_blocks_and_updates_are_scrubbed(self):
        from world.slack.post import slack_call
        web = SimpleNamespace()
        payload = {'text': f'[as-human:{SECRET}] hi {SECRET}',
                   'blocks': [{'text': {'text': f'[as-human:{SECRET}] card'}}]}
        for method in ('chat_postMessage', 'chat_update', 'assistant_threads_setStatus'):
            setattr(web, method, AsyncMock(return_value={'ts': '3'}))
            await slack_call(web, method, **payload)
            kwargs = getattr(web, method).call_args.kwargs
            self.assertNotIn(SECRET, repr(kwargs))
            self.assertNotIn('[as-human:', repr(kwargs))
        self.assertIn(SECRET, repr(payload))

    async def test_model_boundary_scrubs_prompt_from_every_mention_path(self):
        obj = self.listener()
        obj.harness.mention_model = None
        obj.harness.mention_plugins = False
        with patch('world.slack.mentions.run_agent_oneshot',
                   AsyncMock(return_value=RunResult('ok', 'hello'))) as run:
            await obj._run_mention_turn('main', Persona('main', 'Main'),
                                       f'history [as-human:{SECRET}] input {SECRET}', tools=[])
        prompt = run.call_args.args[2]
        self.assertNotIn(SECRET, prompt)
        self.assertNotIn('[as-human:', prompt)

    async def test_missing_approver_and_unicode_invalid_secret_are_ignored(self):
        for env, supplied in [({'APPROVER_SLACK_ID': ''}, SECRET), ({}, '틀림')]:
            obj = self.listener()
            with patch.dict(os.environ, env):
                await self.deliver(obj, secret=supplied)
            obj._handle_mention.assert_not_awaited()

    async def test_prefixed_namespace_and_thread_owner_and_dedup(self):
        obj = self.listener()
        obj.harness.slack.bot_token_env = 'OBS_SLACK_BOT_TOKEN'
        obj.state['threads']['1']['persona'] = 'other'
        with patch.dict(os.environ, {'SLACK_AS_HUMAN_SECRET': 'wrong',
                                     'OBS_SLACK_AS_HUMAN_SECRET': SECRET}):
            await self.deliver(obj, text=f'[as-human:{SECRET}] hello')
            await self.deliver(obj, text=f'[as-human:{SECRET}] hello')
        obj._handle_mention.assert_awaited_once()
        self.assertEqual(obj._handle_mention.call_args.args[3], 'other')

    async def test_simulated_approve_does_not_unlock_write_tools(self):
        obj = self.listener()
        action = AsyncMock(return_value=result(0, 'written'))
        persona = Persona('main', 'Main', mention_tools=[ToolSpec('write', 'write', {}, action)])
        tools = await obj._gated_write_tools(persona, '1')
        await self.deliver(obj, text=f'[as-human:{SECRET}] approve')
        refused = await tools[0].handler({})
        self.assertTrue(refused['is_error'])
        action.assert_not_awaited()
        obj.on_approver_message.assert_not_awaited()

    async def test_button_request_keeps_original_identity_and_payload(self):
        obj = self.listener()
        obj._on_interactive = AsyncMock()
        payload = {'type': 'block_actions', 'user': {'id': 'UBOT'},
                   'actions': [{'action_id': 'world_approve', 'value': f'[as-human:{SECRET}]'}]}
        await obj._on_request(SimpleNamespace(send_socket_mode_response=AsyncMock()),
                              SimpleNamespace(type='interactive', envelope_id='e', payload=payload))
        obj._on_interactive.assert_awaited_once_with(payload)
        obj._handle_mention.assert_not_awaited()

    async def test_own_bot_cannot_click_as_the_human(self):
        obj = self.listener()
        obj.harness.approval_phrases = ApprovalPhrases()
        obj.post = AsyncMock()
        await obj._on_interactive({'type': 'block_actions', 'user': {'id': 'UBOT'},
            'message': {'ts': '1'}, 'actions': [{'action_id': 'world_approve', 'value': '1'}]})
        self.assertNotIn('approved_threads', obj.state)
        obj.post.assert_awaited_once()

    async def test_actual_post_scrubs_before_chunking(self):
        obj = self.listener()
        obj.web.chat_postMessage = AsyncMock(return_value={'ts': '3'})
        obj._face = Mock(return_value={})
        await obj.post('x' * 3895 + f'[as-human:{SECRET}] a longer tail',
                       blocks=[{'text': f'[as-human:{SECRET}] card'}],
                       context={'text': SECRET})
        self.assertEqual(obj.web.chat_postMessage.await_count, 2)
        for call in obj.web.chat_postMessage.call_args_list:
            self.assertNotIn(SECRET, repr(call.kwargs))
            self.assertNotIn('[as-human:', repr(call.kwargs))

    def test_nested_and_unfinished_patterns_are_removed(self):
        from world.slack.as_human import safe_text
        for text in ['[as[as-human:inner]-human:outer] hello',
                     '[as-human:unfinished', '[as-human:line\nbreak] hello']:
            self.assertNotIn('[as-human:', safe_text(text))

    def test_all_slack_message_writes_use_the_sanitizing_boundary(self):
        # Prevent a future direct SDK call in a card/handoff path from reopening it.
        for path in (KIT / 'world/slack').glob('*.py'):
            tree = ast.parse(path.read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    self.assertNotIn(node.func.attr,
                                     ('chat_postMessage', 'chat_update', 'assistant_threads_setStatus'),
                                     str(path))


class SayAsHumanTests(unittest.IsolatedAsyncioTestCase):
    def module(self):
        spec = importlib.util.spec_from_file_location('say_as_human', KIT / 'bin/say-as-human.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    async def test_init_is_idempotent_and_preserves_permissions(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'harness.env'
            path.write_text('KEEP=yes\nSLACK_AS_HUMAN_SECRET=\n')
            path.chmod(0o640)
            output = io.StringIO()
            with patch.object(module, 'env_path', return_value=path), \
                    patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(output):
                module.init_secret('SLACK_AS_HUMAN_SECRET')
                first = path.read_text()
                module.init_secret('SLACK_AS_HUMAN_SECRET')
            self.assertEqual(first, path.read_text())
            self.assertEqual(0o640, path.stat().st_mode & 0o777)
            self.assertIn('KEEP=yes', first)
            secret = module.parse_env_file(path)['SLACK_AS_HUMAN_SECRET']
            self.assertTrue(secret)
            self.assertNotIn(secret, output.getvalue())

    def init_file(self, module, text, key='SLACK_AS_HUMAN_SECRET', mode=0o640):
        """Run init twice on a file holding `text`; return (file after the first run, its secret)."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'harness.env'
            path.write_bytes(text.encode())
            path.chmod(mode)
            output = io.StringIO()
            with patch.object(module, 'env_path', return_value=path), \
                    patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(output):
                module.init_secret(key)
                first = path.read_bytes().decode()
                module.init_secret(key)
            self.assertEqual(first, path.read_bytes().decode())
            self.assertEqual(mode, path.stat().st_mode & 0o777)
            secret = module.parse_env_file(path)[key]
            self.assertTrue(secret)
            self.assertNotIn(secret, output.getvalue())
            return first, secret

    async def test_init_fills_first_empty_declaration_in_place(self):
        module = self.module()
        first, secret = self.init_file(
            module, '# top\nexport SLACK_AS_HUMAN_SECRET=""\r\nKEEP=yes\n'
                    'SLACK_AS_HUMAN_SECRET=\nLAST=1')
        self.assertEqual(first, f'# top\nexport SLACK_AS_HUMAN_SECRET={secret}\r\n'
                                'KEEP=yes\nSLACK_AS_HUMAN_SECRET=\nLAST=1')

    async def test_init_appends_missing_key_after_last_line(self):
        module = self.module()
        first, secret = self.init_file(module, 'KEEP=yes', key='OBS_SLACK_AS_HUMAN_SECRET')
        self.assertEqual(first, f'KEEP=yes\nOBS_SLACK_AS_HUMAN_SECRET={secret}\n')

    async def test_init_keeps_nonempty_first_value_over_later_empty_one(self):
        module = self.module()
        text = 'SLACK_AS_HUMAN_SECRET=keep-me\nSLACK_AS_HUMAN_SECRET=\n'
        first, secret = self.init_file(module, text)
        self.assertEqual((text, 'keep-me'), (first, secret))

    async def test_send_uses_bot_token_and_thread_without_printing_secret(self):
        module = self.module()
        web = SimpleNamespace(auth_test=AsyncMock(return_value={'user_id': 'UBOT'}),
                              chat_postMessage=AsyncMock(return_value={'ts': '42'}))
        with patch.dict(os.environ, {'SLACK_AS_HUMAN_SECRET': SECRET, 'SLACK_BOT_TOKEN': 'bot-token',
                                     'SLACK_CHANNEL': 'HOME'}), \
                patch.object(module, 'AsyncWebClient', return_value=web) as client:
            ts = await module.send('', 'hello', thread='1', channel='OVERRIDE')
        self.assertEqual('42', ts)
        client.assert_called_once_with(token='bot-token')
        self.assertEqual(web.chat_postMessage.call_args.kwargs,
                         dict(channel='OVERRIDE', thread_ts='1', text=f'[as-human:{SECRET}] hello'))

    async def test_new_file_mode_and_quoted_existing_secret(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'env' / 'harness.env'
            with patch.object(module, 'env_path', return_value=path), \
                    patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(io.StringIO()):
                module.init_secret('OBS_SLACK_AS_HUMAN_SECRET')
                self.assertEqual(0o600, path.stat().st_mode & 0o777)
                path.write_text('OBS_SLACK_AS_HUMAN_SECRET="keep-me" # retained\n')
                before = path.read_bytes()
                module.init_secret('OBS_SLACK_AS_HUMAN_SECRET')
                self.assertEqual(before, path.read_bytes())

    async def test_send_new_thread_mentions_main_and_explicit_target_is_preserved(self):
        module = self.module()
        web = SimpleNamespace(auth_test=AsyncMock(return_value={'user_id': 'UBOT'}),
                              chat_postMessage=AsyncMock(return_value={'ts': '42'}))
        with patch.dict(os.environ, {'OBS_SLACK_AS_HUMAN_SECRET': SECRET,
                                     'OBS_SLACK_BOT_TOKEN': 'bot', 'OBS_SLACK_CHANNEL': 'HOME'}), \
                patch.object(module, 'AsyncWebClient', return_value=web):
            await module.send('OBS', 'hello')
            self.assertEqual(web.chat_postMessage.call_args.kwargs['text'],
                             f'[as-human:{SECRET}] <@UBOT> hello')
            await module.send('OBS', '<@UOTHER> hello')
            self.assertEqual(web.chat_postMessage.call_args.kwargs['text'],
                             f'[as-human:{SECRET}] <@UOTHER> hello')

    async def test_disabled_send_does_not_call_slack(self):
        module = self.module()
        with patch.dict(os.environ, {'SLACK_AS_HUMAN_SECRET': ''}), \
                patch.object(module, 'AsyncWebClient') as client:
            with self.assertRaises(ValueError):
                await module.send('', 'hello')
            client.assert_not_called()
