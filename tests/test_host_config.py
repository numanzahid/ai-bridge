import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ResourceNotFoundError

from ai_bridge.host_config import HostConfig, initialize


class HostConfigTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def manifest(self, name, entries):
        (self.root / name).write_text(json.dumps(entries))

    def configured(self):
        initialize(self.root)
        host = HostConfig(self.root)
        server = MCPServer('host-test', instructions=host.instructions)
        host.register(server, lambda text: text.replace('SECRET', '[REDACTED]'))
        return host, server

    async def test_missing_overrides_keep_defaults_and_init_preserves_edits(self):
        host = HostConfig(self.root)
        self.assertIn('list_instructions', host.instructions)
        self.assertEqual(host.resources, [])
        initialize(self.root)
        custom = self.root / 'server-instructions.md'
        custom.write_text('User edited guidance')
        initialize(self.root)
        self.assertEqual(HostConfig(self.root).instructions, 'User edited guidance')
        custom.write_text('')
        self.assertEqual(HostConfig(self.root).instructions, '')

    async def test_tool_description_changes_metadata_but_keeps_arguments(self):
        folder = self.root / 'tool-descriptions'
        folder.mkdir()
        (folder / 'echo.md').write_text('Custom echo guidance')
        host = HostConfig(self.root)
        server = MCPServer('host-test')

        @host.tool(server)
        def echo(value: str) -> str:
            """Built-in description."""
            return value

        @host.tool(server)
        def other() -> str:
            """Keep this description."""
            return 'ok'

        host.register(server, lambda text: text)
        tools = {tool.name: tool for tool in await server.list_tools()}
        self.assertEqual(tools['echo'].description, 'Custom echo guidance')
        self.assertIn('value', tools['echo'].input_schema['properties'])
        self.assertEqual(tools['other'].description, 'Keep this description.')
        self.assertEqual(echo('same behavior'), 'same behavior')

    async def test_listing_does_not_read_resource_or_prompt_bodies(self):
        initialize(self.root)
        original = Path.read_text

        def metadata_only(path, *args, **kwargs):
            if path.parent.name in ('resources', 'prompts'):
                raise AssertionError('Body read before explicit retrieval')
            return original(path, *args, **kwargs)

        with patch.object(Path, 'read_text', metadata_only):
            host = HostConfig(self.root)
            server = MCPServer('host-test')
            host.register(server, lambda text: text)
            self.assertEqual(len(await server.list_resources()), 1)
            self.assertEqual(len(await server.list_prompts()), 1)

    async def test_resource_reads_are_live_and_redacted_and_unknown_uri_fails(self):
        host, server = self.configured()
        resource = host.resources[0]
        path = host.file(resource.path)
        path.write_text('First resource SECRET')
        contents = list(await server.read_resource(resource.uri))
        self.assertEqual(contents[0].content, 'First resource [REDACTED]')
        path.write_text('Updated resource')
        self.assertEqual(list(await server.read_resource(resource.uri))[0].content, 'Updated resource')
        with self.assertRaises(ResourceNotFoundError):
            await server.read_resource('ai-bridge://unlisted')

    async def test_prompt_messages_substitute_strings_without_evaluation(self):
        (self.root / 'user.md').write_text('Task: {{task}}; optional={{note}}; literal={"key":1}')
        (self.root / 'assistant.md').write_text('Acknowledged {{task}}')
        self.manifest('prompts.json', [{
            'name': 'review',
            'arguments': [{'name': 'task', 'required': True}, {'name': 'note'}],
            'messages': [{'role': 'user', 'path': 'user.md'}, {'role': 'assistant', 'path': 'assistant.md'}],
        }])
        host = HostConfig(self.root)
        server = MCPServer('host-test')
        host.register(server, lambda text: text.replace('SECRET', '[REDACTED]'))
        result = await server.get_prompt('review', {'task': 'SECRET {{not_evaluated}}'})
        self.assertEqual(result.messages[0].role, 'user')
        self.assertEqual(result.messages[1].role, 'assistant')
        self.assertIn('[REDACTED] {{not_evaluated}}', result.messages[0].content.text)
        self.assertIn('optional=; literal={"key":1}', result.messages[0].content.text)
        for arguments in ({}, {'task': 'ok', 'unknown': 'value'}):
            with self.assertRaises(ValueError):
                await server.get_prompt('review', arguments)
        (self.root / 'user.md').write_text('Changed {{task}}')
        result = await server.get_prompt('review', {'task': 'now'})
        self.assertEqual(result.messages[0].content.text, 'Changed now')
        (self.root / 'user.md').write_text('Unknown {{undeclared}}')
        with self.assertRaises(ValueError):
            await server.get_prompt('review', {'task': 'now'})

    async def test_invalid_manifests_and_unknown_tool_overrides_fail(self):
        self.manifest('resources.json', [{'name': 'missing', 'uri': 'ai-bridge://x', 'path': 'missing.md'}])
        with self.assertRaisesRegex(ValueError, 'does not exist'):
            HostConfig(self.root)
        (self.root / 'file.md').write_text('test')
        row = {'name': 'same', 'uri': 'ai-bridge://same', 'path': 'file.md'}
        self.manifest('resources.json', [row, row])
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            HostConfig(self.root)
        self.manifest('resources.json', [{**row, 'unexpected': 'field'}])
        with self.assertRaisesRegex(ValueError, 'Invalid'):
            HostConfig(self.root)
        self.manifest('resources.json', [])
        folder = self.root / 'tool-descriptions'
        folder.mkdir()
        (folder / 'typo.md').write_text('Unknown tool')
        with self.assertRaisesRegex(ValueError, 'unknown tools'):
            HostConfig(self.root).register(MCPServer('test'), lambda text: text)

    async def test_retired_tool_override_warns_instead_of_failing(self):
        import contextlib, io
        folder = self.root / 'tool-descriptions'
        folder.mkdir()
        (folder / 'agent_wait.md').write_text('Old guidance')
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            HostConfig(self.root).register(MCPServer('test'), lambda text: text)
        self.assertIn('agent_wait was replaced by job_status', errors.getvalue())
