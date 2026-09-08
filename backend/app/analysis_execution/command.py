"""Explicit, non-shell Claude Code invocation configuration."""

import math
import re
import subprocess
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from app.analysis_packets.codec import json_bytes


@dataclass(frozen=True)
class CliRequest:
    executable: Path
    cwd: Path
    session_id: str
    attempt_id: str
    model: str
    prompt: str
    budget_usd: Decimal
    timeout_seconds: float
    max_output_bytes: int
    resume: bool
    agents: dict | None = None
    create_agent: bool = False
    bare: bool = True
    direct_session: bool = False


def build_command(request: CliRequest) -> list[str]:
    for identity in (request.session_id, request.attempt_id):
        if not isinstance(identity, str) or str(UUID(identity)) != identity:
            raise ValueError("invalid_execution_identity")
    for path, is_directory in ((request.executable, False), (request.cwd, True)):
        if not isinstance(path, Path) or not path.is_absolute():
            raise ValueError("absolute_execution_path_required")
        if not (path.is_dir() if is_directory else path.is_file()):
            raise ValueError("execution_path_missing")
    if (not isinstance(request.budget_usd, Decimal) or not request.budget_usd.is_finite()
            or request.budget_usd <= 0):
        raise ValueError("invalid_execution_budget")
    if (type(request.timeout_seconds) not in (int, float)
            or not math.isfinite(request.timeout_seconds) or request.timeout_seconds <= 0
            or type(request.max_output_bytes) is not int or request.max_output_bytes <= 0):
        raise ValueError("invalid_execution_limits")
    if (type(request.resume) is not bool or not isinstance(request.model, str)
            or not request.model.strip() or request.model.startswith("-")
            or any(ord(c) < 32 for c in request.model)):
        raise ValueError("invalid_execution_config")
    if (not isinstance(request.prompt, str) or not request.prompt.strip()
            or len(request.prompt.encode("utf-8")) > 10 * 1024 * 1024):
        raise ValueError("invalid_execution_prompt")
    if type(request.create_agent) is not bool or type(request.bare) is not bool:
        raise ValueError("invalid_execution_config")
    if type(request.direct_session) is not bool:
        raise ValueError("invalid_execution_config")
    if request.direct_session and (request.agents is not None or request.create_agent
                                   or not request.bare):
        raise ValueError("direct_session_config_invalid")
    if request.create_agent and request.agents is None:
        raise ValueError("agent_definitions_required")
    definitions = request.agents
    if definitions is not None:
        if not isinstance(definitions, dict) or len(definitions) != 1:
            raise ValueError("invalid_agent_definitions")
        for name, definition in definitions.items():
            if (not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", name)
                    or not isinstance(definition, dict)
                    or set(definition) != {"description", "prompt", "tools", "model"}
                    or definition["tools"] != ["Read"] or definition["model"] != "inherit"
                    or any(not isinstance(definition[k], str) or not definition[k].strip()
                           for k in ("description", "prompt"))):
                raise ValueError("invalid_agent_definitions")
    tool_set = ("Read,SendMessage" if not request.create_agent
                else "Read,Agent,SendMessage")
    if request.direct_session:
        tool_set = "Read"
    argv = [
        str(request.executable), "-p", "--output-format", "stream-json",
        "--verbose", "--forward-subagent-text",
        "--resume" if request.resume else "--session-id", request.session_id,
        "--model", request.model, "--max-budget-usd", str(request.budget_usd),
        "--permission-prompts", "none", "--disable-slash-commands",
        "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
        "--tools", tool_set, "--allowedTools", tool_set,
    ]

    if request.direct_session:
        argv.remove("--forward-subagent-text")
    if request.bare:
        argv.append("--bare")
    else:
        argv.extend(["--setting-sources", "", "--settings", '{"disableAllHooks":true}',
                     "--system-prompt-snapshot", "off"])
    if definitions is not None:
        argv.extend(["--agents", json_bytes(definitions).decode("utf-8").strip()])
    if len(subprocess.list2cmdline(argv)) > 30000:
        raise ValueError("command_too_long")
    return argv
