"""Lazy boundary around optional coding-agent support; no CLI work at import time."""

import importlib
import os


class AgentUnavailable(RuntimeError):
    pass


def _load():
    try:
        return importlib.import_module('.agents', __package__)
    except Exception as error:
        raise AgentUnavailable(f'Coding-agent helpers unavailable: {error}') from error


class OptionalAgents:
    AGENTS = ('claude', 'codex', 'cursor')
    PERMISSIONS = ('full', 'edit', 'readonly')

    def __getattr__(self, name):
        if name.startswith('_'):
            raise AttributeError(name)

        def call(*args, **kwargs):
            agent = args[0] if args else kwargs.get('agent')
            disabled = {value.strip() for value in os.environ.get('AI_BRIDGE_DISABLED_AGENTS', '').split(',')}
            if isinstance(agent, str) and agent in self.AGENTS and agent in disabled:
                raise AgentUnavailable(f'{agent} adapter is disabled by AI_BRIDGE_DISABLED_AGENTS')
            return getattr(_load(), name)(*args, **kwargs)
        return call


agents = OptionalAgents()
