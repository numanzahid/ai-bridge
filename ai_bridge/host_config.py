"""Host-edited MCP metadata, text resources, and prompt templates."""

from __future__ import annotations

import argparse
import mimetypes
import os
from pathlib import Path
import re
import socket
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.prompts.base import Prompt, PromptArgument

from .catalog import config_dir

DEFAULTS = Path(__file__).with_name("host_defaults")
VARIABLE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


class Definition(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ResourceDefinition(Definition):
    uri: str = Field(min_length=1)
    name: str = Field(min_length=1)
    path: str = Field(min_length=1)
    title: str | None = None
    description: str | None = None
    mime_type: str | None = None


class ArgumentDefinition(Definition):
    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    description: str | None = None
    required: bool = False


class MessageDefinition(Definition):
    role: Literal["user", "assistant"] = "user"
    path: str = Field(min_length=1)


class PromptDefinition(Definition):
    name: str = Field(min_length=1)
    title: str | None = None
    description: str | None = None
    arguments: list[ArgumentDefinition] = Field(default_factory=list)
    messages: list[MessageDefinition] = Field(min_length=1)


def default_instructions() -> str:
    text = (DEFAULTS / "server-instructions.md").read_text(encoding="utf-8")
    return text.replace("{hostname}", socket.gethostname()).replace("{user}", os.environ.get("USER") or Path.home().name)


class HostConfig:
    def __init__(self, root: Path | None = None):
        self.root = root if root is not None else config_dir()
        self._registered_tools: set[str] = set()
        instruction_file = self.root / "server-instructions.md"
        self.instructions = instruction_file.read_text(encoding="utf-8") if instruction_file.exists() else default_instructions()
        self.tool_descriptions = {
            p.stem: p.read_text(encoding="utf-8").strip()
            for p in sorted((self.root / "tool-descriptions").glob("*.md"))
        }
        for name, description in self.tool_descriptions.items():
            if not description:
                raise ValueError(f"Empty tool description: {self.root / 'tool-descriptions' / (name + '.md')}")
        self.resources = self._load("resources.json", ResourceDefinition)
        self.prompts = self._load("prompts.json", PromptDefinition)
        self._unique([r.uri for r in self.resources], "resource URI")
        self._unique([r.name for r in self.resources], "resource name")
        self._unique([p.name for p in self.prompts], "prompt name")
        for resource in self.resources:
            parsed = urlsplit(resource.uri)
            if not parsed.scheme or "{" in resource.uri or "}" in resource.uri:
                raise ValueError(f"Resource URI must be an absolute, static URI: {resource.uri}")
            self.file(resource.path)
        for prompt in self.prompts:
            self._unique([a.name for a in prompt.arguments], f"argument in prompt {prompt.name}")
            for message in prompt.messages:
                self.file(message.path)

    def _load(self, name: str, model):
        path = self.root / name
        if not path.exists():
            return []
        try:
            return TypeAdapter(list[model]).validate_json(path.read_text(encoding="utf-8"))
        except ValueError as error:
            raise ValueError(f"Invalid {path}: {error}") from error

    @staticmethod
    def _unique(values: list[str], label: str):
        if len(values) != len(set(values)):
            raise ValueError(f"Duplicate {label} in host configuration")

    def file(self, value: str) -> Path:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = self.root / path
        if not path.is_file():
            raise ValueError(f"Configured file does not exist: {path}")
        return path

    def tool(self, server: MCPServer, redact=lambda text: text, **options):
        def register(fn):
            name = options.get("name") or fn.__name__
            self._registered_tools.add(name)
            description = self.tool_descriptions.get(name)
            return server.tool(description=redact(description) if description is not None else None, **options)(fn)
        return register

    def register(self, server: MCPServer, redact):
        unknown = set(self.tool_descriptions) - self._registered_tools
        if unknown:
            raise ValueError(f"Tool descriptions reference unknown tools: {sorted(unknown)}")
        for resource in self.resources:
            self._resource(server, resource, redact)
        for prompt in self.prompts:
            self._prompt(server, prompt, redact)

    def _resource(self, server: MCPServer, spec: ResourceDefinition, redact):
        path = self.file(spec.path)

        def read() -> str:
            return redact(path.read_text(encoding="utf-8"))

        server.resource(spec.uri, name=spec.name, title=spec.title, description=redact(spec.description) if spec.description else None,
                        mime_type=spec.mime_type or mimetypes.guess_type(path.name)[0] or "text/plain")(read)

    def _prompt(self, server: MCPServer, spec: PromptDefinition, redact):
        messages = [(message.role, self.file(message.path)) for message in spec.messages]
        declared = {argument.name for argument in spec.arguments}

        def render(**arguments):
            unknown = set(arguments) - declared
            if unknown:
                raise ValueError(f"Unknown prompt arguments: {sorted(unknown)}")
            if any(not isinstance(value, str) for value in arguments.values()):
                raise ValueError("Prompt arguments must be strings")
            rendered = []
            for role, path in messages:
                text = path.read_text(encoding="utf-8")
                undeclared = set(VARIABLE.findall(text)) - declared
                if undeclared:
                    raise ValueError(f"Undeclared placeholders in {path}: {sorted(undeclared)}")
                text = VARIABLE.sub(lambda match: arguments.get(match[1], ""), text)
                rendered.append({"role": role, "content": {"type": "text", "text": redact(text)}})
            return rendered

        server.add_prompt(Prompt(
            name=spec.name, title=spec.title, description=redact(spec.description) if spec.description else None,
            arguments=[PromptArgument(name=a.name, description=a.description, required=a.required) for a in spec.arguments],
            fn=render,
        ))


def initialize(root: Path | None = None):
    root = root if root is not None else config_dir()
    for directory in ("instructions", "tool-descriptions", "resources", "prompts"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    for source in sorted(DEFAULTS.rglob("*")):
        if not source.is_file():
            continue
        destination = root / source.relative_to(DEFAULTS)
        text = default_instructions() if source.name == "server-instructions.md" else source.read_text(encoding="utf-8")
        try:
            with destination.open("x", encoding="utf-8") as stream:
                stream.write(text)
        except FileExistsError:
            pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--init", action="store_true", help="Create editable defaults without overwriting existing files")
    actions.add_argument("--check", action="store_true", help="Validate host manifests and configured file paths")
    args = parser.parse_args()
    if args.init:
        initialize()
    host = HostConfig()
    print(f"Host configuration: {host.root}; {len(host.resources)} resources, {len(host.prompts)} prompts, {len(host.tool_descriptions)} tool overrides")


if __name__ == "__main__":
    main()
